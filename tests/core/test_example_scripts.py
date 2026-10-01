"""What the shipped Python API workflows compile *to*, not merely that they compile.

`examples/workflows/` is a `search_paths_wic` entry, so every script in it is
discovered, compiled and schema-validated by the same two steps that carry the
image-workflows corpus. That mechanism asserts one thing: no exception escaped.

It cannot see whether a `when` expression reached the emitted CWL, or which
`scatterMethod` was chosen. Those are the claims here, made against the same
files the mechanism runs — one workflow, two claims about it.

Compiled, never executed; whether a runner can execute the result belongs to
the workflow-running lane.
"""
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.python_cwl_adapter import import_python_file

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / 'cwl_adapters'
HERE = Path(__file__).resolve().parent
WORKFLOWS = REPO_ROOT / 'examples' / 'workflows'


def _discovered(stem: str) -> Any:
    """Import a shipped Python workflow the way discovery does.

    `examples/workflows/` is a `search_paths_wic` entry, so
    `test_compile_python_workflows` already imports each script there and calls
    its `workflow()` — the same path `image-workflows/workflows/bbbc.py` takes.
    That proves the script compiles and nothing more, because it asserts only
    that no exception escaped. These tests import the same file and assert what
    the compiled document actually says, so there is one copy of each workflow
    and two claims about it.
    """
    path = WORKFLOWS / f'{stem}.py'
    assert path.is_file(), f'{path} is a discovered Python workflow and must exist'
    return import_python_file(stem, path.resolve())


def _steps(compiled: Any) -> dict[str, dict[str, Any]]:
    """The compiled steps, keyed by the trailing name of their generated id."""
    return {step['id'].rsplit('__', 1)[-1]: step for step in compiled.cwl_workflow['steps']}


@pytest.mark.fast
def test_a_conditional_step_carries_its_when_expression() -> None:
    """`Step.when` reaches the emitted CWL, on that step alone.

    `when` is a CWL v1.2 conditional and was the only surface in the Python API
    no test in this repository touched — a grep for `.when` across `tests/`
    found nothing. An expression dropped on the way to the CWL turns a
    conditional step into an unconditional one, which is a silent change of
    meaning rather than an error.
    """
    steps = _steps(_discovered('when_pyapi').workflow().compile())

    assert steps['echo'].get('when') == '$(inputs.message < "27")'
    assert 'when' not in steps['toString'], 'only the step that declared it may carry when'


@pytest.mark.fast
def test_scattering_one_input_defaults_to_dotproduct() -> None:
    """The single-input `scatter_on`, which the cross-product tests do not reach.

    Existing scatter tests pass `method=` explicitly. The default picked for one
    port is part of the emitted document's meaning: `dotproduct` walks the
    array, and losing the key entirely would run the step once on the whole
    array instead.
    """
    steps = _steps(_discovered('scatter_single_pyapi').workflow().compile())

    assert steps['echo'].get('scatter') == ['message']
    assert steps['echo'].get('scatterMethod') == 'dotproduct'
    assert 'scatter' not in steps['array_indices'], 'only the scattered step may carry scatter'


@pytest.mark.fast
def test_a_compiled_multistep_workflow_matches_its_checked_in_ground_truth() -> None:
    """The whole compiled document, against a golden file.

    The other tests here assert one key each. This compares the entire emitted
    workflow, its job inputs and its name to `ground_truth_multistep1.json`, so
    a change anywhere in the compiled shape has to be acknowledged by updating
    the file rather than passing unnoticed. The comparison was written as an
    example script's `__main__`, which nothing ran.
    """
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'

    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'

    cat = Step(clt_path=ADAPTERS / 'cat.cwl')
    cat.inputs.file = append.outputs.file

    compiled = Workflow([touch, append, cat], 'multistep1_toJson_pyapi_py').compile()
    ground_truth = json.loads((HERE / 'ground_truth_multistep1.json').read_text(encoding='utf-8'))

    assert compiled.name == ground_truth['name']
    assert compiled.cwl_job_inputs == ground_truth['yaml_inputs']
    assert compiled.cwl_workflow == {
        key: value for key, value in ground_truth.items() if key not in {'name', 'yaml_inputs'}
    }


@pytest.mark.fast
def test_two_in_memory_tools_chain_and_keep_their_output_binding() -> None:
    """A built tool's string output survives composition into a workflow.

    Existing tests cover the `Step(CommandLineTool)` bridge with one tool. What
    this adds is the two-tool chain and the output that is a *string read from
    a file* — `load_contents` with an `output_eval`, which becomes an
    `outputBinding` the compiler must carry through untouched for the
    downstream `outputSource` to mean anything.

    The builder is imported from the example rather than copied here: that file
    has a documentation page which links it and tells a reader to run it, so
    moving or duplicating it would leave the page describing something else.

    No tool is validated: `CommandLineTool.validate()` loads the CWL v1.2 schema
    through cwltool, which is seconds of CI and POSIX-only, and
    `test_python_api_tool_builder.py` already owns that claim.
    """
    example = REPO_ROOT / 'examples' / 'scripts' / 'tool_builder_workflow.py'
    assert example.is_file(), f'{example} is linked from docs/tool_builder_workflow.md'
    module = import_python_file(example.stem, example.resolve())

    compiled = module.build_workflow('hello from test').compile()
    steps = _steps(compiled)

    run_outputs = {entry['id']: entry for entry in steps['read_text']['run']['outputs']}
    binding = run_outputs['result']
    assert binding['type'] == 'string'
    assert binding['outputBinding'] == {
        'glob': 'stdout.txt', 'loadContents': True, 'outputEval': '$(self[0].contents)',
    }

    emitted = compiled.cwl_workflow['outputs']['result']
    assert emitted['type'] == 'string'
    assert emitted['outputSource'] == f"{steps['read_text']['id']}/result"


_INPUTS = {'input_dir': '/data/in', 'output_dir': '/data/out', 'model_file': '/data/sam3.pt'}


@pytest.mark.fast
@pytest.mark.parametrize('script', ['ichnaea_compact', 'ichnaea_integrated'])
def test_the_ichnaea_scripts_compile_their_hinted_tool(script: str, tmp_path: Path) -> None:
    """Both scripts build a tool with a GPU hint and a Docker requirement, which
    cwl_utils renders as lists. `Workflow.compile()` crashed on the list, and the
    only test ran the tool through `validate()`, which never takes the compile path.

    The script is imported from a copy: `ichnaea_integrated.workflow()` writes
    `built-ichnaea-autosegmentation.cwl` next to its own file, which would leave
    an untracked file in `examples/scripts/` after every run."""
    source = tmp_path / f'{script}.py'
    shutil.copyfile(REPO_ROOT / 'examples' / 'scripts' / f'{script}.py', source)
    module = import_python_file(script, source)
    compiled = module.workflow(dict(_INPUTS), 'autoseg_workflow').compile()
    run = compiled.cwl_workflow['steps'][0]['run']
    assert any(entry.get('class') == 'DockerRequirement' for entry in run['requirements'])
    assert any(entry.get('class') == 'cwltool:CUDARequirement' for entry in run['hints'])


@pytest.mark.fast
def test_the_sam3_handoff_snippet_compiles_with_stdout_and_stderr_outputs() -> None:
    """docs/tool_builder_sam3.md hands a built tool with `Output.stdout()` to the
    workflow API. `stdout` and `stderr` are tool-only shorthands that cwltool
    rejects at the workflow boundary, so each promoted output must be a `File`."""
    tool = CommandLineTool('echo_tool', Inputs(message=Input(cwl.string, position=1)),
                           Outputs(out=Output.stdout(), err=Output.stderr())
                           ).base_command('echo').stdout('stdout.txt').stderr('stderr.txt')
    step = Step(tool, step_name='say_hello')
    step.inputs.message = 'hello'
    compiled = Workflow([step], 'wf').compile().cwl_workflow
    step_id = compiled['steps'][0]['id']
    assert compiled['outputs'][f'{step_id}___out']['type'] == 'File'
    assert compiled['outputs'][f'{step_id}___err']['type'] == 'File'
    declared = {output['id']: output['type'] for output in compiled['steps'][0]['run']['outputs']}
    assert declared == {'out': 'stdout', 'err': 'stderr'}, 'the tool keeps its own shorthand'
