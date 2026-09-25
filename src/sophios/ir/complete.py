"""Complete graph facts that are determined by resolved process interfaces.

This is the deliberately small seam between semantic inference and emission.
It does not load a process, discover a file, or replay source.  It turns facts
already present in a ``WorkflowGraph`` into the workflow boundary that Link and
Infer compose against.

It states no document. Requirements, `$namespaces`, `$schemas`, a step's `run:`
path and the order fields appear in are how the facts are *spelled*, and they
live in `emit.surface`. Complete ran three times because it did both jobs, and
a spelling recomputed from its own last output is how a subworkflow `run:`
target came to grow a prefix per pass.
"""
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..lang.nodes import EdgeRef, InlineLiteral, RawCwlRef, UnresolvedName
from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode
from .declarations import boundary_declaration, port_declaration
from .types import (
    BoundaryDeclaration,
    Direction,
    Edge,
    Expression,
    JobBinding,
    namespaced,
    Port,
    PortDeclaration,
    PortId,
    Source,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)


def complete(graph: WorkflowGraph, *, partial_failure: bool = False) -> WorkflowGraph:
    """Return a graph carrying every fact its resolved interfaces determine.

    Facts only.  How those facts are spelled as a document is `emit.surface`,
    which runs once; this runs whenever a phase needs the facts current, so it
    is idempotent -- calling it before Link makes recursively derived workflow
    interfaces visible to composition, and calling it after Infer materializes
    newly inferred sources and boundary inputs.
    """
    children = tuple(complete(child, partial_failure=partial_failure)
                     for child in graph.children)
    current = replace(graph, children=children)
    current = _synchronize_children(current)
    current = _materialize_bindings(current, partial_failure)
    current = _materialize_edges(current)
    return _materialize_outputs(current)


def _synchronize_children(graph: WorkflowGraph) -> WorkflowGraph:
    child_by_name = {child.namespace.parts[-1]: child for child in graph.children
                     if child.namespace.parts}
    workflow_inputs = list(graph.workflow_inputs)
    job_bindings = list(graph.job_bindings)
    input_mapping = list(graph.input_mapping)
    steps: list[StepNode] = []

    for step in graph.steps:
        emission = step.emission
        child = child_by_name.get(emission.id) if emission is not None else None
        if emission is None or child is None:
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

        authored = {binding.sink.port for binding in step.bindings}
        emitted_inputs = dict(emission.inputs)
        child_jobs = {binding.name: binding.value for binding in child.job_bindings}
        for boundary in child.workflow_inputs:
            if boundary.name in authored or boundary.name not in child_jobs:
                continue
            outer_name = namespaced(emission.id, boundary.name)
            _put_port(workflow_inputs, WorkflowPort(outer_name, boundary.declaration))
            _put_job(job_bindings, JobBinding(outer_name, child_jobs[boundary.name]))
            sink = next(port.id for port in inputs if port.id.port == boundary.name)
            _put_input_mapping(input_mapping, outer_name, sink)
            emitted_inputs[boundary.name] = Source(outer_name, shorthand=True)

        steps.append(replace(
            step,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            emission=replace(emission, inputs=tuple(emitted_inputs.items()),
                             outputs=tuple(port.name for port in child.workflow_outputs),
                             run=replace(emission.run, child=child)),
        ))
    return replace(graph, steps=tuple(steps), workflow_inputs=tuple(workflow_inputs),
                   job_bindings=tuple(job_bindings), input_mapping=tuple(input_mapping))


def _materialize_bindings(graph: WorkflowGraph, partial_failure: bool) -> WorkflowGraph:
    workflow_inputs = list(graph.workflow_inputs)
    job_bindings = list(graph.job_bindings)
    input_mapping = list(graph.input_mapping)
    steps: list[StepNode] = []
    authored_inputs = {port.name for port in graph.workflow_inputs}

    for step in graph.steps:
        emission = step.emission
        if emission is None:
            steps.append(step)
            continue
        emitted = dict(emission.inputs)
        for binding in step.bindings:
            port = next(port for port in step.inputs if port.id == binding.sink)
            match binding.value:
                case InlineLiteral(value=value):
                    name = namespaced(emission.id, port.id.port)
                    declaration = _input_declaration(port, emission.scatter)
                    _put_port(workflow_inputs, WorkflowPort(name, declaration))
                    _put_job(job_bindings, JobBinding(
                        name, coerce_job_value(name, declaration, value)))
                    _put_input_mapping(input_mapping, name, port.id)
                    emitted[port.id.port] = Source(name)
                case RawCwlRef(expression=expression):
                    emitted[port.id.port] = Expression(expression)
                case UnresolvedName(name=name):
                    emitted[port.id.port] = Source(name, shorthand=True)
                    if name in authored_inputs:
                        _put_input_mapping(input_mapping, name, port.id)
                        _merge_boundary_documentation(workflow_inputs, name, port.declaration)
                case EdgeRef():
                    # Link supplies the concrete source.  Keeping this arm
                    # empty prevents the source spelling from being guessed.
                    pass
        when = emission.when
        if partial_failure:
            required = [port.id.port for port in step.inputs if _required(port.declaration)]
            if required:
                when = '$(' + ' && '.join(f'inputs["{name}"] != null'
                                          for name in required) + ')'
        steps.append(replace(step, emission=replace(emission,
                                                    inputs=tuple(emitted.items()), when=when)))
    return replace(graph, steps=tuple(steps), workflow_inputs=tuple(workflow_inputs),
                   job_bindings=tuple(job_bindings), input_mapping=tuple(input_mapping))


def _materialize_edges(graph: WorkflowGraph) -> WorkflowGraph:
    steps = list(graph.steps)
    positions = {step.id: index for index, step in enumerate(steps)}

    def bind(edge: Edge, *, explicit: bool) -> None:
        target_step, target_port = _direct_sink(graph, edge.sink)
        position = positions.get(target_step)
        if position is None:
            return
        step = steps[position]
        if step.emission is None:
            return
        inputs = dict(step.emission.inputs)
        source = _direct_source(graph, edge.source)
        inputs[target_port] = Source(source, shorthand=not explicit)
        steps[position] = replace(step, emission=replace(step.emission,
                                                         inputs=tuple(inputs.items())))

    for step in graph.steps:
        for binding in step.bindings:
            if isinstance(binding.resolution, Edge):
                bind(binding.resolution, explicit=True)
    for edge in graph.composition_edges:
        bind(edge, explicit=True)
    for edge in graph.inferred_edges:
        bind(edge, explicit=False)
    for name, sinks in graph.input_mapping:
        for sink in sinks:
            target_step, target_port = _direct_sink(graph, sink)
            position = positions.get(target_step)
            if position is None:
                continue
            step = steps[position]
            if step.emission is None:
                continue
            inputs = dict(step.emission.inputs)
            inputs.setdefault(target_port, Source(name))
            steps[position] = replace(
                step, emission=replace(step.emission, inputs=tuple(inputs.items())))
    ordered_steps: list[StepNode] = []
    for step in steps:
        if step.emission is None:
            ordered_steps.append(step)
            continue
        values = dict(step.emission.inputs)
        names = [binding.sink.port for binding in step.bindings]
        names.extend(port.id.port for port in step.inputs if port.id.port not in names)
        names.extend(name for name in values if name not in names)
        ordered_steps.append(replace(
            step, emission=replace(step.emission,
                                   inputs=tuple((name, values[name]) for name in names
                                                if name in values))))
    return replace(graph, steps=tuple(ordered_steps))


def _emitted_source(graph: WorkflowGraph, port_id: PortId) -> str | None:
    """The `step/port` spelling an emitted document can resolve."""
    step = next((item for item in graph.steps if item.id == port_id.step), None)
    if step is None or step.emission is None:
        return None
    return f'{step.emission.id}/{port_id.port}'


def _materialize_outputs(graph: WorkflowGraph) -> WorkflowGraph:
    # An authored `outputSource:` names the step as the document wrote it, and
    # emission renames every step to `{workflow}__step__{i}__{name}`. Carrying
    # the authored string through leaves the emitted document pointing at a
    # step that does not exist there. Link already resolved the same string to
    # a port, so rewrite from that rather than from the text.
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
        if step.emission is None:
            continue
        for port in step.outputs:
            name = namespaced(step.emission.id, port.id.port)
            if name in authored:
                continue
            declaration = _output_declaration(port, step.emission.scatter)
            outputs.append(WorkflowPort(
                name, declaration, f'{step.emission.id}/{port.id.port}', True))
            output_mapping.append((name, port.id))
    return replace(graph, workflow_outputs=tuple(outputs), output_mapping=tuple(output_mapping))


def _direct_source(graph: WorkflowGraph, source: PortId) -> str:
    if source.step.namespace == graph.namespace:
        step = next(step for step in graph.steps if step.id == source.step)
        assert step.emission is not None
        return f'{step.emission.id}/{source.port}'
    child = next(child for child in graph.children
                 if source.step.namespace.parts[:len(child.namespace.parts)] == child.namespace.parts)
    wrapper = next(step for step in graph.steps
                   if step.emission is not None and step.emission.run.child == child)
    boundary = next(name for name, port in child.output_mapping if port == source)
    assert wrapper.emission is not None
    return f'{wrapper.emission.id}/{boundary}'


def _direct_sink(graph: WorkflowGraph, sink: PortId) -> tuple[Any, str]:
    if sink.step.namespace == graph.namespace:
        return sink.step, sink.port
    child = next(child for child in graph.children
                 if sink.step.namespace.parts[:len(child.namespace.parts)] == child.namespace.parts)
    wrapper = next(step for step in graph.steps
                   if step.emission is not None and step.emission.run.child == child)
    boundary = next(name for name, sinks in child.input_mapping if sink in sinks)
    return wrapper.id, boundary


def _input_declaration(port: Port, scatter: Any) -> BoundaryDeclaration:
    declaration = boundary_declaration(port.declaration or port_declaration(port.type.declared))
    keys = [scatter] if isinstance(scatter, str) else (
        list(scatter) if isinstance(scatter, list) else [])
    layers = keys.count(port.id.port)
    raw = _canonical_type(declaration.type.declared)
    for _ in range(layers):
        raw = {'type': 'array', 'items': raw}
    return BoundaryDeclaration(replace(declaration, type=port_declaration({'type': raw}).type))


def _output_declaration(port: Port, scatter: Any) -> BoundaryDeclaration:
    declaration = boundary_declaration(port.declaration or port_declaration(port.type.declared))
    raw = _canonical_type(declaration.type.declared)
    if scatter:
        raw = {'type': 'array', 'items': raw}
    order = tuple((*declaration.field_order, 'outputSource'))
    return BoundaryDeclaration(
        replace(declaration, type=port_declaration({'type': raw}).type, field_order=order))


def _canonical_type(value: Any) -> Any:
    if isinstance(value, str) and value.endswith('?'):
        return ['null', _canonical_type(value[:-1])]
    if isinstance(value, str) and value.endswith('[]'):
        return {'type': 'array', 'items': _canonical_type(value[:-2])}
    if isinstance(value, dict) and value.get('type') == 'array':
        return {**value, 'items': _canonical_type(value.get('items'))}
    return deepcopy(value)


def _put_port(ports: list[WorkflowPort], port: WorkflowPort) -> None:
    if port.name not in {item.name for item in ports}:
        ports.append(port)


def _put_job(bindings: list[JobBinding], binding: JobBinding) -> None:
    if binding.name not in {item.name for item in bindings}:
        bindings.append(binding)


def _put_input_mapping(mappings: list[tuple[str, tuple[PortId, ...]]],
                       name: str, sink: PortId) -> None:
    for index, (existing, sinks) in enumerate(mappings):
        if existing == name:
            if sink not in sinks:
                mappings[index] = (existing, (*sinks, sink))
            return
    mappings.append((name, (sink,)))


def _merge_boundary_documentation(ports: list[WorkflowPort], name: str,
                                  source: PortDeclaration | None) -> None:
    if source is None:
        return
    additions = dict(source.passthrough)
    for index, port in enumerate(ports):
        if port.name != name:
            continue
        declaration = port.declaration
        values = dict(declaration.passthrough)
        order = list(declaration.field_order)
        for key in ('doc', 'label'):
            addition = _as_text(additions.get(key, ''))
            if not addition:
                continue
            existing = _as_text(values.get(key, ''))
            if existing == addition or (isinstance(existing, str)
                                        and existing.endswith(f'\n{addition}')):
                continue
            values[key] = f'{existing}\n{addition}' if existing else addition
            if key not in order:
                order.append(key)
        ports[index] = replace(port, declaration=replace(
            declaration, passthrough=tuple(values.items()), field_order=tuple(order)))
        return


def _as_text(value: Any) -> Any:
    return '\n'.join(value) if isinstance(value, list) else value


def _required(declaration: PortDeclaration | None) -> bool:
    return declaration is None or not (
        (declaration.has_default and declaration.default is not None)
        or declaration.type.optional)


def coerce_job_value(name: str, declaration: PortDeclaration, value: Any) -> Any:
    """Coerce one literal exactly once at the graph/job boundary.

    `OpaqueCwl` admits an `InputValue`, so a literal's body legitimately holds
    parsed nodes wherever the document nested a construct inside it. A job
    value may not: it is written as JSON. Unwrapping here, once, rather than
    in whichever branch happens to notice, is what makes that true of every
    declared type rather than of `string` alone.
    """
    value = _plain(value)
    raw = declaration.type.declared
    if value is None:
        if declaration.type.optional:
            return None
        raise SophiosError.error(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                 f'Required input of type {raw} was not provided.')
    return _coerce_type(name, raw, value, declaration.format if declaration.has_format else None)


def _coerce_type(name: str, raw: Any, value: Any, fmt: Any) -> Any:
    if isinstance(raw, list):
        non_null = [item for item in raw if item != 'null']
        arrays = [item for item in non_null if isinstance(item, dict)
                  and item.get('type') == 'array']
        raw = arrays[0] if arrays else (non_null[0] if len(non_null) == 1 else non_null)
    if isinstance(raw, str) and raw.endswith('?'):
        raw = raw[:-1]
    if isinstance(raw, dict) and raw.get('type') == 'array':
        values = value if isinstance(value, list) else [value]
        return [_coerce_scalar(name, raw.get('items'), item, fmt) for item in values]
    return _coerce_scalar(name, raw, value, fmt)


def _plain(value: Any) -> Any:
    """`value` with any nested literal replaced by the data it stands for.

    A `!ii` body is parsed, so a mapping literal holds `InlineLiteral` nodes
    wherever the document nested one. They are the same data; only the node
    is in the way of serializing it.
    """
    if isinstance(value, InlineLiteral):
        return _plain(value.value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _coerce_scalar(name: str, raw: Any, value: Any, fmt: Any) -> Any:
    if raw == 'File':
        result = {'class': 'File', 'location': value}
        if fmt:
            result['format'] = fmt
        return result
    if raw == 'Directory':
        return {'class': 'Directory', 'location': value}
    if raw == 'string':
        # A mapping or a sequence bound to a `string` port is a document the
        # tool will parse, not a value to print: biobb's `config` is the
        # common case, and it reads the string as JSON. `str` gives Python's
        # repr, whose single quotes are not JSON, so the tool falls through to
        # treating the text as a path and fails on a file named after a dict.
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
    except (TypeError, ValueError) as exc:
        raise SophiosError.error(
            SophiosErrorCode.LITERAL_TYPE_MISMATCH,
            f'Input {name!r} is declared type {raw!r} but its literal {value!r} '
            'does not convert to it.') from exc
    return deepcopy(value)
