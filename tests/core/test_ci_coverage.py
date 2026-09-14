"""Every test this repository ships is collected by some configured lane.

A test nothing runs is the cheapest defect to ship and the hardest to see: it
is green by construction, it inflates the collected count, and nothing about a
marker says out loud that no job selects it. That has shipped here three times
— the benchmark harness contracts, `test_canonical_path.py`, and both
determinism properties in `test_canonical_emission.py` — each time because a
marker was applied by habit while no workflow step named the file.

The packaging lane (`build_wheel.yml`) collects by default and is deliberately
lean, so it already reaches every unmarked test. What it cannot reach is
anything carrying a marker its own `-m` expression excludes. Those tests run
only where a step names their file, which makes exactly one invariant worth
enforcing:

    a test carrying a marker the packaging lane excludes must be named by a
    step in a main lane whose own `-m` expression admits it.

The excluded set is read out of `build_wheel.yml` rather than restated here, so
this check follows the packaging lane instead of drifting from it.
"""
import ast
import re
import shlex
from pathlib import Path
from typing import Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WORKFLOWS: Final = REPO_ROOT / '.github' / 'workflows'
PACKAGING_LANE: Final = WORKFLOWS / 'build_wheel.yml'


def _coverage_lanes() -> list[Path]:
    """Every configured workflow except the packaging lane.

    Derived rather than listed: a new workflow is a new lane the moment it
    exists, and a hand-maintained list would be one more pair of things that
    must agree with nothing checking that they do. A weekly lane counts — a
    test that runs weekly is not a test that runs nowhere — though it is the
    weaker place for anything guarding a claim made on every change.
    """
    return sorted(p for p in WORKFLOWS.glob('*.yml') if p != PACKAGING_LANE)


def _flag(tokens: list[str], name: str) -> str | None:
    """The value following `name` in `tokens`, or None if it is absent or last."""
    if name not in tokens:
        return None
    index = tokens.index(name) + 1
    return tokens[index] if index < len(tokens) else None


def _pytest_runs(workflow: Path) -> list[tuple[list[str], str | None, str | None]]:
    """Every `pytest` invocation in a workflow, as (named test files, -m, -k).

    An invocation naming no file collects from the rootdir, so it reaches every
    test module rather than none — `pytest -k test_fuzzy_compile` is a real
    lane for that test even though it names no path.
    """
    if not workflow.exists():
        return []
    document = yaml.safe_load(workflow.read_text(encoding='utf-8')) or {}
    runs: list[tuple[list[str], str | None, str | None]] = []
    for job in (document.get('jobs') or {}).values():
        for step in job.get('steps') or []:
            script = step.get('run')
            if not script or 'pytest' not in script:
                continue
            for line in script.splitlines():
                if 'pytest' not in line:
                    continue
                tokens = shlex.split(line, comments=True)
                # `python -m pytest -m "not skip_pypi_ci"` carries two `-m`: the
                # first selects the module to run, the second is pytest's marker
                # filter. Only the one after the `pytest` token is the filter.
                start = next((i for i, t in enumerate(tokens) if t == 'pytest' or t.endswith('/pytest')), -1)
                if start < 0:
                    continue
                after = tokens[start + 1:]
                # A directory argument is a path too. Reading only `.py` tokens
                # made `pytest tests/contrib` look like "no path named", which
                # this file treats as the whole rootdir — so one such run would
                # mark every marked test under tests/core as covered, which is
                # the single failure this file exists to catch.
                files = [t for t in after
                         if not t.startswith('-') and (REPO_ROOT / t).exists()]
                runs.append((files, _flag(after, '-m'), _flag(after, '-k')))
    return runs


def _excluded_markers(expression: str | None) -> frozenset[str]:
    """The markers `expression` refuses.

    Only the shapes this repository actually writes are understood — a bare
    marker, `not <marker>`, and those joined by `and`. Anything else raises
    rather than returning an empty set: a filter this cannot read is a filter
    whose effect is unknown, and answering "excludes nothing" would turn that
    into a silent pass.
    """
    if expression is None:
        return frozenset()
    excluded: set[str] = set()
    for clause in re.split(r'\band\b', expression):
        clause = clause.strip()
        if not clause:
            continue
        negated = re.fullmatch(r'not\s+([A-Za-z_][A-Za-z0-9_]*)', clause)
        if negated:
            excluded.add(negated.group(1))
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', clause):
            raise ValueError(f'unreadable pytest marker expression: {expression!r}')
    return frozenset(excluded)


def _keyword_admits(expression: str | None, test: str) -> bool:
    """Whether a `-k` expression selects `test`.

    Read the same way `_excluded_markers` reads `-m`: the shapes this repository
    writes — a bare name and `not <name>`, joined by `and` — and a refusal for
    anything else. Treating an unparsed expression as a plain substring makes a
    boolean one match nothing, which reports a test that does run as an orphan.
    """
    if expression is None:
        return True
    for clause in re.split(r'\band\b', expression):
        clause = clause.strip()
        if not clause:
            continue
        negated = re.fullmatch(r'not\s+([A-Za-z_][A-Za-z0-9_]*)', clause)
        if negated:
            if negated.group(1) in test:
                return False
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', clause):
            raise ValueError(f'unreadable pytest -k expression: {expression!r}')
        if clause not in test:
            return False
    return True


def _marked_tests(path: Path) -> dict[str, frozenset[str]]:
    """Each `test_*` function in `path` mapped to the pytest markers on it.

    Module-level `pytestmark` and class-level decorators both apply to the
    tests beneath them, so both are folded in; a marker inherited from the
    module excludes a test from a lane exactly as a decorator would.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'), str(path))

    def names(decorators: list[ast.expr]) -> set[str]:
        found: set[str] = set()
        for decorator in decorators:
            node = decorator.func if isinstance(decorator, ast.Call) else decorator
            parts: list[str] = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            parts.reverse()
            if len(parts) >= 3 and parts[0] == 'pytest' and parts[1] == 'mark':
                found.add(parts[2])
        return found

    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'pytestmark' for t in node.targets):
            elements = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            module_level |= names(list(elements))

    marked: dict[str, frozenset[str]] = {}

    def walk(body: list[ast.stmt], inherited: set[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, inherited | names(node.decorator_list))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                marked[node.name] = frozenset(inherited | names(node.decorator_list))

    walk(tree.body, module_level)
    return marked


def _path_reaches(files: list[str], relative: str) -> bool:
    """Whether an invocation naming `files` collects `relative`.

    An invocation naming no path collects the whole rootdir. A directory
    argument reaches everything beneath it, so this is a prefix check rather
    than equality.
    """
    if not files:
        return True
    return any(relative == f or relative.startswith(f.rstrip('/') + '/') for f in files)


def _test_files() -> list[Path]:
    """Every test module in the repository."""
    return sorted((REPO_ROOT / 'tests').rglob('test_*.py'))


@pytest.mark.fast
def test_the_census_sees_the_repo() -> None:
    """Zero files, or a packaging lane that excludes nothing, is a green test
    that enforces nothing."""
    assert _test_files(), 'no test modules found; the census is vacuous'
    assert _pytest_runs(PACKAGING_LANE), f'no pytest invocation found in {PACKAGING_LANE.name}'
    assert any(_excluded_markers(m) for _, m, _k in _pytest_runs(PACKAGING_LANE)), (
        f'{PACKAGING_LANE.name} excludes no markers, so this census has nothing to check; '
        'if that is deliberate, this file should go rather than pass vacuously'
    )


@pytest.mark.fast
def test_no_marked_test_is_collected_by_nothing() -> None:
    """A test the packaging lane excludes must be named by a main lane that admits it."""
    packaging_excludes: frozenset[str] = frozenset().union(
        *(_excluded_markers(m) for _, m, _k in _pytest_runs(PACKAGING_LANE)))
    main_runs = [run for lane in _coverage_lanes() for run in _pytest_runs(lane)]

    orphans: list[str] = []
    for path in _test_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for test, markers in _marked_tests(path).items():
            blocking = markers & packaging_excludes
            if not blocking:
                continue  # the packaging lane's default collection reaches it
            admitted = any(
                _path_reaches(files, relative)   # no path named means the whole rootdir
                and _keyword_admits(keyword, test)
                and not (_excluded_markers(marker) & blocking)
                for files, marker, keyword in main_runs
            )
            if not admitted:
                orphans.append(f'{relative}::{test} (marked {", ".join(sorted(blocking))})')

    assert not orphans, (
        'these tests are collected by no configured lane — the packaging lane excludes their '
        'marker and no main-lane step names their file:\n  ' + '\n  '.join(orphans)
    )


@pytest.mark.fast
@pytest.mark.parametrize(('files', 'relative', 'reached'), [
    ([], 'tests/core/test_x.py', True),                                  # no path: the rootdir
    (['tests/contrib'], 'tests/contrib/test_x.py', True),                # a directory reaches beneath it
    (['tests/contrib'], 'tests/core/test_x.py', False),                  # but only beneath it
    (['tests/core/test_x.py'], 'tests/core/test_x.py', True),
])
def test_a_directory_argument_reaches_only_what_is_under_it(
        files: list[str], relative: str, reached: bool) -> None:
    """A directory is a path, not the absence of one.

    Reading only `.py` tokens made `pytest tests/contrib` look like an
    invocation that named no path, which this file treats as the whole rootdir.
    One such run would then mark every marked test under `tests/core` as
    covered — the single failure this file exists to catch.
    """
    assert _path_reaches(files, relative) is reached


@pytest.mark.fast
@pytest.mark.parametrize(('expression', 'test', 'admitted'), [
    (None, 'test_anything', True),
    ('test_fuzzy_compile', 'test_fuzzy_compile', True),
    ('test_fuzzy_compile', 'test_other', False),
    ('not test_a and not test_b', 'test_c', True),
    ('not test_a and not test_b', 'test_a', False),
])
def test_a_k_expression_is_read_not_matched_as_a_substring(
        expression: str | None, test: str, admitted: bool) -> None:
    """`-k` gets the same treatment as `-m`: read the shapes written here, refuse the rest.

    Matching a whole expression as a plain substring makes a boolean one match
    nothing, which reports a test that does run as collected by no lane.
    """
    assert _keyword_admits(expression, test) is admitted


@pytest.mark.fast
def test_an_unreadable_k_expression_raises_rather_than_guessing() -> None:
    """The same refusal `_excluded_markers` makes, for the same reason: an
    expression this cannot read has an unknown effect, and guessing either way
    is a silent wrong answer."""
    with pytest.raises(ValueError, match='unreadable pytest -k expression'):
        _keyword_admits('test_a or test_b', 'test_a')
