"""A neutral type, spelled as CWL writes one."""
from ..lang.nodes import OpaqueCwl
from ..lang.types import Array, Atom, Type, Unread, Union, spell


def written_type(type_: Type, unread: tuple[Unread, ...] = ()) -> OpaqueCwl:
    """`type_` as a port's `type:` field, with what the type wrote beyond its nodes (`unread`) put back: a `null` and
    one other type `X?`, an array of one type `X[]`, where `X` is a named type (and `X?` an array of one) -- CWL
    expands one suffix in that field alone; any other shape in the long form."""
    paths = {item.path for item in unread}
    match type_:
        case Union(members=(Atom.NULL, Array(items=Atom() as items))) if _named(items) \
                and not paths & {(), (1,), (1, 'items')}:
            return f'{items.value}[]?'
        case Union(members=(Atom.NULL, Atom() as member)) if _named(member) and not paths & {(), (1,)}:
            return f'{member.value}?'
        case Array(items=Atom() as items) if _named(items) and not paths & {(), ('items',)}:
            return f'{items.value}[]'
    return spell(type_, unread)


def _named(type_: Atom) -> bool:
    """Whether `type_` takes a suffix: any name but `null` and `Any`."""
    return type_ not in (Atom.NULL, Atom.ANY)
