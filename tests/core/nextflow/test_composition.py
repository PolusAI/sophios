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


def _use_tool(file_ports: tuple[str, ...] = ("files",)) -> dict[str, Any]:
    arrays = {
        name: {"type": FILES, "inputBinding": {"position": position}}
        for position, name in enumerate(file_ports, 1)
    }
    return tool(
        "USE",
        inputs={"x": {"type": "string"}, "y": {"type": "string"}, **arrays},
        baseCommand="cat",
        stdout="used.txt",
        outputs={"used": {"type": "File", "outputBinding": {"glob": "used.txt"}}},
    )


def _pair_then_use(method: str, params: dict[str, Any]) -> ExecutableNextflowWorkflow:
    """PAIR's gathered outputs feed the unscattered ``files`` port of the scattered USE."""
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("USE", **{
            "in": {"x": "xs", "y": "ys", "files": "PAIR/out"}, "out": ["used"],
            "scatter": ["x", "y"], "scatterMethod": method,
        }),
    ]
    workflow = workflow_doc(
        steps,
        inputs={
            "as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"},
            "xs": {"type": STRINGS}, "ys": {"type": STRINGS},
        },
        outputs={"used": {"type": FILES, "outputSource": "USE/used"}},
    )
    return compiled_source_to_nextflow(synthetic_source(workflow, [_tools()[0], _use_tool()], workflow_inputs=params))


_PAIR_THEN_USE_PARAMS = {"as": ["a", "b", "c"], "bs": ["x", "y", "z"], "n": 2, "xs": ["p", "q"], "ys": ["r", "s"]}


def _two_gathered_arrays_into_use(second: str, method: str = "dotproduct") -> ExecutableNextflowWorkflow:
    """USE scatters x and y and takes two gathered File arrays of out.txt files.

    ``files`` gathers PAIR; ``more`` gathers ``second``, which is PAIR again or
    PAIR2, a second producer of out.txt over the swapped inputs.
    """
    pair = _tools()[0]
    tools = [pair]
    producers = [("PAIR", "as", "bs")]
    if second == "PAIR2":
        tools.append({**pair, "id": "PAIR2"})
        producers.append(("PAIR2", "bs", "as"))
    tools.append(_use_tool(("files", "more")))
    steps = [
        step(name, **{
            "in": {"a": a, "b": b, "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        })
        for name, a, b in producers
    ]
    steps.append(step("USE", **{
        "in": {"x": "xs", "y": "ys", "files": "PAIR/out", "more": f"{second}/out"}, "out": ["used"],
        "scatter": ["x", "y"], "scatterMethod": method,
    }))
    used = {"type": "array", "items": FILES} if method == "nested_crossproduct" else FILES
    workflow = workflow_doc(
        steps,
        inputs={
            "as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"},
            "xs": {"type": STRINGS}, "ys": {"type": STRINGS},
        },
        outputs={"used": {"type": used, "outputSource": "USE/used"}},
    )
    return compiled_source_to_nextflow(synthetic_source(workflow, tools, workflow_inputs=_PAIR_THEN_USE_PARAMS))


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
def test_a_gathered_array_of_one_file_reaches_its_consumer_as_a_list(tmp_path: Path) -> None:
    """A scatter of length one gathers one file, which Nextflow delivers as a bare Path unless the arity is declared."""
    extra = tmp_path / "g1.txt"
    extra.write_text("1\n", encoding="utf-8")
    workflow = _composed({"as": ["a"], "bs": ["b"], "n": 1, "gs": [str(extra)]})
    run = tmp_path / "run"
    write_nextflow_artifacts(workflow, run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    [combined] = list((run / "work").glob("*/*/all.txt"))
    assert combined.read_text(encoding="utf-8") == "a b 10\n1\n"


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct"])
def test_a_gathered_array_on_a_scattered_steps_unscattered_port_stages_in_numbered_directories(method: str) -> None:
    rendered = render_nextflow(_pair_then_use(method, _PAIR_THEN_USE_PARAMS))
    assert (
        "tuple val(__sophios_scatter_index_9f72e), val(x), val(y)\n"
        "    path files, arity: '0..*', stageAs: 'gather_files_*/*'\n"
    ) in rendered


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(("method", "tasks"), [("dotproduct", 2), ("flat_crossproduct", 4)])
def test_every_task_of_a_scattered_step_stages_all_the_same_named_gathered_files(
    tmp_path: Path, method: str, tasks: int
) -> None:
    run = tmp_path / "run"
    write_nextflow_artifacts(_pair_then_use(method, _PAIR_THEN_USE_PARAMS), run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    used = list((run / "work").glob("*/*/used.txt"))
    assert len(used) == tasks
    assert {path.read_text(encoding="utf-8") for path in used} == {"a x 20\nb y 20\nc z 20\n"}


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(("method", "tasks"), [("dotproduct", 2), ("flat_crossproduct", 4)])
def test_a_gathered_array_of_one_file_reaches_every_task_of_a_scattered_step_as_a_list(
    tmp_path: Path, method: str, tasks: int
) -> None:
    run = tmp_path / "run"
    write_nextflow_artifacts(_pair_then_use(method, {**_PAIR_THEN_USE_PARAMS, "as": ["a"], "bs": ["x"]}), run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    used = list((run / "work").glob("*/*/used.txt"))
    assert len(used) == tasks
    assert {path.read_text(encoding="utf-8") for path in used} == {"a x 20\n"}


@pytest.mark.fast
@pytest.mark.parametrize("second", ["PAIR", "PAIR2"])
def test_two_gathered_arrays_on_a_scattered_step_stage_under_separate_prefixes(second: str) -> None:
    rendered = render_nextflow(_two_gathered_arrays_into_use(second))
    assert (
        "    path files, arity: '0..*', stageAs: 'gather_files_*/*'\n"
        "    path more, arity: '0..*', stageAs: 'gather_more_*/*'\n"
    ) in rendered


@pytest.mark.fast
def test_two_gathered_arrays_on_an_unscattered_step_stage_under_separate_prefixes() -> None:
    pair = NfProcess(
        "PAIR",
        [NfPort("a", "val"), NfPort("b", "val")],
        [output_port("f", "out.txt"), output_port("h", "h.txt")],
        command("true"),
    )
    following = NfProcess(
        "NEXT",
        [NfPort("x", "path", is_array=True), NfPort("y", "path", is_array=True)],
        [output_port("g", "g.txt")],
        command("true"),
    )
    workflow = ExecutableNextflowWorkflow(
        "wf",
        [pair, following],
        [
            NfWorkflowInputConnection("avals", "PAIR", "a", "dotproduct"),
            NfWorkflowInputConnection("bvals", "PAIR", "b", "dotproduct"),
            NfProcessConnection("PAIR", "f", "NEXT", "x", "gather"),
            NfProcessConnection("PAIR", "h", "NEXT", "y", "gather"),
            NfWorkflowOutputConnection("NEXT", "g", "result"),
        ],
        {"avals": ["x"], "bvals": ["y"]},
    )
    rendered = render_nextflow(workflow)
    assert (
        "    path x, arity: '0..*', stageAs: 'gather_x_*/*'\n"
        "    path y, arity: '0..*', stageAs: 'gather_y_*/*'\n"
    ) in rendered


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(
    ("second", "more"),
    [("PAIR", "a x 20\nb y 20\nc z 20\n"), ("PAIR2", "x a 20\ny b 20\nz c 20\n")],
    ids=["same_producer", "second_producer"],
)
def test_a_scattered_step_stages_two_gathered_arrays_of_same_named_files(
    tmp_path: Path, second: str, more: str
) -> None:
    run = tmp_path / "run"
    write_nextflow_artifacts(_two_gathered_arrays_into_use(second), run)
    result = execute_nextflow(run)
    assert result.returncode == 0, result.stdout + result.stderr
    used = list((run / "work").glob("*/*/used.txt"))
    assert len(used) == 2
    assert {path.read_text(encoding="utf-8") for path in used} == {"a x 20\nb y 20\nc z 20\n" + more}


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
    assert "path x, arity: '0..*', stageAs: 'gather_x_*/*'" in rendered


@pytest.mark.fast
def test_a_gathered_array_feeds_a_single_input_scatter_staged_in_numbered_directories() -> None:
    steps = [
        step("PAIR", **{
            "in": {"a": "as", "b": "bs", "n": "n"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "dotproduct",
        }),
        step("USE", **{"in": {"x": "xs", "y": "y", "files": "PAIR/out"}, "out": ["used"], "scatter": ["x"]}),
    ]
    workflow = workflow_doc(
        steps,
        inputs={
            "as": {"type": STRINGS}, "bs": {"type": STRINGS}, "n": {"type": "int"},
            "xs": {"type": STRINGS}, "y": {"type": "string"},
        },
        outputs={"used": {"type": FILES, "outputSource": "USE/used"}},
    )
    params = {"as": ["a"], "bs": ["x"], "n": 2, "xs": ["p"], "y": "r"}
    rendered = render_nextflow(
        compiled_source_to_nextflow(synthetic_source(workflow, [_tools()[0], _use_tool()], workflow_inputs=params))
    )
    assert (
        "tuple val(__sophios_scatter_index_9f72e), val(x)\n"
        "    val y\n"
        "    path files, arity: '0..*', stageAs: 'gather_files_*/*'\n"
    ) in rendered
