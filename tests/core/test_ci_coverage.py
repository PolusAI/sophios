"""Every test in the repository is collected by some pull-request or weekly lane, but those of a `LOCAL_ONLY`
file, run by hand.

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
import json
import shlex
import subprocess
import sys
import tempfile
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

#: Reporting arguments spelled `--flag=value`. `--junitxml` would also write a
#: report from inside the census's own collection.
REPORTING_PREFIXES: Final = ('--cov', '--junitxml', '--hypothesis-seed')


def _selection_argv(argv: list[str]) -> list[str]:
    """`argv` with the arguments that do not decide selection removed."""
    kept: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token in REPORTING_WITH_VALUE:
            skip = True
        elif token not in REPORTING_ONLY and not token.startswith(REPORTING_PREFIXES):
            kept.append(token)
    return kept


def _invocations(workflow: Path) -> list[list[str]]:
    """Every `pytest` invocation in one workflow, as the arguments after it."""
    found: list[list[str]] = []
    script = yaml.safe_load(workflow.read_text(encoding='utf-8'))
    for job in (script.get('jobs') or {}).values():
        for step in (job.get('steps') or []):
            for line in (step.get('run') or '').splitlines():
                if 'pytest' not in line:
                    continue
                tokens = shlex.split(line, comments=True)
                if 'pytest' in tokens:
                    found.append(_selection_argv(tokens[tokens.index('pytest') + 1:]))
    return found


def _collect_all(argvs: list[list[str]], *, parameters: bool = False) -> list[set[str]]:
    """The tests pytest selects for each of `argvs`, as `path::name`, with its parameters when `parameters`.

    `lane_collector` asks pytest about all of them in one interpreter, so the
    imports every collection needs are paid for once.

    Raises:
        AssertionError: If pytest cannot collect an invocation at all, which
            is what a lane naming a file that no longer exists looks like.
    """
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / 'report.json'
        run = subprocess.run([sys.executable, '-m', 'tests.core.lane_collector', str(report)],
                             input=json.dumps(argvs), capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        assert report.exists(), f'the collector failed:\n{run.stderr[-2000:]}'
        results = json.loads(report.read_text(encoding='utf-8'))
    selected = []
    for argv, (code, ids, output) in zip(argvs, results):
        assert code in (0, 5), f'pytest could not collect `pytest {" ".join(argv)}`:\n{output}'
        selected.append(set(ids) if parameters else {node.split('[')[0] for node in ids})
    return selected


#: The release lane selects every test not marked skip_pypi_ci or nextflow, so counting it would hide a test
#: that no pull-request or weekly lane runs.
RELEASE_LANE: Final = 'test_and_publish_pypi.yml'


def _lane_argvs() -> list[list[str]]:
    """Every distinct pytest invocation the pull-request and weekly lanes make."""
    return [list(argv) for argv in sorted({tuple(argv) for lane in sorted(WORKFLOWS.glob('*.yml'))
                                           if lane.name != RELEASE_LANE for argv in _invocations(lane)})]


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


@pytest.mark.fast
def test_the_weekly_property_lane_runs_the_whole_oracle_suite() -> None:
    """The deep lane draws every oracle property, slow ones included.

    `ORACLE_FILES` names the suite whose inputs are synthetic, which is what
    lets the weekly lane run on a bare hosted runner. A file added there and
    not to the lane would be deepened nowhere, and nothing else would notice.
    """
    from .hermetic import ORACLE_FILES  # pylint: disable=import-outside-toplevel

    *lanes, oracle = _collect_all([*_invocations(WORKFLOWS / 'property_weekly.yml'), list(ORACLE_FILES)])
    missing = sorted(oracle - set().union(*lanes))
    assert not missing, 'the weekly property lane does not run:\n  ' + '\n  '.join(missing)


@pytest.mark.fast
def test_the_corpus_lane_round_trips_every_wic_root_through_the_python_api() -> None:
    """The lane whose config reaches the external corpora runs the from_wic property.

    `test_python_api_from_wic.py` parametrizes its corpus property over every
    `.wic` root `search_paths_wic` reaches. Only `run_workflows.yml` writes a
    config that reaches the mm-workflows and image-workflows roots; every
    other lane checks out neither corpus, so there the property sees the
    repository's own roots and nothing else.
    """
    properties = {
        'tests/core/test_python_api_from_wic.py::test_a_wic_file_and_its_python_objects_compile_alike',
    }
    selected = set().union(*_collect_all(_invocations(WORKFLOWS / 'run_workflows.yml')))
    missing = sorted(properties - selected)
    assert not missing, 'run_workflows.yml does not run:\n  ' + '\n  '.join(missing)


@pytest.mark.fast
@pytest.mark.parametrize('workflow_name', ['build_wheel.yml', 'test_and_publish_pypi.yml'])
def test_package_lanes_leave_nextflow_runtime_to_its_runtime_lane(workflow_name: str) -> None:
    """Package lanes do not collect tests whose executable they do not install: no test marked `nextflow`, in any
    file, a parameter marked so included, as `tests/core/test_sites.py` marks its Nextflow rows."""
    runtime_tests, *package_tests = _collect_all(
        [['tests/core', '-m', 'nextflow'], *_invocations(WORKFLOWS / workflow_name)], parameters=True
    )
    assert runtime_tests
    assert runtime_tests.isdisjoint(set().union(*package_tests))


@pytest.mark.fast
def test_every_nextflow_runtime_test_runs_in_a_pull_request_lane() -> None:
    """Each test marked `nextflow`, but the corpus's, is selected by a lane a pull request runs, a parameter marked
    so included: a file whose other parameters the package job runs, as `tests/core/test_sites.py`, would otherwise
    hide that its Nextflow rows run only in the weekly matrix."""
    pull_request = [argv for lane in sorted(WORKFLOWS.glob('*.yml')) if lane.name != RELEASE_LANE
                    and 'weekly' not in lane.name for argv in _invocations(lane)]
    runtime, *lanes = _collect_all([['tests/core', '-m', 'nextflow and not nextflow_corpus'], *pull_request],
                                   parameters=True)
    missing = sorted(runtime - set().union(*lanes))
    assert runtime and not missing, 'no pull-request lane runs these Nextflow tests:\n  ' + '\n  '.join(missing)


@pytest.mark.fast
def test_only_the_corpus_lane_runs_the_nextflow_corpus() -> None:
    """The corpus runs need its checkouts, images and data; only run_workflows.yml has them."""
    lanes = [(lane.name, argv) for lane in sorted(WORKFLOWS.glob('*.yml')) for argv in _invocations(lane)]
    corpus, *selected = _collect_all([['tests/core/nextflow', '-m', 'nextflow_corpus'], *(argv for _, argv in lanes)])
    assert corpus
    assert {name for (name, _), tests in zip(lanes, selected) if tests & corpus} == {'run_workflows.yml'}
    assert corpus <= set().union(*(tests for (name, _), tests in zip(lanes, selected) if name == 'run_workflows.yml'))


#: The test files no lane can run, each run by hand instead. The set is the
#: claim: every entry is a file CI never checks, so it is named here whole,
#: never matched by a pattern, and the census below fails once a lane does
#: collect it, so the entry cannot outlive its reason.
#:
#: `test_compute_service.py` runs `sophios.compute` against the slurm driver,
#: which lives in a private repository Sophios CI cannot check out. Its lane is
#: local: `SOPHIOS_SLURM_DRIVER=<driver checkout>/packages/slurm-driver
#: SOPHIOS_NEXTFLOW_ENV=<a conda env holding nextflow> pytest
#: tests/core/test_compute_service.py -m skip_pypi_ci`; without
#: SOPHIOS_NEXTFLOW_ENV its Nextflow tests skip. It expires when Sophios
#: CI can check the driver out: then a lane step names the file, and the entry
#: goes.
LOCAL_ONLY: Final = frozenset({'tests/core/test_compute_service.py'})


@pytest.mark.slow
def test_no_test_is_collected_by_nothing() -> None:
    """Every test the repository defines is selected by some pull-request or weekly lane, but those
    of a `LOCAL_ONLY` file, which no lane selects.

    Slow because it asks pytest once per distinct invocation -- about thirty
    collections, in one interpreter. That is the price of the answer being
    pytest's rather than a model's, and a model is what this file used to be.
    """
    everything, *lanes = _collect_all([[], *_lane_argvs()])
    collected = set().union(*lanes)
    local = {node for node in everything if node.split('::')[0] in LOCAL_ONLY}
    assert {node.split('::')[0] for node in local} == LOCAL_ONLY, 'a LOCAL_ONLY file has no test left'
    assert not local & collected, (
        'a lane now collects a LOCAL_ONLY file; take it out of LOCAL_ONLY:\n  '
        + '\n  '.join(sorted(local & collected)))
    orphans = sorted(everything - collected - local)
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
