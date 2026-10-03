"""Complete graph facts that are determined by resolved process interfaces.

The seam between semantic inference and emission: it turns facts already
present in a `WorkflowGraph` into the workflow boundary that Link and Infer
compose against, without loading a process, discovering a file, or replaying
source. A boundary name it derives is a `DerivedName`, not text: `ir.names`
spells it, at Emit. It states no document -- requirements, `$namespaces`,
`$schemas`, a step's `run:` path and field order are spelled in `emit.surface`.
"""
import datetime
import difflib
import json
import re
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final, TypeVar

from ..lang.nodes import CwlRecord, InlineLiteral, UnresolvedName
from ..lang.diagnostics import Locator, SophiosError
from ..lang.spans import SourceSpan
from ..lang.error_codes import SophiosErrorCode
from .declarations import feeding_declaration, produced_declaration
from .names import Names, authored_path
from .types import (
    AuthoredName,
    BoundaryDeclaration,
    DerivedName,
    Direction,
    JobBinding,
    Port,
    PortDeclaration,
    PortId,
    PortName,
    PortType,
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
        outputs = _completed_outputs(step.outputs, child)
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
            _put(workflow_inputs, WorkflowPort(outer_name, declaration))
            _put(job_bindings, JobBinding(outer_name, coerce_job_value(
                boundary.name, declaration, child_jobs[boundary.name], span=step.span,
                locator=Locator(step.id.name, step.id.index, '/'.join(authored_path(boundary.name))))))
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
                case InlineLiteral(value=value, span=span):
                    name = DerivedName(step.id, port.id.port)
                    declaration = feeding_declaration(step, port)
                    _put(workflow_inputs, WorkflowPort(name, declaration))
                    locator = Locator(step.id.name, step.id.index, '/'.join(authored_path(port.id.port)))
                    _put(job_bindings, JobBinding(name, coerce_job_value(
                        port.id.port, declaration, value, span=span, locator=locator)))
                    _put_input_mapping(input_mapping, name, port.id)
                case UnresolvedName(name=text):
                    _relay(workflow_inputs, input_mapping, authored_inputs, AuthoredName(text), port)
                case CwlRecord(sources=sources):
                    for source in sources:
                        if isinstance(source, UnresolvedName):
                            _relay(workflow_inputs, input_mapping, authored_inputs, AuthoredName(source.name), port)
                case _:
                    pass
    return replace(graph, workflow_inputs=tuple(workflow_inputs),
                   job_bindings=tuple(job_bindings), input_mapping=tuple(input_mapping))


def _completed_outputs(outputs: tuple[Port, ...], child: WorkflowGraph) -> list[Port]:
    """`outputs` of a call step, each taking the declaration its completed child gives it.

    Resolve read the child's interface before the child was completed, so an
    output its author left untyped is untyped there until it takes the type the
    child gave it.
    """
    completed = {boundary.name: boundary.declaration for boundary in child.workflow_outputs}
    return [replace(port, type=declaration.type, declaration=declaration)
            if (declaration := completed.get(port.id.port)) is not None else port
            for port in outputs]


def _emitted_source(graph: WorkflowGraph, port_id: PortId) -> StepOutputRef | None:
    """The `step/port` reference an emitted document can resolve."""
    step = next((item for item in graph.steps if item.id == port_id.step), None)
    if step is None or step.run is None:
        return None
    return StepOutputRef(step.id, port_id.port)


def _produced_type(graph: WorkflowGraph, port_id: PortId) -> PortType:
    """The boundary type the output `port_id` produces, scatter layers included.

    `port_id` is a step of `graph`: Link has not yet redirected an output into
    a child, which is why `compile_source` completes a graph before it links it.
    """
    step = next(item for item in graph.steps if item.id == port_id.step)
    port = next(item for item in step.outputs if item.id == port_id)
    # pylint: disable-next=no-member
    return produced_declaration(step, port).type


def _untyped_output(graph: WorkflowGraph, output: WorkflowPort) -> SophiosError:
    """`wic036`: `output` declares no type and names no step output to take one from."""
    names = Names.of(graph)
    where = f'workflow output {names.port(output.name)!r} declares no type'
    if not output.has_output_source:
        return SophiosError.error(
            SophiosErrorCode.UNTYPED_OUTPUT,
            f'{where} and has no `outputSource:` to take one from; '
            'add `type:`, or an `outputSource: <step>/<output>`.')
    # What an `outputSource:` can name: not a name the compiler derives for a
    # call's lifted outputs.
    sources = [f'{step.id.name}/{names.port(port.id.port)}' for step in graph.steps
               for port in step.outputs if not isinstance(port.id.port, DerivedName)]
    close = difflib.get_close_matches(str(output.output_source), sources, n=1)
    check = f"Did you mean '{close[0]}'?" if close else 'Check the step and output names.'
    return SophiosError.error(
        SophiosErrorCode.UNTYPED_OUTPUT,
        f'{where}, and its `outputSource: {output.output_source}` names no output of a step in '
        f'this workflow, so there is no type to take. {check} If it names something that is '
        'not a step output, add `type:`.')


def _materialize_outputs(graph: WorkflowGraph) -> WorkflowGraph:
    # Record Link's resolved producer, not the authored `outputSource:` string:
    # emission renames every step, so the authored text points nowhere.
    resolved = dict(graph.output_mapping)
    outputs = []
    for output in graph.workflow_outputs:
        producer = resolved.get(output.name)
        if output.declaration.type.declared is None:
            if producer is None:
                raise _untyped_output(graph, output)
            output = replace(output, declaration=BoundaryDeclaration(replace(
                output.declaration, type=_produced_type(graph, producer))))
        emitted = _emitted_source(graph, producer) if producer is not None else None
        if output.has_output_source and emitted is not None:
            output = replace(output, output_source=emitted)
        outputs.append(output)
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


def _relay(workflow_inputs: list[WorkflowPort], input_mapping: list[tuple[PortName, tuple[PortId, ...]]],
           authored_inputs: set[PortName], name: AuthoredName, port: Port) -> None:
    """Relay the workflow input `name` to `port`, when the document declares it."""
    if name in authored_inputs:
        _put_input_mapping(input_mapping, name, port.id)
        _merge_boundary_documentation(workflow_inputs, name, port.declaration)


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


def coerce_job_value(name: PortName, declaration: PortDeclaration, value: Any, *,
                     span: SourceSpan | None = None, locator: Locator | None = None) -> Any:
    """`value` in the one plain-JSON form a job document holds for `declaration`.

    A projection: a value already in that form (a lifted child job value, an
    authored `File` object) passes through. Each array layer of the type wraps
    a scalar in a list and keeps a list. A scalar literal must already have the
    declared type: an int is also a float, and a scalar is also a `string`'s
    text, but nothing else is converted.

    `name` is the input the value is for. It is spelled only if the value does
    not convert and a message must name it: a derived name's text prints every
    step id it was exposed through, so spelling it for every value is the cost
    that made nesting exponential. `span` and `locator` say where the value was
    written, for the diagnostic.
    """
    value = _plain(value)
    if value is None:
        if declaration.type.optional:
            return None
        raise SophiosError.error(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                 f'Required input of type {declaration.type.declared} '
                                 'was not provided.', span=span, locator=locator)
    return _coerce_type(name, declaration.type.canonical, value,
                        declaration.format if declaration.has_format else None, span=span, locator=locator)


# pylint: disable-next=too-many-arguments
def _coerce_type(name: PortName, raw: Any, value: Any, fmt: Any, *,
                 span: SourceSpan | None, locator: Locator | None) -> Any:
    if isinstance(raw, list):
        non_null = [item for item in raw if item != 'null']
        arrays = [item for item in non_null if isinstance(item, dict)
                  and item.get('type') == 'array']
        raw = arrays[0] if arrays else (non_null[0] if len(non_null) == 1 else non_null)
    if isinstance(raw, dict) and raw.get('type') == 'array':
        values = value if isinstance(value, list) else [value]
        return [_coerce_type(name, raw.get('items'), item, fmt, span=span, locator=locator) for item in values]
    return _coerce_scalar(name, raw, value, fmt, span=span, locator=locator)


def _plain(value: Any) -> Any:
    """Return `value` with any nested `InlineLiteral` replaced by its data."""
    if isinstance(value, InlineLiteral):
        return _plain(value.value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


# pylint: disable-next=too-many-arguments
def _coerce_scalar(name: PortName, raw: Any, value: Any, fmt: Any, *,
                   span: SourceSpan | None, locator: Locator | None) -> Any:
    if raw in ('File', 'Directory'):
        if isinstance(value, str):
            value = {'class': raw, 'location': value}
        elif not isinstance(value, dict) or value.get('class') != raw:
            raise _mismatch(name, raw, value, span, locator)
        result = deepcopy(value)
        if raw == 'File' and fmt and 'format' not in result:
            result['format'] = fmt
        return result
    if raw == 'string':
        return _coerce_string(name, value, span=span, locator=locator)
    if raw in ('int', 'long'):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise _mismatch(name, raw, value, span, locator)
    if raw in ('float', 'double'):
        return _coerce_float(name, raw, value, span=span, locator=locator)
    if raw == 'boolean':
        if isinstance(value, bool):
            return value
        raise _mismatch(name, raw, value, span, locator)
    return deepcopy(value)


def _coerce_string(name: PortName, value: Any, *, span: SourceSpan | None, locator: Locator | None) -> str:
    """The text of a scalar literal, or of a mapping or list as JSON.

    The one conversion kept besides an int into a float: `!ii 20` cannot be
    spelled as the text "20" (the composer resolves it to a number). A boolean is
    JSON's `true` or `false`, as inside a mapping; a number is Python's spelling
    of it (`1.0e-5` is "1e-05"). YAML reads an unquoted 2024-01-15 as a date; its
    text is what the author wrote.
    """
    if isinstance(value, (dict, list, bool)):
        try:
            return json.dumps(value)
        except TypeError as exc:
            raise _mismatch(name, 'string', value, span, locator) from exc
    if isinstance(value, (str, int, float, datetime.date)):
        return str(value)
    raise _mismatch(name, 'string', value, span, locator)


def _coerce_float(name: PortName, raw: Any, value: Any, *,
                  span: SourceSpan | None, locator: Locator | None) -> float:
    """A float as it is, or an int the float holds exactly."""
    if isinstance(value, float):
        return float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            as_float = float(value)
        except OverflowError as exc:
            raise _mismatch(name, raw, value, span, locator) from exc
        if int(as_float) == value:
            return as_float
    raise _mismatch(name, raw, value, span, locator)


#: A number written in scientific notation that YAML 1.1 reads as text: PyYAML
#: resolves a float only with a decimal point and a signed exponent.
_UNSIGNED_EXPONENT: Final = re.compile(r'([-+]?\d+\.?\d*)[eE]([-+]?)(\d+)')

_KINDS: Final = {str: 'text', bool: 'a boolean', int: 'an int', float: 'a float',
                 list: 'a list', dict: 'a mapping', type(None): 'null'}

#: The Python type each scalar port type is, named when a value of another type is passed.
_PYTHON_TYPE: Final = {'int': int, 'long': int, 'float': float, 'double': float,
                       'boolean': bool, 'string': str}


def _mismatch(name: PortName, raw: Any, value: Any, span: SourceSpan | None,
              locator: Locator | None) -> SophiosError:
    return SophiosError.error(
        SophiosErrorCode.LITERAL_TYPE_MISMATCH,
        f'Input {str(name)!r} is declared type {raw!r} but its literal {value!r} {_what_is_wrong(raw, value)}',
        span=span, locator=locator)


def _what_is_wrong(raw: Any, value: Any) -> str:
    """What `value` is, for a message; for a float port, the cause and the fix."""
    if raw in ('float', 'double'):
        if isinstance(value, int) and not isinstance(value, bool):
            return 'is an int that a float cannot hold exactly.'
        scientific = _UNSIGNED_EXPONENT.fullmatch(value) if isinstance(value, str) else None
        if scientific:
            mantissa, sign, digits = scientific.groups()
            written = f'{mantissa if "." in mantissa else mantissa + ".0"}e{sign or "+"}{digits}'
            return ('is text: YAML reads a number as a float only with a decimal point and a signed '
                    f'exponent. Write {written}.')
    kind = type(value)
    if kind in _KINDS:
        return f'is {_KINDS[kind]}.'
    qualified = kind.__qualname__ if kind.__module__ == 'builtins' else f'{kind.__module__}.{kind.__qualname__}'
    wanted = _PYTHON_TYPE.get(raw)
    return f'is of type {qualified}' + (f', not a Python {wanted.__name__}.' if wanted else '.')
