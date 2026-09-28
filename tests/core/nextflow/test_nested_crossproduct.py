"""Multi-input nested_crossproduct scatter: one array dimension per scattered input.

design_docs/sophios_nextflow_backend.md §6, Topology, "Multi-input scatter"
and "Gather".
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_types import NfConnection, NfPort, NfProcess, NfWorkflowInputConnection, NfWorkflowOutputConnection

from .test_composition import STRINGS, _tools, _two_gathered_arrays_into_use
from .test_dotproduct import _sleep_then_pair, _text_port, _write_and_sink
from .testkit import execute_nextflow, step, synthetic_source, workflow_doc


def _run(names: list[str], params: dict[str, list[str]], directory: Path) -> list[str]:
    process = NfProcess("NEST", [NfPort(name, "val") for name in names], [_text_port("line")], _sleep_then_pair(*names))
    connections: list[NfConnection] = [
        NfWorkflowInputConnection(f"{name}s", "NEST", name, "nested_crossproduct") for name in names
    ]
    connections.append(NfWorkflowOutputConnection("NEST", "line", "line"))
    result = _write_and_sink(process, connections, params, directory, emit_name="line")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return (directory / "gathered.txt").read_text(encoding="utf-8").split("\x01")


@pytest.mark.nextflow
@pytest.mark.serial
def test_two_input_nested_crossproduct_value_shape(tmp_path: Path) -> None:
    """S7: one outer element per first input, each an array over the second, in order."""
    params = {"items": ["a", "b"], "delays": ["1", "0", "0"]}
    assert _run(["item", "delay"], params, tmp_path) == ["[a-1, a-0, a-0]", "[b-1, b-0, b-0]"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_three_input_nested_crossproduct_rank_order_and_empty_dimension(tmp_path: Path) -> None:
    """S8: rank three in declared-input order; an empty inner dimension keeps its outer shape."""
    names = ["item", "tag", "delay"]
    params = {"items": ["a", "b"], "tags": ["x", "y"], "delays": ["0"]}
    assert _run(names, params, tmp_path / "full") == ["[[a-x-0], [a-y-0]]", "[[b-x-0], [b-y-0]]"]
    params = {"items": ["a", "b"], "tags": [], "delays": ["0"]}
    assert _run(names, params, tmp_path / "empty") == ["[]", "[]"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_nested_step_stages_two_gathered_arrays_of_same_named_files(tmp_path: Path) -> None:
    run = tmp_path / "run"
    write_nextflow_artifacts(_two_gathered_arrays_into_use("PAIR2", "nested_crossproduct"), run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    used = list((run / "work").glob("*/*/used.txt"))
    assert len(used) == 4
    expected = "a x 20\nb y 20\nc z 20\nx a 20\ny b 20\nz c 20\n"
    assert {path.read_text(encoding="utf-8") for path in used} == {expected}


@pytest.mark.fast
def test_a_nested_result_feeding_a_later_step_is_rejected_by_name() -> None:
    from sophios.utils_nf import compiled_source_to_nextflow  # pylint: disable=import-outside-toplevel
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "nested_crossproduct",
        }),
        step("CONCAT", **{"in": {"files": "PAIR/out"}, "out": ["all"]}),
    ]
    workflow = workflow_doc(
        steps,
        inputs={"as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"}},
        outputs={"all": {"type": "File", "outputSource": "CONCAT/all"}},
    )
    tools = [tool for tool in _tools() if tool["id"] != "JOIN"]
    message = "'PAIR/out' is a nested_crossproduct result, which reaches workflow outputs only"
    with pytest.raises(ValueError, match=message):
        compiled_source_to_nextflow(
            synthetic_source(workflow, tools, workflow_inputs={"as": ["a"], "bs": ["b"], "n": 1})
        )
