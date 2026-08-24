"""Reader and structural importer for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
import warnings

from .nf_types import (
    ExecutableNextflowWorkflow,
    NfConnection,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from .input_output_nf import NF_SHELL_QUOTE_FUNCTION, render_nextflow

if TYPE_CHECKING:
    from .api.python.workflow import Workflow


_PROCESS = re.compile(r"^\s*process\s+(\S+)\s*\{\s*$")
_WORKFLOW = re.compile(r"^\s*workflow(?:\s+(\S+))?\s*\{\s*$")
_PORT = re.compile(r"^(path|val|tuple|env|stdin)\s+(.+?)(?:,\s*emit:\s*(\S+))?$")
_CALL = re.compile(r"^(\S+)\((.*)\)$")
_PROCESS_OUTPUT = re.compile(r"^(\S+)\.out\.(\S+)$")


@dataclass(frozen=True, slots=True)
class NextflowPort:
    """Recognized structural process port."""

    name: str
    qualifier: str
    emit: str | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class NextflowProcess:
    """Recognized structural process syntax; not executable IR."""

    name: str
    inputs: tuple[NextflowPort, ...]
    outputs: tuple[NextflowPort, ...]
    script: str
    container: str | None = None
    cpus: str | None = None
    memory: str | None = None


@dataclass(frozen=True, slots=True)
class NextflowDocument:
    """Loss-aware structural representation of parsed Nextflow source."""

    name: str
    processes: tuple[NextflowProcess, ...]
    connections: tuple[NfConnection, ...]
    params: Mapping[str, Any]
    source_text: str = field(repr=False, compare=False)
    opaque_regions: tuple[str, ...] = ()
    verified_executable: ExecutableNextflowWorkflow | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "processes", tuple(self.processes))
        object.__setattr__(self, "connections", tuple(self.connections))
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(self, "opaque_regions", tuple(self.opaque_regions))
        if not isinstance(self.source_text, str):
            raise TypeError("source_text must be a string")
        if self.verified_executable is not None and not isinstance(
            self.verified_executable, ExecutableNextflowWorkflow
        ):
            raise TypeError(
                "verified_executable must be an ExecutableNextflowWorkflow or None"
            )


def _blocks(lines: list[str], opener: re.Pattern[str]) -> list[tuple[str | None, int, int]]:
    blocks: list[tuple[str | None, int, int]] = []
    inside_script = False
    for start, line in enumerate(lines):
        if line.strip() == '"""':
            inside_script = not inside_script
            continue
        if inside_script:
            continue
        match = opener.match(line)
        if match is None:
            continue
        depth = 0
        in_script = False
        for end in range(start, len(lines)):
            current = lines[end]
            if current.strip() == '"""':
                in_script = not in_script
                continue
            if not in_script:
                depth += current.count("{") - current.count("}")
            if depth == 0:
                blocks.append((match.group(1), start, end))
                break
        else:
            raise ValueError(f"unterminated Nextflow block beginning on line {start + 1}")
    return blocks


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        parsed = json.loads(value)
        if isinstance(parsed, str):
            return parsed
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("\\'", "'")
    return value


def _parse_process(name: str, body: list[str]) -> tuple[NextflowProcess, tuple[str, ...]]:
    inputs: list[NextflowPort] = []
    outputs: list[NextflowPort] = []
    container: str | None = None
    cpus: str | None = None
    memory: str | None = None
    script: list[str] = []
    unparsed: list[str] = []
    section = "directives"
    in_script = False

    for raw_line in body:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in {"input:", "output:", "script:"}:
            section = stripped[:-1]
            continue
        if stripped == '"""':
            if section != "script":
                unparsed.append(stripped)
            else:
                in_script = not in_script
            continue
        if in_script:
            script.append(raw_line[4:] if raw_line.startswith("    ") else raw_line)
            continue
        if section == "input":
            match = _PORT.match(stripped)
            if match is None or match.group(3) is not None:
                unparsed.append(stripped)
                continue
            inputs.append(NextflowPort(match.group(2), match.group(1)))
            continue
        if section == "output":
            match = _PORT.match(stripped)
            if match is None:
                unparsed.append(stripped)
                continue
            qualifier, target, emit = match.groups()
            port_name = emit or _unquote(target)
            outputs.append(NextflowPort(port_name, qualifier, emit or port_name, _unquote(target)))
            continue
        if stripped.startswith("container "):
            container = _unquote(stripped.removeprefix("container "))
        elif stripped.startswith("cpus "):
            cpus = stripped.removeprefix("cpus ").strip()
        elif stripped.startswith("memory "):
            memory = _unquote(stripped.removeprefix("memory "))
        elif stripped.startswith("// TODO: scatter"):
            unparsed.append(stripped)
        else:
            unparsed.append(stripped)

    if not script:
        raise ValueError(f"process {name!r} does not contain a supported script block")
    return (
        NextflowProcess(name, tuple(inputs), tuple(outputs), "\n".join(script), container, cpus, memory),
        tuple(unparsed),
    )


def _split_arguments(value: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character in "([{":
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1
    final = value[start:].strip()
    if final:
        arguments.append(final)
    return arguments


def _output_name(process: NextflowProcess, emit: str) -> str:
    for port in process.outputs:
        if emit in {port.name, port.emit}:
            return port.name
    raise ValueError(f"workflow references unknown output {process.name}.out.{emit}")


def _parse_workflow(
    name: str,
    body: list[str],
    processes: list[NextflowProcess],
) -> tuple[list[NfConnection], str | None]:
    process_by_name = {process.name: process for process in processes}
    connections: list[NfConnection] = []
    unparsed: list[str] = []
    section = ""
    for raw_line in body:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in {"take:", "main:", "emit:"}:
            section = stripped[:-1]
            continue
        if section == "take":
            continue
        if section == "main":
            match = _CALL.match(stripped)
            if match is None or match.group(1) not in process_by_name:
                unparsed.append(stripped)
                continue
            process = process_by_name[match.group(1)]
            arguments = _split_arguments(match.group(2))
            if len(arguments) != len(process.inputs):
                raise ValueError(
                    f"workflow call {process.name} supplies {len(arguments)} inputs; "
                    f"the process declares {len(process.inputs)}"
                )
            for port, argument in zip(process.inputs, arguments, strict=True):
                source = _PROCESS_OUTPUT.match(argument)
                if source is None:
                    if any(token in argument for token in ("(", ")", "{", "}")):
                        unparsed.append(stripped)
                        continue
                    connections.append(
                        NfWorkflowInputConnection(argument, process.name, port.name)
                    )
                    continue
                source_process = process_by_name.get(source.group(1))
                if source_process is None:
                    raise ValueError(f"workflow references unknown process {source.group(1)!r}")
                connections.append(NfProcessConnection(
                    source_process.name,
                    _output_name(source_process, source.group(2)),
                    process.name,
                    port.name,
                ))
            continue
        if section == "emit":
            if "=" in stripped:
                output_name, expression = (part.strip() for part in stripped.split("=", maxsplit=1))
            else:
                output_name, expression = stripped, stripped
            source = _PROCESS_OUTPUT.match(expression)
            if source is None or source.group(1) not in process_by_name:
                unparsed.append(stripped)
                continue
            process = process_by_name[source.group(1)]
            connections.append(NfWorkflowOutputConnection(
                process.name,
                _output_name(process, source.group(2)),
                output_name,
            ))
            continue
        unparsed.append(stripped)
    return connections, "\n".join(unparsed) or None


def _validate_acyclic(workflow: NextflowDocument) -> None:
    dependencies: dict[str, set[str]] = {process.name: set() for process in workflow.processes}
    for connection in workflow.connections:
        if isinstance(connection, NfProcessConnection):
            dependencies[connection.to_process].add(connection.from_process)
    remaining = set(dependencies)
    while remaining:
        ready = {name for name in remaining if not (dependencies[name] & remaining)}
        if not ready:
            raise ValueError("imported Nextflow workflow contains a cycle")
        remaining -= ready


def parse_nf_text(text: str, *, params: Mapping[str, Any] | None = None) -> NextflowDocument:
    """Parse the supported, generated-style Nextflow DSL2 subset."""
    lines = text.splitlines()
    process_blocks = _blocks(lines, _PROCESS)
    parsed_processes = [
        _parse_process(name or "", lines[start + 1:end])
        for name, start, end in process_blocks
    ]
    processes = [process for process, _opaque in parsed_processes]
    process_names = [process.name for process in processes]
    if len(process_names) != len(set(process_names)):
        raise ValueError("supported Nextflow input cannot contain duplicate process names")
    workflow_blocks = _blocks(lines, _WORKFLOW)
    named = [(name, start, end) for name, start, end in workflow_blocks if name is not None]
    if len(named) != 1:
        raise ValueError("supported Nextflow input must contain exactly one named workflow")
    workflow_name, start, end = named[0]
    assert workflow_name is not None
    connections, workflow_unparsed = _parse_workflow(
        workflow_name,
        lines[start + 1:end],
        processes,
    )

    covered: set[int] = set()
    for _name, block_start, block_end in [*process_blocks, *workflow_blocks]:
        covered.update(range(block_start, block_end + 1))
    helper_lines = NF_SHELL_QUOTE_FUNCTION.splitlines()
    for helper_start in range(len(lines) - len(helper_lines) + 1):
        if lines[helper_start:helper_start + len(helper_lines)] == helper_lines:
            covered.update(range(helper_start, helper_start + len(helper_lines)))
    global_unparsed = [
        line.strip()
        for index, line in enumerate(lines)
        if index not in covered
        and line.strip()
        and line.strip() != "nextflow.enable.dsl=2"
    ]
    opaque_regions = [
        f"process {process.name}: {item}"
        for process, opaque in parsed_processes
        for item in opaque
    ]
    opaque_regions.extend(
        item for item in [workflow_unparsed, *global_unparsed] if item
    )
    workflow = NextflowDocument(
        workflow_name,
        tuple(processes),
        tuple(connections),
        dict(params or {}),
        text,
        tuple(opaque_regions),
    )
    _validate_acyclic(workflow)
    return workflow


def parse_nf_file(path: str | Path) -> NextflowDocument:
    """Read a Nextflow file and its adjacent generated parameter file when present."""
    source = Path(path)
    params_path = source.with_name("nextflow_params.json")
    params: Mapping[str, Any] = {}
    if params_path.exists():
        loaded = json.loads(params_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError("nextflow_params.json must contain a JSON object")
        params = loaded
    source_text = source.read_text(encoding="utf-8")
    document = parse_nf_text(source_text, params=params)
    ir_path = source.with_name("nextflow_workflow.json")
    if not ir_path.exists():
        return document
    executable = ExecutableNextflowWorkflow.from_json(ir_path.read_text(encoding="utf-8"))
    if render_nextflow(executable) != source_text:
        raise ValueError("generated Nextflow source does not match its executable IR artifact")
    if dict(executable.params) != dict(params):
        raise ValueError("generated Nextflow parameters do not match its executable IR artifact")
    return replace(document, verified_executable=executable)


def promote_nextflow_document(
    document: NextflowDocument,
) -> ExecutableNextflowWorkflow:
    """Validate and promote a fully understood document to executable IR."""
    if not isinstance(document, NextflowDocument):
        raise TypeError("promotion requires a NextflowDocument")
    if document.opaque_regions:
        raise ValueError(
            "cannot promote a NextflowDocument containing opaque regions"
        )
    if document.verified_executable is None:
        raise ValueError(
            "cannot promote parsed source without a matching validated executable IR artifact"
        )
    if render_nextflow(document.verified_executable) != document.source_text:
        raise ValueError(
            "cannot promote a NextflowDocument whose source does not match its "
            "validated executable IR artifact"
        )
    if dict(document.verified_executable.params) != dict(document.params):
        raise ValueError(
            "cannot promote a NextflowDocument whose parameters do not match its "
            "validated executable IR artifact"
        )
    return document.verified_executable


def render_nextflow_document(document: NextflowDocument) -> str:
    """Render a structural document losslessly without claiming executability."""
    if not isinstance(document, NextflowDocument):
        raise TypeError("structural rendering requires a NextflowDocument")
    return document.source_text


def _cwl_type(port: NextflowPort) -> Any:
    return "File" if port.qualifier == "path" else "Any"


def nextflow_to_cwl(workflow: NextflowDocument) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert supported Nextflow structure into CWL workflow/tool dictionaries."""
    tools: list[dict[str, Any]] = []
    for process in workflow.processes:
        requirements: dict[str, Any] = {"ShellCommandRequirement": {}}
        if process.container is not None:
            requirements["DockerRequirement"] = {"dockerPull": process.container}
        resources: dict[str, Any] = {}
        if cpus := process.cpus:
            resources["coresMin"] = float(cpus) if "." in cpus else int(cpus)
        if memory := process.memory:
            resources["ramMin"] = float(memory.removesuffix(" MB"))
        if resources:
            requirements["ResourceRequirement"] = resources
        outputs: dict[str, Any] = {}
        for port in process.outputs:
            output: dict[str, Any] = {"type": _cwl_type(port)}
            if port.target is not None:
                output["outputBinding"] = {"glob": port.target}
            outputs[port.name] = output
        tools.append({
            "id": process.name,
            "class": "CommandLineTool",
            "cwlVersion": "v1.2",
            "baseCommand": ["bash", "-c"],
            "arguments": [process.script],
            "inputs": {port.name: {"type": _cwl_type(port)} for port in process.inputs},
            "outputs": outputs,
            "requirements": requirements,
        })

    incoming: dict[
        tuple[str, str],
        NfWorkflowInputConnection | NfProcessConnection,
    ] = {}
    for connection in workflow.connections:
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection)):
            incoming[(connection.to_process, connection.to_port)] = connection
    steps: list[dict[str, Any]] = []
    workflow_inputs: dict[str, Any] = {}
    for process in workflow.processes:
        step_inputs: dict[str, str] = {}
        for port in process.inputs:
            incoming_connection = incoming.get((process.name, port.name))
            if incoming_connection is None:
                continue
            if isinstance(incoming_connection, NfWorkflowInputConnection):
                step_inputs[port.name] = incoming_connection.from_port
                workflow_inputs.setdefault(
                    incoming_connection.from_port,
                    {"type": _cwl_type(port)},
                )
            else:
                step_inputs[port.name] = (
                    f"{incoming_connection.from_process}/{incoming_connection.from_port}"
                )
        steps.append({
            "id": process.name,
            "in": step_inputs,
            "out": [port.name for port in process.outputs],
            "run": f"{process.name}.cwl",
        })
    workflow_outputs = {
        connection.to_port: {
            "type": _cwl_type(next(
                port
                for process in workflow.processes if process.name == connection.from_process
                for port in process.outputs if port.name == connection.from_port
            )),
            "outputSource": f"{connection.from_process}/{connection.from_port}",
        }
        for connection in workflow.connections
        if isinstance(connection, NfWorkflowOutputConnection)
    }
    cwl_workflow = {
        "id": workflow.name,
        "class": "Workflow",
        "cwlVersion": "v1.2",
        "inputs": workflow_inputs,
        "outputs": workflow_outputs,
        "steps": steps,
    }
    return cwl_workflow, tools


def import_nextflow(path: str | Path) -> "Workflow":
    """Import supported Nextflow structure as a public Sophios ``Workflow``."""
    from .api.python.workflow import Step, Workflow  # Avoid a public API import cycle.

    nf_workflow = parse_nf_file(path)
    _cwl_workflow, tool_documents = nextflow_to_cwl(nf_workflow)
    if nf_workflow.opaque_regions:
        warnings.warn(
            "unsupported Nextflow content was retained as opaque regions and cannot be represented in CWL",
            UserWarning,
            stacklevel=2,
        )
    warnings.warn(
        "Nextflow scripts are opaque during structural CWL import; executable equivalence is not guaranteed",
        UserWarning,
        stacklevel=2,
    )

    steps = [
        Step.from_cwl_document(document, process_name=document["id"])
        for document in tool_documents
    ]
    step_by_name = {step.process_name: step for step in steps}
    workflow = Workflow(steps, nf_workflow.name)
    process_by_name = {process.name: process for process in nf_workflow.processes}
    for connection in nf_workflow.connections:
        if isinstance(connection, NfWorkflowOutputConnection):
            workflow.add_output(
                connection.to_port,
                step_by_name[connection.from_process].get_output(connection.from_port),
            )
            continue
        destination = step_by_name[connection.to_process]
        if isinstance(connection, NfWorkflowInputConnection):
            destination_process = process_by_name[connection.to_process]
            destination_port = next(
                port for port in destination_process.inputs if port.name == connection.to_port
            )
            workflow.add_input(connection.from_port, _cwl_type(destination_port))
            if connection.from_port in nf_workflow.params:
                workflow.bind_input(connection.from_port, nf_workflow.params[connection.from_port])
            destination.bind_input(
                connection.to_port,
                getattr(workflow.inputs, connection.from_port),
            )
        else:
            destination.bind_input(
                connection.to_port,
                step_by_name[connection.from_process].get_output(connection.from_port),
            )
    return workflow
