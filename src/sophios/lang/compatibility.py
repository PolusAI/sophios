"""Version-owned judgments over raw CWL port type declarations.

This module answers one deliberately narrow question: may Sophios reject a
user-authored reference before handing the emitted document to CWL?  It does
not normalize declarations into the typed IR reserved for Spec 3, and it does
not replace the legacy edge-inference heuristic.
"""

from collections.abc import Callable
from enum import Enum
from typing import Any, Final

from .versions import KNOWN_VERSIONS


class TypeRelation(Enum):
    """What Sophios can prove about two endpoint type declarations."""

    OVERLAPS = 'overlaps'
    DISJOINT = 'disjoint'
    UNKNOWN = 'unknown'


_ATOMS: Final = frozenset({
    'null', 'boolean', 'int', 'long', 'float', 'double', 'string', 'File', 'Directory',
})


def _expand_shorthand(declared: Any) -> Any:
    """Expand one suffix layer without changing the caller's declaration."""
    if not isinstance(declared, str):
        return declared
    if declared.endswith('[]') and len(declared) > 2:
        return {'type': 'array', 'items': declared[:-2]}
    if declared.endswith('?') and len(declared) > 1:
        return ['null', declared[:-1]]
    return declared


# A flat decision table is clearer here than merging semantically distinct exits.
def _v0_0_1(source: Any, sink: Any,  # pylint: disable=too-many-return-statements
            source_active: frozenset[int] = frozenset(),
            sink_active: frozenset[int] = frozenset()) -> TypeRelation:
    """Judge one pair according to the raw-type rules of language 0.0.1."""
    source = _expand_shorthand(source)
    sink = _expand_shorthand(sink)

    if (isinstance(source, dict) and id(source) in source_active) or \
            (isinstance(sink, dict) and id(sink) in sink_active):
        return TypeRelation.UNKNOWN

    source_members = source if isinstance(source, list) else [source]
    sink_members = sink if isinstance(sink, list) else [sink]
    if isinstance(source, list) or isinstance(sink, list):
        if not source_members or not sink_members:
            return TypeRelation.UNKNOWN
        if (isinstance(source, list) and id(source) in source_active) or \
                (isinstance(sink, list) and id(sink) in sink_active):
            return TypeRelation.UNKNOWN
        nested_source = source_active | {id(source)} if isinstance(source, list) else source_active
        nested_sink = sink_active | {id(sink)} if isinstance(sink, list) else sink_active
        relations = [
            _v0_0_1(source_member, sink_member, nested_source, nested_sink)
            for source_member in source_members
            for sink_member in sink_members
        ]
        if TypeRelation.OVERLAPS in relations:
            return TypeRelation.OVERLAPS
        if all(relation is TypeRelation.DISJOINT for relation in relations):
            return TypeRelation.DISJOINT
        return TypeRelation.UNKNOWN

    source_is_array = isinstance(source, dict) and source.get('type') == 'array'
    sink_is_array = isinstance(sink, dict) and sink.get('type') == 'array'
    if source_is_array or sink_is_array:
        if source_is_array and sink_is_array:
            if 'items' not in source or 'items' not in sink:
                return TypeRelation.UNKNOWN
            return _v0_0_1(source['items'], sink['items'],
                           source_active | {id(source)}, sink_active | {id(sink)})
        scalar = sink if source_is_array else source
        return TypeRelation.DISJOINT \
            if isinstance(scalar, str) and scalar in _ATOMS else TypeRelation.UNKNOWN

    if source == 'Any' or sink == 'Any':
        return TypeRelation.UNKNOWN
    if isinstance(source, str) and isinstance(sink, str):
        if source not in _ATOMS or sink not in _ATOMS:
            return TypeRelation.UNKNOWN
        return TypeRelation.OVERLAPS if source == sink else TypeRelation.DISJOINT
    return TypeRelation.UNKNOWN


_JUDGES: Final[dict[str, Callable[[Any, Any], TypeRelation]]] = {
    '0.0.1': _v0_0_1,
}

if tuple(_JUDGES) != KNOWN_VERSIONS:
    raise RuntimeError('Every Sophios language version must own a reference-type judgment')


def reference_relation(source: Any, sink: Any, *, lang_version: str) -> TypeRelation:
    """Return the relation Sophios can prove between raw endpoint declarations.

    The function is non-mutating and conservative.  A caller may reject a
    reference only for :attr:`TypeRelation.DISJOINT`; ``UNKNOWN`` deliberately
    leaves the final decision to validation of the emitted CWL document.
    """
    try:
        judge = _JUDGES[lang_version]
    except KeyError as exc:
        raise ValueError(f'No reference judgment for lang_version {lang_version!r}') from exc
    return judge(source, sink)
