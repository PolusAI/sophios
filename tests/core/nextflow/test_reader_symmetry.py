"""Every generated pattern reads back and promotes to the model that produced it.

design_docs/sophios_nextflow_backend.md §4.2, Import: a generated source whose
adjacent executable model renders to it byte for byte is fully understood.
"""

# pylint: disable=missing-function-docstring

from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

import pytest

from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_expr import parse
from sophios.nf_reader import nextflow_to_cwl, parse_nf_file, promote_nextflow_document
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfPort,
    NfProcess,
    NfResources,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .test_composition import _composed
from .test_conditions import _when_workflow
from .test_dotproduct import _sleep_then_pair, _text_port
from .test_safe_subset import ARGUMENTS, _calc_workflow


def _scattered(
    method: str, *, condition: str | None = None, names: Sequence[str] = ("item", "tag")
) -> ExecutableNextflowWorkflow:
    process = NfProcess(
        "STEP",
        [NfPort(name, "val") for name in names],
        [_text_port("line")],
        _sleep_then_pair(*names),
        condition=None if condition is None else parse(condition),
    )
    connections = [NfWorkflowInputConnection(f"{name}s", "STEP", name, method) for name in names]
    params = {f"{name}s": ["0", "1"] for name in names}
    return ExecutableNextflowWorkflow(
        "PIPELINE", [process], [*connections, NfWorkflowOutputConnection("STEP", "line", "line")], params
    )


BUILDERS: dict[str, Callable[[], ExecutableNextflowWorkflow]] = {
    "single-input scatter adapter": lambda: _scattered("scatter", names=("item",)),
    "single-input dotproduct": lambda: _scattered("dotproduct", names=("item",)),
    "computed arguments": lambda: _calc_workflow(ARGUMENTS, {"a": 7, "b": 1.25, "d": 2}, a="int", b="float", d="int"),
    "conditional step": lambda: _when_workflow("$(inputs.a > 0)", {"a": 1}, second_step=True, a="int"),
    "dotproduct": lambda: _scattered("dotproduct"),
    "flat_crossproduct": lambda: _scattered("flat_crossproduct"),
    "nested_crossproduct": lambda: _scattered("nested_crossproduct"),
    "per-invocation when": lambda: _scattered("dotproduct", condition="$(inputs.tag !== '1')"),
    "gather and rescatter": lambda: _composed({"as": ["a"], "bs": ["b"], "n": 1, "gs": ["/tmp/g.txt"]}),
}


@pytest.mark.fast
@pytest.mark.parametrize("name", BUILDERS)
def test_every_generated_pattern_reads_back_and_promotes(name: str, tmp_path: Path) -> None:
    workflow = BUILDERS[name]()
    write_nextflow_artifacts(workflow, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert parsed.opaque_regions == ()
    assert parsed.connections == tuple(workflow.connections)
    assert [process.name for process in parsed.processes] == [process.name for process in workflow.processes]
    assert promote_nextflow_document(parsed) == workflow


@pytest.mark.fast
@pytest.mark.parametrize("name", ["dotproduct", "conditional step"])
def test_model_derived_document_reads_exponent_form_memory(name: str, tmp_path: Path) -> None:
    workflow = BUILDERS[name]()
    resources = NfResources(memory_mb=1e-05)
    workflow = replace(workflow, processes=[replace(process, resources=resources) for process in workflow.processes])
    write_nextflow_artifacts(workflow, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert {process.memory for process in parsed.processes} == {"0.00001 MB"}
    _, tools = nextflow_to_cwl(parsed)
    assert [tool["requirements"]["ResourceRequirement"]["ramMin"] for tool in tools] == [1e-05] * len(tools)
