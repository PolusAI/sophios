"""Normalize generated CWL and job inputs for local runner execution."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import PurePath
import re
from typing import Any

from sophios.edam import resolve_file_format
from sophios.wic_types import Json, NodeData, RoseTree


_INPUT_REFERENCE = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_-]*)")


def normalize_job_inputs(cwl_workflow: Mapping[str, Any], job_inputs: Mapping[str, Any]) -> Json:
    """Return job inputs with output-target directories as runner-local names."""
    return _normalize_job_inputs(cwl_workflow, {}, job_inputs)


def normalize_rose_tree_job_inputs(rose_tree: RoseTree, job_inputs: Mapping[str, Any]) -> Json:
    """Return normalized job inputs using a compiled rose tree's step CLTs."""
    run_by_step_id = _run_by_step_id(rose_tree)
    node_data: NodeData = rose_tree.data
    return _normalize_job_inputs(node_data.compiled_cwl, run_by_step_id, job_inputs)


def normalize_cwl_document(cwl_document: Mapping[str, Any]) -> Json:
    """Return generated CWL with output-target Directory inputs converted to strings."""
    return _normalize_cwl_document(cwl_document, {})


def normalize_rose_tree_cwl(rose_tree: RoseTree) -> Json:
    """Return generated CWL normalized using a compiled rose tree's step CLTs."""
    node_data: NodeData = rose_tree.data
    return _normalize_cwl_document(node_data.compiled_cwl, _run_by_step_id(rose_tree))


def _run_by_step_id(rose_tree: RoseTree) -> dict[str, Mapping[str, Any]]:
    run_by_step_id: dict[str, Mapping[str, Any]] = {}
    for sub_tree in rose_tree.sub_trees:
        sub_node_data: NodeData = sub_tree.data
        if sub_node_data.namespaces:
            run_by_step_id[sub_node_data.namespaces[-1]] = sub_node_data.compiled_cwl
    return run_by_step_id


def _normalize_job_inputs(
    cwl_workflow: Mapping[str, Any],
    run_by_step_id: Mapping[str, Mapping[str, Any]],
    job_inputs: Mapping[str, Any],
) -> Json:
    normalized = copy.deepcopy(dict(job_inputs))
    _normalize_file_formats(normalized)
    for source_key in _output_target_source_keys(cwl_workflow, run_by_step_id):
        if source_key in normalized:
            normalized[source_key] = _normalize_output_target_name(normalized[source_key], source_key)
    return normalized


def _normalize_file_formats(value: Any) -> None:
    match value:
        case {"class": "File", "format": list() as formats} as file_input:
            match resolve_file_format(file_input, formats):
                case str() as concrete_format:
                    file_input["format"] = concrete_format
                case _:
                    file_input.pop("format", None)
        case Mapping() as mapping:
            for item in mapping.values():
                _normalize_file_formats(item)
        case list() as items:
            for item in items:
                _normalize_file_formats(item)


def _normalize_cwl_document(
    cwl_document: Mapping[str, Any],
    run_by_step_id: Mapping[str, Mapping[str, Any]],
) -> Json:
    normalized = copy.deepcopy(dict(cwl_document))
    match normalized.get("class"):
        case "CommandLineTool":
            _normalize_command_line_tool(normalized, _output_target_inputs(normalized))
        case "Workflow":
            _normalize_workflow(normalized, run_by_step_id)
    return normalized


def _normalize_workflow(
    cwl_workflow: Json,
    run_by_step_id: Mapping[str, Mapping[str, Any]],
) -> None:
    for step in _as_list(cwl_workflow.get("steps")):
        match step:
            case dict() as step_dict:
                match step_dict.get("run"):
                    case Mapping() as run:
                        target_inputs = _output_target_inputs(run)
                        if target_inputs:
                            step_dict["run"] = _normalize_command_line_tool(copy.deepcopy(dict(run)), target_inputs)
                    case _:
                        target_inputs = _output_target_inputs(run_by_step_id.get(str(step_dict.get("id", "")), {}))
                for source_key in _target_source_keys_for_step(step_dict, target_inputs):
                    _set_workflow_input_type(cwl_workflow, source_key, "string")


def _target_source_keys_for_step(step: Mapping[str, Any], target_inputs: set[str]) -> set[str]:
    source_keys: set[str] = set()
    for input_name, sources in _step_input_sources(step.get("in")).items():
        if input_name in target_inputs:
            source_keys.update(source for source in sources if "/" not in source)
    return source_keys


def _normalize_command_line_tool(cwl_tool: Json, target_inputs: set[str]) -> Json:
    if not target_inputs:
        return cwl_tool
    for input_name in target_inputs:
        _set_parameter_type(cwl_tool.get("inputs"), input_name, "string")
    _rewrite_initial_workdir(cwl_tool, target_inputs)
    _rewrite_input_basename_refs(cwl_tool, target_inputs)
    return cwl_tool


def _rewrite_initial_workdir(cwl_tool: Json, target_inputs: set[str]) -> None:
    for requirement in (cwl_tool.get("requirements"), cwl_tool.get("hints")):
        match requirement:
            case {"InitialWorkDirRequirement": {"listing": list() as listing}}:
                for index, entry in enumerate(listing):
                    match entry:
                        case {"writable": True, "entry": entry_value}:
                            referenced = _referenced_inputs(entry_value) & target_inputs
                            if len(referenced) == 1:
                                input_name = next(iter(referenced))
                                listing[index] = {
                                    "entry": _directory_expression(input_name),
                                    "writable": True,
                                }


def _directory_expression(input_name: str) -> str:
    return (
        "${\n"
        f"  return {{\"class\": \"Directory\", \"basename\": inputs[{input_name!r}], \"listing\": []}};\n"
        "}"
    )


def _rewrite_input_basename_refs(value: Any, target_inputs: set[str]) -> Any:
    match value:
        case dict() as mapping:
            for key, item in mapping.items():
                mapping[key] = _rewrite_input_basename_refs(item, target_inputs)
        case list() as items:
            for index, item in enumerate(items):
                items[index] = _rewrite_input_basename_refs(item, target_inputs)
        case str() as text:
            for input_name in target_inputs:
                text = text.replace(f"inputs.{input_name}.basename", f"inputs.{input_name}")
                text = text.replace(f"inputs[{input_name!r}].basename", f"inputs[{input_name!r}]")
                text = text.replace(f'inputs["{input_name}"].basename', f'inputs["{input_name}"]')
            return text
    return value


def _set_workflow_input_type(cwl_workflow: Json, source_key: str, cwl_type: Any) -> None:
    _set_parameter_type(cwl_workflow.get("inputs"), source_key, cwl_type)


def _set_parameter_type(parameters: Any, parameter_name: str, cwl_type: Any) -> None:
    match parameters:
        case dict() as parameter_map:
            match parameter_map.get(parameter_name):
                case dict() as parameter:
                    parameter["type"] = cwl_type
        case list() as parameter_list:
            for parameter in parameter_list:
                match parameter:
                    case {"id": parameter_id} if _local_id(parameter_id) == parameter_name:
                        parameter["type"] = cwl_type


def _output_target_source_keys(
    cwl_workflow: Mapping[str, Any],
    run_by_step_id: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    source_keys: set[str] = set()
    for step in _as_list(cwl_workflow.get("steps")):
        match step:
            case Mapping() as step_mapping:
                match step_mapping.get("run"):
                    case Mapping() as run:
                        target_inputs = _output_target_inputs(run)
                    case _:
                        target_inputs = _output_target_inputs(run_by_step_id.get(str(step_mapping.get("id", "")), {}))
                if target_inputs:
                    for input_name, source in _step_input_sources(step_mapping.get("in")).items():
                        if input_name in target_inputs:
                            source_keys.update(source_key for source_key in source if "/" not in source_key)
    return source_keys


def _output_target_inputs(run: Mapping[str, Any]) -> set[str]:
    inputs = _parameter_mapping(run.get("inputs"))
    outputs = _parameter_mapping(run.get("outputs"))
    writable_inputs = _writable_initial_workdir_inputs(run)
    if not inputs or not outputs or not writable_inputs:
        return set()

    target_inputs: set[str] = set()
    for output in outputs.values():
        match output:
            case Mapping() as output_mapping if _type_includes(output_mapping.get("type"), "Directory"):
                glob_value = _output_glob(output_mapping)
                for input_name in _referenced_inputs(glob_value):
                    match inputs.get(input_name):
                        case Mapping() as input_definition if (
                            input_name in writable_inputs
                            and _type_includes(input_definition.get("type"), "Directory")
                        ):
                            target_inputs.add(input_name)
    return target_inputs


def _parameter_mapping(parameters: Any) -> dict[str, Any]:
    match parameters:
        case Mapping() as parameter_map:
            return dict(parameter_map)
        case list() as parameter_list:
            result = {}
            for parameter in parameter_list:
                match parameter:
                    case {"id": parameter_id}:
                        result[_local_id(parameter_id)] = parameter
            return result
        case _:
            return {}


def _writable_initial_workdir_inputs(run: Mapping[str, Any]) -> set[str]:
    inputs: set[str] = set()
    for requirement in (run.get("requirements"), run.get("hints")):
        match requirement:
            case {"InitialWorkDirRequirement": {"listing": listing}}:
                for entry in _as_list(listing):
                    match entry:
                        case {"writable": True, "entry": entry_value}:
                            inputs.update(_referenced_inputs(entry_value))
    return inputs


def _output_glob(output: Mapping[str, Any]) -> Any:
    match output.get("outputBinding"):
        case Mapping() as output_binding:
            return output_binding.get("glob")
        case _:
            return None


def _step_input_sources(step_inputs: Any) -> dict[str, list[str]]:
    match step_inputs:
        case list() as step_input_list:
            bindings = {}
            for item in step_input_list:
                match item:
                    case {"id": input_id}:
                        bindings[input_id] = item
        case Mapping() as bindings:
            pass
        case _:
            return {}

    sources: dict[str, list[str]] = {}
    for input_name, binding in bindings.items():
        match binding:
            case str() as source:
                sources[str(input_name)] = [source]
            case {"source": str() as source}:
                sources[str(input_name)] = [source]
            case {"source": list() as source_list}:
                sources[str(input_name)] = _strings(source_list)
    return sources


def _normalize_output_target_name(value: Any, source_key: str) -> Any:
    match value:
        case str() as location:
            return _output_target_basename(location, source_key)
        case {"class": "Directory", "location": {"wic_inline_input": str() as location}}:
            return _output_target_basename(location, source_key)
        case {"class": "Directory", "location": str() as location}:
            return _output_target_basename(location, source_key)
        case {"class": "Directory", "location": _}:
            return value
        case {"class": "Directory", "basename": str() as location}:
            return _output_target_basename(location, source_key)
        case _:
            return value


def _output_target_basename(location: str, source_key: str) -> str:
    if "://" in location or PurePath(location).is_absolute():
        raise ValueError(
            f"output-target Directory input {source_key!r} cannot use an absolute location; "
            "use the runner outdir to control final output placement"
        )
    basename = PurePath(location).name
    if not basename or basename != location:
        raise ValueError(
            f"output-target Directory input {source_key!r} must be a simple directory name, got {location!r}"
        )
    return basename


def _referenced_inputs(value: Any) -> set[str]:
    match value:
        case str() as text:
            return set(_INPUT_REFERENCE.findall(text))
        case list() as items:
            return set().union(*(_referenced_inputs(item) for item in items))
        case Mapping() as mapping:
            return set().union(*(_referenced_inputs(item) for item in mapping.values()))
        case _:
            return set()


def _type_includes(cwl_type: Any, type_name: str) -> bool:
    match cwl_type:
        case str() as type_value:
            return type_value.rstrip("?") == type_name
        case list() as type_list:
            return any(_type_includes(item, type_name) for item in type_list)
        case Mapping() as type_mapping:
            return bool(type_mapping.get("type") == type_name)
        case _:
            return False


def _local_id(value: Any) -> str:
    return str(value).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _strings(values: list[Any]) -> list[str]:
    strings = []
    for value in values:
        match value:
            case str() as text:
                strings.append(text)
    return strings


def _as_list(value: Any) -> list[Any]:
    match value:
        case list() as items:
            return items
        case _:
            return []
