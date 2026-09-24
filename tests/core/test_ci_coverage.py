"""Every test in the repository is collected by some configured lane.

A test that no lane names is a test that never runs, and nothing else notices:
the suite is green, the file is present, and the claim it makes is unchecked.

**Selection is not modelled here.** Each invocation's own arguments are handed
to `pytest --collect-only`, and the node ids it reports are the answer. A model
of `-k` and `-m` is a second implementation of something this repository
already ships, and it has to agree with the first -- which twice it did not. It
matched a test's name but not its module's, so renaming a file silently dropped
three tests from three lanes; and it ignored markers and case, so `-k "not
FAST"` read as selecting every `@pytest.mark.fast` test where pytest deselects
all of them. Asking pytest cannot disagree with pytest.
"""
import ast
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WORKFLOWS: Final = REPO_ROOT / '.github' / 'workflows'

#: Arguments that change what pytest *reports* rather than what it selects.
#: Dropped before collecting: `-vv` overrides the `-q` this parses output with,
#: and `--workers`/`--cwl_runner` need plugins a collection does not.
#:
#: This is the whole of what is assumed about pytest's command line. Everything
#: deciding which tests run -- paths, node ids, `-k`, `-m` -- passes through
#: untouched and is answered by pytest itself.
REPORTING_ONLY: Final = frozenset({
    '-v', '-vv', '-vvv', '--verbose', '-q', '--quiet', '-s', '--no-header', '-ra', '-x',
})

#: Reporting arguments that take a value, so the value is dropped with them.
REPORTING_WITH_VALUE: Final = frozenset({'--workers', '--cwl_runner', '-n', '--parallel'})


def _selection_argv(argv: list[str]) -> list[str]:
    """`argv` with the arguments that do not decide selection removed."""
    kept: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token in REPORTING_WITH_VALUE:
            skip = True
        elif token not in REPORTING_ONLY and not token.startswith('--cov'):
            kept.append(token)
    return kept


def _invocations(workflow: Path) -> list[list[str]]:
    """Every `pytest` invocation in one workflow, as the arguments after it."""
    found: list[list[str]] = []
    script = yaml.safe_load(workflow.read_text(encoding='utf-8'))
    for job in (script.get('jobs') or {}).values():
        for step in (job.get('steps') or []):
            for line in (step.get('run') or '').splitlines():
                if not re.search(r'\bpytest\b', line):
                    continue
                tokens = shlex.split(line, comments=True)
                if 'pytest' in tokens:
                    found.append(_selection_argv(tokens[tokens.index('pytest') + 1:]))
    return found


def _collect(argv: list[str]) -> set[str]:
    """The tests pytest selects for `argv`, as `path::name` without parameters.

    Raises:
        AssertionError: If pytest cannot collect the invocation at all, which
            is what a lane naming a file that no longer exists looks like.
    """
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '--no-header',
         '-p', 'no:randomly', '-p', 'no:cacheprovider', *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    assert result.returncode in (0, 5), (
        f'pytest could not collect `pytest {" ".join(argv)}`:\n{result.stdout[-2000:]}')
    return {line.split('[')[0] for line in result.stdout.splitlines() if '::' in line}


def _covered() -> set[str]:
    """Every test any configured invocation collects."""
    argvs = sorted({tuple(argv) for lane in sorted(WORKFLOWS.glob('*.yml'))
                    for argv in _invocations(lane)})
    covered: set[str] = set()
    for argv in argvs:
        # Collection imports test_setup, which writes the generated schema.
        # Concurrent collectors can observe another process's truncated file.
        covered.update(_collect(list(argv)))
    return covered


@pytest.mark.fast
@pytest.mark.fast
def test_no_lane_checks_our_own_repo_out_at_a_literal_ref() -> None:
    """A lane tests the ref it was triggered on, or it tests nothing it claims.

    `run_workflows_weekly.yml` carried `ref: master` on its own checkout, so a
    `workflow_dispatch` on a branch reported itself against that branch's sha
    and built master. Three dispatches produced byte-identical failures before
    anyone looked at the checkout step; the run page gives no hint, because the
    sha it shows is the dispatched one.

    Third-party checkouts are exempt: pinning `biobb_adapters` to its master is
    a real choice about someone else's repository.
    """
    offenders = []
    for workflow in sorted(WORKFLOWS.glob('*.yml')):
        script = yaml.safe_load(workflow.read_text(encoding='utf-8'))
        for job in (script.get('jobs') or {}).values():
            for step in (job.get('steps') or []):
                if not str(step.get('uses', '')).startswith('actions/checkout'):
                    continue
                spec = step.get('with') or {}
                repo, ref = str(spec.get('repository', '')), str(spec.get('ref', ''))
                if repo.endswith('/sophios') and ref and '${{' not in ref:
                    offenders.append(f'{workflow.name}: checks out sophios at {ref!r}')
    assert not offenders, '\n'.join(offenders)


def test_the_census_sees_the_repo() -> None:
    """Zero invocations is a green census that checks nothing."""
    found = [a for lane in WORKFLOWS.glob('*.yml') for a in _invocations(lane)]
    assert len(found) > 20, f'only {len(found)} pytest invocations found; the census is aimed wrong'


@pytest.mark.fast
def test_collection_is_pytests_answer_and_not_ours() -> None:
    """The helper really asks pytest, and pytest really narrows.

    If `_collect` returned everything whatever its arguments, the census below
    would pass no matter which lanes existed.
    """
    everything = _collect([])
    one_file = _collect(['tests/core/test_ci_coverage.py'])
    assert one_file and one_file < everything
    assert all(node.startswith('tests/core/test_ci_coverage.py::') for node in one_file)
    assert not _collect(['tests/core/test_ci_coverage.py', '-k', 'no_such_test_name_exists'])


@pytest.mark.slow
def test_no_test_is_collected_by_nothing() -> None:
    """Every test the repository defines is selected by some lane.

    Slow because it asks pytest once per distinct invocation -- about thirty
    sequential collections. They cannot run concurrently while test_setup
    writes the shared generated schema. That is the price of the answer being
    pytest's rather than a model's, and a model is what this file used to be.
    """
    orphans = sorted(_collect([]) - _covered())
    assert not orphans, (
        'these tests are collected by no configured lane, so they never run:\n  '
        + '\n  '.join(orphans)
        + '\n\nName their file in a lane step, or delete them.')


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


def _test_files() -> list[Path]:
    """Every test module in the repository."""
    return sorted((REPO_ROOT / 'tests').rglob('test_*.py'))


def _tests_importing(module: str, path: Path) -> set[str]:
    """Every `test_*` function in `path` whose own body imports `module`.

    The imports this looks for are deliberately function-local — cwltool is
    expensive to import, so these tests pay for it only when they run. That
    also means a module-level scan would not find them.
    """
    source = path.read_text(encoding='utf-8')
    if f'import {module}' not in source:
        return set()
    tree = ast.parse(source, str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith('test_')):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import) and any(a.name.split('.')[0] == module for a in inner.names):
                found.add(node.name)
    return found


#: Every test that does not run on the Windows leg, and the evidence that
#: excluding it takes nothing away: each one ran *nowhere* until a lane step
#: named its file, so none has ever executed on Windows. Measured on the
#: `Lint And Test` Windows job, where the step that names them selects 45 tests
#: — 35 pass and these 10 fail on `import pwd`.
#:
#: `test_canonical_path.py`'s two are reached only by `build_wheel.yml`, which
#: is `runs-on: ubuntu-latest`, so they have no Windows run to lose either.
#: `test_emit.py`'s validator pair exercise cwltool itself; the phase lane
#: collects their platform-neutral siblings on Windows and these two run on
#: the POSIX matrix legs where cwltool's `pwd` dependency is available.
#:
#: The list is the claim. Growing it is a deliberate edit here, not a marker
#: added in passing, because every entry is Windows coverage given up.
WINDOWS_EXCLUDED: Final = frozenset({
    'tests/core/test_canonical_path.py::test_compiled_output_validates_as_cwl',
    'tests/core/test_canonical_path.py::test_cwltool_validate_rejects_an_invalid_document',
    'tests/core/test_emit.py::test_emit_validates_as_cwl_v1_2',
    'tests/core/test_emit.py::test_validator_rejects_the_independent_invalid_control',
    'tests/core/test_hermeticity.py::test_every_stub_is_valid_cwl',
    'tests/core/test_lang_version.py::test_annotation_is_declared_and_the_cwl_stays_valid',
    'tests/core/test_leak_boundary.py::test_residue_validates_as_cwl_v1_2',
    # One compile and one `--validate` of a four-line workflow, under a second.
    # It buys the authored `outputSource` path, which the residue property
    # above cannot reach: the workflow strategy never writes one, so only the
    # synthesized path was ever validated, and the authored one emitted a
    # reference to a step the document does not contain.
    'tests/core/test_leak_boundary.py::test_an_authored_output_source_validates',
})


@pytest.mark.fast
def test_no_test_leaves_the_windows_leg_without_being_listed() -> None:
    """The set of tests skipped off POSIX is exactly the recorded one.

    `needs_cwltool` is what `conftest.py` skips on Windows, so marking a test
    with it removes that test from one leg of the matrix. That is a cost, and
    the point of pinning the inventory is that paying it has to be deliberate:
    a marker added in passing fails here, and so does one removed.

    The membership was not derived by reading imports. It is the failure list
    from the Windows job, checked back against which lane collected each test
    before — all five ran nowhere at all until a step named their files.
    """
    marked = {
        f'{path.relative_to(REPO_ROOT).as_posix()}::{test}'
        for path in _test_files()
        for test, markers in _marked_tests(path).items()
        if 'needs_cwltool' in markers
    }
    assert marked == WINDOWS_EXCLUDED, (
        'the set of tests excluded from the Windows leg has changed.\n'
        f'  newly excluded: {sorted(marked - WINDOWS_EXCLUDED) or "none"}\n'
        f'  no longer excluded: {sorted(WINDOWS_EXCLUDED - marked) or "none"}\n'
        'Update WINDOWS_EXCLUDED if that is intended, and say what the test now costs.'
    )


@pytest.mark.fast
def test_every_cwltool_test_declares_that_it_needs_cwltool() -> None:
    """A test that calls cwltool must say so, because cwltool is POSIX-only.

    `import cwltool.main` pulls in spython, which imports `pwd` at module
    scope. On Windows that is a `ModuleNotFoundError`, so such a test fails
    wherever a lane names it on that matrix leg — which is how ten of them
    reddened `lint_and_test` the first time a step collected them.

    `conftest.py` skips `needs_cwltool` off POSIX. That only works for tests
    that carry the marker, so the marker is what this pins: the platform fact
    has one home, and forgetting to point at it is a failure here rather than
    on one leg of a matrix.
    """
    undeclared = [
        f'{path.relative_to(REPO_ROOT).as_posix()}::{test}'
        for path in _test_files()
        for test in sorted(_tests_importing('cwltool', path))
        if 'needs_cwltool' not in _marked_tests(path).get(test, frozenset())
    ]
    assert not undeclared, (
        'these tests import cwltool but do not carry @pytest.mark.needs_cwltool, so nothing '
        'skips them where cwltool cannot be imported:\n  ' + '\n  '.join(undeclared)
    )
