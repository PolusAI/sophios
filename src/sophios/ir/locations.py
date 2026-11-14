"""Where a File or Directory a document or a job names by a relative path is.

A relative `location` or `path` is read against the document or job that wrote
it; once that document is rewritten somewhere else, it is written absolute.
"""
import copy
from collections.abc import Iterator
from pathlib import Path, PurePath
from urllib.parse import urlparse

from ..lang.nodes import OpaqueCwl
from ..wic_types import Yaml


def relative_local_path(written: object) -> str | None:
    """`written` when it names a local file or directory by a relative path; None for anything else.

    Not relative: a non-string, a URI (`file:`, `https:`, ...) and an absolute path. A one-letter scheme
    is a Windows drive.
    """
    if not isinstance(written, str) or len(urlparse(written).scheme) > 1 or Path(written).is_absolute():
        return None
    return written


def input_paths(values: Yaml) -> Iterator[tuple[str, dict[str, OpaqueCwl]]]:
    """Every File and Directory object in a job, with the input it is under, in written order.

    Found in lists, in records, in a File's `secondaryFiles` and in a Directory's `listing`.
    """
    for name, value in values.items():
        stack = [value]
        while stack:
            match stack.pop():
                case list() as items:
                    stack.extend(reversed(items))
                case {'class': 'File' | 'Directory'} as found:
                    yield name, found
                    stack.extend(reversed(found.get('secondaryFiles') or []))
                    stack.extend(reversed(found.get('listing') or []))
                case dict() as record:
                    stack.extend(reversed(list(record.values())))


def absolute_paths(values: Yaml, base: PurePath) -> Yaml:
    """A copy of `values` in which each relative `location` or `path` of a File or Directory, at any
    depth, is resolved against `base`, an absolute directory, and written absolute, so it names the
    same place wherever the run reads it from.

    A `location` is a URI, so it is written as a `file://` URI, on Windows too; a `path` stays a path.
    """
    absolute = copy.deepcopy(values)
    for _name, found in input_paths(absolute):
        for key in ('location', 'path'):
            if (written := relative_local_path(found.get(key))) is not None:
                resolved = base / written
                found[key] = resolved.as_uri() if key == 'location' else str(resolved)
    return absolute
