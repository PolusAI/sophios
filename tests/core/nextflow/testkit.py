"""Shared builders and the real-Nextflow harness for the backend test suites.

Builders are deliberately thin: they remove repetition, never behavior. Tests
that exercise validation failure construct invalid values directly.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, cast

from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfCommand,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.wic_types import GraphReps, NodeData, RoseTree, Tool, Yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

ref = NfInputReference


def template(*segments: str | NfInputReference) -> NfTemplate:
    """Build a template from literal strings and input references."""
    return NfTemplate(tuple(
        segment if isinstance(segment, NfInputReference) else NfLiteral(segment)
        for segment in segments
    ))


def command(
    *tokens: str | NfTemplate,
    stdin: str | NfTemplate | None = None,
    stdout: str | NfTemplate | None = None,
    stderr: str | NfTemplate | None = None,
) -> NfCommand:
    """Build a typed command; bare strings become single-literal tokens."""
    def as_template(value: str | NfTemplate) -> NfTemplate:
        return value if isinstance(value, NfTemplate) else template(value)

    def optional(value: str | NfTemplate | None) -> NfTemplate | None:
        return None if value is None else as_template(value)

    return NfCommand(
        tuple(as_template(token) for token in tokens),
        optional(stdin), optional(stdout), optional(stderr),
    )


def output_port(name: str, glob: str | NfTemplate | None = None) -> NfPort:
    """Build an executable output port with a typed glob."""
    target = glob if isinstance(glob, NfTemplate) else template(glob or name)
    return NfPort(name, "path", name, target)


def node_data(
    name: str,
    compiled_cwl: Yaml,
    *,
    workflow_inputs: Yaml | None = None,
    source_yml: Yaml | None = None,
) -> NodeData:
    """Build the smallest real NodeData needed by a conversion fixture."""
    return NodeData(
        [],
        name,
        source_yml or {},
        compiled_cwl,
        Tool(f"{name}.cwl", compiled_cwl),
        workflow_inputs or {},
        {},
        {},
        cast(GraphReps, None),
        {},
        f'"{name}"',
    )


def subworkflow_child(document: Yaml, tools: list[Yaml]) -> RoseTree:
    """Build one step child whose compiled run is a CWL Workflow."""
    return RoseTree(
        node_data(str(document.get("id", "child")), document),
        [
            RoseTree(node_data(str(tool.get("id", f"tool_{index}")), tool), [])
            for index, tool in enumerate(tools)
        ],
    )


def synthetic_rose(
    workflow_cwl: Yaml,
    tools: list[Yaml | RoseTree],
    *,
    workflow_inputs: Yaml | None = None,
    source_yml: Yaml | None = None,
) -> RoseTree:
    """Construct a typed RoseTree without invoking compiler internals.

    A child may be given as a compiled tool document or as an already-built
    RoseTree, so a step whose run is a subworkflow can be expressed directly.
    """
    children = [
        child if isinstance(child, RoseTree)
        else RoseTree(node_data(str(child.get("id", f"tool_{index}")), child), [])
        for index, child in enumerate(tools)
    ]
    return RoseTree(
        node_data(
            str(workflow_cwl.get("id", "workflow")),
            workflow_cwl,
            workflow_inputs=workflow_inputs,
            source_yml=source_yml,
        ),
        children,
    )


def tool(
    name: str,
    *,
    inputs: Yaml | None = None,
    outputs: Yaml | None = None,
    **extra: Any,
) -> Yaml:
    """Return a compact CommandLineTool dictionary."""
    return {
        "id": name,
        "class": "CommandLineTool",
        "cwlVersion": "v1.2",
        "baseCommand": name,
        "inputs": inputs or {},
        "outputs": outputs or {},
        **extra,
    }


def step(name: str, **fields: Any) -> Yaml:
    """Return a compact compiled workflow step entry."""
    return {"id": name, "in": {}, "out": [], "run": f"{name}.cwl", **fields}


def workflow_doc(
    steps: list[Yaml],
    *,
    inputs: Yaml | None = None,
    outputs: Yaml | None = None,
) -> Yaml:
    """Return a compact compiled CWL Workflow dictionary."""
    return {
        "id": "wf",
        "class": "Workflow",
        "cwlVersion": "v1.2",
        "inputs": inputs or {},
        "outputs": outputs or {},
        "steps": steps,
    }


def runtime_workflow() -> ExecutableNextflowWorkflow:
    """A two-process produce/copy pipeline used across render and runtime tests."""
    produce = NfProcess(
        "PRODUCE",
        [NfPort("message", "val")],
        [output_port("result", "message.txt")],
        command("printf", "%s", template(ref("message")), stdout="message.txt"),
    )
    copy_process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [output_port("copy", "copy.txt")],
        command("cp", template(ref("source")), "copy.txt"),
    )
    return ExecutableNextflowWorkflow(
        "PIPELINE",
        [produce, copy_process],
        [
            NfWorkflowInputConnection("message", "PRODUCE", "message"),
            NfProcessConnection("PRODUCE", "result", "COPY", "source"),
            NfWorkflowOutputConnection("COPY", "copy", "result"),
        ],
        {"message": "hello from Sophios"},
    )


def flag_workflow() -> ExecutableNextflowWorkflow:
    """A one-process pipeline whose command carries a conditional flag token."""
    process = NfProcess(
        "SORT",
        [NfPort("reverse", "val"), NfPort("source", "path")],
        [output_port("result", "sorted.txt")],
        NfCommand(
            (
                template("sort"),
                NfFlag("reverse", "-r"),
                template(ref("source")),
            ),
            stdout=template("sorted.txt"),
        ),
    )
    return ExecutableNextflowWorkflow(
        "PIPELINE",
        [process],
        [
            NfWorkflowInputConnection("reverse", "SORT", "reverse"),
            NfWorkflowInputConnection("source", "SORT", "source"),
            NfWorkflowOutputConnection("SORT", "result", "result"),
        ],
        {"reverse": True, "source": "lines.txt"},
    )


def single_process_workflow(
    process: NfProcess,
    *,
    params: dict[str, Any],
    output_port_name: str,
) -> ExecutableNextflowWorkflow:
    """Wire every process input to a same-named workflow input plus one emit."""
    connections = [
        NfWorkflowInputConnection(port.name, process.name, port.name)
        for port in process.inputs
    ]
    output_connection = NfWorkflowOutputConnection(process.name, output_port_name, "result")
    return ExecutableNextflowWorkflow(
        "PIPELINE", [process], [*connections, output_connection], params
    )


def execute_nextflow(
    directory: Path,
    *,
    timeout: int = 90,
    preview: bool = False,
) -> "subprocess.CompletedProcess[str]":
    """Run the generated artifacts in ``directory`` under pinned harness rules."""
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(directory / ".nxf-home"),
        "NXF_OFFLINE": "true",
    })
    run_command = ["nextflow", "run"]
    if preview:
        run_command.append("-preview")
    run_command.extend([
        "workflow.nf",
        "-params-file",
        "nextflow_params.json",
        "-c",
        "nextflow.config",
        "-work-dir",
        str(directory / "work"),
    ])
    return subprocess.run(
        run_command,
        cwd=directory,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_nextflow(
    workflow: ExecutableNextflowWorkflow,
    directory: Path,
) -> "subprocess.CompletedProcess[str]":
    """Write the four artifacts and execute them."""
    write_nextflow_artifacts(workflow, directory)
    return execute_nextflow(directory)
