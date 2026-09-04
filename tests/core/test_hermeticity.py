"""P25: the oracle suite is hermetic.

Spec 2's properties are statements about the compiler. A property whose inputs
come from `get_tools_cwl` is a statement about the compiler *and* about which
plugin repositories the machine has checked out, and when it fails the two
cannot be told apart. See design_docs/core-refactor-design.md §6.1.

Checked two ways, because either alone is weak. The static scan is fast and
covers every module, but cannot see a computed import. The subprocess run
covers computed imports but only the modules it actually executes.

CANNOT DETECT: an import whose target is computed at runtime
(`import_module(f'.{name}', __name__)`), and any environment dependence that
is not an import — reading a file, an environment variable, or the network.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from sophios.lang.cwl import CWL_VERSION

from .hermetic import compile_hermetic_cwl
from .synthetic_tools import STEMS, _cwl, inputs_of, outputs_of, required_inputs_of
from .test_zone_boundary import _import_graph, _imports_of, _reachable

TESTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_ROOT.parent

#: Every module this Spec adds. Listed rather than globbed: a glob would grow
#: silently, and the point of the scan is that adding a module to the oracle is
#: a decision someone made on purpose.
#:
#: Reduced for Task 1 to the two modules that task delivered; Task 2 adds
#: `core.ast_strategies` and Task 3 `core.equivalence`/`core.test_equivalence`
#: as their own modules land. Tasks 4-6 each add their own module below as it
#: lands, and Task 7's final step restores the full list:
#:   'core.transformations', 'core.test_generators', 'core.test_equivalences',
#:   'core.test_canonical_emission', 'core.test_predicates', 'core.test_canonical_path'
ORACLE_MODULES = (
    'core.synthetic_tools',
    'core.hermetic',
    'core.ast_strategies',
    'core.equivalence',
    'core.test_equivalence',
)

#: Reaching any of these means the suite's meaning depends on the machine.
FORBIDDEN = (
    'sophios.plugins',
    'core.test_setup',
    'core.compile_harness',
    'core.wic_corpus',
)


def _imports_of_test_module(path: Path) -> set[str]:
    """The `core.*` modules one test file imports.

    `tests/` has no `__init__.py`, so pytest puts `tests/` on `sys.path` and
    these modules are `core.<name>`, not `tests.core.<name>`. A relative
    `from .synthetic_tools import X` inside `core/hermetic.py` therefore
    resolves to `core.synthetic_tools`, which is what the caller is passed.
    """
    module = 'core' if path.stem == '__init__' else f'core.{path.stem}'
    found = _imports_of(path, module, package='core')
    return found | _imports_of(path, module, package='sophios')


def _test_import_graph() -> dict[str, set[str]]:
    """The in-package import graph for modules under tests/, keyed like pytest.

    `tests/` has no `__init__.py`, so pytest puts `tests/` on `sys.path` and
    imports these as `core.<name>`, not `tests.core.<name>`. The graph uses the
    same names so a module and its importers agree.
    """
    graph: dict[str, set[str]] = {}
    for path in sorted((TESTS_ROOT / 'core').rglob('*.py')):
        module = 'core.' + path.stem if path.stem != '__init__' else 'core'
        graph[module] = _imports_of_test_module(path)
    return graph


@pytest.mark.fast
def test_the_scan_discovers_the_modules_it_claims_to_cover() -> None:
    """Without this, a typo'd module name makes P25 pass by covering nothing."""
    graph = _test_import_graph()
    missing = [m for m in ORACLE_MODULES if m not in graph]
    assert not missing, f'ORACLE_MODULES names modules that do not exist: {missing}'


@pytest.mark.fast
def test_no_oracle_module_reaches_plugin_discovery() -> None:
    """P25, static half: no Spec 2 module imports the environment."""
    graph = {**_test_import_graph(), **_import_graph()}
    crossings = sorted(
        (module, target)
        for module in ORACLE_MODULES
        for target in _reachable(module, graph) | graph.get(module, set())
        if target in FORBIDDEN
    )
    detail = '\n'.join(f'  {m} -> {t}' for m, t in crossings)
    assert not crossings, (
        'the oracle suite must not depend on plugin discovery.\n'
        'design_docs/core-refactor-design.md §6.1: "no installed plugins, no '
        'search_paths_cwl, no dependence on cached containers."\n\n'
        f'{detail}'
    )


@pytest.mark.fast
def test_the_scan_fires_on_a_deliberate_crossing(tmp_path: Path) -> None:
    """A guard nobody has seen fire is a guard whose green means nothing.

    Writes a module that imports the environment, points the graph at it, and
    asserts the crossing is reported. Reverting the FORBIDDEN tuple to empty
    fails this test.
    """
    breach = tmp_path / 'core' / 'breach.py'
    breach.parent.mkdir()
    breach.write_text('import sophios.plugins\n', encoding='utf-8')
    graph = {'core.breach': _imports_of_test_module(breach)}
    assert any(t in FORBIDDEN for t in graph['core.breach'])


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize('stem', STEMS)
def test_every_stub_is_valid_cwl(stem: str) -> None:
    """A registry that is not valid CWL makes P36 a statement about our stubs.

    Run once per stem rather than inside a property: validity does not vary
    with the workflow, and cwltool costs about a second a call.
    """
    import cwltool.main  # pylint: disable=import-outside-toplevel  # expensive; slow lane only

    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / f'{stem}.cwl'
        target.write_text(yaml.safe_dump(_cwl(stem), sort_keys=False), encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 0


@pytest.mark.fast
@pytest.mark.parametrize('stem', STEMS)
def test_required_inputs_agree_with_the_compilers_own_rule(stem: str) -> None:
    """The second implementation and the compiler's must not have drifted."""
    from sophios.compiler import _arg_has_default_or_is_optional  # pylint: disable=import-outside-toplevel

    in_tool = inputs_of(stem)
    theirs = tuple(a for a in in_tool if not _arg_has_default_or_is_optional(a, in_tool))
    assert required_inputs_of(stem) == theirs


@pytest.mark.fast
def test_the_registry_reaches_the_branches_it_claims_to() -> None:
    """A registry that cannot reach a branch silently disables every property
    that depends on it — the generator-adequacy argument, applied to the tools."""
    assert any(inputs_of(s).get('value', {}).get('type') == ['int', 'string'] for s in STEMS), \
        'no union-typed input: types_match list branches unreachable'
    assert any(outputs_of(s) == {} for s in STEMS), 'no terminal tool'
    assert any(len(required_inputs_of(s)) < len(inputs_of(s)) for s in STEMS), \
        'every input is required: the optional-argument path is unreachable'
    formats = {o.get('format') for s in STEMS for o in outputs_of(s).values()}
    assert len({f for f in formats if f}) >= 2, 'one format: inference never has to choose'


@pytest.mark.fast
def test_the_hermetic_entry_point_actually_compiles() -> None:
    """A harness that only imports cleanly is not a harness that works.

    `compile_hermetic_cwl` is produced here for Tasks 2-8 to build every
    property on; nothing in this task's own steps calls it. This is that
    harness's companion: two independent source steps, no explicit edges, so
    the only thing under test is whether the fourteen-argument call against
    `SYNTHETIC_TOOLS` actually reaches the compiler and comes back with CWL.
    """
    yml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'foo.txt'}}},
        {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'bar.txt'}}},
    ]}
    cwl = compile_hermetic_cwl(yml, 'oracle')
    assert cwl['cwlVersion'] == CWL_VERSION
    assert cwl['class'] == 'Workflow'
    step_ids = [step['id'] for step in cwl['steps']]
    assert step_ids == ['oracle__step__1__mk_file', 'oracle__step__2__mk_text']


#: Test files whose passing constitutes "the oracle suite ran with plugin
#: discovery disabled". Reduced for Task 1, which contributes none of them:
#: Tasks 2-6 each uncomment their own file below as it lands, and Task 7's
#: restore leaves the full list active.
ORACLE_FILES: tuple[str, ...] = (
    'tests/core/test_generators.py',
    'tests/core/test_equivalence.py',
    # 'tests/core/test_equivalences.py',
    # 'tests/core/test_canonical_emission.py',
    # 'tests/core/test_predicates.py',
    # 'tests/core/test_canonical_path.py',
)


#: The exact wording `_poisoned` (tests/core/_poison_plugins.py) raises.
#: Asserted against verbatim below, not just a nonzero exit code: pytest
#: returns nonzero for plenty of reasons that have nothing to do with the
#: poison firing — an ImportError while loading `-p` among them, which is
#: exactly the failure `_run_poisoned`'s explicit `PYTHONPATH` now prevents.
POISON_MESSAGE = 'plugin discovery reached under the hermeticity run'


def _run_poisoned(targets: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run pytest over `targets` with discovery poisoned and no config to read.

    `PYTHONPATH` is set explicitly to `src:tests` rather than inherited from
    `os.environ`. `-p core._poison_plugins` is resolved at pytest's
    `consider_preparse`, before collection would otherwise put `tests/` on
    `sys.path` — so without `tests/` on `PYTHONPATH` up front, the plugin
    itself fails to import (`ImportError: No module named 'core'`) and every
    caller of this function fails for that reason instead of the one it
    claims to test. Built explicitly, not merged with an inherited value: a
    `PYTHONPATH` that happens to already include `tests/` in one shell is not
    evidence this works in general.
    """
    python_path = f'{REPO_ROOT / "src"}:{REPO_ROOT / "tests"}'
    env = {**os.environ, 'HOME': str(Path(tempfile.mkdtemp())), 'PYTHONPATH': python_path}
    return subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'core._poison_plugins', '-q',
         '-m', 'not slow', *targets],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)


@pytest.mark.slow
@pytest.mark.serial
def test_the_oracle_suite_passes_with_plugin_discovery_disabled() -> None:
    """P25, runtime half. `HOME` is redirected too: `get_config` writes
    ~/wic/global_config.json when it is missing, so a suite that reads the
    config is not merely environment-dependent, it provisions the environment.

    ORACLE_FILES is empty until Task 2 lands its first file: there is nothing
    to run poisoned yet, and passing an empty target list to pytest would
    collect the whole repository instead of proving anything about the oracle
    suite. Skipped rather than passed vacuously; Task 2 onward makes this
    assert something real.
    """
    if not ORACLE_FILES:
        pytest.skip('ORACLE_FILES is empty until a later task adds its first oracle test file')
    result = _run_poisoned(ORACLE_FILES)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.slow
@pytest.mark.serial
def test_the_poison_fires_on_a_suite_that_needs_discovery() -> None:
    """The companion. `test_fuzzy_compile` samples an environment-dependent
    schema by design, so it must fail under the poison. If it passes, the
    poison is not installed and the run above proved nothing.

    Asserts `POISON_MESSAGE` appears in the output, not merely that the
    subprocess exits nonzero: pytest exits nonzero for reasons unrelated to
    the poison too (a bad `-p` import, "no tests ran"), and a bare `!= 0`
    check is satisfied by any of them just as well as by the poison firing —
    which is exactly how this test passed for the wrong reason before.
    """
    result = _run_poisoned(('tests/core/test_fuzzy_compile.py',))
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert POISON_MESSAGE in output, output
