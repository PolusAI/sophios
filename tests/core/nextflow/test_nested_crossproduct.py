"""Multi-input nested_crossproduct scatter: one array dimension per scattered input.

design_docs/sophios_nextflow_backend.md §6, Topology, "Multi-input scatter"
and "Gather".
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.input_output_nf import render_nextflow, write_nextflow_artifacts
from sophios.nf_types import (
    NF_NEST_HELPER,
    ExecutableNextflowWorkflow,
    NfConnection,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .test_composition import FILES, STRINGS, _tools, _two_gathered_arrays_into_use
from .test_dotproduct import _sink, _sleep_then_pair, _text_port, _write_and_sink
from .testkit import command, execute_nextflow, output_port, step, synthetic_source, workflow_doc


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


def _gathered_into_nested_join(second: str) -> ExecutableNextflowWorkflow:
    """JOIN nests over PAIR's gathered outputs ``f`` and ``second``: PAIR's again or the ``gs`` input."""
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("JOIN", **{
            "in": {"f": "PAIR/out", "g": second}, "out": ["out"],
            "scatter": ["f", "g"], "scatterMethod": "nested_crossproduct",
        }),
    ]
    inputs = {"as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"}}
    params: dict[str, object] = {"as": ["a", "b"], "bs": ["x", "y"], "n": 1}
    if second == "gs":
        inputs["gs"] = {"type": FILES}
        params["gs"] = ["/tmp/g1", "/tmp/g2", "/tmp/g3"]
    workflow = workflow_doc(
        steps,
        inputs=inputs,
        outputs={"all": {"type": {"type": "array", "items": FILES}, "outputSource": "JOIN/out"}},
    )
    tools = [tool for tool in _tools() if tool["id"] != "CONCAT"]
    return compiled_source_to_nextflow(synthetic_source(workflow, tools, workflow_inputs=params))


@pytest.mark.fast
@pytest.mark.parametrize("second", ["PAIR/out", "gs"], ids=["all_gathered", "one_gathered"])
def test_a_nested_step_over_gathered_arrays_regroups_its_output_by_the_recorded_shape(second: str) -> None:
    rendered = render_nextflow(_gathered_into_nested_join(second))
    assert f"def {NF_NEST_HELPER}(List flat, List shape) {{" in rendered
    assert (
        "all = JOIN.out.out.toSortedList { it[0] }.map { [it.collect { row -> row[1] }] }"
        f".combine(ch_JOIN_shape).flatMap {{ flat, shape -> {NF_NEST_HELPER}(flat, shape) }}"
    ) in rendered


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(
    ("second", "expected"),
    [
        ("gathered", ["[a-1a-1, a-1b-0]", "[b-0a-1, b-0b-0]"]),
        ("workflow_input", ["[a-1x, a-1y, a-1z]", "[b-0x, b-0y, b-0z]"]),
    ],
)
def test_a_nested_step_over_gathered_arrays_emits_one_array_per_outer_element(
    tmp_path: Path, second: str, expected: list[str]
) -> None:
    """PAIR's two files, finishing out of order, are rescattered: the first input is the outer dimension."""
    pair = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("delay", "val")],
        [output_port("line", "out.txt")],
        _sleep_then_pair("item", "delay"),
    )
    # Renamed on staging: every gathered file is called out.txt.
    join = NfProcess(
        "JOIN",
        [NfPort("f", "path", stage_as="f.txt"), NfPort("g", "path", stage_as="g.txt")],
        [_text_port("line", "joined.txt")],
        command("cat", "f.txt", "g.txt", stdout="joined.txt"),
    )
    params: dict[str, list[str]] = {"items": ["a", "b"], "delays": ["1", "0"]}
    g_connection: NfConnection = NfProcessConnection("PAIR", "line", "JOIN", "g", "nested_crossproduct")
    if second == "workflow_input":
        for name in "xyz":
            (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
        params["gs"] = [str(tmp_path / f"{name}.txt") for name in "xyz"]
        g_connection = NfWorkflowInputConnection("gs", "JOIN", "g", "nested_crossproduct")
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("delays", "PAIR", "delay", "dotproduct"),
        NfProcessConnection("PAIR", "line", "JOIN", "f", "nested_crossproduct"),
        g_connection,
        NfWorkflowOutputConnection("JOIN", "line", "all"),
    ]
    run = tmp_path / "run"
    result = _sink(ExecutableNextflowWorkflow("PIPELINE", [pair, join], connections, params), run, emit_name="all")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (run / "gathered.txt").read_text(encoding="utf-8").split("\x01") == expected
