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
"""
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from sophios.lang.diagnostics import Diagnostic

DATA = Path(__file__).resolve().parent / 'data'


def cases(name: str) -> list[Any]:
    """The cases of the YAML list data/<name>, or of every *.yaml in the directory data/<name>, by file name."""
    path = DATA / name
    files = sorted(path.glob('*.yaml')) if path.is_dir() else [path]
    return [pytest.param(case, id=case['id'])
            for file in files for case in yaml.safe_load(file.read_text(encoding='utf-8'))]


def case_dirs(name: str) -> list[Any]:
    """Each directory in data/<name> as the pair (its path, its expected.yaml), named by the directory."""
    return [pytest.param(case, yaml.safe_load((case / 'expected.yaml').read_text(encoding='utf-8')), id=case.name)
            for case in sorted((DATA / name).iterdir()) if case.is_dir()]


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
            return all(item in actual if isinstance(actual, str) else any(match(item, each) for each in actual)
                       for item in given)
        if form == 'exact':
            return bool(actual == given)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and match(value, actual[key])
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(match, expected, actual))
    return bool(expected == actual)
