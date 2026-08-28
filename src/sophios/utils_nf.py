"""Convert compiled Sophios RoseTrees into the Nextflow intermediate representation."""

from collections.abc import Iterable, Mapping
import copy
from os import PathLike
import re
from typing import Any

from .nf_symbols import normalize_nextflow_identifier
from .nf_types import (
    ExecutableNextflowWorkflow,
    NF_INTERNAL_IDENTIFIERS,
    NfCommand,
    NfConnection,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
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
    identifier = normalize_nextflow_identifier(local)
    while identifier in NF_INTERNAL_IDENTIFIERS:
        identifier = f"_{identifier}"
    return identifier


def _normalized_identifiers(values: Iterable[Any], *, context: str) -> dict[Any, str]:
    """Normalize source identifiers and reject every many-to-one mapping."""
    normalized_by_source: dict[Any, str] = {}
    sources_by_normalized: dict[str, list[Any]] = {}
    for source in values:
        normalized = _identifier(source, context=f"{context} identifier")
        normalized_by_source[source] = normalized
        sources_by_normalized.setdefault(normalized, []).append(source)

    collisions = [
        (normalized, sorted(sources, key=str))
        for normalized, sources in sources_by_normalized.items()
        if len(sources) > 1
    ]
    if collisions:
        details = "; ".join(
            f"{', '.join(repr(source) for source in sources)} normalize to {normalized!r}"
            for normalized, sources in sorted(collisions)
        )
        raise ValueError(f"{context} identifiers {details}")
    return normalized_by_source


def _identifier_normalization_findings(
    values: Iterable[Any],
    *,
    context: str,
    path: str,
) -> list[str]:
    """Return a capability finding when source names collapse during normalization."""
    try:
        _normalized_identifiers(values, context=context)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    return []


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
    match _required_type(cwl_type):
        case "File" | "Directory":
            return "path"
        case "string" | "int" | "float" | "boolean":
            return "val"
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
                        glob=_output_template(raw_name, raw_definition) if outputs else None,
                    )
                )
            case _:
                raise ValueError(f"CWL port {raw_name!r} must declare a type")
    return ports


_INPUT_EXPRESSION = re.compile(
    r"\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)(?:\.(?:path|basename))?\s*\)"
)


def _template(value: Any, *, context: str) -> NfTemplate:
    match value:
        case bool() as boolean:
            text = "true" if boolean else "false"
        case (int() | float()) as number:
            text = str(number)
        case str() as text:
            pass
        case _:
            raise ValueError(f"unsupported CWL command value {value!r}")
    segments: list[NfLiteral | NfInputReference] = []
    offset = 0
    for match in _INPUT_EXPRESSION.finditer(text):
        if match.start() > offset:
            segments.append(NfLiteral(text[offset:match.start()]))
        segments.append(NfInputReference(_identifier(match.group(1), context="input reference")))
        offset = match.end()
    if offset < len(text):
        segments.append(NfLiteral(text[offset:]))
    residual = _INPUT_EXPRESSION.sub("", text)
    if "$" in residual:
        raise ValueError(
            f"{context} contains an unsupported CWL expression; Phase 1 supports only "
            "$(inputs.<name>), optionally followed by .path or .basename"
        )
    return NfTemplate(tuple(segments or [NfLiteral(text)]))


def _position(value: Any, *, default: int) -> int:
    match value:
        case None:
            return default
        case bool():
            raise ValueError(f"unsupported non-integer CWL command position {value!r}")
        case int() as position:
            return position
        case str() as position:
            try:
                return int(position)
            except ValueError as exc:
                raise ValueError(f"unsupported non-integer CWL command position {value!r}") from exc
        case _:
            raise ValueError(f"unsupported CWL command position {value!r}")


def _binding_tokens(prefix: Any, value: NfTemplate, *, separate: Any = True) -> tuple[NfTemplate, ...]:
    match prefix:
        case None:
            return (value,)
        case str() as text:
            prefix_template = _template(text, context="CWL command prefix")
            if separate is not False:
                return prefix_template, value
            return (NfTemplate((*prefix_template.segments, *value.segments)),)
        case _:
            raise ValueError("CWL command prefix must be a string")


def _argument_items(arguments: list[Any]) -> list[tuple[tuple[int, int, int], tuple[NfTemplate, ...]]]:
    items: list[tuple[tuple[int, int, int], tuple[NfTemplate, ...]]] = []
    for index, argument in enumerate(arguments):
        match argument:
            case {"valueFrom": value_from}:
                value = _template(value_from, context="CWL argument valueFrom")
                tokens = _binding_tokens(argument.get("prefix"), value, separate=argument.get("separate", True))
                item_position = _position(argument.get("position"), default=0)
            case Mapping():
                raise ValueError("mapped CWL arguments must contain valueFrom")
            case _:
                tokens = (_template(argument, context="CWL argument"),)
                item_position = 0
        items.append(((item_position, 0, index), tokens))
    return items


def _input_binding_items(
    inputs: Mapping[str, Any],
    *,
    start: int,
) -> list[tuple[tuple[int, int, str], tuple[NfTemplate, ...]]]:
    del start
    items: list[tuple[tuple[int, int, str], tuple[NfTemplate, ...]]] = []
    for raw_name, definition in inputs.items():
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
        value = (
            _template(value_from, context=f"CWL input {raw_name!r} valueFrom")
            if value_from is not None else NfTemplate((NfInputReference(name),))
        )
        tokens = _binding_tokens(binding.get("prefix"), value, separate=binding.get("separate", True))
        items.append(((_position(binding.get("position"), default=0), 1, str(raw_name)), tokens))
    return items


def _command_items(tool: Mapping[str, Any]) -> tuple[NfTemplate, ...]:
    arguments = _as_list(tool.get("arguments", []), error="CommandLineTool arguments must be a list")
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    ordered = sorted([*_argument_items(arguments), *_input_binding_items(inputs, start=0)])
    return tuple(token for _key, tokens in ordered for token in tokens)


def _command(tool: Mapping[str, Any]) -> NfCommand:
    base_command = tool.get("baseCommand")
    tokens: list[NfTemplate] = []
    match base_command:
        case str() as command if command:
            tokens.append(_template(command, context="CWL baseCommand"))
        case list() as command:
            for token in command:
                match token:
                    case str():
                        tokens.append(_template(token, context="CWL baseCommand"))
                    case _:
                        raise ValueError("CommandLineTool baseCommand must be a string or list of strings")
        case None:
            pass
        case _:
            raise ValueError("CommandLineTool baseCommand must be a string or list of strings")

    tokens.extend(_command_items(tool))
    if not tokens:
        tokens.append(_template("true", context="empty CWL command"))

    def stream(name: str) -> NfTemplate | None:
        return None if tool.get(name) is None else _template(tool[name], context=f"CWL {name}")

    return NfCommand(tuple(tokens), stream("stdin"), stream("stdout"), stream("stderr"))


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
    return resource.get(maximum, resource.get(minimum))


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


def _resources(tool: Mapping[str, Any]) -> NfResources:
    resource = _requirement(tool, "ResourceRequirement")
    if resource is None:
        return NfResources()
    cpus = _resource_value(resource, "coresMin", "coresMax")
    memory = _resource_value(resource, "ramMin", "ramMax")
    rendered_cpus = None if cpus is None else float(_render_resource(cpus, name="CPU"))
    rendered_memory = None if memory is None else float(_render_resource(memory, name="memory"))
    return NfResources(rendered_cpus, rendered_memory)


def _output_template(raw_name: Any, definition: Any) -> NfTemplate:
    output_definition = _as_mapping(definition, error=f"CWL output {raw_name!r} must be a mapping")
    match output_definition.get("outputBinding"):
        case None:
            raise ValueError(f"CWL output {raw_name!r} must define outputBinding.glob")
        case Mapping() as binding:
            pass
        case _:
            raise ValueError(f"CWL outputBinding for {raw_name!r} must be a mapping")
    match binding.get("glob"):
        case None:
            raise ValueError(f"CWL output {raw_name!r} must define outputBinding.glob")
        case str() as glob if glob:
            return _template(glob, context=f"CWL output glob for {raw_name!r}")
        case str():
            raise ValueError(f"CWL output glob for {raw_name!r} cannot be empty")
        case list():
            raise ValueError(
                f"CWL output glob lists for {raw_name!r} are deferred beyond Nextflow Phase 1"
            )
        case _:
            raise ValueError(f"CWL output glob for {raw_name!r} must be a string or list")


_SUPPORTED_REQUIREMENTS = frozenset({
    "DockerRequirement",
    "InlineJavascriptRequirement",
    "ResourceRequirement",
})
_DEFERRED_REQUIREMENTS = {
    "InitialWorkDirRequirement": "in-place staging is deferred to Phase 2",
    "ShellCommandRequirement": "shell-mode command lowering is deferred to Phase 2",
}

_INERT_DOCUMENTATION_FIELDS = frozenset({"doc", "label"})
_TOOL_CONSUMED_FIELDS = frozenset({
    "$namespaces",
    "$schemas",
    "arguments",
    "baseCommand",
    "class",
    "cwlVersion",
    "hints",
    "id",
    "inputs",
    "outputs",
    "requirements",
    "stderr",
    "stdin",
    "stdout",
}) | _INERT_DOCUMENTATION_FIELDS
_INPUT_CONSUMED_FIELDS = (
    frozenset({"default", "inputBinding", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_INPUT_BINDING_CONSUMED_FIELDS = frozenset({
    "position",
    "prefix",
    "separate",
    "shellQuote",
    "valueFrom",
})
_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputBinding", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_OUTPUT_BINDING_CONSUMED_FIELDS = frozenset({"glob"})
_OUTPUT_BINDING_DEFERRED_FIELDS = frozenset({"loadContents", "outputEval"})
_ARGUMENT_CONSUMED_FIELDS = frozenset({
    "position",
    "prefix",
    "separate",
    "shellQuote",
    "valueFrom",
})
_SUPPORTED_REQUIREMENT_FIELDS = {
    "DockerRequirement": frozenset({"class", "dockerImageId", "dockerPull"}),
    "InlineJavascriptRequirement": frozenset({"class"}),
    "ResourceRequirement": frozenset({
        "class",
        "coresMax",
        "coresMin",
        "ramMax",
        "ramMin",
    }),
}
_WORKFLOW_CONSUMED_FIELDS = frozenset({
    "$namespaces",
    "$schemas",
    "class",
    "cwlVersion",
    "id",
    "inputs",
    "outputs",
    "steps",
}) | _INERT_DOCUMENTATION_FIELDS
_WORKFLOW_INPUT_CONSUMED_FIELDS = (
    frozenset({"default", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_WORKFLOW_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputSource", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_STEP_CONSUMED_FIELDS = frozenset({
    "id",
    "in",
    "out",
    "run",
    "scatter",
    "scatterMethod",
    "when",
}) | _INERT_DOCUMENTATION_FIELDS
_STEP_INPUT_CONSUMED_FIELDS = frozenset({"source"})


def _unconsumed_field_findings(
    value: Mapping[str, Any],
    *,
    consumed: frozenset[str],
    path: str,
) -> list[str]:
    """Reject source fields that the Phase 1 lowering does not consume."""
    return [
        f"{path}.{field_name}: {field_name} is not consumed by Nextflow Phase 1 lowering"
        for field_name in sorted(set(value) - consumed)
    ]


def _phase1_value_matches(cwl_type: Any, value: Any) -> bool:
    """Return whether a concrete boundary value has the supported runtime shape."""
    match _required_type(cwl_type), value:
        case "string", str():
            return True
        case "boolean", bool():
            return True
        case "int", int() if not isinstance(value, bool):
            return True
        case "float", (int() | float()) if not isinstance(value, bool):
            return True
        case ("File" | "Directory") as expected, (str() | PathLike()):
            return True
        case ("File" | "Directory") as expected, Mapping() as mapping:
            return (
                set(mapping) <= {"class", "path"}
                and mapping.get("class", expected) == expected
                and isinstance(mapping.get("path"), (str, PathLike))
            )
        case _:
            return False


def _requirement_definition(
    section: Any,
    *,
    class_name: str,
    suffix: str,
) -> Mapping[str, Any] | None:
    """Return a requirement payload for closed-world field analysis."""
    match section:
        case Mapping() as requirements:
            definition = requirements.get(class_name)
        case list() as requirements:
            definition = requirements[int(suffix)]
        case _:
            return None
    return definition if isinstance(definition, Mapping) else None


def _requirement_names(section: Any) -> list[tuple[str, str]]:
    """Return requirement names and stable path suffixes for capability analysis."""
    match section:
        case None:
            return []
        case Mapping() as requirements:
            return [(str(name), str(name)) for name in requirements]
        case list() as requirements:
            names: list[tuple[str, str]] = []
            for index, requirement in enumerate(requirements):
                match requirement:
                    case Mapping() if isinstance(requirement.get("class"), str):
                        names.append((requirement["class"], str(index)))
                    case _:
                        continue
            return names
        case _:
            return []


def _tool_capability_findings(
    step: Mapping[str, Any],
    child: RoseTree,
    *,
    step_index: int,
) -> list[str]:
    """Collect unsupported executable semantics without lowering the tool."""
    path = f"steps[{step_index}]"
    findings: list[str] = []
    if "when" in step:
        findings.append(f"{path}.when: CWL step when conditions are not supported in Nextflow Phase 1")
    if "scatter" in step:
        findings.append(f"{path}.scatter: executable scatter is deferred to Phase 2")

    match child:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            return findings
    if sub_trees:
        findings.append(f"{path}.run: nested workflows are deferred to Phase 2")
    match node_data.compiled_cwl:
        case Mapping() as tool:
            pass
        case _:
            return findings
    match tool.get("class"):
        case "CommandLineTool":
            pass
        case "Workflow":
            if not sub_trees:
                findings.append(f"{path}.run: nested workflows are deferred to Phase 2")
            return findings
        case unsupported_class:
            findings.append(
                f"{path}.run.class: unsupported compiled step class {unsupported_class!r}"
            )
            return findings

    findings.extend(
        _unconsumed_field_findings(
            tool,
            consumed=_TOOL_CONSUMED_FIELDS,
            path=f"{path}.run",
        )
    )

    for section_name in ("requirements", "hints"):
        section = tool.get(section_name)
        for class_name, suffix in _requirement_names(section):
            requirement_path = f"{path}.run.{section_name}.{suffix}"
            if class_name in _DEFERRED_REQUIREMENTS:
                findings.append(f"{requirement_path}: {_DEFERRED_REQUIREMENTS[class_name]}")
            elif class_name not in _SUPPORTED_REQUIREMENTS:
                findings.append(
                    f"{requirement_path}: {class_name} is not supported by Nextflow Phase 1"
                )
            elif definition := _requirement_definition(
                section,
                class_name=class_name,
                suffix=suffix,
            ):
                findings.extend(
                    _unconsumed_field_findings(
                        definition,
                        consumed=_SUPPORTED_REQUIREMENT_FIELDS[class_name],
                        path=requirement_path,
                    )
                )
    match tool.get("inputs", {}):
        case Mapping() as inputs:
            findings.extend(
                _identifier_normalization_findings(
                    inputs,
                    context="tool input",
                    path=f"{path}.run.inputs",
                )
            )
            for raw_name, raw_definition in inputs.items():
                if not isinstance(raw_definition, Mapping):
                    continue
                input_path = f"{path}.run.inputs.{raw_name}"
                findings.extend(
                    _unconsumed_field_findings(
                        raw_definition,
                        consumed=_INPUT_CONSUMED_FIELDS,
                        path=input_path,
                    )
                )
                if "default" in raw_definition and (
                    raw_definition["default"] is None
                    or isinstance(raw_definition["default"], (Mapping, list))
                ):
                    findings.append(
                        f"{input_path}.default: Phase 1 supports JSON scalar defaults only"
                    )
                elif "default" in raw_definition and not _phase1_value_matches(
                    raw_definition.get("type"), raw_definition["default"]
                ):
                    findings.append(
                        f"{input_path}.default: value does not match its supported CWL type"
                    )
                binding = raw_definition.get("inputBinding")
                if not isinstance(binding, Mapping):
                    continue
                input_binding_path = f"{input_path}.inputBinding"
                findings.extend(
                    _unconsumed_field_findings(
                        binding,
                        consumed=_INPUT_BINDING_CONSUMED_FIELDS,
                        path=input_binding_path,
                    )
                )
                if binding.get("shellQuote") is False:
                    findings.append(
                        f"{input_binding_path}.shellQuote: shellQuote false is deferred to Phase 2"
                    )
        case _:
            pass

    match tool.get("arguments", []):
        case list() as arguments:
            for argument_index, argument in enumerate(arguments):
                if not isinstance(argument, Mapping):
                    continue
                argument_path = f"{path}.run.arguments[{argument_index}]"
                findings.extend(
                    _unconsumed_field_findings(
                        argument,
                        consumed=_ARGUMENT_CONSUMED_FIELDS,
                        path=argument_path,
                    )
                )
                if argument.get("shellQuote") is False:
                    findings.append(
                        f"{argument_path}.shellQuote: "
                        "shellQuote false is deferred to Phase 2"
                    )
        case _:
            pass

    match tool.get("outputs", {}):
        case Mapping() as outputs:
            findings.extend(
                _identifier_normalization_findings(
                    outputs,
                    context="tool output",
                    path=f"{path}.run.outputs",
                )
            )
            for raw_name, raw_definition in outputs.items():
                if not isinstance(raw_definition, Mapping):
                    continue
                output_path = f"{path}.run.outputs.{raw_name}"
                findings.extend(
                    _unconsumed_field_findings(
                        raw_definition,
                        consumed=_OUTPUT_CONSUMED_FIELDS,
                        path=output_path,
                    )
                )
                try:
                    qualifier = cwl_type_to_nf_qualifier(raw_definition.get("type"))
                except ValueError:
                    qualifier = "unsupported"
                if qualifier != "path":
                    findings.append(
                        f"{output_path}.type: primitive and non-path output capture is deferred to Phase 2"
                    )
                binding = raw_definition.get("outputBinding")
                if not isinstance(binding, Mapping):
                    continue
                output_binding_path = f"{output_path}.outputBinding"
                findings.extend(
                    _unconsumed_field_findings(
                        binding,
                        consumed=(
                            _OUTPUT_BINDING_CONSUMED_FIELDS
                            | _OUTPUT_BINDING_DEFERRED_FIELDS
                        ),
                        path=output_binding_path,
                    )
                )
                for field_name in ("loadContents", "outputEval"):
                    if field_name in binding:
                        findings.append(
                            f"{output_binding_path}.{field_name}: "
                            f"{field_name} output capture is deferred to Phase 2"
                        )
        case _:
            pass
    return findings


def _workflow_capability_findings(
    workflow: Mapping[str, Any],
    node_data: NodeData,
    steps: list[Mapping[str, Any]],
) -> list[str]:
    """Apply closed-world analysis to workflow, step, and boundary values."""
    findings = _unconsumed_field_findings(
        workflow,
        consumed=_WORKFLOW_CONSUMED_FIELDS,
        path="workflow",
    )
    provided = _as_mapping(
        node_data.workflow_inputs_file,
        error="compiled workflow input values must be a mapping",
    )
    match workflow.get("inputs", {}):
        case Mapping() as inputs:
            findings.extend(
                _identifier_normalization_findings(
                    inputs,
                    context="workflow input",
                    path="workflow.inputs",
                )
            )
            declared_names = {str(name) for name in inputs}
            for raw_name, definition in inputs.items():
                input_path = f"workflow.inputs.{raw_name}"
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition,
                            consumed=_WORKFLOW_INPUT_CONSUMED_FIELDS,
                            path=input_path,
                        )
                    )
                    has_default = "default" in definition
                    default = definition.get("default")
                    cwl_type = definition.get("type")
                    if has_default and (
                        default is None or isinstance(default, (Mapping, list))
                    ):
                        findings.append(
                            f"{input_path}.default: Phase 1 supports JSON scalar defaults only"
                        )
                    elif has_default and not _phase1_value_matches(cwl_type, default):
                        findings.append(
                            f"{input_path}.default: value does not match its supported CWL type"
                        )
                else:
                    has_default = False
                    cwl_type = definition
                if raw_name not in provided and not has_default and not _is_optional(cwl_type):
                    findings.append(f"{input_path}: required workflow input value is missing")
                if raw_name in provided and provided[raw_name] is None:
                    findings.append(
                        f"{input_path}: explicit null input values are deferred to Phase 2"
                    )
                elif raw_name in provided and not _phase1_value_matches(
                    cwl_type, provided[raw_name]
                ):
                    findings.append(
                        f"{input_path}: supplied value does not match its supported CWL type"
                    )
            for extra_name in sorted(set(map(str, provided)) - declared_names):
                findings.append(
                    f"workflow.input_values.{extra_name}: value has no declared workflow input"
                )
        case _:
            pass

    match workflow.get("outputs", {}):
        case Mapping() as outputs:
            findings.extend(
                _identifier_normalization_findings(
                    outputs,
                    context="workflow output",
                    path="workflow.outputs",
                )
            )
            for raw_name, definition in outputs.items():
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition,
                            consumed=_WORKFLOW_OUTPUT_CONSUMED_FIELDS,
                            path=f"workflow.outputs.{raw_name}",
                        )
                    )
        case _:
            pass

    for step_index, step in enumerate(steps):
        step_path = f"steps[{step_index}]"
        findings.extend(
            _unconsumed_field_findings(
                step,
                consumed=_STEP_CONSUMED_FIELDS,
                path=step_path,
            )
        )
        if "scatterMethod" in step and "scatter" not in step:
            findings.append(
                f"{step_path}.scatterMethod: scatterMethod without scatter is not executable"
            )
        match step.get("in", {}):
            case Mapping() as step_inputs:
                for raw_name, definition in step_inputs.items():
                    if isinstance(definition, Mapping):
                        findings.extend(
                            _unconsumed_field_findings(
                                definition,
                                consumed=_STEP_INPUT_CONSUMED_FIELDS,
                                path=f"{step_path}.in.{raw_name}",
                            )
                        )
            case _:
                pass
    return findings


def _absent_optional_findings(
    workflow: Mapping[str, Any],
    node_data: NodeData,
    steps: list[Mapping[str, Any]],
    sub_trees: list[Any],
) -> list[str]:
    """Reject optional inputs whose compiled workflow value is absent."""
    workflow_inputs = _as_mapping(
        workflow.get("inputs", {}),
        error="compiled CWL Workflow inputs must be a mapping",
    )
    if _identifier_normalization_findings(
        workflow_inputs,
        context="workflow input",
        path="workflow.inputs",
    ):
        # The capability pass already owns this diagnostic.  There is no
        # unambiguous parameter lookup for the optional-input analysis.
        return []
    params = _workflow_params(workflow, node_data)
    findings: list[str] = []
    for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True)):
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                pass
            case _:
                continue
        tool_inputs = tool.get("inputs", {})
        step_inputs = step.get("in", {})
        if not isinstance(tool_inputs, Mapping) or not isinstance(step_inputs, Mapping):
            continue
        for raw_name, raw_definition in tool_inputs.items():
            if not isinstance(raw_definition, Mapping) or not _is_optional(raw_definition.get("type")):
                continue
            raw_source = step_inputs.get(raw_name)
            if raw_source is None:
                absent = "default" not in raw_definition
            else:
                try:
                    sources = _source_values(
                        raw_source,
                        context=f"step input {step_index}.{raw_name}",
                    )
                except ValueError:
                    continue
                boundary_sources = [source for source in sources if "/" not in source]
                absent = any(
                    params.get(_identifier(source, context="workflow input source")) is None
                    for source in boundary_sources
                )
            if absent:
                findings.append(
                    f"steps[{step_index}].run.inputs.{raw_name}: absent optional values are "
                    "deferred to Phase 2"
                )
    return findings


def _raise_capability_findings(findings: list[str]) -> None:
    if findings:
        details = "\n".join(f"- {finding}" for finding in findings)
        raise ValueError(f"Nextflow Phase 1 capability analysis failed:\n{details}")


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
    return NfProcess(
        name=_identifier(step.get("id"), context="workflow step id"),
        inputs=_ports(tool.get("inputs", {}), outputs=False),
        outputs=_ports(tool.get("outputs", {}), outputs=True),
        command=_command(tool),
        container=_container(tool),
        resources=_resources(tool),
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
                if source_process is None:
                    connections.append(
                        NfWorkflowInputConnection(source_port, process.name, destination_port)
                    )
                else:
                    connections.append(
                        NfProcessConnection(
                            source_process,
                            source_port,
                            process.name,
                            destination_port,
                        )
                    )
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
                    if source_process is None:
                        raise ValueError(
                            f"workflow output {destination_port!r} directly forwards a workflow input; "
                            "boundary passthrough is not executable in Nextflow Phase 1"
                        )
                    connections.append(
                        NfWorkflowOutputConnection(source_process, source_port, destination_port)
                    )
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


def _default_bindings(
    steps: list[Mapping[str, Any]],
    sub_trees: list[Any],
    processes: list[NfProcess],
) -> tuple[list[NfWorkflowInputConnection], dict[str, Any]]:
    connections: list[NfWorkflowInputConnection] = []
    params: dict[str, Any] = {}
    for step, child, process in zip(steps, sub_trees, processes, strict=True):
        bound = _as_mapping(step.get("in", {}), error="compiled step inputs must be a mapping")
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                inputs = _as_mapping(tool.get("inputs", {}), error="tool inputs must be a mapping")
            case _:
                continue
        for raw_name, definition in inputs.items():
            if raw_name in bound or not isinstance(definition, Mapping) or "default" not in definition:
                continue
            port_name = _identifier(raw_name, context="defaulted process input")
            param_name = _identifier(f"{process.name}___{port_name}", context="default parameter")
            connections.append(NfWorkflowInputConnection(param_name, process.name, port_name))
            params[param_name] = _json_value(definition["default"])
    return connections, params


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
    params: dict[str, Any] = {}
    normalized_names = _normalized_identifiers(workflow_inputs, context="workflow input")
    missing = object()
    for name, definition in workflow_inputs.items():
        value = provided_params.get(name, missing)
        if value is missing and isinstance(definition, Mapping) and "default" in definition:
            value = definition["default"]
        elif value is missing:
            value = None
        params[normalized_names[name]] = _json_value(value)
    return params


def _container_policy_findings(sub_trees: list[Any]) -> list[str]:
    """Require one workflow-wide host or container execution policy."""
    containerized: list[int] = []
    host: list[int] = []
    for step_index, child in enumerate(sub_trees):
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                requirement_names = [
                    class_name
                    for section_name in ("requirements", "hints")
                    for class_name, _suffix in _requirement_names(tool.get(section_name))
                ]
                target = containerized if "DockerRequirement" in requirement_names else host
                target.append(step_index)
            case _:
                continue
    if not containerized or not host:
        return []
    return [
        "workflow.steps: mixed container execution is not supported; "
        "DockerRequirement must be declared by every process or by none "
        f"(containerized steps: {containerized}; host steps: {host})"
    ]


def cwl_rosetree_to_nextflow(rose_tree: RoseTree) -> ExecutableNextflowWorkflow:
    """Convert a compiled flat CWL RoseTree without invoking inference again."""
    node_data, sub_trees, workflow = _compiled_workflow(rose_tree)
    steps = _workflow_steps(workflow, child_count=len(sub_trees))
    findings = [
        finding
        for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True))
        for finding in _tool_capability_findings(step, child, step_index=step_index)
    ]
    findings.extend(_workflow_capability_findings(workflow, node_data, steps))
    findings.extend(_absent_optional_findings(workflow, node_data, steps, sub_trees))
    findings.extend(_container_policy_findings(sub_trees))
    _raise_capability_findings(findings)
    processes = [
        _process(step, child)
        for step, child in zip(steps, sub_trees, strict=True)
    ]
    default_connections, default_params = _default_bindings(steps, sub_trees, processes)
    params = _workflow_params(workflow, node_data)
    if collisions := sorted(set(params) & set(default_params)):
        raise ValueError(
            "lowered workflow parameter names collide: "
            f"{', '.join(collisions)}"
        )
    params.update(default_params)
    return ExecutableNextflowWorkflow(
        name=_identifier(node_data.name, context="workflow name"),
        processes=processes,
        connections=[*_connections(workflow, list(steps), processes), *default_connections],
        params=params,
    )
