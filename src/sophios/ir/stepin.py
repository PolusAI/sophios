"""A step's `in:` and `out:` surface, derived from the finished graph.

Complete records what each binding resolved to, and Link and Infer record
what edges and boundary relays exist. This is the one place those facts turn
into the entries a step is emitted with, so nothing upstream needs to store a
mirror of them. Read once per document, at Emit; nothing here mutates a graph.
"""
from typing import TypeAlias

from ..lang.nodes import EdgeRef, InlineLiteral, RawCwlRef, UnresolvedName
from .types import (
    AuthoredName,
    DerivedName,
    Edge,
    EdgeOrigin,
    EmittedValue,
    Expression,
    PortId,
    PortName,
    Source,
    StepId,
    StepNode,
    StepOutputRef,
    WorkflowGraph,
)

StepInputs: TypeAlias = dict[StepId, dict[PortName, EmittedValue]]


def step_inputs(graph: WorkflowGraph) -> dict[StepId, tuple[tuple[PortName, EmittedValue], ...]]:
    """Every step's `in:` entries, keyed by step.

    `in:` is a mapping, so nothing here orders its keys to match what was
    authored: only list-shaped facts (`out:`, `scatter:`) carry order.
    """
    ins: StepInputs = {step.id: {} for step in graph.steps}

    for step in graph.steps:
        for binding in step.bindings:
            port = next(port for port in step.inputs if port.id == binding.sink)
            match binding.value:
                case InlineLiteral():
                    ins[step.id][port.id.port] = Source(DerivedName(step.id, port.id.port))
                case RawCwlRef(expression=expression):
                    ins[step.id][port.id.port] = Expression(expression)
                case UnresolvedName(name=text):
                    ins[step.id][port.id.port] = Source(AuthoredName(text), shorthand=True)
                case EdgeRef():
                    # Link supplies the concrete source; left unset here so the
                    # source spelling is never guessed.
                    pass

    def bind(edge: Edge, *, explicit: bool) -> None:
        target_step, target_port = direct_sink(graph, edge.sink)
        if target_step not in ins:
            return
        source = direct_source(graph, edge.source)
        ins[target_step][target_port] = Source(source, shorthand=not explicit)

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
            if target_step not in ins:
                continue
            # Complete and Infer mark, at the point each is created, the
            # relays that stand for this same step's own unbound input rather
            # than a value relayed in from elsewhere; everything else -- a
            # workflow input shared across sinks, or a relay chain through an
            # ancestor scope -- is written as an explicit `source:` mapping.
            ins[target_step].setdefault(target_port, Source(name, shorthand=name in shorthand_relays))

    return {step_id: tuple(values.items()) for step_id, values in ins.items()}


def step_out(step: StepNode) -> tuple[PortName, ...]:
    """`step`'s `out:` list, in the order it is emitted.

    A subworkflow call's boundary is exactly its child's `workflow_outputs`,
    which may list ports in an order `step.outputs` itself does not preserve
    (new boundary ports are appended by `Complete` as they are found).
    """
    if step.run is not None and step.run.child is not None:
        return tuple(port.name for port in step.run.child.workflow_outputs)
    return tuple(port.id.port for port in step.outputs)


def direct_source(graph: WorkflowGraph, source: PortId) -> StepOutputRef:
    """The `step/port` reference `source` resolves to, one hop into `graph`.

    Public so a phase that needs to know what an `input_mapping` sink already
    resolves to (Infer's `bound` check) applies the same translation `in:`
    derivation does, rather than comparing a deep identity that never
    appears as one of this graph's own steps.
    """
    if source.step.namespace == graph.namespace:
        return StepOutputRef(source.step, source.port)
    child = next(child for child in graph.children
                 if source.step.namespace.parts[:len(child.namespace.parts)] == child.namespace.parts)
    wrapper = next(step for step in graph.steps
                   if step.run is not None and step.run.child == child)
    boundary = next(name for name, port in child.output_mapping if port == source)
    return StepOutputRef(wrapper.id, boundary)


def direct_sink(graph: WorkflowGraph, sink: PortId) -> tuple[StepId, PortName]:
    """The step and port `sink` is reached through, one hop into `graph`."""
    if sink.step.namespace == graph.namespace:
        return sink.step, sink.port
    child = next(child for child in graph.children
                 if sink.step.namespace.parts[:len(child.namespace.parts)] == child.namespace.parts)
    wrapper = next(step for step in graph.steps
                   if step.run is not None and step.run.child == child)
    boundary = next(name for name, sinks in child.input_mapping if sink in sinks)
    return wrapper.id, boundary
