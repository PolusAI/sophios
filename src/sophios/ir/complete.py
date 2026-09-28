"""Complete graph facts that are determined by resolved process interfaces.

The seam between semantic inference and emission: it turns facts already
present in a `WorkflowGraph` into the workflow boundary that Link and Infer
compose against, without loading a process, discovering a file, or replaying
source. A boundary name it derives is a `DerivedName`, not text: `ir.names`
spells it, at Emit. It states no document -- requirements, `$namespaces`,
`$schemas`, a step's `run:` path and field order are spelled in `emit.surface`.
"""
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any, TypeVar

from ..lang.nodes import InlineLiteral, UnresolvedName
from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode
from .declarations import feeding_declaration, produced_declaration
from .types import (
    AuthoredName,
    DerivedName,
    Direction,
    JobBinding,
    Port,
    PortDeclaration,
    PortId,
    PortName,
    StepOutputRef,
    WorkflowGraph,
    WorkflowPort,
)


def complete(graph: WorkflowGraph) -> WorkflowGraph:
    """Return a graph carrying every fact its resolved interfaces determine.

    Idempotent, so any phase may call it whenever it needs the facts current:
    calling it before Link makes recursively derived workflow interfaces
    visible to composition, and calling it after Infer materializes newly
    inferred sources and boundary inputs.
    """
    children = tuple(complete(child) for child in graph.children)
    current = replace(graph, children=children)
    current = _synchronize_children(current)
    current = _materialize_bindings(current)
    return _materialize_outputs(current)


def _synchronize_children(graph: WorkflowGraph) -> WorkflowGraph:
    child_by_step = {child.namespace.parts[-1]: child for child in graph.children
                     if child.namespace.parts}
    workflow_inputs = list(graph.workflow_inputs)
    job_bindings = list(graph.job_bindings)
    input_mapping = list(graph.input_mapping)
    shorthand_relays = list(graph.shorthand_relays)
    steps = []

    for step in graph.steps:
        run = step.run
        child = child_by_step.get(step.id)
        if run is None or child is None:
            steps.append(step)
            continue

        inputs = list(step.inputs)
        outputs = list(step.outputs)
        input_names = {port.id.port for port in inputs}
        output_names = {port.id.port for port in outputs}
        for boundary in child.workflow_inputs:
            if boundary.name not in input_names:
                inputs.append(Port(PortId(step.id, Direction.INPUT, boundary.name),
                                   boundary.declaration.type, boundary.declaration, step.span))
        for boundary in child.workflow_outputs:
            if boundary.name not in output_names:
                outputs.append(Port(PortId(step.id, Direction.OUTPUT, boundary.name),
                                    boundary.declaration.type, boundary.declaration, step.span))

        # A child boundary input this step never authored, but the child can
        # satisfy from its own job bindings, is lifted to a workflow input of
        # this document -- Emit renders it, from `input_mapping` alone, as a
        # relay back into the port just added above.
        authored = {binding.sink.port for binding in step.bindings}
        child_jobs = {binding.name: binding.value for binding in child.job_bindings}
        for boundary in child.workflow_inputs:
            if boundary.name in authored or boundary.name not in child_jobs:
                continue
            outer_name = DerivedName(step.id, boundary.name)
            sink = next(port for port in inputs if port.id.port == boundary.name)
            declaration = feeding_declaration(step, sink)
            value = child_jobs[boundary.name]
            _put(workflow_inputs, WorkflowPort(outer_name, declaration))
            _put(job_bindings, JobBinding(outer_name, coerce_job_value(
                str(boundary.name), declaration, value)))
            _put_input_mapping(input_mapping, outer_name, sink.id)
            if outer_name not in shorthand_relays:
                shorthand_relays.append(outer_name)

        steps.append(replace(
            step,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            run=replace(run, child=child),
        ))
    return replace(graph, steps=tuple(steps), workflow_inputs=tuple(workflow_inputs),
                   job_bindings=tuple(job_bindings), input_mapping=tuple(input_mapping),
                   shorthand_relays=tuple(shorthand_relays))


def _materialize_bindings(graph: WorkflowGraph) -> WorkflowGraph:
    workflow_inputs = list(graph.workflow_inputs)
    job_bindings = list(graph.job_bindings)
    input_mapping = list(graph.input_mapping)
    authored_inputs = {port.name for port in graph.workflow_inputs}

    for step in graph.steps:
        if step.run is None:
            continue
        for binding in step.bindings:
            port = next(port for port in step.inputs if port.id == binding.sink)
            match binding.value:
                case InlineLiteral(value=value):
                    name = DerivedName(step.id, port.id.port)
                    declaration = feeding_declaration(step, port)
                    _put(workflow_inputs, WorkflowPort(name, declaration))
                    _put(job_bindings, JobBinding(
                        name, coerce_job_value(str(port.id.port), declaration, value)))
                    _put_input_mapping(input_mapping, name, port.id)
                case UnresolvedName(name=text):
                    authored = AuthoredName(text)
                    if authored in authored_inputs:
                        _put_input_mapping(input_mapping, authored, port.id)
                        _merge_boundary_documentation(workflow_inputs, authored, port.declaration)
                case _:
                    pass
    return replace(graph, workflow_inputs=tuple(workflow_inputs),
                   job_bindings=tuple(job_bindings), input_mapping=tuple(input_mapping))


def _emitted_source(graph: WorkflowGraph, port_id: PortId) -> StepOutputRef | None:
    """The `step/port` reference an emitted document can resolve."""
    step = next((item for item in graph.steps if item.id == port_id.step), None)
    if step is None or step.run is None:
        return None
    return StepOutputRef(step.id, port_id.port)


def _materialize_outputs(graph: WorkflowGraph) -> WorkflowGraph:
    # Record Link's resolved producer, not the authored `outputSource:` string:
    # emission renames every step, so the authored text points nowhere.
    resolved = dict(graph.output_mapping)
    outputs = [
        replace(port, output_source=emitted)
        if port.has_output_source and port.name in resolved
        and (emitted := _emitted_source(graph, resolved[port.name])) is not None
        else port
        for port in graph.workflow_outputs
    ]
    output_mapping = list(graph.output_mapping)
    authored = {port.name for port in outputs}
    for step in graph.steps:
        if step.run is None:
            continue
        for port in step.outputs:
            name = DerivedName(step.id, port.id.port)
            if name in authored:
                continue
            outputs.append(WorkflowPort(name, produced_declaration(step, port),
                                        StepOutputRef(step.id, port.id.port), True))
            output_mapping.append((name, port.id))
    return replace(graph, workflow_outputs=tuple(outputs), output_mapping=tuple(output_mapping))


_Named = TypeVar('_Named', WorkflowPort, JobBinding)


def _put(items: list[_Named], item: _Named) -> None:
    """Append `item` unless an entry with its name is already present."""
    if item.name not in {existing.name for existing in items}:
        items.append(item)


def _put_input_mapping(mappings: list[tuple[PortName, tuple[PortId, ...]]],
                       name: PortName, sink: PortId) -> None:
    for index, (existing, sinks) in enumerate(mappings):
        if existing == name:
            if sink not in sinks:
                mappings[index] = (existing, (*sinks, sink))
            return
    mappings.append((name, (sink,)))


def _merge_boundary_documentation(ports: list[WorkflowPort], name: PortName,
                                  source: PortDeclaration | None) -> None:
    if source is None:
        return
    additions = dict(source.passthrough)
    for index, port in enumerate(ports):
        if port.name != name:
            continue
        declaration = port.declaration
        values = dict(declaration.passthrough)
        for key in ('doc', 'label'):
            addition = _as_text(additions.get(key, ''))
            if not addition:
                continue
            existing = _as_text(values.get(key, ''))
            if existing == addition or (isinstance(existing, str)
                                        and existing.endswith(f'\n{addition}')):
                continue
            values[key] = f'{existing}\n{addition}' if existing else addition
        ports[index] = replace(port, declaration=replace(
            declaration, passthrough=tuple(values.items())))
        return


def _as_text(value: Any) -> Any:
    return '\n'.join(value) if isinstance(value, list) else value


def coerce_job_value(name: str, declaration: PortDeclaration, value: Any) -> Any:
    """`value` in the one plain-JSON form a job document holds for `declaration`.

    A projection: a value already in that form (a lifted child job value, an
    authored `File` object) passes through. Each array layer of the type wraps
    a scalar in a list and keeps a list.
    """
    value = _plain(value)
    if value is None:
        if declaration.type.optional:
            return None
        raise SophiosError.error(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                 f'Required input of type {declaration.type.declared} '
                                 'was not provided.')
    return _coerce_type(name, declaration.type.canonical, value,
                        declaration.format if declaration.has_format else None)


def _coerce_type(name: str, raw: Any, value: Any, fmt: Any) -> Any:
    if isinstance(raw, list):
        non_null = [item for item in raw if item != 'null']
        arrays = [item for item in non_null if isinstance(item, dict)
                  and item.get('type') == 'array']
        raw = arrays[0] if arrays else (non_null[0] if len(non_null) == 1 else non_null)
    if isinstance(raw, dict) and raw.get('type') == 'array':
        values = value if isinstance(value, list) else [value]
        return [_coerce_type(name, raw.get('items'), item, fmt) for item in values]
    return _coerce_scalar(name, raw, value, fmt)


def _plain(value: Any) -> Any:
    """Return `value` with any nested `InlineLiteral` replaced by its data."""
    if isinstance(value, InlineLiteral):
        return _plain(value.value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _coerce_scalar(name: str, raw: Any, value: Any, fmt: Any) -> Any:
    if raw in ('File', 'Directory'):
        if isinstance(value, str):
            value = {'class': raw, 'location': value}
        elif not isinstance(value, dict) or value.get('class') != raw:
            raise _mismatch(name, raw, value)
        result = deepcopy(value)
        if raw == 'File' and fmt and 'format' not in result:
            result['format'] = fmt
        return result
    if raw == 'string':
        # A dict/list bound to `string` is JSON to parse, not Python repr to print.
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)
    try:
        if raw == 'int':
            return int(value)
        if raw == 'float':
            return float(value)
        if raw == 'boolean':
            return bool(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _mismatch(name, raw, value) from exc
    return deepcopy(value)


def _mismatch(name: str, raw: Any, value: Any) -> SophiosError:
    return SophiosError.error(
        SophiosErrorCode.LITERAL_TYPE_MISMATCH,
        f'Input {name!r} is declared type {raw!r} but its literal {value!r} '
        'does not convert to it.')
