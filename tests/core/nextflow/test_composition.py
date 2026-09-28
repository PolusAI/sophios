"""Collection and conditional composition over indexed scatters.

design_docs/sophios_nextflow_backend.md §6, Topology, "Multi-input scatter",
"Gather", and "Conditional steps".
"""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any

import pytest

from sophios.input_output_nf import render_nextflow, write_nextflow_artifacts
from sophios.nf_expr import parse
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfConnection,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .test_dotproduct import _sleep_then_pair, _text_port, _write_and_sink
from .testkit import command, execute_nextflow, output_port, step, synthetic_source, tool, workflow_doc

STRINGS = {"type": "array", "items": "string"}
FILES = {"type": "array", "items": "File"}
FILE_OUT = {"type": "File", "outputBinding": {"glob": "out.txt"}}


def _tools() -> list[dict[str, Any]]:
    pair = tool(
        "PAIR",
        inputs={
            "a": {"type": "string", "inputBinding": {"position": 1}},
            "b": {"type": "string", "inputBinding": {"position": 2}},
            "n": {"type": "int"},
        },
        arguments=[{"position": 3, "valueFrom": "$(inputs.n * 10)"}],
        baseCommand="echo",
        stdout="out.txt",
        outputs={"out": FILE_OUT},
    )
    join = tool(
        "JOIN",
        inputs={
            "f": {"type": "File", "inputBinding": {"position": 1}},
            "g": {"type": "File", "inputBinding": {"position": 2}},
        },
        baseCommand="cat",
        # Not out.txt: Nextflow stages inputs in the task directory, so an
        # output named like a staged input would truncate it first.
        stdout="joined.txt",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "joined.txt"}}},
    )
    concat = tool(
        "CONCAT",
        inputs={"files": {"type": FILES, "inputBinding": {"position": 1}}},
        baseCommand="cat",
        stdout="all.txt",
        outputs={"all": {"type": "File", "outputBinding": {"glob": "all.txt"}}},
    )
    return [pair, join, concat]


def _composed(params: dict[str, Any]) -> Any:
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("JOIN", **{
            "in": {"f": "PAIR/out", "g": "gs"}, "out": ["out"],
            "scatter": ["f", "g"], "scatterMethod": "dotproduct",
        }),
        step("CONCAT", **{"in": {"files": "JOIN/out"}, "out": ["all"]}),
    ]
    workflow = workflow_doc(
        steps,
        inputs={"as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"}, "gs": {"type": FILES}},
        outputs={"all": {"type": "File", "outputSource": "CONCAT/all"}},
    )
    return compiled_source_to_nextflow(synthetic_source(workflow, _tools(), workflow_inputs=params))


@pytest.mark.nextflow
@pytest.mark.serial
def test_gathered_scatter_rescatters_and_gathers_again_in_order(tmp_path: Path) -> None:
    """S12, S13, S14: calculator binding, dotproduct, gather into a later scatter, gather into one step."""
    gs = []
    for digit in "123":
        path = tmp_path / f"g{digit}.txt"
        path.write_text(f"{digit}\n", encoding="utf-8")
        gs.append(str(path))
    workflow = _composed({"as": ["a", "b", "c"], "bs": ["x", "y", "z"], "n": 2, "gs": gs})
    run = tmp_path / "run"
    write_nextflow_artifacts(workflow, run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    [combined] = list((run / "work").glob("*/*/all.txt"))
    assert combined.read_text(encoding="utf-8") == "a x 20\n1\nb y 20\n2\nc z 20\n3\n"


@pytest.mark.nextflow
@pytest.mark.serial
def test_per_combination_when_keeps_null_at_skipped_positions(tmp_path: Path) -> None:
    """W10: the condition runs per invocation; the gather keeps the sentinel in place."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag"),
        condition=parse("$(inputs.tag !== '1')"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {"items": ["a", "b", "c"], "tags": ["2", "1", "0"]}
    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01") == ["a-2", "[]", "c-0"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_per_combination_when_reads_a_scattered_and_a_broadcast_port(tmp_path: Path) -> None:
    """The condition sees each invocation's scattered element and the broadcast value."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val"), NfPort("skipped", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag"),
        condition=parse("$(inputs.tag !== inputs.skipped)"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
        NfWorkflowInputConnection("skipped", "PAIR", "skipped"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {"items": ["a", "b", "c"], "tags": ["2", "1", "0"], "skipped": "1"}
    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01") == ["a-2", "[]", "c-0"]


@pytest.mark.fast
def test_a_condition_on_a_broadcast_port_branches_the_indexed_scatter_and_indexes_the_skip() -> None:
    process = NfProcess(
        "PAIR",
        [NfPort("a", "val"), NfPort("b", "val"), NfPort("c", "val")],
        [output_port("f", "out.txt")],
        command("true"),
        condition=parse("$(inputs.c == 'go')"),
    )
    workflow = ExecutableNextflowWorkflow(
        "wf",
        [process],
        [
            NfWorkflowInputConnection("avals", "PAIR", "a", "dotproduct"),
            NfWorkflowInputConnection("bvals", "PAIR", "b", "dotproduct"),
            NfWorkflowInputConnection("c", "PAIR", "c"),
            NfWorkflowOutputConnection("PAIR", "f", "result"),
        ],
        {"avals": ["x"], "bvals": ["y"], "c": "go"},
    )
    rendered = render_nextflow(workflow)
    # One closure parameter per tuple element: index, two scattered, one broadcast.
    assert "ch_PAIR_scatter.combine(c.map { [it] }).branch { __i, __w0, __w1, __b0 ->" in rendered
    assert "run: (__b0 == 'go')" in rendered
    assert (
        "PAIR(ch_PAIR_branch.run.map { __i, __w0, __w1, __b0 -> tuple(__i, __w0, __w1) }, "
        "ch_PAIR_branch.run.map { __i, __w0, __w1, __b0 -> __b0 })"
    ) in rendered
    assert "ch_PAIR_branch.skip.map { __i, __w0, __w1, __b0 -> tuple(__i, []) }" in rendered
    assert "tuple val(__sophios_scatter_index_9f72e), val(a), val(b)\n    val c\n" in rendered


@pytest.mark.fast
def test_one_output_of_a_scattered_step_gathers_into_a_step_while_another_reaches_the_workflow() -> None:
    pair = NfProcess(
        "PAIR",
        [NfPort("a", "val"), NfPort("b", "val")],
        [output_port("f", "out.txt"), output_port("h", "h.txt")],
        command("true"),
    )
    following = NfProcess("NEXT", [NfPort("x", "path", is_array=True)], [output_port("g", "g.txt")], command("true"))
    workflow = ExecutableNextflowWorkflow(
        "wf",
        [pair, following],
        [
            NfWorkflowInputConnection("avals", "PAIR", "a", "dotproduct"),
            NfWorkflowInputConnection("bvals", "PAIR", "b", "dotproduct"),
            NfProcessConnection("PAIR", "h", "NEXT", "x", "gather"),
            NfWorkflowOutputConnection("PAIR", "f", "pairs"),
            NfWorkflowOutputConnection("NEXT", "g", "result"),
        ],
        {"avals": ["x"], "bvals": ["y"]},
    )
    rendered = render_nextflow(workflow)
    assert "NEXT(PAIR.out.h.toSortedList { it[0] }.map { it.collect { row -> row[1] } })" in rendered
    assert "pairs = PAIR.out.f.toSortedList { it[0] }.flatMap { it.collect { row -> row[1] } }" in rendered
    assert "path x, stageAs: 'gather*/*'" in rendered


@pytest.mark.fast
def test_gathering_a_single_input_scatter_is_rejected_by_name() -> None:
    tools = _tools()
    steps = [
        step("JOIN", **{"in": {"f": "fs", "g": "g"}, "out": ["out"], "scatter": ["f"]}),
        step("CONCAT", **{"in": {"files": "JOIN/out"}, "out": ["all"]}),
    ]
    workflow = workflow_doc(
        steps,
        inputs={"fs": {"type": FILES}, "g": {"type": "File"}},
        outputs={"all": {"type": "File", "outputSource": "CONCAT/all"}},
    )
    with pytest.raises(ValueError, match="gathering a single-input scatter is not supported yet"):
        compiled_source_to_nextflow(
            synthetic_source(workflow, tools[1:], workflow_inputs={"fs": ["/tmp/a"], "g": "/tmp/b"})
        )
