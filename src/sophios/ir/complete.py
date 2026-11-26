"""Complete graph facts that are determined by resolved process interfaces.

It turns facts already present in a lowered `WorkflowGraph` into the workflow
boundary that Link and Infer compose against, without loading a process,
discovering a file, or replaying source. A boundary name it derives is a
`DerivedName`, not text: `ir.names` spells it, at Emit. It states no document
-- requirements, `$namespaces`, `$schemas`, a step's `run:` path and field
order are spelled in `cwl.emit`.
"""
import datetime
import difflib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Final

from ..lang.nodes import InlineLiteral, InputRecord, UnresolvedName, Value
from ..lang.diagnostics import Locator, SophiosError
from ..lang.spans import SourceSpan
from ..lang.error_codes import SophiosErrorCode
from ..lang.types import (Array, Atom, BoundaryDeclaration, Other, PortDeclaration, Type, Union, optional, spell,
                          typed)
from ..lang.values import FileKind, FileValue, Location, read_value, write_value
from .declarations import feeding_declaration, input_rank, produced_declaration
from .names import Names, authored_path
from .types import (
    AuthoredName,
    DerivedName,
    Direction,
    Port,
    PortId,
    PortName,
    StepNode,
    StepOutputRef,
    WorkflowGraph,
    WorkflowPort,
)


def complete(graph: WorkflowGraph) -> WorkflowGraph:
    """`graph` and every graph beneath it, each with the boundary its steps determine, for Link to compose.

    A value a step input holds is lifted to a workflow input and the job, an
    output left untyped takes the type of the step output it names, and every
    step output is promoted. Run once, on what Lower built: Infer keeps the
    boundary current for what it adds (`call_interfaces`, `promote_outputs`).
    """
    children = tuple(complete(child) for child in graph.children)
    current = _lift_values(call_interfaces(replace(graph, children=children)))
    return promote_outputs(_materialize_outputs(current))


def call_interfaces(graph: WorkflowGraph) -> WorkflowGraph:
    """`graph`, each call having the ports of its child's boundary as it is now: the inputs it had, then
    the others, and every output as the child declares it.

    Resolve read the child's interface before the child had its own boundary
    made: an output its author left untyped takes the type the child gave it.
    """
    def exposed(step: StepNode, direction: Direction, boundary: WorkflowPort) -> Port:
        return Port(PortId(step.id, direction, boundary.name), boundary.declaration.type, boundary.declaration,
                    step.span)

    steps = []
    for step in graph.steps:
        child = graph.child(step.id)
        if child is not None:
            held = {port.id.port for port in step.inputs}
            added = tuple(exposed(step, Direction.INPUT, boundary) for boundary in child.workflow_inputs
                          if boundary.name not in held)
            outputs = tuple(exposed(step, Direction.OUTPUT, boundary) for boundary in child.workflow_outputs)
            step = replace(step, inputs=step.inputs + added, outputs=outputs)
        steps.append(step)
    return replace(graph, steps=tuple(steps))


def _lift_values(graph: WorkflowGraph) -> WorkflowGraph:
    """Lift each value a step input holds to a workflow input of `graph` and its job: a job value a child
    holds for an input its call leaves unbound, which Emit renders from `input_mapping` alone as a relay back
    into the call's port, then each literal an input is bound to. A workflow input a step reads takes the
    `doc` and `label` of the port it feeds."""
    workflow_inputs = list(graph.workflow_inputs)
    job_bindings = list(graph.job_bindings)
    input_mapping = list(graph.input_mapping)

    def lift(step: StepNode, port: Port, value: Value, span: SourceSpan | None) -> None:
        name = DerivedName(step.id, port.id.port)
        declaration = feeding_declaration(step, port)
        workflow_inputs.append(WorkflowPort(name, declaration))
        job_bindings.append((name, coerce_job_value(
            port.id.port, declaration, value, scatter_rank=input_rank(step, port.id.port), span=span,
            locator=Locator(step.id.name, step.id.index, '/'.join(authored_path(port.id.port))))))
        input_mapping.append((name, (port.id,)))

    for step in graph.steps:
        child = graph.child(step.id)
        bound = {binding.sink.port for binding in step.bindings}
        jobs = dict(child.job_bindings) if child is not None else {}
        for port in step.inputs:
            if port.id.port in jobs and port.id.port not in bound:
                lift(step, port, jobs[port.id.port], step.span)
    for step in graph.steps:
        for binding in step.bindings:
            port = next(port for port in step.inputs if port.id == binding.sink)
            value = binding.value
            for source in value.sources if isinstance(value, InputRecord) else (value,):
                if isinstance(source, UnresolvedName):
                    _merge_boundary_documentation(workflow_inputs, AuthoredName(source.name), port.declaration)
            if isinstance(value, InlineLiteral):
                lift(step, port, value.value, value.span)
    return replace(graph, workflow_inputs=tuple(workflow_inputs), job_bindings=tuple(job_bindings),
                   input_mapping=tuple(input_mapping))


def _produced_type(graph: WorkflowGraph, port_id: PortId) -> Type:
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
    # Record the producer Lower resolved, not the authored `outputSource:` string:
    # emission renames every step, so the authored text points nowhere.
    resolved = dict(graph.output_mapping)
    outputs = []
    for output in graph.workflow_outputs:
        producer = resolved.get(output.name)
        if not typed(output.declaration.type):
            if producer is None:
                raise _untyped_output(graph, output)
            output = replace(output, declaration=BoundaryDeclaration(replace(
                output.declaration, type=_produced_type(graph, producer))))
        if producer is not None:
            output = replace(output, output_source=StepOutputRef(producer.step, producer.port))
        outputs.append(output)
    return replace(graph, workflow_outputs=tuple(outputs))


def promote_outputs(graph: WorkflowGraph) -> WorkflowGraph:
    """`graph`, every output of its steps a workflow output under its derived name, unless it is one already."""
    outputs = list(graph.workflow_outputs)
    output_mapping = list(graph.output_mapping)
    held = {port.name for port in outputs}
    for step in graph.steps:
        for port in step.outputs:
            name = DerivedName(step.id, port.id.port)
            if name not in held:
                outputs.append(WorkflowPort(name, produced_declaration(step, port),
                                            StepOutputRef(step.id, port.id.port), True))
                output_mapping.append((name, port.id))
    return replace(graph, workflow_outputs=tuple(outputs), output_mapping=tuple(output_mapping))


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


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def coerce_job_value(name: PortName, declaration: PortDeclaration, value: Value, *, scatter_rank: int = 0,
                     span: SourceSpan | None = None, locator: Locator | None = None) -> Value:
    """`value` in the one plain-JSON form a job document holds for `declaration`.

    A projection: a value already in that form (a lifted child job value, an
    authored `File` object) passes through. Each array layer of the type wraps
    a scalar in a list and keeps a list. A scalar literal must already have the
    declared type: an int is also a float, and a scalar is also a `string`'s
    text, but nothing else is converted. A scattered input (`scatter_rank`
    above zero, the layers `scatter:` adds) takes a list and never wraps a
    scalar: the scatter splits the value, so a scalar has nothing to split.

    `name` is the input the value is for. It is spelled only if the value does
    not convert and a message must name it: a derived name's text prints every
    step id it was exposed through, so spelling it for every value is the cost
    that made nesting exponential. `span` and `locator` say where the value was
    written, for the diagnostic.
    """
    value = read_value(_plain(value))
    if value is None:
        if optional(declaration.type):
            return None
        raise SophiosError.error(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                 f'Required input of type {spell(declaration.type)} '
                                 'was not provided.', span=span, locator=locator)
    if scatter_rank and not isinstance(value, list):
        raise SophiosError.error(
            SophiosErrorCode.LITERAL_TYPE_MISMATCH,
            f'Input {str(name)!r} is scattered, so its literal must be a list of values to scatter over; '
            f'{write_value(value)!r} is a scalar. Write `!ii [...]`, or drop the port from `scatter:`.',
            span=span, locator=locator)
    try:
        return _coerce_type(declaration.type, value, declaration.format if declaration.has_format else None)
    except _Mismatch as error:
        raise SophiosError.error(
            SophiosErrorCode.LITERAL_TYPE_MISMATCH,
            f'Input {str(name)!r} is declared type {error.raw!r} but its literal {write_value(error.value)!r} '
            f'{_what_is_wrong(error.raw, write_value(error.value))}', span=span, locator=locator) from error


@dataclass(eq=False)
class _Mismatch(Exception):
    """A literal that is not of the scalar type its port declares."""

    raw: str
    value: Value


def _coerce_type(type_: Type, value: Value, fmt: Value) -> Value:
    if isinstance(type_, Union):
        non_null = [member for member in type_.members if member is not Atom.NULL]
        arrays = [member for member in non_null if isinstance(member, Array)]
        scalars = [member for member in non_null if member not in arrays]
        if arrays and not isinstance(value, list):
            # A scalar that already is a scalar member's type takes that member;
            # any other scalar is wrapped into the array. `False` given to
            # `boolean | string[]` is the boolean, not `['false']`.
            type_ = _scalar_member_for(scalars, value) or arrays[0]
        else:
            type_ = arrays[0] if arrays else (non_null[0] if len(non_null) == 1 else Union(tuple(non_null)))
    if isinstance(type_, Array):
        values = value if isinstance(value, list) else [value]
        return [_coerce_type(Other() if type_.items is None else type_.items, item, fmt) for item in values]
    return _coerce_scalar(type_.value if isinstance(type_, Atom) else None, value, fmt)


def _scalar_member_for(members: Sequence[Type], value: Value) -> Type | None:
    """The scalar member of a union whose type `value` already is, or None.

    A bool is a `boolean`, an int an `int` or `long`, a float a `float` or
    `double`, text a `string`, and a `File` or `Directory` object its class.
    No conversion counts (an int into a float, a number into text), and an enum
    or record member is not a type name, so neither matches.
    """
    for member in members:
        name = member.value if isinstance(member, Atom) else member.name if isinstance(member, Other) else None
        if isinstance(name, str) and (_PYTHON_TYPE.get(name) is type(value)
                                      or isinstance(value, FileValue) and value.kind == name):
            return member if name else None
    return None


def _plain(value: Value) -> Value:
    """Return `value` with any nested `InlineLiteral` replaced by its data."""
    if isinstance(value, InlineLiteral):
        return _plain(value.value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _coerce_scalar(kind: str | None, value: Value, fmt: Value) -> Value:
    """`value` as the atom named `kind`; whatever else is not judged, and is taken as it is."""
    if kind in ('File', 'Directory'):
        if isinstance(value, str):
            value = FileValue(FileKind(kind), Location(value, True))
        elif not isinstance(value, FileValue) or value.kind != kind:
            raise _Mismatch(kind, value)
        result = deepcopy(value)
        if kind == 'File' and fmt and not result.has_format:
            result = replace(result, format=deepcopy(fmt), has_format=True)
        return result
    if kind == 'string':
        return _coerce_string(value)
    if kind in ('int', 'long'):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise _Mismatch(kind, value)
    if kind in ('float', 'double'):
        return _coerce_float(kind, value)
    if kind == 'boolean':
        if isinstance(value, bool):
            return value
        raise _Mismatch(kind, value)
    return deepcopy(value)


def _coerce_string(value: Value) -> str:
    """The text of a scalar literal, or of a mapping or list as JSON.

    The one conversion kept besides an int into a float: `!ii 20` cannot be
    spelled as the text "20" (the composer resolves it to a number). A boolean is
    JSON's `true` or `false`, as inside a mapping; a number is Python's spelling
    of it (`1.0e-5` is "1e-05"). YAML reads an unquoted 2024-01-15 as a date; its
    text is what the author wrote.
    """
    if isinstance(value, (dict, list, bool, FileValue)):
        try:
            return json.dumps(write_value(value))
        except TypeError as exc:
            raise _Mismatch('string', value) from exc
    if isinstance(value, (str, int, float, datetime.date)):
        return str(value)
    raise _Mismatch('string', value)


def _coerce_float(raw: str, value: Value) -> float:
    """A float as it is, or an int the float holds exactly."""
    if isinstance(value, float):
        return float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            as_float = float(value)
        except OverflowError as exc:
            raise _Mismatch(raw, value) from exc
        if int(as_float) == value:
            return as_float
    raise _Mismatch(raw, value)


#: A number written in scientific notation that YAML 1.1 reads as text: PyYAML
#: resolves a float only with a decimal point and a signed exponent.
_UNSIGNED_EXPONENT: Final = re.compile(r'([-+]?\d+\.?\d*)[eE]([-+]?)(\d+)')

_KINDS: Final = {str: 'text', bool: 'a boolean', int: 'an int', float: 'a float',
                 list: 'a list', dict: 'a mapping', type(None): 'null'}

#: The Python type each scalar port type is, named when a value of another type is passed.
_PYTHON_TYPE: Final[Mapping[object, type]] = {
    'int': int, 'long': int, 'float': float, 'double': float, 'boolean': bool, 'string': str}


def _what_is_wrong(raw: str, value: Value) -> str:
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
