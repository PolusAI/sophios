"""A step's `in:` and `out:`, derived from the finished graph at Emit."""
from ..lang.nodes import CwlRecord, EdgeRef, InlineLiteral, RawCwlRef, UnresolvedName
from .types import (
    AuthoredName,
    DeferredObligation,
    DerivedName,
    Edge,
    EdgeOrigin,
    EmittedValue,
    Expression,
    PortId,
    PortName,
    Record,
    Source,
    SourceList,
    StepId,
    StepNode,
    StepOutputRef,
    WorkflowGraph,
)


def step_inputs(graph: WorkflowGraph) -> dict[StepId, tuple[tuple[PortName, EmittedValue], ...]]:
    """Every step's `in:` entries: bindings, then edges, then relays for sinks still unset,
    then records, each spelled from its own sources."""
    ins: dict[StepId, dict[PortName, EmittedValue]] = {step.id: {} for step in graph.steps}

    for step in graph.steps:
        for binding in step.bindings:
            match binding.value:
                case InlineLiteral():
                    ins[step.id][binding.sink.port] = Source(DerivedName(step.id, binding.sink.port))
                case RawCwlRef(expression=expression):
                    ins[step.id][binding.sink.port] = Expression(expression)
                case UnresolvedName(name=text):
                    ins[step.id][binding.sink.port] = Source(AuthoredName(text), shorthand=True)

    def bind(edge: Edge, *, explicit: bool) -> None:
        target_step, target_port = direct_sink(graph, edge.sink)
        if target_step in ins:
            ins[target_step][target_port] = Source(direct_source(graph, edge.source), shorthand=not explicit)

    for step in graph.steps:
        for binding in step.bindings:
            if isinstance(binding.resolution, Edge):
                bind(binding.resolution, explicit=True)
    for edge in graph.linked_edges:
        bind(edge, explicit=edge.origin is not EdgeOrigin.INFERRED)

    shorthand_relays = set(graph.shorthand_relays)
    for name, sinks in graph.input_mapping:
        for sink in sinks:
            target_step, target_port = direct_sink(graph, sink)
            if target_step in ins:
                ins[target_step].setdefault(target_port, Source(name, shorthand=name in shorthand_relays))

    _spell_records(graph, ins)
    return {step_id: tuple(values.items()) for step_id, values in ins.items()}


def _spell_records(graph: WorkflowGraph, ins: dict[StepId, dict[PortName, EmittedValue]]) -> None:
    """Replace each record-bound entry of `ins` by the record, spelled from its own sources."""
    for step in graph.steps:
        for binding in step.bindings:
            if isinstance(binding.value, CwlRecord) and isinstance(binding.resolution, SourceList):
                relayed = ins[step.id].get(binding.sink.port)
                ins[step.id][binding.sink.port] = Record(
                    tuple(_record_source(graph, source, item, relayed)
                          for source, item in zip(binding.value.sources, binding.resolution.items, strict=True)),
                    binding.value.fields)


def _record_source(graph: WorkflowGraph, source: EdgeRef | UnresolvedName,
                   item: Edge | DeferredObligation | None, relayed: EmittedValue | None) -> PortName | StepOutputRef:
    """How one record source is spelled: its edge's producer, the workflow input it
    names, or, for an edge from outside this document, what Link bound the sink to."""
    if isinstance(item, Edge):
        return direct_source(graph, item.source)
    if item is None:
        return AuthoredName(source.name)
    assert isinstance(relayed, Source), f'Link discharged {item.name!r} into {item.sink} without a source'
    return relayed.ref


def step_out(step: StepNode) -> tuple[PortName, ...]:
    """`step`'s `out:`: a subworkflow call's is its child's `workflow_outputs`, in their order."""
    if step.run is not None and step.run.child is not None:
        return tuple(port.name for port in step.run.child.workflow_outputs)
    return tuple(port.id.port for port in step.outputs)


def _call_of(graph: WorkflowGraph, step: StepId) -> tuple[WorkflowGraph, StepNode]:
    """The child graph holding `step`, and the step of `graph` that calls it."""
    child = next(child for child in graph.children
                 if step.namespace.parts[:len(child.namespace.parts)] == child.namespace.parts)
    return child, next(node for node in graph.steps if node.run is not None and node.run.child == child)


def direct_source(graph: WorkflowGraph, source: PortId) -> StepOutputRef:
    """The `step/port` reference `source` resolves to, one hop into `graph`."""
    if source.step.namespace == graph.namespace:
        return StepOutputRef(source.step, source.port)
    child, call = _call_of(graph, source.step)
    return StepOutputRef(call.id, next(name for name, port in child.output_mapping if port == source))


def direct_sink(graph: WorkflowGraph, sink: PortId) -> tuple[StepId, PortName]:
    """The step and port `sink` is reached through, one hop into `graph`."""
    if sink.step.namespace == graph.namespace:
        return sink.step, sink.port
    child, call = _call_of(graph, sink.step)
    return call.id, next(name for name, sinks in child.input_mapping if sink in sinks)
