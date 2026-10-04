"""The identifiers a Sophios failure carries.

Its own module because a code is contract: a caller matches on
`SophiosErrorCode.UNDEFINED_EDGE`, suppresses it, or reads it out of a log,
without importing the reporting plumbing.

Two ranges, one enum: `wic0NN` is the language (a document said something
the language does not accept); `api0NN` is the Python API (the document is
fine, the call was not). One type so a caller matches on one `except`;
separate ranges so an API code does not point at the language reference.
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
    # wic021 is retired, not free (duplicated wic006).
    FIXED_POINT_NOT_REACHED = 'wic022'
    INCOMPATIBLE_INPUT_REFERENCE = 'wic023'
    RESERVED_KEY = 'wic024'
    UNDEFINED_EDGE = 'wic025'
    DUPLICATE_EDGE_DEF = 'wic026'
    #: A name the document left empty, in an input, an `out` entry or an edge.
    EMPTY_NAME = 'wic027'
    #: A step naming a port its resolved process does not have, on either
    #: side, for either a CommandLineTool or a subworkflow.
    UNDECLARED_PORT = 'wic028'
    RECURSIVE_ALIAS = 'wic030'
    #: Two different things the emitted document would spell the same way:
    #: ports, e.g. an authored name equal to one the compiler derives, or the
    #: URIs one `$namespaces` prefix would stand for in the `format:` of a
    #: promoted port.
    DUPLICATE_DOCUMENT_NAME = 'wic031'
    #: A scatter entry naming no input of its step, or one nothing binds once
    #: inference is done. On a subworkflow call the inputs are the ones the
    #: subworkflow declares.
    UNKNOWN_SCATTER_PORT = 'wic032'
    #: A key in a ``wic:`` block that the block does not have (§5).
    UNKNOWN_WIC_KEY = 'wic033'
    #: A ``wic:`` key whose value does not have the shape the key declares (§5).
    MALFORMED_WIC_VALUE = 'wic034'
    #: An authored `cwlVersion` the substrate toolchain does not run (§1).
    UNSUPPORTED_CWL_VERSION = 'wic035'
    #: An authored workflow output with no `type` and no producer to take one from.
    UNTYPED_OUTPUT = 'wic036'
    #: CWL's WorkflowStepInput written where Sophios does not read it: an
    #: untagged mapping in `in:` carrying `source`, `default`, `linkMerge`,
    #: ..., a `!cwl {...}` record carrying a key or a `source` it may not, or
    #: `linkMerge`/`pickValue`/a list `outputSource` on a workflow output.
    STEP_INPUT_RECORD = 'wic038'

    #: A positional `(index, name)/port` outputSource whose index does not hold
    #: that step, or written in a workflow with an inferred edge.
    POSITIONAL_OUTPUT_SOURCE = 'wic039'

    #: --- Python API. The document is valid; the call was not. ---
    INVALID_INPUT_VALUE = 'api001'
    INVALID_STEP = 'api002'
    INVALID_LINK = 'api003'
    INVALID_TOOL = 'api004'
    #: A local run that ended with a non-zero exit code. The document and the
    #: call were fine; the runner was not.
    WORKFLOW_RUN_FAILED = 'api005'

    @property
    def is_language(self) -> bool:
        """Whether this code describes a document rather than a call."""
        return self.value.startswith('wic')
