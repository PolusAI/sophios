"""One attack per diagnostic code — the registry behind the provocation rule.

A diagnostic that cannot be provoked is dead on arrival (the parser's
UNKNOWN_TAG sat unreachable for days), so every `SophiosErrorCode` member must appear in
exactly one tier here, and the meta-test in `test_lang_parser.py` fails the
build for any member that does not.

Two tiers, because the codes have two habitats. PARSE codes fire through
`parse()` alone and their provocations are source strings. COMPILED codes fire
through the compiler or its helpers; their provocations are zero-arg callables
that must raise `SophiosError` carrying the code, importing what they need
lazily so this module stays cheap to import. A branch that adds a `SophiosErrorCode`
extends this registry in the same commit, or the meta-test says so.
"""
from collections.abc import Callable
from functools import partial
from typing import Any, Final

from sophios.lang.error_codes import SophiosErrorCode

#: Codes provoked through `parse()` alone: source text in, diagnostic out.
PARSE: Final[dict[SophiosErrorCode, str]] = {
    SophiosErrorCode.INVALID_YAML: 'steps:\n  - [unclosed\n',
    SophiosErrorCode.NOT_A_MAPPING: '- just\n- a list\n',
    SophiosErrorCode.EXPECTED_MAPPING: 'steps: 3\n',
    SophiosErrorCode.EXPECTED_SEQUENCE: 'steps:\n- id: s\n  out: 3\n',
    SophiosErrorCode.EXPECTED_SCALAR: 'steps:\n  ? [a, b]\n  : {}\n',
    SophiosErrorCode.MISSING_STEP_ID: 'steps:\n- {a: 1, b: 2}\n',
    SophiosErrorCode.EMPTY_STEP_ID: "steps:\n- id: ''\n",
    SophiosErrorCode.MALFORMED_WIC_STEP_KEY: 'wic:\n  steps:\n    "not a key":\n      x: 1\n',
    SophiosErrorCode.UNKNOWN_TAG: 'top: !foo bar\n',
    SophiosErrorCode.DUPLICATE_KEY: 'steps:\n- id: s\n  in:\n    f: !ii a\n    f: !ii b\n',
    SophiosErrorCode.RECURSIVE_ALIAS: 'top: &a [*a]\n',
    SophiosErrorCode.MISPLACED_EDGE_DEF: 'top: !& e\n',
    SophiosErrorCode.RESERVED_KEY: 'steps:\n- id: s\n  in:\n    f:\n      wic_inline_inpt: 1\n',
    SophiosErrorCode.UNKNOWN_WIC_KEY: 'wic:\n  nonsense_key: 1\n',
    SophiosErrorCode.MALFORMED_WIC_VALUE: 'wic:\n  inlineable: sometimes\n',
    SophiosErrorCode.UNSUPPORTED_CWL_VERSION: 'cwlVersion: draft-3\n',
    SophiosErrorCode.STEP_INPUT_RECORD: 'steps:\n- id: s\n  in:\n    f: {source: x}\n',
}

#: Codes provoked through the compiler or its helpers. Callables raise
#: `SophiosError` carrying the code. Extended by the branches that add the
#: codes; empty here because this branch declares no compile-phase codes.
COMPILED: Final[dict[SophiosErrorCode, Callable[[], object]]] = {}


def _provoke_undefined_edge() -> None:
    from .hermetic import compile_production  # pylint: disable=import-outside-toplevel

    compile_production({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
        {'id': 'sink', 'in': {'file': {'wic_alias': 'nothing_defines_this'}}}]})


def _provoke_duplicate_edge_def() -> None:
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel

    compile_hermetic({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}},
         'out': [{'file': {'wic_anchor': 'twice'}}]},
        {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b'}},
         'out': [{'file': {'wic_anchor': 'twice'}}]},
        {'id': 'sink', 'in': {'file': {'wic_alias': 'twice'}}}]})


def _compile_minimal(yml: dict) -> None:
    """Compile one in-memory workflow with the real tool registry.

    Imported lazily, like everything else here: this module is imported by the
    meta-test to enumerate codes, and pulling in the compiler and the tool
    registry to do that would make a cheap import expensive.
    """
    from .compile_harness import compile_info  # pylint: disable=import-outside-toplevel

    compile_info(yml, 'provoke')


def _provoke_unresolved_input() -> None:
    _compile_minimal({'steps': [{'id': 'touch', 'in': {'filename': 'not_a_workflow_input'}}]})


def _provoke_missing_required_input() -> None:
    # A null !ii on a non-nullable input.
    _compile_minimal({'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': None}}}]})


def _provoke_subworkflow_invalid() -> None:
    """Compile a document that declares no step at all.

    The compiler refuses a workflow with nothing to run. Previously provoked
    through the file loader with a validator that refused everything; that
    loader is gone, and this reaches the same code from the compiler itself.
    """
    _compile_minimal({'steps': []})


def _provoke_script_argument_mismatch() -> None:
    from types import ModuleType  # pylint: disable=import-outside-toplevel

    from sophios.python_cwl_adapter import check_args_match_inputs  # pylint: disable=import-outside-toplevel

    module = ModuleType('provoked_script')
    module.inputs = {'expected': int}  # type: ignore[attr-defined]
    check_args_match_inputs(module, {'unexpected': 1}, check=True)


def _provoke_container_engine_unavailable() -> None:
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    from unittest import mock  # pylint: disable=import-outside-toplevel

    from sophios import preflight  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root:
        tool = Path(root) / 'tool.cwl'
        tool.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nrequirements:\n  DockerRequirement:\n'
                        '    dockerPull: docker.io/bash:4.4\nbaseCommand: [echo]\ninputs: {}\noutputs: {}\n',
                        encoding='utf-8')
        with mock.patch.object(preflight.sub, 'run', side_effect=FileNotFoundError('docker')):
            preflight.check(preflight.needs([tool]), preflight.RunSettings('docker', root))


def _provoke_missing_input_file() -> None:
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios import preflight  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root:
        job = preflight.Job({'f': {'class': 'File', 'location': 'definitely_absent.txt'}}, Path(root), 'the workflow')
        preflight.check(preflight.Needs((Path(root) / 'none.cwl',), (), (job,)), preflight.RunSettings('docker', root))


COMPILED.update({
    SophiosErrorCode.UNRESOLVED_INPUT: _provoke_unresolved_input,
    SophiosErrorCode.MISSING_REQUIRED_INPUT: _provoke_missing_required_input,
    SophiosErrorCode.SUBWORKFLOW_INVALID: _provoke_subworkflow_invalid,
    SophiosErrorCode.SCRIPT_ARGUMENT_MISMATCH: _provoke_script_argument_mismatch,
    SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE: _provoke_container_engine_unavailable,
    SophiosErrorCode.MISSING_INPUT_FILE: _provoke_missing_input_file,
})


def _provoke_directory_not_writable() -> None:
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    from unittest import mock  # pylint: disable=import-outside-toplevel

    from sophios import preflight  # pylint: disable=import-outside-toplevel
    from sophios.lang.diagnostics import SophiosError  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root, mock.patch.object(preflight.os, 'access', return_value=False):
        problem = preflight.unwritable(Path(root) / 'autogenerated', 'the compiled workflow', 'run elsewhere')
    if problem is not None:
        raise SophiosError([problem])


COMPILED.update({SophiosErrorCode.DIRECTORY_NOT_WRITABLE: _provoke_directory_not_writable})


def _provoke_unknown_lang_version() -> None:
    from sophios.lang import resolve_lang_version  # pylint: disable=import-outside-toplevel
    resolve_lang_version('9.9.9')


def _provoke_lang_version_conflict() -> None:
    from sophios.lang import resolve_lang_version  # pylint: disable=import-outside-toplevel
    resolve_lang_version(None, ('0.0.1', '0.0.2'), known=('0.0.1', '0.0.2'))


COMPILED.update({
    SophiosErrorCode.UNKNOWN_LANG_VERSION: _provoke_unknown_lang_version,
    SophiosErrorCode.LANG_VERSION_CONFLICT: _provoke_lang_version_conflict,
})


def _provoke_literal_type_mismatch() -> None:
    # !ii places no constraint relating a literal to the declared CWL type of
    # the input it binds, so the typed job boundary must reject a literal that
    # is not of that type.
    from sophios.ir.complete import coerce_job_value  # pylint: disable=import-outside-toplevel
    from sophios.ir.declarations import port_declaration  # pylint: disable=import-outside-toplevel
    from sophios.ir.types import AuthoredName  # pylint: disable=import-outside-toplevel
    coerce_job_value(AuthoredName('n'), port_declaration({'type': 'int'}), '_')


COMPILED.update({
    SophiosErrorCode.LITERAL_TYPE_MISMATCH: _provoke_literal_type_mismatch,
})


def _never_converges() -> None:
    """Drive the typed Infer fixed-point guard to its explicit limit."""
    # pylint: disable=import-outside-toplevel
    from sophios.ir import InferencePolicy, Namespace, WorkflowGraph, infer
    from sophios.lang.diagnostics import SophiosError
    result = infer(WorkflowGraph(Namespace()), InferencePolicy(iteration_limit=0))
    if result.graph is None:
        raise SophiosError(result.diagnostics)


COMPILED.update({
    SophiosErrorCode.FIXED_POINT_NOT_REACHED: _never_converges,
})


def _provoke_incompatible_input_reference() -> None:
    """Bind a declared string workflow input to a File argument."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel
    compile_hermetic({'inputs': {'wf_name': {'type': 'string'}},
                      'steps': [{'id': 'count', 'in': {'file': 'wf_name'}}]}, 'provoke')


COMPILED.update({
    SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE: _provoke_incompatible_input_reference,
})


def _provoke_empty_name() -> None:
    """A name the document leaves empty, which `parse` accepts and lowering cannot represent.

    Raises:
        SophiosError: Always, carrying `wic027`.
    """
    from sophios.ir.declarations import port_declaration  # pylint: disable=import-outside-toplevel
    from sophios.ir.lower import lower  # pylint: disable=import-outside-toplevel
    from sophios.ir.resolve import (  # pylint: disable=import-outside-toplevel
        RegistryKey, ResolvedDocument, ResolvedPort, ResolvedProcess, ResolvedStep,
    )
    from sophios.lang.diagnostics import SophiosError  # pylint: disable=import-outside-toplevel
    from sophios.lang.parser import parse  # pylint: disable=import-outside-toplevel

    document = parse('steps:\n- id: s\n  in:\n    "": !* e\n', 'provoke.wic').document
    assert document is not None
    step = document.steps[0]
    process = ResolvedProcess(
        RegistryKey('test', 's'), 's.cwl',
        (ResolvedPort('', port_declaration(None)),), (),
        {'class': 'CommandLineTool'},
    )
    resolved = ResolvedDocument(
        'provoke', document, (ResolvedStep(step, process),), '0.0.1')
    diagnostics = lower(resolved).diagnostics
    raise SophiosError(tuple(diagnostics))


def _provoke_undeclared_port() -> None:
    """Bind an input of a tool whose interface does not declare it.

    A CommandLineTool, deliberately: the check runs for every resolved step,
    and reporting a subworkflow code for a tool step was what this code
    replaced.

    Raises:
        SophiosError: Always, carrying `wic028`.
    """
    from sophios.ir.lower import lower  # pylint: disable=import-outside-toplevel
    from sophios.ir.resolve import RegistrySnapshot, resolve  # pylint: disable=import-outside-toplevel
    from sophios.lang.diagnostics import SophiosError  # pylint: disable=import-outside-toplevel
    from sophios.lang.parser import parse  # pylint: disable=import-outside-toplevel

    from .synthetic_tools import SYNTHETIC_TOOLS  # pylint: disable=import-outside-toplevel

    document = parse('steps:\n- id: mk_file\n  in: {name: !ii a, ghost: !ii x}\n',
                     'provoke.wic').document
    assert document is not None
    resolved = resolve(document, RegistrySnapshot.from_tools(SYNTHETIC_TOOLS), name='provoke')
    assert resolved.document is not None
    raise SophiosError(tuple(lower(resolved.document).diagnostics))


def _provoke_invalid_input_value() -> None:
    """Bind a list holding a step output, which is a port and never a literal."""
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios.api.python.workflow import Step  # pylint: disable=import-outside-toplevel

    adapters = Path(__file__).resolve().parents[2] / 'cwl_adapters'
    touch = Step(clt_path=adapters / 'touch.cwl')
    Step(clt_path=adapters / 'append.cwl').inputs.file = [touch.outputs.file]


def _provoke_invalid_step() -> None:
    """Compile a workflow whose input is linked to an external step."""
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios.api.python.workflow import Step, Workflow  # pylint: disable=import-outside-toplevel

    adapters = Path(__file__).resolve().parents[2] / 'cwl_adapters'
    external = Step(clt_path=adapters / 'touch.cwl')
    external.inputs.filename = 'empty.txt'
    append = Step(clt_path=adapters / 'append.cwl')
    append.inputs.file = external.outputs.file
    append.inputs.str = 'Hello'
    Workflow([append], 'provoke').compile()


def _provoke_invalid_link() -> None:
    """Bind a workflow output to a value that is not a port."""
    from sophios.api.python.workflow import Workflow  # pylint: disable=import-outside-toplevel

    workflow = Workflow([], 'provoke')
    workflow.outputs.out = 3


def _provoke_duplicate_document_name() -> None:
    """Bind a `$namespaces` prefix to one URI in the document and to another in a tool it promotes.

    The promoted `myns:` format would resolve to whichever binding the
    emitted document kept, so neither can be emitted.

    Raises:
        SophiosError: Always, carrying `wic031`.
    """
    import copy  # pylint: disable=import-outside-toplevel

    from sophios.wic_types import StepId, Tool  # pylint: disable=import-outside-toplevel

    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel
    from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt  # pylint: disable=import-outside-toplevel

    probe = clt({'f': {'type': 'File', 'format': 'myns:format_1'}}, {})
    probe['$namespaces'] = {'myns': 'http://tool.example/'}
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    tools[StepId('probe', SYNTHETIC_NS)] = Tool('/synthetic/probe.cwl', probe)
    compile_hermetic({'$namespaces': {'myns': 'http://other.example/'}, 'steps': [{'id': 'probe'}]},
                     'provoke', tools=tools)


def _provoke_unknown_scatter_port() -> None:
    """Scatter a step over a name that is none of its inputs."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel

    compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a']}},
                                 'scatter': ['nope']}]}, 'provoke')


def _provoke_invalid_tool() -> None:
    """A CWL tool that cannot be loaded."""
    from sophios.api.python.workflow import Step  # pylint: disable=import-outside-toplevel

    Step(clt_path='no_such_tool.cwl')


def _provoke_invalid_inference() -> None:
    """Renamings given while naming conventions are off."""
    from sophios.api.python.workflow import Inference  # pylint: disable=import-outside-toplevel

    Inference(renamings=[('a', 'b')])


def _provoke_workflow_run_failed() -> None:
    """Run a workflow whose runner exits non-zero.

    The runner, the container engine and the disk are replaced; the raise is `Workflow.run()`'s own.
    """
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    from unittest import mock  # pylint: disable=import-outside-toplevel

    import sophios.api.python._workflow_runtime as runtime  # pylint: disable=import-outside-toplevel
    from sophios.api.python.workflow import RunResult, RunState, Step, Workflow  # pylint: disable=C0415

    echo = Step(clt_path=Path(__file__).resolve().parents[2] / 'cwl_adapters' / 'echo.cwl')
    echo.inputs.message = 'hello'
    failed = RunResult(RunState.FAILED, None, Path('outdir'), Path('run_provoke.log'), 1)
    with mock.patch.object(runtime.preflight, 'prepare'), \
            mock.patch.object(runtime.input_output, 'write_artifacts_to_disk'), \
            mock.patch.object(runtime.run_local, 'start', return_value=mock.Mock(**{'wait.return_value': failed})):
        Workflow([echo], 'provoke').run()


def _provoke_realtime_not_async() -> None:
    """Await `run_async()` on a workflow that declares a real-time analysis."""
    import asyncio  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios.api.python.workflow import Step, Workflow  # pylint: disable=import-outside-toplevel

    adapters = Path(__file__).resolve().parents[2] / 'cwl_adapters'
    touch = Step(clt_path=adapters / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    declaration = Step(clt_path=adapters / 'file_watchers' / 'cwl_subinterpreter.cwl')
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    asyncio.run(Workflow([touch, declaration], 'provoke').run_async())


COMPILED.update({
    SophiosErrorCode.INVALID_INPUT_VALUE: _provoke_invalid_input_value,
    SophiosErrorCode.INVALID_STEP: _provoke_invalid_step,
    SophiosErrorCode.INVALID_LINK: _provoke_invalid_link,
    SophiosErrorCode.INVALID_TOOL: _provoke_invalid_tool,
    SophiosErrorCode.INVALID_INFERENCE: _provoke_invalid_inference,
    SophiosErrorCode.WORKFLOW_RUN_FAILED: _provoke_workflow_run_failed,
    SophiosErrorCode.REALTIME_NOT_ASYNC: _provoke_realtime_not_async,
    SophiosErrorCode.UNDEFINED_EDGE: _provoke_undefined_edge,
    SophiosErrorCode.DUPLICATE_EDGE_DEF: _provoke_duplicate_edge_def,
    SophiosErrorCode.EMPTY_NAME: _provoke_empty_name,
    SophiosErrorCode.UNDECLARED_PORT: _provoke_undeclared_port,
    SophiosErrorCode.DUPLICATE_DOCUMENT_NAME: _provoke_duplicate_document_name,
    SophiosErrorCode.UNKNOWN_SCATTER_PORT: _provoke_unknown_scatter_port,
})


def _provoke_untyped_output() -> None:
    """An authored output with neither a type nor a producer."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel

    compile_hermetic({'outputs': {'o': {'label': 'no type here'}},
                      'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]},
                     'provoke')


COMPILED.update({
    SophiosErrorCode.UNTYPED_OUTPUT: _provoke_untyped_output,
})


def _provoke_positional_output_source() -> None:
    """A positional outputSource whose index holds a different step."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel

    compile_hermetic({'outputs': {'o': {'type': 'File', 'outputSource': '(1, xform)/file'}},
                      'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]},
                     'provoke')


COMPILED.update({SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE: _provoke_positional_output_source})


def _provoke_inference_tie() -> None:
    """Under strict inference, one producer offering two matching outputs."""
    from sophios.wic_types import StepId, Tool  # pylint: disable=import-outside-toplevel

    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel
    from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt  # pylint: disable=import-outside-toplevel

    multi = clt({}, {'first': {'type': 'File', 'outputBinding': {'glob': 'first'}},
                     'last': {'type': 'File', 'outputBinding': {'glob': 'last'}}})
    tools = {**SYNTHETIC_TOOLS,
             StepId('multi_file', SYNTHETIC_NS): Tool('/synthetic/multi_file.cwl', multi)}
    compile_hermetic({'steps': [{'id': 'multi_file'}, {'id': 'count'}]}, 'provoke',
                     tools=tools, inference_strict=True)


def _provoke_inference_recency() -> None:
    """Under strict inference, an earlier producer that also matched."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel

    compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
                                {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'b'}}},
                                {'id': 'count'}]},
                     'provoke', inference_strict=True)


COMPILED.update({
    SophiosErrorCode.INFERENCE_TIE: _provoke_inference_tie,
    SophiosErrorCode.INFERENCE_RECENCY: _provoke_inference_recency,
})


def _provoke_no_python_spelling() -> None:
    """Read into Python objects a `.wic` that declares top-level `hints:`, which no Python `Workflow` carries."""
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios.api.python.workflow import Workflow  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'provoke.wic'
        root.write_text('hints:\n  ResourceRequirement:\n    coresMin: 1\n'
                        'steps:\n- id: say\n  run:\n    class: CommandLineTool\n    baseCommand: "true"\n'
                        '    inputs: {}\n    outputs: {}\n', encoding='utf-8')
        Workflow.from_wic(root)


COMPILED.update({SophiosErrorCode.NO_PYTHON_SPELLING: _provoke_no_python_spelling})


def _provoke_realtime_declaration() -> None:
    """A real-time analysis declaration whose `max_times` is not a number."""
    _compile_minimal({'steps': [{'id': 'cwl_subinterpreter', 'in': {
        'file_pattern': {'wic_inline_input': '*.txt'},
        'cwl_tool': {'wic_inline_input': 'touch'},
        'max_times': {'wic_inline_input': 'many'},
        'config': {'wic_inline_input': {}}}}]})


COMPILED.update({SophiosErrorCode.REALTIME_DECLARATION: _provoke_realtime_declaration})


def _provoke_invalid_name() -> None:
    """A step id with a `~`, which the compiled CWL joins names with."""
    _compile_minimal({'steps': [{'id': 'a~b', 'run': 'touch', 'in': {'filename': {'wic_inline_input': 'x'}}}]})


COMPILED.update({SophiosErrorCode.INVALID_NAME: _provoke_invalid_name})


def _provoke_program_missing() -> None:
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    from unittest import mock  # pylint: disable=import-outside-toplevel

    from sophios import preflight  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root, mock.patch.object(preflight.shutil, 'which', return_value=None):
        preflight.check(preflight.Needs((Path(root) / 'none.cwl',), ()),
                        preflight.RunSettings('docker', root, run_script=True))


COMPILED.update({SophiosErrorCode.PROGRAM_MISSING: _provoke_program_missing})


def _provoke_image_unavailable() -> None:
    import subprocess  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    from unittest import mock  # pylint: disable=import-outside-toplevel

    from cwl_utils.parser.cwl_v1_2 import DockerRequirement  # pylint: disable=import-outside-toplevel

    from sophios import preflight  # pylint: disable=import-outside-toplevel

    failed = subprocess.CompletedProcess([], 1, stderr='pull access denied')
    with mock.patch.object(preflight.sub, 'run', return_value=failed):
        preflight.pull(preflight.Needs((Path('w.cwl'),), (DockerRequirement(dockerPull='x/y:1'),)),
                       preflight.RunSettings('docker', '.'))


COMPILED.update({SophiosErrorCode.IMAGE_UNAVAILABLE: _provoke_image_unavailable})


def _provoke_unchecked_cuda_field() -> None:
    """A GPU tool lowered to Nextflow with a `cudaVersionMin`. A note stops nothing, so it is raised here."""
    # pylint: disable=import-outside-toplevel
    from sophios.api.python._workflow_runtime import compile_workflow_result
    from sophios.api.python.tool_builder import CommandLineTool, Inputs, Output, Outputs, cwl
    from sophios.api.python.workflow import Step, Workflow
    from sophios.lang.diagnostics import SophiosError
    from sophios.utils_nf import compiled_source_to_nextflow_with_notes

    gpu = CommandLineTool('gpu', Inputs(), Outputs(out=Output(cwl.file, glob='out'))).gpu(cuda_version_min='11.4')
    _, notes = compiled_source_to_nextflow_with_notes(compile_workflow_result(Workflow([Step(gpu)], 'provoke')))
    raise SophiosError(notes)


COMPILED.update({SophiosErrorCode.UNCHECKED_CUDA_FIELD: _provoke_unchecked_cuda_field})


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
        'workflow.nf', "    ${__sophios_shell_quote_9f72e('echo')}", '    echo hello\n    echo', 'echo hello'),
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
    # pylint: disable=import-outside-toplevel
    from pathlib import Path

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
    import tempfile  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root:
        import_edited(root, *IMPORT_EDITS[code][:3])


COMPILED.update({code: partial(_import_edited, code) for code in IMPORT_EDITS})
