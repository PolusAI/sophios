"""Where a File or Directory a document or a job names by a relative path is.

A relative location is read against the document or job that wrote it; once that document is rewritten
somewhere else, it is written absolute.
"""
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlparse

from ..lang.values import FileValue, Location, map_files


def relative_local_path(written: object) -> str | None:
    """`written` when it names a local file or directory by a relative path; None for anything else.

    Not relative: a non-string, a URI (`file:`, `https:`, ...) and an absolute path. A one-letter scheme
    is a Windows drive.
    """
    if not isinstance(written, str) or len(urlparse(written).scheme) > 1 or Path(written).is_absolute():
        return None
    return written


def input_paths(values: Mapping[str, Any]) -> Iterator[tuple[str, FileValue]]:
    """Every File and Directory in a job, with the input it is under, in written order.

    Found in lists, in records, and among what a file holds.
    """
    for name, value in values.items():
        stack = [value]
        while stack:
            match stack.pop():
                case list() as items:
                    stack.extend(reversed(items))
                case FileValue() as found:
                    yield name, found
                    stack.extend(reversed([*found.companions, *(item for _key, item in found.written)]))
                case dict() as record:
                    stack.extend(reversed(list(record.values())))


def absolute_paths(values: Mapping[str, Any], base: PurePath) -> dict[str, Any]:
    """A copy of `values` in which each relative location of a File or Directory, at any depth, is resolved
    against `base`, an absolute directory, and written absolute, so it names the same place wherever the run reads
    it from.

    A location is a URI, so it is written as a `file://` URI, on Windows too; a path stays a path.
    """
    def resolved(found: FileValue) -> FileValue:
        at = found.location
        if at is None or (written := relative_local_path(at.text)) is None:
            return found
        place = base / written
        return replace(found, location=Location(place.as_uri() if at.uri else str(place), at.uri))

    changed: dict[str, Any] = map_files(dict(values), resolved)
    return changed
