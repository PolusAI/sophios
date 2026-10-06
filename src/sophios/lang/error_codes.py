"""The identifiers a Sophios failure carries.

Its own module because a code is contract: a caller matches on
`SophiosErrorCode.UNDEFINED_EDGE`, suppresses it, or reads it out of a log,
without importing the reporting plumbing.

Two ranges, one enum: `wic0NN` is a document (it said something the language
does not accept) or what running it needs from the machine (kind `machine`,
such as wic015, wic016 and wic021); `api0NN` is the Python API (the document is
fine, the call was not). One type so a caller matches on one `except`;
separate ranges so an API code does not point at the language guide.

Every member has an `Explanation` (`EXPLANATIONS`): what it means and what to
do, which `sophios --explain` and docs/error_codes.md show.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


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
    #: A directory a compile or a run writes into that this user cannot write.
    DIRECTORY_NOT_WRITABLE = 'wic021'
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
    #: A key in a ``wic:`` block that the block does not have (language guide §7).
    UNKNOWN_WIC_KEY = 'wic033'
    #: A ``wic:`` key whose value does not have the shape the key declares (language guide §7).
    MALFORMED_WIC_VALUE = 'wic034'
    #: An authored `cwlVersion` the substrate toolchain does not run (language spec §1).
    UNSUPPORTED_CWL_VERSION = 'wic035'
    #: An authored workflow output with no `type` and no producer to take one from.
    UNTYPED_OUTPUT = 'wic036'
    #: CWL's WorkflowStepInput written where Sophios does not read it: an
    #: untagged mapping in `in:` carrying `source`, `default`, `linkMerge`,
    #: ..., a `!cwl {...}` record carrying a key or a `source` it may not, or
    #: `linkMerge`/`pickValue`/a list `outputSource` on a workflow output.
    STEP_INPUT_RECORD = 'wic038'

    #: A positional `(index, name)/port` outputSource whose index does not hold
    #: that step.
    POSITIONAL_OUTPUT_SOURCE = 'wic039'

    #: Notes, not errors (errors under --inference_strict): inference chose
    #: between equals. wic042: one producer offered several matching outputs.
    #: wic043: an earlier producer also matched and recency decided.
    INFERENCE_TIE = 'wic042'
    INFERENCE_RECENCY = 'wic043'

    #: A real-time analysis declaration (a `cwl_subinterpreter` step) that is
    #: not well formed, or whose analysis does not compile.
    REALTIME_DECLARATION = 'wic044'

    #: --- Python API. The document is valid; the call was not. ---
    INVALID_INPUT_VALUE = 'api001'
    INVALID_STEP = 'api002'
    INVALID_LINK = 'api003'
    INVALID_TOOL = 'api004'
    #: A local run that ended with a non-zero exit code. The document and the
    #: call were fine; the runner was not.
    WORKFLOW_RUN_FAILED = 'api005'
    #: A `.wic` construct the Python API has no spelling for, met by
    #: `Workflow.from_wic`. The document is valid and compiles; the API cannot
    #: hold it. The message names the construct and what to write instead.
    NO_PYTHON_SPELLING = 'api006'

    @property
    def is_language(self) -> bool:
        """Whether this code describes a document rather than a call."""
        return self.value.startswith('wic')

    @property
    def explanation(self) -> 'Explanation':
        """What this code means and what to do about it."""
        return EXPLANATIONS[self]


class Kind(StrEnum):
    """What a reader changes when a code is reported."""

    DOCUMENT = 'document'  #: the .wic file or the Python workflow
    CALL = 'call'          #: the Python API call
    MACHINE = 'machine'    #: this machine: what is installed, running, readable or writable
    RUN = 'run'            #: nothing in Sophios: a tool failed while it ran


@dataclass(frozen=True, slots=True)
class Explanation:
    """What a code means and what to do about it, in one line each."""

    kind: Kind
    meaning: str
    fix: str


#: One entry per member. docs/error_codes.md shows the same text; a test keeps them equal.
EXPLANATIONS: Final[Mapping[SophiosErrorCode, Explanation]] = {
    SophiosErrorCode.INVALID_YAML: Explanation(
        Kind.DOCUMENT,
        'The file is not valid YAML.',
        'Correct the YAML at the position shown: an indentation, a missing `:`, or an unclosed quote or bracket.'),
    SophiosErrorCode.NOT_A_MAPPING: Explanation(
        Kind.DOCUMENT,
        'The document is not a mapping.',
        'Make the top level a mapping, with keys such as `inputs:`, `steps:` and `outputs:`.'),
    SophiosErrorCode.EXPECTED_MAPPING: Explanation(
        Kind.DOCUMENT,
        'A mapping was expected here, as in `steps: 3`.',
        'Write `key: value` entries at the position shown.'),
    SophiosErrorCode.EXPECTED_SEQUENCE: Explanation(
        Kind.DOCUMENT,
        'A sequence was expected here, as in `out: 3`.',
        'Write a list at the position shown: `- item` lines, or `[a, b]`.'),
    SophiosErrorCode.EXPECTED_SCALAR: Explanation(
        Kind.DOCUMENT,
        'A scalar was expected here, such as a mapping key that is itself a list.',
        'Write one name, number or string at the position shown.'),
    SophiosErrorCode.MISSING_STEP_ID: Explanation(
        Kind.DOCUMENT,
        'A step in a sequence has no `id:`.',
        'Add `id:` with the tool or workflow the step runs, or write `steps:` as a mapping keyed by step (language '
        'guide §2.1).'),
    SophiosErrorCode.EMPTY_STEP_ID: Explanation(
        Kind.DOCUMENT,
        "A step's `id:` is empty.",
        'Write the name of the tool or workflow the step runs.'),
    SophiosErrorCode.MALFORMED_WIC_STEP_KEY: Explanation(
        Kind.DOCUMENT,
        'A `wic: steps:` key is neither `(index, name)` nor a step id.',
        "Key the entry `(index, name)` with the step's 1-based position and id, or by the id alone when no other "
        "step has it (language guide §7)."),
    SophiosErrorCode.UNKNOWN_TAG: Explanation(
        Kind.DOCUMENT,
        'An unknown YAML tag; the Sophios tags are `!ii`, `!&`, `!*` and `!cwl`.',
        'Use one of the four Sophios tags, or remove the tag.'),
    SophiosErrorCode.DUPLICATE_KEY: Explanation(
        Kind.DOCUMENT,
        'A key bound twice: an input, a step, a `wic:` key, or a second `id:`.',
        'Keep one of the two and delete the other (language guide §3.8).'),
    SophiosErrorCode.UNRESOLVED_INPUT: Explanation(
        Kind.DOCUMENT,
        'An untagged input value names no workflow input.',
        'Write a literal as `!ii <value>`, or declare the name under `inputs:` (language guide §3.3).'),
    SophiosErrorCode.MISSING_REQUIRED_INPUT: Explanation(
        Kind.DOCUMENT,
        'A required input gets no value, such as `!ii null` on an input that is not optional.',
        "Bind the input to a value, an edge or a workflow input, or make the tool's input optional."),
    SophiosErrorCode.SUBWORKFLOW_INVALID: Explanation(
        Kind.DOCUMENT,
        "A workflow or a step's process cannot be used: no steps, a tool or subworkflow not found, a CWL `Workflow` "
        "as a step, subworkflows that call each other in a cycle, `implementations` with none chosen, or ports only "
        "the CWL runner can read.",
        'Do what the message names: put the tool or `.wic` file on a search path of the config (`search_paths_cwl`, '
        '`search_paths_wic`), give the workflow steps, or break the cycle (language guide §2.3).'),
    SophiosErrorCode.SCRIPT_ARGUMENT_MISMATCH: Explanation(
        Kind.DOCUMENT,
        'The arguments given to a Python script step do not match its declared inputs.',
        'Bind exactly the inputs the script declares.'),
    SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE: Explanation(
        Kind.MACHINE,
        'The container engine (`docker` by default) is not installed or not working.',
        'Install or start the engine as the message says; `--container_engine` names another engine, and '
        '`--ignore_docker_install` skips the check.'),
    SophiosErrorCode.MISSING_INPUT_FILE: Explanation(
        Kind.MACHINE,
        'An input file named in the inputs does not exist.',
        'Correct the path or create the file; a relative path is read beside the workflow file.'),
    SophiosErrorCode.UNKNOWN_LANG_VERSION: Explanation(
        Kind.DOCUMENT,
        'An unknown `lang_version`.',
        'Pin a version the message lists, or remove the pin.'),
    SophiosErrorCode.LANG_VERSION_CONFLICT: Explanation(
        Kind.DOCUMENT,
        'Two `lang_version` pins in one compilation disagree.',
        'Make the pins agree, or set one version for the whole compilation with `--lang_version`.'),
    SophiosErrorCode.MISPLACED_EDGE_DEF: Explanation(
        Kind.DOCUMENT,
        "`!&` outside a step's `out:` entry.",
        "Name the output with `!&` on the producing step's `out:` entry, and read it with `!*` (language guide "
        "§3.6)."),
    SophiosErrorCode.LITERAL_TYPE_MISMATCH: Explanation(
        Kind.DOCUMENT,
        "A literal does not have the type of its input, or a scattered input's literal is not a list.",
        "Write a value of the input's type, quoting a string YAML would read as something else, and give a scattered"
        " input a list (language guide §3.2, §6.1)."),
    SophiosErrorCode.DIRECTORY_NOT_WRITABLE: Explanation(
        Kind.MACHINE,
        'A directory Sophios writes into cannot be written.',
        'Run Sophios from a directory you can write to; from Python, `run(basepath=...)` takes another.'),
    SophiosErrorCode.FIXED_POINT_NOT_REACHED: Explanation(
        Kind.DOCUMENT,
        'Inference did not settle within its iteration limit.',
        'Bind the inputs explicitly with `!&` and `!*` or workflow inputs, or drop `--insert_steps_automatically`.'),
    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE: Explanation(
        Kind.DOCUMENT,
        'A reference whose type can never feed the input it binds, such as a `string` into a `File`.',
        'Bind the input to a source of a matching type.'),
    SophiosErrorCode.RESERVED_KEY: Explanation(
        Kind.DOCUMENT,
        'A `wic_` mapping in an input position that is not a Sophios construct.',
        'Correct the spelling to a construct of language guide §3.7, or rename the key.'),
    SophiosErrorCode.UNDEFINED_EDGE: Explanation(
        Kind.DOCUMENT,
        'An edge reference with no definition before it, anywhere in the compilation.',
        "Define the name with `!&` on an earlier step's `out:` entry, or correct its spelling (language guide §4)."),
    SophiosErrorCode.DUPLICATE_EDGE_DEF: Explanation(
        Kind.DOCUMENT,
        'An edge name defined twice.',
        'Give each `!&` its own name.'),
    SophiosErrorCode.EMPTY_NAME: Explanation(
        Kind.DOCUMENT,
        'An input, `out:` entry or edge with an empty name.',
        'Write the name.'),
    SophiosErrorCode.UNDECLARED_PORT: Explanation(
        Kind.DOCUMENT,
        "An input or output name the step's process does not declare.",
        'Use a port the tool or subworkflow declares.'),
    SophiosErrorCode.RECURSIVE_ALIAS: Explanation(
        Kind.DOCUMENT,
        'A YAML alias that contains itself.',
        'Remove the alias that refers back to its own anchor.'),
    SophiosErrorCode.DUPLICATE_DOCUMENT_NAME: Explanation(
        Kind.DOCUMENT,
        'Two ports the compiled CWL would spell the same way, or a namespace prefix bound to two URIs.',
        'Rename one of the two ports, or bind the prefix to one URI (language guide §5.2).'),
    SophiosErrorCode.UNKNOWN_SCATTER_PORT: Explanation(
        Kind.DOCUMENT,
        'A `scatter:` entry that is not an input of its step.',
        'Scatter over an input the step declares and binds (language guide §6.1).'),
    SophiosErrorCode.UNKNOWN_WIC_KEY: Explanation(
        Kind.DOCUMENT,
        'A key the `wic:` block does not have.',
        'Use a key from language guide §7, or remove it.'),
    SophiosErrorCode.MALFORMED_WIC_VALUE: Explanation(
        Kind.DOCUMENT,
        'A `wic:` value of the wrong shape.',
        'Write the value in the shape language guide §7 gives for its key.'),
    SophiosErrorCode.UNSUPPORTED_CWL_VERSION: Explanation(
        Kind.DOCUMENT,
        'A `cwlVersion` other than `v1.0`, `v1.1` or `v1.2`.',
        'Declare `cwlVersion: v1.2`.'),
    SophiosErrorCode.UNTYPED_OUTPUT: Explanation(
        Kind.DOCUMENT,
        'A workflow output with no `type:` and no producer to take it from.',
        'Give the output a `type:`, or an `outputSource:` naming the step output it comes from (language guide '
        '§5.2).'),
    SophiosErrorCode.STEP_INPUT_RECORD: Explanation(
        Kind.DOCUMENT,
        "CWL's step input written where Sophios does not read it: an untagged mapping of step-input fields, a "
        "malformed `!cwl {...}` record, or a list `outputSource`, `linkMerge` or `pickValue` on a workflow output.",
        'Write step-input fields as a `!cwl {...}` record (language guide §3.5); a workflow output names one step '
        'output.'),
    SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE: Explanation(
        Kind.DOCUMENT,
        'A positional `(index, name)` `outputSource` that names the wrong step.',
        'Use the index and id of the step the output comes from, or write `step/port` (language guide §5.2).'),
    SophiosErrorCode.INFERENCE_TIE: Explanation(
        Kind.DOCUMENT,
        'Note: one step offered several matching outputs and inference took the last.',
        'Pin the choice the note shows, with `!&` on the output and `!*` on the input (language guide §8); '
        '`--inference_strict` makes this an error.'),
    SophiosErrorCode.INFERENCE_RECENCY: Explanation(
        Kind.DOCUMENT,
        'Note: an earlier step also matched and inference took the most recent.',
        'Pin the choice the note shows, with `!&` on the output and `!*` on the input (language guide §8); '
        '`--inference_strict` makes this an error.'),
    SophiosErrorCode.REALTIME_DECLARATION: Explanation(
        Kind.DOCUMENT,
        'A real-time analysis declaration (a `cwl_subinterpreter` step) with an input that is not a literal or has '
        'the wrong shape, or whose analysis does not compile.',
        'Write each input of the step as a literal of the shape the message names, and fix the analysis it names '
        '(docs/advanced.md, Real-time Analysis).'),
    SophiosErrorCode.INVALID_INPUT_VALUE: Explanation(
        Kind.CALL,
        'A value bound to a step input that the input cannot take.',
        "Bind a value of the input's type; a File or Directory value must name a path that exists."),
    SophiosErrorCode.INVALID_STEP: Explanation(
        Kind.CALL,
        'A step the workflow cannot place: a repeated step name, a link to a step outside the workflow or later in '
        'it, a workflow input with no type, or two tools that share a file stem.',
        'Do what the message names: pass `step_name=` to a reused tool, list the source step earlier, or add it to '
        'this workflow.'),
    SophiosErrorCode.INVALID_LINK: Explanation(
        Kind.CALL,
        "A link from this workflow's own output, or between ports whose types do not match.",
        "Bind the input to an earlier step's output, or to a workflow input, of a matching type."),
    SophiosErrorCode.INVALID_TOOL: Explanation(
        Kind.CALL,
        'A CWL tool that could not be loaded or parsed.',
        'Point the step at a valid CWL CommandLineTool; the message says what is wrong with it.'),
    SophiosErrorCode.WORKFLOW_RUN_FAILED: Explanation(
        Kind.RUN,
        'A local run that finished with a non-zero exit code.',
        "Read the failed step's messages, printed before the error."),
    SophiosErrorCode.NO_PYTHON_SPELLING: Explanation(
        Kind.CALL,
        'A `.wic` construct the Python API has no spelling for, met by `Workflow.from_wic`.',
        'Write the workflow in the Python API in the way the message names, or keep it as a `.wic` file.'),
}
