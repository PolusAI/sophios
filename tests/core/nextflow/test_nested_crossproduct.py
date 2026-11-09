"""Multi-input nested_crossproduct scatter: one array dimension per scattered input (design §6, Topology)."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.input_output_nf import render_nextflow, write_nextflow_artifacts
from sophios.nf_types import (
    NF_NEST_HELPER,
    ExecutableNextflowWorkflow,
    NfConnection,
    NfEntry,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .test_composition import FILES, STRINGS, _tools, _two_gathered_arrays_into_use
from .test_dotproduct import _sleep_then_pair, _text_port
from .testkit import (
    assert_succeeded, command, execute_nextflow, output_port, run_observed, step, synthetic_source, workflow_doc,
)


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


def _single_input_join(**method: str) -> ExecutableNextflowWorkflow:
    """JOIN scatters over PAIR's gathered output alone, and CONCAT gathers JOIN's."""
    steps = [
        step("PAIR", **{
            "in": {"a": "xs", "b": "ys", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("JOIN", **{"in": {"f": "PAIR/out", "g": "g"}, "out": ["out"], "scatter": ["f"], **method}),
        step("CONCAT", **{"in": {"files": "JOIN/out"}, "out": ["all"]}),
    ]
    workflow = workflow_doc(
        steps,
        inputs={"xs": {"type": STRINGS}, "ys": {"type": STRINGS}, "n": {"type": "int"}, "g": {"type": "File"}},
        outputs={"all": {"type": "File", "outputSource": "CONCAT/all"}},
    )
    params = {"xs": ["a"], "ys": ["b"], "n": 1, "g": "/tmp/g"}
    return compiled_source_to_nextflow(synthetic_source(workflow, _tools(), workflow_inputs=params))


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_a_single_input_scatter_over_a_gathered_source_lowers_alike_under_each_method(method: str) -> None:
    """At one scattered input the three methods coincide, as over a workflow input (design §6, Topology)."""
    assert _single_input_join(scatterMethod=method).connections == _single_input_join().connections


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
        "all = _JOIN.out.out.toSortedList { it[0] }.map { [it.collect { row -> row[1] }] }"
        f".combine(ch__JOIN_shape).flatMap {{ flat, shape -> {NF_NEST_HELPER}(flat, shape) }}"
    ) in rendered


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_nested_step_over_one_gathered_array_twice_emits_one_array_per_outer_element(tmp_path: Path) -> None:
    """PAIR's two files, finishing out of order, are rescattered twice: the first input is the outer dimension. Each
    is an entry under its own name, as every gathered file is called out.txt (test_runtime.py's scatter property
    rescatters one gathered array beside an input array)."""
    pair = NfProcess("PAIR", [NfPort("item", "val"), NfPort("delay", "val")], [output_port("line", "out.txt")],
                     _sleep_then_pair("item", "delay"))
    join = NfProcess(
        "JOIN",
        [NfPort("f", "path", entry=NfEntry("f.txt")), NfPort("g", "path", entry=NfEntry("g.txt"))],
        [_text_port("line", "joined.txt")],
        command("cat", "f.txt", "g.txt", stdout="joined.txt"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("delays", "PAIR", "delay", "dotproduct"),
        *(NfProcessConnection("PAIR", "line", "JOIN", port, "nested_crossproduct") for port in "fg"),
        NfWorkflowOutputConnection("JOIN", "line", "all"),
    ]
    workflow = ExecutableNextflowWorkflow("PIPELINE", [pair, join], connections, {"items": ["a", "b"], "delays": [
        "1", "0"]}, {"items": "string[]", "delays": "string[]"})
    result, observed = run_observed(workflow, tmp_path / "run", emits=["all"])
    assert_succeeded(result)
    assert observed["all"] == ["[a-1a-1, a-1b-0]", "[b-0a-1, b-0b-0]"]
