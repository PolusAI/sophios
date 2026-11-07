"""Test cases held as data under tests/core/data, and the forms their expectations are written in.

A case list is a YAML list of mappings, each with an `id`: the layout of data/cwl-v1.2/cases.yaml. A case
directory holds a case's files beside its expected.yaml. Each case is one pytest parameter.

`match(expected, actual)` reads an expectation:
- `{re: pattern}`: text in which re.search finds the pattern
- `{startswith: text}`: text starting with `text`
- `{contains: [item, ...]}`: text holding each item, or a list in which each item matches some element
- `{exact: value}`: equal to `value`, so a mapping with any other key fails
- any other mapping: a mapping with at least its keys, each value matching
- a list: a list of the same length, matching element by element
- anything else: an equal value
A mapping whose one key is re, startswith, contains or exact is therefore always a form, never data.
`contains: []` matches nothing, so an empty list cannot pass for an expectation.

The loaders raise ValueError, a collection error of the calling module, for no cases, a repeated id, or a key the
test does not read: pytest skips an empty parametrize silently, and a misspelled key would go unread.
"""
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from sophios.lang.diagnostics import Diagnostic

DATA = Path(__file__).resolve().parent / 'data'


def _checked(where: Path, found: list[tuple[Path, dict[str, Any]]], keys: tuple[str, ...]) -> None:
    """Refuse `found`, the (file, case) pairs of `where`, when it is empty, repeats an id or has a key not in `keys`."""
    if not found:
        raise ValueError(f'{where}: holds no cases')
    ids = [case['id'] for _, case in found]
    if repeated := sorted({each for each in ids if ids.count(each) > 1}):
        raise ValueError(f'{where}: ids {repeated} name more than one case')
    for file, case in found:
        if unread := set(case) - {'id', 'why', *keys}:
            raise ValueError(f'{file}: case {case["id"]!r} holds {sorted(unread)}, which its test does not read')


def cases(name: str, *keys: str) -> list[Any]:
    """The cases of the YAML list data/<name>, or of every *.yaml in the directory data/<name>, by file name.

    `keys` are the keys the calling reader reads; `id` and `why` are always allowed."""
    path = DATA / name
    if not path.exists():
        raise ValueError(f'{path}: no such case file or directory')
    found = []
    for file in sorted(path.glob('*.yaml')) if path.is_dir() else [path]:
        loaded = yaml.safe_load(file.read_text(encoding='utf-8'))
        if not isinstance(loaded, list) or not loaded:
            raise ValueError(f'{file}: holds no case list')
        for index, case in enumerate(loaded):
            if not isinstance(case, dict) or not isinstance(case.get('id'), str):
                raise ValueError(f'{file}: case {index} has no id')
            found.append((file, case))
    _checked(path, found, keys)
    return [pytest.param(case, id=case['id']) for _, case in found]


def case_dirs(name: str, *keys: str) -> list[Any]:
    """Each directory in data/<name> as the pair (its path, its expected.yaml), named by the directory.

    `keys` are the keys the calling reader reads from expected.yaml; `why` is always allowed."""
    path = DATA / name
    if not path.is_dir():
        raise ValueError(f'{path}: no such case directory')
    found = []
    for entry in sorted(path.iterdir()):
        if entry.name.startswith('.'):
            continue
        file = entry / 'expected.yaml'
        if not entry.is_dir() or not file.is_file():
            raise ValueError(f'{entry}: not a case directory with an expected.yaml')
        expected = yaml.safe_load(file.read_text(encoding='utf-8'))
        if not isinstance(expected, dict):
            raise ValueError(f'{file}: holds no mapping')
        found.append((file, {**expected, 'id': entry.name}))
    _checked(path, found, keys)
    return [pytest.param(file.parent, {key: value for key, value in case.items() if key != 'id'},
                         id=case['id']) for file, case in found]


def diagnostic(found: Diagnostic) -> dict[str, Any]:
    """`found` as `--diagnostics json` prints it, but with its code by name, as cases write it."""
    return {**found.to_json(), 'code': found.code.name}


def match(expected: Any, actual: Any) -> bool:  # pylint: disable=too-many-return-statements  # one per form
    """Whether `actual` is what `expected` says, in the forms the module docstring lists."""
    if isinstance(expected, dict) and len(expected) == 1:
        (form, given), = expected.items()
        if form == 're':
            return isinstance(actual, str) and re.search(given, actual) is not None
        if form == 'startswith':
            return isinstance(actual, str) and actual.startswith(given)
        if form == 'contains':
            return bool(given) and all(item in actual if isinstance(actual, str)
                                       else any(match(item, each) for each in actual) for item in given)
        if form == 'exact':
            return bool(actual == given)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and match(value, actual[key])
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(match, expected, actual))
    return bool(expected == actual)
