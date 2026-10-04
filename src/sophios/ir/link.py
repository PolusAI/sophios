"""Pure composition and reference linking over workflow graphs."""
from dataclasses import dataclass, replace
from typing import Any

from ..lang import CwlRecord, SophiosErrorCode
from ..lang.compatibility import TypeRelation, reference_relation
from ..lang.diagnostics import Diagnostics, Locator
from .declarations import boundary_declaration, input_rank, layered, output_rank
from .names import Names
from .types import (
    DerivedName,
    Edge,
    EdgeOrigin,
    Namespace,
    Port,
    WorkflowPort,
    PortId,
    SourceList,
    StepId,
    StepNode,
    WorkflowGraph,
)


@dataclass(frozen=True, slots=True)
class Linked:
    """A linked graph when successful and every diagnostic either way."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        """Whether Link produced a graph with no errors."""
        return self.graph is not None and not self.diagnostics.has_errors


def link(graph: WorkflowGraph) -> Linked:
    """Compose ``graph`` and discharge every obligation it can prove."""
    diagnostics = Diagnostics()
    attached = _attach_children(graph)
    attached = _attach_children(_normalize_explicit_edges(attached, attached, diagnostics))
    _check_workflow_inputs(attached, diagnostics)
    definitions = _definitions(attached)
    unjudged = _merging_sinks(attached)
    edges: list[Edge] = []
    discharged: list[PortId] = []

    for obligation in attached.obligations:
        source = definitions.get(obligation.name)
        if source is None:
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' names an edge nothing in the composed workflow defines.",
                obligation.span, _locator(attached, obligation.sink),
            )
            continue
        if _position(attached, source.step) >= _position(attached, obligation.sink.step):
            # Language guide §4: a call does not exempt a document from resolving
            # against definitions seen so far, or splitting it would legalize the error.
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' is defined after the step that consumes it. "
                'An edge reference resolves against the definitions before it.',
                obligation.span, _locator(attached, obligation.sink),
            )
            continue
        for sink in _concrete_input_sinks(attached, obligation.sink):
            edge = Edge(source, sink, obligation.span)
            if obligation.sink not in unjudged and _relation(attached, edge) is TypeRelation.DISJOINT:
                produced, consumed = _compared_types(attached, edge)
                diagnostics.error(
                    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
                    f"edge '{obligation.name}' is provably disjoint: "
                    f'{produced!r} cannot feed {consumed!r}.',
                    obligation.span, _locator(attached, sink),
                )
                continue
            edges.append(edge)
        discharged.append(obligation.sink)

    edges.extend(_judged_call_edges(attached, unjudged, diagnostics))
    unique_edges = tuple(dict.fromkeys(edges))
    linked = _place_edges(attached, unique_edges, tuple(discharged))
    linked = _expose_cross_scope_inputs(linked, unique_edges)
    linked = _redirect_output_mappings(linked)
    return Linked(linked if not diagnostics.has_errors else None, diagnostics)


def _normalize_explicit_edges(graph: WorkflowGraph, universe: WorkflowGraph,
                              diagnostics: Diagnostics) -> WorkflowGraph:
    """Move wrapper sources to producers and judge every authored local edge."""
    steps: list[StepNode] = []
    for step in graph.steps:
        bindings = []
        for binding in step.bindings:
            judged = not _merges(binding.value)
            resolution = binding.resolution
            if isinstance(resolution, Edge):
                resolution = _normalized(resolution, graph, universe, diagnostics, judged=judged)
            elif isinstance(resolution, SourceList):
                resolution = replace(resolution, items=tuple(
                    _normalized(item, graph, universe, diagnostics, judged=judged)
                    if isinstance(item, Edge) else item for item in resolution.items))
            bindings.append(replace(binding, resolution=resolution))
        steps.append(replace(step, bindings=tuple(bindings)))
    children = tuple(_normalize_explicit_edges(child, universe, diagnostics)
                     for child in graph.children)
    return replace(graph, steps=tuple(steps), children=children)


def _normalized(edge: Edge, graph: WorkflowGraph, universe: WorkflowGraph,
                diagnostics: Diagnostics, *, judged: bool) -> Edge:
    """`edge` from its concrete producer, judged unless its sink merges."""
    edge = replace(edge, source=_concrete_output(universe, edge.source))
    # `universe` resolves the alias to its concrete producer, but the
    # judgment stays scoped to `graph` so ancestor scatter layers
    # outside this local edge's scope are not double-counted.
    if judged:
        _reject_if_disjoint(graph, edge, diagnostics)
    return edge


def _merges(value: object) -> bool:
    """Whether a binding is a record whose input is not its one source's value as it is.

    CWL types such an input from the merge, pick or `valueFrom` that makes it,
    so no source of it is judged against the input's own type.
    """
    return isinstance(value, CwlRecord) and not value.delivers_its_source


def _merging_sinks(graph: WorkflowGraph) -> frozenset[PortId]:
    """Every input in `graph`'s tree bound by a record that `_merges`."""
    return frozenset(binding.sink for step in graph.all_steps for binding in step.bindings
                     if _merges(binding.value))


def _check_workflow_inputs(graph: WorkflowGraph, diagnostics: Diagnostics) -> None:
    """Apply the same conservative relation to typed workflow-input references."""
    declarations = {port.name: port.declaration.type.canonical
                    for port in graph.workflow_inputs}
    unjudged = _merging_sinks(graph)
    for name, sinks in graph.input_mapping:
        source_type = declarations.get(name)
        for sink in sinks:
            if sink in unjudged:
                continue
            sink_type = _effective_type(graph, sink, producing=False)
            relation = reference_relation(
                source_type, sink_type, lang_version=graph.lang_version)
            if relation is TypeRelation.DISJOINT:
                diagnostics.error(
                    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
                    f"workflow input '{Names.of(graph).port(name)}' is provably disjoint: "
                    f'{source_type!r} cannot feed {sink_type!r}.',
                    _port(graph, sink).span,
                    _locator(graph, sink),
                )
    for child in graph.children:
        _check_workflow_inputs(child, diagnostics)


def _reject_if_disjoint(graph: WorkflowGraph, edge: Edge,
                        diagnostics: Diagnostics) -> bool:
    """Diagnose one edge only when the language relation proves rejection."""
    if _relation(graph, edge) is not TypeRelation.DISJOINT:
        return False
    produced, consumed = _compared_types(graph, edge)
    diagnostics.error(
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
        f'edge is provably disjoint: {produced!r} cannot feed {consumed!r}.',
        edge.span, _locator(graph, edge.sink),
    )
    return True


def _locator(graph: WorkflowGraph, sink: PortId) -> Locator:
    """Where `sink` sits, for a reader with no source position to open."""
    return Locator(sink.step.name, sink.step.index, Names.of(graph).port(sink.port))


def _attach_children(graph: WorkflowGraph) -> WorkflowGraph:
    children = tuple(_attach_children(child) for child in graph.children)
    return attach_step_children(graph, children)


def attach_step_children(graph: WorkflowGraph,
                         children: tuple[WorkflowGraph, ...]) -> WorkflowGraph:
    """Attach ``children`` to the steps that call them, at this level only.

    Shared by Link (which recurses itself, see ``_attach_children`` above)
    and Infer (whose own tree walk already recursed before calling this).
    """
    by_namespace = {child.namespace.parts[-1]: child for child in children
                    if child.namespace.parts}
    steps: list[StepNode] = []
    for step in graph.steps:
        if step.run is None:
            steps.append(step)
            continue
        child = by_namespace.get(step.id)
        run = replace(step.run, child=child) if child is not None else step.run
        steps.append(replace(step, run=run))
    return replace(graph, steps=tuple(steps), children=children)


def _position(graph: WorkflowGraph, step_id: StepId) -> tuple[int, ...]:
    """Document order for a step: the indices on the path down to it, compared lexicographically."""
    for step in graph.steps:
        if step.id == step_id:
            return (step.id.index,)
    for child in graph.children:
        if not _namespace_contains(child.namespace, step_id.namespace):
            continue
        nested = _position(child, step_id)
        if not nested:
            continue
        wrapper = next((step for step in graph.steps
                        if step.run is not None
                        and step.run.child is not None
                        and step.run.child.namespace == child.namespace), None)
        if wrapper is not None:
            return (wrapper.id.index, *nested)
    return ()


def _definitions(graph: WorkflowGraph) -> dict[str, PortId]:
    found: dict[str, PortId] = {}
    for name, port in graph.explicit_edge_defs:
        found.setdefault(name, _concrete_output(graph, port))
    for child in graph.children:
        for name, port in _definitions(child).items():
            found.setdefault(name, port)
    return found


def _concrete_output(graph: WorkflowGraph, port: PortId) -> PortId:
    step = _step(graph, port.step)
    if step is None or step.run is None or step.run.child is None:
        return port
    child = step.run.child
    mapped = dict(child.output_mapping).get(port.port)
    return _concrete_output(child, mapped) if mapped is not None else port


def _concrete_input_sinks(graph: WorkflowGraph, port: PortId) -> tuple[PortId, ...]:
    step = _step(graph, port.step)
    if step is None or step.run is None or step.run.child is None:
        return (port,)
    mapped = dict(step.run.child.input_mapping).get(port.port, ())
    return tuple(mapped) if mapped else (port,)


def _judged_call_edges(graph: WorkflowGraph, unjudged: frozenset[PortId],
                       diagnostics: Diagnostics) -> tuple[Edge, ...]:
    """Each call edge but those proved disjoint; one into a merging record is not judged."""
    return tuple(edge for sink, edge in _workflow_call_edges(graph)
                 if sink in unjudged or not _reject_if_disjoint(graph, edge, diagnostics))


def _workflow_call_edges(graph: WorkflowGraph) -> tuple[tuple[PortId, Edge], ...]:
    """Each edge a call step's binding carries on into its child, beside the call's own input."""
    found: list[tuple[PortId, Edge]] = []
    for step in graph.steps:
        child = step.run.child if step.run is not None else None
        if child is None:
            continue
        child_inputs = dict(child.input_mapping)
        for binding in step.bindings:
            resolution = binding.resolution
            items = resolution.items if isinstance(resolution, SourceList) else (resolution,)
            for item in items:
                if not isinstance(item, Edge):
                    continue
                for sink in child_inputs.get(binding.sink.port, ()):
                    found.append((binding.sink, Edge(item.source, sink, item.span)))
    for child in graph.children:
        found.extend(_workflow_call_edges(child))
    return tuple(found)


def _compared_types(graph: WorkflowGraph, edge: Edge) -> tuple[Any, Any]:
    """The two types `_relation` judges, in the scope it judges them in."""
    scope = _graph_at(graph, _owner_namespace(edge))
    return (_effective_type(scope, edge.source, producing=True),
            _effective_type(scope, edge.sink, producing=False))


def _relation(graph: WorkflowGraph, edge: Edge) -> TypeRelation:
    """Judge one edge in its own scope.

    Ancestor scatter layers that wrap both endpoints alike are excluded; they
    would inflate the comparison unevenly.
    """
    scope = _graph_at(graph, _owner_namespace(edge))
    source, sink = _compared_types(graph, edge)
    return reference_relation(source, sink, lang_version=scope.lang_version)


def _effective_type(graph: WorkflowGraph, port: PortId, *, producing: bool) -> Any:
    return layered(_port(graph, port).type, _rank(graph, port, producing=producing)).canonical


def _rank(graph: WorkflowGraph, port: PortId, *, producing: bool) -> int:
    """The array layers every scatter between `graph` and `port` adds."""
    path = _step_path(graph, port.step)
    if producing:
        return sum(output_rank(step) for _owner, step in path)
    if not path:
        return 0
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
    local = next((step for step in graph.steps if step.id == step_id), None)
    if local is not None:
        return ((graph, local),)
    for child in graph.children:
        if not _namespace_contains(child.namespace, step_id.namespace):
            continue
        nested = _step_path(child, step_id)
        wrapper = next((step for step in graph.steps
                        if step.run is not None
                        and step.run.child is not None
                        and step.run.child.namespace == child.namespace), None)
        if wrapper is not None and nested:
            return ((graph, wrapper), *nested)
    return ()


def _port(graph: WorkflowGraph, port_id: PortId) -> Port:
    return next(port for step in graph.all_steps for port in step.inputs + step.outputs
                if port.id == port_id)


def _step(graph: WorkflowGraph, step_id: StepId) -> StepNode | None:
    return next((step for step in graph.all_steps if step.id == step_id), None)


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


def _place_edges(graph: WorkflowGraph, edges: tuple[Edge, ...],
                 discharged: tuple[PortId, ...]) -> WorkflowGraph:
    children = tuple(_place_edges(child, edges, discharged) for child in graph.children)
    local_edges = tuple(replace(edge, origin=EdgeOrigin.COMPOSED)
                        for edge in edges if _owner_namespace(edge) == graph.namespace)
    local_discharged = tuple(sink for sink in discharged
                             if _namespace_contains(graph.namespace, sink.step.namespace)
                             and not any(_namespace_contains(child.namespace, sink.step.namespace)
                                         for child in children))
    return replace(graph, children=children,
                   linked_edges=graph.linked_edges + local_edges,
                   discharged_obligations=graph.discharged_obligations + local_discharged)


def _expose_cross_scope_inputs(graph: WorkflowGraph,
                               edges: tuple[Edge, ...]) -> WorkflowGraph:
    """Give every edge entering a child an explicit workflow boundary input."""
    children = tuple(_expose_cross_scope_inputs(child, edges) for child in graph.children)
    current = replace(graph, children=children)
    inputs = list(current.workflow_inputs)
    mappings = list(current.input_mapping)
    for edge in edges:
        if not _namespace_contains(current.namespace, edge.sink.step.namespace):
            continue
        if _namespace_contains(current.namespace, edge.source.step.namespace):
            continue
        if edge.sink.step.namespace == current.namespace:
            step = _step(current, edge.sink.step)
            if step is None or step.run is None:
                continue
            # Reuse a sink already relayed by name, or deriving a second name
            # here adds an `inputs:` entry no step ever reads.
            relayed = next((entry for entry, sinks in mappings if edge.sink in sinks), None)
            derived_from: PortId | None = None if relayed is not None else edge.sink
            name = relayed if relayed is not None else DerivedName(step.id, edge.sink.port)
        elif any(_namespace_contains(child.namespace, edge.sink.step.namespace)
                 for child in current.children):
            child = next(child for child in current.children
                         if _namespace_contains(child.namespace, edge.sink.step.namespace))
            child_mapping = next(((name, sinks) for name, sinks in child.input_mapping
                                  if edge.sink in sinks), None)
            if child_mapping is None:
                continue
            name = child_mapping[0]
            derived_from = next((port.origin for port in child.workflow_inputs
                                 if port.name == name), None)
        else:
            continue
        if name not in {port.name for port in inputs}:
            sink_port = _port(current, edge.sink)
            inputs.append(WorkflowPort(
                name, boundary_declaration(sink_port, _rank(current, edge.sink, producing=False)),
                origin=sink_port.origin or derived_from))
        for index, (existing, sinks) in enumerate(mappings):
            if existing == name:
                if edge.sink not in sinks:
                    mappings[index] = (existing, (*sinks, edge.sink))
                break
        else:
            mappings.append((name, (edge.sink,)))
    return replace(current, workflow_inputs=tuple(inputs), input_mapping=tuple(mappings))


def _namespace_contains(parent: Namespace, child: Namespace) -> bool:
    return child.parts[:len(parent.parts)] == parent.parts


def _redirect_output_mappings(graph: WorkflowGraph) -> WorkflowGraph:
    children = tuple(_redirect_output_mappings(child) for child in graph.children)
    temporary = replace(graph, children=children)
    mappings = tuple((name, _concrete_output(temporary, port))
                     for name, port in graph.output_mapping)
    return replace(temporary, output_mapping=mappings)
