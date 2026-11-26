"""Lower a resolved Sophios compilation into an executable Nextflow workflow."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Any, Final

from ..ir.complete import coerce_job_value
from ..ir.refusals import Construct, NotLowered, Refusal
from ..ir.resolved import (
    Crossing, InputRef, ResolvedStep, ResolvedWorkflow, Through, WorkflowInput, WorkflowOutput, output_sources,
    output_unbound, spelling, tool_steps,
)
from ..ir.types import AuthoredName
from ..lang.diagnostics import Diagnostic, SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..lang.subset import Expr
from ..lang.tools import ToolNode
from ..lang.types import PortDeclaration, ScatterMethod, lists, read_type, without_null
from ..lang.values import write_value
from .analyze import (
    _Step, _WhenError, _absent_optional_findings, _call_findings, _carried, _condition_value,
    _conditional_consumer_findings, _json_value, _param_type, _scatter_findings, _staged_name_findings,
    _step_input_findings, _text_capture_sink_findings, _wired, _workflow_capability_findings, _workflow_params,
    written_link,
)
from .model import (
    ARRAY_REFERENCE, ExecutableNextflowWorkflow, NfConnection, NfPort, NfProcess, NfProcessConnection,
    NfStepInputConnection, NfWorkflowInputConnection, NfWorkflowOutputConnection, absent_path_problems,
    condition_without_inputs, endpoint_problems, incoming_connections, merge_of, optional_output_problems,
    reference_problems, staged_write_problems, topological_order, with_scalar_types,
)
from .tool import (
    ABSENT_VAL_SENTINEL, _Finding, _Tool, _Unsupported, _command, _container, _cuda_notes, _generated_files,
    _identifier, _iwdr_entries, _normalized_identifiers, _ports, _resources, _tool_capability_findings,
    local_paths,
)


#: The code of each reference rule (`model.reference_problems`) a tool's command or globs can break: an array
#: read as one value; a reference to an input the tool does not declare, which is no expression the target lowers.
_REFERENCE_CODES: Final = {ARRAY_REFERENCE: SophiosErrorCode.NF_ARRAY_BINDING,
                           "unknown": SophiosErrorCode.NF_EXPRESSION}


def _process(step: _Step) -> tuple[NfProcess, Expr | None]:
    """A step's process, before the workflow types its inputs, and its condition, which needs them typed."""
    name = _identifier(step.node.name, variable=False)
    inputs = _ports(step.tool.inputs, outputs=False, entries=_iwdr_entries(step.tool))
    outputs = _ports(step.tool.outputs, outputs=True, body=step.tool.body)
    command, container, resources = _command(step.tool), _container(step.tool), _resources(step.tool)
    condition = _condition_value(step) if step.node.condition is not None else None
    # A carried input is a val, staging nothing, and a list where what it delivers is one.
    carried = [NfPort(_identifier(port.name), "val", is_array=lists(port.type))
               for port, _sources in step.bound() if port.declared is None]
    if broken := reference_problems(name, inputs, carried, outputs, command):
        rule, problem = broken[0]
        raise _Unsupported(_REFERENCE_CODES.get(rule, SophiosErrorCode.NF_INVALID_CWL), problem)
    if problems := staged_write_problems(name, inputs, outputs, command):
        raise _Unsupported(SophiosErrorCode.NF_WRITE_THROUGH_STAGED, problems[0])
    return NfProcess(name, inputs, outputs, command, container, resources, step_only_inputs=carried,
                     files=_generated_files(step.tool)), condition


def _typed(processes: list[NfProcess], connections: list[NfConnection], param_types: Mapping[str, str],
           conditions: Mapping[str, Expr]) -> tuple[list[NfProcess], dict[str, _Unsupported]]:
    """Each process with its inputs' scalar types and its condition, in its place; and why each process
    that is not, is not.

    A process is typed after the processes it reads, since what an output gives depends on its process's condition.
    """
    incoming = incoming_connections(connections)
    position = {process.name: index for index, process in enumerate(processes)}
    done: dict[str, NfProcess] = {}
    failed: dict[str, _Unsupported] = {}
    for name in topological_order(position, connections):
        done[name] = typed = with_scalar_types(processes[position[name]], incoming, param_types, done)
        if (condition := conditions.get(name)) is None:
            continue
        if (problem := condition_without_inputs(name, typed.all_inputs)) is not None:
            failed[name] = _Unsupported(SophiosErrorCode.NF_WHEN_WITHOUT_INPUTS, problem)
            continue
        try:
            done[name] = replace(typed, condition=condition)
        except ValueError as error:  # the condition does not type as a boolean over its typed inputs
            failed[name] = _Unsupported(SophiosErrorCode.NF_WHEN_SUBSET, str(error))
    return [done[process.name] for process in processes], failed


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source)
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process)
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port)


def _step_connections(
    steps: tuple[_Step, ...],
    processes: list[NfProcess],
    step_names: Mapping[str, str],
    defaults: Mapping[tuple[str, str], NfWorkflowInputConnection],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    # Only a multi-input scatter carries the invocation index a gather sorts by.
    multi_scattered = {process.name for step, process in zip(steps, processes, strict=True)
                       if step.node.scatter is not None}
    for step, process in zip(steps, processes, strict=True):
        scattered = {_identifier(raw_name) for raw_name in step.scattered}
        # Capability analysis already admitted only an approved method for two or more; at one scattered input the
        # three methods coincide.
        method = step.node.scatter.method if step.node.scatter is not None else None
        scatter_method = method.value if isinstance(method, ScatterMethod) and len(scattered) > 1 else "dotproduct"
        for port, found in step.bound():
            destination_port = _identifier(port.name)
            spelled = _wired(port, found)
            if spelled is None:
                raise ValueError(f"step input {process.name}.{destination_port} must define source")
            sources: list[NfWorkflowInputConnection | NfProcessConnection] = []
            for source in spelled:
                source_process, source_port = _source_endpoint(source, step_names)
                if source_process is None:
                    # Every scatter is indexed, so its gather keeps input order.
                    adapter = scatter_method if destination_port in scattered else None
                    sources.append(NfWorkflowInputConnection(source_port, process.name, destination_port, adapter))
                else:
                    gathers = source_process in multi_scattered
                    sources.append(NfProcessConnection(
                        source_process, source_port, process.name, destination_port,
                        (scatter_method if destination_port in scattered else "gather") if gathers else None,
                    ))
            # Capability analysis admitted only the step inputs the idioms lower.
            written_merge, pick_value = written_link(port)
            link_merge = merge_of(written_merge, sources)
            if sources and port.has_default:
                # The default takes the place of a null source: the first value that is not null.
                sources.append(defaults[(process.name, destination_port)])
                link_merge, pick_value = "merge_nested", "first_non_null"
            if link_merge is None:
                connections.extend(sources)
            else:
                connections.append(NfStepInputConnection(tuple(sources), str(link_merge),
                                                         None if pick_value is None else str(pick_value)))
    return connections


def _workflow_output_connections(outputs: tuple[tuple[WorkflowOutput, tuple[Through | Crossing, ...]], ...],
                                 step_names: Mapping[str, str]) -> list[NfConnection]:
    """The connection into each workflow output from each of its sources. `_source_text_findings` has refused an
    output with no source and one whose source is inside a call; text that names no step's output stops here."""
    connections: list[NfConnection] = []
    for port, found in outputs:
        destination_port = _identifier(port.name)
        sources = [port.source] if isinstance(port.source, str) else [
            spelling(source.ref) for source in found if isinstance(source, Through)]
        for source in sources:
            source_process, source_port = _source_endpoint(source, step_names)
            if source_process is None:
                raise ValueError(
                    f"workflow output {destination_port!r} directly forwards a workflow input; boundary passthrough "
                    "is not executable on the Nextflow target"
                )
            connections.append(NfWorkflowOutputConnection(source_process, source_port, destination_port))
    return connections


def _connections(
    lowering: "_Lowering",
    processes: list[NfProcess],
    defaults: list[NfWorkflowInputConnection],
) -> list[NfConnection]:
    """Every connection: a default beside a source joins that step input, and every other default is its own."""
    step_names = {step.node.name: process.name for step, process in zip(lowering.steps, processes, strict=True)}
    step_connections = _step_connections(
        lowering.steps, processes, step_names,
        {(default.to_process, default.to_port): default for default in defaults},
    )
    joined = {
        (connection.to_process, connection.to_port)
        for connection in step_connections if isinstance(connection, NfStepInputConnection)
    }
    return [
        *step_connections,
        *_workflow_output_connections(lowering.outputs, step_names),
        *(default for default in defaults if (default.to_process, default.to_port) not in joined),
    ]


def _default_bindings(
    steps: tuple[_Step, ...],
    processes: list[NfProcess],
    workflow_inputs: Iterable[str],
) -> tuple[list[NfWorkflowInputConnection], dict[str, Any], dict[str, str]]:
    """Synthesize a workflow parameter for every default, and a connection for every unwired process input.

    Covers three cases: a step input's default, a declared CWL default on an unwired input, and an unwired optional
    input with no default. Capability analysis has already proven any remaining unwired, no-default optional input
    is a safe absence, so this always contributes a null value for those rather than re-deriving safety. A step
    input's default beside a source is one of that step input's sources (``_step_connections``), so it gets a
    parameter and no connection of its own.

    Each parameter is made as the compiler makes a name, `<step>~<port>`, and normalized; one that normalizes like
    another or like a workflow input is rejected, naming both.
    """
    made: list[tuple[str, str, str, Any, str]] = []
    for step, process in zip(steps, processes, strict=True):
        bound = {port.name: port for port, _sources in step.bound()}
        made.extend((f"{step.node.name}~{port.name}", process.name, _identifier(port.name),
                     _json_value(port.default), _param_type(port.type))
                    for port in bound.values() if port.declared is None and port.has_default)
        for raw_name, declared in step.tool.inputs.items():
            port = bound.get(raw_name)
            if port is not None and port.has_default:
                value = _json_value(port.default)
            elif port is not None:
                continue
            elif declared.default is not None:
                value = _json_value(declared.default)
            elif without_null(declared.type)[1]:
                # Capability analysis has already proven this unwired, no-default optional input is a safe absence.
                # _apply_absent_sentinel converts None to the runtime-proven [] representation once, at the end of
                # the conversion.
                value = None
            else:
                continue
            port_name = _identifier(raw_name)
            made.append((f"{step.node.name}~{raw_name}", process.name, port_name, value,
                         _param_type(declared.type)))
    names = _normalized_identifiers([*workflow_inputs, *(name for name, *_ in made)], context="workflow parameter")
    return (
        [NfWorkflowInputConnection(names[name], process, port) for name, process, port, *_ in made],
        {names[name]: value for name, _process, _port, value, _type in made},
        {names[name]: param_type for name, *_, param_type in made},
    )


def _workflow_param_types(workflow: ResolvedWorkflow) -> dict[str, str]:
    """Each workflow input's declared type, by parameter name."""
    names = _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    return {names[port.name]: _param_type(port.declaration.type)
            for port in workflow.inputs}


def _apply_absent_sentinel(params: Mapping[str, Any]) -> dict[str, Any]:
    """Replace every remaining null parameter with the runtime-proven [] sentinel.

    Capability analysis (via _absent_optional_findings, which itself calls _workflow_params and depends on None to
    detect absence) has already proven any null still present here is a safe absent-optional val input. This
    substitution happens only once, after that analysis, so it never interferes with it.
    """
    return {name: (ABSENT_VAL_SENTINEL if value is None else value) for name, value in params.items()}


@dataclass(frozen=True, slots=True)
class _Lowering:
    """A view as the lowering reads it: its tool steps, each tool on the lowering's own copy, and its outputs, each
    source resolved through call boundaries."""

    view: ResolvedWorkflow
    steps: tuple[_Step, ...]
    #: What the target refuses of each tool the standard's loader refuses.
    refused: tuple[_Finding, ...]
    outputs: tuple[tuple[WorkflowOutput, tuple[Through | Crossing, ...]], ...]
    #: The (call, input) pairs a kept output relays that the call leaves unbound, with no default.
    unbound: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, view: ResolvedWorkflow) -> "_Lowering":
        """The lowering's reading of `view`."""
        steps: list[_Step] = []
        refused: list[_Finding] = []
        for found in tool_steps(view):
            if not isinstance(run := found.step.run, ToolNode):
                continue
            try:
                steps.append(_Step(found, _Tool.of(run, found.step.inherited), _carried(found)))
            except SophiosError as error:  # one refusal for a tool the standard's loader refuses, at its step
                refused.append(_Finding(SophiosErrorCode.NF_INVALID_CWL, ".run",
                                        next(iter(error.diagnostics)).message, found.step))
        return cls(view, tuple(steps), tuple(refused), tuple(zip(view.outputs, output_sources(view), strict=True)),
                   tuple(dict.fromkeys(pair for pairs in output_unbound(view) for pair in pairs)))


_PATH_FIELD = re.compile(r"\.(?P<field>[^.\[]+)(?P<rest>.*)")


def _parts(path: str) -> tuple[str, str]:
    """The first field of a path and the rest of it; nothing for a path that names no field."""
    found = _PATH_FIELD.fullmatch(path)
    return (found["field"], found["rest"]) if found else ("", "")


def _with_field(rest: str, reason: str) -> str:
    """`reason`, after the field the rest of a path names, unless the reason names it first."""
    field = rest.removeprefix(".")
    return reason if not field or reason.startswith(field.rsplit(".", maxsplit=1)[-1]) else f"{field}: {reason}"


def _step_named(workflow: ResolvedWorkflow, name: str) -> ResolvedStep | None:
    """The step of `workflow`, at any depth, whose qualified name is `name`."""
    for step in workflow.steps:
        if step.name == name:
            return step
        if isinstance(step.run, ResolvedWorkflow) and (inner := _step_named(step.run, name)) is not None:
            return inner
    return None


def _workflow_refusal(workflow: ResolvedWorkflow, finding: _Finding, *, root: str, step: str | None) -> Refusal:
    """The refusal of a construct of `workflow`, the root or the workflow the step named `step` calls, at the
    finding's path."""
    field, after = _parts(finding.path)
    name, within = _parts(after)
    construct, subject, span, text = Construct.WORKFLOW, None, workflow.span, _with_field(finding.path, finding.text)
    if field in ("inputs", "outputs"):
        construct = Construct.WORKFLOW_INPUT if field == "inputs" else Construct.WORKFLOW_OUTPUT
        subject, text = name or None, _with_field(within, finding.text)
        port = next((port for port in (workflow.inputs if field == "inputs" else workflow.outputs)
                     if port.name == name), None)
        made = port.made_from if isinstance(port, WorkflowInput) else None
        if made is not None and (maker := _step_named(workflow, made[0])) is not None:
            # A made input has no line of its own: its value was written on the step input it was made from.
            step = maker.name
            span = next((each.span for each in maker.inputs if each.name == made[1]), None) or maker.span
        elif port is not None:
            span = port.span or workflow.span
    elif field in ("requirements", "hints"):
        construct, subject, text = Construct.REQUIREMENT, name or None, _with_field(within, finding.text)
    elif field == "steps":
        construct, text = Construct.STEP, finding.text
    return Refusal(finding.code, construct, subject, root, step, span, text)


def _step_refusal(node: ResolvedStep, finding: _Finding, *, root: str) -> Refusal:
    """The refusal of `node` or a construct of it, at the finding's path."""
    field, after = _parts(finding.path)
    if isinstance(node.run, ResolvedWorkflow) and field == "run":
        return _workflow_refusal(node.run, replace(finding, path=after), root=root, step=node.name)
    name, within = _parts(after)
    construct = Construct.CALL if isinstance(node.run, ResolvedWorkflow) else Construct.STEP
    subject, span, text = None, node.span, _with_field(finding.path, finding.text)
    match field:
        case "when":
            construct, text = Construct.WHEN, _with_field(after, finding.text)
        case "scatter" | "scatterMethod":
            construct, text = Construct.SCATTER, finding.text
        case "in":
            construct, subject, text = Construct.STEP_INPUT, name or None, _with_field(within, finding.text)
            span = next((port.span for port in node.inputs if port.name == name), None) or node.span
        case "run":
            field_path = after.removeprefix(".")
            construct, text = Construct.TOOL, finding.text
            tool_file = Path(node.run.run_path).name if isinstance(node.run, ToolNode) else node.name
            subject = f"{tool_file}: {field_path}" if field_path else tool_file
        case "requirements" | "hints":
            construct, subject, text = Construct.REQUIREMENT, name or None, _with_field(within, finding.text)
    return Refusal(finding.code, construct, subject, root, node.name, span, text)


def _unbound_output_findings(lowering: _Lowering) -> list[_Finding]:
    """A finding for each input a call leaves unbound, with no default, that a kept workflow output relays.

    Such an output has no source to follow, so the input is refused once, at the call, however many outputs relay it
    and through whatever chain, not dropped.
    """
    reason = "a workflow output relays it, but nothing binds it and it has no default"
    return [_Finding(SophiosErrorCode.NF_FORWARDED_INPUT, f".in.{name}", reason, _step_named(lowering.view, call))
            for call, name in lowering.unbound]


def _refusal(finding: _Finding, view: ResolvedWorkflow) -> Refusal:
    """A finding as the refusal of the construct at its path: it names the step and the construct by the names their
    author wrote, at the author's line."""
    if finding.step is not None:
        return _step_refusal(finding.step, finding, root=view.name)
    return _workflow_refusal(view, finding, root=view.name, step=None)


def _at_step(finding: _Finding, step: _Step) -> _Finding:
    """A finding of a step's tool, at the step; one about an entry the tool inherits, at the step's requirement of
    its class: the author wrote it outside the tool's file."""
    for place, cls in step.tool.inherited.items():
        written = f".run.{place}"
        if finding.path == written or finding.path.startswith(f"{written}."):
            section = place.split(".", maxsplit=1)[0]
            return replace(finding, path=f".{section}.{cls}{finding.path.removeprefix(written)}", step=step.node)
    return replace(finding, step=step.node)


def _not_lowered(findings: Iterable[_Finding], view: ResolvedWorkflow) -> NotLowered:
    """What the target refuses of the view: the refusal of each finding."""
    return NotLowered(tuple(_refusal(finding, view) for finding in findings))


def _analysis(lowering: _Lowering) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
    """The workflow each tool step of the view lowers into, and the notes made on the way.

    Under partial failure the policy is the one refusal: it is the CWL target's, and the tools the view carries are
    not edited for it.

    Raises:
        NotLowered: Every construct of the view the target cannot lower.
    """
    view, steps = lowering.view, lowering.steps
    if view.policy.partial_failure:
        raise NotLowered((Refusal(SophiosErrorCode.NF_PARTIAL_FAILURE, Construct.POLICY, "partial failure",
                                  view.name, None, view.span, "partial failure is a policy of the CWL target"),))
    findings = _call_findings(view)
    notes: list[Diagnostic] = []
    processes: list[NfProcess] = []
    conditions: dict[str, Expr] = {}

    for step in steps:
        if step_findings := [_at_step(finding, step)
                             for finding in _tool_capability_findings(step.tool)]:
            findings.extend(step_findings)
            continue
        try:
            process, condition = _process(step)
            processes.append(process)
            if condition is not None:
                conditions[process.name] = condition
        except _WhenError as error:
            findings.append(_Finding(error.code, ".when", str(error), step.node))
        except _Unsupported as error:  # what lowering the tool finds, which no source pass reads
            findings.append(_Finding(error.code, "", str(error), step.node))
        notes.extend(_cuda_notes(step.node.name, step.tool))
    findings.extend(lowering.refused)
    findings.extend(_workflow_capability_findings(view, steps))
    findings.extend(_unbound_output_findings(lowering))
    findings.extend(_absent_optional_findings(view, steps))
    findings.extend(_conditional_consumer_findings(steps))
    findings.extend(_scatter_findings(view, steps))
    findings.extend(_step_input_findings(view, steps))
    findings.extend(_text_capture_sink_findings(steps))
    findings.extend(_staged_name_findings(steps))
    findings.extend(_source_text_findings(lowering))
    if findings:
        raise _not_lowered(findings, view)
    return _assembled(lowering, processes, conditions), tuple(notes)


def _assembled(lowering: _Lowering, processes: list[NfProcess],
               conditions: Mapping[str, Expr]) -> ExecutableNextflowWorkflow:
    """The workflow the processes make, once every source pass has admitted the view.

    Raises:
        NotLowered: Each error the lowering or the model would raise, of the step it is about where there is one.
    """
    view = lowering.view
    params = _workflow_params(view)
    try:
        default_connections, default_params, default_types = _default_bindings(
            lowering.steps, processes, [port.name for port in view.inputs])
    except _Unsupported as error:  # a parameter made for a default shares a name with another
        raise _not_lowered([_Finding(error.code, "", str(error))], view) from None
    params.update(default_params)
    param_types = {**_workflow_param_types(view), **default_types}
    connections = _connections(lowering, processes, default_connections)
    processes, failed = _typed(processes, connections, param_types, conditions)
    if failed:
        raise _not_lowered((
            _Finding(failed[process.name].code, ".when" if process.name in conditions else "",
                     str(failed[process.name]), step.node)
            for step, process in zip(lowering.steps, processes, strict=True) if process.name in failed), view)
    name = _identifier(view.name, variable=False)
    while name in {process.name for process in processes}:  # a step may share the workflow's name
        name = f"_{name}"
    # A default is as authored; each value is held as a value written on a step is (design §4.2, Losses): coerced
    # to its declared type, so an int given for a float is the float, and a local location a path.
    values = _apply_absent_sentinel({key: write_value(local_paths(coerce_job_value(
        AuthoredName(key), PortDeclaration(read_type(param_types[key])), value))) for key, value in params.items()})
    problems = {SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP: endpoint_problems(processes, connections),
                SophiosErrorCode.NF_ABSENT_OPTIONAL: absent_path_problems(values, param_types),
                SophiosErrorCode.NF_OPTIONAL_OUTPUT: optional_output_problems(processes, connections)}
    if refused := [_Finding(code, "", problem) for code, found in problems.items() for problem in found]:
        raise _not_lowered(refused, view)
    return ExecutableNextflowWorkflow(name=name, processes=processes, connections=connections, params=values,
                                      param_types=param_types)


def _names_a_step(workflow: ResolvedWorkflow, source: str) -> bool:
    """Whether `source`, `step/port` text, names a step of `workflow` by its id there."""
    step, _, _port = source.rpartition("/")
    return any(each.local == step for each in workflow.steps)


def _source_text_findings(lowering: "_Lowering") -> list[_Finding]:
    """Each source written as `step/port` text that names no tool step the lowering makes a process of, and each
    workflow output the target cannot connect: one with no source, or one that forwards a workflow input.

    Sophios keeps text it could not resolve as it was written. Text naming a step of its
    workflow, a nested workflow's call, is valid CWL the target does not follow; text naming no step is the
    document's mistake.
    """
    view = lowering.view
    processes = {step.node.name for step in lowering.steps}
    findings: list[_Finding] = []

    def text(source: str, workflow: ResolvedWorkflow) -> None:
        if "/" in source and (process := source.rpartition("/")[0]) not in processes:
            code = (SophiosErrorCode.NF_SOURCE_AS_TEXT if _names_a_step(workflow, source)
                    else SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP)
            findings.append(_Finding(code, "", f"connection references unknown source process {process!r}"))

    for step in lowering.steps:
        level = step.view.calls[-1].run if step.view.calls else view
        assert isinstance(level, ResolvedWorkflow)
        for port, _ in step.bound():
            if port.expression is not None:
                text(port.expression, level)
    for output, found in lowering.outputs:
        if output.source is None:
            findings.append(_Finding(SophiosErrorCode.NF_OUTPUT_WITHOUT_SOURCE,
                                     "", f"workflow output {output.name!r} must define outputSource"))
        elif isinstance(output.source, str):
            text(output.source, view)
        elif crossings := [each for each in found if isinstance(each, Crossing)]:
            call = _step_named(view, crossings[0].call)
            child = call.run if call is not None and isinstance(call.run, ResolvedWorkflow) else None
            inner = next((each.source for each in child.outputs if each.name == crossings[0].port), None) \
                if child is not None else None
            code = (SophiosErrorCode.NF_SOURCE_AS_TEXT if isinstance(inner, str) and child is not None
                    and _names_a_step(child, inner) else SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP)
            findings.append(_Finding(code, "", f"workflow output {output.name!r} names no step output the "
                                     "target can follow"))
        elif any(isinstance(each, Through) and isinstance(each.ref, InputRef) for each in found):
            findings.append(_Finding(SophiosErrorCode.NF_FORWARDED_INPUT,
                                     "", f"workflow output {output.name!r} directly forwards a workflow input; "
                                     "boundary passthrough is not executable on the Nextflow target"))
    return findings


class NextflowTarget:  # pylint: disable=too-few-public-methods
    """The Nextflow target: it lowers a resolved view to the executable Nextflow model (design §7)."""

    def lower(self, view: ResolvedWorkflow) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
        """Lower `view` without repeating core semantics.

        Every unsupported source semantic, including one that only lowering or model validation detects, is
        refused at its author's line, with its code, and aggregated with the rest before the workflow is assembled.

        Args:
            view (ResolvedWorkflow): The resolved view of a finished compilation (`compiler.resolved_view`).

        Raises:
            NotLowered: If there is any refusal; it holds every one.

        Returns:
            tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]: The validated executable representation, and
                the notes the lowering made, such as a CUDA field it does not check.
        """
        return _analysis(_Lowering.of(view))


#: The Nextflow target.
NEXTFLOW: Final = NextflowTarget()


def nextflow_names(view: ResolvedWorkflow) -> dict[str, dict[str, str]]:
    """The Sophios name of each process and workflow output the lowering of `view` makes, by its Nextflow name.

    A process is named for its step's qualified id and a workflow output for its own name, normalized as the
    lowering normalizes them.
    """
    return {
        "processes": {_identifier(tool.step.name, variable=False): tool.step.name
                      for tool in tool_steps(view) if isinstance(tool.step.run, ToolNode)},
        "outputs": {_identifier(port.name): port.name for port in view.outputs},
    }
