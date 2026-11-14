"""The resolved view: a read-only reading of a compiled graph, for a target to lower.

It resolves and parses what the graph already says and nothing more: each step
input's sources at its level, its record fields and the type it delivers, the
parsed and typed `when`, `scatter`, workflow inputs and outputs, and the run
policy. A construct Sophios took from CWL unchanged means what CWL v1.2 says;
what Sophios invented (edge inference, the names it makes, anchors) is
resolved before the view exists, and the view holds only the result, as
references (design_docs/resolved-view-design.md §3-§6).

`resolve` builds the view from a finished `WorkflowGraph` and the tool document
each tool step runs; it never reads a workflow's emitted CWL and refuses
nothing. Refusing is a target's job. Every name is spelled by `Names`, so a
name in the view is the name the CWL target writes.
"""
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import NamedTuple, TypeAlias

from ..lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, lines
from ..lang.error_codes import SophiosErrorCode
from ..lang.nodes import OpaqueCwl
from ..lang.spans import SourceSpan
from ..lang.subset import BOOLEAN, Expr, check, is_safe_subset_text, parse
from .declarations import delivered, layered, output_rank, written_order
from .names import Names
from .policy import RunPolicy, one_expression
from .stepin import step_inputs
from .types import (
    DerivedName, EmittedValue, Expression, PortDeclaration, PortName, PortType, Record, Source, StepId,
    StepNode, StepOutputRef, WorkflowGraph,
)


@dataclass(frozen=True, slots=True)
class InputRef:
    """A workflow input of the same workflow, spelled as the job spells it."""

    name: str


@dataclass(frozen=True, slots=True)
class OutputRef:
    """An output of a step of the same workflow: the step as it is spelled there, and the port."""

    step: str
    port: str


Ref: TypeAlias = InputRef | OutputRef


@dataclass(frozen=True, slots=True)
class Requirement:
    """One requirement or hint, by class, with its body verbatim."""

    cls: str
    body: OpaqueCwl
    hint: bool


@dataclass(frozen=True, slots=True)
class WorkflowInput:  # pylint: disable=too-many-instance-attributes
    """A workflow input, and on the root, this run's value."""

    name: str
    declaration: PortDeclaration
    value: OpaqueCwl = None
    has_value: bool = False
    #: The step and input whose written value this input holds, when the compiler made it from one.
    made_from: tuple[str, str] | None = None
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class WorkflowOutput:
    """A workflow output and its `outputSource` at this workflow's level."""

    name: str
    declaration: PortDeclaration
    #: A reference; or the text Link left unresolved, as written; or `None` for an output with no `outputSource`.
    source: Ref | str | None
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    """The tool a step runs: the file the registry read it from and the document the CWL target writes for the
    step."""

    path: str
    document: OpaqueCwl


@dataclass(frozen=True, slots=True)
class StepInput:  # pylint: disable=too-many-instance-attributes
    """One entry of a step's `in:`, in the order the CWL target writes it."""

    name: str
    #: The tool's or nested workflow's declaration; `None` for an input the tool does not declare.
    declared: PortDeclaration | None
    sources: tuple[Ref, ...]
    #: As written; CWL's values are strings, and a target refuses any other.
    link_merge: OpaqueCwl
    pick_value: OpaqueCwl
    default: OpaqueCwl
    has_default: bool
    value_from: OpaqueCwl
    #: The record's other fields, verbatim (label, loadContents, ...).
    fields: tuple[tuple[str, OpaqueCwl], ...]
    #: A raw `!cwl` reference the graph holds unresolved.
    expression: str | None
    #: The canonical CWL type of the value the step input delivers (§5).
    type: OpaqueCwl
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class Scatter:
    """A step's `scatter:`, spelled, in written order, and its `scatterMethod` as written."""

    inputs: tuple[str, ...]
    method: OpaqueCwl


@dataclass(frozen=True, slots=True)
class Condition:
    """A step's authored `when`: as written, its safe-subset tree, and the tree's type over the step inputs."""

    written: OpaqueCwl
    expr: Expr | None
    type: str | None
    #: Why there is no tree, or why it does not type, in the subset's words.
    problem: str | None


@dataclass(frozen=True, slots=True)
class ResolvedStep:  # pylint: disable=too-many-instance-attributes
    """One step occurrence: what it runs, what it binds, and the keys it carries."""

    #: Its id in its own workflow.
    local: str
    #: Its qualified id, outer~inner: what messages print.
    name: str
    run: 'ResolvedTool | ResolvedWorkflow'
    inputs: tuple[StepInput, ...]
    #: Its outputs' names, as its out: lists them.
    outputs: tuple[str, ...]
    scatter: Scatter | None
    condition: Condition | None
    #: The step's own requirements, then its hints.
    requirements: tuple[Requirement, ...]
    #: Every other step key, verbatim (label, doc, ...).
    fields: tuple[tuple[str, OpaqueCwl], ...]
    span: SourceSpan | None
    #: A tool step's requirements and hints from outside its tool, as cwltool gives them to it (§8): for each class,
    #: the innermost requirement written on an enclosing workflow, a call or the step, else the innermost such hint;
    #: requirements first. A call's is empty: its workflow's steps inherit.
    inherited: tuple[Requirement, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedWorkflow:  # pylint: disable=too-many-instance-attributes
    """A workflow: its inputs in the order the CWL target writes them, the outputs kept, and its steps."""

    name: str
    inputs: tuple[WorkflowInput, ...]
    outputs: tuple[WorkflowOutput, ...]
    steps: tuple[ResolvedStep, ...]
    #: This document's own requirements, then its hints.
    requirements: tuple[Requirement, ...]
    #: Every other top-level key, verbatim ($namespaces, doc, ...).
    fields: tuple[tuple[str, OpaqueCwl], ...]
    #: The compilation's; the same object at every depth.
    policy: RunPolicy
    span: SourceSpan | None

    def __post_init__(self) -> None:
        """Reject a reference to anything outside this workflow."""
        inputs = {port.name for port in self.inputs}
        outputs = {step.local: set(step.outputs) for step in self.steps}

        def known(ref: Ref) -> bool:
            match ref:
                case InputRef(name=name):
                    return name in inputs
                case OutputRef(step=step, port=port):
                    return port in outputs.get(step, ())
            return False

        for ref in [*(source for step in self.steps for port in step.inputs for source in port.sources),
                    *(port.source for port in self.outputs if isinstance(port.source, (InputRef, OutputRef)))]:
            if not known(ref):
                raise ValueError(f'{ref} names nothing in workflow {self.name!r}')


def resolve(graph: WorkflowGraph, tools: Mapping[StepId, ResolvedTool], *, policy: RunPolicy = RunPolicy(),
            values: Mapping[str, OpaqueCwl] | None = None,
            outputs: Collection[str] | None = None) -> ResolvedWorkflow:
    """The view of a finished compilation's graph.

    Args:
        graph (WorkflowGraph): `CompilationResult.graph`: linked, inferred and completed.
        tools (Mapping[StepId, ResolvedTool]): The tool every tool step runs, at any depth.
        policy (RunPolicy): The compilation's run policy.
        values (Mapping[str, OpaqueCwl] | None): This run's values, by root input as the job spells it, each in
            place of the value the graph binds; `None` keeps the graph's.
        outputs (Collection[str] | None): The root outputs to keep; `None` keeps them all.

    Raises:
        ValueError: If `values` or `outputs` names nothing the root has: the caller's mistake, not the user's.

    Returns:
        ResolvedWorkflow: The root workflow, every nested workflow inside its calls.
    """
    names = Names.of(graph)
    root = _workflow(graph, _Level(names, tools, policy, {step.id: step for step in graph.all_steps}, graph, {}))
    bound = {names.port(binding.name): binding.value for binding in graph.job_bindings}
    for name in values or {}:
        if name not in {port.name for port in root.inputs}:
            raise ValueError(f'value {name!r} has no declared workflow input')
    bound.update(values or {})
    kept = root.outputs
    if outputs is not None:
        if unknown := sorted(set(outputs) - {port.name for port in root.outputs}):
            raise ValueError(f'workflow {root.name!r} has no output {", ".join(map(repr, unknown))}')
        kept = tuple(port for port in root.outputs if port.name in outputs)
    inputs = tuple(replace(port, value=bound.get(port.name), has_value=port.name in bound) for port in root.inputs)
    return replace(root, inputs=inputs, outputs=kept)


def _workflow(graph: WorkflowGraph, outer: '_Level', given: Mapping[str, OpaqueCwl] | None = None) -> ResolvedWorkflow:
    """A workflow's view, inside the level `outer` of the call that runs it, or of nothing for the root; `given` is
    the type each input the call binds holds, which is what its sources deliver (§5)."""
    names = outer.names
    requirements, fields = _requirements(graph.passthrough, graph.requirements)
    order = written_order(graph)
    inputs = tuple(WorkflowInput(names.port(port.name), port.declaration,
                                 made_from=_made_from(port.name, names), span=port.span or graph.span)
                   for port in sorted(graph.workflow_inputs, key=lambda port: order(port.name)))
    level = replace(outer, graph=graph, inputs={
        port.name: (given or {}).get(port.name, port.declaration.type.canonical) for port in inputs},
        outside=(*outer.outside, *requirements))
    ins = step_inputs(graph)
    steps = tuple(_step(step, graph.child(step.id), ins[step.id], level) for step in graph.steps)
    outputs = tuple(WorkflowOutput(names.port(port.name), port.declaration,
                                   _output_source(port.output_source, level.inputs, names),
                                   port.span or graph.span)
                    for port in graph.workflow_outputs)
    documents: tuple[tuple[str, OpaqueCwl], ...] = (('$namespaces', dict(graph.namespaces)),) \
        if graph.namespaces else ()
    documents += (('$schemas', list(graph.schemas)),) if graph.schemas else ()
    return ResolvedWorkflow(graph.name, inputs, outputs, steps, requirements, documents + fields, level.policy,
                            graph.span)


def _made_from(name: PortName, names: Names) -> tuple[str, str] | None:
    """The step and input a made input's value was written on: what its derived name joins."""
    if not isinstance(name, DerivedName):
        return None
    return names.qualified(name.step), names.port(name.port)


def _output_source(source: StepOutputRef | OpaqueCwl, inputs: Collection[str], names: Names) -> Ref | str | None:
    """An output's source at its workflow's level, as the CWL target writes it: the step output its `outputSource`
    names; else the workflow input it names; else the text as written, or `None` for no `outputSource`."""
    match source:
        case StepOutputRef():
            return _ref(source, names)
        case str() if source in inputs:
            return InputRef(source)
        case str():
            return source
    return None


def _requirements(passthrough: tuple[tuple[str, OpaqueCwl], ...],
                  requirements: tuple[tuple[str, OpaqueCwl], ...] = ()) -> tuple[
                      tuple[Requirement, ...], tuple[tuple[str, OpaqueCwl], ...]]:
    """The requirements, then the hints, written as a mapping; and every other key, a section of another shape
    among them."""
    found = [Requirement(str(cls), body, False) for cls, body in requirements]
    fields: list[tuple[str, OpaqueCwl]] = []
    for key, value in passthrough:
        if key in ('requirements', 'hints') and isinstance(value, dict):
            found.extend(Requirement(str(cls), body, key == 'hints') for cls, body in value.items())
        else:
            fields.append((key, value))
    return tuple(sorted(found, key=lambda requirement: requirement.hint)), tuple(fields)


@dataclass(frozen=True, slots=True)
class _Level:
    """What a step's sources are read against: the spelling, the tools, every step of the tree, the step's own
    workflow and the type each of its inputs holds; and the requirements and hints written outside the step,
    outermost first."""

    names: Names
    tools: Mapping[StepId, ResolvedTool]
    policy: RunPolicy
    everything: Mapping[StepId, StepNode]
    graph: WorkflowGraph
    inputs: Mapping[str, OpaqueCwl]
    outside: tuple[Requirement, ...] = ()


def _inherited(written: tuple[Requirement, ...]) -> tuple[Requirement, ...]:
    """What a tool inherits of the requirements and hints written outside it, outermost first (§8): for each class,
    the innermost requirement, else the innermost hint, as cwltool merges a step's with its workflow's and a lookup
    takes the last requirement of a class, then the last hint (cwltool/workflow.py:202-223, cwltool/utils.py:481-489).
    """
    found: dict[str, Requirement] = {}
    for requirement in written:
        held = found.get(requirement.cls)
        if held is None or held.hint or not requirement.hint:
            found[requirement.cls] = requirement
    return tuple(sorted(found.values(), key=lambda requirement: requirement.hint))


def _step(step: StepNode, child: WorkflowGraph | None, ins: tuple[tuple[PortName, EmittedValue], ...],
          level: _Level) -> ResolvedStep:
    names = level.names
    called = {names.port(port.name): port.declaration for port in child.workflow_inputs} if child is not None else {}
    inputs = tuple(_step_input(name, value, step, called, level) for name, value in ins)
    requirements, fields = _requirements(step.passthrough)
    within = replace(level, outside=(*level.outside, *requirements))
    run: ResolvedTool | ResolvedWorkflow = (
        _workflow(child, within, {port.name: port.type for port in inputs}) if child is not None
        else level.tools[step.id])
    outputs = tuple(names.port(port.id.port) for port in step.outputs)
    interpreted = dict(step.interpreted)
    scatter = (Scatter(tuple(names.port(port) for port in step.scatter_ports), interpreted.get('scatterMethod'))
               if interpreted.get('scatter') is not None else None)
    condition = _condition(interpreted['when'], inputs, scatter) if interpreted.get('when') is not None else None
    if scatter is None and 'scatterMethod' in interpreted:
        # A method with nothing to scatter is not a scatter: a key as written.
        fields = (('scatterMethod', interpreted['scatterMethod']), *fields)
    return ResolvedStep(names.step(step.id), names.qualified(step.id), run, inputs, outputs, scatter,
                        condition, requirements, fields, step.span,
                        _inherited(within.outside) if isinstance(run, ResolvedTool) else ())


# pylint: disable-next=too-many-locals
def _step_input(name: PortName, value: EmittedValue, step: StepNode, called: Mapping[str, PortDeclaration],
                level: _Level) -> StepInput:
    """One `in:` entry as the CWL target writes it, its sources references at this level; `called` holds the
    declaration of each input of the workflow a call runs."""
    spelled = level.names.port(name)
    port = next((port for port in step.inputs if port.id.port == name), None)
    declared = called.get(spelled, port.declaration if port is not None else None)
    binding = next((binding for binding in step.bindings if binding.sink.port == name), None)
    span = getattr(binding.value, 'span', None) if binding is not None else None
    refs: tuple[PortName | StepOutputRef, ...]
    fields: dict[str, OpaqueCwl]
    expression: str | None
    match value:
        case Source(ref=str() as ref) if level.names.port(ref) not in level.inputs:
            # A name `--allow_raw_cwl` lets through unresolved: raw CWL, as `!cwl` writes it.
            refs, fields, expression = (), {}, str(ref)
        case Source(ref=ref):
            refs, fields, expression = (ref,), {}, None
        case Record(sources=sources, fields=written):
            refs, fields, expression = sources, dict(written), None
        case Expression(text=text):
            refs, fields, expression = (), {}, text
    carried = {'linkMerge', 'pickValue', 'default', 'valueFrom'}
    return StepInput(
        spelled, declared, tuple(_ref(ref, level.names) for ref in refs), fields.get('linkMerge'),
        fields.get('pickValue'), fields.get('default'), 'default' in fields, fields.get('valueFrom'),
        tuple((key, field) for key, field in fields.items() if key not in carried), expression,
        _delivered_type([_source_type(ref, level) for ref in refs], fields, declared), span)


def _ref(ref: PortName | StepOutputRef, names: Names) -> Ref:
    match ref:
        case StepOutputRef(step=step, port=port):
            return OutputRef(names.step(step), names.port(port))
    return InputRef(names.port(ref))


def _source_type(ref: PortName | StepOutputRef, level: _Level) -> OpaqueCwl:
    """A source's type at this level (§5): a workflow input's as declared; a step output's inside one array
    layer per scatter it came through, nullable when its producer, or a call it came out of, has a `when`."""
    match ref:
        case StepOutputRef(step=producer_id, port=port):
            producer = level.everything[producer_id]
            declared = next(candidate for candidate in producer.outputs if candidate.id.port == port)
            produced = layered(declared.type, output_rank(producer)).canonical
            return PortType(['null', produced]).canonical if _may_skip(producer, port, level) else produced
    return level.inputs.get(level.names.port(ref), 'Any')


def _may_skip(producer: StepNode, port: PortName, level: _Level) -> bool:
    """Whether `producer`'s output `port` may be `null`: the producer, or for a call the tool step that makes the
    output and every call on the way out to it, has a `when`."""
    if dict(producer.interpreted).get('when') is not None:
        return True
    child = level.graph.child(producer.id)
    inner = dict(child.output_mapping).get(port) if child is not None else None
    if inner is None:
        return False
    parts = inner.step.namespace.parts
    on_the_way = (*parts[parts.index(producer.id) + 1:], inner.step)
    return any(dict(level.everything[step].interpreted).get('when') is not None for step in on_the_way)


def _delivered_type(produced: list[OpaqueCwl], fields: Mapping[str, OpaqueCwl],
                    declared: PortDeclaration | None) -> OpaqueCwl:
    """What a step input holds after its sources, `linkMerge`, `pickValue` and `default` (§5): `Any` after a
    `valueFrom`; the declared type where no source says more; sources of different types merge as `Any`."""
    if 'valueFrom' in fields:
        return 'Any'
    if not produced:
        return declared.type.canonical if declared is not None else 'Any'
    alike = all(item == produced[0] for item in produced)
    merged: OpaqueCwl = delivered(produced[0] if alike else 'Any', sources=len(produced),
                                  link_merge=fields.get('linkMerge'), pick_value=fields.get('pickValue'))
    if 'default' in fields and isinstance(merged, list) and 'null' in merged:
        rest = [member for member in merged if member != 'null']
        return rest[0] if len(rest) == 1 else rest
    return merged


def _seen(port: StepInput, scatter: Scatter | None) -> OpaqueCwl:
    """What a `when` reads of a step input: its value, or one element of it in each job of a scatter (§5)."""
    match port.type:
        case {'type': 'array', 'items': items} if scatter is not None and port.name in scatter.inputs:
            return items
    return port.type


def _condition(when: OpaqueCwl, inputs: tuple[StepInput, ...], scatter: Scatter | None) -> Condition:
    if not isinstance(when, str):
        return Condition(when, None, None, 'a when is a string')
    if not is_safe_subset_text(when):
        return Condition(when, None, None, 'the safe JavaScript subset requires the whole field to be one $( … ) '
                         'reference')
    try:
        expr = parse(when)
    except ValueError as error:
        return Condition(when, None, None, str(error))
    try:
        typed = check(expr, {port.name: _seen(port, scatter) for port in inputs})
    except ValueError as error:
        return Condition(when, expr, None, str(error))
    return Condition(when, expr, typed, None)


def boolean_problem(condition: Condition) -> str | None:
    """Why `condition` cannot give `true` or `false`, which CWL requires of a `when` (§10), the `when` quoted first;
    `None` when it gives one, and when nothing here can tell: the subset cannot parse or type it, or it is not a
    string, which cwltool's schema validation refuses itself."""
    written = condition.written
    if isinstance(written, str) and one_expression(written) is None:
        return f'{written!r} is not one $(...) or ${{...}} expression'
    if condition.type not in (None, BOOLEAN):
        return f'{written!r} must compute a boolean, not a {condition.type}'
    return None


def non_boolean_whens(workflow: ResolvedWorkflow) -> tuple[tuple[ResolvedStep, str], ...]:
    """Each step of `workflow` at any depth, a call among them, in graph order, whose `when` cannot give `true` or
    `false`, with why (`boolean_problem`): the one check of a `when` every target shares."""
    found: list[tuple[ResolvedStep, str]] = []
    for step in workflow.steps:
        if step.condition is not None and (problem := boolean_problem(step.condition)) is not None:
            found.append((step, problem))
        if isinstance(step.run, ResolvedWorkflow):
            found.extend(non_boolean_whens(step.run))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Through:
    """A source resolved through call boundaries (§6): a root input, or an output of a tool step, the step spelled
    by its qualified name."""

    ref: Ref


@dataclass(frozen=True, slots=True)
class Crossing:
    """A source that does not cross a call as one plain source: the call's binding of an input merges, picks,
    defaults or computes; an input the call leaves unbound has a default; or an output's `outputSource` names no
    step output. `call` is the call's qualified name, `port` its workflow's input or output."""

    call: str
    port: str


@dataclass(frozen=True, slots=True)
class ToolStep:
    """A tool step at any depth, each of its inputs' sources resolved through the calls it sits in."""

    step: ResolvedStep
    #: The calls it sits in, outermost first.
    calls: tuple[ResolvedStep, ...]
    #: Each of `step.inputs`' sources, resolved, in order.
    sources: tuple[tuple[Through | Crossing, ...], ...]


def tool_steps(workflow: ResolvedWorkflow) -> tuple[ToolStep, ...]:
    """Every tool step of `workflow` at any depth, in graph order, its sources resolved through call boundaries."""
    return _Flattened(workflow).steps


def output_sources(workflow: ResolvedWorkflow) -> tuple[tuple[Through | Crossing, ...], ...]:
    """Each of `workflow.outputs`' sources, resolved through call boundaries as `tool_steps` resolves an input's."""
    return _Flattened(workflow).outputs


def output_unbound(workflow: ResolvedWorkflow) -> tuple[tuple[tuple[str, str], ...], ...]:
    """For each of `workflow.outputs`, the inputs its resolution reached that a call leaves unbound, with no default.

    An input is `(call, input)`: the qualified name of the call that leaves it unbound and the input's name. Such an
    output has no source in `output_sources`.
    """
    return _Flattened(workflow).unbound


class _Found(NamedTuple):
    """What a reference resolves to, and the unbound inputs (call, input) it reached on the way."""

    sources: tuple[Through | Crossing, ...]
    unbound: tuple[tuple[str, str], ...] = ()


class _Flattened:  # pylint: disable=too-few-public-methods
    """The tool steps and root outputs of a view, resolved through call boundaries, once."""

    def __init__(self, root: ResolvedWorkflow) -> None:
        self._found: list[ToolStep] = []
        roots = {port.name: _Found((Through(InputRef(port.name)),)) for port in root.inputs}
        through = self._level(root, roots, ())
        self.steps = tuple(self._found)
        outputs = [through(port.source) if isinstance(port.source, (InputRef, OutputRef)) else _Found(())
                   for port in root.outputs]
        self.outputs = tuple(found.sources for found in outputs)
        self.unbound = tuple(found.unbound for found in outputs)

    def _level(self, workflow: ResolvedWorkflow, env: Mapping[str, _Found],
               calls: tuple[ResolvedStep, ...]) -> Callable[[Ref], _Found]:
        local = {step.local: step for step in workflow.steps}
        inner: dict[str, Callable[[Ref], _Found]] = {}

        def through(ref: Ref) -> _Found:
            if isinstance(ref, InputRef):
                # The env lacks only an input the enclosing call leaves unbound, with no default.
                return env[ref.name] if ref.name in env else _Found((), ((calls[-1].name, ref.name),))
            producer = local[ref.step]
            if isinstance(producer.run, ResolvedTool):
                return _Found((Through(OutputRef(producer.name, ref.port)),))
            source = next(port.source for port in producer.run.outputs if port.name == ref.port)
            if not isinstance(source, (InputRef, OutputRef)):
                return _Found((Crossing(producer.name, ref.port),))
            return inner[producer.local](source)

        for step in workflow.steps:
            if isinstance(step.run, ResolvedTool):
                self._found.append(ToolStep(step, calls, tuple(
                    tuple(found for ref in port.sources for found in through(ref).sources) for port in step.inputs)))
                continue
            bound = {port.name: port for port in step.inputs}
            child_env: dict[str, _Found] = {}
            for port in step.run.inputs:
                given = bound.get(port.name)
                if given is not None and _plain(given):
                    child_env[port.name] = through(given.sources[0])
                elif given is not None or port.declaration.has_default:
                    child_env[port.name] = _Found((Crossing(step.name, port.name),))
            inner[step.local] = self._level(step.run, child_env, (*calls, step))
        return through


def _plain(port: StepInput) -> bool:
    """Whether a call's binding passes its one source as it is: no merge, pick, default or computed value."""
    return (len(port.sources) == 1 and port.expression is None and port.link_merge is None
            and port.pick_value is None and not port.has_default and port.value_from is None)


class Construct(StrEnum):
    """What a target refuses: the construct of the view a refusal is about."""

    WORKFLOW = 'workflow'
    WORKFLOW_INPUT = 'workflow input'
    WORKFLOW_OUTPUT = 'workflow output'
    STEP = 'step'
    STEP_INPUT = 'input'
    WHEN = 'when'
    SCATTER = 'scatter'
    CALL = 'call'
    REQUIREMENT = 'requirement'
    TOOL = 'tool'
    POLICY = 'policy'


@dataclass(frozen=True, slots=True)
class Refusal:  # pylint: disable=too-many-instance-attributes
    """One construct a target cannot lower, where its author wrote it, and the code of the reason.

    It is reported as its `diagnostic`, an error: `<span>: error [<code>] <construct>[ '<subject>']: <reason>`, then
    `(step '<step>')` for a construct of a step.
    """

    code: SophiosErrorCode
    construct: Construct
    #: The construct's name: an input or output, a requirement class, a policy, or for a tool the tool's file and the
    #: field's path in it, `echo.cwl: inputs.message.inputBinding`.
    subject: str | None
    #: The root workflow's name.
    workflow: str
    #: The qualified name of the step it is about; `None` for a construct of the workflow's own.
    step: str | None
    span: SourceSpan | None
    reason: str

    @property
    def message(self) -> str:
        """What it says: the construct, its subject, and the reason."""
        subject = f' {self.subject!r}' if self.subject is not None else ''
        return f'{self.construct}{subject}: {self.reason}'

    @property
    def diagnostic(self) -> Diagnostic:
        """The refusal as Sophios reports any error."""
        return Diagnostic(Severity.ERROR, self.code, self.message, self.span,
                          Locator(step=self.step) if self.step is not None else None)


class NotLowered(ValueError):
    """What a target refuses to lower: every refusal, all at once.

    Its text is its `diagnostics`, one line each, as Sophios prints any error.
    """

    def __init__(self, refusals: tuple[Refusal, ...]) -> None:
        self.refusals = refusals
        self.diagnostics = Diagnostics(refusal.diagnostic for refusal in refusals)
        super().__init__('\n'.join(lines(self.diagnostics)))
