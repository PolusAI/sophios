"""The types a port can carry, as Sophios means them and not as any one target spells them.

A port's type is a `Type`: an `Atom`, an `Array`, a `Union`, a `Record`, an `Enum` or `Other`, the one thing it was
not able to judge. The meaning is what the compiler reads; a target spells it in its own words. The door that reads a
spelling (`read_type`, which also holds the one shorthand every door reads) builds the node, and what a spelling says
that no node holds (a key of a schema, an unresolved shape) is an `Unread` item on the port, which only the target
that wrote it back reads. Pure data and functions over it, importing nothing of the front door or of a target.
"""
import enum
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, NewType, TypeAlias

if TYPE_CHECKING:
    from .nodes import OpaqueCwl


class Atom(str, enum.Enum):
    """The names a type can be. `Any` stands for a value nothing is proved against."""

    NULL = 'null'
    BOOLEAN = 'boolean'
    INT = 'int'
    LONG = 'long'
    FLOAT = 'float'
    DOUBLE = 'double'
    STRING = 'string'
    FILE = 'File'
    DIRECTORY = 'Directory'
    ANY = 'Any'


@dataclass(frozen=True, slots=True)
class Array:
    """A list of `items`. `items` is None only where the type wrote no `items` at all: a type that wrote
    `items: null` has `Other()` items, a different thing to take an element of."""

    items: 'Type | None'


@dataclass(frozen=True, slots=True)
class Union:
    """Any one of `members`: `null` first when it is one, no member twice, none itself a union."""

    members: 'tuple[Type, ...]'


@dataclass(frozen=True, slots=True)
class Record:
    """A value with the named `fields`, each a type, in the order they were declared."""

    fields: 'tuple[tuple[str, Type], ...]'


@dataclass(frozen=True, slots=True)
class Enum:
    """One of the `symbols`, in the order they were declared."""

    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Other:
    """A type no node holds and nothing judges: a name that no door resolved here (`name`, which tells two such
    types apart) or no type at all (`name` None, the shape of a type that wrote none or wrote one that is none of
    the above, which the port's `Unread` item keeps)."""

    name: str | None = None


#: What a port carries.
Type: TypeAlias = Atom | Array | Union | Record | Enum | Other

#: Where in a type a node sits: `items` into an array, an index into a union's members, `fields` and a field's name
#: into a record, and `type` after those into the type a field holds.
Place: TypeAlias = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class Unread:
    """What a type wrote that no node of it holds: `value` under `key` of the schema at `path` or, with no `key`,
    the whole spelling of the node there. The middle end never reads it; the target that spells the type puts it back
    where it was."""

    path: Place
    key: str | None
    value: 'OpaqueCwl'


@dataclass(frozen=True, slots=True)
class PortDeclaration:  # pylint: disable=too-many-instance-attributes
    """The complete declaration of one process or workflow port. The fields
    beside `type` preserve emission facts without inviting Link or Infer to
    interpret arbitrary CWL; `has_default` distinguishes an authored
    `default: null` from no default at all.
    """

    type: Type
    format: 'OpaqueCwl' = None
    has_format: bool = False
    default: 'OpaqueCwl' = None
    has_default: bool = False
    passthrough: 'tuple[tuple[str, OpaqueCwl], ...]' = ()
    shorthand: bool = False
    #: What the type wrote that none of its nodes holds, in the order the door found it.
    unread: tuple[Unread, ...] = ()
    #: The fields of `passthrough` a workflow boundary promoted from this port may state, already reduced, in order.
    kept: 'tuple[tuple[str, OpaqueCwl], ...]' = ()


#: A `PortDeclaration` reduced to what a workflow boundary may state. Only
#: `declarations.boundary_declaration` produces one.
BoundaryDeclaration = NewType('BoundaryDeclaration', PortDeclaration)


class ScatterMethod(str, enum.Enum):
    """How a scatter over several inputs combines them: the names of the standard's `scatterMethod`."""

    dotproduct = "dotproduct"
    flat_crossproduct = "flat_crossproduct"
    nested_crossproduct = "nested_crossproduct"


class Merge(str, enum.Enum):
    """How a step input joins several sources into one value."""

    NESTED = 'merge_nested'
    FLATTENED = 'merge_flattened'


class Pick(str, enum.Enum):
    """Which of the non-null values a step input takes."""

    FIRST = 'first_non_null'
    ONLY = 'the_only_non_null'
    ALL = 'all_non_null'


#: The names of the methods above, which a document spells them with.
SCATTER_METHODS: Final = frozenset(method.value for method in ScatterMethod)
LINK_MERGES: Final = frozenset(way.value for way in Merge)
PICK_VALUES: Final = frozenset(way.value for way in Pick)
#: The two picks that give one value where `all_non_null` gives a list.
PICKS_ONE: Final = frozenset({Pick.FIRST.value, Pick.ONLY.value})


class TypeRelation(enum.Enum):
    """What Sophios can prove about two endpoint types."""

    OVERLAPS = 'overlaps'
    DISJOINT = 'disjoint'
    UNKNOWN = 'unknown'


def spell(type_: Type, unread: tuple[Unread, ...] = (), path: Place = ()) -> 'OpaqueCwl':
    """`type_` as 0.1.0 writes it, in plain `str`, `list` and `dict` values: the text a diagnostic prints and a
    target reads where it has no spelling of its own. With the `unread` items of its port, the spelling that read as
    `type_` and said what no node holds."""
    held = [item for item in unread if item.path == path]
    if any(item.key is None for item in held):
        return deepcopy(held[0].value)
    body: dict[str, OpaqueCwl]
    match type_:
        case Atom():
            return type_.value
        case Other(name=name):
            return name
        case Union(members=members):
            return [spell(member, unread, (*path, index)) for index, member in enumerate(members)]
        case Array(items=items):
            body = {'type': 'array'}
            if items is not None:
                body['items'] = spell(items, unread, (*path, 'items'))
        case Record(fields=fields):
            body = {'type': 'record', 'fields': [
                {'name': name, 'type': spell(field, unread, (*path, 'fields', name, 'type')),
                 **{str(item.key): deepcopy(item.value) for item in unread if item.path == (*path, 'fields', name)}}
                for name, field in fields]}
        case Enum(symbols=symbols):
            body = {'type': 'enum', 'symbols': list(symbols)}
        case _:
            raise TypeError(f'not a type: {type_!r}')
    return body | {str(item.key): deepcopy(item.value) for item in held}


def nullable(type_: Type) -> Type:
    """`type_` or `null`: a union with `null` first and no member twice."""
    members = type_.members if isinstance(type_, Union) else (type_,)
    return Union((Atom.NULL, *(member for member in members if member is not Atom.NULL)))


def without_null(type_: Type) -> tuple[Type, bool]:
    """`type_` without its `null`, and whether it had one: the one type that remains, or the union of the several."""
    if not isinstance(type_, Union) or Atom.NULL not in type_.members:
        return type_, False
    rest = tuple(member for member in type_.members if member is not Atom.NULL)
    return (rest[0] if len(rest) == 1 else Union(rest)), True


def optional(type_: Type) -> bool:
    """Whether a port of `type_` accepts `null` itself, not merely in its items."""
    return isinstance(type_, Union) and Atom.NULL in type_.members


def lists(type_: Type) -> bool:
    """Whether a value of `type_` is a list, `null` aside."""
    return isinstance(without_null(type_)[0], Array)


def atoms(type_: Type) -> frozenset[Atom]:
    """Every atom `type_` names, through the items of an array and the members of a union."""
    match type_:
        case Atom():
            return frozenset({type_})
        case Array(items=items) if items is not None:
            return atoms(items)
        case Union(members=members):
            return frozenset().union(*map(atoms, members))
    return frozenset()


def typed(type_: Type) -> bool:
    """Whether a port of `type_` says what it carries."""
    return type_ != Other()


def layered(type_: Type, rank: int) -> Type:
    """`type_` inside `rank` array layers."""
    for _ in range(rank):
        type_ = Array(type_)
    return type_


def delivered(produced: Type, *, sources: int, merge: 'Merge | OpaqueCwl', pick: 'Pick | OpaqueCwl') -> Type:
    """The type a step-input record delivers from sources of type `produced`: the list `merge` makes of them (a
    merged array stays flat; one source with no `merge` is not wrapped) or, for a `pick` that picks one value, one
    element of it. `Any` where a pick has no array to take an element of, so nothing is proved against it."""
    if (sources == 1 and merge is None) or (merge is Merge.FLATTENED and isinstance(produced, Array)):
        merged = produced
    else:
        merged = Array(produced)
    if pick not in (Pick.FIRST, Pick.ONLY):
        return merged
    return merged.items if isinstance(merged, Array) and merged.items is not None else Atom.ANY


def _kind(type_: Type) -> str | None:
    """An atom's own name, `array` for an array, and None for what Sophios does not judge: `Any`, a record, an enum,
    a named schema. An array with no `items` is malformed, not an array: calling it one would answer `disjoint` for
    what a target's own validation is to reject."""
    match type_:
        case Array(items=items) if items is not None:
            return 'array'
        case Atom() if type_ is not Atom.ANY:
            return str(type_.value)
    return None


def _of_union(relations: list[TypeRelation]) -> TypeRelation:
    """What the members of a union prove together: overlapping if any does, disjoint only if every one is."""
    if TypeRelation.OVERLAPS in relations:
        return TypeRelation.OVERLAPS
    return TypeRelation.DISJOINT if relations and set(relations) == {TypeRelation.DISJOINT} else TypeRelation.UNKNOWN


def relation(source: Type, sink: Type) -> TypeRelation:
    """The relation Sophios can prove between two endpoint types.

    Conservative: a caller may reject a reference only for `DISJOINT`; `UNKNOWN` leaves the final decision to the
    target's own validation of what it emits.
    """
    if isinstance(source, Union):
        return _of_union([relation(member, sink) for member in source.members])
    if isinstance(sink, Union):
        return _of_union([relation(source, member) for member in sink.members])
    match source, sink:
        case Array(items=source_items), Array(items=sink_items) if source_items is not None and sink_items is not None:
            return relation(source_items, sink_items)
    kinds = _kind(source), _kind(sink)
    if None in kinds:
        return TypeRelation.UNKNOWN
    return TypeRelation.OVERLAPS if kinds[0] == kinds[1] else TypeRelation.DISJOINT


def types_match(in_type: Type, out_type: Type) -> bool:
    """Legacy inferred-candidate relation, intentionally not a rejection judge."""
    if in_type == out_type:
        return True
    match in_type, out_type:
        case Union(members=members), Union(members=others):
            return any(member in members for member in others)
        case Union(members=members), _:
            return any(member == out_type for member in members)
        case _, Union(members=members):
            return any(member == in_type for member in members)
    return False


def permits_format(type_: Type | None) -> bool:
    """Whether a value of `type_` can carry a `format`: a `File`, or an array or union holding one."""
    match type_:
        case Atom.FILE:
            return True
        case Array(items=items):
            return permits_format(items)
        case Union(members=members):
            return any(permits_format(member) for member in members)
    return False


def expand_shorthand(declared: 'OpaqueCwl') -> 'OpaqueCwl':
    """One layer of CWL's type shorthand, without changing the caller's declaration: `T[]` is an array of `T`, and
    `T?` is `T` or null."""
    if not isinstance(declared, str):
        return declared
    if declared.endswith('[]') and len(declared) > 2:
        return {'type': 'array', 'items': declared[:-2]}
    if declared.endswith('?') and len(declared) > 1:
        return ['null', declared[:-1]]
    return declared


_ATOMS: Final = {atom.value: atom for atom in Atom}
_STREAMS: Final = ('stdin', 'stdout', 'stderr')


def read_type(raw: 'OpaqueCwl') -> Type:
    """The neutral type a written one means; `read_typed` says what it wrote beyond that. `T?` and `T[]` shorthand
    is expanded wherever it is written, through array items and every union member, so `string?[]` is an array of
    nullable strings and not a nullable array, and `[File?, string]` accepts null in either spelling of that member.
    `stdin`, `stdout` and `stderr` mean `File`, the tool's own shorthand for "the stream, as a File"; only a
    CommandLineTool port may say it, and a workflow boundary promoted from one must say `File`, which is what
    cwltool makes of it. A type that holds itself is `Other` where it comes round again."""
    return _read(raw, (), [], frozenset())


def read_typed(raw: 'OpaqueCwl') -> tuple[Type, tuple[Unread, ...]]:
    """`read_type`, and what the type wrote that no node of it holds: every key of a schema but the ones its node
    holds, a record field's keys but its name and type, and the whole of a shape that is none of the types."""
    found: list[Unread] = []
    return _read(raw, (), found, frozenset()), tuple(found)


def _fields(raw: dict[str, Any]) -> 'list[tuple[str, Any, dict[str, Any]]] | None':
    """A record's fields as `(name, type, other keys)` in the order written, from the list form or the map form, or
    None where it writes neither."""
    match raw.get('fields'):
        case list() as entries if all(isinstance(entry, dict) and isinstance(entry.get('name'), str)
                                      and 'type' in entry for entry in entries):
            return [(entry['name'], entry['type'], {key: value for key, value in entry.items()
                                                    if key not in ('name', 'type')}) for entry in entries]
        case dict() as entries:
            return [(str(name), entry['type'], {key: value for key, value in entry.items() if key != 'type'})
                    if isinstance(entry, dict) and 'type' in entry else (str(name), entry, {})
                    for name, entry in entries.items()]
    return None


def _read(raw: Any, path: Place, unread: list[Unread], open_: frozenset[int]) -> Type:
    """`raw` as a type, the keys no node holds appended to `unread` at `path`."""
    if isinstance(raw, str):
        if raw in _STREAMS:
            return Atom.FILE
        layer = expand_shorthand(raw)
        if layer is not raw:
            return _read(layer, path, unread, open_)
        return _ATOMS.get(raw) or Other(raw)
    if id(raw) in open_:
        unread.append(Unread(path, None, deepcopy(raw)))
        return Other()
    inside = open_ | {id(raw)}
    if isinstance(raw, list):
        # A member's own shorthand, expanded: `null` once, first; no duplicates. What a member left unread moves to
        # the place its type takes in the union that results, not the place it was written.
        read: list[tuple[Type, list[Unread]]] = []
        for item in raw:
            held: list[Unread] = []
            read.append((_read(item, (), held, inside), held))
        members: list[Type] = []
        for expanded, _ in read:
            for member in expanded.members if isinstance(expanded, Union) else (expanded,):
                if member not in members:
                    members.append(member)
        union = Union((*[Atom.NULL] * (Atom.NULL in members), *(member for member in members if member != Atom.NULL)))
        for expanded, held in read:
            for found in held:
                inner, rest = ((expanded.members[int(found.path[0])], found.path[1:]) if isinstance(expanded, Union)
                               else (expanded, found.path))
                unread.append(replace(found, path=(*path, union.members.index(inner), *rest)))
        return union
    kind = raw.get('type') if isinstance(raw, dict) else None
    fields = _fields(raw) if kind == 'record' else None
    if kind not in ('array', 'record', 'enum') or kind == 'record' and fields is None \
            or kind == 'enum' and not (isinstance(raw.get('symbols'), list)
                                       and all(isinstance(symbol, str) for symbol in raw['symbols'])):
        if raw is not None:
            unread.append(Unread(path, None, deepcopy(raw)))
        return Other()
    keys = {'array': ('type', 'items'), 'record': ('type', 'fields'), 'enum': ('type', 'symbols')}[kind]
    unread.extend(Unread(path, str(key), deepcopy(value))
                  for key, value in sorted(raw.items(), key=lambda entry: str(entry[0])) if key not in keys)
    match kind:
        case 'array':
            return Array(_read(raw['items'], (*path, 'items'), unread, inside) if 'items' in raw else None)
        case 'record':
            for name, _, other in fields or ():
                unread.extend(Unread((*path, 'fields', name), key, deepcopy(value)) for key, value in other.items())
            return Record(tuple((name, _read(field, (*path, 'fields', name, 'type'), unread, inside))
                                for name, field, _ in fields or ()))
    return Enum(tuple(raw['symbols']))


def required(type_: 'OpaqueCwl') -> 'tuple[OpaqueCwl, bool]':
    """A port's type without its `null`, and whether it had one: the one type that remains, or the list of the
    several."""
    if not isinstance(type_, list) or 'null' not in type_:
        return type_, False
    rest = [member for member in type_ if member != 'null']
    return (rest[0] if len(rest) == 1 else rest), True
