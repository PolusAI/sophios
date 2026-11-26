"""A port's type is a neutral `Type`: the door reads a spelling into one, `spell` writes it as plain values, the text a
diagnostic prints, and the CWL target writes it in CWL's short forms."""
from typing import Any

import pytest
from hypothesis import given

from sophios.cwl.spelling import written_type
from sophios.lang.cwl import read_type, read_typed
from sophios.lang.types import (Array, Atom, Enum, Merge, Other, Pick, Record, TypeRelation, Unread, Union, atoms,
                                delivered, layered, lists, relation, spell)

from .strategies import cwl_types

_ARRAY = {'type': 'array', 'items': 'File'}

#: A written type and the canonical spelling it means: shorthand expanded at every depth, `null` first, no member
#: twice, a stream a `File`, and what is not judged left as written, key order included.
_CANONICAL: tuple[tuple[Any, Any], ...] = (
    ('string', 'string'), ('Any', 'Any'), ('string?', ['null', 'string']),
    ('string[]', {'type': 'array', 'items': 'string'}),
    ('string?[]', {'type': 'array', 'items': ['null', 'string']}),
    ('string[]?', ['null', {'type': 'array', 'items': 'string'}]),
    ('File??', ['null', 'File']), (['File?', 'string'], ['null', 'File', 'string']),
    (['string', 'null', 'string?'], ['null', 'string']), ([['null', 'int'], 'File'], ['null', 'int', 'File']),
    (['string'], ['string']), ([], []), ('stdin', 'File'), ('stdout', 'File'), (['stderr', 'File'], ['File']),
    ({'type': 'array', 'items': 'stdout'}, {'type': 'array', 'items': 'File'}),
    ({'type': 'array'}, {'type': 'array'}), ({'type': 'array', 'items': None}, {'type': 'array', 'items': None}),
    ({'items': 'int[]', 'type': 'array', 'doc': 'd'},
     {'items': {'type': 'array', 'items': 'int'}, 'type': 'array', 'doc': 'd'}),
    ({'type': 'array', 'items': ['int?', 'string[]']},
     {'type': 'array', 'items': ['null', 'int', {'type': 'array', 'items': 'string'}]}),
    ({'type': 'record', 'fields': []}, {'type': 'record', 'fields': []}),
    ({'type': 'record', 'name': 'r',
      'fields': [{'name': 'a', 'type': 'int?', 'doc': 'd'}, {'name': 'b', 'type': 'File'}]},
     {'type': 'record', 'name': 'r', 'fields': [{'name': 'a', 'type': ['null', 'int'], 'doc': 'd'},
                                                {'name': 'b', 'type': 'File'}]}),
    ({'type': 'record', 'fields': {'a': 'string[]'}},
     {'type': 'record', 'fields': [{'name': 'a', 'type': {'type': 'array', 'items': 'string'}}]}),
    ({'type': 'record', 'fields': 3}, {'type': 'record', 'fields': 3}),
    ({'type': 'enum', 'symbols': ['a']}, {'type': 'enum', 'symbols': ['a']}),
    ({'type': 'enum', 'symbols': ['a', 'b'], 'name': 'e'}, {'type': 'enum', 'symbols': ['a', 'b'], 'name': 'e'}),
    ({'type': 'enum', 'symbols': 'a'}, {'type': 'enum', 'symbols': 'a'}),
    # a union's member keeps what it wrote beyond its type when the union puts `null` first
    (['string', {'type': 'array', 'items': 'string', 'inputBinding': {'prefix': '-a'}}, 'null'],
     ['null', 'string', {'type': 'array', 'items': 'string', 'inputBinding': {'prefix': '-a'}}]),
    ('example.org#Thing', 'example.org#Thing'), ('?', '?'), ('[]', '[]'), (None, None), (3, 3),
)


@pytest.mark.fast
@pytest.mark.parametrize(('written', 'canonical'), _CANONICAL)
def test_a_written_type_means_its_canonical_spelling(written: Any, canonical: Any) -> None:
    """`spell` of what the door reads is the canonical spelling, and it is read back as itself."""
    type_, unread = read_typed(written)
    assert spell(type_, unread) == canonical
    assert read_typed(canonical) == (type_, unread)
    assert unread or spell(type_) == canonical


@pytest.mark.fast
@given(cwl_types('string', 'File', 'int', 'null', 'Any', 'stdin', 'example.org#Thing', None, {'type': 'array'}))
def test_a_spelling_holds_plain_values_only(written: Any) -> None:
    """An `Atom` member would print as `<Atom.INT: 'int'>` in a diagnostic and cannot be dumped to YAML."""
    def plain(value: Any) -> bool:
        match value:
            case dict():
                return all(plain(item) for item in value.values())
            case list():
                return all(map(plain, value))
        return not isinstance(value, Atom)

    assert plain(spell(read_type(written))) and spell(read_type(spell(read_type(written)))) == spell(read_type(written))
    assert read_typed(written_type(*read_typed(written))) == read_typed(written)


@pytest.mark.fast
def test_layers_wrap_the_type() -> None:
    """A promoted port is its type inside as many array layers as it is scattered over, none or several."""
    assert layered(read_type('File?'), 0) == read_type('File?')
    assert spell(layered(read_type('File?'), 2)) == {'type': 'array',
                                                     'items': {'type': 'array', 'items': ['null', 'File']}}
    assert layered(read_type('File'), 1) == Array(Atom.FILE)
    assert spell(Atom.INT).__class__ is str


_RELATIONS = (
    ('string', 'string', TypeRelation.OVERLAPS), ('string', 'File', TypeRelation.DISJOINT),
    ('string', 'string?', TypeRelation.OVERLAPS), (['int', 'boolean'], 'string', TypeRelation.DISJOINT),
    (['null', 'Any'], 'File', TypeRelation.UNKNOWN), ('File[]', _ARRAY, TypeRelation.OVERLAPS),
    ({'type': 'array', 'items': 'string'}, _ARRAY, TypeRelation.DISJOINT),
    ('string', 'string[]', TypeRelation.DISJOINT),
    ('Any', 'File[]', TypeRelation.UNKNOWN), ({'type': 'array'}, 'string', TypeRelation.UNKNOWN),
    ({'type': 'array', 'items': None}, 'File[]', TypeRelation.UNKNOWN), (None, 'string', TypeRelation.UNKNOWN),
    ({'type': 'record', 'fields': []}, 'string', TypeRelation.UNKNOWN),
)


@pytest.mark.fast
@pytest.mark.parametrize(('source', 'sink', 'expected'), _RELATIONS)
def test_what_is_provably_disjoint(source: Any, sink: Any, expected: TypeRelation) -> None:
    """Disjoint only where both are judged and differ; what is not judged is unknown."""
    assert relation(read_type(source), read_type(sink)) is expected


@pytest.mark.fast
def test_a_record_delivers_what_its_merge_and_pick_make_of_its_sources() -> None:
    """A merged array stays flat, one source is not wrapped, a pick takes one element, none of a non-array is `Any`."""
    files, one = Array(Atom.FILE), Atom.FILE
    assert delivered(one, sources=1, merge=None, pick=None) == one
    assert delivered(one, sources=2, merge=Merge.NESTED, pick=None) == files
    assert delivered(files, sources=2, merge=Merge.FLATTENED, pick=None) == files
    assert delivered(files, sources=2, merge=Merge.NESTED, pick=None) == Array(files)
    assert delivered(one, sources=2, merge=Merge.NESTED, pick=Pick.FIRST) == one
    assert delivered(one, sources=1, merge=None, pick=Pick.ONLY) == Atom.ANY
    assert delivered(one, sources=2, merge=Merge.NESTED, pick=Pick.ALL) == files
    assert delivered(Union((Atom.NULL, Atom.INT)), sources=1, merge=None, pick=None) == Union((Atom.NULL, Atom.INT))
    assert delivered(Other(), sources=1, merge=Merge.NESTED, pick=Pick.FIRST) == Other()


@pytest.mark.fast
def test_an_api_type_with_an_empty_union_inside_a_union_is_judged_as_the_compiler_does() -> None:
    """The eager API reads a type as the compiler does, where a union that holds nothing adds nothing: `[[], 'File']`
    is `File`, disjoint from `string`."""
    from sophios.api.python.workflow import _python_api_types_match  # pylint: disable=import-outside-toplevel
    assert not _python_api_types_match('string', [[], 'File'])
    assert not _python_api_types_match('string', ['File'])
    assert relation(read_type([[], 'File']), Atom.STRING) is TypeRelation.DISJOINT


@pytest.mark.fast
def test_an_api_stream_type_is_the_file_the_compiler_reads_it_as() -> None:
    """`stdout` is a `File` to the eager API as to the compiler, so it is not a `Directory`."""
    from sophios.api.python.workflow import _python_api_types_match  # pylint: disable=import-outside-toplevel
    assert not _python_api_types_match('Directory', 'stdout')
    assert _python_api_types_match('File', 'stdout')


@pytest.mark.fast
def test_a_record_and_an_enum_are_nodes_and_what_they_wrote_beyond_is_unread() -> None:
    """A record holds its fields in the order declared, an enum its symbols; a key of either, a field's other keys and
    a shape no node holds are unread items at the place they were written, which scatter's layers move down."""
    record, unread = read_typed({'type': 'record', 'name': 'r', 'fields': [{'name': 'a', 'type': 'int', 'doc': 'd'}]})
    assert record == Record((('a', Atom.INT),))
    assert [(item.path, item.key, item.value) for item in unread] == [((), 'name', 'r'), (('fields', 'a'), 'doc', 'd')]
    assert read_typed({'type': 'enum', 'symbols': ['a', 'b']}) == (Enum(('a', 'b')), ())
    assert read_typed({'type': 'map'}) == (Other(), (Unread((), None, {'type': 'map'}),))
    assert read_typed(None) == (Other(), ())
    assert read_typed('example.org#Thing') == (Other('example.org#Thing'), ())


@pytest.mark.fast
@pytest.mark.parametrize('written, is_list, names', [
    ('string', False, {Atom.STRING}), ('string[]', True, {Atom.STRING}), ('string[]?', True, {Atom.STRING, Atom.NULL}),
    (['null', 'string', {'type': 'array', 'items': 'long'}], False, {Atom.NULL, Atom.STRING, Atom.LONG}),
    ({'type': 'array', 'items': 'double?'}, True, {Atom.DOUBLE, Atom.NULL}),
    ({'type': 'record', 'fields': [{'name': 'a', 'type': 'long'}]}, False, set()),
    ({'type': 'array'}, True, set()), (None, False, set()),
])
def test_a_target_asks_a_type_whether_it_lists_and_which_atoms_it_names(written: Any, is_list: bool, names: set[Atom]
                                                                        ) -> None:
    """A value of a type is a list, `null` aside, when the type is an array; the atoms a type names are those of its
    items and members, and a record's fields and an enum's symbols name none."""
    type_ = read_type(written)
    assert lists(type_) is is_list and atoms(type_) == names
