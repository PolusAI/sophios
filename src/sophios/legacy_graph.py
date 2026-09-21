"""Temporary bridge from the legacy compiler's terminal state to the IR.

This module exists only during the typed-IR migration.  Its input is the set of
semantic components immediately before legacy finalization -- never a
completed CWL document -- and its output is a complete ``WorkflowGraph``.
The old finalizer remains independently callable as the differential oracle.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .ir import (
    Direction,
    JobBinding,
    Namespace,
    Port,
    PortDeclaration,
    PortId,
    PortType,
    ProcessRun,
    StepEmission,
    StepId as IrStepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)
from .lang import versions
from .wic_types import Tool, WorkflowInputsFile, Yaml


_WORKFLOW_FIELDS = {
    'cwlVersion', 'class', '$namespaces', '$schemas', 'inputs',
    versions.ANNOTATION_KEY, 'outputs', 'requirements',
}
_STEP_FIELDS = {'id', 'in', 'run', 'out', 'scatter', 'scatterMethod', 'when'}


@dataclass(frozen=True, slots=True)
class LegacyEmissionState:  # pylint: disable=too-many-instance-attributes
    """Terminal semantic components, still separate rather than assembled CWL."""

    name: str
    namespace: tuple[str, ...]
    lang_version: str
    top_level_items: tuple[tuple[str, Any], ...]
    field_order: tuple[str, ...]
    steps: tuple[Yaml, ...]
    tools: tuple[Tool, ...]
    job_inputs: WorkflowInputsFile
    children: tuple[WorkflowGraph, ...] = ()


# The locals spell the one-to-one state-to-graph projection; collapsing them
# into a dict would recreate the untyped representation this bridge removes.
# pylint: disable-next=too-many-locals
def graph_from_legacy_state(state: LegacyEmissionState) -> WorkflowGraph:
    """Build an emission-complete graph from terminal legacy state.

    ``top_level_items`` explicitly excludes ``steps``.  Requiring the steps as
    a separate structured component prevents this compatibility bridge from
    accepting, caching, or replaying a completed CWL workflow.
    """
    top = dict(state.top_level_items)
    here = Namespace(state.namespace)
    child_by_step = {
        child.namespace.parts[-1]: child
        for child in state.children
        if child.namespace.parts
    }
    nodes = tuple(_step_node(here, index, step, tool, child_by_step)
                  for index, (step, tool) in enumerate(
                      zip(state.steps, state.tools, strict=True), start=1))

    namespaces = top.get('$namespaces', {})
    schemas = top.get('$schemas', [])
    # CWL also spells `requirements:` as a list, and a bare `requirements:`
    # parses to None. Sophios merges into the mapping form and models only
    # that; any other shape is residue the graph carries opaquely, so Emit
    # writes back what the document had. The bridge does not decide the shape.
    authored = top.get('requirements', {})
    requirements = authored if isinstance(authored, dict) else {}
    residue = {key: value for key, value in top.items() if key not in _WORKFLOW_FIELDS}
    if 'requirements' in top and not isinstance(authored, dict):
        residue['requirements'] = authored
    return WorkflowGraph(
        namespace=here,
        steps=nodes,
        passthrough=tuple((key, deepcopy(value)) for key, value in residue.items()),
        name=state.name,
        lang_version=state.lang_version,
        cwl_version=str(top['cwlVersion']),
        workflow_inputs=_workflow_ports(top.get('inputs', {}), outputs=False),
        workflow_outputs=_workflow_ports(top.get('outputs', {}), outputs=True),
        job_bindings=tuple(JobBinding(key, deepcopy(value))
                           for key, value in state.job_inputs.items()),
        requirements=tuple((key, deepcopy(value)) for key, value in requirements.items()),
        namespaces=tuple((key, deepcopy(value)) for key, value in namespaces.items()),
        schemas=tuple(deepcopy(schemas)),
        children=state.children,
        field_order=state.field_order,
    )


def legacy_emit(*, top_level_items: Iterable[tuple[str, Any]],
                field_order: Sequence[str], steps: Sequence[Yaml]) -> Yaml:
    """The independently callable old assembly used only as an oracle."""
    values = {key: deepcopy(value) for key, value in top_level_items}
    values['steps'] = deepcopy(list(steps))
    return {key: values[key] for key in field_order}


# pylint: disable-next=too-many-locals
def _step_node(namespace: Namespace, index: int, step: Yaml, tool: Tool,
               children: Mapping[str, WorkflowGraph]) -> StepNode:
    emitted_id = str(step['id'])
    identity = IrStepId(namespace, index, emitted_id)
    tool_inputs = tool.cwl.get('inputs', {})
    tool_outputs = tool.cwl.get('outputs', {})
    inputs = tuple(_process_port(identity, Direction.INPUT, name, declaration)
                   for name, declaration in tool_inputs.items())
    outputs = tuple(_process_port(identity, Direction.OUTPUT, name, declaration)
                    for name, declaration in tool_outputs.items())
    raw_inputs = step.get('in', {})
    raw_outputs = step.get('out', [])
    child = children.get(emitted_id)
    process_id = child.name if child is not None else str(tool.run_path)
    descriptor = StepEmission(
        id=emitted_id,
        inputs=tuple((key, deepcopy(value)) for key, value in raw_inputs.items()),
        run=ProcessRun(deepcopy(step['run']), process_id, child),
        outputs=tuple(deepcopy(raw_outputs)),
        scatter=deepcopy(step.get('scatter')),
        scatter_method=deepcopy(step.get('scatterMethod')),
        when=deepcopy(step.get('when')),
        passthrough=tuple((key, deepcopy(value)) for key, value in step.items()
                          if key not in _STEP_FIELDS),
        field_order=tuple(step),
    )
    return StepNode(identity, inputs=inputs, outputs=outputs, emission=descriptor)


def _process_port(step: IrStepId, direction: Direction, name: str, raw: Any) -> Port:
    declaration = _port_declaration(raw, outputs=False)
    return Port(PortId(step, direction, str(name)), declaration.type, declaration)


def _workflow_ports(raw_ports: Any, *, outputs: bool) -> tuple[WorkflowPort, ...]:
    if not isinstance(raw_ports, dict):
        raise TypeError('legacy finalization requires mapping-form workflow ports')
    ports: list[WorkflowPort] = []
    for name, raw in raw_ports.items():
        declaration = _port_declaration(raw, outputs=outputs)
        has_source = isinstance(raw, dict) and 'outputSource' in raw
        source = deepcopy(raw.get('outputSource')) if has_source else None
        ports.append(WorkflowPort(str(name), declaration, source, has_source))
    return tuple(ports)


def _port_declaration(raw: Any, *, outputs: bool) -> PortDeclaration:
    if not isinstance(raw, dict):
        return PortDeclaration(PortType(deepcopy(raw)), shorthand=True)
    reserved = {'type', 'format', 'default'}
    if outputs:
        reserved.add('outputSource')
    return PortDeclaration(
        type=PortType(deepcopy(raw.get('type'))),
        format=deepcopy(raw.get('format')),
        has_format='format' in raw,
        default=deepcopy(raw.get('default')),
        has_default='default' in raw,
        passthrough=tuple((key, deepcopy(value)) for key, value in raw.items()
                          if key not in reserved),
        field_order=tuple(raw),
    )
