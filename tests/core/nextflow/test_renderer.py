"""Pure deterministic rendering of validated executable IR into artifacts."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, cast

import pytest

from sophios.input_output_nf import (
    render_nextflow,
    render_nextflow_config,
    write_nextflow_artifacts,
)
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfBasenameReference,
    NfCommand,
    NfFlag,
    NfLiteral,
    NfPort,
    NfProcess,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow
from sophios.wic_types import RoseTree

from .testkit import command, flag_workflow, output_port, runtime_workflow


@pytest.mark.serial
def test_writes_four_deterministic_artifacts(tmp_path: Path) -> None:
    workflow = runtime_workflow()
    before = workflow.to_json()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}
    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]
    write_nextflow_artifacts(workflow, tmp_path)
    assert {path.name: path.read_bytes() for path in paths} == first
    assert workflow.to_json() == before


@pytest.mark.fast
def test_renderer_rejects_non_executable_representation() -> None:
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        render_nextflow(cast(Any, {"representation_kind": "structural"}))


@pytest.mark.fast
def test_write_artifacts_rejects_non_executable_representation(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        write_nextflow_artifacts(cast(Any, {"representation_kind": "structural"}), tmp_path)


@pytest.mark.serial
def test_renders_named_workflow_and_entry_wrapper() -> None:
    rendered = render_nextflow(runtime_workflow())
    assert "workflow PIPELINE {" in rendered
    assert "take:\n    message" in rendered
    assert "COPY(PRODUCE.out.result)" in rendered
    assert "result = COPY.out.copy" in rendered
    assert "workflow {\n    PIPELINE(Channel.value(params.message))\n}" in rendered


@pytest.mark.serial
def test_renders_real_compiled_source_with_typed_glob(real_supported_rose: RoseTree) -> None:
    rendered = render_nextflow(cwl_rosetree_to_nextflow(real_supported_rose))
    assert 'path "${filename}", emit: result' in rendered
    assert "wf__step__2__copy(wf__step__1__touch.out.result)" in rendered


@pytest.mark.serial
def test_renders_supported_process_metadata() -> None:
    process = NfProcess(
        "TASK",
        [NfPort("source", "path")],
        [output_port("report", "report.txt")],
        command("touch", "report.txt"),
        container="ubuntu:24.04",
        resources=NfResources(2, 1024),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "TASK", "source")],
        {"source": "input.txt"},
    ))
    assert "container 'ubuntu:24.04'" in rendered
    assert "cpus 2" in rendered
    assert 'memory "1024 MB"' in rendered
    assert "path 'report.txt', emit: report" in rendered


@pytest.mark.serial
def test_renders_resources_without_numeric_semantic_loss() -> None:
    process = NfProcess(
        "TASK",
        [],
        [output_port("report", "report.txt")],
        command("touch", "report.txt"),
        resources=NfResources(cpus=2, memory_mb=1234567),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("WF", [process], [], {}))

    assert "cpus 2" in rendered
    assert 'memory "1234567 MB"' in rendered
    assert "e+" not in rendered.lower()


@pytest.mark.serial
def test_path_parameter_rendering_is_runtime_shape_independent() -> None:
    process = NfProcess(
        "READ",
        [NfPort("source", "path")],
        [],
        command("true"),
    )
    connections = [NfWorkflowInputConnection("source", "READ", "source")]
    from_string = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": "input.txt"},
    )
    from_mapping = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": {"class": "File", "path": "input.txt"}},
    )

    rendered = render_nextflow(from_string)
    assert rendered == render_nextflow(from_mapping)
    assert (
        "Channel.fromPath(params.source instanceof Map ? params.source.path : "
        "params.source, checkIfExists: true, type: 'file', glob: false)"
    ) in rendered


@pytest.mark.serial
def test_renders_boolean_flag_as_a_conditional_argv_word() -> None:
    process = NfProcess(
        "SORT",
        [NfPort("reverse", "val")],
        [output_port("result", "sorted.txt")],
        NfCommand(
            (NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r")),
            stdout=NfTemplate((NfLiteral("sorted.txt"),)),
        ),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("reverse", "SORT", "reverse")],
        {"reverse": True},
    ))

    assert "${reverse ? __sophios_shell_quote_9f72e('-r') : ''}" in rendered


@pytest.mark.serial
def test_flag_workflow_artifacts_are_byte_stable(tmp_path: Path) -> None:
    workflow = flag_workflow()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}

    write_nextflow_artifacts(workflow, tmp_path)

    assert {path.name: path.read_bytes() for path in paths} == first
    assert render_nextflow(workflow) == render_nextflow(workflow)


@pytest.mark.serial
def test_renders_basename_segments_in_commands_and_globs() -> None:
    basename = NfTemplate((NfBasenameReference("source"), NfLiteral(".copy")))
    process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [NfPort("result", "path", "result", basename)],
        NfCommand((NfTemplate((NfLiteral("cp"),)), basename)),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "COPY", "source")],
        {"source": "lines.txt"},
    ))

    assert "source.name.toString() + '.copy'" in rendered
    assert 'path "${source.name}.copy", emit: result' in rendered


@pytest.mark.fast
def test_config_uses_validated_workflow_container_policy() -> None:
    host = ExecutableNextflowWorkflow(
        "HOST_WF",
        [NfProcess("HOST", [], [], command("true"))],
        [],
        {},
    )
    containerized = ExecutableNextflowWorkflow(
        "CONTAINER_WF",
        [NfProcess("CONTAINER", [], [], command("true"), container="ubuntu:24.04")],
        [],
        {},
    )
    assert render_nextflow_config(host) == "docker.enabled = false\n"
    assert render_nextflow_config(containerized) == "docker.enabled = true\n"
