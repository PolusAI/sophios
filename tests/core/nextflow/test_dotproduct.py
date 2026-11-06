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

from .testkit import execute_nextflow, ref, template


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
    # The entry workflow ends the file with "}\n", so appending before its
    # closing brace keeps PIPELINE.out in scope.
    script.write_text(text[:-2] + sink + "}\n", encoding="utf-8")
    return execute_nextflow(directory)


def _write_and_sink(
    process: NfProcess,
    connections: list[NfConnection],
    params: dict,
    param_types: dict[str, str],
    directory: Path,
    *,
    emit_name: str,
    sink_name: str = "gathered.txt",
) -> subprocess.CompletedProcess[str]:
    """Run one process as ``PIPELINE`` and observe its ``emit_name`` output through ``_sink``."""
    workflow = ExecutableNextflowWorkflow("PIPELINE", [process], connections, params, param_types)
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
        {"items": ["a"], "tags": ["b"]}, {"items": "string[]", "tags": "string[]"},
    )

    rendered = render_nextflow(workflow)

    assert (
        "PAIR: dotproduct scatter inputs have mismatched lengths: "
        "items=${__d0.size()}, tags=${__d1.size()}" in rendered
    )


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
    types = {"items": "string[]", "tags": "string[]", "sources": "File[]"}
    return ExecutableNextflowWorkflow("PIPELINE", [process], connections, params, types)


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_an_unscattered_file_array_declares_the_list_arity_beside_the_scattered_tuple(method: str) -> None:
    rendered = render_nextflow(_scatter_with_file_array(method, ["source_0.txt"]))
    assert (
        "tuple val(__sophios_scatter_index_9f72e), val(item), val(tag)\n"
        "    path sources, arity: '0..*', stageAs: 'gather_sources_*/*'\n"
    ) in rendered
