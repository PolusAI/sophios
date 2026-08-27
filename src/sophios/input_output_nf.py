"""Deterministic serializers for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .nf_types import (
    ExecutableNextflowWorkflow,
    NF_SHELL_QUOTE_HELPER,
    NfConnection,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)


NEXTFLOW_JSON = "nextflow_workflow.json"
NEXTFLOW_SCRIPT = "workflow.nf"
NEXTFLOW_CONFIG = "nextflow.config"
NEXTFLOW_PARAMS = "nextflow_params.json"
NF_SHELL_QUOTE_FUNCTION = f'''def {NF_SHELL_QUOTE_HELPER}(value) {{
    return "'" + value.toString().replace("'", "'\\\"'\\\"'") + "'"
}}'''


def _require_executable(workflow: Any) -> ExecutableNextflowWorkflow:
    if not isinstance(workflow, ExecutableNextflowWorkflow):
        raise TypeError("Nextflow rendering requires ExecutableNextflowWorkflow")
    return workflow


def _write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def write_nextflow_json(workflow: ExecutableNextflowWorkflow, outdir: str | Path) -> Path:
    """Write the stable JSON representation of a Nextflow workflow."""
    _require_executable(workflow)
    return _write_text(Path(outdir) / NEXTFLOW_JSON, f"{workflow.to_json()}\n")


def _groovy_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _groovy_literal(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
        elif character == "'":
            escaped.append("\\'")
        elif ord(character) < 32 or ord(character) == 127:
            escaped.append(f"\\u{ord(character):04x}")
        else:
            escaped.append(character)
    return f"'{''.join(escaped)}'"


def _template_expression(template: Any) -> str:
    return " + ".join(
        f"{segment.name}.toString()"
        if isinstance(segment, NfInputReference)
        else _groovy_literal(segment.value)
        for segment in template.segments
    )


def _shell_quote(value: str) -> str:
    """Return the POSIX shell word produced by the generated Groovy helper."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_template(template: Any) -> str:
    """Render one typed token through the generated shell-quoting function."""
    return f"${{{NF_SHELL_QUOTE_HELPER}({_template_expression(template)})}}"


def _render_glob(template: Any) -> str:
    if all(isinstance(segment, NfLiteral) for segment in template.segments):
        return _groovy_string("".join(segment.value for segment in template.segments))
    rendered: list[str] = []
    for segment in template.segments:
        if isinstance(segment, NfInputReference):
            rendered.append(f"${{{segment.name}}}")
        else:
            literal = segment.value.replace("\\", "\\\\").replace('"', '\\"')
            rendered.append(literal.replace("$", "\\$").replace("`", "\\`"))
    return f'"{"".join(rendered)}"'


def _process_output(port: NfPort, process: NfProcess) -> str:
    emit = port.emit or port.name
    if port.qualifier != "path":
        raise ValueError(
            f"invalid executable Nextflow workflow: process {process.name!r} output "
            f"{port.name!r} uses unsupported qualifier {port.qualifier!r}"
        )
    if port.glob is None:
        raise ValueError(f"invalid executable output {process.name}.{port.name}: missing glob")
    target = _render_glob(port.glob)
    return f"{port.qualifier} {target}, emit: {emit}"


def _render_process(process: NfProcess) -> str:
    lines = [f"process {process.name} {{"]
    if process.container is not None:
        lines.append(f"    container {_groovy_string(process.container)}")
    if process.resources.cpus is not None:
        lines.append(f"    cpus {process.resources.cpus:g}")
    if process.resources.memory_mb is not None:
        lines.append(f'    memory "{process.resources.memory_mb:g} MB"')

    if process.inputs:
        lines.extend(["", "    input:"])
        lines.extend(f"    {port.qualifier} {port.name}" for port in process.inputs)
    if process.outputs:
        lines.extend(["", "    output:"])
        lines.extend(f"    {_process_output(port, process)}" for port in process.outputs)

    command = " ".join(_render_template(token) for token in process.command.tokens)
    for operator, stream in (("<", process.command.stdin), (">", process.command.stdout), ("2>", process.command.stderr)):
        if stream is not None:
            command += f" {operator} {_render_template(stream)}"
    lines.extend(["", "    script:", '    \"\"\"'])
    lines.append(f"    {command}")
    lines.extend(['    \"\"\"', "}"])
    return "\n".join(lines)


def _process_map(workflow: ExecutableNextflowWorkflow) -> dict[str, NfProcess]:
    return {process.name: process for process in workflow.processes}


def _incoming_connections(workflow: ExecutableNextflowWorkflow) -> dict[tuple[str, str], NfConnection]:
    incoming: dict[tuple[str, str], NfConnection] = {}
    for connection in workflow.connections:
        match connection:
            case NfWorkflowInputConnection(_, to_process, to_port) | NfProcessConnection(
                _, _, to_process, to_port
            ):
                pass
            case NfWorkflowOutputConnection():
                continue
        key = (to_process, to_port)
        if key in incoming:
            raise ValueError(
                f"Nextflow Phase 1 supports one source per process input; "
                f"{to_process}.{to_port} has more than one"
            )
        incoming[key] = connection
    return incoming


def _source_expression(connection: NfConnection, processes: Mapping[str, NfProcess]) -> str:
    match connection:
        case NfWorkflowInputConnection(from_port, _, _):
            return from_port
        case NfProcessConnection(from_process, from_port, _, _) | NfWorkflowOutputConnection(
            from_process, from_port, _
        ):
            process = processes[from_process]
            output = next(port for port in process.outputs if port.name == from_port)
            return f"{process.name}.out.{output.emit or output.name}"


def _ordered_processes(workflow: ExecutableNextflowWorkflow) -> list[NfProcess]:
    """Return processes in stable topological order and reject cycles."""
    position = {process.name: index for index, process in enumerate(workflow.processes)}
    dependencies: dict[str, set[str]] = {
        process.name: set() for process in workflow.processes
    }
    for connection in workflow.connections:
        if isinstance(connection, NfProcessConnection):
            dependencies[connection.to_process].add(connection.from_process)

    ordered: list[NfProcess] = []
    remaining = set(dependencies)
    while remaining:
        ready = sorted(
            (name for name in remaining if not (dependencies[name] & remaining)),
            key=position.__getitem__,
        )
        if not ready:
            raise ValueError("Nextflow workflow connections contain a cycle")
        for name in ready:
            ordered.append(workflow.processes[position[name]])
            remaining.remove(name)
    return ordered


def _workflow_input_names(workflow: ExecutableNextflowWorkflow) -> list[str]:
    names: list[str] = []
    for connection in workflow.connections:
        if isinstance(connection, NfWorkflowInputConnection) and connection.from_port not in names:
            names.append(connection.from_port)
    return names


def _render_named_workflow(workflow: ExecutableNextflowWorkflow) -> str:
    processes = _process_map(workflow)
    incoming = _incoming_connections(workflow)
    workflow_inputs = _workflow_input_names(workflow)
    lines = [f"workflow {workflow.name} {{"]
    if workflow_inputs:
        lines.append("    take:")
        lines.extend(f"    {name}" for name in workflow_inputs)

    lines.append("    main:")
    for process in _ordered_processes(workflow):
        arguments: list[str] = []
        for port in process.inputs:
            connection = incoming.get((process.name, port.name))
            if connection is None:
                raise ValueError(f"process input {process.name}.{port.name} is not connected")
            arguments.append(_source_expression(connection, processes))
        lines.append(f"    {process.name}({', '.join(arguments)})")

    workflow_outputs = [
        connection
        for connection in workflow.connections
        if isinstance(connection, NfWorkflowOutputConnection)
    ]
    if workflow_outputs:
        lines.append("    emit:")
        for connection in workflow_outputs:
            lines.append(
                f"    {connection.to_port} = {_source_expression(connection, processes)}"
            )
    lines.append("}")
    return "\n".join(lines)


def _workflow_input_port(
    workflow: ExecutableNextflowWorkflow,
    name: str,
) -> tuple[NfProcess, NfPort]:
    processes = _process_map(workflow)
    for connection in workflow.connections:
        if isinstance(connection, NfWorkflowInputConnection) and connection.from_port == name:
            process = processes[connection.to_process]
            return process, next(port for port in process.inputs if port.name == connection.to_port)
    raise ValueError(f"workflow input {name!r} is not connected")


def _parameter_expression(workflow: ExecutableNextflowWorkflow, name: str) -> str:
    process, port = _workflow_input_port(workflow, name)
    value: Any = workflow.params.get(name)
    if port.qualifier == "path":
        if isinstance(value, (list, tuple)):
            return f"Channel.fromPath(params.{name}.collect {{ it.path ?: it }}, checkIfExists: true)"
        if isinstance(value, Mapping):
            return f"Channel.fromPath(params.{name}.path, checkIfExists: true)"
        return f"Channel.fromPath(params.{name}, checkIfExists: true)"
    return f"Channel.value(params.{name})"


def render_nextflow(workflow: ExecutableNextflowWorkflow) -> str:
    """Render a supported workflow as deterministic Nextflow DSL2 source."""
    _require_executable(workflow)
    sections = [
        "nextflow.enable.dsl=2",
        NF_SHELL_QUOTE_FUNCTION,
    ]
    sections.extend(_render_process(process) for process in workflow.processes)
    sections.append(_render_named_workflow(workflow))
    arguments = ",\n        ".join(
        _parameter_expression(workflow, name) for name in _workflow_input_names(workflow)
    )
    invocation = f"    {workflow.name}({arguments})" if arguments else f"    {workflow.name}()"
    sections.append(f"workflow {{\n{invocation}\n}}")
    return "\n\n".join(sections) + "\n"


def render_nextflow_config(workflow: ExecutableNextflowWorkflow) -> str:
    """Render the deterministic executor configuration."""
    _require_executable(workflow)
    docker_enabled = any(process.container for process in workflow.processes)
    return f"docker.enabled = {str(docker_enabled).lower()}\n"


def render_nextflow_params(workflow: ExecutableNextflowWorkflow) -> str:
    """Render workflow parameters as deterministic JSON."""
    _require_executable(workflow)
    return json.dumps(
        workflow.to_dict()["params"],
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def write_nextflow_files(
    workflow: ExecutableNextflowWorkflow,
    outdir: str | Path,
) -> tuple[Path, Path, Path]:
    """Write ``workflow.nf``, ``nextflow.config``, and the parameters file."""
    output = Path(outdir)
    return (
        _write_text(output / NEXTFLOW_SCRIPT, render_nextflow(workflow)),
        _write_text(output / NEXTFLOW_CONFIG, render_nextflow_config(workflow)),
        _write_text(output / NEXTFLOW_PARAMS, render_nextflow_params(workflow)),
    )


def write_nextflow_artifacts(
    workflow: ExecutableNextflowWorkflow,
    outdir: str | Path,
) -> tuple[Path, Path, Path, Path]:
    """Validate every representation before writing the four artifacts."""
    serialized = f"{workflow.to_json()}\n"
    script = render_nextflow(workflow)
    config = render_nextflow_config(workflow)
    params = render_nextflow_params(workflow)
    output = Path(outdir)
    return (
        _write_text(output / NEXTFLOW_JSON, serialized),
        _write_text(output / NEXTFLOW_SCRIPT, script),
        _write_text(output / NEXTFLOW_CONFIG, config),
        _write_text(output / NEXTFLOW_PARAMS, params),
    )
