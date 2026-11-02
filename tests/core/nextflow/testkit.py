"""Shared builders and the real-Nextflow harness for the backend test suites.

Builders are deliberately thin: they remove repetition, never behavior. Tests
that exercise validation failure construct invalid values directly.
"""

from collections.abc import Sequence
import os
import subprocess
from pathlib import Path
from typing import Any

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
from sophios.utils_nf import CompiledNextflowSource
from sophios.wic_types import Yaml

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


def synthetic_source(
    workflow_cwl: Yaml,
    tools: list[Yaml],
    *,
    workflow_inputs: Yaml | None = None,
) -> CompiledNextflowSource:
    """Construct the backend's compiled-document seam for capability tests."""
    return CompiledNextflowSource(
        str(workflow_cwl.get("id", "workflow")),
        workflow_cwl,
        tuple(tools),
        workflow_inputs or {},
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


def one_step(run: Yaml, out: Sequence[str] = (), **values: Any) -> CompiledNextflowSource:
    """The tool `run` as a workflow's only step, each input wired from a same-named workflow input.

    Each name in `out` is forwarded as a same-named workflow output, and `values` are the workflow's inputs.
    """
    name = run["id"]
    return synthetic_source(
        workflow_doc(
            [step(name, **{"in": {port: port for port in run["inputs"]}, "out": list(out)})],
            inputs={port: {"type": definition["type"]} for port, definition in run["inputs"].items()},
            outputs={port: {"type": run["outputs"][port]["type"], "outputSource": f"{name}/{port}"} for port in out},
        ),
        [run],
        workflow_inputs=values,
    )


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
        {"message": "hello from Sophios"}, {"message": "string"},
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
        {"reverse": True, "source": "lines.txt"}, {"reverse": "boolean", "source": "File"},
    )


def single_process_workflow(
    process: NfProcess,
    *,
    params: dict[str, Any],
    param_types: dict[str, str],
    output_port_name: str,
) -> ExecutableNextflowWorkflow:
    """Wire every process input to a same-named workflow input plus one emit."""
    connections = [
        NfWorkflowInputConnection(port.name, process.name, port.name)
        for port in process.inputs
    ]
    output_connection = NfWorkflowOutputConnection(process.name, output_port_name, "result")
    return ExecutableNextflowWorkflow(
        "PIPELINE", [process], [*connections, output_connection], params, param_types
    )


def execute_nextflow(
    directory: Path,
    *arguments: str,
    timeout: int = 90,
) -> "subprocess.CompletedProcess[str]":
    """Run the generated artifacts in ``directory`` under pinned harness rules, with extra ``arguments``."""
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(directory / ".nxf-home"),
        "NXF_OFFLINE": "true",
    })
    run_command = [
        "nextflow",
        "run",
        "workflow.nf",
        "-params-file",
        "nextflow_params.json",
        "-c",
        "nextflow.config",
        "-work-dir",
        str(directory / "work"),
        *arguments,
    ]
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
