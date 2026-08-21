"""The CWL substrate the Sophios language compiles onto.

The language reference pins Sophios to a specific CWL version and says the two
move together, so the version belongs to the language definition rather than to
whichever module happens to emit a document. Before this module there were six
places that named a version, and two of them disagreed with the other four: a
CommandLineTool generator still emitting `v1.0`, and a schema accepting any
non-empty string at all.

Import `CWL_VERSION` rather than writing a version literal. A test asserts that
no emitting path in the tree carries its own (see `tests/core/test_cwl_version.py`),
because the failure it prevents is silent: a document that declares the wrong
version still looks fine until a runner rejects a feature it should have had.

See docs/sophios_language_reference.md.
"""
from enum import StrEnum
from typing import Final


class CwlVersion(StrEnum):
    """Every CWL version the substrate toolchain actually accepts.

    Deliberately *not* the CWL spec's full `CWLVersion` enumeration. That list
    is a museum — `draft-2` and friends were dropped by cwltool years ago, and
    the `*-dev*` snapshots only run behind `--enable-dev` — so admitting a tag
    from it means passing validation here and dying later in the runner with a
    worse error. What Sophios validates is what it can process: accepting a
    version is a promise, and this enum is the set of versions the promise is
    kept for. Sophios itself emits exactly one of these, `CWL_VERSION`.

    https://www.commonwl.org/v1.2/Workflow.html#CWLVersion names the rest.
    """

    V1_0 = 'v1.0'
    V1_1 = 'v1.1'
    V1_2 = 'v1.2'


#: The version Sophios emits. Every generated document declares this one.
#:
#: A plain `str`, deliberately, not the enum member. `CwlVersion` is a
#: `StrEnum`, so it compares and formats like a string — but PyYAML dispatches
#: its representers on exact type, not `isinstance`, and refuses to serialise a
#: subclass it was not told about. Emitting the member would put an object into
#: every generated document that `yaml.safe_dump` cannot write. The enum stays
#: for validation; what gets embedded is the value.
CWL_VERSION: Final[str] = CwlVersion.V1_2.value

#: Every admissible version as plain strings, for JSON Schema `enum` fields.
CWL_VERSIONS: Final[tuple[str, ...]] = tuple(version.value for version in CwlVersion)
