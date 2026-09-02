"""Deterministic serializers for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .nf_types import (
    ExecutableNextflowWorkflow,
    NF_SHELL_QUOTE_HELPER,
    NfBasenameReference,
    NfConnection,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
    process_dependencies,
    topological_order,
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


_CONTROL_ESCAPES = {code: f"\\u{code:04x}" for code in (*range(32), 127)}
_GROOVY_LITERAL_TABLE = {ord("\\"): "\\\\", ord("'"): "\\'", **_CONTROL_ESCAPES}
_GSTRING_FRAGMENT_TABLE = {ord("\\"): "\\\\", ord('"'): '\\"', ord("$"): "\\$", **_CONTROL_ESCAPES}


def _groovy_literal(value: str) -> str:
    return f"'{value.translate(_GROOVY_LITERAL_TABLE)}'"


def _groovy_gstring_fragment(value: str) -> str:
    """Escape literal data embedded beside typed GString references."""
    return value.translate(_GSTRING_FRAGMENT_TABLE)


def _segment_expression(segment: Any) -> str:
    match segment:
        case NfInputReference():
            return f"{segment.name}.toString()"
        case NfBasenameReference():
            return f"{segment.name}.name.toString()"
        case _:
            return _groovy_literal(segment.value)


def _template_expression(template: Any) -> str:
    return " + ".join(_segment_expression(segment) for segment in template.segments)


def _shell_quote(value: str) -> str:
    """Return the POSIX shell word produced by the generated Groovy helper."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_template(template: Any) -> str:
    """Render one typed token through the generated shell-quoting function."""
    return f"${{{NF_SHELL_QUOTE_HELPER}({_template_expression(template)})}}"


def _render_command_token(token: Any) -> str:
    """Render one argv token; flags collapse to nothing when their input is false."""
    if isinstance(token, NfFlag):
        quoted = f"{NF_SHELL_QUOTE_HELPER}({_groovy_literal(token.prefix)})"
        return f"${{{token.name} ? {quoted} : ''}}"
    return _render_template(token)


def _render_glob(template: Any) -> str:
    if all(isinstance(segment, NfLiteral) for segment in template.segments):
        return _groovy_literal("".join(segment.value for segment in template.segments))
    rendered: list[str] = []
    for segment in template.segments:
        match segment:
            case NfInputReference():
                rendered.append(f"${{{segment.name}}}")
            case NfBasenameReference():
                rendered.append(f"${{{segment.name}.name}}")
            case _:
                rendered.append(_groovy_gstring_fragment(segment.value))
    return f'"{"".join(rendered)}"'


def _render_number(value: int | float) -> str:
    """Render a validated JSON number without exponent notation."""
    if isinstance(value, int):
        return str(value)
    return format(Decimal(str(value)), "f")


def _process_output(port: NfPort) -> str:
    # NfProcess guarantees every output is a path port with a typed glob.
    assert port.glob is not None
    return f"path {_render_glob(port.glob)}, emit: {port.emit or port.name}"


def _render_process(process: NfProcess) -> str:
    lines = [f"process {process.name} {{"]
    if process.container is not None:
        lines.append(f"    container {_groovy_literal(process.container)}")
    if process.resources.cpus is not None:
        lines.append(f"    cpus {process.resources.cpus}")
    if process.resources.memory_mb is not None:
        lines.append(f'    memory "{_render_number(process.resources.memory_mb)} MB"')

    if process.inputs:
        lines.extend(["", "    input:"])
        lines.extend(f"    {port.qualifier} {port.name}" for port in process.inputs)
    if process.outputs:
        lines.extend(["", "    output:"])
        lines.extend(f"    {_process_output(port)}" for port in process.outputs)

    command = " ".join(_render_command_token(token) for token in process.command.tokens)
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
    # The executable IR rejects multiple sources per input at construction.
    return {
        (connection.to_process, connection.to_port): connection
        for connection in workflow.connections
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
    }


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
    names = topological_order(
        process_dependencies(position, workflow.connections),
        error="Nextflow workflow connections contain a cycle",
        key=position.__getitem__,
    )
    return [workflow.processes[position[name]] for name in names]


def _workflow_input_names(workflow: ExecutableNextflowWorkflow) -> list[str]:
    return list(dict.fromkeys(
        connection.from_port
        for connection in workflow.connections
        if isinstance(connection, NfWorkflowInputConnection)
    ))


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
        # The executable IR guarantees every process input is connected.
        arguments = ", ".join(
            _source_expression(incoming[(process.name, port.name)], processes)
            for port in process.inputs
        )
        lines.append(f"    {process.name}({arguments})")

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
) -> NfPort:
    processes = _process_map(workflow)
    for connection in workflow.connections:
        if isinstance(connection, NfWorkflowInputConnection) and connection.from_port == name:
            process = processes[connection.to_process]
            return next(port for port in process.inputs if port.name == connection.to_port)
    raise ValueError(f"workflow input {name!r} is not connected")


def _parameter_expression(workflow: ExecutableNextflowWorkflow, name: str) -> str:
    port = _workflow_input_port(workflow, name)
    if port.qualifier == "path":
        path_type = "dir" if port.path_kind == "directory" else "file"
        return (
            f"Channel.fromPath(params.{name} instanceof Map ? params.{name}.path : "
            f"params.{name}, checkIfExists: true, type: '{path_type}', glob: false)"
        )
    return f"Channel.value(params.{name})"


def render_nextflow(workflow: ExecutableNextflowWorkflow) -> str:
    """Render a supported workflow as deterministic Nextflow DSL2 source.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR; no
            other representation is accepted.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The complete ``workflow.nf`` text; byte-stable across calls.
    """
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
    """Render the deterministic executor configuration.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The ``nextflow.config`` text carrying the validated
            workflow-wide container policy.
    """
    _require_executable(workflow)
    return f"docker.enabled = {str(workflow.containers_enabled).lower()}\n"


def render_nextflow_params(workflow: ExecutableNextflowWorkflow) -> str:
    """Render workflow parameters as deterministic JSON.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The ``nextflow_params.json`` text with sorted keys.
    """
    _require_executable(workflow)
    return json.dumps(
        workflow.to_dict()["params"],
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def write_nextflow_artifacts(
    workflow: ExecutableNextflowWorkflow,
    outdir: str | Path,
) -> tuple[Path, Path, Path, Path]:
    """Validate every representation before writing the four artifacts.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.
        outdir (str | Path): Output directory; created when missing.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        tuple[Path, Path, Path, Path]: Paths to the versioned JSON IR,
            ``workflow.nf``, ``nextflow.config``, and
            ``nextflow_params.json``, in that order.
    """
    _require_executable(workflow)
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
