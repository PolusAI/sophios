"""Multi-input dotproduct scatter: index-paired invocations, index-ordered gather (design §6, Topology)."""

# pylint: disable=missing-function-docstring

import pytest

from sophios.input_output_nf import render_nextflow
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfCommand,
    NfConnection,
    NfInputReference,
    NfPort,
    NfProcess,
    NfShellLiteral,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .testkit import ref, template


def _text_port(name: str, glob: str = "out.txt") -> NfPort:
    """A val output port whose captured text is the globbed file's decoded content."""
    return NfPort(name, "val", name, template(glob), capture="text")


def _sleep_then_pair(*port_names: str) -> NfCommand:
    """sleep <last port> ; printf the hyphen-joined ports, to out.txt; the sleep reorders task completion."""
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
