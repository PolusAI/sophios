"""The identifiers a Sophios failure carries.

Its own module because a code is contract: a caller matches on
`SophiosErrorCode.UNDEFINED_EDGE`, suppresses it, or reads it out of a log,
without importing the reporting plumbing.

Three ranges, one enum: `wic0NN` is a document (it said something the language
does not accept) or what running it needs from the machine (kind `machine`);
`api0NN` is the Python API (the document is fine, the call was not); `nf-sNNN`
is Nextflow support (kind `target`: the workflow is fine, the Nextflow target
cannot run it yet). One type so a caller matches on one `except`; separate
ranges so an API code does not point at the .wic reference. An `nf-sNNN` code
is retired, never reused, when the Nextflow target learns its construct, so the
range is the Nextflow target's support table.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Kind(StrEnum):
    """What a reader changes when a code is reported."""

    DOCUMENT = 'document'  #: the .wic file, the Python workflow, or an imported Nextflow pipeline
    CALL = 'call'          #: the Python API call
    MACHINE = 'machine'    #: this machine: what is installed, running, readable or writable
    RUN = 'run'            #: nothing in Sophios: a tool failed while it ran
    TARGET = 'target'      #: nothing in the workflow: the target cannot run it yet


@dataclass(frozen=True, slots=True)
class Explanation:
    """What a code means and what to do about it, in one line each."""

    kind: Kind
    meaning: str
    fix: str


#: What a code of each range is about, unless the code says otherwise.
_KIND_OF_RANGE: Final = {'wic': Kind.DOCUMENT, 'api': Kind.CALL, 'nf-': Kind.TARGET}
_explanations: dict[str, Explanation] = {}


def _explained(code: str, meaning: str, fix: str, kind: Kind | None = None) -> str:
    """`code`, the value of a member, with what it means and what to do about it recorded for `EXPLANATIONS`."""
    _explanations[code] = Explanation(kind or _KIND_OF_RANGE[code[:3]], meaning, fix)
    return code


class SophiosErrorCode(StrEnum):
    """Stable identifiers for diagnostics, each declared with what it means and what to do about it.

    Codes are part of the contract: they can be matched on, suppressed, and
    documented, whereas message wording is free to improve.
    """

    INVALID_YAML = _explained(
        'wic001',
        'The file is not valid YAML.',
        'Correct the YAML at the position shown: an indentation, a missing `:`, or an unclosed quote or bracket.')
    NOT_A_MAPPING = _explained(
        'wic002',
        'The document is not a mapping.',
        'Make the top level a mapping, with keys such as `inputs:`, `steps:` and `outputs:`.')
    EXPECTED_MAPPING = _explained(
        'wic003',
        'A mapping was expected here, as in `steps: 3`.',
        'Write `key: value` entries at the position shown.')
    EXPECTED_SEQUENCE = _explained(
        'wic004',
        'A sequence was expected here, as in `out: 3`.',
        'Write a list at the position shown: `- item` lines, or `[a, b]`.')
    EXPECTED_SCALAR = _explained(
        'wic005',
        'A scalar was expected here, such as a mapping key that is itself a list or carries a Sophios tag.',
        'Write one name, number or string at the position shown; a tag goes on the value, as in `a: !ii b`.')
    MISSING_STEP_ID = _explained(
        'wic006',
        'A step in a sequence has no `id:`.',
        'Add `id:` with the tool or workflow the step runs, or write `steps:` as a mapping keyed by step (.wic '
        'reference §2.1).')
    EMPTY_STEP_ID = _explained(
        'wic007',
        "A step's `id:` is empty.",
        'Write the name of the tool or workflow the step runs.')
    MALFORMED_WIC_STEP_KEY = _explained(
        'wic008',
        'A `wic: steps:` key is neither `(index, name)` nor a step id.',
        "Key the entry `(index, name)` with the step's 1-based position and id, or by the id alone when no other "
        "step has it (.wic reference §7).")
    UNKNOWN_TAG = _explained(
        'wic009',
        'An unknown YAML tag; the Sophios tags are `!ii`, `!&`, `!*` and `!cwl`.',
        'Use one of the four Sophios tags, or remove the tag.')
    DUPLICATE_KEY = _explained(
        'wic010',
        'A key bound twice: an input, a step, a `wic:` key, or a second `id:`.',
        'Keep one of the two and delete the other (.wic reference §3.8).')
    UNRESOLVED_INPUT = _explained(
        'wic011',
        'An untagged input value names no workflow input.',
        'Write a literal as `!ii <value>`, or declare the name under `inputs:` (.wic reference §3.3).')
    MISSING_REQUIRED_INPUT = _explained(
        'wic012',
        'A required input gets no value, such as `!ii null` on an input that is not optional.',
        "Bind the input to a value, an edge or a workflow input, or make the tool's input optional.")
    SUBWORKFLOW_INVALID = _explained(
        'wic013',
        "A workflow or a step's process cannot be used: no steps, a tool or subworkflow not found, a CWL `Workflow` "
        "as a step, a packed `$graph` with no `main`, subworkflows that call each other in a cycle, "
        "`implementations` with none chosen, ports only the CWL runner can read, or a tool's relative files "
        "(`$include`, `$import`, a relative `location`) that cwltool cannot read.",
        'Do what the message names: put the tool or `.wic` file on a search path of the config (`search_paths_cwl`, '
        '`search_paths_wic`), give the workflow steps, or break the cycle (.wic reference §2.3).')
    SCRIPT_ARGUMENT_MISMATCH = _explained(
        'wic014',
        'The arguments given to a Python script step do not match its declared inputs.',
        'Bind exactly the inputs the script declares.')
    CONTAINER_ENGINE_UNAVAILABLE = _explained(
        'wic015',
        'The container engine cannot be used: not installed, not running, not yours to use, or overloaded. '
        'Checked only when a step runs in a container.',
        'Do what the message says: install the engine, start it, or give your user access to it; '
        'under `--target cwl`, `--container_engine` names another engine, and `--ignore_docker_install` skips the '
        'check.',
        kind=Kind.MACHINE)
    MISSING_INPUT_FILE = _explained(
        'wic016',
        'An input file or directory cannot be used: it does not exist, it is the other kind, or you may not read it.',
        'Correct the path, create it, or make it readable; the message says where Sophios looked.',
        kind=Kind.MACHINE)
    UNKNOWN_LANG_VERSION = _explained(
        'wic017',
        'An unknown `lang_version`.',
        'Pin a version the message lists, or remove the pin.')
    LANG_VERSION_CONFLICT = _explained(
        'wic018',
        'Two `lang_version` pins in one compilation disagree.',
        'Make the pins agree, or set one version for the whole compilation with `--lang_version`.')
    MISPLACED_EDGE_DEF = _explained(
        'wic019',
        "`!&` outside a step's `out:` entry.",
        "Name the output with `!&` on the producing step's `out:` entry, and read it with `!*` (.wic reference "
        "§3.6).")
    LITERAL_TYPE_MISMATCH = _explained(
        'wic020',
        "A literal does not have the type of its input, or a scattered input's literal is not a list.",
        "Write a value of the input's type, quoting a string YAML would read as something else, and give a scattered"
        " input a list (.wic reference §3.2, §6.1).")
    DIRECTORY_NOT_WRITABLE = _explained(
        'wic021',
        'A directory Sophios writes into cannot be created or written: the base path (`autogenerated/` under '
        'the working directory unless `--basepath` or `Workflow.run(basepath=)` names another), `--outdir`, '
        "or an `outdir` given to `Workflow.run()`.",
        'Name a directory you can write to with `--basepath` and `--outdir`; from Python, '
        '`run(basepath=...)` and `run_args_dict={"outdir": ...}` take another.',
        kind=Kind.MACHINE)
    FIXED_POINT_NOT_REACHED = _explained(
        'wic022',
        'Inference did not settle within its iteration limit.',
        'Bind the inputs explicitly with `!&` and `!*` or workflow inputs, or drop `--insert_steps_automatically`.')
    INCOMPATIBLE_INPUT_REFERENCE = _explained(
        'wic023',
        'A reference whose type can never feed the input it binds, such as a `string` into a `File`.',
        'Bind the input to a source of a matching type.')
    RESERVED_KEY = _explained(
        'wic024',
        'A `wic_` mapping in an input position that is not a Sophios construct.',
        'Correct the spelling to a construct of .wic reference §3.7, or rename the key.')
    UNDEFINED_EDGE = _explained(
        'wic025',
        'An edge reference with no definition before it, anywhere in the compilation.',
        "Define the name with `!&` on an earlier step's `out:` entry, or correct its spelling (.wic reference §4).")
    DUPLICATE_EDGE_DEF = _explained(
        'wic026',
        'An edge name defined twice.',
        'Give each `!&` its own name.')
    EMPTY_NAME = _explained(
        'wic027',
        'An input, `out:` entry or edge with an empty name.',
        'Write the name.')
    UNDECLARED_PORT = _explained(
        'wic028',
        "An input or output name the step's process does not declare.",
        'Use a port the tool or subworkflow declares.')
    PROGRAM_MISSING = _explained(
        'wic029',
        'A program the run calls is not installed, or cannot run on this platform.',
        'Install the program the message names, as it says; on Windows, run Sophios inside WSL.',
        kind=Kind.MACHINE)
    RECURSIVE_ALIAS = _explained(
        'wic030',
        'A YAML alias that contains itself.',
        'Remove the alias that refers back to its own anchor.')
    DUPLICATE_DOCUMENT_NAME = _explained(
        'wic031',
        'A promoted port whose `format:` uses a `$namespaces` prefix that two of its sources bind to different '
        'URIs.',
        'Bind the prefix to the same URI in both, or use another prefix in one of them (.wic reference §5.2).')
    UNKNOWN_SCATTER_PORT = _explained(
        'wic032',
        'A `scatter:` entry that is not an input of its step.',
        'Scatter over an input the step declares and binds (.wic reference §6.1).')
    UNKNOWN_WIC_KEY = _explained(
        'wic033',
        'A key the `wic:` block does not have.',
        'Use a key from .wic reference §7, or remove it.')
    MALFORMED_WIC_VALUE = _explained(
        'wic034',
        'A `wic:` value of the wrong shape.',
        'Write the value in the shape .wic reference §7 gives for its key.')
    UNSUPPORTED_CWL_VERSION = _explained(
        'wic035',
        'A `cwlVersion` other than `v1.0`, `v1.1` or `v1.2`.',
        'Declare `cwlVersion: v1.2`.')
    UNTYPED_OUTPUT = _explained(
        'wic036',
        'A workflow output with no `type:` and no producer to take it from.',
        'Give the output a `type:`, or an `outputSource:` naming the step output it comes from (.wic reference '
        '§5.2).')
    IMAGE_UNAVAILABLE = _explained(
        'wic037',
        'An image the run needs could not be pulled, loaded or imported before the run; the message quotes the '
        "engine's last line.",
        'Check the image name or source, the network and your registry login, then run again.',
        kind=Kind.MACHINE)
    STEP_INPUT_RECORD = _explained(
        'wic038',
        "CWL's step input written where Sophios does not read it: an untagged mapping of step-input fields, a "
        "malformed `!cwl {...}` record, or a list `outputSource`, `linkMerge` or `pickValue` on a workflow output.",
        'Write step-input fields as a `!cwl {...}` record (.wic reference §3.5); a workflow output names one step '
        'output.')
    POSITIONAL_OUTPUT_SOURCE = _explained(
        'wic039',
        'A positional `(index, name)` `outputSource` that names the wrong step.',
        'Use the index and id of the step the output comes from, or write `step/port` (.wic reference §5.2).')
    INFERENCE_GUESS = _explained(
        'wic043',
        'Note: inference guessed an edge, as another output also fitted the input.',
        'Draw the workflow and check the guessed edges, with `--graphviz` or `Workflow.draw()`; write the ones that '
        'are wrong with `!&` on the output and `!*` on the input (.wic reference §8). `--inference_strict` makes '
        'each guess an error.')
    REALTIME_DECLARATION = _explained(
        'wic044',
        'A real-time analysis declaration (a `cwl_subinterpreter` step) with an input that is not a literal or has '
        'the wrong shape, or whose analysis does not compile.',
        'Write each input of the step as a literal of the shape the message names, and fix the analysis it names '
        '(docs/realtime-analysis.md).')
    INVALID_NAME = _explained(
        'wic045',
        'A step id, workflow input or output, or workflow name with a character other than letters, digits, '
        '`_`, `.` and `-`, or starting with `.`.',
        'Rename it with letters, digits, `_`, `.` and `-`: the name becomes part of file names and of the names '
        'Sophios makes with `~` and `@`.')
    UNCHECKED_CUDA_FIELD = _explained(
        'wic046',
        'Note: under `--target nextflow`, a `cwltool:CUDARequirement` names a `cudaVersionMin` or '
        '`cudaComputeCapability`, which the generated pipeline does not check; it asks only for a number of GPUs.',
        'Run the pipeline where the GPUs and the container image provide that CUDA version and compute '
        'capability, or remove the field.')
    IMPORT_EDITED_HELPER = _explained(
        'wic047',
        'Importing Nextflow: a `def` in `workflow.nf` that is not a helper Sophios generates, byte for byte.',
        'Restore the helper as Sophios wrote it, or remove the `def`: the import reads only what Sophios writes.')
    IMPORT_FOREIGN_STATEMENT = _explained(
        'wic048',
        'Importing Nextflow: a statement, directive or channel operator Sophios does not write, such as an '
        '`include`, an `errorStrategy` directive or a `.view()` call.',
        'Remove the construct the message names: the import reads only the pipelines Sophios writes '
        '(docs/nextflow.md).')
    IMPORT_FOREIGN_DECLARATION = _explained(
        'wic049',
        'Importing Nextflow: a process input or output declaration Sophios does not write, such as '
        '`tuple val(id), path(reads)`.',
        'Write the process as a Sophios tool and compile it to Nextflow; the import reads only what Sophios writes.')
    IMPORT_FOREIGN_SCRIPT = _explained(
        'wic050',
        'Importing Nextflow: a process script or stub that is not the command or stub Sophios writes, such as a '
        'hand-written shell script.',
        'Write the command as a Sophios tool and compile it to Nextflow; the import reads only what Sophios writes.')
    IMPORT_PARAMS = _explained(
        'wic051',
        'Importing Nextflow: a `nextflow_params.json` that does not hold the values of the parameters the pipeline '
        'checks, as Sophios writes them.',
        'Give each parameter the entry workflow checks one value, and no other key, in the form Sophios writes.')
    IMPORT_CONFIG = _explained(
        'wic052',
        'Importing Nextflow: a `nextflow.config` other than the one Sophios writes for the pipeline.',
        'Restore the line Sophios wrote: `docker.enabled` is true exactly when a process has a container.')
    IMPORT_MISMATCH = _explained(
        'wic053',
        'Importing Nextflow: text in the forms Sophios writes that is not what it writes for the workflow the '
        'text describes, such as a derived form that disagrees with the rest.',
        'Restore the line the message names as Sophios wrote it, or regenerate the pipeline from its workflow.')
    IMPORT_UNWRITABLE = _explained(
        'wic054',
        'Importing Nextflow: a pipeline no workflow built through the Python API compiles to as it is written, '
        'such as one with a capitalized process name, which the compile would give a leading `_`, or with a port '
        'of a CWL tool named like an attribute of the tool builder\'s `Inputs` or `Outputs`, such as `keys`.',
        'Rename what the message names in the workflow the pipeline was compiled from, to a name the compile keeps, '
        'and compile it again.')
    INFERENCE_DUPLICATE = _explained(
        'wic055',
        'Inference would give two inputs of one step the same output, and nothing says that is meant: the inputs '
        'are not all written and do not all declare a file format that output carries.',
        'Write the edge each input should read, with `!&` on the output and `!*` on the input (.wic reference §8); '
        'two written edges to one output are always accepted.')
    WHEN_NOT_BOOLEAN = _explained(
        'wic056',
        "Note: a step's `when` cannot give `true` or `false`: it computes a number, a string or `null`, or it is "
        'not one `$(...)` or `${...}` expression but text, which CWL refuses or evaluates to text. cwltool stops '
        'the run there, and `--target nextflow` refuses it.',
        'Write the `when` as one expression that gives a boolean: `$(inputs.count > 0)` rather than '
        '`$(inputs.count)`, and `$(inputs.ready)` rather than `inputs.ready`.')

    # --- Python API. The document is valid; the call was not. ---
    INVALID_INPUT_VALUE = _explained(
        'api001',
        'A value bound to a step input that the input cannot take.',
        "Bind a port object directly, or several with `StepInput(source=[...])`; a string never names a port.")
    INVALID_STEP = _explained(
        'api002',
        'A step the workflow cannot place: a repeated step name, a link to a step outside the workflow or later in '
        'it, a workflow input with no type, or two tools that share a file stem.',
        'Do what the message names: pass `step_name=` to a reused tool, list the source step earlier, or add it to '
        'this workflow.')
    INVALID_LINK = _explained(
        'api003',
        "A link from this workflow's own output, or between ports whose types do not match.",
        "Bind the input to an earlier step's output, or to a workflow input, of a matching type.")
    INVALID_TOOL = _explained(
        'api004',
        'A CWL tool that could not be loaded or parsed.',
        'Point the step at a valid CWL CommandLineTool; the message says what is wrong with it.')
    WORKFLOW_RUN_FAILED = _explained(
        'api005',
        'A local run that did not complete: it failed, or was cancelled.',
        "Read the lines printed before the error: the failed step's messages, or that the run was stopped.",
        kind=Kind.RUN)
    NO_PYTHON_SPELLING = _explained(
        'api006',
        'A `.wic` construct the Python API has no spelling for, met by `Workflow.from_wic`.',
        'Write the workflow in the Python API in the way the message names, or keep it as a `.wic` file.')
    INVALID_INFERENCE = _explained(
        'api007',
        'An `Inference` the compiler cannot be given: a format rule other than `continue` or `break`, '
        'a renaming that is not a pair of strings, renamings with naming conventions off, '
        'or format rules with step insertion off.',
        'Do what the message names: use `continue` or `break`, write each renaming as `(before, after)`, '
        'set `naming_conventions=True` for renamings, or set `insert_steps=True` for format rules.')
    REALTIME_NOT_ASYNC = _explained(
        'api008',
        'A workflow that declares real-time analyses, given to `Workflow.run_async`, which never runs them.',
        'Run it with `Workflow.run()` or `Workflow.start()`; from a coroutine, '
        '`await asyncio.to_thread(workflow.run)` keeps the event loop free.')

    # --- Nextflow support. The workflow is valid; the Nextflow target cannot run it yet. ---
    NF_PARTIAL_FAILURE = _explained(
        'nf-s001',
        'Partial failure (`--partial_failure_enable`, `RunPolicy(partial_failure=True)`), a policy of the CWL '
        'target.',
        'Compile without the policy, or compile with `--target cwl`.')
    NF_NAME = _explained(
        'nf-s002',
        'Two names that become one Nextflow identifier, or a name with none: steps, tool inputs or outputs, '
        'workflow inputs or outputs, or a parameter Sophios makes for a default.',
        'Rename one of the names the message lists.')
    NF_SCATTERED_CALL = _explained(
        'nf-s003',
        'A `scatter` on a step that calls a nested workflow.',
        'Scatter the tool steps inside the nested workflow, or compile with `--target cwl`.')
    NF_SCATTER_OF_ARRAYS = _explained(
        'nf-s004',
        'A scatter over an input whose elements are arrays.',
        'Scatter over an array of values, Files or Directories, or compile with `--target cwl`.')
    NF_SCATTERED_STEP_ONLY_INPUT = _explained(
        'nf-s005',
        'A scatter over a step-only input, which no process port receives.',
        "Scatter over an input the step's tool declares.")
    NF_SCATTER_SOURCE = _explained(
        'nf-s006',
        'A scattered input fed by anything but one source as it is: several sources, `linkMerge`, `pickValue` or '
        '`default`, or a workflow input that is not an array.',
        "Feed the scattered input from one array, a workflow input or an earlier step's output.")
    NF_NESTED_RESULT = _explained(
        'nf-s007',
        'A `nested_crossproduct` result read by a later step.',
        'Read it from a workflow output only, or scatter with `dotproduct` or `flat_crossproduct`.')
    NF_SCATTER_METHOD_ALONE = _explained(
        'nf-s008',
        'A `scatterMethod` on a step with no `scatter`, which cwltool ignores.',
        'Remove the `scatterMethod`, or give the step its `scatter`.')
    NF_CONDITIONAL_CALL = _explained(
        'nf-s009',
        'A `when` on a step that calls a nested workflow.',
        'Put the `when` on the tool steps inside the nested workflow.')
    NF_WHEN_SUBSET = _explained(
        'nf-s010',
        'A `when` outside the safe JavaScript subset: a `${...}` body, an operator or function the subset does '
        'not have, or an ordered comparison of anything but numbers.',
        'Write the condition in the subset (docs/nextflow.md, Conditional steps), or compile with `--target cwl`.')
    NF_WHEN_UNBOUND = _explained(
        'nf-s011',
        'A `when` that reads an input its step does not bind, which cwltool reads as `undefined`.',
        'Bind the input on the step, as a step-only input if its tool does not take it.')
    NF_WHEN_WITHOUT_INPUTS = _explained(
        'nf-s012',
        "A `when` on a step whose tool has no inputs: Nextflow decides a condition each time the step's inputs "
        'arrive.',
        'Give the step an input to decide on, such as a step-only input from a workflow input.')
    NF_SKIPPED_OUTPUT = _explained(
        'nf-s013',
        'The output of a conditional step read where the value a skipped step gives, `[]` in Nextflow, cannot '
        'stand for `null`: an array output outside a scatter, or an array, File, Directory or required input that'
        ' reads it.',
        'Read it into an optional value input or a workflow output, or compile with `--target cwl`.')
    NF_INPUT_WITHOUT_VALUE = _explained(
        'nf-s014',
        "A required workflow input with no value: the pipeline's parameters are written when it compiles.",
        'Give the input a value: `!ii` in the `.wic` file, or `flow.inputs.<name> = ...` in Python.')
    NF_INPUT_VALUE = _explained(
        'nf-s015',
        "A workflow input's value the pipeline's parameters cannot hold, such as a File or Directory with "
        '`secondaryFiles`.',
        "Give a plain value of the input's type.")
    NF_CALL_INPUT = _explained(
        'nf-s016',
        "A nested workflow's input bound by anything but one source as it is (several sources, `linkMerge`, "
        '`pickValue`, `default`, `valueFrom`), or with a `default` of its own.',
        'Merge, pick or default on a step inside the nested workflow, or bind the input on the calling step.')
    NF_DEFAULT = _explained(
        'nf-s017',
        "A `default` the pipeline cannot hold: one that is not a JSON scalar of its input's type, a File or "
        'Directory on a step input, or one on a step-only input with no source of a scalar type.',
        "Give a scalar default of the input's type, or the path as a workflow input.")
    NF_JOIN = _explained(
        'nf-s018',
        'A step input that joins its sources in a way the target does not form: `merge_nested` over an array, '
        '`pickValue` over an array or over one source, a `null` element, a `default` beside them, or a join that '
        'gives a list to an input that takes one value, or the reverse.',
        'Join the values in a tool step, or compile with `--target cwl`.')
    NF_STEP_INPUT_VALUE_FROM = _explained(
        'nf-s019',
        '`valueFrom` on a step input.',
        'Compute the value in a tool step, or compile with `--target cwl`.')
    NF_ABSENT_OPTIONAL = _explained(
        'nf-s020',
        'An optional input with no value whose absence changes the command, or an absent optional File or '
        'Directory workflow input.',
        'Give the input a value, or bind it so that its absence leaves the command as it is (docs/nextflow.md, '
        'Absent optional values).')
    NF_WORKFLOW_INPUT_BINDING = _explained(
        'nf-s021',
        '`inputBinding` on a workflow input.',
        'Remove it: the steps that read the input bind it.')
    NF_LOAD_CONTENTS = _explained(
        'nf-s022',
        '`loadContents` on an input, a binding, a step input or a workflow input.',
        "Read the file in the tool's command.")
    NF_LOAD_LISTING = _explained(
        'nf-s023',
        '`loadListing`, or `LoadListingRequirement`: a Directory is staged whole.',
        'Remove it.')
    NF_SECONDARY_FILES = _explained(
        'nf-s024',
        '`secondaryFiles`.',
        'Stage each file as its own input.')
    NF_FORWARDED_INPUT = _explained(
        'nf-s025',
        'A workflow output that forwards a workflow input.',
        "Output a step's output.")
    NF_OUTPUT_WITHOUT_SOURCE = _explained(
        'nf-s026',
        'A workflow output with no `outputSource`.',
        'Name the step output it comes from.')
    NF_SOURCE_AS_TEXT = _explained(
        'nf-s027',
        "A source written as `step/port` text that names a nested workflow's step, which Sophios keeps as written"
        ' and the target does not follow.',
        "Read the nested workflow's output through an edge (`!&` and `!*`), or compile with `--target cwl`.")
    NF_SOURCE_NAMES_NO_STEP = _explained(
        'nf-s028',
        'A source written as `step/port` text whose step is no step of its workflow, or whose port is no output of'
        ' that step.',
        'Name a step of the workflow, as the compiled CWL spells it, or read the output through an edge (`!&` and'
        ' `!*`).',
        kind=Kind.DOCUMENT)
    NF_CAPTURED_TEXT_READ = _explained(
        'nf-s029',
        'File text captured by `outputEval: $(self[0].contents)` read by another step.',
        'Read the captured text from a workflow output only, or pass the File to the next step.')
    NF_OPTIONAL_OUTPUT = _explained(
        'nf-s030',
        'An optional File or Directory output read by another step, or one whose glob may match more than one '
        'file and reaches no workflow output.',
        'Make the output required, or read it from a workflow output only.')
    NF_OUTPUT_TYPE = _explained(
        'nf-s031',
        'A tool output that is not one File or Directory found by its glob, or one string captured as text: an '
        'array output among them.',
        'Collect one File or Directory, or capture a `string` with `loadContents` and `outputEval: '
        '$(self[0].contents)`.')
    NF_GLOB = _explained(
        'nf-s032',
        'An output whose glob is not one non-empty string: a list of globs, an empty glob, or none.',
        'Write one glob.')
    NF_OUTPUT_EVAL = _explained(
        'nf-s033',
        'An `outputEval` other than `$(self[0])` and `$(self[0].contents)`.',
        'Write one of those two, or compute the value in a later tool step.')
    NF_CAPTURE = _explained(
        'nf-s034',
        '`$(self[0])` or `$(self[0].contents)` without what makes it a projection: `loadContents: true` beside '
        'the capture, a `string` output for captured text, one File or Directory output and one literal glob with'
        ' no wildcard or input reference for `$(self[0])`.',
        'Add what the message names.')
    NF_WRITE_THROUGH_STAGED = _explained(
        'nf-s035',
        'An output glob, `stdout` or `stderr` that writes through the name a staged input is linked under.',
        'Derive a new name from the input, or make it a writable InitialWorkDirRequirement entry.')
    NF_REQUIREMENT = _explained(
        'nf-s036',
        'A requirement or hint class the target does not lower, such as `SoftwareRequirement` or `ToolTimeLimit`,'
        ' or a workflow feature class written on a tool.',
        'Use `DockerRequirement` for software, declare a feature on the workflow, or remove the class.')
    NF_NETWORK_ACCESS = _explained(
        'nf-s037',
        '`NetworkAccess` other than the literal `true`: a Nextflow task has network access.',
        'Remove it, or write `networkAccess: true`.')
    NF_SHELL_IN_CONTAINER = _explained(
        'nf-s038',
        '`ShellCommandRequirement` beside `DockerRequirement`: CWL runs the command as `/bin/sh -c` after the '
        "image's entrypoint, which a Nextflow task does not.",
        'Drop one of the two, or compile with `--target cwl`.')
    NF_DOCKER = _explained(
        'nf-s039',
        'A `DockerRequirement` with `dockerFile`, `dockerImport`, `dockerLoad` or `dockerOutputDirectory`.',
        'Use `dockerPull` or `dockerImageId`.')
    NF_RESOURCE = _explained(
        'nf-s040',
        'A resource request the target cannot make: `outdirMin`, `outdirMax`, `tmpdirMin`, `tmpdirMax`, '
        'fractional cores, or a value computed by an expression, a CUDA device count among them.',
        'Request literal whole cores and literal memory and devices, or remove the field.')
    NF_ENVIRONMENT = _explained(
        'nf-s041',
        'An environment variable the task cannot set: a name that is not a shell variable name, or an `envValue` '
        'other than literal text or exactly `$(inputs.<name>)` of a string input.',
        'Name the variable with letters, digits and `_`, and set it to text or a string input.')
    NF_EXIT_CODES = _explained(
        'nf-s042',
        '`successCodes`, `permanentFailCodes` or `temporaryFailCodes`: a nonzero exit fails a Nextflow task.',
        'Make the command exit 0 on success, or compile with `--target cwl`.')
    NF_STAGING = _explained(
        'nf-s043',
        'An InitialWorkDirRequirement listing entry other than a bare `$(inputs.<name>)` or a Dirent of one File '
        'or Directory input: inline content, a listing that is not a list, a computed `entryname` or `writable`, '
        "an array or value input, an undeclared input, or an `entryname` with a path separator or Sophios's "
        'reserved prefix.',
        'Stage each File or Directory input as its own entry, under a literal name or its own basename '
        '(docs/nextflow.md, Staging with InitialWorkDirRequirement).')
    NF_STAGED_NAMES = _explained(
        'nf-s044',
        'Staged names that collide: an input listed twice, two inputs under one literal name, or an input staged '
        'under a literal name and also referenced in the command.',
        'Give each staged input one entry and one name, and name it by its literal in the command.')
    NF_EXPRESSION = _explained(
        'nf-s045',
        'A CWL expression the target does not lower: anything but `$(inputs.<name>)`, and on a File or Directory '
        'input `$(inputs.<name>.path)` and `$(inputs.<name>.basename)`; `expressionLib` among them.',
        'Write one of those references, or compute the value in a tool step.')
    NF_PATH_REFERENCE = _explained(
        'nf-s046',
        'A File or Directory reference whose CWL value is not the text it stands for: `.path` or `.basename` of '
        'an input that is not a File or Directory, a File object inside other text (cwltool writes it as JSON), '
        'an object where CWL takes a name, an absolute path where it takes a relative one, or a `stdin` that is '
        "not one file's whole path.",
        'Write the reference the message names.',
        kind=Kind.DOCUMENT)
    NF_NOT_A_COMMAND_LINE_TOOL = _explained(
        'nf-s047',
        'A step that runs something other than a `CommandLineTool`, such as an `ExpressionTool`.',
        'Write the step as a CommandLineTool, or compile with `--target cwl`.')
    NF_TYPE = _explained(
        'nf-s048',
        'A type the target does not lower: `long`, `double`, `enum`, a record, `Any`, a union other than one type'
        ' and `null`, nested arrays, or an array schema with fields beyond `type` and `items`.',
        'Declare `int`, `float`, `string`, `boolean`, `File`, `Directory`, or one array of them.')
    NF_BOOLEAN_BINDING = _explained(
        'nf-s049',
        "A boolean binding the target cannot render: a boolean array's binding, a `valueFrom` other than the "
        "input's own value, or a `valueFrom` on an optional boolean.",
        'Bind the boolean as a flag with a `prefix` (docs/nextflow.md, Boolean flags).')
    NF_ARRAY_BINDING = _explained(
        'nf-s050',
        'An array binding the target cannot render: `valueFrom`, `separate: false`, `itemSeparator`, or a binding'
        ' on each item; or an array input read outside its own binding, as in a glob.',
        'Bind the array as a whole, each element its own word (docs/nextflow.md, Array-typed inputs).')
    NF_SHELL_QUOTE = _explained(
        'nf-s051',
        '`shellQuote: false` on anything but literal text with no input reference, `prefix` or `$`, beside '
        '`ShellCommandRequirement`.',
        'Write the unquoted text as a literal `valueFrom` (docs/nextflow.md, Shell mode).')
    NF_EMPTY_PREFIX = _explained(
        'nf-s052',
        'A binding with an empty or blank `prefix`, which cwltool skips.',
        'Remove the `prefix`, or write one.')
    NF_EMPTY_COMMAND = _explained(
        'nf-s053',
        "A containerized tool whose command can render empty: CWL then runs the image's `CMD`, which the Nextflow"
        ' task does not.',
        'Give the tool a `baseCommand`, or an argument that is always there.')
    NF_FIELD = _explained(
        'nf-s054',
        'A field the target does not lower and the field table does not list.',
        'Remove the field, or compile with `--target cwl`.')
    NF_INVALID_CWL = _explained(
        'nf-s055',
        'CWL v1.2 that cwltool would not run either: a multi-input scatter with no `scatterMethod`; a '
        '`scatterMethod`, `linkMerge` or `pickValue` CWL does not have; a requirement without a string `class`; a'
        ' section, binding, `envDef`, `outputBinding`, glob or `outputEval` of the wrong shape; a `prefix` that '
        'is not text; `separate` without `prefix`; a `position` that is not an integer; a port with no `type`; a '
        '`when` that is not a string; a resource or CUDA field that is not a positive number.',
        'Correct what the message names; `cwltool --validate` on the tool reports the same.',
        kind=Kind.DOCUMENT)

    @property
    def is_language(self) -> bool:
        """Whether this code describes a document rather than a call: every `wic0NN` code, and an `nf-sNNN` code of
        kind `document`, a mistake in the workflow the Nextflow target is the one to read."""
        return self.startswith('wic') or self.explanation.kind is Kind.DOCUMENT

    @property
    def explanation(self) -> Explanation:
        """What this code means and what to do about it."""
        return EXPLANATIONS[self]


#: One entry per member, as it is declared. docs/error_codes.md shows the same text; a test keeps them equal.
EXPLANATIONS: Final[Mapping[SophiosErrorCode, Explanation]] = {
    SophiosErrorCode(code): explanation for code, explanation in _explanations.items()}
