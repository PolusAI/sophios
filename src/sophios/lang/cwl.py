"""The CWL substrate the Sophios language compiles onto.

The language reference pins Sophios to one CWL version; that version belongs
here, in the language definition, not to whichever module emits a document.
Import `CWL_VERSION` rather than writing a version literal.
"""
from enum import StrEnum
from typing import Final


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
