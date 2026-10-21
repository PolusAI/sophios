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
    NfCommand,
    NfEnvVar,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .test_composition import _PAIR_THEN_USE_PARAMS, _composed, _pair_then_use
from .test_conditions import _when_workflow
from .test_dotproduct import _scatter_with_file_array, _sleep_then_pair, _text_port
from .test_step_inputs import _GATHERED, _ONE, _XS, _merged
from .testkit import command, output_port, ref, single_process_workflow, template


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
    types = {f"{name}s": "string[]" for name in names}
    return ExecutableNextflowWorkflow(
        "PIPELINE", [process], [*connections, NfWorkflowOutputConnection("STEP", "line", "line")], params, types
    )


def _tool_shapes() -> ExecutableNextflowWorkflow:
    process = NfProcess(
        "STEP",
        [NfPort("s", "val")],
        [output_port("out", "out.txt")],
        NfCommand((template("printenv"),), stdout=template("out.txt"),
                  env=(NfEnvVar("A", template("a $b")), NfEnvVar("B", template(ref("s"))))),
        resources=NfResources(accelerator=2),
    )
    return single_process_workflow(process, params={"s": "v"}, param_types={"s": "string"}, output_port_name="out")


BUILDERS: dict[str, Callable[[], ExecutableNextflowWorkflow]] = {
    "exported environment variables and a GPU count": _tool_shapes,
    "single-input dotproduct": lambda: _scattered("dotproduct", names=("item",)),
    "conditional step": lambda: _when_workflow("$(inputs.a > 0)", {"a": 1}, second_step=True, a="int"),
    "dotproduct": lambda: _scattered("dotproduct"),
    "flat_crossproduct": lambda: _scattered("flat_crossproduct"),
    "nested_crossproduct": lambda: _scattered("nested_crossproduct"),
    "per-invocation when": lambda: _scattered("dotproduct", condition="$(inputs.tag !== '1')"),
    "gather and rescatter": lambda: _composed({"as": ["a"], "bs": ["b"], "n": 1, "gs": ["/tmp/g.txt"]}),
    "gather into a scattered step's array of paths": lambda: _pair_then_use("dotproduct", _PAIR_THEN_USE_PARAMS),
    "array of paths beside a scatter": lambda: _scatter_with_file_array("flat_crossproduct", ["/tmp/s.txt"]),
    "step-input idioms": lambda: _merged(_GATHERED, _ONE, _XS),
    "an unscattered output into a scatter": lambda: ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess("A", [], [output_port("f", "a.txt")], command("true")),
         NfProcess("EACH", [NfPort("item", "val"), NfPort("f", "path")], [], command("true"))],
        [NfWorkflowInputConnection("items", "EACH", "item", "dotproduct"), NfProcessConnection("A", "f", "EACH", "f")],
        {"items": ["x"]}, {"items": "string[]"},
    ),
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
def test_a_model_derived_document_reads_the_accelerator(tmp_path: Path) -> None:
    workflow = BUILDERS["dotproduct"]()
    resources = NfResources(accelerator=2)
    workflow = replace(workflow, processes=[replace(process, resources=resources) for process in workflow.processes])
    write_nextflow_artifacts(workflow, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert [process.accelerator for process in parsed.processes] == ["2"]
    _, [tool] = nextflow_to_cwl(parsed)
    assert tool["hints"] == {"cwltool:CUDARequirement": {"cudaDeviceCountMin": 2}}


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
