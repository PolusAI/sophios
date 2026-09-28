"""Multi-input flat_crossproduct scatter: CWL-ordered Cartesian invocations.

design_docs/sophios_nextflow_backend.md §6, Topology, "Multi-input scatter
(approved Phase 3 lowering)" and "Gather".
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.nf_types import (
    NfConnection,
    NfPort,
    NfProcess,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .test_dotproduct import _sleep_then_pair, _text_port, _write_and_sink
from .testkit import step, synthetic_source, tool, workflow_doc


def _flat(names: list[str], outputs: list[NfPort] | None = None) -> NfProcess:
    return NfProcess(
        "CROSS",
        [NfPort(name, "val") for name in names],
        outputs or [_text_port("line")],
        _sleep_then_pair(*names),
    )


def _connections(names: list[str], emits: list[str]) -> list[NfConnection]:
    connections: list[NfConnection] = [
        NfWorkflowInputConnection(f"{name}s", "CROSS", name, "flat_crossproduct") for name in names
    ]
    connections.extend(NfWorkflowOutputConnection("CROSS", emit, emit) for emit in emits)
    return connections


def _gathered(directory: Path, name: str = "gathered.txt") -> list[str]:
    return (directory / name).read_text(encoding="utf-8").split("\x01")


@pytest.mark.fast
def test_flat_crossproduct_lowers_from_cwl_to_its_adapter() -> None:
    cross = tool(
        "CROSS",
        inputs={"a": {"type": "string"}, "b": {"type": "string"}},
        stdout="out.txt",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    workflow = workflow_doc(
        [step("CROSS", **{
            "in": {"a": "as", "b": "bs"}, "out": ["out"],
            "scatter": ["a", "b"], "scatterMethod": "flat_crossproduct",
        })],
        inputs={name: {"type": {"type": "array", "items": "string"}} for name in ("as", "bs")},
        outputs={"out": {"type": {"type": "array", "items": "File"}, "outputSource": "CROSS/out"}},
    )
    lowered = compiled_source_to_nextflow(
        synthetic_source(workflow, [cross], workflow_inputs={"as": ["x"], "bs": ["y"]})
    )
    adapters = {
        connection.adapter for connection in lowered.connections if isinstance(connection, NfWorkflowInputConnection)
    }
    assert adapters == {"flat_crossproduct"}


@pytest.mark.nextflow
@pytest.mark.serial
def test_two_input_flat_crossproduct_values_and_cwl_order(tmp_path: Path) -> None:
    """S5: first declared input outermost, gathered in that order despite reverse finishing."""
    names = ["item", "delay"]
    params = {"items": ["a", "b"], "delays": ["1", "0"]}
    result = _write_and_sink(_flat(names), _connections(names, ["line"]), params, tmp_path, emit_name="line")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _gathered(tmp_path) == ["a-1", "a-0", "b-1", "b-0"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_three_input_flat_crossproduct_cardinality_order_and_empty(tmp_path: Path) -> None:
    """S6: N-input cardinality is the product of lengths; an empty input yields zero invocations."""
    names = ["item", "tag", "delay"]
    params = {"items": ["a", "b"], "tags": ["x", "y", "z"], "delays": ["0"]}
    result = _write_and_sink(_flat(names), _connections(names, ["line"]), params, tmp_path, emit_name="line")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _gathered(tmp_path) == [f"{i}-{t}-0" for i in "ab" for t in "xyz"]

    empty = tmp_path / "empty"
    params = {"items": ["a"], "tags": [], "delays": ["0"]}
    result = _write_and_sink(_flat(names), _connections(names, ["line"]), params, empty, emit_name="line")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not list((empty / "work").glob("*/*"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_flat_crossproduct_keeps_per_invocation_cardinality_for_every_output(tmp_path: Path) -> None:
    """S11: each output carries exactly one value per invocation, in invocation order."""
    names = ["item", "delay"]
    process = _flat(names, [_text_port("line"), _text_port("copy")])
    params = {"items": ["a", "b"], "delays": ["1", "0"]}
    result = _write_and_sink(process, _connections(names, ["line", "copy"]), params, tmp_path, emit_name="copy")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _gathered(tmp_path) == ["a-1", "a-0", "b-1", "b-0"]
