"""Convert compiled Sophios RoseTrees into the Nextflow intermediate representation."""

from collections.abc import Mapping
import copy
import json
from os import PathLike
import re
import shlex
from typing import Any

from .nf_symbols import normalize_nextflow_identifier
from .nf_types import NfConnection, NfPort, NfProcess, NextflowWorkflow
from .wic_types import NodeData, RoseTree


def _identifier(value: Any, *, context: str) -> str:
    """Normalize a CWL identifier into a stable Nextflow identifier."""
    match value:
        case str() if value:
            pass
        case _:
            raise ValueError(f"{context} must be a non-empty string")
    local = value.rsplit("#", maxsplit=1)[-1]
    if not local:
        raise ValueError(f"{context} cannot be normalized to a Nextflow identifier")
    return normalize_nextflow_identifier(local)


def _as_mapping(value: Any, *, error: str) -> Mapping[str, Any]:
    match value:
        case Mapping() as mapping:
            return mapping
        case _:
            raise ValueError(error)


def _as_list(value: Any, *, error: str) -> list[Any]:
    match value:
        case list() as items:
            return items
        case _:
            raise ValueError(error)


def _is_optional(cwl_type: Any) -> bool:
    match cwl_type:
        case str() as type_name:
            return type_name.endswith("?")
        case list() as union:
            return "null" in union
        case _:
            return False


def _required_type(cwl_type: Any) -> Any:
    match cwl_type:
        case str() as type_name if type_name.endswith("?"):
            return type_name[:-1]
        case list() as union if "null" in union:
            remaining = [item for item in union if item != "null"]
            if len(remaining) != 1:
                raise ValueError(f"unsupported optional CWL union {cwl_type!r}")
            return remaining[0]
        case _:
            return cwl_type


def cwl_type_to_nf_qualifier(cwl_type: Any) -> str:
    """Map the documented Phase 1 CWL type subset to a Nextflow qualifier."""
    if _is_optional(cwl_type):
        return "val"
    match _required_type(cwl_type):
        case "File" | "Directory" | "File[]":
            return "path"
        case "string" | "int" | "float" | "boolean" | "Any":
            return "val"
        case {"type": "array", "items": "File"}:
            return "path"
        case _:
            raise ValueError(f"unsupported CWL type for Nextflow Phase 1: {cwl_type!r}")


def _ports(raw_ports: Any, *, outputs: bool) -> list[NfPort]:
    port_definitions = _as_mapping(raw_ports, error="CommandLineTool ports must be a mapping")
    ports: list[NfPort] = []
    for raw_name, raw_definition in port_definitions.items():
        match raw_definition:
            case {"type": cwl_type}:
                name = _identifier(raw_name, context="port name")
                ports.append(
                    NfPort(
                        name,
                        cwl_type_to_nf_qualifier(cwl_type),
                        emit=name if outputs else None,
                    )
                )
            case _:
                raise ValueError(f"CWL port {raw_name!r} must declare a type")
    return ports


_INPUT_EXPRESSION = re.compile(
    r"\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)(?:\.(?:path|basename))?\s*\)"
)


def _translate_input_expressions(value: str) -> str:
    return _INPUT_EXPRESSION.sub(lambda match: f"${_identifier(match.group(1), context='input reference')}", value)


def _shell_literal(value: Any) -> str:
    match value:
        case bool() as boolean:
            return "true" if boolean else "false"
        case (int() | float()) as number:
            return str(number)
        case str() as text:
            translated = _translate_input_expressions(text)
            if "$" in translated or any(character.isspace() for character in translated):
                return translated if "$" in translated else shlex.quote(translated)
            return shlex.quote(translated)
        case _:
            raise ValueError(f"unsupported CWL command value {value!r}")


def _position(value: Any, *, default: int) -> int:
    match value:
        case None:
            return default
        case int() as position:
            return position
        case str() as position:
            try:
                return int(position)
            except ValueError as exc:
                raise ValueError(f"unsupported non-integer CWL command position {value!r}") from exc
        case _:
            raise ValueError(f"unsupported CWL command position {value!r}")


def _binding_token(prefix: Any, value: str, *, separate: Any = True) -> str:
    match prefix:
        case None:
            return value
        case str() as text:
            rendered_prefix = shlex.quote(text)
            return f"{rendered_prefix} {value}" if separate is not False else f"{rendered_prefix}{value}"
        case _:
            raise ValueError("CWL command prefix must be a string")


def _argument_items(arguments: list[Any]) -> list[tuple[int, int, str]]:
    items: list[tuple[int, int, str]] = []
    for index, argument in enumerate(arguments):
        match argument:
            case {"valueFrom": value_from}:
                value = _shell_literal(value_from)
                token = _binding_token(argument.get("prefix"), value, separate=argument.get("separate", True))
                item_position = _position(argument.get("position"), default=index)
            case Mapping():
                raise ValueError("mapped CWL arguments must contain valueFrom")
            case _:
                token = _shell_literal(argument)
                item_position = index
        items.append((item_position, index, token))
    return items


def _input_binding_items(
    inputs: Mapping[str, Any],
    *,
    start: int,
) -> list[tuple[int, int, str]]:
    items: list[tuple[int, int, str]] = []
    for index, (raw_name, definition) in enumerate(inputs.items(), start=start):
        input_definition = _as_mapping(definition, error=f"CWL input {raw_name!r} must be a mapping")
        match input_definition.get("inputBinding"):
            case None:
                continue
            case Mapping() as binding:
                pass
            case _:
                raise ValueError(f"CWL inputBinding for {raw_name!r} must be a mapping")
        name = _identifier(raw_name, context="input binding name")
        value_from = binding.get("valueFrom")
        value = _shell_literal(value_from) if value_from is not None else f"${name}"
        token = _binding_token(binding.get("prefix"), value, separate=binding.get("separate", True))
        items.append((_position(binding.get("position"), default=index), index, token))
    return items


def _command_items(tool: Mapping[str, Any]) -> list[tuple[int, int, str]]:
    arguments = _as_list(tool.get("arguments", []), error="CommandLineTool arguments must be a list")
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    return sorted(
        [
            *_argument_items(arguments),
            *_input_binding_items(inputs, start=len(arguments)),
        ]
    )


def _script(tool: Mapping[str, Any]) -> str:
    base_command = tool.get("baseCommand")
    tokens: list[str] = []
    match base_command:
        case str() as command if command:
            tokens.append(shlex.quote(command))
        case list() as command:
            for token in command:
                match token:
                    case str():
                        tokens.append(shlex.quote(token))
                    case _:
                        raise ValueError("CommandLineTool baseCommand must be a string or list of strings")
        case None:
            pass
        case _:
            raise ValueError("CommandLineTool baseCommand must be a string or list of strings")

    tokens.extend(token for _position_value, _sequence, token in _command_items(tool))
    command = " ".join(tokens) or "true"
    stdin = tool.get("stdin")
    stdout = tool.get("stdout")
    stderr = tool.get("stderr")
    if stdin is not None:
        command += f" < {_shell_literal(stdin)}"
    if stdout is not None:
        command += f" > {_shell_literal(stdout)}"
    if stderr is not None:
        command += f" 2> {_shell_literal(stderr)}"
    return command


def _requirement(tool: Mapping[str, Any], class_name: str) -> Mapping[str, Any] | None:
    for section_name in ("requirements", "hints"):
        section = tool.get(section_name, {})
        match section:
            case None:
                continue
            case Mapping() as requirements:
                match requirements.get(class_name):
                    case None:
                        continue
                    case Mapping() as requirement:
                        return requirement
                    case _:
                        raise ValueError(f"{class_name} must be a mapping")
            case list() as requirements:
                for requirement in requirements:
                    match requirement:
                        case Mapping() if requirement.get("class") == class_name:
                            return requirement
                        case _:
                            continue
            case _:
                raise ValueError(f"CommandLineTool {section_name} must be a mapping or list")
    return None


def _container(tool: Mapping[str, Any]) -> str | None:
    docker = _requirement(tool, "DockerRequirement")
    if docker is None:
        return None
    match docker.get("dockerPull", docker.get("dockerImageId")):
        case str() as image if image:
            return image
        case _:
            raise ValueError("DockerRequirement must define dockerPull or dockerImageId")


def _resource_value(resource: Mapping[str, Any], minimum: str, maximum: str) -> Any:
    return resource.get(minimum, resource.get(maximum))


def _render_resource(value: Any, *, name: str) -> str:
    match value:
        case bool():
            raise ValueError(f"{name} resource requirement must be numeric")
        case float() as number if number.is_integer():
            return str(int(number))
        case (int() | float()) as number:
            return str(number)
        case _:
            raise ValueError(f"{name} resource requirement must be numeric")


def _resource_directives(tool: Mapping[str, Any]) -> dict[str, str]:
    resource = _requirement(tool, "ResourceRequirement")
    if resource is None:
        return {}
    directives: dict[str, str] = {}
    if (cpus := _resource_value(resource, "coresMin", "coresMax")) is not None:
        directives["cpus"] = _render_resource(cpus, name="CPU")
    if (memory := _resource_value(resource, "ramMin", "ramMax")) is not None:
        directives["memory"] = f"{_render_resource(memory, name='memory')} MB"
    return directives


def _optional_input_directive(tool: Mapping[str, Any]) -> str | None:
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    optional_inputs: list[str] = []
    for name, definition in inputs.items():
        match definition:
            case Mapping() as input_definition if _is_optional(input_definition.get("type")):
                optional_inputs.append(_identifier(name, context="optional input name"))
            case _:
                continue
    return ",".join(optional_inputs) or None


def _output_glob_directives(tool: Mapping[str, Any]) -> dict[str, str]:
    outputs = _as_mapping(tool.get("outputs", {}), error="CommandLineTool outputs must be a mapping")
    directives: dict[str, str] = {}
    for raw_name, definition in outputs.items():
        output_definition = _as_mapping(definition, error=f"CWL output {raw_name!r} must be a mapping")
        match output_definition.get("outputBinding"):
            case None:
                continue
            case Mapping() as binding:
                pass
            case _:
                raise ValueError(f"CWL outputBinding for {raw_name!r} must be a mapping")
        match binding.get("glob"):
            case None:
                continue
            case str() as glob:
                rendered_glob = glob
            case list() as glob:
                rendered_glob = json.dumps(glob, sort_keys=True)
            case _:
                raise ValueError(f"CWL output glob for {raw_name!r} must be a string or list")
        directives[f"_output_glob.{_identifier(raw_name, context='output name')}"] = rendered_glob
    return directives


def _directives(tool: Mapping[str, Any]) -> dict[str, str]:
    directives = _resource_directives(tool)
    if optional_inputs := _optional_input_directive(tool):
        directives["_optional_inputs"] = optional_inputs
    directives.update(_output_glob_directives(tool))
    return directives


def _scatter_inputs(value: Any) -> list[str]:
    match value:
        case None:
            return []
        case str() as scatter:
            return [scatter]
        case list() as scatter if scatter:
            inputs: list[str] = []
            for item in scatter:
                match item:
                    case str():
                        inputs.append(item)
                    case _:
                        raise ValueError("CWL scatter must be a port name or non-empty list of port names")
            return inputs
        case _:
            raise ValueError("CWL scatter must be a port name or non-empty list of port names")


def _scatter_directives(step: Mapping[str, Any], tool: Mapping[str, Any]) -> dict[str, str]:
    scatter_inputs = _scatter_inputs(step.get("scatter"))
    if not scatter_inputs:
        return {}
    process_inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    input_names = {_identifier(name, context="scatter process input") for name in process_inputs}
    normalized_scatter = [_identifier(name, context="scatter input") for name in scatter_inputs]
    if missing := set(normalized_scatter) - input_names:
        raise ValueError(f"scatter references unknown process inputs: {', '.join(sorted(missing))}")
    match step.get("scatterMethod", "dotproduct"):
        case str() as scatter_method if scatter_method:
            return {
                "_scatter": "true",
                "_scatter_inputs": ",".join(normalized_scatter),
                "_scatter_method": scatter_method,
            }
        case _:
            raise ValueError("CWL scatterMethod must be a non-empty string")


def _process(step: Mapping[str, Any], child: RoseTree) -> NfProcess:
    match child:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            raise TypeError("each compiled workflow step must have a RoseTree[NodeData] child")
    if "when" in step:
        raise ValueError("CWL step when conditions are not supported in Nextflow Phase 1")
    if sub_trees:
        raise ValueError("nested workflows are deferred to Phase 2")
    match node_data.compiled_cwl:
        case Mapping() as tool:
            pass
        case _:
            raise ValueError("compiled CommandLineTool must be a mapping")
    match tool.get("class"):
        case "Workflow":
            raise ValueError("nested workflows are deferred to Phase 2")
        case "CommandLineTool":
            pass
        case unsupported_class:
            raise ValueError(f"unsupported compiled step class {unsupported_class!r}")
    directives = _directives(tool)
    directives.update(_scatter_directives(step, tool))

    return NfProcess(
        name=_identifier(step.get("id"), context="workflow step id"),
        inputs=_ports(tool.get("inputs", {}), outputs=False),
        outputs=_ports(tool.get("outputs", {}), outputs=True),
        script=_script(tool),
        container=_container(tool),
        directives=directives,
    )


def _source_values(value: Any, *, context: str) -> list[str]:
    match value:
        case str() as source:
            return [source]
        case list() as sources if sources:
            normalized: list[str] = []
            for source in sources:
                match source:
                    case str():
                        normalized.append(source)
                    case _:
                        raise ValueError(f"{context} must be a source string or list of source strings")
            return normalized
        case {"source": source} as source_definition:
            unsupported = set(source_definition) - {"source"}
            if unsupported:
                raise ValueError(f"unsupported {context} fields: {', '.join(sorted(unsupported))}")
            return _source_values(source, context=context)
        case Mapping():
            raise ValueError(f"{context} must define source")
        case _:
            raise ValueError(f"{context} must be a source string or list of source strings")


def _step_name_map(steps: list[Mapping[str, Any]], processes: list[NfProcess]) -> dict[str, str]:
    names: dict[str, str] = {}
    for step, process in zip(steps, processes, strict=True):
        match step.get("id"):
            case str() as raw_name:
                pass
            case _:
                raise ValueError("workflow step id must be a string")
        for candidate in (raw_name, raw_name.rsplit("#", maxsplit=1)[-1]):
            if candidate in names and names[candidate] != process.name:
                raise ValueError(f"workflow step identifier {candidate!r} is ambiguous")
            names[candidate] = process.name
    return names


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source, context="workflow input source")
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process, step_names.get(raw_process.rsplit("#", maxsplit=1)[-1]))
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port, context="process output source")


def _step_connections(
    steps: list[Mapping[str, Any]],
    processes: list[NfProcess],
    step_names: Mapping[str, str],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    for step, process in zip(steps, processes, strict=True):
        match step.get("in", {}):
            case Mapping() as raw_inputs:
                pass
            case _:
                raise ValueError(f"compiled step {process.name!r} inputs must be a mapping")
        for raw_port, raw_source in raw_inputs.items():
            destination_port = _identifier(raw_port, context="process input destination")
            for source in _source_values(raw_source, context=f"step input {process.name}.{destination_port}"):
                source_process, source_port = _source_endpoint(source, step_names)
                connections.append(NfConnection(source_process, source_port, process.name, destination_port))
    return connections


def _workflow_output_connections(
    workflow: Mapping[str, Any],
    step_names: Mapping[str, str],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    match workflow.get("outputs", {}):
        case Mapping() as raw_outputs:
            pass
        case _:
            raise ValueError("compiled CWL Workflow outputs must be a mapping")
    for raw_port, definition in raw_outputs.items():
        match definition:
            case {"outputSource": output_source}:
                destination_port = _identifier(raw_port, context="workflow output destination")
                for source in _source_values(output_source, context=f"workflow output {destination_port}"):
                    source_process, source_port = _source_endpoint(source, step_names)
                    connections.append(NfConnection(source_process, source_port, None, destination_port))
            case _:
                raise ValueError(f"workflow output {raw_port!r} must define outputSource")
    return connections


def _connections(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    processes: list[NfProcess],
) -> list[NfConnection]:
    step_names = _step_name_map(steps, processes)
    return [
        *_step_connections(steps, processes, step_names),
        *_workflow_output_connections(workflow, step_names),
    ]


def _json_value(value: Any) -> Any:
    match value:
        case Mapping() as mapping:
            return {str(key): _json_value(item) for key, item in mapping.items()}
        case list() as items:
            return [_json_value(item) for item in items]
        case tuple() as items:
            return [_json_value(item) for item in items]
        case PathLike() as path:
            return str(path)
        case _:
            return copy.deepcopy(value)


def _compiled_workflow(rose_tree: RoseTree) -> tuple[NodeData, list[Any], Mapping[str, Any]]:
    match rose_tree:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            raise TypeError("cwl_rosetree_to_nextflow requires a RoseTree[NodeData]")
    match node_data.compiled_cwl:
        case Mapping() as workflow if workflow.get("class") == "Workflow":
            pass
        case _:
            raise ValueError("RoseTree root must contain a compiled CWL Workflow")
    return node_data, sub_trees, workflow


def _workflow_steps(workflow: Mapping[str, Any], *, child_count: int) -> list[Mapping[str, Any]]:
    raw_steps = _as_list(workflow.get("steps", []), error="compiled CWL Workflow steps must be a list")
    steps: list[Mapping[str, Any]] = []
    for step in raw_steps:
        match step:
            case Mapping() as step_definition:
                steps.append(step_definition)
            case _:
                raise ValueError("compiled CWL Workflow steps must be mappings")
    if len(steps) != child_count:
        raise ValueError("compiled CWL steps do not match RoseTree children")
    return steps


def _workflow_params(workflow: Mapping[str, Any], node_data: NodeData) -> dict[str, Any]:
    workflow_inputs = _as_mapping(
        workflow.get("inputs", {}),
        error="compiled CWL Workflow inputs must be a mapping",
    )
    provided_params = _as_mapping(
        node_data.workflow_inputs_file,
        error="compiled workflow input values must be a mapping",
    )
    return {
        _identifier(name, context="workflow input name"): _json_value(provided_params.get(name))
        for name in workflow_inputs
    }


def cwl_rosetree_to_nextflow(rose_tree: RoseTree) -> NextflowWorkflow:
    """Convert a compiled flat CWL RoseTree without invoking inference again."""
    node_data, sub_trees, workflow = _compiled_workflow(rose_tree)
    steps = _workflow_steps(workflow, child_count=len(sub_trees))
    processes = [
        _process(step, child)
        for step, child in zip(steps, sub_trees, strict=True)
    ]
    return NextflowWorkflow(
        name=_identifier(node_data.name, context="workflow name"),
        processes=processes,
        connections=_connections(workflow, list(steps), processes),
        params=_workflow_params(workflow, node_data),
    )
