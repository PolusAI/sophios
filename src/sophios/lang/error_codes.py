"""The identifiers a Sophios failure carries.

Its own module because a code is contract in a way the machinery around it is
not: a caller matches on `SophiosErrorCode.UNDEFINED_EDGE`, suppresses it, or
reads it out of a log, and none of that should require importing the reporting
plumbing.

**Two ranges, one enum.** `wic0NN` is the language: a document said something
the language does not accept, and every one of these is described in the
language reference. `api0NN` is the Python API: the document is fine and the
*call* was not -- a tool that will not load, a step bound to a workflow that
does not own it. They are one type because a caller wants one `except` and one
thing to match on; they are separate ranges because a language code in an API
position would send a reader to a section of the reference that does not
describe their problem.
"""
from enum import StrEnum


class SophiosErrorCode(StrEnum):
    """Stable identifiers for diagnostics.

    Codes are part of the contract: they can be matched on, suppressed, and
    documented, whereas message wording is free to improve.
    """

    INVALID_YAML = 'wic001'
    NOT_A_MAPPING = 'wic002'
    EXPECTED_MAPPING = 'wic003'
    EXPECTED_SEQUENCE = 'wic004'
    EXPECTED_SCALAR = 'wic005'
    MISSING_STEP_ID = 'wic006'
    EMPTY_STEP_ID = 'wic007'
    MALFORMED_WIC_STEP_KEY = 'wic008'
    UNKNOWN_TAG = 'wic009'
    DUPLICATE_KEY = 'wic010'
    UNRESOLVED_INPUT = 'wic011'
    MISSING_REQUIRED_INPUT = 'wic012'
    SUBWORKFLOW_INVALID = 'wic013'
    SCRIPT_ARGUMENT_MISMATCH = 'wic014'
    CONTAINER_ENGINE_UNAVAILABLE = 'wic015'
    MISSING_INPUT_FILE = 'wic016'
    UNKNOWN_LANG_VERSION = 'wic017'
    LANG_VERSION_CONFLICT = 'wic018'
    MISPLACED_EDGE_DEF = 'wic019'
    LITERAL_TYPE_MISMATCH = 'wic020'
    # wic021 is retired, not free: it was a second spelling of wic006.
    FIXED_POINT_NOT_REACHED = 'wic022'
    INCOMPATIBLE_INPUT_REFERENCE = 'wic023'
    RESERVED_KEY = 'wic024'
    UNDEFINED_EDGE = 'wic025'
    DUPLICATE_EDGE_DEF = 'wic026'
    #: A name the document left empty, in a position that identifies something:
    #: an input, an `out:` entry, or an edge. One code across the positions
    #: because it is one mistake -- the reader wrote nothing where a name goes.
    EMPTY_NAME = 'wic027'
    #: A step naming a port its resolved process does not have, on either
    #: side. One code across the positions because it is one mistake, and
    #: one code across the process kinds because a CommandLineTool has an
    #: interface just as a subworkflow does.
    UNDECLARED_PORT = 'wic028'
    RECURSIVE_ALIAS = 'wic030'

    #: --- Python API. The document is valid; the call was not. ---
    #:
    #: Kept out of the `wic0NN` range on purpose. These are not things a `.wic`
    #: file can be wrong about, so a reader who looks one up in the language
    #: reference should find nothing rather than the wrong thing.
    INVALID_INPUT_VALUE = 'api001'
    INVALID_STEP = 'api002'
    INVALID_LINK = 'api003'
    INVALID_TOOL = 'api004'

    @property
    def is_language(self) -> bool:
        """Whether this code describes a document rather than a call."""
        return self.value.startswith('wic')
