"""Job inputs as a runner gets them."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any, TypeAlias

from ..edam import resolve_file_format
from ..ir.resolved import InputRef, ResolvedWorkflow, Through, tool_steps
from ..lang.cwl import inputs_read
from ..lang.tools import Dirent, NeedKind, Staging, ToolBody, ToolNode
from ..lang.types import required, spell
from ..wic_types import Json


#: An empty directory of a given name, as a runner takes one in a job.
EmptyDirectory: TypeAlias = Callable[[str], Json]


def directory_literal(name: str) -> Json:
    """An empty directory named `name` as CWL writes one: a Directory literal the runner creates."""
    return {"class": "Directory", "basename": name, "listing": []}


def runner_job_inputs(job_inputs: Mapping[str, Any], output_targets: Collection[str],
                      empty_directory: EmptyDirectory = directory_literal) -> Json:
    """`job_inputs` as a runner gets them: each File's format resolved, and each of `output_targets` (the root
    job keys `output_target_inputs` names) an empty directory the runner creates under the name it gave."""
    return name_output_targets(resolve_file_formats(job_inputs), output_targets, empty_directory)


def name_output_targets(job_inputs: Mapping[str, Any], output_targets: Collection[str],
                        empty_directory: EmptyDirectory = directory_literal) -> Json:
    """A copy of `job_inputs` in which each of `output_targets` is an empty directory the runner creates under the
    name it gave, as `empty_directory` writes one: a runner given an absolute path for a Directory would never
    create it."""
    job = dict(job_inputs)
    for name in output_targets:
        if name in job:
            job[name] = _output_target_directory(job[name], name, empty_directory)
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
        if not isinstance(run, ToolNode):
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


def _output_target_inputs(run: ToolNode) -> set[str]:
    body = run.body()
    inputs = {each.name: each for each in body.inputs}
    writable_inputs = _writable_initial_workdir_inputs(body)
    target_inputs: set[str] = set()
    for output in body.outputs:
        if _is_directory(spell(output.type)) and output.capture is not None:
            for input_name in (output.capture.glob.names if output.capture.glob is not None
                               else inputs_read(output.capture.unread_glob)):
                if input_name in writable_inputs and input_name in inputs \
                        and _is_directory(spell(inputs[input_name].type)):
                    target_inputs.add(input_name)
    return target_inputs


def _writable_initial_workdir_inputs(body: ToolBody) -> set[str]:
    staging = body.need(NeedKind.STAGING)
    listing = staging.body.entries if staging is not None and isinstance(staging.body, Staging) else None
    return {name for entry in listing or () if isinstance(entry, Dirent) and entry.writable is True
            for name in inputs_read(entry.entry)}


def _is_directory(cwl_type: Any) -> bool:
    inner, _ = required(cwl_type)
    return inner == "Directory" or isinstance(inner, list) and "Directory" in inner


def _output_target_directory(value: Any, source_key: str, empty_directory: EmptyDirectory) -> Any:
    """The empty directory for a name, or for a Directory read as one: its `location`, `path` or `basename`. An
    absolute path or a URL is left as written: it is a Directory as CWL reads any, and where a tool may write is the
    runner's to say (`InplaceUpdateRequirement`)."""
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
    if "://" in name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
        return value
    return empty_directory(_output_target_basename(name, source_key))


def _output_target_basename(location: str, source_key: str) -> str:
    basename = PurePath(location).name
    if not basename or basename != location:
        raise ValueError(
            f"output-target Directory input {source_key!r} must be a simple directory name, got {location!r}"
        )
    return basename
