"""A File or a Directory as a value: what the middle end holds, and how a door reads one and a target spells one.

A value is JSON plus a `FileValue`. A door that meets a `{class: File, ...}` mapping (a `.wic` literal, a default, a
job file, the Python API) reads it with `read_value`; a target writes it with `write_value`, or as a path. Nothing
between them looks inside a mapping for a file. Pure data and functions over it, importing nothing of the front door
or of a target.
"""
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

_CLASS = 'class'
_LOCATION = 'location'
_PATH = 'path'
_SECONDARY = 'secondaryFiles'
_LISTING = 'listing'
_FORMAT = 'format'


class FileKind(StrEnum):
    """What a `FileValue` is."""

    FILE = 'File'
    DIRECTORY = 'Directory'


@dataclass(frozen=True, slots=True)
class Location:
    """Where a file is: `text`, a path or a URI. `uri` is whether the author wrote it as a location (a URI or a
    path) rather than as a path."""

    text: str
    uri: bool


@dataclass(frozen=True, slots=True)
class FileValue:
    """A File or a Directory.

    `companions` are a File's secondary files and a Directory's listing, themselves `FileValue`s; an empty one is
    not stored. `format` is the format it says it has, where `has_format`. `written` holds every field of the mapping
    the author or the job wrote that no field above holds (`basename`, `checksum`, `contents`, `size`, ...), in the
    order it was written.
    """

    kind: FileKind
    location: Location | None = None
    companions: 'tuple[FileValue, ...]' = ()
    written: 'tuple[tuple[str, Any], ...]' = ()
    format: Any = None
    has_format: bool = False
    #: The keys of the mapping in the order it was written, for a message to print it as the author did.
    keys: 'tuple[str, ...]' = field(default=(), compare=False)


def read_value(value: Any) -> Any:
    """`value` with each `{class: File | Directory, ...}` mapping in it, at any depth, read into a `FileValue`.

    Anything else is as it was: a mapping with a `path` and no class is not a file. Reading what is read already is
    reading it again.
    """
    match value:
        case {'class': 'File' | 'Directory' as kind}:
            return _file(FileKind(kind), value)
        case dict():
            return {key: read_value(item) for key, item in value.items()}
        case list():
            return [read_value(item) for item in value]
    return value


def _file(kind: FileKind, mapping: dict[str, Any]) -> FileValue:
    """The `FileValue` of a File or Directory mapping. Each fact has one home: a `location` beside a `path` is the
    location and the path stays a field of `written`, and a list that is not a list of files stays a field."""
    source = next((key for key in (_LOCATION, _PATH) if isinstance(mapping.get(key), str)), None)
    kind_of_list = _SECONDARY if kind is FileKind.FILE else _LISTING
    listed = [read_value(each) for each in mapping.get(kind_of_list) or ()] if isinstance(
        mapping.get(kind_of_list), list) else []
    held = kind_of_list if listed and all(isinstance(each, FileValue) for each in listed) else None
    return FileValue(
        kind,
        None if source is None else Location(mapping[source], source == _LOCATION),
        tuple(listed) if held else (),
        tuple((key, read_value(item)) for key, item in mapping.items() if key not in (_CLASS, source, held, _FORMAT)),
        read_value(mapping.get(_FORMAT)), _FORMAT in mapping, tuple(mapping))


def write_value(value: Any) -> Any:
    """`value` with each `FileValue` in it, at any depth, written as the mapping CWL spells it: `class`, its
    `location` or `path`, its `secondaryFiles` or `listing`, its `format`, then the rest; in the order the door read the
    keys where it did."""
    match value:
        case FileValue(kind=kind, location=at, companions=companions, written=written, format=format_,
                       has_format=has_format, keys=keys):
            found: dict[str, Any] = {_CLASS: kind.value}
            if at is not None:
                found[_LOCATION if at.uri else _PATH] = at.text
            if companions:
                found[_SECONDARY if kind is FileKind.FILE else _LISTING] = [write_value(each) for each in companions]
            if has_format:
                found[_FORMAT] = write_value(format_)
            found.update((key, write_value(item)) for key, item in written)
            return {key: found[key] for key in keys if key in found} | found
        case dict():
            return {key: write_value(item) for key, item in value.items()}
        case list():
            return [write_value(item) for item in value]
    return value


def map_files(value: Any, change: Callable[[FileValue], FileValue]) -> Any:
    """`value` with `change` of each `FileValue` in it, those it holds first, at any depth."""
    match value:
        case FileValue():
            return change(replace(value, companions=tuple(map_files(each, change) for each in value.companions),
                                  written=tuple((key, map_files(item, change)) for key, item in value.written)))
        case dict():
            return {key: map_files(item, change) for key, item in value.items()}
        case list():
            return [map_files(item, change) for item in value]
    return value
