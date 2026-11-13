"""The CWL substrate the Sophios language compiles onto.

The language spec pins Sophios to one CWL version; that version belongs
here, in the language definition, not to whichever module emits a document.
Import `CWL_VERSION` rather than writing a version literal.
"""
from enum import Enum, StrEnum
from typing import Any, Final

from cwl_utils.parser import cwl_v1_2 as _standard


class CwlVersion(StrEnum):
    """Every CWL version the substrate toolchain actually accepts.

    Deliberately not the CWL spec's full `CWLVersion` enumeration: admitting a
    version cwltool cannot run would pass validation here and die later in the
    runner with a worse error. Sophios itself emits exactly one of these,
    `CWL_VERSION`.
    """

    V1_0 = 'v1.0'
    V1_1 = 'v1.1'
    V1_2 = 'v1.2'


#: The version Sophios emits. Every generated document declares this one.
#:
#: A plain `str`, not the enum member: PyYAML dispatches representers on
#: exact type, not `isinstance`, and cannot serialise a `StrEnum` subclass.
CWL_VERSION: Final[str] = CwlVersion.V1_2.value

#: Every admissible version as plain strings, for JSON Schema `enum` fields.
CWL_VERSIONS: Final[tuple[str, ...]] = tuple(version.value for version in CwlVersion)

# CWL's own vocabularies, read from the standard's schema as cwl_utils ships it: nothing else spells them.

#: The types that are not arrays, records or enums.
ATOMIC_TYPES: Final[frozenset[str]] = frozenset(_standard.CWLTypeLoader.symbols)

#: `scatterMethod`.
SCATTER_METHODS: Final[frozenset[str]] = frozenset(_standard.ScatterMethodLoader.symbols)

#: `linkMerge`.
LINK_MERGES: Final[frozenset[str]] = frozenset(_standard.LinkMergeMethodLoader.symbols)

#: `pickValue`, and the two of its methods that give one value where `all_non_null` gives a list.
PICK_VALUES: Final[frozenset[str]] = frozenset(_standard.PickValueMethodLoader.symbols)
PICKS_ONE: Final[frozenset[str]] = PICK_VALUES - {'all_non_null'}


def _hold_to_the_standard(enum: type[Enum], *loaders: Any) -> None:
    """Fail at import when `enum` has a member more or a member less than the standard's own enumerations."""
    symbols = {symbol for loader in loaders for symbol in loader.symbols}
    if (values := {member.value for member in enum}) != symbols:
        raise TypeError(f"{enum.__name__} is not the CWL standard's: {sorted(values ^ symbols)}")


class CWLAtomicType(str, Enum):
    """The names of `ATOMIC_TYPES`, and `Any`."""

    NULL = "null"
    BOOLEAN = "boolean"
    INT = "int"
    LONG = "long"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    FILE = "File"
    DIRECTORY = "Directory"
    ANY = "Any"


class ScatterMethod(str, Enum):
    """The names of `SCATTER_METHODS`."""

    dotproduct = "dotproduct"
    flat_crossproduct = "flat_crossproduct"
    nested_crossproduct = "nested_crossproduct"


_hold_to_the_standard(CWLAtomicType, _standard.CWLTypeLoader, _standard.AnyLoader)
_hold_to_the_standard(ScatterMethod, _standard.ScatterMethodLoader)
