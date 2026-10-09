"""The YAML tags Sophios owns, and the desugared key each one is also spelled as.

What a tag or key means is decided by `sophios.lang.parse`, not here.
"""
from typing import ClassVar, Final, final


@final
class Tag:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The custom YAML tags Sophios owns.

    A namespace rather than loose module constants: these four are one
    vocabulary and are always reasoned about together, and grouping them keeps
    `TAG_` prefixes from being the only thing relating them. Class attributes
    are read-only in practice and shared safely across threads.
    """

    ANCHOR: Final = '!&'
    ALIAS: Final = '!*'
    INLINE_INPUT: Final = '!ii'
    RAW_CWL: Final = '!cwl'

    #: Every tag, for membership tests. Assigned below, from the members
    #: declared above, so adding or renaming one cannot leave it behind.
    ALL: ClassVar[frozenset[str]]


@final
class Key:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The desugared spelling of each tag: a single-key mapping such as
    `{wic_inline_input: value}`, the form `sophios.lang.to_json` writes.
    """

    ANCHOR: Final = 'wic_anchor'
    ALIAS: Final = 'wic_alias'
    INLINE_INPUT: Final = 'wic_inline_input'
    RAW_CWL: Final = 'wic_raw_cwl'

    #: Every desugared key, for membership tests. Derived, as above.
    ALL: ClassVar[frozenset[str]]


def _declared_spellings(vocabulary: type) -> frozenset[str]:
    """Every spelling declared on a vocabulary namespace.

    Derived rather than restated. A hand-written membership set is one edit
    away from being wrong, and the failure is quiet in the worst way: the
    parser reads these sets to decide which tags it owns, so a tag missing
    from `ALL` would be diagnosed as unknown.
    """
    return frozenset(value for name, value in vars(vocabulary).items()
                     if name.isupper() and isinstance(value, str))


Tag.ALL = _declared_spellings(Tag)
Key.ALL = _declared_spellings(Key)
