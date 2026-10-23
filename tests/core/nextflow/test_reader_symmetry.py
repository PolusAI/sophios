"""Every generated pattern reads back and promotes to the model that produced it.

design_docs/sophios_nextflow_backend.md §4.2, Import: a generated source whose
adjacent executable model renders to it byte for byte is fully understood.
"""

# pylint: disable=missing-function-docstring

from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

import pytest

from sophios.input_output_nf import render_nextflow, write_nextflow_artifacts
from sophios.nf_expr import parse
from sophios.nf_reader import NextflowPort, nextflow_to_cwl, parse_nf_file, parse_nf_text, promote_nextflow_document
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfEntry,
    NfEnvVar,
    NfOptionalBinding,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfTemplate,
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


def _entries() -> ExecutableNextflowWorkflow:
    process = NfProcess(
        "STEP",
        [NfPort("src", "path", entry=NfEntry("renamed.txt")),
         NfPort("out", "path", path_kind="directory", entry=NfEntry(copy=True))],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("out"),)), "directory")],
        NfCommand((template("cat"), template("renamed.txt"), template(ref("out")))),
    )
    return single_process_workflow(
        process, params={"src": "/tmp/s.txt", "out": "/tmp/o"}, param_types={"src": "File", "out": "Directory"},
        output_port_name="result",
    )


def _floats() -> ExecutableNextflowWorkflow:
    process = NfProcess(
        "STEP",
        [NfPort("x", "val"), NfPort("xs", "val", is_array=True), NfPort("o", "val")],
        [output_port("out", "out.txt")],
        NfCommand((template("echo"), template(ref("x")), NfArrayBinding("xs"), NfOptionalBinding("o", "--o")),
                  stdout=template("out.txt")),
    )
    return single_process_workflow(process, params={"x": -0.0, "xs": [1e-07], "o": []},
                                   param_types={"x": "float", "xs": "float[]", "o": "float?"}, output_port_name="out")


BUILDERS: dict[str, Callable[[], ExecutableNextflowWorkflow]] = {
    "a float's text": _floats,
    "InitialWorkDirRequirement entries": _entries,
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
    "an absent optional binding": lambda: single_process_workflow(NfProcess(
        "GREET", [NfPort("n", "val")], [output_port("out", "out.txt")],
        NfCommand((template("echo"), NfOptionalBinding("n", "--name=", False)), stdout=template("out.txt")),
    ), params={"n": []}, param_types={"n": "string?"}, output_port_name="out"),
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
def test_a_source_read_alone_reads_each_entry_input_as_a_path() -> None:
    workflow = _entries()
    parsed = parse_nf_text(render_nextflow(workflow), params=dict(workflow.params))
    assert parsed.processes[0].inputs == (NextflowPort("src", "path"), NextflowPort("out", "path"))
    assert parsed.connections == tuple(workflow.connections)


@pytest.mark.fast
def test_a_source_read_alone_reads_the_float_text_helper_and_float_params(tmp_path: Path) -> None:
    workflow = _floats()
    write_nextflow_artifacts(workflow, tmp_path)
    (tmp_path / "nextflow_workflow.json").unlink()
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert parsed.opaque_regions == ()
    assert dict(parsed.params) == workflow.to_dict()["params"]


@pytest.mark.fast
def test_a_model_derived_document_reads_the_accelerator(tmp_path: Path) -> None:
    workflow = BUILDERS["dotproduct"]()
    resources = NfResources(accelerator=2)
    workflow = replace(workflow, processes=[replace(process, resources=resources) for process in workflow.processes])
    write_nextflow_artifacts(workflow, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert [process.accelerator for process in parsed.processes] == ["2"]
    _, [tool] = nextflow_to_cwl(parsed)
    assert "hints" not in tool


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
