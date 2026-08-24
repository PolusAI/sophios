"""Deterministic serializers for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .nf_types import NfConnection, NfPort, NfProcess, NextflowWorkflow


NEXTFLOW_JSON = "nextflow_workflow.json"
NEXTFLOW_SCRIPT = "workflow.nf"
NEXTFLOW_CONFIG = "nextflow.config"
NEXTFLOW_PARAMS = "nextflow_params.json"


def _write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def write_nextflow_json(workflow: NextflowWorkflow, outdir: str | Path) -> Path:
    """Write the stable JSON representation of a Nextflow workflow."""
    return _write_text(Path(outdir) / NEXTFLOW_JSON, f"{workflow.to_json()}\n")


def _groovy_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _process_output(port: NfPort, process: NfProcess) -> str:
    emit = port.emit or port.name
    glob = process.directives.get(f"_output_glob.{port.name}")
    if port.qualifier == "path":
        target = _groovy_string(glob or port.name)
    else:
        target = port.name
    return f"{port.qualifier} {target}, emit: {emit}"


def _render_process(process: NfProcess) -> str:
    lines = [f"process {process.name} {{"]
    if process.container is not None:
        lines.append(f"    container {_groovy_string(process.container)}")
    for name in ("cpus", "memory"):
        if value := process.directives.get(name):
            rendered = value if name == "cpus" else _groovy_string(value)
            lines.append(f"    {name} {rendered}")
    if process.directives.get("_scatter") == "true":
        lines.append("    // TODO: scatter is represented as metadata in Phase 1")

    if process.inputs:
        lines.extend(["", "    input:"])
        lines.extend(f"    {port.qualifier} {port.name}" for port in process.inputs)
    if process.outputs:
        lines.extend(["", "    output:"])
        lines.extend(f"    {_process_output(port, process)}" for port in process.outputs)

    if '\"\"\"' in process.script:
        raise ValueError(f"process {process.name!r} script contains an unsupported triple-quote delimiter")
    lines.extend(["", "    script:", '    \"\"\"'])
    lines.extend(f"    {line}" for line in process.script.splitlines())
    lines.extend(['    \"\"\"', "}"])
    return "\n".join(lines)


def _process_map(workflow: NextflowWorkflow) -> dict[str, NfProcess]:
    return {process.name: process for process in workflow.processes}


def _incoming_connections(workflow: NextflowWorkflow) -> dict[tuple[str, str], NfConnection]:
    incoming: dict[tuple[str, str], NfConnection] = {}
    for connection in workflow.connections:
        if connection.to_process is None:
            continue
        key = (connection.to_process, connection.to_port)
        if key in incoming:
            raise ValueError(
                f"Nextflow Phase 1 supports one source per process input; "
                f"{connection.to_process}.{connection.to_port} has more than one"
            )
        incoming[key] = connection
    return incoming


def _source_expression(connection: NfConnection, processes: Mapping[str, NfProcess]) -> str:
    if connection.from_process is None:
        return connection.from_port
    process = processes[connection.from_process]
    output = next(port for port in process.outputs if port.name == connection.from_port)
    return f"{process.name}.out.{output.emit or output.name}"


def _ordered_processes(workflow: NextflowWorkflow) -> list[NfProcess]:
    """Return processes in stable topological order and reject cycles."""
    position = {process.name: index for index, process in enumerate(workflow.processes)}
    dependencies: dict[str, set[str]] = {
        process.name: set() for process in workflow.processes
    }
    for connection in workflow.connections:
        if connection.from_process is not None and connection.to_process is not None:
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


def _workflow_input_names(workflow: NextflowWorkflow) -> list[str]:
    names: list[str] = []
    for connection in workflow.connections:
        if connection.from_process is None and connection.from_port not in names:
            names.append(connection.from_port)
    return names


def _render_named_workflow(workflow: NextflowWorkflow) -> str:
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
                if port.name in process.directives.get("_optional_inputs", "").split(","):
                    arguments.append("Channel.empty().ifEmpty(null)")
                    continue
                raise ValueError(f"process input {process.name}.{port.name} is not connected")
            arguments.append(_source_expression(connection, processes))
        lines.append(f"    {process.name}({', '.join(arguments)})")

    workflow_outputs = [connection for connection in workflow.connections if connection.to_process is None]
    if workflow_outputs:
        lines.append("    emit:")
        for connection in workflow_outputs:
            lines.append(
                f"    {connection.to_port} = {_source_expression(connection, processes)}"
            )
    lines.append("}")
    return "\n".join(lines)


def _workflow_input_port(workflow: NextflowWorkflow, name: str) -> tuple[NfProcess, NfPort]:
    processes = _process_map(workflow)
    for connection in workflow.connections:
        if connection.from_process is None and connection.from_port == name and connection.to_process is not None:
            process = processes[connection.to_process]
            return process, next(port for port in process.inputs if port.name == connection.to_port)
    raise ValueError(f"workflow input {name!r} is not connected")


def _parameter_expression(workflow: NextflowWorkflow, name: str) -> str:
    process, port = _workflow_input_port(workflow, name)
    value: Any = workflow.params.get(name)
    if port.qualifier == "path":
        if isinstance(value, list):
            return f"Channel.fromPath(params.{name}.collect {{ it.path ?: it }}, checkIfExists: true)"
        if isinstance(value, Mapping):
            return f"Channel.fromPath(params.{name}.path, checkIfExists: true)"
        return f"Channel.fromPath(params.{name}, checkIfExists: true)"
    optional = name in process.directives.get("_optional_inputs", "").split(",")
    if optional and value is None:
        return "Channel.empty().ifEmpty(null)"
    return f"Channel.value(params.{name})"


def render_nextflow(workflow: NextflowWorkflow) -> str:
    """Render a supported workflow as deterministic Nextflow DSL2 source."""
    sections = ["nextflow.enable.dsl=2"]
    sections.extend(_render_process(process) for process in workflow.processes)
    sections.append(_render_named_workflow(workflow))
    arguments = ",\n        ".join(
        _parameter_expression(workflow, name) for name in _workflow_input_names(workflow)
    )
    invocation = f"    {workflow.name}({arguments})" if arguments else f"    {workflow.name}()"
    sections.append(f"workflow {{\n{invocation}\n}}")
    return "\n\n".join(sections) + "\n"


def render_nextflow_config(workflow: NextflowWorkflow) -> str:
    """Render the deterministic executor configuration."""
    docker_enabled = any(process.container for process in workflow.processes)
    return f"docker.enabled = {str(docker_enabled).lower()}\n"


def render_nextflow_params(workflow: NextflowWorkflow) -> str:
    """Render workflow parameters as deterministic JSON."""
    return json.dumps(workflow.params, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def write_nextflow_files(
    workflow: NextflowWorkflow,
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
    workflow: NextflowWorkflow,
    outdir: str | Path,
) -> tuple[Path, Path, Path, Path]:
    """Write the JSON IR and all three executable Nextflow artifacts."""
    json_path = write_nextflow_json(workflow, outdir)
    script, config, params = write_nextflow_files(workflow, outdir)
    return json_path, script, config, params
