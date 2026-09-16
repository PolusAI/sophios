"""Python API surfaces that used to live as example scripts nothing ran.

`examples/scripts/` was documentation that happened to be executable: CI
type-checked, linted and formatted it and never executed one file.
`test_compile_python_workflows` discovers through `search_paths_wic`, which
reaches the corpus repositories and `docs/tutorials` — and `docs/tutorials`
holds no `.py` files at all. So an example could stop compiling and every lane
that touched it would stay green.

Three of them were carrying claims no test made, so the claims moved here and
the scripts are gone. `tool_builder_workflow.py` stays where it is: it has a
walkthrough page of its own that links it and tells a reader to run it, and it
is imported below rather than copied, so there is still exactly one of it.

Compiled, never executed. These assert the shape of the emitted CWL; whether a
runner can execute it is `run_workflows.yml`'s question.
"""
import json
from pathlib import Path
from typing import Any

import pytest

from sophios.api.python.workflow import Step, Workflow
from sophios.python_cwl_adapter import import_python_file

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / 'cwl_adapters'
HERE = Path(__file__).resolve().parent


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
    to_string = Step(clt_path=ADAPTERS / 'toString.cwl')
    to_string.inputs.input = 27

    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    echo.inputs.message = to_string.outputs.output
    echo.when = '$(inputs.message < "27")'

    steps = _steps(Workflow([to_string, echo], 'when_pyapi_py').compile())

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
    array_ind = Step(clt_path=ADAPTERS / 'array_indices.cwl')
    array_ind.inputs.input_array = ['hello world', 'not', 'what world?']
    array_ind.inputs.input_indices = [0, 1]

    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    echo.inputs.message = array_ind.outputs.output_array
    echo.scatter_on(echo.inputs.message)

    steps = _steps(Workflow([array_ind, echo], 'scatter_pyapi_py').compile())

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
    `test_tool_builder.py` already owns that claim.
    """
    example = REPO_ROOT / 'examples' / 'scripts' / 'tool_builder_workflow.py'
    assert example.is_file(), f'{example} is linked from docs/tool_builder_workflow.md'
    module = import_python_file(example.stem, example.resolve())

    compiled = module.build_workflow('hello from test').compile()
    steps = _steps(compiled)

    binding = steps['read_text']['run']['outputs']['result']
    assert binding['type'] == 'string'
    assert binding['outputBinding'] == {
        'glob': 'stdout.txt', 'loadContents': True, 'outputEval': '$(self[0].contents)',
    }

    emitted = compiled.cwl_workflow['outputs']['result']
    assert emitted['type'] == 'string'
    assert emitted['outputSource'] == f"{steps['read_text']['id']}/result"
