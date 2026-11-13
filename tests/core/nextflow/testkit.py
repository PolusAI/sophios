"""Shared builders and the real-Nextflow harness for the backend suites: thin, removing repetition, never behavior."""

from collections.abc import Sequence
import copy
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sophios.compiler import CompileSettings, compile_source, resolved_view
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.resolved import NotLowered
from sophios.lang.diagnostics import Diagnostic
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfCommand,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.plugins import get_tools_cwl
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_nf import NEXTFLOW
from sophios.wic_types import StepId, Tool, Yaml

from ..hermetic import bundle
from ..synthetic_tools import SYNTHETIC_NS

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
    """Return a compact workflow step entry: the step that runs the tool `name`."""
    return {"id": name, "in": {}, "out": [], **fields}


def one_step(run: Yaml, out: Sequence[str] = (), **values: Any) -> Yaml:
    """The case of the tool `run` as a workflow's only step, each input wired from a same-named workflow input.

    Each name in `out` is forwarded as a same-named workflow output, and `values` are the workflow's inputs.
    """
    return {"tool": run, "out": list(out), "job": values}


def _case_document(data: Yaml) -> tuple[Yaml, list[Yaml]]:
    """A case as a Sophios document, and the tool each of its steps runs, in step order.

    `{tool, out, job}` is the tool as a workflow's only step, as `one_step` says; `{workflow, tools, job}` is a
    document and its steps' tools. A `tool` without an `id` is named `make`. Each tool takes tool()'s value only
    for a key it leaves out.
    """
    if "tool" in data:
        run = {"id": "make", **data["tool"]}
        run = {**tool(run["id"]), **run}
        name = run["id"]
        document = {
            "inputs": {port: {"type": definition["type"]} for port, definition in run["inputs"].items()},
            "outputs": {port: {"type": run["outputs"][port]["type"], "outputSource": f"{name}/{port}"}
                        for port in data.get("out", ())},
            "steps": [{"id": name, "in": {port: port for port in run["inputs"]}}],
        }
        return document, [run]
    return data["workflow"], [{**tool(run["id"]), **run} for run in data.get("tools", [])]


def compiled_case(data: Yaml) -> CompilationResult:
    """A case compiled through `compile_source`, against a registry of its tools, as `hermetic` compiles a document.

    Each tool is registered under its `id`, as the file door registers a tool by its stem, and a step runs the tool
    its id names. A step holding a `subtree` calls that nested workflow, as `hermetic.subworkflow_step` writes it.
    """
    document, tools = _case_document(data)
    registry = {StepId(run["id"], SYNTHETIC_NS): Tool(f"{run['id']}.cwl", desugar_into_canonical_normal_form(
        copy.deepcopy(run))) for run in tools}
    return compile_source(bundle(document, "wf", registry), CompileSettings(), quiet=True)


def lowered(result: CompilationResult, values: Yaml | None = None) -> ExecutableNextflowWorkflow:
    """The Nextflow model of `result`, its root inputs given `values` in place of the values the graph holds."""
    return NEXTFLOW.lower(resolved_view(result, values=values))[0]


def lowered_case(data: Yaml) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
    """A case's model and notes: compiled, given its `job` as the workflow's values, keeping the outputs it declares."""
    document, _tools = _case_document(data)
    return NEXTFLOW.lower(resolved_view(compiled_case(data), values=data.get("job", {}),
                                        outputs=list(document.get("outputs", {}))))


def refusal_of(root: Path) -> NotLowered:
    """What the Nextflow target refuses of the `.wic` file `root`, read as the command line reads a file, with the
    tools and the other workflows of its directory, given the values of the `.yml` of its name beside it, if any."""
    tools = get_tools_cwl({"search_paths_cwl": {"global": [str(root.parent)]}})
    others = {"global": {path.stem: path for path in root.parent.glob("*.wic") if path != root}}
    result = compile_source(bundle_from_disk(root, others, tools), CompileSettings(), quiet=True)
    job = root.with_suffix(".yml")
    try:
        lowered(result, yaml.safe_load(job.read_text(encoding="utf-8")) if job.exists() else None)
    except NotLowered as refused:
        return refused
    raise AssertionError(f"{root} is lowered: the Nextflow target refuses nothing of it")


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


def assert_succeeded(result: "subprocess.CompletedProcess[str]") -> None:
    """Fail with the process's whole output unless it exited 0."""
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def execute_nextflow(
    directory: Path,
    *arguments: str,
    timeout: int = 90,
) -> "subprocess.CompletedProcess[str]":
    """Run the generated artifacts in ``directory`` under pinned harness rules, with extra ``arguments``."""
    env = {**os.environ, "NXF_ANSI_LOG": "false", "NXF_HOME": str(directory / ".nxf-home"), "NXF_OFFLINE": "true"}
    return subprocess.run(
        ["nextflow", "run", "workflow.nf", "-params-file", "nextflow_params.json", "-c", "nextflow.config",
         "-work-dir", str(directory / "work"), *arguments],
        cwd=directory, env=env, text=True, capture_output=True, timeout=timeout, check=False,
    )


def run_observed(
    workflow: ExecutableNextflowWorkflow, directory: Path, *arguments: str, emits: Sequence[str] | None = None
) -> tuple["subprocess.CompletedProcess[str]", dict[str, list[str]]]:
    """Write and run `workflow`, observing each of `emits` (default all) in arrival order (collectFile loses it):
    a value's text, a path's contents, joined on U+0001 as a value may hold a newline."""
    write_nextflow_artifacts(workflow, directory)
    names = (list(emits) if emits is not None
             else [c.to_port for c in workflow.connections if isinstance(c, NfWorkflowOutputConnection)])
    script = directory / "workflow.nf"
    text = script.read_text(encoding="utf-8")
    assert text.endswith("}\n")  # the entry workflow's closing brace: sinks go before it, where `.out` is in scope
    sinks = "".join(
        f"\n    {workflow.name}.out.{emit}.toList().subscribe {{ list -> new File('{emit}.sink').text = "
        f"list.collect {{ it instanceof java.nio.file.Path ? it.text : it.toString() }}.join('\\u0001') }}\n"
        for emit in names
    )
    script.write_text(text[:-2] + sinks + "}\n", encoding="utf-8")
    result = execute_nextflow(directory, *arguments)
    return result, {
        emit: (directory / f"{emit}.sink").read_text(encoding="utf-8").split("\x01")
        for emit in names if (directory / f"{emit}.sink").exists()
    }
