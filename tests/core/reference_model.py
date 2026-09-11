"""Independent test model for Sophios reference-type judgments.

The production implementation recursively compares raw declarations.  This
oracle instead lowers them to a small test-only set of structural variants and
compares the Cartesian product.  Keeping both the representation and the
algorithm separate prevents generated inputs from asking the system under test
which cases it should generate.
"""

from enum import Enum
from typing import Any, Final


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
