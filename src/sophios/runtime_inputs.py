"""Job inputs as a runner gets them."""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from pathlib import PurePath
import re
from typing import Any

from sophios.edam import resolve_file_format
from sophios.ir.resolved import InputRef, ResolvedTool, ResolvedWorkflow, Through, tool_steps
from sophios.wic_types import Json


_INPUT_REFERENCE = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_-]*)")


def runner_job_inputs(job_inputs: Mapping[str, Any], output_targets: Collection[str]) -> Json:
    """`job_inputs` as a runner gets them: each File's format resolved, and each of `output_targets` (the root
    job keys `output_target_inputs` names) a Directory literal the runner creates, empty, under the name it gave."""
    job = resolve_file_formats(job_inputs)
    for name in output_targets:
        if name in job:
            job[name] = _output_target_directory(job[name], name)
    return job


def output_target_inputs(view: ResolvedWorkflow) -> set[str]:
    """The root inputs of `view` that name an output-target Directory, at any depth of calls.

    A tool's input is one when the tool writes into it through a writable InitialWorkDirRequirement entry and
    captures it as a Directory output. A root input is named when a step's such input takes it as a plain value
    through every call; a `Crossing` (a merge, pick, default or computed value) is not a plain root value, so it
    is left alone.
    """
    names: set[str] = set()
    for found in tool_steps(view):
        run = found.step.run
        if not isinstance(run, ResolvedTool) or not isinstance(run.document, Mapping):
            continue
        targets = _output_target_inputs(run.document)
        for port, sources in zip(found.step.inputs, found.sources, strict=True):
            if port.name in targets:
                names.update(source.ref.name for source in sources
                             if isinstance(source, Through) and isinstance(source.ref, InputRef))
    return names


def resolve_file_formats(job_inputs: Mapping[str, Any]) -> Json:
    """A copy of `job_inputs` in which each File's list of possible formats is the one its file has, or none:
    what every runner needs, local or not."""
    resolved = copy.deepcopy(dict(job_inputs))
    _normalize_file_formats(resolved)
    return resolved


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
                    definition = inputs.get(input_name)
                    # `outDir: Directory` is a type, not a mapping that has one.
                    declared = definition.get("type") if isinstance(definition, Mapping) else definition
                    if input_name in writable_inputs and _type_includes(declared, "Directory"):
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


def _output_target_directory(value: Any, source_key: str) -> Any:
    """The literal for a name, or for a Directory read as one: its `location`, `path` or `basename`."""
    match value:
        case str() as name:
            pass
        case {"class": "Directory", "location": str() as name}:
            pass
        case {"class": "Directory", "path": str() as name}:
            pass
        case {"class": "Directory", "basename": str() as name}:
            pass
        case _:
            return value
    return {"class": "Directory", "basename": _output_target_basename(name, source_key), "listing": []}


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


def _as_list(value: Any) -> list[Any]:
    match value:
        case list() as items:
            return items
        case _:
            return []
