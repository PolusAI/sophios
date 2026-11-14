"""Job inputs as a runner gets them."""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from pathlib import PurePath
import re
from typing import Any

from cwl_utils.parser.cwl_v1_2 import Dirent, InitialWorkDirRequirement

from sophios.edam import resolve_file_format
from sophios.ir.resolved import InputRef, ResolvedWorkflow, Through, tool_steps
from sophios.lang.cwl import ports_named, required, requirement
from sophios.wic_types import Json, Tool


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
        if not isinstance(run, Tool):
            continue
        targets = _output_target_inputs(run)
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


def _output_target_inputs(run: Tool) -> set[str]:
    process, _ = run.loaded
    inputs = ports_named(process.inputs)
    writable_inputs = _writable_initial_workdir_inputs(run)
    target_inputs: set[str] = set()
    for output in process.outputs:
        if _is_directory(output.type_) and (binding := getattr(output, "outputBinding", None)) is not None:
            for input_name in _referenced_inputs(binding.glob):
                if input_name in writable_inputs and input_name in inputs and _is_directory(inputs[input_name].type_):
                    target_inputs.add(input_name)
    return target_inputs


def _writable_initial_workdir_inputs(run: Tool) -> set[str]:
    """The inputs a tool stages writable. Only for a tool that writes the requirement in a mapping: one that lists it,
    as `tool_builder` does, has never been read here, and `examples/scripts/ichnaea_integrated.py` writes in place
    through an absolute path because of it."""
    if not any(isinstance(section, Mapping) and "InitialWorkDirRequirement" in section
               for section in (run.cwl.get("requirements"), run.cwl.get("hints"))):
        return set()
    listing = getattr(requirement(run.loaded[0], InitialWorkDirRequirement), "listing", None)
    return {name for entry in listing if isinstance(entry, Dirent) and entry.writable is True
            for name in _referenced_inputs(entry.entry)} if isinstance(listing, list) else set()


def _is_directory(cwl_type: Any) -> bool:
    inner, _ = required(cwl_type)
    return inner == "Directory" or isinstance(inner, list) and "Directory" in inner


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
