"""Independent test models: reference-type judgments, lowering declarations to a small set of structural variants and
comparing the Cartesian product, so generated inputs never ask the system under test which cases it should generate;
and inference's default choice of source for an unbound input."""

from enum import Enum
from typing import Any, Final

from sophios.ir.declarations import input_rank, layered, output_rank
from sophios.ir.types import DerivedName, Port, PortDeclaration, PortId, PortName, StepNode


class ReferenceExpectation(Enum):
    """Expected relation between two generated endpoint declarations."""

    OVERLAPS = 'overlaps'
    DISJOINT = 'disjoint'
    UNKNOWN = 'unknown'


_ATOMS: Final = frozenset({
    'null', 'boolean', 'int', 'long', 'float', 'double', 'string', 'File', 'Directory',
})
_UNKNOWN: Final = object()


def _variants(declared: Any,  # pylint: disable=too-many-return-statements
              active: frozenset[int] = frozenset()) -> list[Any]:
    """Lower a raw declaration to atomic/array alternatives for the oracle."""
    if isinstance(declared, str):
        if declared.endswith('[]') and len(declared) > 2:
            return [('array', item) for item in _variants(declared[:-2], active)]
        if declared.endswith('?') and len(declared) > 1:
            return [('atom', 'null'), *_variants(declared[:-1], active)]
        if declared == 'Any' or declared not in _ATOMS:
            return [_UNKNOWN]
        return [('atom', declared)]

    if isinstance(declared, list):
        if not declared or id(declared) in active:
            return [_UNKNOWN]
        nested = active | {id(declared)}
        return [variant for member in declared for variant in _variants(member, nested)]

    if isinstance(declared, dict):
        if id(declared) in active or declared.get('type') != 'array' or 'items' not in declared:
            return [_UNKNOWN]
        return [('array', item) for item in _variants(declared['items'], active | {id(declared)})]

    return [_UNKNOWN]


def _variant_relation(source: Any, sink: Any) -> ReferenceExpectation:
    if source is _UNKNOWN or sink is _UNKNOWN:
        return ReferenceExpectation.UNKNOWN
    source_kind, source_value = source
    sink_kind, sink_value = sink
    if source_kind != sink_kind:
        return ReferenceExpectation.DISJOINT
    if source_kind == 'atom':
        return ReferenceExpectation.OVERLAPS \
            if source_value == sink_value else ReferenceExpectation.DISJOINT
    return _variant_relation(source_value, sink_value)


def reference_expectation(source: Any, sink: Any) -> ReferenceExpectation:
    """Return the independently modelled relation for two raw declarations."""
    relations = [
        _variant_relation(source_variant, sink_variant)
        for source_variant in _variants(source)
        for sink_variant in _variants(sink)
    ]
    if ReferenceExpectation.OVERLAPS in relations:
        return ReferenceExpectation.OVERLAPS
    if relations and all(relation is ReferenceExpectation.DISJOINT for relation in relations):
        return ReferenceExpectation.DISJOINT
    return ReferenceExpectation.UNKNOWN


def may_reference(source: Any, sink: Any) -> bool:
    """Whether a generator may safely put this reference in a compilable document."""
    return reference_expectation(source, sink) is not ReferenceExpectation.DISJOINT


def inference_choice(steps: tuple[StepNode, ...], position: int, sink: Port) -> PortId | None:
    """Independent, deliberately small model of default candidate selection: the most recent earlier step with an
    output whose type and format fit, the first such output declared, among those exposed through the same steps."""
    sink_type = layered(sink.type, input_rank(steps[position], sink.id.port)).canonical
    sink_formats = _formats(sink.declaration)
    for producer in reversed(steps[:position]):
        matches = [output for output in producer.outputs
                   if _types_match(sink_type, _source_type(producer, output))
                   and _formats_match(sink_formats, _formats(output.declaration), _source_type(producer, output))]
        if matches:
            through = _exposed_through(matches[-1].id.port)
            return next(output.id for output in matches if _exposed_through(output.id.port) == through)
    return None


def _exposed_through(name: PortName) -> list[object]:
    """The steps a port name was exposed through, outermost first."""
    steps: list[object] = []
    while isinstance(name, DerivedName):
        steps.append(name.step)
        name = name.port
    return steps


def _types_match(sink: Any, source: Any) -> bool:
    if sink == source:
        return True
    sink_members = sink if isinstance(sink, list) else [sink]
    source_members = source if isinstance(source, list) else [source]
    return any(member in sink_members for member in source_members)


def _formats(declaration: PortDeclaration | None) -> tuple[Any, ...]:
    if declaration is None or not declaration.has_format:
        return ()
    return tuple(declaration.format) if isinstance(declaration.format, list) else (declaration.format,)


def _formats_match(sink: tuple[Any, ...], source: tuple[Any, ...], source_type: Any) -> bool:
    if not sink:
        return True
    if source:
        return any(item in sink for item in source)
    return not _permits_format(source_type)


def _permits_format(value: Any) -> bool:
    if value == 'File':
        return True
    if isinstance(value, list):
        return any(_permits_format(item) for item in value)
    if isinstance(value, dict) and value.get('type') == 'array':
        return _permits_format(value.get('items'))
    return False


def _source_type(step: StepNode, port: Port) -> Any:
    """CWL v1.2: `nested_crossproduct` nests one array per scattered input; every other method yields one flat array.
    Rank is `output_rank`."""
    return layered(port.type, output_rank(step)).canonical
