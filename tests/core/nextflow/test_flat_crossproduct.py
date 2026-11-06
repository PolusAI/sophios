"""Multi-input flat_crossproduct scatter: CWL-ordered Cartesian invocations (design §6, Topology); test_runtime.py
runs them."""

# pylint: disable=missing-function-docstring

import pytest

from sophios.nf_types import NfWorkflowInputConnection
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import step, synthetic_source, tool, workflow_doc


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
