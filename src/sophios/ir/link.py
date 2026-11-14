"""Pure composition and reference linking over workflow graphs."""
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from ..lang import CwlRecord, SophiosErrorCode
from ..lang.compatibility import TypeRelation, reference_relation
from ..lang.diagnostics import Diagnostics, Locator
from .declarations import boundary_declaration, delivered, input_rank, layered, output_rank
from .names import Names
from .types import (
    Binding,
    DeferredObligation,
    DerivedName,
    Edge,
    EdgeOrigin,
    Namespace,
    PhaseResult,
    Port,
    WorkflowPort,
    PortId,
    StepId,
    StepNode,
    WorkflowGraph,
    resolved_items,
)


def link(graph: WorkflowGraph) -> PhaseResult:
    """Compose ``graph`` and discharge every obligation it can prove."""
    diagnostics = Diagnostics()
    merging = _merging(graph)
    graph = _normalize_explicit_edges(graph, graph, merging, diagnostics)
    _check_workflow_inputs(graph, merging, diagnostics)
    definitions = _definitions(graph)
    edges: list[Edge] = []

    for obligation in graph.obligations:
        source = definitions.get(obligation.name)
        if source is None:
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' names an edge nothing in the composed workflow defines.",
                obligation.span, _locator(graph, obligation.sink),
            )
            continue
        if _position(graph, source.step) >= _position(graph, obligation.sink.step):
            # .wic reference §4: a call does not exempt a document from resolving
            # against definitions seen so far, or splitting it would legalize the error.
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' is defined after the step that consumes it. "
                'An edge reference resolves against the definitions before it.',
                obligation.span, _locator(graph, obligation.sink),
            )
            continue
        record = merging.get(obligation.sink)
        for sink in _concrete_input_sinks(graph, obligation.sink):
            edge = Edge(source, sink, obligation.span)
            if (record is None and obligation.sink in merging) \
                    or not _reject_if_disjoint(graph, edge, diagnostics, record):
                edges.append(edge)

    # A call edge into a merging record is not judged.
    edges += [edge for sink, edge in _call_edges(graph)
              if sink in merging or not _reject_if_disjoint(graph, edge, diagnostics)]
    linked = _placed(graph, tuple(dict.fromkeys(edges)))
    return PhaseResult(linked if not diagnostics.has_errors else None, diagnostics)


def _normalize_explicit_edges(graph: WorkflowGraph, universe: WorkflowGraph,
                              merging: dict[PortId, CwlRecord | None], diagnostics: Diagnostics) -> WorkflowGraph:
    """Move wrapper sources to producers and judge every authored local edge, as `merging` says its input
    is judged."""
    def moved(binding: Binding, item: Edge | DeferredObligation | None) -> Edge | DeferredObligation | None:
        if not isinstance(item, Edge):
            return item
        # `universe` resolves the alias to its concrete producer, but the
        # judgment stays scoped to `graph` so ancestor scatter layers
        # outside this local edge's scope are not double-counted.
        edge = replace(item, source=_concrete_output(universe, item.source))
        record = merging.get(binding.sink)
        if record is not None or binding.sink not in merging:
            _reject_if_disjoint(graph, edge, diagnostics, record)
        return edge

    steps: list[StepNode] = []
    for step in graph.steps:
        bindings = []
        for binding in step.bindings:
            resolution = binding.resolution
            if isinstance(resolution, tuple):
                resolution = tuple(moved(binding, item) for item in resolution)
            else:
                resolution = moved(binding, resolution)
            bindings.append(replace(binding, resolution=resolution))
        steps.append(replace(step, bindings=tuple(bindings)))
    children = tuple(_normalize_explicit_edges(child, universe, merging, diagnostics)
                     for child in graph.children)
    return replace(graph, steps=tuple(steps), children=children)


def _merging(graph: WorkflowGraph) -> dict[PortId, CwlRecord | None]:
    """Every input in `graph`'s tree bound by a record whose input is not its one source's value as it is.

    CWL types such an input from the merge, pick or `valueFrom` that makes it,
    so no source of it is judged against the input's own type. A record that
    merges or picks and leaves the result as it is delivers a type of its own,
    which its sources are judged by: the input is held with its record. A
    `valueFrom` computes the input, so nothing is judged: it is held with None.
    """
    return {binding.sink: None if 'valueFrom' in dict(binding.value.fields) else binding.value
            for step in graph.all_steps for binding in step.bindings
            if isinstance(binding.value, CwlRecord) and not binding.value.delivers_its_source}


def _check_workflow_inputs(graph: WorkflowGraph, merging: dict[PortId, CwlRecord | None],
                           diagnostics: Diagnostics) -> None:
    """Apply the same conservative relation to typed workflow-input references."""
    declarations = {port.name: port.declaration.type.canonical
                    for port in graph.workflow_inputs}
    for name, sinks in graph.input_mapping:
        source_type = declarations.get(name)
        for sink in sinks:
            if sink in merging:
                continue
            sink_type = _effective_type(graph, sink, producing=False)
            relation = reference_relation(source_type, sink_type)
            if relation is TypeRelation.DISJOINT:
                diagnostics.error(
                    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
                    f"workflow input '{Names.of(graph).port(name)}' is provably disjoint: "
                    f'{source_type!r} cannot feed {sink_type!r}.',
                    _port(graph, sink).span,
                    _locator(graph, sink),
                )
    for child in graph.children:
        _check_workflow_inputs(child, merging, diagnostics)


def _reject_if_disjoint(graph: WorkflowGraph, edge: Edge, diagnostics: Diagnostics,
                        record: CwlRecord | None = None) -> bool:
    """Diagnose one edge only when the language relation proves rejection, judged in its own scope.

    Ancestor scatter layers that wrap both endpoints alike are excluded; they
    would inflate the comparison unevenly. Where `record` merges or picks, the
    source is judged as what the record makes of it.
    """
    scope = _graph_at(graph, _owner_namespace(edge))
    produced = _effective_type(scope, edge.source, producing=True)
    if record is not None:
        fields = dict(record.fields)
        produced = delivered(produced, sources=len(record.sources), link_merge=fields.get('linkMerge'),
                             pick_value=fields.get('pickValue'))
    consumed = _effective_type(scope, edge.sink, producing=False)
    if reference_relation(produced, consumed) is not TypeRelation.DISJOINT:
        return False
    diagnostics.error(
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
        f'edge is provably disjoint: {produced!r} cannot feed {consumed!r}.',
        edge.span, _locator(graph, edge.sink),
    )
    return True


def _locator(graph: WorkflowGraph, sink: PortId) -> Locator:
    """Where `sink` sits, for a reader with no source position to open."""
    return Locator(sink.step.name, sink.step.index, Names.of(graph).port(sink.port))


def _position(graph: WorkflowGraph, step_id: StepId) -> tuple[int, ...]:
    """Document order for a step: the indices on the path down to it, compared lexicographically."""
    return tuple(step.id.index for _owner, step in _step_path(graph, step_id))


def _definitions(graph: WorkflowGraph) -> dict[str, PortId]:
    found: dict[str, PortId] = {}
    for name, port in graph.explicit_edge_defs:
        found.setdefault(name, _concrete_output(graph, port))
    for child in graph.children:
        for name, port in _definitions(child).items():
            found.setdefault(name, port)
    return found


def _concrete_output(graph: WorkflowGraph, port: PortId) -> PortId:
    """The step output `port` stands for: a call's output is the one its child maps it to, all the way down."""
    child = _graph_at(graph, port.step.namespace).child(port.step)
    mapped = dict(child.output_mapping).get(port.port) if child is not None else None
    return port if child is None or mapped is None else _concrete_output(child, mapped)


def _concrete_input_sinks(graph: WorkflowGraph, port: PortId) -> tuple[PortId, ...]:
    """The step inputs `port` feeds: a call's input feeds the ones its child maps it to."""
    child = _graph_at(graph, port.step.namespace).child(port.step)
    mapped = dict(child.input_mapping).get(port.port) if child is not None else None
    return mapped or (port,)


def _call_edges(graph: WorkflowGraph) -> Iterator[tuple[PortId, Edge]]:
    """Each edge a call step's binding carries on into its child, with the call's own input."""
    for step in graph.steps:
        child = graph.child(step.id)
        inner = dict(child.input_mapping) if child is not None else {}
        for binding in step.bindings:
            for item in resolved_items(binding.resolution):
                if isinstance(item, Edge):
                    for sink in inner.get(binding.sink.port, ()):
                        yield binding.sink, Edge(item.source, sink, item.span)
    for child in graph.children:
        yield from _call_edges(child)


def _effective_type(graph: WorkflowGraph, port: PortId, *, producing: bool) -> Any:
    return layered(_port(graph, port).type, _rank(graph, port, producing=producing)).canonical


def _rank(graph: WorkflowGraph, port: PortId, *, producing: bool) -> int:
    """The array layers every scatter between `graph` and `port` adds."""
    path = _step_path(graph, port.step)
    if producing:
        return sum(output_rank(step) for _owner, step in path)
    rank = input_rank(path[-1][1], port.port)
    # A wrapper's `scatter:` names the callee's declared input, which
    # `input_mapping` relates to the port it feeds.
    for index, (_owner, wrapper) in enumerate(path[:-1]):
        child = path[index + 1][0]
        crossing = next((name for name, sinks in child.input_mapping if port in sinks), None)
        if crossing is not None:
            rank += input_rank(wrapper, crossing)
    return rank


def _step_path(graph: WorkflowGraph,
               step_id: StepId) -> tuple[tuple[WorkflowGraph, StepNode], ...]:
    """Each call from `graph` down to `step_id`, then the step, each with the graph it is a step of."""
    path: list[tuple[WorkflowGraph, StepNode]] = []
    for part in (*step_id.namespace.parts[len(graph.namespace.parts):], step_id):
        path.append((graph, next(step for step in graph.steps if step.id == part)))
        graph = graph.child(part) or graph
    return tuple(path)


def _port(graph: WorkflowGraph, port_id: PortId) -> Port:
    return next(port for step in graph.all_steps for port in step.inputs + step.outputs
                if port.id == port_id)


def _owner_namespace(edge: Edge) -> Namespace:
    left, right = edge.source.step.namespace.parts, edge.sink.step.namespace.parts
    common: list[StepId] = []
    for source_part, sink_part in zip(left, right):
        if source_part != sink_part:
            break
        common.append(source_part)
    return Namespace(tuple(common))


def _graph_at(graph: WorkflowGraph, namespace: Namespace) -> WorkflowGraph:
    """The tree node whose own namespace is exactly `namespace`.

    Always found: callers derive `namespace` as an ancestor-or-self of `graph`.
    """
    if graph.namespace == namespace:
        return graph
    child = next(child for child in graph.children
                 if _namespace_contains(child.namespace, namespace))
    return _graph_at(child, namespace)


def _placed(graph: WorkflowGraph, edges: tuple[Edge, ...]) -> WorkflowGraph:
    """`graph` and every graph beneath it, composed: each holds the edges it is the lowest to enclose,
    has a boundary input for every edge that enters it, and maps each output to its concrete producer."""
    current = replace(graph, children=tuple(_placed(child, edges) for child in graph.children))
    inputs = list(current.workflow_inputs)
    mappings = dict(current.input_mapping)
    for edge in edges:
        inside = edge.sink.step.namespace
        if not _namespace_contains(current.namespace, inside) \
                or _namespace_contains(current.namespace, edge.source.step.namespace):
            continue
        # The name the sink is relayed under already, here or by the child it is in: deriving a second
        # name here adds an `inputs:` entry no step ever reads.
        relays = mappings if inside == current.namespace else dict(next(
            child for child in current.children if _namespace_contains(child.namespace, inside)).input_mapping)
        name = next((entry for entry, sinks in relays.items() if edge.sink in sinks),
                    DerivedName(edge.sink.step, edge.sink.port))
        if name not in {port.name for port in inputs}:
            inputs.append(WorkflowPort(name, boundary_declaration(
                _port(current, edge.sink), _rank(current, edge.sink, producing=False))))
        if edge.sink not in mappings.get(name, ()):
            mappings[name] = (*mappings.get(name, ()), edge.sink)
    return replace(
        current, workflow_inputs=tuple(inputs), input_mapping=tuple(mappings.items()),
        linked_edges=graph.linked_edges + tuple(replace(edge, origin=EdgeOrigin.COMPOSED) for edge in edges
                                                if _owner_namespace(edge) == graph.namespace),
        output_mapping=tuple((name, _concrete_output(current, port)) for name, port in graph.output_mapping))


def _namespace_contains(parent: Namespace, child: Namespace) -> bool:
    return child.parts[:len(parent.parts)] == parent.parts
