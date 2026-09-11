"""Deterministic serializers for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .nf_types import (
    ExecutableNextflowWorkflow,
    NF_SHELL_QUOTE_HELPER,
    NfArrayBinding,
    NfBasenameReference,
    NfConnection,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfShellLiteral,
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


def _render_array_binding(token: NfArrayBinding) -> str:
    """Render an array binding: nothing when empty, else prefix once plus each item."""
    items_expression = f"{token.name}.collect{{ {NF_SHELL_QUOTE_HELPER}(it.toString()) }}"
    if token.prefix is not None:
        quoted_prefix = f"{NF_SHELL_QUOTE_HELPER}({_groovy_literal(token.prefix)})"
        joined = f"([{quoted_prefix}] + {items_expression}).join(' ')"
    else:
        joined = f"{items_expression}.join(' ')"
    return f"${{{token.name}.isEmpty() ? '' : {joined}}}"


def _render_shell_literal(token: NfShellLiteral) -> str:
    """Render one approved shellQuote:false literal exactly as written, unquoted.

    Bypasses the shell-quoting helper entirely: the text is a CWL-author
    literal with no input reference, proven by capability analysis before
    this token can exist, so only the enclosing GString needs escaping.
    """
    return _groovy_gstring_fragment(token.text)


def _render_command_token(token: Any) -> str:
    """Render one argv token; flags and array bindings collapse away when falsy/empty."""
    if isinstance(token, NfFlag):
        quoted = f"{NF_SHELL_QUOTE_HELPER}({_groovy_literal(token.prefix)})"
        return f"${{{token.name} ? {quoted} : ''}}"
    if isinstance(token, NfArrayBinding):
        return _render_array_binding(token)
    if isinstance(token, NfShellLiteral):
        return _render_shell_literal(token)
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


_GLOB_METACHARACTERS = frozenset("*?[]{}")


def _glob_names_one_file(template: Any) -> bool:
    """True when an assembled output name is meant literally, not as a pattern.

    Nextflow pattern-matches an output ``path`` name, so a name assembled
    from a staged file's own name is matched against whatever metacharacters
    that name happens to carry: a staged ``a[1].txt`` yields the pattern
    ``a[1].txt``, whose ``[1]`` is a character class that cannot match the
    file the process wrote.

    Only a basename reference makes the name literal by construction -- it
    is some real file's name, never a pattern. A plain input reference
    carries arbitrary data, and a glob *is* a legitimate thing to pass as
    an input (``glob: $(inputs.pattern)`` with ``pattern="*.txt")``, so
    turning globbing off there would break a pattern that is meant to
    match. Which reference kind is present is decidable here, at compile
    time, where the assembled value is not; so the rule keys on the kind.

    A metacharacter written into a literal part is the author asking for a
    pattern and is left alone, and an all-literal name is unchanged.
    """
    references = [segment for segment in template.segments if not isinstance(segment, NfLiteral)]
    if not references:
        return False
    if not all(isinstance(segment, NfBasenameReference) for segment in references):
        return False
    return not any(
        isinstance(segment, NfLiteral) and _GLOB_METACHARACTERS & set(segment.value)
        for segment in template.segments
    )


def _process_output(port: NfPort) -> str:
    # NfProcess guarantees every output is a path port with a typed glob.
    assert port.glob is not None
    literal = ", glob: false" if _glob_names_one_file(port.glob) else ""
    return f"path {_render_glob(port.glob)}{literal}, emit: {port.emit or port.name}"


def _process_input(port: NfPort) -> str:
    if port.stage_as is not None:
        return f"{port.qualifier} {port.name}, stageAs: {_groovy_literal(port.stage_as)}"
    return f"{port.qualifier} {port.name}"


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
        lines.extend(f"    {_process_input(port)}" for port in process.inputs)
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


_ADAPTER_OPERATORS = {"scatter": ".flatten()"}


def _source_expression(connection: NfConnection, processes: Mapping[str, NfProcess]) -> str:
    match connection:
        case NfWorkflowInputConnection(from_port, _, _, adapter):
            # The adapter is applied per consumption site, so each scattered
            # sink derives its own queue channel from the shared parameter.
            return from_port + (_ADAPTER_OPERATORS[adapter] if adapter else "")
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


def _workflow_input_sink(
    workflow: ExecutableNextflowWorkflow,
    name: str,
) -> tuple[NfWorkflowInputConnection, NfPort]:
    processes = _process_map(workflow)
    for connection in workflow.connections:
        if isinstance(connection, NfWorkflowInputConnection) and connection.from_port == name:
            process = processes[connection.to_process]
            port = next(port for port in process.inputs if port.name == connection.to_port)
            return connection, port
    raise ValueError(f"workflow input {name!r} is not connected")


def _parameter_expression(workflow: ExecutableNextflowWorkflow, name: str) -> str:
    connection, port = _workflow_input_sink(workflow, name)
    # A scatter-adapted parameter carries the whole source array; the graph
    # validator keeps every sink of one parameter in agreement, so one sink
    # decides the construction for all of them.
    carries_list = port.is_array or connection.adapter == "scatter"
    if port.qualifier == "path":
        path_type = "dir" if port.path_kind == "directory" else "file"
        if carries_list:
            # One Channel.value(...) element holding a Groovy list, so
            # Nextflow stages every element for a single process call
            # instead of fanning the channel out over several calls.
            return (
                f"Channel.value(params.{name}.collect {{ entry -> file("
                f"entry instanceof Map ? entry.path : entry, "
                f"checkIfExists: true, type: '{path_type}') }})"
            )
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
