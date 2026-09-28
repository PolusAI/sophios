"""Pure fixed-point edge inference over a linked workflow graph."""
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from ..lang import SophiosErrorCode
from ..lang.diagnostics import Diagnostics
from .declarations import boundary_declaration, port_declaration
from .link import attach_step_children
from .resolve import RegistrySnapshot
from .stepin import direct_sink
from .types import (
    AuthoredName,
    DerivedName,
    Direction,
    Edge,
    EdgeOrigin,
    Port,
    PortDeclaration,
    PortId,
    PortName,
    PortType,
    ProcessRun,
    RegistryKey,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)


@dataclass(frozen=True, slots=True)
class InferencePolicy:
    """Every choice that can alter inference, supplied by the caller."""

    disabled: bool = False
    use_naming_conventions: bool = False
    renaming_conventions: tuple[tuple[str, str], ...] = ()
    insert_steps_automatically: bool = False
    format_rules: tuple[tuple[str, str], ...] = ()
    iteration_limit: int = 100


@dataclass(frozen=True, slots=True, order=True)
class Insertion:
    """One registry process Infer may insert speculatively."""

    namespace: str
    name: str
    run_path: str
    inputs: tuple[tuple[AuthoredName, PortDeclaration], ...]
    outputs: tuple[tuple[AuthoredName, PortDeclaration], ...]


@dataclass(frozen=True, slots=True)
class InsertionCatalog:
    """An immutable, deterministically ordered converter whitelist."""

    entries: tuple[Insertion, ...] = ()

    @classmethod
    def from_registry(cls, registry: RegistrySnapshot) -> 'InsertionCatalog':
        """Project whitelisted converter interfaces from a registry snapshot."""
        entries: list[Insertion] = []
        for tool in registry.tools:
            if not tool.key.name.startswith('insert_steps_automatically_'):
                continue
            if not isinstance(tool.cwl, dict):
                continue
            inputs = _catalog_ports(tool.cwl.get('inputs', {}), output=False)
            outputs = _catalog_ports(tool.cwl.get('outputs', {}), output=True)
            entries.append(Insertion(tool.key.namespace, tool.key.name, tool.run_path,
                                     inputs, outputs))
        return cls(tuple(sorted(entries)))


@dataclass(frozen=True, slots=True)
class Inferred:
    """An inferred graph when successful and every diagnostic either way."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics
    iterations: int

    @property
    def ok(self) -> bool:
        """Whether Infer produced a graph without errors."""
        return self.graph is not None and not self.diagnostics.has_errors


def infer(graph: WorkflowGraph, policy: InferencePolicy = InferencePolicy(),
          catalog: InsertionCatalog = InsertionCatalog()) -> Inferred:
    """Infer missing edges, with speculative insertion as the only loop."""
    diagnostics = Diagnostics()
    if policy.iteration_limit < 1:
        _exhausted(diagnostics, policy.iteration_limit)
        return Inferred(None, diagnostics, 0)
    current = graph
    for iteration in range(1, policy.iteration_limit + 1):
        current, inserted = _infer_tree(current, policy, catalog)
        if not inserted:
            return Inferred(current, diagnostics, iteration)
    _exhausted(diagnostics, policy.iteration_limit)
    return Inferred(None, diagnostics, policy.iteration_limit)


def types_match(in_type: Any, out_type: Any) -> bool:
    """Legacy inferred-candidate relation, intentionally not a rejection judge."""
    if in_type == out_type:
        return True
    if isinstance(in_type, list) and not isinstance(out_type, list):
        return any(member == out_type for member in in_type)
    if isinstance(out_type, list) and not isinstance(in_type, list):
        return any(member == in_type for member in out_type)
    if isinstance(in_type, list) and isinstance(out_type, list):
        return any(member in in_type for member in out_type)
    return False


def _infer_tree(graph: WorkflowGraph, policy: InferencePolicy,
                catalog: InsertionCatalog) -> tuple[WorkflowGraph, bool]:
    children: list[WorkflowGraph] = []
    for child in graph.children:
        inferred_child, inserted = _infer_tree(child, policy, catalog)
        children.append(inferred_child)
        if inserted:
            return replace(graph, children=tuple(children) + graph.children[len(children):]), True
    graph_with_children = replace(graph, children=tuple(children))
    current = attach_step_children(graph_with_children, graph_with_children.children)
    current = _propagate_child_interface(current)
    if policy.disabled:
        return current, False
    return _infer_local(current, policy, catalog)


# pylint: disable-next=too-many-locals
def _infer_local(graph: WorkflowGraph, policy: InferencePolicy,
                 catalog: InsertionCatalog) -> tuple[WorkflowGraph, bool]:
    linked_edges = list(graph.linked_edges)
    workflow_inputs = list(graph.workflow_inputs)
    input_mapping = list(graph.input_mapping)
    shorthand_relays = list(graph.shorthand_relays)
    steps = list(graph.steps)
    bound = {binding.sink for step in steps for binding in step.bindings}
    # A sink an earlier phase already relayed to a workflow input, or already
    # connected by an edge, satisfies this graph's own boundary port for it
    # even though that port carries no `Binding` of its own -- translated
    # through `direct_sink`, since `input_mapping` and a cross-scope edge both
    # record the deep port a connection actually names, not the local step
    # `Complete` synchronized to relay it.

    def _local(sink: PortId) -> PortId:
        step_id, port_name = direct_sink(graph, sink)
        return PortId(step_id, Direction.INPUT, port_name)

    bound.update(_local(sink) for _name, sinks in graph.input_mapping for sink in sinks)
    bound.update(_local(edge.sink) for edge in linked_edges)

    for position, step in enumerate(steps):
        for port in step.inputs:
            if port.id in bound or not _required(port):
                continue
            source, attempted = _candidate(steps, position, port, policy)
            if source is not None:
                linked_edges.append(Edge(source, port.id, port.span, origin=EdgeOrigin.INFERRED))
                bound.add(port.id)
                continue
            insertion = _insertion_candidate(attempted, port, catalog)
            if policy.insert_steps_automatically and insertion is not None:
                return _insert(graph, position, insertion, policy), True
            current_step = steps[position]
            input_name = DerivedName(current_step.id, port.id.port)
            if input_name not in {item.name for item in workflow_inputs}:
                declaration = port.declaration or port_declaration(port.type.declared)
                declaration = boundary_declaration(replace(
                    declaration,
                    format=(_canonical_boundary_format(declaration.format)
                            if declaration.has_format else declaration.format),
                ))
                workflow_inputs.append(
                    WorkflowPort(input_name, declaration, origin=port.origin or port.id))
            if input_name not in {name for name, _ in input_mapping}:
                input_mapping.append((input_name, (port.id,)))
            if input_name not in shorthand_relays:
                shorthand_relays.append(input_name)
            bound.add(port.id)

    return replace(graph, steps=tuple(steps), linked_edges=tuple(linked_edges),
                   workflow_inputs=tuple(workflow_inputs),
                   input_mapping=tuple(input_mapping),
                   shorthand_relays=tuple(shorthand_relays)), False


def _candidate(steps: list[StepNode], position: int, sink: Port,
               policy: InferencePolicy) -> tuple[PortId | None, tuple[Port, ...]]:
    sink_type = _effective_sink_type(steps[position], sink)
    sink_formats = _formats(sink.declaration)
    break_inference = False
    break_scope: StepId | None = None
    attempted: list[Port] = []
    for producer in reversed(steps[:position]):
        matches: list[Port] = []
        for output in reversed(producer.outputs):
            scope = output.origin.step if output.origin is not None else None
            if break_inference and scope != break_scope:
                break
            attempted.append(output)
            output_type = _effective_source_type(producer, output)
            output_formats = _formats(output.declaration)
            if (types_match(sink_type, output_type)
                    and _formats_match(sink_formats, output_formats, output_type)
                    and not any('_log_' in part for part in _parts(output.id.port))):
                matches.append(output)
            if _rule(producer, output.id.port) == 'break':
                break_inference = True
                break_scope = scope
        if matches:
            return _choose_by_name(matches, sink, policy).id, tuple(attempted)
        if break_inference:
            break
    return None, tuple(attempted)


def _choose_by_name(matches: list[Port], sink: Port,
                    policy: InferencePolicy) -> Port:
    if not policy.use_naming_conventions or len(matches) == 1:
        return matches[0]
    wanted = _authored(sink).replace('input_', '')
    for before, after in policy.renaming_conventions:
        wanted = wanted.replace(before, after)
    named = [port for port in matches
             if _authored(port).replace('output_', '') == wanted]
    return named[0] if named else matches[0]


def _authored(port: Port) -> str:
    """The name the port was written under, wherever that was."""
    return _parts(port.origin.port if port.origin is not None else port.id.port)[-1]


def _parts(name: PortName) -> tuple[str, ...]:
    """The authored names `name` is made of: each step it was exposed
    through, outermost first, then the port a tool or author declared."""
    if isinstance(name, DerivedName):
        return (name.step.name, *_parts(name.port))
    return (name,)


def _rule(step: StepNode, port: PortName) -> str:
    """The inference rule `step` declares for `port`. Rules are keyed by the
    names an author wrote, so a derived name has none."""
    if isinstance(port, DerivedName):
        return 'default'
    return dict(step.inference_rules).get(port, 'default')


def _insertion_candidate(attempted: tuple[Port, ...], sink: Port,
                         catalog: InsertionCatalog) -> Insertion | None:
    sink_formats = _formats(sink.declaration)
    if not sink_formats:
        return None
    previous_formats = {
        value
        for output in attempted
        for value in _formats(output.declaration)
    }
    matches = []
    for insertion in catalog.entries:
        accepted = {value for _, port in insertion.inputs for value in _formats(port)}
        produced = {value for _, port in insertion.outputs for value in _formats(port)}
        if previous_formats & accepted and set(sink_formats) & produced:
            matches.append(insertion)
    return min(matches) if matches else None


def _insert(graph: WorkflowGraph, position: int, insertion: Insertion,
            policy: InferencePolicy) -> WorkflowGraph:
    identity = StepId(graph.namespace,
                      max((step.id.index for step in graph.steps), default=0) + 1,
                      insertion.name)
    inputs = tuple(Port(PortId(identity, Direction.INPUT, name), declaration.type,
                        declaration) for name, declaration in insertion.inputs)
    outputs = tuple(Port(PortId(identity, Direction.OUTPUT, name), declaration.type,
                         declaration) for name, declaration in insertion.outputs)
    run = ProcessRun(insertion.run_path, RegistryKey(insertion.namespace, insertion.name))
    format_rules = dict(policy.format_rules)
    rules = tuple((name, format_rules.get(str(declaration.format), 'default'))
                  for name, declaration in insertion.outputs
                  if declaration.has_format)
    inserted = StepNode(identity, inputs, outputs, run=run,
                        inference_rules=rules, synthesized=True)
    steps = list(graph.steps)
    steps.insert(position, inserted)
    return replace(graph, steps=tuple(steps))


def _propagate_child_interface(graph: WorkflowGraph) -> WorkflowGraph:
    """Give each workflow call the interface its compiled child exposes."""
    children = {child.namespace.parts[-1]: child for child in graph.children
                if child.namespace.parts}
    steps: list[StepNode] = []
    for step in graph.steps:
        if step.run is None:
            steps.append(step)
            continue
        child = children.get(step.id)
        if child is None:
            child = step.run.child
        if child is None:
            steps.append(step)
            continue
        existing_inputs = {port.id.port for port in step.inputs}
        added_inputs = tuple(
            Port(PortId(step.id, Direction.INPUT, workflow_port.name),
                 workflow_port.declaration.type, workflow_port.declaration, step.span,
                 workflow_port.origin)
            for workflow_port in child.workflow_inputs
            if workflow_port.name not in existing_inputs
        )
        existing_outputs = {port.id.port for port in step.outputs}
        added_outputs = tuple(
            Port(PortId(step.id, Direction.OUTPUT, name), declaration.type,
                 declaration, step.span, origin)
            for name, declaration, origin in _exported_outputs(child)
            if name not in existing_outputs
        )
        run = replace(step.run, child=child)
        steps.append(replace(step, inputs=step.inputs + added_inputs,
                             outputs=step.outputs + added_outputs, run=run))
    return replace(graph, steps=tuple(steps))


def _exported_outputs(
        graph: WorkflowGraph) -> tuple[tuple[PortName, PortDeclaration, PortId | None], ...]:
    """The output interface legacy compilation gives a child workflow.

    Each derived name is paired with the port it was derived from, so a reader
    wanting the step or the authored name back has them rather than a slice.
    """
    exported: dict[PortName, tuple[PortDeclaration, PortId | None]] = {
        port.name: (port.declaration, port.origin) for port in graph.workflow_outputs}
    for step in graph.steps:
        for output in step.outputs:
            name = DerivedName(step.id, output.id.port)
            declaration = output.declaration or port_declaration(output.type.declared)
            exported[name] = (replace(
                declaration,
                type=port_declaration(_effective_source_type(step, output)).type,
            ), output.origin or output.id)
    return tuple((name, declaration, origin)
                 for name, (declaration, origin) in exported.items())


def _required(port: Port) -> bool:
    declaration = port.declaration
    if declaration is None:
        return True
    return not ((declaration.has_default and declaration.default is not None)
                or declaration.type.optional)


def _effective_source_type(step: StepNode, port: Port) -> Any:
    raw = _candidate_type(port.type)
    if dict(step.interpreted).get('scatter'):
        return {'type': 'array', 'items': raw}
    return raw


def _effective_sink_type(step: StepNode, port: Port) -> Any:
    raw = _candidate_type(port.type)
    if port.id.port in step.scatter_ports:
        return {'type': 'array', 'items': raw}
    return raw


def _candidate_type(port_type: PortType) -> Any:
    """Canonical type shape used by the legacy candidate matcher."""
    raw = port_type.declared
    if not isinstance(raw, str):
        return raw
    base = raw[:-1] if raw.endswith('?') else raw
    while base.endswith('[]'):
        base = base[:-2]
    value: Any = base
    for _ in range(port_type.array_depth):
        value = {'type': 'array', 'items': value}
    return ['null', value] if port_type.optional else value


def _formats(declaration: PortDeclaration | None) -> tuple[Any, ...]:
    if declaration is None or not declaration.has_format:
        return ()
    value = declaration.format
    return tuple(value) if isinstance(value, list) else (value,)


def _canonical_boundary_format(value: Any) -> Any:
    """Canonicalize a literal IRI without rewriting an opaque CWL expression."""
    if isinstance(value, str) and not any(marker in value for marker in ('$(', '${')):
        return [value]
    return value


def _formats_match(sink: tuple[Any, ...], source: tuple[Any, ...],
                   source_type: Any) -> bool:
    if not sink:
        return True
    if not source:
        return not _type_permits_format(source_type)
    return any(value in sink for value in source)


def _type_permits_format(cwl_type: Any) -> bool:
    if cwl_type == 'File':
        return True
    if isinstance(cwl_type, dict) and cwl_type.get('type') == 'array':
        return _type_permits_format(cwl_type.get('items'))
    if isinstance(cwl_type, list):
        return any(_type_permits_format(member) for member in cwl_type)
    return False


def _catalog_ports(raw: Any, *, output: bool) -> tuple[tuple[AuthoredName, PortDeclaration], ...]:
    if not isinstance(raw, dict):
        return ()
    return tuple((AuthoredName(str(name)), port_declaration(deepcopy(declaration), output=output))
                 for name, declaration in raw.items())


def _exhausted(diagnostics: Diagnostics, limit: int) -> None:
    diagnostics.error(
        SophiosErrorCode.FIXED_POINT_NOT_REACHED,
        f'Inference did not reach a fixed point within {limit} iteration(s).',
    )
