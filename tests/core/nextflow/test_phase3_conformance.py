"""Phase 3 cells no narrower test executes: calculator calls from Python, a real mismatch.

design_docs/sophios_nextflow_backend.md §6, Expressions and Topology.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import execute_nextflow
from .test_reader_symmetry import _scattered

CALLS = [
    "$(Math.pow(inputs.x, 2))",
    "$(Math.sqrt(inputs.x))",
    "$(Math.abs(0 - inputs.x))",
    "$(Math.floor(inputs.x))",
    "$(Math.ceil(inputs.x))",
    "$(Math.min(inputs.x, 1, 2))",
]


@pytest.mark.nextflow
@pytest.mark.serial
def test_calculator_calls_from_the_python_api_match_javascript(tmp_path: Path) -> None:
    """A5, A7, A8, A9, A10: expected text is node's value as cwltool prints it."""
    calc = CommandLineTool("calc", Inputs(x=Input(cwl.float)), Outputs(result=Output(cwl.file, glob="out.txt")))
    calc = calc.base_command("echo").stdout("out.txt")
    for position, text in enumerate(CALLS, start=1):
        calc = calc.argument(text, position=position)
    step = Step(calc, step_name="calc")
    step.inputs.x = 2.25
    workflow = compiled_source_to_nextflow(compile_workflow_result(Workflow([step], "root")))
    write_nextflow_artifacts(workflow, tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    [output] = list((tmp_path / "work").glob("*/*/out.txt"))
    assert output.read_text(encoding="utf-8") == "5.0625 1.5 2.25 2 3 1\n"


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_dotproduct_length_mismatch_fails_the_run_naming_each_input(tmp_path: Path) -> None:
    """S3: the run fails, naming each scattered input and its length; nothing runs truncated."""
    workflow = _scattered("dotproduct")
    params = {"items": ["a", "b"], "tags": ["0"]}
    workflow = type(workflow)(workflow.name, workflow.processes, workflow.connections, params)
    write_nextflow_artifacts(workflow, tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode != 0
    assert "STEP: dotproduct scatter inputs have mismatched lengths: items=2, tags=1" in result.stdout + result.stderr
    assert not list((tmp_path / "work").glob("*/*/.command.sh"))
