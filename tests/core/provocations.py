"""One attack per diagnostic code neither a document under tests/core/data/parse nor a fixture family states, as a code
nothing provokes is dead on arrival: each callable of COMPILED raises a SophiosError carrying its code, or
test_lang_parser's census fails."""
# pylint: disable=import-outside-toplevel  # the compiler and API load lazily, so enumerating codes stays cheap
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from unittest import mock

from sophios.lang import SophiosError, SophiosErrorCode, resolve_lang_version
from sophios.wic_types import Yaml


def _compile_minimal(yml: Yaml) -> None:
    """Compile with the real tool registry, imported here because loading it discovers plugins."""
    from .compile_harness import compile_info

    compile_info(yml, 'provoke')


def _provoke_script_argument_mismatch() -> None:
    from sophios.python_cwl_adapter import check_args_match_inputs

    module = ModuleType('provoked_script')
    module.inputs = {'expected': int}  # type: ignore[attr-defined]
    check_args_match_inputs(module, {'unexpected': 1})


def _provoke_invalid_inference() -> None:
    """Renamings given while naming conventions are off."""
    from sophios.api.python.workflow import Inference

    Inference(naming_conventions=False, renamings=[('a', 'b')])


def _provoke_workflow_run_failed() -> None:
    """Run a workflow whose runner fails, engine and disk replaced; the raise is collect()'s, through run()."""
    import sophios.api.python._workflow_runtime as runtime
    from sophios.api.python.workflow import RunState, RunStatus

    from .helpers import echo_workflow

    failed = RunStatus(RunState.FAILED, 'the runner exited with status 1', exit_code=1)
    with mock.patch.object(runtime.preflight, 'ready'), \
            mock.patch.object(runtime.input_output, 'write_artifacts_to_disk'), \
            mock.patch.object(runtime.run_local.RunHandle, '_start'), \
            mock.patch.object(runtime.run_local.RunHandle, 'wait', return_value=failed):
        echo_workflow('provoke').run()


#: One edit of the pipeline `import_edited` writes, for each import code: the file, the text replaced (every
#: occurrence), its replacement, and text on the line the diagnostic names.
IMPORT_EDITS: Final[dict[SophiosErrorCode, tuple[str, str, str, str]]] = {
    # An edited helper; a directive Sophios does not write; a hand-written tuple input; a plain shell script.
    SophiosErrorCode.IMPORT_EDITED_HELPER: (
        'workflow.nf', 'value.toString().replace(', 'value.replace(', 'def __sophios_shell_quote_9f72e'),
    SophiosErrorCode.IMPORT_FOREIGN_STATEMENT: (
        'workflow.nf', '    input:\n', "    errorStrategy 'retry'\n    input:\n", 'errorStrategy'),
    SophiosErrorCode.IMPORT_FOREIGN_DECLARATION: (
        'workflow.nf', '    val message\n', '    tuple val(id), path(reads)\n', 'tuple val(id)'),
    SophiosErrorCode.IMPORT_FOREIGN_SCRIPT: (
        'workflow.nf', "${__sophios_shell_quote_9f72e('echo')}", 'echo hello\necho', 'echo hello'),
    # A parameter the pipeline does not check; Docker enabled for a pipeline with no container.
    SophiosErrorCode.IMPORT_PARAMS: ('nextflow_params.json', '{', '{\n  "extra": 1,', '"extra"'),
    SophiosErrorCode.IMPORT_CONFIG: ('nextflow.config', 'false', 'true', 'docker.enabled'),
    # A `take:` name the workflow does not use, which the text shows only through its calls.
    SophiosErrorCode.IMPORT_MISMATCH: ('workflow.nf', '    take:\n    message\n', '    take:\n    msg\n', 'msg'),
    # A capitalized process name, which the compile writes with a leading `_`.
    SophiosErrorCode.IMPORT_UNWRITABLE: ('workflow.nf', 'greet', 'Greet', 'process Greet'),
}


def import_edited(directory: str, file: str, old: str, new: str, workflow: Any = None) -> Any:
    """Write the pipeline of `workflow`, by default a one-step workflow, into `directory`, replace `old` in one of
    its files with `new`, and import it. A surrogate such as `\\udcff` in `new` is written as the byte it escapes."""
    from sophios.api.python.nextflow import import_nextflow
    from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
    from sophios.api.python.workflow import Workflow

    if workflow is None:
        greet = CommandLineTool('greet', Inputs(message=Input(cwl.string, position=1)),
                                Outputs(out=Output(cwl.file, glob='o'))).base_command('echo').stdout('o').to_step()
        workflow = Workflow([greet], 'provoke')
        greet.inputs.message = workflow.inputs.message.as_type('string')
        workflow.inputs.message = 'hi'
        workflow.outputs.out = greet.outputs.out
    workflow.compile(target='nextflow').write(directory)
    path = Path(directory) / file
    text = path.read_text(encoding='utf-8')
    assert old in text, old
    path.write_text(text.replace(old, new), encoding='utf-8', errors='surrogateescape')
    return import_nextflow(directory)


def _import_edited(code: SophiosErrorCode) -> None:
    with tempfile.TemporaryDirectory() as root:
        import_edited(root, *IMPORT_EDITS[code][:3])


#: Codes neither a parsed document nor a fixture family states: each callable raises SophiosError carrying its code.
COMPILED: Final[dict[SophiosErrorCode, Callable[[], object]]] = {
    SophiosErrorCode.SCRIPT_ARGUMENT_MISMATCH: _provoke_script_argument_mismatch,
    SophiosErrorCode.UNKNOWN_LANG_VERSION: partial(resolve_lang_version, '9.9.9'),
    SophiosErrorCode.LANG_VERSION_CONFLICT: partial(
        resolve_lang_version, None, ('0.0.1', '0.0.2'), known=('0.0.1', '0.0.2')),
    SophiosErrorCode.INVALID_INFERENCE: _provoke_invalid_inference,
    SophiosErrorCode.WORKFLOW_RUN_FAILED: _provoke_workflow_run_failed,
    # A step id with a `~`, which the compiled CWL joins names with.
    SophiosErrorCode.INVALID_NAME: partial(
        _compile_minimal, {'steps': [{'id': 'a~b', 'run': 'touch', 'in': {'filename': {'wic_inline_input': 'x'}}}]}),
    **{code: partial(_import_edited, code) for code in IMPORT_EDITS},
}


def _provoke_partial_failure() -> None:
    """Partial failure, which the Nextflow target refuses as a policy of the CWL target; the refusals raised as
    Sophios raises any error."""
    # pylint: disable=import-outside-toplevel
    from sophios.api.python.nextflow import NotLowered
    from sophios.api.python.workflow import RunPolicy, Workflow
    from sophios.lang.diagnostics import SophiosError

    from .helpers import adapter_step

    try:
        Workflow([adapter_step('touch', filename='empty.txt')], 'provoke').compile(
            target='nextflow', policy=RunPolicy(partial_failure=True))
    except NotLowered as refused:
        raise SophiosError(refused.diagnostics) from None


def _provoke_nextflow_refusal(code: SophiosErrorCode) -> None:
    """The first root of tests/core/data/nextflow/refused whose refusals name `code`, lowered to Nextflow, or for the
    one code no file can provoke, a NaN written in Python; the refusals raised as Sophios raises any error."""
    # pylint: disable=import-outside-toplevel
    from .cases import case_dirs
    from .nextflow.testkit import refusal_of

    for family in case_dirs('nextflow/refused', 'roots'):
        directory, expected = family.values
        for root, refusals in expected['roots'].items():
            if any(refusal[0] == str(code) for refusal in refusals):
                raise SophiosError(refusal_of(directory / root).diagnostics)
    _provoke_not_a_number()


def _provoke_not_a_number() -> None:
    """A float given NaN in Python, which no file can write and the parameters' JSON cannot hold."""
    # pylint: disable=import-outside-toplevel
    from sophios.api.python.nextflow import NotLowered
    from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Outputs, cwl
    from sophios.api.python.workflow import Step, Workflow

    ratio = Step(CommandLineTool('ratio', Inputs(x=Input(cwl.float, position=1)), Outputs()).base_command('echo'))
    ratio.inputs.x = float('nan')
    try:
        Workflow([ratio], 'provoke').compile(target='nextflow')
    except NotLowered as refused:
        raise SophiosError(refused.diagnostics) from None


COMPILED.update({SophiosErrorCode.NF_PARTIAL_FAILURE: _provoke_partial_failure})
COMPILED.update({code: partial(_provoke_nextflow_refusal, code) for code in SophiosErrorCode
                 if code.value.startswith('nf-s')
                 and code not in (SophiosErrorCode.NF_PARTIAL_FAILURE, SophiosErrorCode.NF_WHEN_SUBSET)})
