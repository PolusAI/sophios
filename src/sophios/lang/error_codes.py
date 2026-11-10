"""The identifiers a Sophios failure carries.

Its own module because a code is contract: a caller matches on
`SophiosErrorCode.UNDEFINED_EDGE`, suppresses it, or reads it out of a log,
without importing the reporting plumbing.

Three ranges, one enum: `wic0NN` is a document (it said something the language
does not accept) or what running it needs from the machine or a site (kind
`machine`, such as wic015, wic016, wic021, wic029 and wic057); `api0NN` is the Python API (the document is
fine, the call was not); `nf-sNNN` is Nextflow support (kind `target`: the workflow is
fine, the Nextflow target cannot run it yet). One type so a caller matches on one
`except`; separate ranges so an API code does not point at the .wic reference. An
`nf-sNNN` code is retired, never reused, when the Nextflow target learns its construct,
so the range is the Nextflow target's support table.

Every member has an `Explanation` (`EXPLANATIONS`): what it means and what to
do, which `sophios --explain` and docs/error_codes.md show.
"""
# pylint: disable=too-many-lines  # the catalog: one entry per code
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
    #: A program the run calls that is not installed here, or cannot run on this platform.
    PROGRAM_MISSING = 'wic029'
    RECURSIVE_ALIAS = 'wic030'
    #: Two different URIs bound to one `$namespaces` prefix by a workflow and
    #: the tools it promotes ports from, when a promoted port's `format:` uses
    #: that prefix: the emitted document could keep only one of them.
    DUPLICATE_DOCUMENT_NAME = 'wic031'
    #: A scatter entry naming no input of its step, or one nothing binds once
    #: inference is done. On a subworkflow call the inputs are the ones the
    #: subworkflow declares.
    UNKNOWN_SCATTER_PORT = 'wic032'
    #: A key in a ``wic:`` block that the block does not have (.wic reference §7).
    UNKNOWN_WIC_KEY = 'wic033'
    #: A ``wic:`` key whose value does not have the shape the key declares (.wic reference §7).
    MALFORMED_WIC_VALUE = 'wic034'
    #: An authored `cwlVersion` the substrate toolchain does not run (language spec §1).
    UNSUPPORTED_CWL_VERSION = 'wic035'
    #: An authored workflow output with no `type` and no producer to take one from.
    UNTYPED_OUTPUT = 'wic036'
    #: An image the run needs that the engine could not pull, load or import before the run.
    IMAGE_UNAVAILABLE = 'wic037'
    #: CWL's WorkflowStepInput written where Sophios does not read it: an
    #: untagged mapping in `in:` carrying `source`, `default`, `linkMerge`,
    #: ..., a `!cwl {...}` record carrying a key or a `source` it may not, or
    #: `linkMerge`/`pickValue`/a list `outputSource` on a workflow output.
    STEP_INPUT_RECORD = 'wic038'

    #: A positional `(index, name)/port` outputSource whose index does not hold
    #: that step.
    POSITIONAL_OUTPUT_SOURCE = 'wic039'

    #: A note, not an error (an error under --inference_strict): inference
    #: guessed an edge, as another output also fitted the input.
    INFERENCE_GUESS = 'wic043'

    #: A real-time analysis declaration (a `cwl_subinterpreter` step) that is
    #: not well formed, or whose analysis does not compile.
    REALTIME_DECLARATION = 'wic044'

    #: A step id, workflow input or output, or workflow name spelled with a
    #: character outside `[A-Za-z0-9_.-]`, or starting with `.`
    #: (`sophios.lang.names`).
    INVALID_NAME = 'wic045'

    #: A note, not an error: under ``--target nextflow``, a ``cwltool:CUDARequirement``
    #: field the generated pipeline does not check, ``cudaVersionMin`` or
    #: ``cudaComputeCapability``.
    UNCHECKED_CUDA_FIELD = 'wic046'

    #: Importing a Nextflow pipeline (docs/nextflow.md): text in its three
    #: files that is not what Sophios's renderer writes, at its line.
    #: wic047: a `def` that is not a generated helper, byte for byte.
    IMPORT_EDITED_HELPER = 'wic047'
    #: wic048: a statement, directive or channel operator the renderer does not write.
    IMPORT_FOREIGN_STATEMENT = 'wic048'
    #: wic049: a process input or output declaration the renderer does not write.
    IMPORT_FOREIGN_DECLARATION = 'wic049'
    #: wic050: a process script or stub that is not the command or stub the renderer writes.
    IMPORT_FOREIGN_SCRIPT = 'wic050'
    #: wic051: a `nextflow_params.json` that does not hold the values of the parameters the pipeline checks.
    IMPORT_PARAMS = 'wic051'
    #: wic052: a `nextflow.config` other than the one the renderer writes for the pipeline.
    IMPORT_CONFIG = 'wic052'
    #: wic053: text in the renderer's forms that is not what it writes for the workflow the text describes.
    IMPORT_MISMATCH = 'wic053'
    #: wic054: a pipeline no workflow built through the Python API compiles to, as it is written.
    IMPORT_UNWRITABLE = 'wic054'

    #: Inference would wire two inputs of one tool step to the same output, and
    #: not every input declares a file format that output carries.
    INFERENCE_DUPLICATE = 'wic055'

    #: A note, not an error: a step's `when` that cannot give `true` or `false`, which cwltool stops the run at.
    #: Under ``--target nextflow`` it is refused instead.
    WHEN_NOT_BOOLEAN = 'wic056'

    #: --- A site (`sophios.sites`): what only a run on another computer has. ---
    #: A site that cannot be reached or used: ssh, the login profile there, a call there, or its platform.
    SITE_UNREACHABLE = 'wic057'
    #: The runner is one no site runs (cwltool), is not in the folder a site's `bin` names, does not run there, or is
    #: older than a site runs.
    SITE_RUNNER = 'wic058'
    #: A run id that names no run on the site.
    SITE_RUN_UNKNOWN = 'wic059'
    #: A Nextflow process with a container, on a site whose container engine is not Docker.
    SITE_NEXTFLOW_CONTAINER = 'wic060'
    #: Note: a step Toil gives 4 GiB on a site, its tool declaring no memory.
    SITE_DEFAULT_MEMORY = 'wic061'
    #: Slurm on a site cannot run the run: a command, a setting of yours there, a partition, or a GPU step.
    SITE_SLURM = 'wic062'

    #: --- Python API. The document is valid; the call was not. ---
    INVALID_INPUT_VALUE = 'api001'
    INVALID_STEP = 'api002'
    INVALID_LINK = 'api003'
    INVALID_TOOL = 'api004'
    #: A local run that did not complete: it failed, or was cancelled. The
    #: document and the call were fine; the run was not.
    WORKFLOW_RUN_FAILED = 'api005'
    #: A `.wic` construct the Python API has no spelling for, met by
    #: `Workflow.from_wic`. The document is valid and compiles; the API cannot
    #: hold it. The message names the construct and what to write instead.
    NO_PYTHON_SPELLING = 'api006'
    #: An `Inference` the compiler cannot be given: a format rule that is
    #: neither `continue` nor `break`, a renaming that is not a pair of
    #: strings, renamings with naming conventions off, or format rules with
    #: step insertion off.
    INVALID_INFERENCE = 'api007'
    #: A workflow that declares real-time analyses, given to
    #: `Workflow.run_async`, which never runs them.
    REALTIME_NOT_ASYNC = 'api008'

    #: --- Nextflow support. The workflow is valid; the Nextflow target cannot run it yet. ---
    NF_PARTIAL_FAILURE = 'nf-s001'
    NF_NAME = 'nf-s002'
    NF_SCATTERED_CALL = 'nf-s003'
    NF_SCATTER_OF_ARRAYS = 'nf-s004'
    NF_SCATTERED_STEP_ONLY_INPUT = 'nf-s005'
    NF_SCATTER_SOURCE = 'nf-s006'
    NF_NESTED_RESULT = 'nf-s007'
    NF_SCATTER_METHOD_ALONE = 'nf-s008'
    NF_CONDITIONAL_CALL = 'nf-s009'
    NF_WHEN_SUBSET = 'nf-s010'
    NF_WHEN_UNBOUND = 'nf-s011'
    NF_WHEN_WITHOUT_INPUTS = 'nf-s012'
    NF_SKIPPED_OUTPUT = 'nf-s013'
    NF_INPUT_WITHOUT_VALUE = 'nf-s014'
    NF_INPUT_VALUE = 'nf-s015'
    NF_CALL_INPUT = 'nf-s016'
    NF_DEFAULT = 'nf-s017'
    NF_JOIN = 'nf-s018'
    NF_STEP_INPUT_VALUE_FROM = 'nf-s019'
    NF_ABSENT_OPTIONAL = 'nf-s020'
    NF_WORKFLOW_INPUT_BINDING = 'nf-s021'
    NF_LOAD_CONTENTS = 'nf-s022'
    NF_LOAD_LISTING = 'nf-s023'
    NF_SECONDARY_FILES = 'nf-s024'
    NF_FORWARDED_INPUT = 'nf-s025'
    NF_OUTPUT_WITHOUT_SOURCE = 'nf-s026'
    NF_SOURCE_AS_TEXT = 'nf-s027'
    NF_SOURCE_NAMES_NO_STEP = 'nf-s028'
    NF_CAPTURED_TEXT_READ = 'nf-s029'
    NF_OPTIONAL_OUTPUT = 'nf-s030'
    NF_OUTPUT_TYPE = 'nf-s031'
    NF_GLOB = 'nf-s032'
    NF_OUTPUT_EVAL = 'nf-s033'
    NF_CAPTURE = 'nf-s034'
    NF_WRITE_THROUGH_STAGED = 'nf-s035'
    NF_REQUIREMENT = 'nf-s036'
    NF_NETWORK_ACCESS = 'nf-s037'
    NF_SHELL_IN_CONTAINER = 'nf-s038'
    NF_DOCKER = 'nf-s039'
    NF_RESOURCE = 'nf-s040'
    NF_ENVIRONMENT = 'nf-s041'
    NF_EXIT_CODES = 'nf-s042'
    NF_STAGING = 'nf-s043'
    NF_STAGED_NAMES = 'nf-s044'
    NF_EXPRESSION = 'nf-s045'
    NF_PATH_REFERENCE = 'nf-s046'
    NF_NOT_A_COMMAND_LINE_TOOL = 'nf-s047'
    NF_TYPE = 'nf-s048'
    NF_BOOLEAN_BINDING = 'nf-s049'
    NF_ARRAY_BINDING = 'nf-s050'
    NF_SHELL_QUOTE = 'nf-s051'
    NF_EMPTY_PREFIX = 'nf-s052'
    NF_EMPTY_COMMAND = 'nf-s053'
    NF_FIELD = 'nf-s054'
    NF_INVALID_CWL = 'nf-s055'

    @property
    def is_language(self) -> bool:
        """Whether this code describes a document rather than a call: every `wic0NN` code, and an `nf-sNNN` code of
        kind `document`, a mistake in the workflow the Nextflow target is the one to read."""
        return self.value.startswith('wic') or EXPLANATIONS[self].kind is Kind.DOCUMENT

    @property
    def explanation(self) -> 'Explanation':
        """What this code means and what to do about it."""
        return EXPLANATIONS[self]


class Kind(StrEnum):
    """What a reader changes when a code is reported."""

    DOCUMENT = 'document'  #: the .wic file, the Python workflow, or an imported Nextflow pipeline
    CALL = 'call'          #: the Python API call
    MACHINE = 'machine'    #: this machine or a site: what is installed, running, readable or writable
    RUN = 'run'            #: nothing in Sophios: a tool failed while it ran
    TARGET = 'target'      #: nothing in the workflow: the target cannot run it yet


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
        'A scalar was expected here, such as a mapping key that is itself a list or carries a Sophios tag.',
        'Write one name, number or string at the position shown; a tag goes on the value, as in `a: !ii b`.'),
    SophiosErrorCode.MISSING_STEP_ID: Explanation(
        Kind.DOCUMENT,
        'A step in a sequence has no `id:`.',
        'Add `id:` with the tool or workflow the step runs, or write `steps:` as a mapping keyed by step (.wic '
        'reference §2.1).'),
    SophiosErrorCode.EMPTY_STEP_ID: Explanation(
        Kind.DOCUMENT,
        "A step's `id:` is empty.",
        'Write the name of the tool or workflow the step runs.'),
    SophiosErrorCode.MALFORMED_WIC_STEP_KEY: Explanation(
        Kind.DOCUMENT,
        'A `wic: steps:` key is neither `(index, name)` nor a step id.',
        "Key the entry `(index, name)` with the step's 1-based position and id, or by the id alone when no other "
        "step has it (.wic reference §7)."),
    SophiosErrorCode.UNKNOWN_TAG: Explanation(
        Kind.DOCUMENT,
        'An unknown YAML tag; the Sophios tags are `!ii`, `!&`, `!*` and `!cwl`.',
        'Use one of the four Sophios tags, or remove the tag.'),
    SophiosErrorCode.DUPLICATE_KEY: Explanation(
        Kind.DOCUMENT,
        'A key bound twice: an input, a step, a `wic:` key, or a second `id:`.',
        'Keep one of the two and delete the other (.wic reference §3.8).'),
    SophiosErrorCode.UNRESOLVED_INPUT: Explanation(
        Kind.DOCUMENT,
        'An untagged input value names no workflow input.',
        'Write a literal as `!ii <value>`, or declare the name under `inputs:` (.wic reference §3.3).'),
    SophiosErrorCode.MISSING_REQUIRED_INPUT: Explanation(
        Kind.DOCUMENT,
        'A required input gets no value, such as `!ii null` on an input that is not optional.',
        "Bind the input to a value, an edge or a workflow input, or make the tool's input optional."),
    SophiosErrorCode.SUBWORKFLOW_INVALID: Explanation(
        Kind.DOCUMENT,
        "A workflow or a step's process cannot be used: no steps, a tool or subworkflow not found, a CWL `Workflow` "
        "as a step, a packed `$graph` with no `main`, subworkflows that call each other in a cycle, "
        "`implementations` with none chosen, ports only the CWL runner can read, or a tool's relative files "
        "(`$include`, `$import`, a relative `location`) that cwltool cannot read.",
        'Do what the message names: put the tool or `.wic` file on a search path of the config (`search_paths_cwl`, '
        '`search_paths_wic`), give the workflow steps, or break the cycle (.wic reference §2.3).'),
    SophiosErrorCode.SCRIPT_ARGUMENT_MISMATCH: Explanation(
        Kind.DOCUMENT,
        'The arguments given to a Python script step do not match its declared inputs.',
        'Bind exactly the inputs the script declares.'),
    SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE: Explanation(
        Kind.MACHINE,
        'The container engine cannot be used: not installed, not running, not yours to use, or overloaded. '
        'Checked only when a step runs in a container.',
        'Do what the message says: install the engine, start it, or give your user access to it; '
        'under `--target cwl`, `--container_engine` names another engine, and `--ignore_docker_install` skips the '
        'check; on a site, `container_engine=` names another.'),
    SophiosErrorCode.MISSING_INPUT_FILE: Explanation(
        Kind.MACHINE,
        'An input file or directory cannot be used: it does not exist, it is the other kind, or you may not read it; '
        'or, on a site, its path holds `..`, `~` or `$`.',
        'Correct the path, create it, or make it readable; the message says where Sophios looked. On a site, copy your '
        'data there first, and write the path relative to your project folder.'),
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
        "Name the output with `!&` on the producing step's `out:` entry, and read it with `!*` (.wic reference "
        "§3.6)."),
    SophiosErrorCode.LITERAL_TYPE_MISMATCH: Explanation(
        Kind.DOCUMENT,
        "A literal does not have the type of its input, or a scattered input's literal is not a list.",
        "Write a value of the input's type, quoting a string YAML would read as something else, and give a scattered"
        " input a list (.wic reference §3.2, §6.1)."),
    SophiosErrorCode.DIRECTORY_NOT_WRITABLE: Explanation(
        Kind.MACHINE,
        'A directory Sophios writes into cannot be created or written: the base path (`autogenerated/` under '
        'the working directory unless `--basepath` or `Workflow.run(basepath=)` names another), `--outdir`, '
        "or an `outdir` given to `Workflow.run()`; on a site, `<root>/sophios`, or an `outdir=` that exists already.",
        'Name a directory you can write to with `--basepath` and `--outdir`; from Python, '
        '`run(basepath=...)` and `run_args_dict={"outdir": ...}` take another; on a site, `root=` and a new '
        '`outdir=`.'),
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
        'Correct the spelling to a construct of .wic reference §3.7, or rename the key.'),
    SophiosErrorCode.UNDEFINED_EDGE: Explanation(
        Kind.DOCUMENT,
        'An edge reference with no definition before it, anywhere in the compilation.',
        "Define the name with `!&` on an earlier step's `out:` entry, or correct its spelling (.wic reference §4)."),
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
    SophiosErrorCode.PROGRAM_MISSING: Explanation(
        Kind.MACHINE,
        'A program the run calls is not installed, or cannot run on this platform.',
        'Install the program the message names, as it says; on Windows, run Sophios inside WSL.'),
    SophiosErrorCode.RECURSIVE_ALIAS: Explanation(
        Kind.DOCUMENT,
        'A YAML alias that contains itself.',
        'Remove the alias that refers back to its own anchor.'),
    SophiosErrorCode.DUPLICATE_DOCUMENT_NAME: Explanation(
        Kind.DOCUMENT,
        'A promoted port whose `format:` uses a `$namespaces` prefix that two of its sources bind to different '
        'URIs.',
        'Bind the prefix to the same URI in both, or use another prefix in one of them (.wic reference §5.2).'),
    SophiosErrorCode.UNKNOWN_SCATTER_PORT: Explanation(
        Kind.DOCUMENT,
        'A `scatter:` entry that is not an input of its step.',
        'Scatter over an input the step declares and binds (.wic reference §6.1).'),
    SophiosErrorCode.UNKNOWN_WIC_KEY: Explanation(
        Kind.DOCUMENT,
        'A key the `wic:` block does not have.',
        'Use a key from .wic reference §7, or remove it.'),
    SophiosErrorCode.MALFORMED_WIC_VALUE: Explanation(
        Kind.DOCUMENT,
        'A `wic:` value of the wrong shape.',
        'Write the value in the shape .wic reference §7 gives for its key.'),
    SophiosErrorCode.UNSUPPORTED_CWL_VERSION: Explanation(
        Kind.DOCUMENT,
        'A `cwlVersion` other than `v1.0`, `v1.1` or `v1.2`.',
        'Declare `cwlVersion: v1.2`.'),
    SophiosErrorCode.UNTYPED_OUTPUT: Explanation(
        Kind.DOCUMENT,
        'A workflow output with no `type:` and no producer to take it from.',
        'Give the output a `type:`, or an `outputSource:` naming the step output it comes from (.wic reference '
        '§5.2).'),
    SophiosErrorCode.IMAGE_UNAVAILABLE: Explanation(
        Kind.MACHINE,
        'An image the run needs could not be pulled, loaded or imported before the run; the message quotes the '
        "engine's last line.",
        'Check the image name or source, the network and your registry login, then run again.'),
    SophiosErrorCode.STEP_INPUT_RECORD: Explanation(
        Kind.DOCUMENT,
        "CWL's step input written where Sophios does not read it: an untagged mapping of step-input fields, a "
        "malformed `!cwl {...}` record, or a list `outputSource`, `linkMerge` or `pickValue` on a workflow output.",
        'Write step-input fields as a `!cwl {...}` record (.wic reference §3.5); a workflow output names one step '
        'output.'),
    SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE: Explanation(
        Kind.DOCUMENT,
        'A positional `(index, name)` `outputSource` that names the wrong step.',
        'Use the index and id of the step the output comes from, or write `step/port` (.wic reference §5.2).'),
    SophiosErrorCode.INFERENCE_GUESS: Explanation(
        Kind.DOCUMENT,
        'Note: inference guessed an edge, as another output also fitted the input.',
        'Draw the workflow and check the guessed edges, with `--graphviz` or `Workflow.draw()`; write the ones that '
        'are wrong with `!&` on the output and `!*` on the input (.wic reference §8). `--inference_strict` makes '
        'each guess an error.'),
    SophiosErrorCode.REALTIME_DECLARATION: Explanation(
        Kind.DOCUMENT,
        'A real-time analysis declaration (a `cwl_subinterpreter` step) with an input that is not a literal or has '
        'the wrong shape, or whose analysis does not compile.',
        'Write each input of the step as a literal of the shape the message names, and fix the analysis it names '
        '(docs/realtime-analysis.md).'),
    SophiosErrorCode.INVALID_NAME: Explanation(
        Kind.DOCUMENT,
        'A step id, workflow input or output, or workflow name with a character other than letters, digits, '
        '`_`, `.` and `-`, or starting with `.`.',
        'Rename it with letters, digits, `_`, `.` and `-`: the name becomes part of file names and of the names '
        'Sophios makes with `~` and `@`.'),
    SophiosErrorCode.UNCHECKED_CUDA_FIELD: Explanation(
        Kind.DOCUMENT,
        'Note: under `--target nextflow`, a `cwltool:CUDARequirement` names a `cudaVersionMin` or '
        '`cudaComputeCapability`, which the generated pipeline does not check; it asks only for a number of GPUs.',
        'Run the pipeline where the GPUs and the container image provide that CUDA version and compute '
        'capability, or remove the field.'),
    SophiosErrorCode.IMPORT_EDITED_HELPER: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a `def` in `workflow.nf` that is not a helper Sophios generates, byte for byte.',
        'Restore the helper as Sophios wrote it, or remove the `def`: the import reads only what Sophios writes.'),
    SophiosErrorCode.IMPORT_FOREIGN_STATEMENT: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a statement, directive or channel operator Sophios does not write, such as an '
        '`include`, an `errorStrategy` directive or a `.view()` call.',
        'Remove the construct the message names: the import reads only the pipelines Sophios writes '
        '(docs/nextflow.md).'),
    SophiosErrorCode.IMPORT_FOREIGN_DECLARATION: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a process input or output declaration Sophios does not write, such as '
        '`tuple val(id), path(reads)`.',
        'Write the process as a Sophios tool and compile it to Nextflow; the import reads only what Sophios writes.'),
    SophiosErrorCode.IMPORT_FOREIGN_SCRIPT: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a process script or stub that is not the command or stub Sophios writes, such as a '
        'hand-written shell script.',
        'Write the command as a Sophios tool and compile it to Nextflow; the import reads only what Sophios writes.'),
    SophiosErrorCode.IMPORT_PARAMS: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a `nextflow_params.json` that does not hold the values of the parameters the pipeline '
        'checks, as Sophios writes them.',
        'Give each parameter the entry workflow checks one value, and no other key, in the form Sophios writes.'),
    SophiosErrorCode.IMPORT_CONFIG: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a `nextflow.config` other than the one Sophios writes for the pipeline.',
        'Restore the line Sophios wrote: `docker.enabled` is true exactly when a process has a container.'),
    SophiosErrorCode.IMPORT_MISMATCH: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: text in the forms Sophios writes that is not what it writes for the workflow the '
        'text describes, such as a derived form that disagrees with the rest.',
        'Restore the line the message names as Sophios wrote it, or regenerate the pipeline from its workflow.'),
    SophiosErrorCode.IMPORT_UNWRITABLE: Explanation(
        Kind.DOCUMENT,
        'Importing Nextflow: a pipeline no workflow built through the Python API compiles to as it is written, '
        'such as one with a capitalized process name, which the compile would give a leading `_`, or with a port '
        'of a CWL tool named like an attribute of the tool builder\'s `Inputs` or `Outputs`, such as `keys`.',
        'Rename what the message names in the workflow the pipeline was compiled from, to a name the compile keeps, '
        'and compile it again.'),
    SophiosErrorCode.INFERENCE_DUPLICATE: Explanation(
        Kind.DOCUMENT,
        'Inference would give two inputs of one step the same output, and nothing says that is meant: the inputs '
        'are not all written and do not all declare a file format that output carries.',
        'Write the edge each input should read, with `!&` on the output and `!*` on the input (.wic reference §8); '
        'two written edges to one output are always accepted.'),
    SophiosErrorCode.WHEN_NOT_BOOLEAN: Explanation(
        Kind.DOCUMENT,
        "Note: a step's `when` cannot give `true` or `false`: it computes a number, a string or `null`, or it is "
        'not one `$(...)` or `${...}` expression but text, which CWL refuses or evaluates to text. cwltool stops '
        'the run there, and `--target nextflow` refuses it.',
        'Write the `when` as one expression that gives a boolean: `$(inputs.count > 0)` rather than '
        '`$(inputs.count)`, and `$(inputs.ready)` rather than `inputs.ready`.'),
    SophiosErrorCode.SITE_UNREACHABLE: Explanation(
        Kind.MACHINE,
        'A site that cannot be reached or used: ssh failed, the login profile there read the script Sophios sent on '
        'its input, a command Sophios ran there failed, the site is neither macOS nor Linux, or Sophios runs on '
        'Windows itself.',
        'Run `ssh <host>` in a terminal and fix what it says: a key, a `Host` in `~/.ssh/config`, or a second factor '
        'through a shared connection opened by hand. Make a login profile read no input; run Sophios inside WSL on '
        'Windows. The message quotes what ssh or the site said.'),
    SophiosErrorCode.SITE_RUNNER: Explanation(
        Kind.MACHINE,
        'The runner is cwltool, which no site runs, or it is not in the folder `bin=` names on a site, does not run '
        'there, or is older than the version a site runs, which the message names.',
        "Leave `runner=` out for a site's default, Toil for `target='cwl'`; or install the runner in that folder "
        'there, with the Java Nextflow runs on, or name the folder that holds it with `bin=`.'),
    SophiosErrorCode.SITE_RUN_UNKNOWN: Explanation(
        Kind.MACHINE,
        'A run id that names no run on the site: there is no `<root>/sophios/runs/<id>/run.json`.',
        'Check the id, the host and `root=`: the line `submit()` printed, `repr(run)`, attaches to the run.'),
    SophiosErrorCode.SITE_NEXTFLOW_CONTAINER: Explanation(
        Kind.MACHINE,
        "A Nextflow process with a container, on a site whose `container_engine` is not `docker`: the Nextflow target "
        'runs containers with Docker only.',
        "Run it on a site with Docker, `container_engine='docker'`, or with `target='cwl'`."),
    SophiosErrorCode.SITE_DEFAULT_MEMORY: Explanation(
        Kind.MACHINE,
        'Note: a step Toil runs on a site whose tool declares no memory. Toil gives it 4 GiB there '
        '(`--defaultMemory 4Gi`), and refuses to start it on a computer with less.',
        'Declare the memory the tool needs, `ramMin` in its `ResourceRequirement` '
        '(`CommandLineTool.resources(ram_min=...)` in Python); a site with 4 GiB or more runs it as it is.'),
    SophiosErrorCode.SITE_SLURM: Explanation(
        Kind.MACHINE,
        'Slurm on a site cannot run the run: one of its commands (`sbatch`, `squeue`, `sacct`, `scancel`, `sinfo`) is '
        'missing or does not answer, `sacct` included; your login profile there sends jobs to another cluster '
        '(`SBATCH_CLUSTERS`, `SLURM_CLUSTERS`) or sets an `SBATCH_` value a job script cannot carry; a partition the '
        'run needs is not there; or a step asks for a GPU and no one partition has GPUs.',
        'Do what the message names: load the module that provides Slurm with `modules=`, ask the administrators to '
        'turn on accounting, correct your login profile there, or name the partition with `partition=` or '
        '`gpu_partition=`.'),
    SophiosErrorCode.INVALID_INPUT_VALUE: Explanation(
        Kind.CALL,
        'A value bound to a step input that the input cannot take.',
        "Bind a port object directly, or several with `StepInput(source=[...])`; a string never names a port."),
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
        'A local run that did not complete: it failed, or was cancelled.',
        "Read the lines printed before the error: the failed step's messages, or that the run was stopped."),
    SophiosErrorCode.NO_PYTHON_SPELLING: Explanation(
        Kind.CALL,
        'A `.wic` construct the Python API has no spelling for, met by `Workflow.from_wic`.',
        'Write the workflow in the Python API in the way the message names, or keep it as a `.wic` file.'),
    SophiosErrorCode.INVALID_INFERENCE: Explanation(
        Kind.CALL,
        'An `Inference` the compiler cannot be given: a format rule other than `continue` or `break`, '
        'a renaming that is not a pair of strings, renamings with naming conventions off, '
        'or format rules with step insertion off.',
        'Do what the message names: use `continue` or `break`, write each renaming as `(before, after)`, '
        'set `naming_conventions=True` for renamings, or set `insert_steps=True` for format rules.'),
    SophiosErrorCode.REALTIME_NOT_ASYNC: Explanation(
        Kind.CALL,
        'A workflow that declares real-time analyses, given to `Workflow.run_async` or to a site '
        '(`sophios.sites`), neither of which runs them.',
        'Run it with `Workflow.run()` or `Workflow.start()`; from a coroutine, '
        '`await asyncio.to_thread(workflow.run)` keeps the event loop free.'),
    SophiosErrorCode.NF_PARTIAL_FAILURE: Explanation(
        Kind.TARGET,
        'Partial failure (`--partial_failure_enable`, `RunPolicy(partial_failure=True)`), a policy of the CWL '
        'target.',
        'Compile without the policy, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_NAME: Explanation(
        Kind.TARGET,
        'Two names that become one Nextflow identifier, or a name with none: steps, tool inputs or outputs, '
        'workflow inputs or outputs, or a parameter Sophios makes for a default.',
        'Rename one of the names the message lists.'),
    SophiosErrorCode.NF_SCATTERED_CALL: Explanation(
        Kind.TARGET,
        'A `scatter` on a step that calls a nested workflow.',
        'Scatter the tool steps inside the nested workflow, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_SCATTER_OF_ARRAYS: Explanation(
        Kind.TARGET,
        'A scatter over an input whose elements are arrays.',
        'Scatter over an array of values, Files or Directories, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_SCATTERED_STEP_ONLY_INPUT: Explanation(
        Kind.TARGET,
        'A scatter over a step-only input, which no process port receives.',
        "Scatter over an input the step's tool declares."),
    SophiosErrorCode.NF_SCATTER_SOURCE: Explanation(
        Kind.TARGET,
        'A scattered input fed by anything but one source as it is: several sources, `linkMerge`, `pickValue` or '
        '`default`, or a workflow input that is not an array.',
        "Feed the scattered input from one array, a workflow input or an earlier step's output."),
    SophiosErrorCode.NF_NESTED_RESULT: Explanation(
        Kind.TARGET,
        'A `nested_crossproduct` result read by a later step.',
        'Read it from a workflow output only, or scatter with `dotproduct` or `flat_crossproduct`.'),
    SophiosErrorCode.NF_SCATTER_METHOD_ALONE: Explanation(
        Kind.TARGET,
        'A `scatterMethod` on a step with no `scatter`, which cwltool ignores.',
        'Remove the `scatterMethod`, or give the step its `scatter`.'),
    SophiosErrorCode.NF_CONDITIONAL_CALL: Explanation(
        Kind.TARGET,
        'A `when` on a step that calls a nested workflow.',
        'Put the `when` on the tool steps inside the nested workflow.'),
    SophiosErrorCode.NF_WHEN_SUBSET: Explanation(
        Kind.TARGET,
        'A `when` outside the safe JavaScript subset: a `${...}` body, an operator or function the subset does '
        'not have, or an ordered comparison of anything but numbers.',
        'Write the condition in the subset (docs/nextflow.md, Conditional steps), or compile with `--target cwl`.'),
    SophiosErrorCode.NF_WHEN_UNBOUND: Explanation(
        Kind.TARGET,
        'A `when` that reads an input its step does not bind, which cwltool reads as `undefined`.',
        'Bind the input on the step, as a step-only input if its tool does not take it.'),
    SophiosErrorCode.NF_WHEN_WITHOUT_INPUTS: Explanation(
        Kind.TARGET,
        "A `when` on a step whose tool has no inputs: Nextflow decides a condition each time the step's inputs "
        'arrive.',
        'Give the step an input to decide on, such as a step-only input from a workflow input.'),
    SophiosErrorCode.NF_SKIPPED_OUTPUT: Explanation(
        Kind.TARGET,
        'The output of a conditional step read where the value a skipped step gives, `[]` in Nextflow, cannot '
        'stand for `null`: an array output outside a scatter, or an array, File, Directory or required input that'
        ' reads it.',
        'Read it into an optional value input or a workflow output, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_INPUT_WITHOUT_VALUE: Explanation(
        Kind.TARGET,
        "A required workflow input with no value: the pipeline's parameters are written when it compiles.",
        'Give the input a value: `!ii` in the `.wic` file, or `flow.inputs.<name> = ...` in Python.'),
    SophiosErrorCode.NF_INPUT_VALUE: Explanation(
        Kind.TARGET,
        "A workflow input's value the pipeline's parameters cannot hold, such as a File or Directory with "
        '`secondaryFiles`.',
        "Give a plain value of the input's type."),
    SophiosErrorCode.NF_CALL_INPUT: Explanation(
        Kind.TARGET,
        "A nested workflow's input bound by anything but one source as it is (several sources, `linkMerge`, "
        '`pickValue`, `default`, `valueFrom`), or with a `default` of its own.',
        'Merge, pick or default on a step inside the nested workflow, or bind the input on the calling step.'),
    SophiosErrorCode.NF_DEFAULT: Explanation(
        Kind.TARGET,
        "A `default` the pipeline cannot hold: one that is not a JSON scalar of its input's type, a File or "
        'Directory on a step input, or one on a step-only input with no source of a scalar type.',
        "Give a scalar default of the input's type, or the path as a workflow input."),
    SophiosErrorCode.NF_JOIN: Explanation(
        Kind.TARGET,
        'A step input that joins its sources in a way the target does not form: `merge_nested` over an array, '
        '`pickValue` over an array or over one source, a `null` element, a `default` beside them, or a join that '
        'gives a list to an input that takes one value, or the reverse.',
        'Join the values in a tool step, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_STEP_INPUT_VALUE_FROM: Explanation(
        Kind.TARGET,
        '`valueFrom` on a step input.',
        'Compute the value in a tool step, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_ABSENT_OPTIONAL: Explanation(
        Kind.TARGET,
        'An optional input with no value whose absence changes the command, or an absent optional File or '
        'Directory workflow input.',
        'Give the input a value, or bind it so that its absence leaves the command as it is (docs/nextflow.md, '
        'Absent optional values).'),
    SophiosErrorCode.NF_WORKFLOW_INPUT_BINDING: Explanation(
        Kind.TARGET,
        '`inputBinding` on a workflow input.',
        'Remove it: the steps that read the input bind it.'),
    SophiosErrorCode.NF_LOAD_CONTENTS: Explanation(
        Kind.TARGET,
        '`loadContents` on an input, a binding, a step input or a workflow input.',
        "Read the file in the tool's command."),
    SophiosErrorCode.NF_LOAD_LISTING: Explanation(
        Kind.TARGET,
        '`loadListing`, or `LoadListingRequirement`: a Directory is staged whole.',
        'Remove it.'),
    SophiosErrorCode.NF_SECONDARY_FILES: Explanation(
        Kind.TARGET,
        '`secondaryFiles`.',
        'Stage each file as its own input.'),
    SophiosErrorCode.NF_FORWARDED_INPUT: Explanation(
        Kind.TARGET,
        'A workflow output that forwards a workflow input.',
        "Output a step's output."),
    SophiosErrorCode.NF_OUTPUT_WITHOUT_SOURCE: Explanation(
        Kind.TARGET,
        'A workflow output with no `outputSource`.',
        'Name the step output it comes from.'),
    SophiosErrorCode.NF_SOURCE_AS_TEXT: Explanation(
        Kind.TARGET,
        "A source written as `step/port` text that names a nested workflow's step, which Sophios keeps as written"
        ' and the target does not follow.',
        "Read the nested workflow's output through an edge (`!&` and `!*`), or compile with `--target cwl`."),
    SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP: Explanation(
        Kind.DOCUMENT,
        'A source written as `step/port` text whose step is no step of its workflow, or whose port is no output of'
        ' that step.',
        'Name a step of the workflow, as the compiled CWL spells it, or read the output through an edge (`!&` and'
        ' `!*`).'),
    SophiosErrorCode.NF_CAPTURED_TEXT_READ: Explanation(
        Kind.TARGET,
        'File text captured by `outputEval: $(self[0].contents)` read by another step.',
        'Read the captured text from a workflow output only, or pass the File to the next step.'),
    SophiosErrorCode.NF_OPTIONAL_OUTPUT: Explanation(
        Kind.TARGET,
        'An optional File or Directory output read by another step, or one whose glob may match more than one '
        'file and reaches no workflow output.',
        'Make the output required, or read it from a workflow output only.'),
    SophiosErrorCode.NF_OUTPUT_TYPE: Explanation(
        Kind.TARGET,
        'A tool output that is not one File or Directory found by its glob, or one string captured as text: an '
        'array output among them.',
        'Collect one File or Directory, or capture a `string` with `loadContents` and `outputEval: '
        '$(self[0].contents)`.'),
    SophiosErrorCode.NF_GLOB: Explanation(
        Kind.TARGET,
        'An output whose glob is not one non-empty string: a list of globs, an empty glob, or none.',
        'Write one glob.'),
    SophiosErrorCode.NF_OUTPUT_EVAL: Explanation(
        Kind.TARGET,
        'An `outputEval` other than `$(self[0])` and `$(self[0].contents)`.',
        'Write one of those two, or compute the value in a later tool step.'),
    SophiosErrorCode.NF_CAPTURE: Explanation(
        Kind.TARGET,
        '`$(self[0])` or `$(self[0].contents)` without what makes it a projection: `loadContents: true` beside '
        'the capture, a `string` output for captured text, one File or Directory output and one literal glob with'
        ' no wildcard or input reference for `$(self[0])`.',
        'Add what the message names.'),
    SophiosErrorCode.NF_WRITE_THROUGH_STAGED: Explanation(
        Kind.TARGET,
        'An output glob, `stdout` or `stderr` that writes through the name a staged input is linked under.',
        'Derive a new name from the input, or make it a writable InitialWorkDirRequirement entry.'),
    SophiosErrorCode.NF_REQUIREMENT: Explanation(
        Kind.TARGET,
        'A requirement or hint class the target does not lower, such as `SoftwareRequirement` or `ToolTimeLimit`,'
        ' or a workflow feature class written on a tool.',
        'Use `DockerRequirement` for software, declare a feature on the workflow, or remove the class.'),
    SophiosErrorCode.NF_NETWORK_ACCESS: Explanation(
        Kind.TARGET,
        '`NetworkAccess` other than the literal `true`: a Nextflow task has network access.',
        'Remove it, or write `networkAccess: true`.'),
    SophiosErrorCode.NF_SHELL_IN_CONTAINER: Explanation(
        Kind.TARGET,
        '`ShellCommandRequirement` beside `DockerRequirement`: CWL runs the command as `/bin/sh -c` after the '
        "image's entrypoint, which a Nextflow task does not.",
        'Drop one of the two, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_DOCKER: Explanation(
        Kind.TARGET,
        'A `DockerRequirement` with `dockerFile`, `dockerImport`, `dockerLoad` or `dockerOutputDirectory`.',
        'Use `dockerPull` or `dockerImageId`.'),
    SophiosErrorCode.NF_RESOURCE: Explanation(
        Kind.TARGET,
        'A resource request the target cannot make: `outdirMin`, `outdirMax`, `tmpdirMin`, `tmpdirMax`, '
        'fractional cores, or a value computed by an expression, a CUDA device count among them.',
        'Request literal whole cores and literal memory and devices, or remove the field.'),
    SophiosErrorCode.NF_ENVIRONMENT: Explanation(
        Kind.TARGET,
        'An environment variable the task cannot set: a name that is not a shell variable name, or an `envValue` '
        'other than literal text or exactly `$(inputs.<name>)` of a string input.',
        'Name the variable with letters, digits and `_`, and set it to text or a string input.'),
    SophiosErrorCode.NF_EXIT_CODES: Explanation(
        Kind.TARGET,
        '`successCodes`, `permanentFailCodes` or `temporaryFailCodes`: a nonzero exit fails a Nextflow task.',
        'Make the command exit 0 on success, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_STAGING: Explanation(
        Kind.TARGET,
        'An InitialWorkDirRequirement listing entry other than a bare `$(inputs.<name>)` or a Dirent of one File '
        'or Directory input: inline content, a listing that is not a list, a computed `entryname` or `writable`, '
        "an array or value input, an undeclared input, or an `entryname` with a path separator or Sophios's "
        'reserved prefix.',
        'Stage each File or Directory input as its own entry, under a literal name or its own basename '
        '(docs/nextflow.md, Staging with InitialWorkDirRequirement).'),
    SophiosErrorCode.NF_STAGED_NAMES: Explanation(
        Kind.TARGET,
        'Staged names that collide: an input listed twice, two inputs under one literal name, or an input staged '
        'under a literal name and also referenced in the command.',
        'Give each staged input one entry and one name, and name it by its literal in the command.'),
    SophiosErrorCode.NF_EXPRESSION: Explanation(
        Kind.TARGET,
        'A CWL expression the target does not lower: anything but `$(inputs.<name>)`, and on a File or Directory '
        'input `$(inputs.<name>.path)` and `$(inputs.<name>.basename)`; `expressionLib` among them.',
        'Write one of those references, or compute the value in a tool step.'),
    SophiosErrorCode.NF_PATH_REFERENCE: Explanation(
        Kind.DOCUMENT,
        'A File or Directory reference whose CWL value is not the text it stands for: `.path` or `.basename` of '
        'an input that is not a File or Directory, a File object inside other text (cwltool writes it as JSON), '
        'an object where CWL takes a name, an absolute path where it takes a relative one, or a `stdin` that is '
        "not one file's whole path.",
        'Write the reference the message names.'),
    SophiosErrorCode.NF_NOT_A_COMMAND_LINE_TOOL: Explanation(
        Kind.TARGET,
        'A step that runs something other than a `CommandLineTool`, such as an `ExpressionTool`.',
        'Write the step as a CommandLineTool, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_TYPE: Explanation(
        Kind.TARGET,
        'A type the target does not lower: `long`, `double`, `enum`, a record, `Any`, a union other than one type'
        ' and `null`, nested arrays, or an array schema with fields beyond `type` and `items`.',
        'Declare `int`, `float`, `string`, `boolean`, `File`, `Directory`, or one array of them.'),
    SophiosErrorCode.NF_BOOLEAN_BINDING: Explanation(
        Kind.TARGET,
        "A boolean binding the target cannot render: a boolean array's binding, a `valueFrom` other than the "
        "input's own value, or a `valueFrom` on an optional boolean.",
        'Bind the boolean as a flag with a `prefix` (docs/nextflow.md, Boolean flags).'),
    SophiosErrorCode.NF_ARRAY_BINDING: Explanation(
        Kind.TARGET,
        'An array binding the target cannot render: `valueFrom`, `separate: false`, `itemSeparator`, or a binding'
        ' on each item; or an array input read outside its own binding, as in a glob.',
        'Bind the array as a whole, each element its own word (docs/nextflow.md, Array-typed inputs).'),
    SophiosErrorCode.NF_SHELL_QUOTE: Explanation(
        Kind.TARGET,
        '`shellQuote: false` on anything but literal text with no input reference, `prefix` or `$`, beside '
        '`ShellCommandRequirement`.',
        'Write the unquoted text as a literal `valueFrom` (docs/nextflow.md, Shell mode).'),
    SophiosErrorCode.NF_EMPTY_PREFIX: Explanation(
        Kind.TARGET,
        'A binding with an empty or blank `prefix`, which cwltool skips.',
        'Remove the `prefix`, or write one.'),
    SophiosErrorCode.NF_EMPTY_COMMAND: Explanation(
        Kind.TARGET,
        "A containerized tool whose command can render empty: CWL then runs the image's `CMD`, which the Nextflow"
        ' task does not.',
        'Give the tool a `baseCommand`, or an argument that is always there.'),
    SophiosErrorCode.NF_FIELD: Explanation(
        Kind.TARGET,
        'A field the target does not lower and the field table does not list.',
        'Remove the field, or compile with `--target cwl`.'),
    SophiosErrorCode.NF_INVALID_CWL: Explanation(
        Kind.DOCUMENT,
        'CWL v1.2 that cwltool would not run either: a multi-input scatter with no `scatterMethod`; a '
        '`scatterMethod`, `linkMerge` or `pickValue` CWL does not have; a requirement without a string `class`; a'
        ' section, binding, `envDef`, `outputBinding`, glob or `outputEval` of the wrong shape; a `prefix` that '
        'is not text; `separate` without `prefix`; a `position` that is not an integer; a port with no `type`; a '
        '`when` that is not a string; a resource or CUDA field that is not a positive number.',
        'Correct what the message names; `cwltool --validate` on the tool reports the same.'),
}
