"""The paths a written job names; the config `sophios.input_output` reads is test_config.py's."""
import string
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

import pytest
from hypothesis import example, given, strategies as st

from sophios.ir.locations import absolute_paths

from .budgets import budget


class _At(NamedTuple):
    key: str
    path: str
    relative: bool


_RELATIVE = st.lists(st.text(string.ascii_letters + string.digits + ' _.-#%?é', min_size=1, max_size=4),
                     min_size=1, max_size=3).map('/'.join)
_KEPT = _RELATIVE.map(lambda path: Path.cwd().anchor + path) | st.sampled_from(
    ['file:///', 'https://example.org/', 's3://bucket/']).flatmap(lambda scheme: _RELATIVE.map(scheme.__add__))


def _named(key: str) -> st.SearchStrategy[_At]:
    return st.builds(_At, st.just(key), _RELATIVE, st.just(True)) | st.builds(_At, st.just(key), _KEPT, st.just(False))


def _objects(children: st.SearchStrategy[list[Any]]) -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(lambda kind, named, nested, kids: {'class': kind, **named, nested: kids},
                     st.sampled_from(['File', 'Directory']), st.sets(st.sampled_from(['location', 'path']), min_size=1)
                     .flatmap(lambda keys: st.fixed_dictionaries({key: _named(key) for key in keys})),
                     st.sampled_from(['secondaryFiles', 'listing']), children)


_SCALARS = st.integers() | st.text(max_size=3) | st.none()
_JOBS = st.dictionaries(st.text(max_size=3), st.recursive(
    _SCALARS | _objects(st.lists(_SCALARS | _objects(st.just([])), max_size=2)),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=3), inner, max_size=3)
    | _objects(st.lists(inner, max_size=2)), max_leaves=8))


def _written(value: Any, base: PurePath | None = None) -> Any:
    """`value` as written, or as written against `base`: a relative `location` as a file URI, a `path` as a path."""
    match value:
        case _At(key, path, relative):
            return path if base is None or not relative else (base / path).as_uri() if key == 'location' else str(
                base / path)
        case dict():
            return {key: _written(item, base) for key, item in value.items()}
        case list():
            return [_written(item, base) for item in value]
    return value


@pytest.mark.fast
@given(_JOBS, st.sampled_from([PurePosixPath('/project'), PureWindowsPath('C:/project'), PurePosixPath('/a b/é')]))
# A `location` is a URI wherever the job is written, a Windows drive too; a `path` stays a path.
@example({'f': {'class': 'File', 'location': _At('location', 'data/a.txt', True),
                'secondaryFiles': [{'class': 'File', 'path': _At('path', 'data/a.idx', True)}]}},
         PureWindowsPath('C:/project'))
@budget(30)
def test_a_relative_job_location_is_written_as_a_file_uri(job: dict[str, Any], base: PurePath) -> None:
    """Each relative `location` or `path` in a job, in lists, records, `secondaryFiles` and `listing`, is written
    against the job's directory; nothing else changes, the job given included."""
    values = _written(job)
    assert absolute_paths(values, base) == _written(job, base)
    assert values == _written(job)
