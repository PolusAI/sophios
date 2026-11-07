"""Multi-input nested_crossproduct scatter: one array dimension per scattered input (design §6, Topology)."""

# pylint: disable=missing-function-docstring

import pytest

from sophios.input_output_nf import render_nextflow
from sophios.nf_types import NF_NEST_HELPER, ExecutableNextflowWorkflow

from .test_composition import FILES, STRINGS, _tools
from .testkit import lowered_case, step


@pytest.mark.fast
def test_a_nested_result_feeding_a_later_step_is_rejected_by_name() -> None:
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": [{"out": {"wic_anchor": "pair_out"}}],
            "scatter": ["a", "b"], "scatterMethod": "nested_crossproduct",
        }),
        step("CONCAT", **{"in": {"files": {"wic_alias": "pair_out"}}, "out": ["all"]}),
    ]
    workflow = {
        "steps": steps,
        "inputs": {"as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"}},
        "outputs": {"all": {"type": "File", "outputSource": "CONCAT/all"}},
    }
    pair, _join, concat = _tools()
    # A two-input nested result is a File[][], which a File[] port cannot take (wic023).
    tools = [pair, {**concat, "inputs": {"files": {"type": {"type": "array", "items": FILES}, "inputBinding": {
        "position": 1}}}}]
    message = "'PAIR/out' is a nested_crossproduct result, which reaches workflow outputs only"
    with pytest.raises(ValueError, match=message):
        lowered_case({"workflow": workflow, "tools": tools, "job": {"as": ["a"], "bs": ["b"], "n": 1}})


def _single_input_join(**method: str) -> ExecutableNextflowWorkflow:
    """JOIN scatters over PAIR's gathered output alone, and CONCAT gathers JOIN's."""
    steps = [
        step("PAIR", **{
            "in": {"a": "xs", "b": "ys", "n": "n"}, "out": [{"out": {"wic_anchor": "pair_out"}}],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("JOIN", **{"in": {"f": {"wic_alias": "pair_out"}, "g": "g"}, "out": [{"out": {"wic_anchor": "join_out"}}],
                        "scatter": ["f"], **method}),
        step("CONCAT", **{"in": {"files": {"wic_alias": "join_out"}}, "out": ["all"]}),
    ]
    workflow = {
        "steps": steps,
        "inputs": {"xs": {"type": STRINGS}, "ys": {"type": STRINGS}, "n": {"type": "int"}, "g": {"type": "File"}},
        "outputs": {"all": {"type": "File", "outputSource": "CONCAT/all"}},
    }
    params = {"xs": ["a"], "ys": ["b"], "n": 1, "g": "/tmp/g"}
    return lowered_case({"workflow": workflow, "tools": _tools(), "job": params})[0]


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_a_single_input_scatter_over_a_gathered_source_lowers_alike_under_each_method(method: str) -> None:
    """At one scattered input the three methods coincide, as over a workflow input (design §6, Topology)."""
    assert _single_input_join(scatterMethod=method).connections == _single_input_join().connections


def _gathered_into_nested_join(second: str) -> ExecutableNextflowWorkflow:
    """JOIN nests over PAIR's gathered outputs ``f`` and ``second``: PAIR's again or the ``gs`` input."""
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": [{"out": {"wic_anchor": "pair_out"}}],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("JOIN", **{
            "in": {"f": {"wic_alias": "pair_out"}, "g": {"wic_alias": "pair_out"} if second == "PAIR/out" else second},
            "out": ["out"], "scatter": ["f", "g"], "scatterMethod": "nested_crossproduct",
        }),
    ]
    inputs = {"as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"}}
    params: dict[str, object] = {"as": ["a", "b"], "bs": ["x", "y"], "n": 1}
    if second == "gs":
        inputs["gs"] = {"type": FILES}
        params["gs"] = ["/tmp/g1", "/tmp/g2", "/tmp/g3"]
    workflow = {
        "steps": steps,
        "inputs": inputs,
        "outputs": {"all": {"type": {"type": "array", "items": FILES}, "outputSource": "JOIN/out"}},
    }
    tools = [tool for tool in _tools() if tool["id"] != "CONCAT"]
    return lowered_case({"workflow": workflow, "tools": tools, "job": params})[0]


@pytest.mark.fast
@pytest.mark.parametrize("second", ["PAIR/out", "gs"], ids=["all_gathered", "one_gathered"])
def test_a_nested_step_over_gathered_arrays_regroups_its_output_by_the_recorded_shape(second: str) -> None:
    rendered = render_nextflow(_gathered_into_nested_join(second))
    assert f"def {NF_NEST_HELPER}(List flat, List shape) {{" in rendered
    assert (
        "all = _JOIN.out.out.toSortedList { it[0] }.map { [it.collect { row -> row[1] }] }"
        f".combine(ch__JOIN_shape).flatMap {{ flat, shape -> {NF_NEST_HELPER}(flat, shape) }}"
    ) in rendered
