"""The resolved view: a read-only reading of a compiled graph, for a target to lower.

It resolves and parses what the graph already says and nothing more: each step
input's sources at its level, its record fields and the type it delivers, the
parsed and typed `when`, `scatter`, workflow inputs and outputs, and the run
policy. A construct Sophios took from CWL unchanged means what CWL v1.2 says;
what Sophios invented (edge inference, the names it makes, anchors) is
resolved before the view exists, and the view holds only the result, as
references.

`resolve` builds the view from a finished `WorkflowGraph` and the tool document
each tool step runs; it never reads a workflow's emitted CWL and refuses
nothing. Refusing is a target's job. Every name is spelled by `Names`, so a
name in the view is the name the CWL target writes.
"""
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import NamedTuple, TypeAlias

from ..lang.needs import Inherited
from ..lang.nodes import Value
from ..lang.spans import SourceSpan
from ..lang.subset import Expr, check
from ..lang.types import (
    Array, Atom, Merge, Pick, PortDeclaration, ScatterMethod, Type, Unknown, delivered, layered, nullable, spell,
    without_null,
)
from ..lang.unread import UnreadField
from .declarations import output_rank, required as needs_value, written_order
from .names import Names
from .policy import RunPolicy
from .stepin import step_inputs
from ..lang.tools import NeedKind, ToolNode
from ..lang.values import read_value
from .types import (
    DerivedName, EmittedValue, Expression, Guard, PortName, Record, Source, StepId,
    StepNode, StepOutputRef, WorkflowGraph, WorkflowPort,
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
class WorkflowInput:  # pylint: disable=too-many-instance-attributes
    """A workflow input, and this workflow's own job value for it: the graph's, and on the root, this run's."""

    name: str
    declaration: PortDeclaration
    value: Value = None
    has_value: bool = False
    #: The step and input whose written value this input holds, when the compiler made it from one.
    made_from: tuple[str, str] | None = None
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class Verbatim:
    """An `outputSource` that names nothing a target can follow: text no step output or input matches, or anything
    that is not text. Written back as it was."""

    value: Value


#: What a workflow output's `outputSource` is: a reference; the text as written; or `None` for no `outputSource`.
OutputSource: TypeAlias = Ref | Verbatim | None


@dataclass(frozen=True, slots=True)
class WorkflowOutput:
    """A workflow output and its `outputSource` at this workflow's level."""

    name: str
    declaration: PortDeclaration
    output_source: OutputSource
    span: SourceSpan | None

    @property
    def source(self) -> Ref | str | None:
        """The reference, or the text Link left unresolved, as written; `None` for no `outputSource` or one that is
        not text."""
        match self.output_source:
            case InputRef() | OutputRef() as ref:
                return ref
            case Verbatim(value=str() as text):
                return text
            case Verbatim() | None:
                return None


def spelling(ref: Ref) -> str:
    """The text a `source:` or `outputSource:` writes for `ref`."""
    match ref:
        case InputRef(name=name):
            return name
        case OutputRef(step=step, port=port):
            return f'{step}/{port}'


@dataclass(frozen=True, slots=True)
class Raw:
    """`in: {x: text}`: text the target reads as the source or expression it is, kept as written. A name
    `--allow_raw_cwl` lets through unresolved is one."""

    text: str


@dataclass(frozen=True, slots=True)
class Entry:
    """`in: {x: src}`, or `in: {x: {source: ..., label: ...}}`: a WorkflowStepInput. `merge`, `pick` and `default` are
    what its fields mean; `passthrough` holds the fields no meaning holds, in the order the author wrote them, and
    `unread` holds, with its refusal, each of those a target that reads none of them must refuse. A merge or a pick
    the standard does not have is an `Unknown`, for the target that cannot run it to refuse by name."""

    sources: tuple[Ref, ...]
    passthrough: tuple[tuple[str, Value], ...]
    merge: Merge | Unknown | None = None
    pick: Pick | Unknown | None = None
    computes: bool = False
    default: Value = None
    has_default: bool = False
    unread: tuple[UnreadField, ...] = ()


#: What a step's `in:` entry says, in the form it was written.
StepValue: TypeAlias = Raw | Entry


@dataclass(frozen=True, slots=True)
class StepInput:
    """One entry of a step's `in:`, in the order the CWL target writes it."""

    name: str
    #: The tool's or nested workflow's declaration; `None` for an input the tool does not declare.
    declared: PortDeclaration | None
    #: What the entry says, once: everything else but `type` is read from it.
    value: StepValue
    #: The type of the value the step input delivers.
    type: Type
    span: SourceSpan | None

    @property
    def sources(self) -> tuple[Ref, ...]:
        """The references it takes its value from."""
        match self.value:
            case Entry(sources=sources):
                return sources
            case Raw():
                return ()

    @property
    def passthrough(self) -> tuple[tuple[str, Value], ...]:
        """The fields of its mapping no meaning holds, in written order."""
        match self.value:
            case Entry(passthrough=passthrough):
                return passthrough
            case Raw():
                return ()

    @property
    def unread(self) -> tuple[UnreadField, ...]:
        """The fields of its mapping a target that reads none of them must refuse, each with its refusal."""
        return self.value.unread if isinstance(self.value, Entry) else ()

    @property
    def expression(self) -> str | None:
        """A `!cwl` passthrough reference the graph holds unresolved."""
        match self.value:
            case Raw(text=text):
                return text
            case Entry():
                return None

    @property
    def merge(self) -> Merge | Unknown | None:
        """How its sources join, when it says one."""
        return self.value.merge if isinstance(self.value, Entry) else None

    @property
    def pick(self) -> Pick | Unknown | None:
        """Which of the non-null values it takes, when it says one."""
        return self.value.pick if isinstance(self.value, Entry) else None

    @property
    def computes(self) -> bool:
        """Whether it says a `valueFrom` that is not null."""
        return isinstance(self.value, Entry) and self.value.computes

    @property
    def default(self) -> Value:
        """Its `default`, as written."""
        return self.value.default if isinstance(self.value, Entry) else None

    @property
    def has_default(self) -> bool:
        """Whether it has a `default`, whatever the value."""
        return isinstance(self.value, Entry) and self.value.has_default


@dataclass(frozen=True, slots=True)
class Scatter:
    """A step's scatter: its inputs, spelled, in written order, and how it combines them. A method the enum does not
    hold is an `Unknown`, for a target to refuse by name."""

    inputs: tuple[str, ...]
    method: ScatterMethod | Unknown | None


@dataclass(frozen=True, slots=True)
class Condition:
    """A step's authored `when`: its safe-subset tree, where the subset reads the text, and the tree's type over the
    step inputs. The text the subset does not read rides unread in the step's `passthrough`."""

    expr: Expr | None
    type: str | None
    #: Why there is no tree, or why it does not type, in the subset's words.
    problem: str | None
    #: What the author wrote, which a diagnostic quotes.
    text: Value = None


@dataclass(frozen=True, slots=True)
class ResolvedStep:  # pylint: disable=too-many-instance-attributes
    """One step occurrence: what it runs, what it binds, and the keys it carries."""

    #: Its id in its own workflow.
    local: str
    #: Its qualified id, outer~inner: what messages print.
    name: str
    run: 'ToolNode | ResolvedWorkflow'
    inputs: tuple[StepInput, ...]
    #: Its outputs' names, as its out: lists them.
    outputs: tuple[str, ...]
    scatter: Scatter | None
    condition: Condition | None
    #: Every other step key, verbatim and in written order: label, doc, requirements, hints, ... A scatter method no
    #: scatter holds and a `when` the subset does not read are first.
    passthrough: tuple[tuple[str, Value], ...]
    #: The inputs its process must be given a value for (no default, not optional; one it does not declare counts),
    #: spelled, in the order the process declares them.
    required_inputs: tuple[str, ...]
    span: SourceSpan | None
    #: What the step asks of its tool: its own requirements, then its hints.
    needs: tuple[Inherited, ...] = ()
    #: Its other keys that nothing reads (label, doc, ...), each with its refusal; a scatter method with nothing to
    #: scatter is first.
    unread: tuple[UnreadField, ...] = ()
    #: A tool step's requirements and hints from outside its tool, as cwltool gives them to it: for each class,
    #: the innermost requirement written on an enclosing workflow, a call or the step, else the innermost such hint;
    #: requirements first. A call's is empty: its workflow's steps inherit.
    inherited: tuple[Inherited, ...] = ()

    @property
    def quoted_when(self) -> Value:
        """Its `when` as a diagnostic quotes it: what the author wrote; None without one."""
        return None if self.condition is None else self.condition.text


@dataclass(frozen=True, slots=True)
class ResolvedWorkflow:  # pylint: disable=too-many-instance-attributes
    """A workflow: its inputs in the order the CWL target writes them, the outputs kept, and its steps."""

    name: str
    inputs: tuple[WorkflowInput, ...]
    outputs: tuple[WorkflowOutput, ...]
    steps: tuple[ResolvedStep, ...]
    #: `$namespaces`.
    namespaces: tuple[tuple[str, Value], ...]
    #: Every other top-level key, verbatim and in written order: requirements, hints, `$schemas`, doc, ...
    passthrough: tuple[tuple[str, Value], ...]
    #: What it asks of the tools beneath it: its own requirements, then its hints.
    needs: tuple[Inherited, ...]
    #: Its other keys that nothing reads (`$schemas`, doc, ...), each with its refusal.
    unread: tuple[UnreadField, ...]
    #: The compilation's; the same object at every depth.
    policy: RunPolicy
    #: False when the workflow is marked `wic: inlineable: false`.
    inlineable: bool
    span: SourceSpan | None

    @property
    def values(self) -> dict[str, Value]:
        """The job values this workflow holds, by input name."""
        return {port.name: port.value for port in self.inputs if port.has_value}


def resolve(graph: WorkflowGraph, tools: Mapping[StepId, ToolNode], *,
            policy: RunPolicy = RunPolicy()) -> ResolvedWorkflow:
    """The view of a finished compilation's graph.

    Args:
        graph (WorkflowGraph): `CompilationResult.graph`: linked, inferred and completed.
        tools (Mapping[StepId, ToolNode]): The tool every tool step runs, at any depth.
        policy (RunPolicy): The compilation's run policy.

    Returns:
        ResolvedWorkflow: The root workflow, every nested workflow inside its calls.
    """
    names = Names.of(graph)
    return _workflow(graph, _Level(names, tools, policy, {step.id: step for step in graph.all_steps}, graph, {}))


def bind(view: ResolvedWorkflow, *, values: Mapping[str, Value] | None = None,
         outputs: Collection[str] | None = None) -> ResolvedWorkflow:
    """`view` for this run.

    Args:
        view (ResolvedWorkflow): A workflow as `resolve` made it.
        values (Mapping[str, Value] | None): This run's values, by root input as the job spells it, each in
            place of the value the root holds, read as a job is (`read_value`); `None` keeps them.
        outputs (Collection[str] | None): The root outputs to keep; `None` keeps them all.

    Raises:
        ValueError: If `values` or `outputs` names nothing the root has: the caller's mistake, not the user's.
    """
    bound = view.values
    for name in values or {}:
        if name not in {port.name for port in view.inputs}:
            raise ValueError(f'value {name!r} has no declared workflow input')
    bound.update(read_value(values or {}))
    kept = view.outputs
    if outputs is not None:
        if unknown := sorted(set(outputs) - {port.name for port in view.outputs}):
            raise ValueError(f'workflow {view.name!r} has no output {", ".join(map(repr, unknown))}')
        kept = tuple(port for port in view.outputs if port.name in outputs)
    inputs = tuple(replace(port, value=bound.get(port.name), has_value=port.name in bound) for port in view.inputs)
    return replace(view, inputs=inputs, outputs=kept)


def _workflow(graph: WorkflowGraph, outer: '_Level', given: Mapping[str, Type] | None = None) -> ResolvedWorkflow:
    """A workflow's view, inside the level `outer` of the call that runs it, or of nothing for the root; `given` is
    the type each input the call binds holds, which is what its sources deliver."""
    names = outer.names
    order = written_order(graph)
    jobs = {names.port(name): value for name, value in graph.job_bindings}
    inputs = tuple(WorkflowInput(names.port(port.name), port.declaration, jobs.get(names.port(port.name)),
                                 names.port(port.name) in jobs, made_from=_made_from(port.name, names),
                                 span=port.span or graph.span)
                   for port in sorted(graph.workflow_inputs, key=lambda port: order(port.name)))
    level = replace(outer, graph=graph, inputs={
        port.name: (given or {}).get(port.name, port.declaration.type) for port in inputs},
        outside=(*outer.outside, *graph.needs))
    ins = step_inputs(graph)
    steps = tuple(_step(step, graph.child(step.id), ins[step.id], level) for step in graph.steps)
    outputs = tuple(WorkflowOutput(names.port(port.name), port.declaration, _output_source(port, level.inputs, names),
                                   port.span or graph.span)
                    for port in graph.workflow_outputs)
    return ResolvedWorkflow(graph.name, inputs, outputs, steps, graph.namespaces, graph.passthrough, graph.needs,
                            graph.unread, level.policy, graph.inlineable, graph.span)


def _made_from(name: PortName, names: Names) -> tuple[str, str] | None:
    """The step and input a made input's value was written on: what its derived name joins."""
    if not isinstance(name, DerivedName):
        return None
    return names.qualified(name.step), names.port(name.port)


def _output_source(port: WorkflowPort, inputs: Collection[str], names: Names) -> OutputSource:
    """An output's source at its workflow's level, as the CWL target writes it: the step output its `outputSource`
    names; else the workflow input it names; else the value as written; `None` for no `outputSource`."""
    if not port.has_output_source:
        return None
    match port.output_source:
        case StepOutputRef() as ref:
            return _ref(ref, names)
        case str() as text if text in inputs:
            return InputRef(text)
    return Verbatim(port.output_source)


@dataclass(frozen=True, slots=True)
class _Level:
    """What a step's sources are read against: the spelling, the tools, every step of the tree, the step's own
    workflow and the type each of its inputs holds; and the requirements and hints written outside the step,
    outermost first."""

    names: Names
    tools: Mapping[StepId, ToolNode]
    policy: RunPolicy
    everything: Mapping[StepId, StepNode]
    graph: WorkflowGraph
    inputs: Mapping[str, Type]
    outside: tuple[Inherited, ...] = ()


def _inherited(written: tuple[Inherited, ...]) -> tuple[Inherited, ...]:
    """What a tool inherits of the requirements and hints written outside it, outermost first: for each kind of need,
    the innermost requirement, else the innermost hint, as cwltool merges a step's with its workflow's and a lookup
    takes the last requirement of a class, then the last hint (cwltool/workflow.py:202-223, cwltool/utils.py:481-489).
    A need of no kind the targets read, and one the reader refused, is told apart by its name.
    """
    found: dict[NeedKind | str, Inherited] = {}
    for requirement in written:
        kind = requirement.need.kind if requirement.need is not None else NeedKind.OTHER
        key = requirement.name if kind is NeedKind.OTHER else kind
        held = found.get(key)
        if held is None or held.hint or not requirement.hint:
            found[key] = requirement
    return tuple(sorted(found.values(), key=lambda requirement: requirement.hint))


def _step(step: StepNode, child: WorkflowGraph | None, ins: tuple[tuple[PortName, EmittedValue], ...],
          level: _Level) -> ResolvedStep:
    names = level.names
    called = {names.port(port.name): port.declaration for port in child.workflow_inputs} if child is not None else {}
    inputs = tuple(_step_input(name, value, step, called, level) for name, value in ins)
    within = replace(level, outside=(*level.outside, *step.needs))
    run: ToolNode | ResolvedWorkflow = (
        _workflow(child, within, {port.name: port.type for port in inputs}) if child is not None
        else level.tools[step.id])
    outputs = tuple(names.port(port.id.port) for port in step.outputs)
    scatter = (Scatter(tuple(names.port(port) for port in step.scatter.ports), step.scatter.method)
               if step.scatter is not None else None)
    condition = _condition(step.guard, inputs, scatter) if step.guard is not None else None
    needed = tuple(names.port(port.id.port) for port in step.inputs if needs_value(port.declaration))
    return ResolvedStep(names.step(step.id), names.qualified(step.id), run, inputs, outputs, scatter, condition,
                        step.passthrough, needed, step.span, step.needs, step.unread,
                        _inherited(within.outside) if isinstance(run, ToolNode) else ())


# pylint: disable-next=too-many-locals
def _step_input(name: PortName, value: EmittedValue, step: StepNode, called: Mapping[str, PortDeclaration],
                level: _Level) -> StepInput:
    """One `in:` entry as the CWL target writes it, its sources references at this level; `called` holds the
    declaration of each input of the workflow a call runs."""
    spelled_name = level.names.port(name)
    port = next((port for port in step.inputs if port.id.port == name), None)
    declared = called.get(spelled_name, port.declaration if port is not None else None)
    binding = next((binding for binding in step.bindings if binding.sink.port == name), None)
    span = getattr(binding.value, 'span', None) if binding is not None else None
    refs: tuple[PortName | StepOutputRef, ...]
    form: StepValue
    record: Record | None = None
    match value:
        case Source(ref=str() as ref) if level.names.port(ref) not in level.inputs:
            # A name `--allow_raw_cwl` lets through unresolved: raw CWL, as `!cwl` writes it.
            refs, form = (), Raw(str(ref))
        case Source(ref=ref):
            refs, form = (ref,), Entry((_ref(ref, level.names),), ())
        case Record(sources=sources) as record:
            refs, form = sources, Entry(tuple(_ref(ref, level.names) for ref in sources), record.passthrough,
                                        record.merge, record.pick, record.computes, record.default,
                                        record.has_default, record.unread)
        case Expression(text=text):
            refs, form = (), Raw(text)
    return StepInput(spelled_name, declared, form,
                     _delivered_type([_source_type(ref, level) for ref in refs], record, declared), span)


def _ref(ref: PortName | StepOutputRef, names: Names) -> Ref:
    match ref:
        case StepOutputRef(step=step, port=port):
            return OutputRef(names.step(step), names.port(port))
    return InputRef(names.port(ref))


def _source_type(ref: PortName | StepOutputRef, level: _Level) -> Type:
    """A source's type at this level: a workflow input's as declared; a step output's inside one array
    layer per scatter it came through, nullable when its producer, or a call it came out of, has a `when`."""
    match ref:
        case StepOutputRef(step=producer_id, port=port):
            producer = level.everything[producer_id]
            declared = next(candidate for candidate in producer.outputs if candidate.id.port == port)
            produced = layered(declared.type, output_rank(producer))
            return nullable(produced) if _may_skip(producer, port, level) else produced
    return level.inputs.get(level.names.port(ref), Atom.ANY)


def _may_skip(producer: StepNode, port: PortName, level: _Level) -> bool:
    """Whether `producer`'s output `port` may be `null`: the producer, or for a call the tool step that makes the
    output and every call on the way out to it, has a `when`."""
    if producer.guard is not None:
        return True
    child = level.graph.child(producer.id)
    inner = dict(child.output_mapping).get(port) if child is not None else None
    if inner is None:
        return False
    parts = inner.step.namespace.parts
    on_the_way = (*parts[parts.index(producer.id) + 1:], inner.step)
    return any(level.everything[step].guard is not None for step in on_the_way)


def _delivered_type(produced: list[Type], record: Record | None, declared: PortDeclaration | None) -> Type:
    """What a step input holds after its sources, its merge, its pick and its `default`: `Any` after a `valueFrom`;
    the declared type where no source says more; sources of different types merge as `Any`."""
    if record is not None and record.computed:
        return Atom.ANY
    if not produced:
        return declared.type if declared is not None else Atom.ANY
    alike = all(item == produced[0] for item in produced)
    merge, pick = (record.merge, record.pick) if record is not None else (None, None)
    merged = delivered(produced[0] if alike else Atom.ANY, sources=len(produced),
                       merge=merge if isinstance(merge, Merge) else None, pick=pick if isinstance(pick, Pick) else None)
    return without_null(merged)[0] if record is not None and record.has_default else merged


def _seen(port: StepInput, scatter: Scatter | None) -> Type:
    """What a `when` reads of a step input: its value, or one element of it in each job of a scatter."""
    match port.type:
        case Array(items=items) if items is not None and scatter is not None and port.name in scatter.inputs:
            return items
    return port.type


def _condition(guard: Guard, inputs: tuple[StepInput, ...], scatter: Scatter | None) -> Condition:
    if guard.expr is None:
        return Condition(None, None, guard.problem, guard.text)
    try:
        typed = check(guard.expr, {port.name: spell(_seen(port, scatter)) for port in inputs})
    except ValueError as error:
        return Condition(guard.expr, None, str(error), guard.text)
    return Condition(guard.expr, typed, None, guard.text)


@dataclass(frozen=True, slots=True)
class Through:
    """A source resolved through call boundaries: a root input, or an output of a tool step, the step spelled
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
            if isinstance(producer.run, ToolNode):
                return _Found((Through(OutputRef(producer.name, ref.port)),))
            source = next(port.source for port in producer.run.outputs if port.name == ref.port)
            if not isinstance(source, (InputRef, OutputRef)):
                return _Found((Crossing(producer.name, ref.port),))
            return inner[producer.local](source)

        for step in workflow.steps:
            if isinstance(step.run, ToolNode):
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
    return (len(port.sources) == 1 and port.expression is None and port.merge is None
            and port.pick is None and not port.has_default and not port.computes)
