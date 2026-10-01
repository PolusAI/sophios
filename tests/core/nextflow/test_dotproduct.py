"""Multi-input dotproduct scatter: index-paired invocations, index-ordered gather.

design_docs/sophios_nextflow_backend.md §6, Topology, "Multi-input scatter
(approved Phase 3 lowering)" and "Gather".
"""

# pylint: disable=missing-function-docstring

from pathlib import Path
import subprocess

import pytest

from sophios.input_output_nf import render_nextflow, write_nextflow_artifacts
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfCommand,
    NfConnection,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfShellLiteral,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .testkit import execute_nextflow, ref, run_nextflow, template


def _text_port(name: str, glob: str = "out.txt") -> NfPort:
    """A val output port whose captured text is the globbed file's decoded content."""
    return NfPort(name, "val", name, NfTemplate((NfLiteral(glob),)), capture="text")


def _sink(
    workflow: ExecutableNextflowWorkflow,
    directory: Path,
    *,
    emit_name: str,
    sink_name: str = "gathered.txt",
) -> subprocess.CompletedProcess[str]:
    """Write the artifacts, append a test-only sink observing the gathered emission
    order, and run. ``PIPELINE.out.<emit_name>`` is the already sorted-and-stripped
    channel produced inside the named workflow (design §6, Topology, Gather); this
    only observes, in a file, the order its items actually arrive downstream.
    """
    write_nextflow_artifacts(workflow, directory)
    script = directory / "workflow.nf"
    text = script.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    # collectFile does not preserve arrival order; toList().subscribe does,
    # since a single subscriber receives a dataflow channel's items in the
    # order they arrive and toList never reorders what it collects. Items
    # join on a record separator, not a newline, since a gathered item (S10)
    # may itself contain embedded newlines.
    sink = (
        f"\n    PIPELINE.out.{emit_name}.toList().subscribe {{ list -> "
        f"new File('{sink_name}').text = list.join('\\u0001') }}\n"
    )
    # The entry point is exactly "workflow {\n    PIPELINE(...)\n}\n" at the end
    # of the file, so appending before its closing brace keeps PIPELINE.out
    # in scope.
    script.write_text(text[:-2] + sink + "}\n", encoding="utf-8")
    return execute_nextflow(directory)


def _write_and_sink(
    process: NfProcess,
    connections: list[NfConnection],
    params: dict,
    directory: Path,
    *,
    emit_name: str,
    sink_name: str = "gathered.txt",
) -> subprocess.CompletedProcess[str]:
    """Run one process as ``PIPELINE`` and observe its ``emit_name`` output through ``_sink``."""
    workflow = ExecutableNextflowWorkflow("PIPELINE", [process], connections, params)
    return _sink(workflow, directory, emit_name=emit_name, sink_name=sink_name)


def _sleep_then_pair(*port_names: str) -> NfCommand:
    """sleep <last port> ; printf the hyphen-joined ports, to out.txt.

    The last named port doubles as an artificial per-invocation delay, so a
    runtime test can force tasks to finish out of index order and still prove
    the gathered output is in index order (design §6, Topology, Gather).
    """
    parts: list[str | NfInputReference] = [ref(port_names[0])]
    for name in port_names[1:]:
        parts += ["-", ref(name)]
    joined = template(*parts)
    return NfCommand(
        (
            template("sleep"),
            template(ref(port_names[-1])),
            NfShellLiteral(";"),
            template("printf"),
            template("%s"),
            joined,
        ),
        stdout=template("out.txt"),
    )


@pytest.mark.nextflow
@pytest.mark.serial
def test_two_input_dotproduct_pairs_by_index_and_gathers_in_order(tmp_path: Path) -> None:
    """S1: two scattered inputs pair by index; the gathered output stays in index
    order even though the tasks are forced to finish in reverse."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {"items": ["a", "b", "c"], "tags": ["2", "1", "0"]}

    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gathered = (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01")
    assert gathered == ["a-2", "b-1", "c-0"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_three_input_dotproduct_pairs_by_index_and_gathers_in_order(tmp_path: Path) -> None:
    """S2: three scattered inputs pair by index; gathered values and order both hold."""
    process = NfProcess(
        "TRIPLE",
        [NfPort("item", "val"), NfPort("tag", "val"), NfPort("note", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag", "note"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "TRIPLE", "item", "dotproduct"),
        NfWorkflowInputConnection("tags", "TRIPLE", "tag", "dotproduct"),
        NfWorkflowInputConnection("notes", "TRIPLE", "note", "dotproduct"),
        NfWorkflowOutputConnection("TRIPLE", "line", "result"),
    ]
    params = {"items": ["a", "b", "c"], "tags": ["x", "y", "z"], "notes": ["0", "0", "0"]}

    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gathered = (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01")
    assert gathered == ["a-x-0", "b-y-0", "c-z-0"]


@pytest.mark.fast
def test_mismatched_lengths_name_each_scattered_input_and_its_length() -> None:
    """S3: the rendered idiom fails the run naming each scattered input and its
    length, never truncating silently (design §6, Topology)."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag"),
    )
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [process],
        [
            NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
            NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
            NfWorkflowOutputConnection("PAIR", "line", "result"),
        ],
        {"items": ["a"], "tags": ["b"]},
    )

    rendered = render_nextflow(workflow)

    assert (
        "PAIR: dotproduct scatter inputs have mismatched lengths: "
        "items=${__d0.size()}, tags=${__d1.size()}" in rendered
    )


@pytest.mark.nextflow
@pytest.mark.serial
def test_empty_dotproduct_inputs_run_zero_tasks_and_succeed(tmp_path: Path) -> None:
    """S4: empty scattered arrays run zero invocations and the run still succeeds."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag"),
    )
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [process],
        [
            NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
            NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
            NfWorkflowOutputConnection("PAIR", "line", "result"),
        ],
        {"items": [], "tags": []},
    )

    result = run_nextflow(workflow, tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert list((tmp_path / "work").rglob("out.txt")) == []


@pytest.mark.nextflow
@pytest.mark.serial
def test_dotproduct_broadcasts_an_unscattered_scalar(tmp_path: Path) -> None:
    """S9: an unscattered value input broadcasts to every dotproduct invocation."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val"), NfPort("note", "val")],
        [_text_port("line")],
        _sleep_then_pair("item", "tag", "note"),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
        NfWorkflowInputConnection("tags", "PAIR", "tag", "dotproduct"),
        NfWorkflowInputConnection("note", "PAIR", "note"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {"items": ["a", "b"], "tags": ["x", "y"], "note": "0"}

    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gathered = (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01")
    assert gathered == ["a-x-0", "b-y-0"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_dotproduct_scatters_file_arrays_and_broadcasts_a_file(tmp_path: Path) -> None:
    """S10: two File-array inputs pair by index; a broadcast File reaches every task."""
    first_a = tmp_path / "first_a.txt"
    first_b = tmp_path / "first_b.txt"
    second_a = tmp_path / "second_a.txt"
    second_b = tmp_path / "second_b.txt"
    reference = tmp_path / "reference.txt"
    first_a.write_text("firstA\n", encoding="utf-8")
    first_b.write_text("firstB\n", encoding="utf-8")
    second_a.write_text("secondA\n", encoding="utf-8")
    second_b.write_text("secondB\n", encoding="utf-8")
    reference.write_text("shared\n", encoding="utf-8")

    process = NfProcess(
        "PAIR",
        [NfPort("first", "path"), NfPort("second", "path"), NfPort("reference", "path")],
        [_text_port("line", "combined.txt")],
        NfCommand(
            (
                template("cat"),
                template(ref("first")),
                template(ref("second")),
                template(ref("reference")),
            ),
            stdout=template("combined.txt"),
        ),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("firsts", "PAIR", "first", "dotproduct"),
        NfWorkflowInputConnection("seconds", "PAIR", "second", "dotproduct"),
        NfWorkflowInputConnection("reference", "PAIR", "reference"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {
        "firsts": [
            {"class": "File", "path": str(first_a)},
            {"class": "File", "path": str(first_b)},
        ],
        "seconds": [
            {"class": "File", "path": str(second_a)},
            {"class": "File", "path": str(second_b)},
        ],
        "reference": {"class": "File", "path": str(reference)},
    }

    result = _write_and_sink(process, connections, params, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gathered = (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01")
    assert gathered == ["firstA\nsecondA\nshared\n", "firstB\nsecondB\nshared\n"]


def _scatter_with_file_array(method: str, sources: list[str]) -> ExecutableNextflowWorkflow:
    """Two scattered inputs and a File array that stays outside the scattered tuple."""
    process = NfProcess(
        "PAIR",
        [NfPort("item", "val"), NfPort("tag", "val"), NfPort("sources", "path", is_array=True)],
        [_text_port("line", "combined.txt")],
        NfCommand(
            (template("echo"), template(ref("item")), template(ref("tag")), NfArrayBinding("sources")),
            stdout=template("combined.txt"),
        ),
    )
    connections: list[NfConnection] = [
        NfWorkflowInputConnection("items", "PAIR", "item", method),
        NfWorkflowInputConnection("tags", "PAIR", "tag", method),
        NfWorkflowInputConnection("sources", "PAIR", "sources"),
        NfWorkflowOutputConnection("PAIR", "line", "result"),
    ]
    params = {"items": ["a", "b"], "tags": ["x", "y"], "sources": sources}
    return ExecutableNextflowWorkflow("PIPELINE", [process], connections, params)


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_an_unscattered_file_array_declares_the_list_arity_beside_the_scattered_tuple(method: str) -> None:
    rendered = render_nextflow(_scatter_with_file_array(method, ["source_0.txt"]))
    assert (
        "tuple val(__sophios_scatter_index_9f72e), val(item), val(tag)\n    path sources, arity: '0..*'\n"
    ) in rendered


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize("names", [[], ["source_0.txt"]], ids=["empty", "one-element"])
def test_dotproduct_broadcasts_an_unscattered_file_array_as_a_list(names: list[str], tmp_path: Path) -> None:
    """An array port outside the scattered tuple still receives a list of zero or one files."""
    for name in names:
        (tmp_path / name).write_text("shared\n", encoding="utf-8")

    workflow = _scatter_with_file_array("dotproduct", [str(tmp_path / name) for name in names])

    result = _sink(workflow, tmp_path, emit_name="result")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gathered = (tmp_path / "gathered.txt").read_text(encoding="utf-8").split("\x01")
    suffix = "".join(f" {name}" for name in names)
    assert gathered == [f"a x{suffix}\n", f"b y{suffix}\n"]
