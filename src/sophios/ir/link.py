"""Pure composition and reference linking over workflow graphs."""
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from ..lang import SophiosErrorCode
from ..lang.compatibility import TypeRelation, reference_relation
from ..lang.diagnostics import Diagnostics
from .declarations import port_declaration
from .types import (
    Edge,
    Namespace,
    Port,
    WorkflowPort,
    PortId,
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
    normalized = _normalize_explicit_edges(attached, attached, diagnostics)
    attached = _attach_children(normalized)
    _check_workflow_inputs(attached, diagnostics)
    definitions = _definitions(attached)
    edges: list[Edge] = []
    discharged: list[PortId] = []

    for obligation in attached.obligations:
        source = definitions.get(obligation.name)
        if source is None:
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' names an edge nothing in the composed workflow defines.",
                obligation.span,
            )
            continue
        if _position(attached, source.step) >= _position(attached, obligation.sink.step):
            # Reference §4.1.2: a reference resolves against the definitions
            # seen so far, and a child may consume what its includer has
            # *already* defined. Lower applies that within one scope; a call
            # does not exempt a document from it, or the same program would
            # be an error flat and legal once split.
            diagnostics.error(
                SophiosErrorCode.UNDEFINED_EDGE,
                f"'!* {obligation.name}' is defined after the step that consumes it. "
                'An edge reference resolves against the definitions before it.',
                obligation.span,
            )
            continue
        for sink in _concrete_input_sinks(attached, obligation.sink):
            edge = Edge(source, sink, obligation.span)
            if _relation(attached, edge) is TypeRelation.DISJOINT:
                diagnostics.error(
                    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
                    f"edge '{obligation.name}' is provably disjoint: "
                    f'{_raw_type(attached, source)!r} cannot feed '
                    f'{_raw_type(attached, sink)!r}.',
                    obligation.span,
                )
                continue
            edges.append(edge)
        discharged.append(obligation.sink)

    for edge in _workflow_call_edges(attached):
        if _reject_if_disjoint(attached, edge, diagnostics):
            continue
        edges.append(edge)
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
            resolution = binding.resolution
            if isinstance(resolution, Edge):
                resolution = replace(
                    resolution,
                    source=_concrete_output(universe, resolution.source),
                )
                _reject_if_disjoint(universe, resolution, diagnostics)
            bindings.append(replace(binding, resolution=resolution))
        steps.append(replace(step, bindings=tuple(bindings)))
    children = tuple(_normalize_explicit_edges(child, universe, diagnostics)
                     for child in graph.children)
    return replace(graph, steps=tuple(steps), children=children)


def _check_workflow_inputs(graph: WorkflowGraph, diagnostics: Diagnostics) -> None:
    """Apply the same conservative relation to typed workflow-input references."""
    declarations = {port.name: port.declaration.type.declared
                    for port in graph.workflow_inputs}
    for name, sinks in graph.input_mapping:
        source_type = declarations.get(name)
        for sink in sinks:
            sink_type = _effective_type(graph, sink, producing=False)
            sink_port = _port(graph, sink)
            relation = reference_relation(
                source_type, sink_type, lang_version=graph.lang_version)
            if relation is TypeRelation.DISJOINT:
                diagnostics.error(
                    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
                    f"workflow input '{name}' is provably disjoint: "
                    f'{source_type!r} cannot feed {sink_type!r}.',
                    sink_port.span if sink_port is not None else None,
                )
    for child in graph.children:
        _check_workflow_inputs(child, diagnostics)


def _reject_if_disjoint(graph: WorkflowGraph, edge: Edge,
                        diagnostics: Diagnostics) -> bool:
    """Diagnose one edge only when the language relation proves rejection."""
    if _relation(graph, edge) is not TypeRelation.DISJOINT:
        return False
    diagnostics.error(
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE,
        f'edge is provably disjoint: {_raw_type(graph, edge.source)!r} cannot feed '
        f'{_raw_type(graph, edge.sink)!r}.',
        edge.span,
    )
    return True


def _attach_children(graph: WorkflowGraph) -> WorkflowGraph:
    children = tuple(_attach_children(child) for child in graph.children)
    by_namespace = {child.namespace.parts[-1]: child for child in children
                    if child.namespace.parts}
    steps: list[StepNode] = []
    for step in graph.steps:
        emission = step.emission
        if emission is None:
            steps.append(step)
            continue
        child = by_namespace.get(emission.id)
        run = replace(emission.run, child=child) if child is not None else emission.run
        steps.append(replace(step, emission=replace(emission, run=run)))
    return replace(graph, steps=tuple(steps), children=children)


def _position(graph: WorkflowGraph, step_id: StepId) -> tuple[int, ...]:
    """Document order for a step, as the indices on the path down to it.

    Compared lexicographically, so a step of an enclosing workflow precedes
    everything inside a call that comes after it, and follows everything
    inside a call that comes before.
    """
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
                        if step.emission is not None
                        and step.emission.run.child is not None
                        and step.emission.run.child.namespace == child.namespace), None)
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
    if step is None or step.emission is None or step.emission.run.child is None:
        return port
    child = step.emission.run.child
    mapped = dict(child.output_mapping).get(port.port)
    return _concrete_output(child, mapped) if mapped is not None else port


def _concrete_input_sinks(graph: WorkflowGraph, port: PortId) -> tuple[PortId, ...]:
    step = _step(graph, port.step)
    if step is None or step.emission is None or step.emission.run.child is None:
        return (port,)
    mapped = dict(step.emission.run.child.input_mapping).get(port.port, ())
    return tuple(mapped) if mapped else (port,)


def _workflow_call_edges(graph: WorkflowGraph) -> tuple[Edge, ...]:
    found: list[Edge] = []
    for step in graph.steps:
        child = step.emission.run.child if step.emission is not None else None
        if child is None:
            continue
        child_inputs = dict(child.input_mapping)
        for binding in step.bindings:
            if not isinstance(binding.resolution, Edge):
                continue
            for sink in child_inputs.get(binding.sink.port, ()):
                found.append(Edge(binding.resolution.source, sink, binding.resolution.span))
    for child in graph.children:
        found.extend(_workflow_call_edges(child))
    return tuple(found)


def _relation(graph: WorkflowGraph, edge: Edge) -> TypeRelation:
    source = _effective_type(graph, edge.source, producing=True)
    sink = _effective_type(graph, edge.sink, producing=False)
    return reference_relation(source, sink, lang_version=graph.lang_version)


def _effective_type(graph: WorkflowGraph, port: PortId, *, producing: bool) -> Any:
    raw = _raw_type(graph, port)
    path = _step_path(graph, port.step)
    if not path:
        return raw
    if producing:
        layers = sum(_output_scatter_rank(step) for _owner, step in path)
    else:
        actual = path[-1][1]
        layers = _scatter_keys(actual).count(port.port)
        for index, (_owner, wrapper) in enumerate(path[:-1]):
            child = path[index + 1][0]
            layers += _scatter_keys(wrapper).count(_boundary_name(child, port))
    for _ in range(layers):
        raw = {'type': 'array', 'items': raw}
    return raw


def _scatter_keys(step: StepNode) -> tuple[str, ...]:
    if step.emission is None:
        return ()
    scatter = step.emission.scatter
    if isinstance(scatter, str):
        return (scatter,)
    if isinstance(scatter, list):
        return tuple(item for item in scatter if isinstance(item, str))
    return ()


def _output_scatter_rank(step: StepNode) -> int:
    keys = _scatter_keys(step)
    if not keys:
        return 0
    assert step.emission is not None
    return len(keys) if step.emission.scatter_method == 'nested_crossproduct' else 1


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
                        if step.emission is not None
                        and step.emission.run.child is not None
                        and step.emission.run.child.namespace == child.namespace), None)
        if wrapper is not None and nested:
            return ((graph, wrapper), *nested)
    return ()


def _boundary_name(graph: WorkflowGraph, port: PortId) -> str:
    path = _step_path(graph, port.step)
    if not path:
        return port.port
    step = path[-1][1]
    assert step.emission is not None
    return f'{step.emission.id}___{port.port}'


def _raw_type(graph: WorkflowGraph, port: PortId) -> Any:
    found = _port(graph, port)
    return found.type.declared if found is not None else None


def _port(graph: WorkflowGraph, port_id: PortId) -> Port | None:
    return next((port for step in graph.all_steps for port in step.inputs + step.outputs
                 if port.id == port_id), None)


def _step(graph: WorkflowGraph, step_id: StepId) -> StepNode | None:
    return next((step for step in graph.all_steps if step.id == step_id), None)


def _owner_namespace(edge: Edge) -> Namespace:
    left, right = edge.source.step.namespace.parts, edge.sink.step.namespace.parts
    common: list[str] = []
    for source_part, sink_part in zip(left, right):
        if source_part != sink_part:
            break
        common.append(source_part)
    return Namespace(tuple(common))


def _place_edges(graph: WorkflowGraph, edges: tuple[Edge, ...],
                 discharged: tuple[PortId, ...]) -> WorkflowGraph:
    children = tuple(_place_edges(child, edges, discharged) for child in graph.children)
    local_edges = tuple(edge for edge in edges if _owner_namespace(edge) == graph.namespace)
    local_discharged = tuple(sink for sink in discharged
                             if _namespace_contains(graph.namespace, sink.step.namespace)
                             and not any(_namespace_contains(child.namespace, sink.step.namespace)
                                         for child in children))
    return replace(graph, children=children,
                   composition_edges=graph.composition_edges + local_edges,
                   discharged_obligations=graph.discharged_obligations + local_discharged)


def _expose_cross_scope_inputs(graph: WorkflowGraph,
                               edges: tuple[Edge, ...]) -> WorkflowGraph:
    """Give every edge entering a child an explicit workflow boundary.

    Composition edges remain owned by their lowest common ancestor.  The
    child still needs a typed input through which that edge can enter its CWL
    document; otherwise emission would have to rediscover a semantic path.
    """
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
            if step is None or step.emission is None:
                continue
            name = f'{step.emission.id}___{edge.sink.port}'
        elif any(_namespace_contains(child.namespace, edge.sink.step.namespace)
                 for child in current.children):
            child = next(child for child in current.children
                         if _namespace_contains(child.namespace, edge.sink.step.namespace))
            child_mapping = next(((name, sinks) for name, sinks in child.input_mapping
                                  if edge.sink in sinks), None)
            if child_mapping is None:
                continue
            name = child_mapping[0]
        else:
            continue
        if name not in {port.name for port in inputs}:
            sink_port = _port(current, edge.sink)
            if sink_port is None:
                continue
            declaration = sink_port.declaration
            if declaration is None:
                declaration = port_declaration(deepcopy(sink_port.type.declared))
            effective = _effective_type(current, edge.sink, producing=False)
            declaration = replace(
                declaration,
                type=port_declaration({'type': deepcopy(effective)}).type,
                shorthand=False,
            )
            inputs.append(WorkflowPort(name, declaration))
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
