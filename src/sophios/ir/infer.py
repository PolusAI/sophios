"""Pure fixed-point edge inference over a linked workflow graph."""
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from ..lang import SophiosErrorCode
from ..lang.diagnostics import Diagnostics
from .declarations import port_declaration
from .resolve import RegistrySnapshot
from .types import (
    Direction,
    Edge,
    Port,
    PortDeclaration,
    PortId,
    ProcessRun,
    StepEmission,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
    PortType,
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
    inputs: tuple[tuple[str, PortDeclaration], ...]
    outputs: tuple[tuple[str, PortDeclaration], ...]


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
    current = _attach_children(replace(graph, children=tuple(children)))
    current = _propagate_child_inputs(current)
    if policy.disabled:
        return current, False
    return _infer_local(current, policy, catalog)


# pylint: disable-next=too-many-locals
def _infer_local(graph: WorkflowGraph, policy: InferencePolicy,
                 catalog: InsertionCatalog) -> tuple[WorkflowGraph, bool]:
    inferred_edges = list(graph.inferred_edges)
    workflow_inputs = list(graph.workflow_inputs)
    input_mapping = list(graph.input_mapping)
    steps = list(graph.steps)
    bound = {binding.sink for step in steps for binding in step.bindings}
    bound.update(edge.sink for edge in graph.composition_edges)
    bound.update(edge.sink for edge in inferred_edges)

    for position, step in enumerate(steps):
        for port in step.inputs:
            if port.id in bound or not _required(port):
                continue
            source = _candidate(steps, position, port, policy)
            if source is not None:
                inferred_edges.append(Edge(source, port.id, port.span))
                bound.add(port.id)
                continue
            insertion = _insertion_candidate(steps, position, port, catalog)
            if policy.insert_steps_automatically and insertion is not None:
                return _insert(graph, position, insertion, policy), True
            current_step = steps[position]
            input_name = _input_name(current_step, port)
            if input_name not in {item.name for item in workflow_inputs}:
                declaration = port.declaration or port_declaration(port.type.declared)
                declaration = replace(
                    declaration,
                    passthrough=tuple((name, value) for name, value in declaration.passthrough
                                      if name not in {'inputBinding', 'loadContents'}),
                    field_order=tuple(name for name in declaration.field_order
                                      if name not in {'inputBinding', 'loadContents'}),
                )
                workflow_inputs.append(WorkflowPort(input_name, declaration))
            if input_name not in {name for name, _ in input_mapping}:
                input_mapping.append((input_name, (port.id,)))
            steps[position] = _set_emission_input(current_step, port.id.port, input_name)
            bound.add(port.id)

    return replace(graph, steps=tuple(steps), inferred_edges=tuple(inferred_edges),
                   workflow_inputs=tuple(workflow_inputs),
                   input_mapping=tuple(input_mapping)), False


def _candidate(steps: list[StepNode], position: int, sink: Port,
               policy: InferencePolicy) -> PortId | None:
    sink_type = _effective_sink_type(steps[position], sink)
    sink_formats = _formats(sink.declaration)
    break_inference = False
    break_namespace = ''
    for producer in reversed(steps[:position]):
        matches: list[Port] = []
        for output in reversed(producer.outputs):
            namespace = output.id.port.split('___')[-2] if '___' in output.id.port else ''
            if break_inference and namespace != break_namespace:
                break
            output_type = _effective_source_type(producer, output)
            output_formats = _formats(output.declaration)
            if (types_match(sink_type, output_type)
                    and _formats_match(sink_formats, output_formats, output_type)
                    and '_log_' not in output.id.port):
                matches.append(output)
            if dict(producer.inference_rules).get(output.id.port, 'default') == 'break':
                break_inference = True
                break_namespace = namespace
        if matches:
            return _choose_by_name(matches, sink.id.port, policy).id
        if break_inference:
            break
    return None


def _choose_by_name(matches: list[Port], sink_name: str,
                    policy: InferencePolicy) -> Port:
    if not policy.use_naming_conventions or len(matches) == 1:
        return matches[0]
    wanted = sink_name.split('___')[-1].replace('input_', '')
    for before, after in policy.renaming_conventions:
        wanted = wanted.replace(before, after)
    named = [port for port in matches
             if port.id.port.split('___')[-1].replace('output_', '') == wanted]
    return named[0] if named else matches[0]


def _insertion_candidate(steps: list[StepNode], position: int, sink: Port,
                         catalog: InsertionCatalog) -> Insertion | None:
    sink_formats = _formats(sink.declaration)
    if not sink_formats:
        return None
    previous_formats = {
        value
        for step in steps[:position]
        for output in step.outputs
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
    descriptor = StepEmission(
        id=f'{graph.name}__step__{position + 1}__{insertion.name}',
        inputs=(),
        run=ProcessRun(insertion.run_path,
                       f'{insertion.namespace}/{insertion.name}'),
        outputs=tuple(name for name, _ in insertion.outputs),
        field_order=('id', 'run', 'out'),
    )
    format_rules = dict(policy.format_rules)
    rules = tuple((name, format_rules.get(str(declaration.format), 'default'))
                  for name, declaration in insertion.outputs
                  if declaration.has_format)
    inserted = StepNode(identity, inputs, outputs, emission=descriptor,
                        inference_rules=rules, synthesized=True)
    steps = list(graph.steps)
    steps.insert(position, inserted)
    steps = [_renumber_emission(graph.name, index, step)
             for index, step in enumerate(steps, start=1)]
    return replace(graph, steps=tuple(steps))


def _renumber_emission(name: str, index: int, step: StepNode) -> StepNode:
    if step.emission is None:
        return step
    return replace(step, emission=replace(
        step.emission, id=f'{name}__step__{index}__{step.id.name}'))


def _propagate_child_inputs(graph: WorkflowGraph) -> WorkflowGraph:
    children = {child.namespace.parts[-1]: child for child in graph.children
                if child.namespace.parts}
    steps: list[StepNode] = []
    for step in graph.steps:
        if step.emission is None:
            steps.append(step)
            continue
        child = children.get(step.emission.id)
        if child is None:
            child = step.emission.run.child
        if child is None:
            steps.append(step)
            continue
        existing = {port.id.port for port in step.inputs}
        added = tuple(
            Port(PortId(step.id, Direction.INPUT, workflow_port.name),
                 workflow_port.declaration.type, workflow_port.declaration, step.span)
            for workflow_port in child.workflow_inputs if workflow_port.name not in existing
        )
        emission = replace(step.emission, run=replace(step.emission.run, child=child))
        steps.append(replace(step, inputs=step.inputs + added, emission=emission))
    return replace(graph, steps=tuple(steps))


def _attach_children(graph: WorkflowGraph) -> WorkflowGraph:
    by_name = {child.namespace.parts[-1]: child for child in graph.children
               if child.namespace.parts}
    steps = tuple(
        replace(step, emission=replace(
            step.emission,
            run=replace(step.emission.run,
                        child=by_name.get(step.emission.id, step.emission.run.child))))
        if step.emission is not None else step
        for step in graph.steps
    )
    return replace(graph, steps=steps)


def _set_emission_input(step: StepNode, name: str, value: str) -> StepNode:
    if step.emission is None:
        return step
    inputs = dict(step.emission.inputs)
    inputs[name] = value
    order = step.emission.field_order
    if 'in' not in order:
        order = tuple(item for item in order if item != 'id')
        order = ('id', 'in', *order)
    return replace(step, emission=replace(step.emission, inputs=tuple(inputs.items()),
                                          field_order=order))


def _required(port: Port) -> bool:
    declaration = port.declaration
    if declaration is None:
        return True
    return not ((declaration.has_default and declaration.default is not None)
                or declaration.type.optional)


def _input_name(step: StepNode, port: Port) -> str:
    emitted = step.emission.id if step.emission is not None else step.id.name
    return f'{emitted}___{port.id.port}'


def _effective_source_type(step: StepNode, port: Port) -> Any:
    raw = _candidate_type(port.type)
    if step.emission is not None and step.emission.scatter:
        return {'type': 'array', 'items': raw}
    return raw


def _effective_sink_type(step: StepNode, port: Port) -> Any:
    raw = _candidate_type(port.type)
    scatter = step.emission.scatter if step.emission is not None else None
    keys = [scatter] if isinstance(scatter, str) else (
        [item for item in scatter if isinstance(item, str)]
        if isinstance(scatter, list) else [])
    if port.id.port in keys:
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


def _catalog_ports(raw: Any, *, output: bool) -> tuple[tuple[str, PortDeclaration], ...]:
    if not isinstance(raw, dict):
        return ()
    return tuple((str(name), port_declaration(deepcopy(declaration), output=output))
                 for name, declaration in raw.items())


def _exhausted(diagnostics: Diagnostics, limit: int) -> None:
    diagnostics.error(
        SophiosErrorCode.FIXED_POINT_NOT_REACHED,
        f'Inference did not reach a fixed point within {limit} iteration(s).',
    )
