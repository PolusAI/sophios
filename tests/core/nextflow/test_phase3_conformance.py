"""Phase 3 cells no narrower test executes: a real length mismatch, and every predicate kind.

design_docs/sophios_nextflow_backend.md §6, Expressions and Topology.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_types import (
    ExecutableNextflowWorkflow, NfPort, NfProcess, NfWorkflowInputConnection, NfWorkflowOutputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import execute_nextflow
from .test_dotproduct import _sleep_then_pair, _text_port


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_dotproduct_length_mismatch_fails_the_run_naming_each_input(tmp_path: Path) -> None:
    """S3: the run fails, naming each scattered input and its length; nothing runs truncated."""
    process = NfProcess("STEP", [NfPort("item", "val"), NfPort("tag", "val")], [_text_port("line")],
                        _sleep_then_pair("item", "tag"))
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE", [process],
        [*(NfWorkflowInputConnection(f"{name}s", "STEP", name, "dotproduct") for name in ("item", "tag")),
         NfWorkflowOutputConnection("STEP", "line", "line")],
        {"items": ["a", "b"], "tags": ["0"]}, {"items": "string[]", "tags": "string[]"},
    )
    write_nextflow_artifacts(workflow, tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode != 0
    assert "STEP: dotproduct scatter inputs have mismatched lengths: items=2, tags=1" in result.stdout + result.stderr
    assert not list((tmp_path / "work").glob("*/*/.command.sh"))


PREDICATES = {
    "$(true)": True,
    "$(false)": False,
    "$(inputs.flag)": True,
    "$(inputs.s === 'x')": True,
    "$(inputs.s == 'y')": False,
    "$(inputs.flag && !(inputs.n < 0) || false)": True,
    "$(Math.round(inputs.n / 2) >= 2)": True,
    "$(inputs.maybe === null)": True,
    "$(inputs.maybe !== null)": False,
}


@pytest.mark.nextflow
@pytest.mark.serial
def test_every_predicate_kind_decides_under_nextflow(tmp_path: Path) -> None:
    """W1-W7: literals, boolean input, equality, logic, calculator operand, and null checks execute."""
    # pylint: disable=import-outside-toplevel
    from .testkit import step, synthetic_source, tool, workflow_doc
    declared = {"flag": {"type": "boolean"}, "s": {"type": "string"}, "n": {"type": "int"}, "maybe": {"type": "int?"}}
    tools, steps, outputs = [], [], {}
    for index, predicate in enumerate(PREDICATES):
        name = f"P{index}"
        tools.append(tool(
            name, inputs=declared, baseCommand="touch", arguments=[f"ran_{index}.txt"],
            outputs={"out": {"type": "File", "outputBinding": {"glob": f"ran_{index}.txt"}}},
        ))
        steps.append(step(name, **{"in": {key: key for key in declared}, "out": ["out"], "when": predicate}))
        outputs[f"out_{index}"] = {"type": "File?", "outputSource": f"{name}/out"}
    workflow = workflow_doc(steps, inputs={key: value["type"] for key, value in declared.items()}, outputs=outputs)
    source = synthetic_source(workflow, tools, workflow_inputs={"flag": True, "s": "x", "n": 3})
    write_nextflow_artifacts(compiled_source_to_nextflow(source), tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = {path.name for path in (tmp_path / "work").glob("*/*/ran_*.txt")}
    assert ran == {f"ran_{index}.txt" for index, expected in enumerate(PREDICATES.values()) if expected}
