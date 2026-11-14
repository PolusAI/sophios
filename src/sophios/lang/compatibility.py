"""The judgment over raw CWL port type declarations.

Answers one narrow question: may Sophios reject a user-authored reference
before handing the emitted document to CWL? It does not normalize
declarations into a larger CWL type system, and does not replace the
separate edge-inference candidate heuristic.
"""

from enum import Enum
from typing import Any

from .cwl import ATOMIC_TYPES


class TypeRelation(Enum):
    """What Sophios can prove about two endpoint type declarations."""

    OVERLAPS = 'overlaps'
    DISJOINT = 'disjoint'
    UNKNOWN = 'unknown'


def _expand_shorthand(declared: Any) -> Any:
    """Expand one suffix layer without changing the caller's declaration."""
    if not isinstance(declared, str):
        return declared
    if declared.endswith('[]') and len(declared) > 2:
        return {'type': 'array', 'items': declared[:-2]}
    if declared.endswith('?') and len(declared) > 1:
        return ['null', declared[:-1]]
    return declared


def _kind(declared: Any) -> str | None:
    """An atom's own name, `array` for an array, and None for what Sophios does not judge: `Any`, a record, an enum,
    a named schema. An array with no `items` is malformed, not an array: calling it one would answer `disjoint` for
    what is CWL's to reject."""
    if isinstance(declared, dict) and declared.get('type') == 'array' and 'items' in declared:
        return 'array'
    return declared if isinstance(declared, str) and declared in ATOMIC_TYPES else None


def _of_union(relations: list[TypeRelation]) -> TypeRelation:
    """What the members of a union prove together: overlapping if any does, disjoint only if every one is."""
    if TypeRelation.OVERLAPS in relations:
        return TypeRelation.OVERLAPS
    return TypeRelation.DISJOINT if relations and set(relations) == {TypeRelation.DISJOINT} else TypeRelation.UNKNOWN


def reference_relation(source: Any, sink: Any, _in_source: frozenset[int] = frozenset(),
                       _in_sink: frozenset[int] = frozenset()) -> TypeRelation:
    """Return the relation Sophios can prove between raw endpoint declarations.

    Non-mutating and conservative: a caller may reject a reference only for
    :attr:`TypeRelation.DISJOINT`; ``UNKNOWN`` leaves the final decision to
    validation of the emitted CWL document. A declaration that holds itself
    is unknown where it comes round again.
    """
    source, sink = _expand_shorthand(source), _expand_shorthand(sink)
    if id(source) in _in_source or id(sink) in _in_sink:
        return TypeRelation.UNKNOWN
    if isinstance(source, list):
        return _of_union([reference_relation(member, sink, _in_source | {id(source)}, _in_sink) for member in source])
    if isinstance(sink, list):
        return _of_union([reference_relation(source, member, _in_source, _in_sink | {id(sink)}) for member in sink])
    kinds = _kind(source), _kind(sink)
    if kinds == ('array', 'array'):
        return reference_relation(source['items'], sink['items'], _in_source | {id(source)}, _in_sink | {id(sink)})
    if None in kinds:
        return TypeRelation.UNKNOWN
    return TypeRelation.OVERLAPS if kinds[0] == kinds[1] else TypeRelation.DISJOINT
