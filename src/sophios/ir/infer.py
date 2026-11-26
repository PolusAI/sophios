"""Pure edge inference over a linked workflow graph, in one pass."""
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from ..lang import SophiosErrorCode, Value
from ..lang.diagnostics import Diagnostics, Locator
from ..lang.types import PortDeclaration, Type, layered, permits_format, types_match
from .complete import call_interfaces, promote_outputs
from .declarations import feeding_declaration, input_rank, output_rank, required
from .names import Names
from .resolve import RegistrySnapshot, ToolDefinition
from .stepin import direct_sink
from .types import (
    AuthoredName,
    DerivedName,
    Direction,
    Edge,
    PhaseResult,
    Port,
    PortId,
    PortName,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)


@dataclass(frozen=True, slots=True)
class InferencePolicy:
    """Every choice that can alter inference, supplied by the caller."""

    disabled: bool = False
    use_naming_conventions: bool = True
    renaming_conventions: tuple[tuple[str, str], ...] = ()
    insert_steps_automatically: bool = False
    format_rules: tuple[tuple[str, str], ...] = ()
    #: A choice between equal candidates is an error rather than a note.
    strict: bool = False


@dataclass(frozen=True, slots=True)
class InsertionCatalog:
    """The converters Infer may insert: the tools named `insert_steps_automatically_*`, in key order, each read
    when an input needs one, whatever language it is written in."""

    tools: tuple[ToolDefinition, ...] = ()

    @classmethod
    def from_registry(cls, registry: RegistrySnapshot) -> 'InsertionCatalog':
        """The converters of a registry snapshot."""
        return cls(tuple(tool for tool in registry.tools if tool.key.name.startswith('insert_steps_automatically_')))


def infer(graph: WorkflowGraph, policy: InferencePolicy = InferencePolicy(),
          catalog: InsertionCatalog = InsertionCatalog()) -> PhaseResult:
    """Infer the missing edges, inserting a converter step where one is wanted and the policy allows."""
    diagnostics = Diagnostics()
    graph = _infer_tree(graph, policy, catalog, diagnostics)
    _unbound_scatter(graph, diagnostics)
    return PhaseResult(None if diagnostics.has_errors else graph, diagnostics)


def _infer_tree(graph: WorkflowGraph, policy: InferencePolicy, catalog: InsertionCatalog,
                diagnostics: Diagnostics) -> WorkflowGraph:
    children = tuple(_infer_tree(child, policy, catalog, diagnostics) for child in graph.children)
    current = call_interfaces(replace(graph, children=children))
    return current if policy.disabled else _infer_local(current, policy, catalog, diagnostics)


# pylint: disable-next=too-many-locals
def _infer_local(graph: WorkflowGraph, policy: InferencePolicy, catalog: InsertionCatalog,
                 diagnostics: Diagnostics) -> WorkflowGraph:
    """Infer the missing edges of one workflow, front to back.

    An input nothing fits is given a converter, when the policy allows one, as an ordinary step before its
    step, wired as any step is; the input is then read again. Inference only looks backwards, so nothing decided
    before the step changes.
    """
    linked_edges = list(graph.linked_edges)
    workflow_inputs = list(graph.workflow_inputs)
    input_mapping = list(graph.input_mapping)
    bound = _bound(graph)
    steps = list(graph.steps)
    choices: list[tuple[StepNode, Port, PortId, int]] = []

    def link(step: StepNode, port: Port, source: PortId | None, others: int) -> None:
        """Feed `port` from `source`, a guess when `others` fit too; with no source, the job gives it."""
        if source is not None:
            linked_edges.append(Edge(source, port.id, port.span, inferred=True))
            if others:
                choices.append((step, port, source, others))
            return
        name = DerivedName(step.id, port.id.port)
        workflow_inputs.append(WorkflowPort(name, feeding_declaration(step, port)))
        input_mapping.append((name, (port.id,)))

    position = 0
    while position < len(steps):
        step = steps[position]
        for port in step.inputs:
            if port.id in bound or not required(port.declaration):
                continue
            source, alternatives = _candidate(steps, position, port, policy)
            others = len(alternatives)
            if source is None and (fitting := _converters(steps, position, port, catalog, policy)):
                for own, (fed, guesses) in _reads(steps, position, fitting[0], policy):
                    link(fitting[0], own, fed, len(guesses))
                steps.insert(position, fitting[0])
                position += 1
                source, alternatives = _candidate(steps, position, port, policy)
                others = len(alternatives) + len(fitting) - 1
            link(step, port, source, others)
        position += 1

    graph = replace(graph, steps=tuple(steps))
    repeated = _repeated(graph)
    linked_edges = _refuse_duplicates(graph, linked_edges, diagnostics, repeated)
    wired = {edge.sink for edge in linked_edges}
    for step, port, source, others in choices:
        if port.id in wired:
            _note_choice(diagnostics, policy, step, port, source, others, repeated)
    return promote_outputs(replace(graph, linked_edges=tuple(linked_edges),
                                   workflow_inputs=tuple(workflow_inputs),
                                   input_mapping=tuple(input_mapping)))


def _refuse_duplicates(graph: WorkflowGraph, edges: list[Edge],
                       diagnostics: Diagnostics, repeated: set[StepId]) -> list[Edge]:
    """`edges` without the inferred edges of every refused duplicate.

    Inputs of one tool step that read the same output are a duplicate. When
    one of them was inferred, it stands only if every input declares file
    formats that the output's declared formats intersect; two written edges
    are never refused. A refused group is reported once, and its inferred
    members are not wired. The tool step is the leaf step that declares the
    input, and the output is the leaf port, however a call exposes it.
    """
    ports = {port.id: port for step in graph.steps for port in (*step.inputs, *step.outputs)}
    groups: dict[tuple[StepId, PortId], list[tuple[Port, Edge]]] = {}
    written = [binding.resolution for step in graph.steps for binding in step.bindings
               if isinstance(binding.resolution, Edge)]
    for edge in (*written, *edges):
        local = direct_sink(graph, edge.sink)
        source = _underlying_output(edge.source)
        for leaf in _leaf_steps(graph, local):
            groups.setdefault((leaf, source), []).append((ports[local], edge))
    refused: set[Edge] = set()
    for members in groups.values():
        if len(members) < 2 or not any(edge.inferred for _, edge in members):
            continue
        output_formats = {value for _, edge in members if edge.source in ports
                          for value in _formats(ports[edge.source].declaration)}
        if all(output_formats & set(_formats(sink.declaration)) for sink, _ in members):
            continue
        inferred = [(sink, edge) for sink, edge in members if edge.inferred]
        if refused.issuperset(edge for _, edge in inferred):
            continue
        refused.update(edge for _, edge in inferred)
        _note_duplicate(diagnostics, members, inferred[0], repeated)
    return [edge for edge in edges if edge not in refused]


def _underlying_output(source: PortId) -> PortId:
    """The leaf output `source` stands for: a call's exposed port is named
    by the step that makes it, down to the tool step."""
    while isinstance(source.port, DerivedName):
        source = PortId(source.port.step, Direction.OUTPUT, source.port.port)
    return source


def _leaf_steps(graph: WorkflowGraph, local: PortId) -> tuple[StepId, ...]:
    """The leaf steps that declare the input `local` of a step of `graph`:
    a call's input belongs to every inner step its input mapping
    sends it to, down through calls inside calls."""
    child = graph.child(local.step)
    if child is None:
        return (local.step,)
    leaves: dict[StepId, None] = {}
    for name, sinks in child.input_mapping:
        if name != local.port:
            continue
        for sink in sinks:
            leaves.update(dict.fromkeys(_leaf_steps(child, direct_sink(child, sink))))
    return tuple(leaves) or (local.step,)


def _note_duplicate(diagnostics: Diagnostics, members: list[tuple[Port, Edge]],
                    first: tuple[Port, Edge], repeated: set[StepId]) -> None:
    """Report the inputs of one tool step that would read one output, and that each needs the edge it should read."""
    source = _spelled(*_declared_at(first[1].source.step, first[1].source.port), repeated)
    sinks = [_spelled(*_declared_at(sink.id.step, sink.id.port), repeated) for sink, _ in members]
    step = members[0][0].id.step
    first_name = _declared_at(first[0].id.step, first[0].id.port)[1]
    diagnostics.error(
        SophiosErrorCode.INFERENCE_DUPLICATE,
        f"{', '.join(sinks[:-1])} and {sinks[-1]} would {'both' if len(sinks) == 2 else 'all'} read {source}; "
        "write the edge each should read",
        first[1].span, Locator(step=step.name, index=step.index, port=first_name))


def _bound(graph: WorkflowGraph) -> set[PortId]:
    """Every step input of `graph` that receives a value.

    A binding, a relay to a workflow input, or an edge. `input_mapping` and a
    cross-scope edge name the deep port a connection reaches, so each is
    translated through `direct_sink` to the local step port it arrives at.
    """
    bound = {binding.sink for step in graph.steps for binding in step.bindings}
    bound.update(direct_sink(graph, sink) for _name, sinks in graph.input_mapping for sink in sinks)
    bound.update(direct_sink(graph, edge.sink) for edge in graph.linked_edges)
    return bound


def _unbound_scatter(graph: WorkflowGraph, diagnostics: Diagnostics,
                     names: Names | None = None) -> None:
    """Report each scattered input still without a value once inference is done,
    spelled as `scatter:` spells it."""
    names = names or Names.of(graph)
    bound = _bound(graph)
    for step in graph.steps:
        for name in step.scatter.ports if step.scatter is not None else ():
            if PortId(step.id, Direction.INPUT, name) not in bound:
                spelled = names.port(name)
                diagnostics.error(
                    SophiosErrorCode.UNKNOWN_SCATTER_PORT,
                    f"step '{step.id.name}' scatters over '{spelled}', but nothing binds it, and a "
                    "scatter needs a value to split. Bind it in `in:`, or drop it from `scatter:`.",
                    step.span, Locator(step=step.id.name, index=step.id.index, port=spelled))
    for child in graph.children:
        _unbound_scatter(child, diagnostics, names)


# pylint: disable-next=too-many-locals
def _candidate(steps: Sequence[StepNode], position: int, sink: Port, policy: InferencePolicy
               ) -> tuple[PortId | None, tuple[PortId, ...]]:
    """The source inferred for `sink`, and the equal candidates it was chosen over.

    The most recent producer with a match wins, and within it the first
    declared match (unless naming conventions single one out); a subworkflow
    call counts its inner steps as producers, the most recent inner step first. The scan goes
    on past the winner only to collect the earlier producers' matches, so
    the choice can be reported; it never changes the choice.
    """
    sink_type = layered(sink.type, input_rank(steps[position], sink.id.port))
    sink_formats = _formats(sink.declaration)
    chosen: Port | None = None
    alternatives: list[PortId] = []
    for producer in reversed(steps[:position]):
        matches: list[Port] = []
        for output in reversed(producer.outputs):
            output_type = layered(output.type, output_rank(producer))
            output_formats = _formats(output.declaration)
            if (types_match(sink_type, output_type)
                    and _formats_match(sink_formats, output_formats, output_type)):
                matches.append(output)
        if matches and chosen is None:
            named = _named(matches, sink, policy)
            chosen = _first_declared(named or matches)
            if len(named) != 1:
                alternatives.extend(match.id for match in matches if match is not chosen)
        elif matches:
            alternatives.extend(match.id for match in matches)
        # A step's `inference:` rules are keyed by the names its author wrote: a derived name has none.
        breaks = {name for name, rule in producer.inference_rules if rule == 'break'}
        if any(output.id.port in breaks for output in producer.outputs):
            break
    return (None, ()) if chosen is None else (chosen.id, tuple(alternatives))


def _first_declared(matches: list[Port]) -> Port:
    """Of `matches`, listed last declared first: the first declared of the most recent step that declares one."""
    leaf = _steps(matches[0].id.port)
    return [match for match in matches if _steps(match.id.port) == leaf][-1]


def _named(matches: list[Port], sink: Port, policy: InferencePolicy) -> list[Port]:
    """The matches whose name the naming conventions pair with `sink`; none
    when the conventions are off. The input's own name is tried first; when it
    pairs with nothing, its renamed name is."""
    if not policy.use_naming_conventions:
        return []
    plain = _leaf(sink.id.port).replace('input_', '')
    renamed = plain
    for before, after in policy.renaming_conventions:
        renamed = renamed.replace(before, after)
    for wanted in (plain, renamed):
        paired = [port for port in matches if _leaf(port.id.port).replace('output_', '') == wanted]
        if paired:
            return paired
    return []


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _note_choice(diagnostics: Diagnostics, policy: InferencePolicy, step: StepNode, port: Port,
                 chosen: PortId, others: int, repeated: set[StepId]) -> None:
    """Note an edge inference guessed, as `sink <- source`, and how many other outputs also fitted.

    The input is named at the step the author wrote, a call's input by the port
    its tool declares, so one guess fanned out inside a call is one note. The
    output is named by the path of steps down to the one that declares it, as
    the outputs that fit may differ only inside a call.
    """
    sink = _leaf(port.id.port)
    edge = f'{_spelled((step.id,), sink, repeated)} <- {_spelled(*_declared_at(chosen.step, chosen.port), repeated)}'
    fit = f"{others} {'other fits' if others == 1 else 'others fit'}"
    locator = Locator(step=step.id.name, index=step.id.index, port=sink)
    if policy.strict:
        diagnostics.error(SophiosErrorCode.INFERENCE_GUESS, f'{edge} was guessed, {fit}; write the edge it should read',
                          port.span, locator)
    else:
        diagnostics.note(SophiosErrorCode.INFERENCE_GUESS, f'{edge} ({fit})', port.span, locator)


def _declared_at(step: StepId, name: PortName) -> tuple[tuple[StepId, ...], str]:
    """The path of steps from `step` down to the one that declares `name`,
    and the name that step declares it under."""
    return (step, *_steps(name)), _leaf(name)


def _repeated(graph: WorkflowGraph) -> set[StepId]:
    """The steps whose name another step of the same workflow also has, down through every call."""
    counts = Counter(step.id.name for step in graph.steps)
    found = {step.id for step in graph.steps if counts[step.id.name] > 1}
    return found.union(*map(_repeated, graph.children))


def _spelled(owner: tuple[StepId, ...], port: str, repeated: set[StepId]) -> str:
    """`port` of the last step of `owner`, as `call.wic/tool.port`: the steps by name, outermost first,
    and a step whose name repeats in its workflow by its position as well, as `touch@2`, as in the emitted CWL."""
    return '/'.join(f'{step.name}@{step.index}' if step in repeated else step.name for step in owner) + f'.{port}'


def _steps(name: PortName) -> tuple[StepId, ...]:
    """The steps `name` was exposed through, outermost first."""
    if isinstance(name, DerivedName):
        return (name.step, *_steps(name.port))
    return ()


def _leaf(name: PortName) -> str:
    """The port a tool or author declared, under the steps `name` runs through."""
    return _leaf(name.port) if isinstance(name, DerivedName) else name


def _converters(steps: Sequence[StepNode], position: int, sink: Port, catalog: InsertionCatalog,
                policy: InferencePolicy) -> list[StepNode]:
    """The converters that would feed `sink`, in key order, each as the step that would stand before `steps[position]`.

    A converter qualifies when one of its outputs fits `sink` and one of its own required inputs fits an output
    upstream of it, by the relation `_candidate` draws edges with: it converts something already there. The job
    gives the inputs nothing upstream fits, as for any step.
    """
    return [converter for converter in (_converter(steps, tool, policy) for tool in catalog.tools)
            if any(fed for _, (fed, _) in _reads(steps, position, converter, policy))
            and _candidate([*steps[:position], converter, steps[position]], position + 1, sink, policy)[0]]


def _reads(steps: Sequence[StepNode], position: int, converter: StepNode,
           policy: InferencePolicy) -> list[tuple[Port, tuple[PortId | None, tuple[PortId, ...]]]]:
    """Each required input of `converter` standing before `steps[position]`, with what `_candidate` finds for it.

    A converter reads the steps the author wrote, never one inserted for another input: a converter never needs
    another converter.
    """
    upstream = [step for step in steps[:position] if not step.synthesized]
    return [(port, _candidate([*upstream, converter], len(upstream), port, policy))
            for port in converter.inputs if required(port.declaration)]


def _converter(steps: Sequence[StepNode], tool: ToolDefinition, policy: InferencePolicy) -> StepNode:
    """The converter `tool` as the step that would stand after `steps`, its ports read as Resolve reads a tool's."""
    identity = StepId(steps[0].id.namespace, max((step.id.index for step in steps), default=0) + 1, tool.key.name)
    node = tool.node
    declared = node.inputs, node.outputs
    inputs, outputs = (tuple(Port(PortId(identity, direction, AuthoredName(port.name)), port.declaration.type,
                                  port.declaration) for port in ports)
                       for direction, ports in zip((Direction.INPUT, Direction.OUTPUT), declared))
    format_rules = dict(policy.format_rules)
    rules = tuple((port.name, format_rules.get(str(port.declaration.format), 'default'))
                  for port in declared[1] if port.declaration.has_format)
    return StepNode(identity, tool.key, inputs, outputs, inference_rules=rules, synthesized=True)


def _formats(declaration: PortDeclaration | None) -> tuple[Value, ...]:
    if declaration is None or not declaration.has_format:
        return ()
    value = declaration.format
    return tuple(value) if isinstance(value, list) else (value,)


def _formats_match(sink: tuple[Value, ...], source: tuple[Value, ...],
                   source_type: Type) -> bool:
    if not sink:
        return True
    if not source:
        return not permits_format(source_type)
    return any(value in sink for value in source)
