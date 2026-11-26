"""Workflow-level Nextflow capability analysis, step wiring, and parameter collection."""

from collections.abc import Iterator, Mapping
import copy
from dataclasses import dataclass, replace
from os import PathLike
import posixpath
from typing import Any

from cwl_utils.parser import cwl_v1_2 as standard

from ..ir.resolved import (
    Crossing, InputRef, ResolvedStep, ResolvedWorkflow, StepInput, Through, ToolStep, boolean_problem, spelling,
)
from ..lang.cwl import LINK_MERGES, PICK_VALUES, SCATTER_METHODS, canonical, required
from ..lang.error_codes import SophiosErrorCode
from ..lang.subset import Expr, is_safe_subset_text, references
from ..lang.types import spell
from .model import PARAM_TYPES, gives_list, merge_of, text_stays_out, wildcard_problem
from .support import FIELDS, consumed
from .tool import (
    _FEATURES, _Finding, _Tool, _Unsupported, _WORKFLOW_CONSUMED_FIELDS, _array_item_type, _declared_findings,
    _default_value_findings, _identifier, _is_array_type, _iwdr_entries, _literal_text, _normalized_identifiers,
    _output_capture, _phase1_value_matches, _qualifier, _referenced_names, _reported, _type_problem,
    _unconsumed_field_findings, cwl_type_to_nf_qualifier, local_locations_as_paths,
)


@dataclass(frozen=True, slots=True)
class _Step:
    """One tool step as the lowering reads it: the view's, its tool as the lowering reads it, and the step-only
    inputs its process carries."""

    view: ToolStep
    tool: _Tool
    carried: frozenset[str]

    @property
    def node(self) -> ResolvedStep:
        """The step itself."""
        return self.view.step

    @property
    def scattered(self) -> tuple[str, ...]:
        """The inputs the step scatters over; none for a step that does not scatter."""
        return self.node.scatter.inputs if self.node.scatter is not None else ()

    def bound(self) -> Iterator[tuple[StepInput, tuple[Through | Crossing, ...]]]:
        """Each step input the process receives and its sources, in `in:` order: those the tool declares, and the
        step-only ones it carries."""
        for port, sources in zip(self.node.inputs, self.view.sources, strict=True):
            if port.declared is not None or port.name in self.carried:
                yield port, sources

    def wired(self) -> Iterator[tuple[StepInput, list[str]]]:
        """Each step input the process receives, of a shape the idioms read, and its sources spelled."""
        for port, sources in self.bound():
            spelled = _wired(port, sources)
            if spelled is not None:
                yield port, spelled


def _idioms(port: StepInput) -> set[str]:
    """The fixed step-input idioms a step input writes."""
    return {name for name, written in (("default", port.has_default), ("linkMerge", port.link_merge is not None),
                                       ("pickValue", port.pick_value is not None)) if written}


_NULLABLE_FIELDS = frozenset({"linkMerge", "pickValue", "valueFrom"})


def _record_fields(port: StepInput) -> set[str]:
    """Every WorkflowStepInput field a step input writes besides its sources: a `linkMerge`, `pickValue` or
    `valueFrom` written as null says nothing."""
    return {name for name, value in port.written if value is not None or name not in _NULLABLE_FIELDS}


def _wired(port: StepInput, sources: tuple[Through | Crossing, ...]) -> list[str] | None:
    """The sources a step input names, spelled, or None for a shape the idioms do not read.

    A raw reference is read as the source it names; a default alone, or with a label, is the value; any other
    field the lowering does not consume is reported by the field pass.
    """
    if port.expression is not None:
        return [port.expression]
    if any(isinstance(found, Crossing) for found in sources):
        return None
    if port.sources:
        if _record_fields(port) - consumed(FIELDS["WorkflowStepInput"]):
            return None
        return [spelling(found.ref) for found in sources if isinstance(found, Through)]
    return [] if port.has_default and _record_fields(port) <= {"default", "label"} else None


def _gives_non_null(port: StepInput) -> bool:
    """Whether a step input's idioms give a value that is not null whatever its sources: a pickValue or a default."""
    return port.pick_value is not None or port.has_default


def _call_findings(workflow: ResolvedWorkflow) -> list[_Finding]:
    """Reject the semantics of a call the flat executable graph would erase.

    The core compiler owns composition, edge resolution, and namespace identity. The backend still owns its
    capability boundary: what a call or its nested workflow says must be consumed here or rejected before the call
    is inlined.
    """
    findings: list[_Finding] = []
    for step in workflow.steps:
        child = step.run
        if not isinstance(child, ResolvedWorkflow):
            continue
        found: list[_Finding] = []
        if step.condition is not None:
            found.append(_Finding(SophiosErrorCode.NF_CONDITIONAL_CALL, ".when",
                                  "a when on a nested-workflow step is not supported; put it on a tool step"))
        if step.scatter is not None:
            found.append(_Finding(SophiosErrorCode.NF_SCATTERED_CALL, ".scatter",
                                  "scatter on a nested workflow step is deferred beyond this lowering; scattering a "
                                  "sub-DAG is not the single-process shape scatter supports"))
        else:
            found.extend(_scatter_method_alone(step))
        found.extend(_own_findings(step, path=""))
        for port in step.inputs:
            input_path = f".in.{port.name}"
            # The inlining joins one source to each input a call binds.
            merging = sorted(_idioms(port))
            if len(port.sources) > 1:
                merging.append("source")
            found.extend(
                _Finding(SophiosErrorCode.NF_CALL_INPUT, f"{input_path}.{name}",
                         "a nested workflow step's input takes one source as it is; merge on a step inside the "
                         "nested workflow")
                for name in merging
            )
            if port.expression is None:
                found.extend(_unconsumed_field_findings(
                    _record_fields(port) - _idioms(port), "WorkflowStepInput",
                    path=input_path))
        found.extend(_own_findings(child, path=".run"))
        for declared in child.inputs:
            port_path = f".run.inputs.{declared.name}"
            found.extend(_declared_findings(declared.declaration, "WorkflowInputParameter", path=port_path))
            # Inlining replaces a nested workflow's input with the calling step's binding, where CWL would apply the
            # input's default to a null one.
            if declared.declaration.has_default and not declared.declaration.shorthand:
                found.append(_Finding(SophiosErrorCode.NF_CALL_INPUT, f"{port_path}.default",
                                      "a nested workflow input's default is not supported: bind the input on the "
                                      "calling step"))
        for output in child.outputs:
            found.extend(_declared_findings(output.declaration, "WorkflowOutputParameter",
                                            path=f".run.outputs.{output.name}", source=output.source))
        findings.extend(replace(each, step=step) for each in found)
        findings.extend(_call_findings(child))
    return findings


def _carried(tool: ToolStep) -> frozenset[str]:
    """The step-only inputs a step's process carries: each one but those fed only by root workflow
    inputs, through every call, that neither the step's `when` nor its `scatter` reads. Such an input cannot change
    what the step does or when it starts, so it is left out."""
    step = tool.step
    read = references(step.condition.expr) if step.condition is not None and step.condition.expr is not None else set()
    read |= set(step.scatter.inputs) if step.scatter is not None else set()
    return frozenset(port.name for port, sources in zip(step.inputs, tool.sources, strict=True)
                     if port.declared is None and (port.name in read or any(
                         not isinstance(found, Through) or not isinstance(found.ref, InputRef) for found in sources)))


def _lists(cwl_type: Any) -> bool:
    """Whether a value of `cwl_type` is a list, null aside."""
    return _is_array_type(required(cwl_type)[0])


def _workflow_capability_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Apply closed-world analysis to the workflow, its steps, and its boundary values."""
    findings = _own_findings(workflow, path="")
    with _reported(findings, ".inputs"):
        _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    for given in workflow.inputs:
        input_path = f".inputs.{given.name}"
        findings.extend(_declared_findings(given.declaration, "WorkflowInputParameter", path=input_path))
        cwl_type = spell(given.declaration.type, given.declaration.unread)
        has_default = given.declaration.has_default and not given.declaration.shorthand
        if has_default:
            findings.extend(_default_value_findings(cwl_type, given.declaration.default, path=input_path))
        if not given.has_value and not has_default and not required(cwl_type)[1]:
            findings.append(_Finding(SophiosErrorCode.NF_INPUT_WITHOUT_VALUE,
                            input_path, "required workflow input value is missing"))
        if given.has_value and given.value is None and not required(cwl_type)[1]:
            findings.append(_Finding(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                            input_path, "explicit null input values are not valid for a required input"))
        elif given.has_value and given.value is None:
            # An explicit null on an optional input is only safe for specific consuming positions;
            # _absent_optional_findings analyzes each consuming step and port for that.
            pass
        elif (problem := _type_problem(cwl_type)) is not None:
            findings.append(_Finding(SophiosErrorCode.NF_TYPE, f"{input_path}.type", problem))
        elif given.has_value and not _phase1_value_matches(cwl_type, local_locations_as_paths(given.value)):
            findings.append(_Finding(SophiosErrorCode.NF_INPUT_VALUE,
                                     input_path, "supplied value does not match its supported CWL type"))
    with _reported(findings, ".outputs"):
        _normalized_identifiers([port.name for port in workflow.outputs], context="workflow output")
    for output in workflow.outputs:
        findings.extend(_declared_findings(output.declaration, "WorkflowOutputParameter",
                                           path=f".outputs.{output.name}", source=output.source))
    with _reported(findings, ".steps"):
        _normalized_identifiers([step.node.name for step in steps], context="workflow step", variable=False)
    for step in steps:
        found = _own_findings(step.node, path="")
        found.extend(_scatter_method_alone(step.node))
        for port in step.node.inputs:
            if port.expression is None:
                found.extend(_unconsumed_field_findings(_record_fields(port),
                                                        "WorkflowStepInput", path=f".in.{port.name}"))
        findings.extend(replace(each, step=step.node) for each in found)
    return findings


def _scatter_method_alone(node: ResolvedStep) -> list[_Finding]:
    """A step that names a scatterMethod and no scatter."""
    if node.scatter is None and "scatterMethod" in dict(node.fields):
        return [_Finding(SophiosErrorCode.NF_SCATTER_METHOD_ALONE, ".scatterMethod",
                         "scatterMethod without scatter is not executable")]
    return []


def _own_findings(node: ResolvedWorkflow | ResolvedStep, *, path: str) -> list[_Finding]:
    """A workflow's or a step's own fields, requirements and hints: a field the lowering does not consume; a feature
    class is consumed in a requirement or hint, an inert declaration with no field of its own; every other class is
    inherited by each tool step inside, and judged there as written on its tool."""
    fields = {*dict(node.fields), *("hints" if requirement.hint else "requirements"
                                    for requirement in node.requirements)}
    if isinstance(node, ResolvedWorkflow):
        owner, kept = "Workflow", _WORKFLOW_CONSUMED_FIELDS
        fields |= {"class", "cwlVersion", "inputs", "outputs", "steps"}
    else:
        owner, kept = "WorkflowStep", None
        fields |= {"id", "in", "out", "run"}
        if node.scatter is not None:
            fields.add("scatter")
            if node.scatter.method is not None:
                fields.add("scatterMethod")
        if node.condition is not None:
            fields.add("when")
    findings = _unconsumed_field_findings(fields, owner, path=path, kept=kept)
    findings += [_Finding(SophiosErrorCode.NF_INVALID_CWL,
                          f"{path}.{section}", f"CWL {owner} {section} must be a mapping or list")
                 for section in ("requirements", "hints") if section in dict(node.fields)]
    for requirement in node.requirements:
        if requirement.cls in _FEATURES:
            findings.extend(_unconsumed_field_findings(
                requirement.body if isinstance(requirement.body, Mapping) else {}, requirement.cls,
                path=f"{path}.{'hints' if requirement.hint else 'requirements'}.{requirement.cls}"))
    return findings


def _scatter_source_findings(step: _Step, raw_name: str, source_types: Mapping[str, Any]) -> list[_Finding]:
    """Require one array-typed workflow-input source for the scattered input.

    The compiler has already refused an element type the scattered port cannot take.
    """
    sources = next((spelled for wired, spelled in step.wired() if wired.name == raw_name), None)
    if sources is None:
        # Every unrecognized source shape is already reported by path.
        return []
    if len(sources) != 1:
        return []  # the step-input pass refuses a scattered input that joins sources
    source = sources[0]
    if "/" in source:
        # A process-output source is reported once, by the cross-step pass.
        return []
    declared = source_types.get(source)
    if not _lists(declared):
        return [_Finding(SophiosErrorCode.NF_SCATTER_SOURCE, f".in.{raw_name}",
                         f"a scattered input needs an array-typed source; {source!r} declares {declared!r}", step.node)]
    return []


def _scatter_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Validate every scattered step against the single- and multi-input scatter lowerings."""
    findings: list[_Finding] = []
    source_types = _source_types(workflow, steps)
    for step in steps:
        if step.node.scatter is None:
            continue
        names = step.scattered
        if not names:
            findings.append(_Finding(SophiosErrorCode.NF_INVALID_CWL, ".scatter",
                                     "scatter must name one input, as a string or a non-empty list", step.node))
            continue
        method = step.node.scatter.method
        if method is not None and method not in SCATTER_METHODS:
            findings.append(_Finding(SophiosErrorCode.NF_INVALID_CWL,
                            ".scatterMethod", f"unsupported CWL scatter method {method!r}", step.node))
        elif len(names) > 1 and method is None:
            # At exactly one scattered input all three methods coincide, so a method is required to decide two or
            # more; each of them is an approved multi-input lowering (design §6, Topology).
            findings.append(
                _Finding(SophiosErrorCode.NF_INVALID_CWL,
                         ".scatterMethod", f"multi-input scatter over {len(names)} inputs requires an "
                         "explicit scatterMethod", step.node)
            )
        for raw_name in names:
            port = step.tool.inputs.get(raw_name)
            if port is None:
                # No process input receives the element: a step-only input is a val after the tool's own.
                findings.append(_Finding(SophiosErrorCode.NF_SCATTERED_STEP_ONLY_INPUT,
                                         f".in.{raw_name}", "scatter over an input the step's tool does not "
                                         "declare is not supported", step.node))
                continue
            if _lists(canonical(port.type_)):
                findings.append(
                    _Finding(SophiosErrorCode.NF_SCATTER_OF_ARRAYS,
                             ".scatter", f"scattered input {raw_name!r} is array-typed; scattering over an "
                             "array of arrays is deferred beyond this lowering", step.node)
                )
                continue
            findings.extend(_scatter_source_findings(step, raw_name, source_types))
    findings.extend(_scatter_edge_findings(steps))
    return findings


def _scatter_edge_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject every process edge whose cardinality a scattered step changes and no lowering keeps.

    A scattered step's output is gathered, in invocation order, into one array; a nested_crossproduct result
    reaches workflow outputs only. An unscattered step's output reaches a scattered step as one value: it
    broadcasts to every task, and it is not an array to scatter over, since no tool output is an array.
    """
    findings: list[_Finding] = []
    scattered = {step.node.name: step.node.scatter for step in steps if step.scattered}
    for step in steps:
        for port, sources in step.wired():
            for source in sources:
                if "/" not in source:
                    continue
                scatter = scattered.get(source.rsplit("/", maxsplit=1)[0])
                if scatter is not None:
                    # Every scatter is indexed, so its outputs are gathered once, in invocation order (design §6,
                    # Topology, Gather).
                    if scatter.method == "nested_crossproduct" and len(set(scatter.inputs)) > 1:
                        findings.append(_Finding(SophiosErrorCode.NF_NESTED_RESULT, f".in.{port.name}",
                                                 f"{source!r} is a nested_crossproduct result, which reaches "
                                                 "workflow outputs only", step.node))
                elif port.name in step.scattered:
                    findings.append(_Finding(SophiosErrorCode.NF_SCATTER_SOURCE, f".in.{port.name}",
                                             f"a scattered input needs an array-typed source; {source!r}, from a "
                                             "step that is not scattered, is one value", step.node))
    return findings


def _step_input_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Check each step input the fixed idioms lower: a default, a linkMerge, a pickValue, or two or more sources.

    Each source is one value (design §6, Topology, Step inputs). Null is the [] sentinel, so an idiom may not take
    an array source where [] would be read as null, and a merged list may not hold a source that may be null.
    """
    source_types = _source_types(workflow, steps)
    by_name = {step.node.name: step for step in steps}

    def is_array(source: str) -> bool:
        if "/" in source:  # a scatter's outputs are gathered
            return (producer := by_name.get(source.rsplit("/", maxsplit=1)[0])) is not None and bool(producer.scattered)
        return _is_array_type(required(source_types.get(source))[0])

    def may_be_null(source: str) -> bool:
        if "/" in source:
            producer = by_name.get(source.rsplit("/", maxsplit=1)[0])
            return producer is not None and producer.node.condition is not None
        return required(source_types.get(source))[1]

    def problems(step: _Step, port: StepInput, sources: list[str]) -> Iterator[tuple[SophiosErrorCode, str, str]]:
        """Each problem of one step input: its code, the field of the input it is at, and the reason."""
        link_merge, pick_value = port.link_merge, port.pick_value
        if len(sources) < 2 and not _idioms(port):
            return
        if port.name in step.scattered:
            yield SophiosErrorCode.NF_SCATTER_SOURCE, "", "a scattered input takes its one source as it is"
            return
        merges = merge_of(link_merge, sources)
        if link_merge is not None and link_merge not in LINK_MERGES:
            yield SophiosErrorCode.NF_INVALID_CWL, ".linkMerge", f"unsupported linkMerge {link_merge!r}"
            return
        if pick_value is not None and pick_value not in PICK_VALUES:
            yield SophiosErrorCode.NF_INVALID_CWL, ".pickValue", f"unsupported pickValue {pick_value!r}"
            return
        if pick_value is not None and merges is None:
            yield (SophiosErrorCode.NF_JOIN, ".pickValue",
                   "pickValue picks from a list; one source with no linkMerge is not one")
        # A step-only input has no declaration: its type is what it delivers.
        declared_type = canonical(step.tool.inputs[port.name].type_) if port.declared is not None else spell(port.type)
        if port.has_default:
            if port.declared is None and _parameter_type(declared_type) not in PARAM_TYPES:
                yield (SophiosErrorCode.NF_DEFAULT, ".default",
                       "a default of an input the step's tool does not declare needs a source of a scalar type to be "
                       "checked against; give the value as a workflow input")
            elif sources and merges is not None:
                yield (SophiosErrorCode.NF_JOIN, ".default",
                       "a default never applies beside a linkMerge or a pickValue: the joined value is not null")
            elif required(declared_type)[0] in ("File", "Directory"):
                yield (SophiosErrorCode.NF_DEFAULT, ".default", "a step-input default of a File or Directory input "
                       "is not supported; give the path as a workflow input")
            elif port.default is None or isinstance(port.default, (Mapping, list)) or not _phase1_value_matches(
                    declared_type, port.default):
                yield (SophiosErrorCode.NF_DEFAULT, ".default",
                       "the Nextflow target supports a JSON scalar default of the tool input's type")
        # A default is a JSON scalar, so it never sits beside an array source of its type.
        arrays = [source for source in sources if is_array(source)]
        if arrays and pick_value is not None:
            yield (SophiosErrorCode.NF_JOIN, "", f"{arrays[0]!r} is an array, and [] stands for both a null and an "
                   "empty array, so pickValue cannot take it")
        elif arrays and merges == "merge_nested":
            yield (SophiosErrorCode.NF_JOIN, "",
                   f"merge_nested over the array {arrays[0]!r} makes a nested array, which is not supported")
        if merges is not None and pick_value is None and (nullable := [s for s in sources if may_be_null(s)]):
            yield (SophiosErrorCode.NF_JOIN, "", f"{nullable[0]!r} may be null, and a joined list has no lowering of "
                   "a null element; pick from it with pickValue")
        gives = gives_list(pick_value)
        if merges is not None and port.declared is not None and gives != _lists(declared_type):
            yield (SophiosErrorCode.NF_JOIN, "", f"the step input gives {'a list' if gives else 'one value'}, "
                   f"but its tool input declares {declared_type!r}")

    return [_Finding(code, f".in.{port.name}{field}", text, step.node)
            for step in steps for port, sources in step.wired() for code, field, text in problems(step, port, sources)]


def _source_types(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> dict[str, Any]:
    """Every wireable source name, and the CWL type it carries: a workflow input's as declared, a tool output's as its
    tool declares it."""
    types: dict[str, Any] = {port.name: spell(port.declaration.type, port.declaration.unread)
                             for port in workflow.inputs}
    for step in steps:
        for raw_port, port in step.tool.outputs.items():
            types[f"{step.node.name}/{raw_port}"] = canonical(port.type_)
    return types


def _safe_absence_names(tool: _Tool) -> set[str] | None:
    """Return the input names whose absence cannot affect command rendering.

    A name is safe when its absence leaves rendering unchanged: it is not
    referenced by any command token, stream target, or output glob, it does
    not own a shell-literal or valueFrom binding that CWL would omit, or it is referenced
    only by the boolean-flag token it drives or its own optional binding
    (each tests its value, never calls ``.toString()`` on the sentinel).
    Returns None when the tool's command or
    outputs cannot be analyzed; lowering reports why, so absence is not judged.
    """
    referenced = _referenced_names(tool)
    if referenced is None:
        return None
    # Every template reference dereferences the value, so it is unsafe even
    # when the same input also drives a flag: excusing it on the strength of
    # the flag use would let the absence sentinel render into a command line.
    # A flag or an optional binding is not a template, so it never enters `referenced`.
    try:
        all_names = {_identifier(raw_name) for raw_name in tool.inputs}
        # A shell literal, or a valueFrom other than a boolean's flag, renders whatever the value is.
        unconditional_names = {
            _identifier(raw_name)
            for raw_name, port in tool.inputs.items()
            if (binding := port.inputBinding) is not None
            and (binding.shellQuote is False
                 or binding.valueFrom is not None and required(canonical(port.type_))[0] != "boolean")
        }
    except ValueError:
        return None
    return all_names - referenced - unconditional_names


def _absent_optional_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject optional inputs whose value is absent and unsafe."""
    try:
        params = _workflow_params(workflow)
    except _Unsupported:
        # Two inputs normalize alike, which the capability pass reports: there is no unambiguous parameter lookup
        # for the optional-input analysis.
        return []
    findings: list[_Finding] = []
    for step in steps:
        safe_names = _safe_absence_names(step.tool)
        if safe_names is None:
            continue
        bound = {port.name: (port, sources) for port, sources in step.bound()}
        for raw_name, declared in step.tool.inputs.items():
            optional = required(canonical(declared.type_))[1]
            if raw_name not in bound:
                # Unwired inputs are only a missingness problem when optional without a default; required unwired
                # inputs fail compilation.
                absent = optional and declared.default is None
            else:
                port, found = bound[raw_name]
                sources = _wired(port, found)
                if sources is None:
                    continue
                # A wired input is absent when any boundary source resolves to null, including an absent optional
                # workflow input feeding a required tool input. A pickValue or a default gives a value that is not
                # null.
                absent = not _gives_non_null(port) and any(
                    params.get(_identifier(source)) is None
                    for source in sources if "/" not in source
                )
            if not absent:
                continue
            if optional:
                # Every input's name normalizes where its absence is judged.
                if _qualifier(canonical(declared.type_)) == "val" and _identifier(raw_name) in safe_names:
                    continue
                findings.append(_Finding(
                    SophiosErrorCode.NF_ABSENT_OPTIONAL,
                    f".run.inputs.{raw_name}", "absent optional values are supported only for a val input "
                    "whose absence leaves command rendering unchanged", step.node))
            else:
                findings.append(_Finding(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                         f".run.inputs.{raw_name}", "resolves to an absent required value", step.node))
    return findings


def _conditional_consumer_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject every consumer of a conditional step's output the design does not admit.

    A conditional step's output may be null at runtime (design §6, Topology). That null is admitted only at a
    workflow output (which reports null) or an optional val input of a later step whose use the absent-optional
    lowering already admits (never referenced, or referenced only by a boolean-flag token or its own optional
    binding). Every other consumer is rejected by name, as is an array-typed output of a conditional step outside
    scatter, where the sentinel would collide with a genuinely empty array.
    """
    findings: list[_Finding] = []
    conditional: set[str] = set()
    for step in steps:
        if step.node.condition is None or not isinstance(step.tool.process, standard.CommandLineTool):
            continue
        conditional.add(step.node.name)
        for raw_name, produced in step.tool.outputs.items():
            if _is_array_type(required(canonical(produced.type_))[0]):
                findings.append(_Finding(SophiosErrorCode.NF_SKIPPED_OUTPUT, f".out.{raw_name}", "array-typed output "
                                         "of a conditional step is not supported outside scatter", step.node))
    if not conditional:
        return findings
    for step in steps:
        for port, sources in step.wired():
            consumes = any("/" in source and source.rsplit("/", maxsplit=1)[0] in conditional for source in sources)
            if not consumes or _gives_non_null(port):
                continue
            declared = step.tool.inputs.get(port.name)
            if declared is None:
                continue
            cwl_type = canonical(declared.type_)
            if _is_array_type(required(cwl_type)[0]):
                problem = ("an array-typed port cannot consume a conditional step's output; [] already stands for a "
                           "skipped step")
            elif _qualifier(cwl_type) == "path":
                problem = "a path consumer of a possibly-null conditional step output is not supported"
            elif not required(cwl_type)[1]:
                problem = "a non-optional port cannot consume a possibly-null conditional step output"
            elif (safe_names := _safe_absence_names(step.tool)) is None or _identifier(port.name) in safe_names:
                continue
            else:
                problem = ("this input's use of a possibly-null conditional step output is not one the "
                           "absent-optional lowering admits")
            findings.append(_Finding(SophiosErrorCode.NF_SKIPPED_OUTPUT, f".in.{port.name}", problem, step.node))
    return findings


def _text_capture_sink_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Restrict a captured value's sink to a workflow output.

    Every process output before this lowering carried the ``path`` qualifier, so the executable graph has no
    source-to-destination qualifier agreement check for process edges to lean on.
    """
    endpoints: set[str] = set()
    for step in steps:
        for raw_port, produced in step.tool.outputs.items():
            try:
                capture = _output_capture(raw_port, produced)
            except ValueError:
                continue
            # A conditional step's is the exception: _conditional_consumer_findings governs its downstream sinks.
            if text_stays_out(capture, step.node.condition is not None):
                endpoints.add(f"{step.node.name}/{raw_port}")
    return [
        _Finding(SophiosErrorCode.NF_CAPTURED_TEXT_READ,
                 f".in.{port.name}", f"{source!r} captures file text; its only approved sink is a workflow "
                 "output, because the executable graph has no qualifier agreement check for process edges", step.node)
        for step in steps for port, sources in step.wired() for source in sources if source in endpoints
    ]


def _literal_outputs(tool: _Tool) -> dict[str, str]:
    """Each output whose glob is one literal name, with no wildcard, by port."""
    names: dict[str, str] = {}
    for raw_port, port in tool.outputs.items():
        glob = _literal_text(port.outputBinding.glob) if port.outputBinding is not None else None
        if glob and not wildcard_problem(glob):
            names[raw_port] = glob
    return names


def _staged_names(step: _Step, produced: Mapping[str, str]) -> dict[str, tuple[set[str], bool]]:
    """The names known at compile time that each File or Directory input is linked under in the task directory, and
    whether the link is an InitialWorkDirRequirement entry.

    An entry with a literal name is linked under it; any other input not in an array is linked under its basename,
    which is a literal output name when its source produces one. A writable entry is a copy, not a link, and an
    array's files are linked in numbered directories.
    """
    try:
        entries = _iwdr_entries(step.tool)
    except ValueError:  # its own finding; what it stages is unknown
        return {}
    wired = {port.name: sources for port, sources in step.wired()}
    staged: dict[str, tuple[set[str], bool]] = {}
    for raw_name, port in step.tool.inputs.items():
        try:
            if cwl_type_to_nf_qualifier(canonical(port.type_)) != "path":
                continue
            entry = entries.get(_identifier(raw_name))
        except ValueError:  # an array, or what another finding names
            continue
        sources = wired.get(raw_name)
        if sources is None:  # unbound, or no source of a shape the idioms read
            continue
        if entry is not None and entry.copy:
            continue
        staged[raw_name] = (
            {entry.name} if entry is not None and entry.name is not None
            else {produced[source] for source in sources if source in produced},
            entry is not None,
        )
    return staged


def _staged_name_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Name each file a task writes at or under a name one of its inputs is linked under.

    Nextflow links a File or Directory input into the task directory, so a command that writes the link's name, or
    a name inside a linked directory, writes through it into the file the input names: an upstream task's output, or
    a workflow input. Only a name known at compile time is checked: a literal stdout, stderr or glob against a
    literal entry name or a literal upstream glob. A glob only collects an entry, which cwltool stages in the output
    directory too, so an entry is checked against stdout and stderr alone. A name a workflow input gives, or a
    pattern or a reference makes, is known only when the task runs.
    """
    produced = {f"{step.node.name}/{raw_port}": glob.rsplit("/", maxsplit=1)[-1]
                for step in steps for raw_port, glob in _literal_outputs(step.tool).items()}
    findings: list[_Finding] = []
    for step in steps:
        if not isinstance(step.tool.process, standard.CommandLineTool):
            continue
        path = ".run"
        # Each name once, where it is first written: a stdout's glob names it again.
        written: dict[str, tuple[str, str, bool]] = {}
        for where, name, collects in [
            *((f"{path}.{stream}", _literal_text(getattr(step.tool.process, stream)), False)
              for stream in ("stdout", "stderr")),
            *((f"{path}.outputs.{port}.outputBinding.glob", glob, True)
              for port, glob in _literal_outputs(step.tool).items()),
        ]:
            if name:
                written.setdefault(posixpath.normpath(name), (where, name, collects))
        staged = _staged_names(step, produced)
        findings.extend(
            _Finding(SophiosErrorCode.NF_WRITE_THROUGH_STAGED,
                     where, f"writing {name!r} writes through {link!r}, the name input {raw_name!r} is linked under "
                     "in the task directory, into the file the input names; give the output another name", step.node)
            for normal, (where, name, collects) in written.items()
            for raw_name, (links, entry) in staged.items()
            if not (collects and entry)
            for link in links
            if normal == link or normal.startswith(f"{link}/")
        )
    return findings


class _WhenError(_Unsupported):
    """A step's `when` the target cannot lower."""


def _condition_value(step: _Step) -> Expr:
    """A step's ``when`` as a boolean tree over the inputs the step binds, typed by what each one delivers: a
    step-only input as any other, and through every call.

    A step's when is evaluated once per invocation, after inputs are bound.
    """
    condition = step.node.condition
    assert condition is not None
    when = condition.written
    if not isinstance(when, str):
        raise _WhenError(SophiosErrorCode.NF_INVALID_CWL,
                         f"{when!r} must be a safe JavaScript subset $( … ) expression")
    if (problem := boolean_problem(condition)) is not None:
        raise _WhenError(SophiosErrorCode.WHEN_NOT_BOOLEAN, problem)
    if not is_safe_subset_text(when):
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} must be a safe JavaScript subset $( … ) expression")
    if condition.expr is None:
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} is outside the safe JavaScript subset: {condition.problem}")
    if unbound := sorted(references(condition.expr) - {port.name for port in step.node.inputs}):
        names = ", ".join(f"inputs.{name}" for name in unbound)
        raise _WhenError(SophiosErrorCode.NF_WHEN_UNBOUND, f"{when!r} reads {names}, which the step does not bind")
    if condition.type is None:
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} is outside the safe JavaScript subset: {condition.problem}")
    return condition.expr


def _parameter_type(cwl_type: Any) -> str | None:
    """`_param_type`, or None for a type no parameter has."""
    try:
        return _param_type(cwl_type)
    except ValueError:
        return None


def _param_type(cwl_type: Any) -> str:
    """The declared type the generated pipeline checks a parameter's value against, as `PARAM_TYPES` spells it."""
    inner, optional = required(cwl_type)
    base = f"{required(_array_item_type(inner))[0]}[]" if _is_array_type(inner) else inner
    return f"{base}?" if optional else str(base)


def _json_value(value: Any) -> Any:
    match value:
        case Mapping() as mapping:
            return {str(key): _json_value(item) for key, item in mapping.items()}
        case list() | tuple() as items:
            return [_json_value(item) for item in items]
        case PathLike() as path:
            return str(path)
        case _:
            return copy.deepcopy(value)


def _workflow_params(workflow: ResolvedWorkflow) -> dict[str, Any]:
    """Each workflow input's value, by parameter name: the run's, else its default, else None."""
    names = _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    params: dict[str, Any] = {}
    for port in workflow.inputs:
        if port.has_value:
            value = local_locations_as_paths(port.value)
        elif port.declaration.has_default and not port.declaration.shorthand:
            value = port.declaration.default
        else:
            value = None
        params[names[port.name]] = _json_value(value)
    return params
