"""What the shipped Python API workflows compile to, not merely that they compile: the discovery lanes assert only
that no exception escaped."""
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from sophios.api.python.workflow import Workflow
from sophios.python_cwl_adapter import import_python_file

from .helpers import REPO_ROOT, adapter_step
from .test_setup import home_with_shipped_tools, use_home

WORKFLOWS = REPO_ROOT / 'examples' / 'workflows'


def _discovered(stem: str) -> Any:
    """Import a shipped Python workflow as discovery does, so these claims are about the file the lanes run."""
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
    the file rather than passing unnoticed.
    """
    touch = adapter_step('touch', filename='empty.txt')
    append = adapter_step('append', file=touch.outputs.file, str='Hello')
    cat = adapter_step('cat', file=append.outputs.file)
    compiled = Workflow([touch, append, cat], 'multistep1_toJson_pyapi_py').compile()
    truth = json.loads(Path(__file__).with_name('ground_truth_multistep1.json').read_text(encoding='utf-8'))
    assert (compiled.name, compiled.cwl_job_inputs) == (truth.pop('name'), truth.pop('yaml_inputs'))
    assert compiled.cwl_workflow == truth


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
    assert example.is_file(), f'{example} is linked from docs/tool-builder.md'
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


_INPUTS = {'input_dir': '/data/in', 'output_dir': '/data/out', 'model_dir': '/data/models'}


@pytest.mark.fast
def test_the_compact_ichnaea_script_names_its_outputs_folder_relative_to_basepath(
        capsys: pytest.CaptureFixture[str]) -> None:
    """With basepath, the compute service requires the outputs folder, relative to it, so the canonical example names
    one; its output folder is just a name, which lands in that folder."""
    example = REPO_ROOT / 'examples' / 'scripts' / 'ichnaea_compact.py'
    module = import_python_file(example.stem, example.resolve())
    assert module.main() == 0
    printed = capsys.readouterr().out  # the compiler's progress, then the request
    request, _ = json.JSONDecoder().raw_decode(printed, printed.index('{'))
    assert (request['basepath'], request['workflowOutDir']) == (module.BASEPATH, 'results_compact')
    assert request['inputs']['autoseg~output'] == {'class': 'Directory', 'location': 'segmentation_compact'}


@pytest.mark.fast
@pytest.mark.needs_cwltool
def test_the_integrated_ichnaea_script_compiles_its_hinted_tool(tmp_path: Path) -> None:
    """The hinted tool compiles: the script builds a tool with a GPU hint and a Docker requirement, which cwl_utils
    renders as lists and compile() once crashed on; imported from a copy, as `workflow()` writes a .cwl beside its
    own file. The script also writes the tool with `validate=True`, which runs cwltool in-process."""
    source = tmp_path / 'ichnaea_integrated.py'
    shutil.copyfile(REPO_ROOT / 'examples' / 'scripts' / source.name, source)
    module = import_python_file(source.stem, source)
    compiled = module.workflow(dict(_INPUTS), 'autoseg_workflow').compile()
    run = compiled.cwl_workflow['steps'][0]['run']
    assert any(entry.get('class') == 'DockerRequirement' for entry in run['requirements'])
    assert any(entry.get('class') == 'cwltool:CUDARequirement' for entry in run['hints'])


@pytest.mark.fast
def test_the_ichnaea_scripts_build_one_tool_with_no_base_command() -> None:
    """The three scripts build the same tool, but for its doc. The image's ENTRYPOINT is its command, which the
    runner puts in front of the tool's command line, so a base command would reach it as arguments."""
    tools = []
    for script, build in (('ichnaea_compact', 'build_autoseg_CLT'), ('ichnaea_integrated', 'build_autoseg_CLT'),
                          ('ichnaea_tool_builder', 'build_tool')):
        example = REPO_ROOT / 'examples' / 'scripts' / f'{script}.py'
        document = getattr(import_python_file(script, example.resolve()), build)().to_cwl_document()
        document.pop('doc')
        tools.append(document)
    assert 'baseCommand' not in tools[0]
    assert tools[1:] == [tools[0], tools[0]]


@pytest.mark.fast
def test_the_word_count_example_runs_wc_on_the_greeting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`examples/scripts/count_words.py` is the tool-builder example the README, the landing page and
    docs/tool-builder.md show: its third step runs `wc -w` and keeps what it prints as `counts.txt`."""
    # It names the shipped tools under `Path.home() / 'wic' / 'cwl_adapters'`, as the docs run it.
    use_home(monkeypatch, home_with_shipped_tools(tmp_path, REPO_ROOT))
    example = REPO_ROOT / 'examples' / 'scripts' / 'count_words.py'
    module = import_python_file(example.stem, example)
    count = module.workflow().compile().cwl_workflow['steps'][2]
    assert count['run']['baseCommand'] == ['wc', '-w']
    assert count['run']['stdout'] == 'counts.txt'
    assert count['in']['text'] == {'source': count['in']['text']['source']}
    assert count['in']['text']['source'] == 'append/file'


@pytest.mark.fast
def test_the_landing_page_shows_the_whole_word_count_tool() -> None:
    """docs/index.md includes the `word_count` expression of count_words.py by line numbers; they must still
    frame exactly that expression."""
    index = (REPO_ROOT / 'docs' / 'index.md').read_text(encoding='utf-8')
    included = re.search(r'count_words\.py\n:language: python\n:lines: (\d+)-(\d+)', index)
    assert included is not None, 'docs/index.md no longer includes count_words.py by line numbers'
    first, last = map(int, included.groups())
    lines = (REPO_ROOT / 'examples' / 'scripts' / 'count_words.py').read_text(encoding='utf-8').splitlines()
    shown = lines[first - 1:last]
    assert (shown[0], shown[-1]) == ('word_count = (', ')')
