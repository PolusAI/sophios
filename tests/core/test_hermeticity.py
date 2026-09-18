"""The oracle suite is hermetic.

These properties are statements about the compiler. A property whose inputs
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
from typing import Any

import pytest
import yaml

from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form

from .hermetic import compile_hermetic_cwl
from .synthetic_tools import STEMS, _cwl, clt, inputs_of, outputs_of, required_inputs_of
from .test_zone_boundary import _import_graph, _imports_of, _reachable

TESTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_ROOT.parent

#: Every module in the oracle. Listed rather than globbed so adding a
#: module to the trusted suite remains a reviewable decision. Every executable
#: test file below must also appear here; the coverage test links the two lists.
ORACLE_MODULES = (
    'core.synthetic_tools',
    'core.hermetic',
    'core.ast_strategies',
    'core.reference_model',
    'core.equivalence',
    'core.test_equivalence',
    'core.transformations',
    'core.test_equivalences',
    'core.test_generators',
    'core.test_canonical_emission',
    'core.test_predicates',
    'core.test_reference_compatibility',
    'core.test_canonical_path',
    'core.test_emit',
    'core.test_resolve',
    'core.test_link',
    'core.test_infer_phase',
)

#: Reaching any of these means the suite's meaning depends on the machine.
#: Modules that run tool discovery **at import**, so reaching one at all —
#: however indirectly — makes the suite's meaning depend on the machine.
FORBIDDEN = (
    'core.test_setup',
    'core.compile_harness',
    'core.wic_corpus',
)

#: `sophios.plugins` is different: importing it performs no discovery, while
#: calling its entry points does. A transitive production dependency is safe;
#: a direct oracle import almost certainly intends to call it. The poisoned
#: subprocess below proves dynamically that no indirect call occurs.
#:
FORBIDDEN_DIRECT = ('sophios.plugins',)


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
    """Without this, a typo'd module name makes the check pass by covering nothing."""
    graph = _test_import_graph()
    missing = [m for m in ORACLE_MODULES if m not in graph]
    assert not missing, f'ORACLE_MODULES names modules that do not exist: {missing}'


def _crossings(seeds: tuple[str, ...], graph: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Every `(module, forbidden target)` pair reachable from `seeds`.

    The scan itself, extracted so the test that proves it fires runs the same
    code the test that trusts it runs. Before this, the firing test asserted on
    `FORBIDDEN` and an import set directly — `_reachable` was never called —
    so it stayed green through mutations that broke the scan outright.
    """
    return sorted(
        (module, target)
        for module in seeds
        for target in _reachable(module, graph) | graph.get(module, set())
        if target in FORBIDDEN
    ) + sorted(
        (module, target)
        for module in seeds
        for target in graph.get(module, set())
        if target in FORBIDDEN_DIRECT
    )


@pytest.mark.fast
def test_no_oracle_module_reaches_plugin_discovery() -> None:
    """Static half: no oracle module imports the environment."""
    crossings = _crossings(ORACLE_MODULES, {**_test_import_graph(), **_import_graph()})
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

    Writes a module that imports the environment, points a real oracle module
    at it, and asserts `_crossings` — the scan the test above runs — names both
    crossings. Both halves of the rule are exercised, because they catch
    different things and by different routes:

      * `FORBIDDEN` transitively, so the breach sits one hop past the seed.
        This fails if `_reachable` stops traversing, if the crossings filter is
        inverted, or if the tuple goes empty.
      * `FORBIDDEN_DIRECT` on the seed's own imports, since a transitive reach
        to `sophios.plugins` is deliberately *not* a finding.

    Reading `_imports_of_test_module`'s result and intersecting
    it with the two tuples by hand — `_crossings` was never called, so it
    stayed green through every mutation but the last, in a module whose own
    sibling defect is the same shape.
    """
    breach = tmp_path / 'core' / 'breach.py'
    breach.parent.mkdir()
    breach.write_text('from . import test_setup\n', encoding='utf-8')

    seed = 'core.hermetic'
    graph = {**_test_import_graph(), **_import_graph(),
             'core.breach': _imports_of_test_module(breach)}
    graph[seed] = graph[seed] | {'core.breach', 'sophios.plugins'}
    assert _crossings((seed,), graph) == [(seed, 'core.test_setup'),
                                          (seed, 'sophios.plugins')]


@pytest.mark.needs_cwltool
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize('stem', STEMS)
def test_every_stub_is_valid_cwl(stem: str) -> None:
    """A registry that is not valid CWL makes the validity property a statement about our stubs.

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
    """The independent model and typed declarations must not drift."""
    from sophios.ir.declarations import port_declaration  # pylint: disable=import-outside-toplevel

    in_tool = inputs_of(stem)
    declarations = {name: port_declaration(raw) for name, raw in in_tool.items()}
    theirs = tuple(name for name, declaration in declarations.items()
                   if not ((declaration.has_default and declaration.default is not None)
                           or declaration.type.optional))
    assert required_inputs_of(stem) == theirs


@pytest.mark.fast
def test_falsy_default_still_counts_as_a_default() -> None:
    """Typed declarations preserve the presence of falsy defaults.

    `in_tool[arg].get('default')` used to be used directly as a boolean, so a
    tool declaring `default: False` (or `0`, or `''`) was treated as having no
    default at all — the input was silently promoted to a required,
    caller-supplied workflow input, discarding the tool author's default.
    Reproduces `mm-workflows/cwl_adapters/extract_pdbbind_refined.cwl`'s
    `convert_Kd_dG` input; `control` is the ordinary truthy-default case that
    must keep working alongside it.

    Two assertions so the test fails whichever way the rule breaks: revert to
    truthiness and the falsy one fails; break the ordinary case and the truthy
    one fails.
    """
    from sophios.ir.declarations import port_declaration  # pylint: disable=import-outside-toplevel

    in_tool = {
        'convert_Kd_dG': {'type': 'boolean', 'default': False},
        'control': {'type': 'boolean', 'default': True},
        'extras': {'type': 'File[]', 'default': []},
    }
    declarations = {name: port_declaration(raw) for name, raw in in_tool.items()}
    assert declarations['convert_Kd_dG'].has_default, \
        'a present-but-falsy default must still count as a default'
    assert declarations['control'].has_default, \
        'a present, truthy default must still count as a default'
    assert declarations['extras'].has_default, \
        'an empty-collection default must still count as a default'


@pytest.mark.fast
def test_a_null_default_does_not_satisfy_a_non_nullable_input() -> None:
    """`default: null` carries the one value the input cannot take.

    The predicate also gates edge inference — `args_required` is what the
    compiler iterates to decide which inputs get an inferred edge at all — so
    an input counted as satisfied is an input inference never sees. `null`
    satisfies nothing a non-nullable type accepts, and where the type does
    accept null the optional arms answer on their own; `nullable` is that
    control, and must stay optional for the type's sake rather than the
    default's.
    """
    from sophios.ir.declarations import port_declaration  # pylint: disable=import-outside-toplevel

    in_tool = {
        'required': {'type': 'File', 'default': None},
        'nullable': {'type': ['null', 'File'], 'default': None},
    }
    declarations = {name: port_declaration(raw) for name, raw in in_tool.items()}
    required = declarations['required']
    nullable = declarations['nullable']
    assert not ((required.has_default and required.default is not None)
                or required.type.optional), \
        'a null default cannot satisfy a non-nullable input, so the input stays required'
    assert nullable.type.optional, \
        'a null-permitting type is optional whatever its default'


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

    `compile_hermetic_cwl` is produced here for the property suites to build
    on; nothing in this module calls it. This is that
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
#: discovery disabled". The static-coverage test requires each one to have a
#: matching `ORACLE_MODULES` entry.
ORACLE_FILES: tuple[str, ...] = (
    'tests/core/test_generators.py',
    'tests/core/test_equivalence.py',
    'tests/core/test_equivalences.py',
    'tests/core/test_canonical_emission.py',
    'tests/core/test_predicates.py',
    'tests/core/test_reference_compatibility.py',
    'tests/core/test_canonical_path.py',
    'tests/core/test_emit.py',
    'tests/core/test_resolve.py',
    'tests/core/test_link.py',
    'tests/core/test_infer_phase.py',
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

    Joined with `os.pathsep`, not `':'`. On Windows a hardcoded colon makes
    `D:\\...\\src:D:\\...\\tests` one unparsable entry — `tests/` never
    reaches `sys.path`, and both callers then fail on the very `ImportError`
    the explicit `PYTHONPATH` exists to prevent.
    """
    python_path = os.pathsep.join((str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')))
    env = {**os.environ, 'HOME': str(Path(tempfile.mkdtemp())), 'PYTHONPATH': python_path}
    return subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'core._poison_plugins', '-q',
         '-m', 'not slow', *targets],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)


@pytest.mark.fast
def test_the_static_scan_covers_every_file_the_poisoned_run_covers() -> None:
    """The two halves of this check are two lists, and nothing linked them.

    `ORACLE_MODULES` seeds the static scan; `ORACLE_FILES` is what the poisoned
    subprocess runs. A test file in the second and not the first is covered only
    at runtime, and only on the paths that run actually executes — which is the
    weaker half by this module's own docstring. Nothing imports a test module,
    so it is never reached transitively either.

    Adding `import sophios.plugins` to
    `tests/core/test_generators.py` — a file `ORACLE_FILES` names — left
    `test_no_oracle_module_reaches_plugin_discovery` green.
    """
    named = {f'core.{Path(f).stem}' for f in ORACLE_FILES}
    missing = sorted(named - set(ORACLE_MODULES))
    assert not missing, (
        'ORACLE_FILES runs these under the poison but ORACLE_MODULES does not scan '
        f'them, so an import of the environment in one is invisible: {missing}')


@pytest.mark.slow
@pytest.mark.serial
def test_the_oracle_suite_passes_with_plugin_discovery_disabled() -> None:
    """Runtime half. `HOME` is redirected too: `get_config` writes
    ~/wic/global_config.json when it is missing, so a suite that reads the
    config is not merely environment-dependent, it provisions the environment.

    ORACLE_FILES is empty until the first oracle file lands: there is nothing
    to run poisoned yet, and passing an empty target list to pytest would
    collect the whole repository instead of proving anything about the oracle
    suite. Skipped rather than passed vacuously; the first entry makes this
    assert something real.
    """
    if not ORACLE_FILES:
        pytest.skip('ORACLE_FILES is empty until a later task adds its first oracle test file')
    result = _run_poisoned(ORACLE_FILES)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.slow
@pytest.mark.serial
def test_the_poison_fires_on_a_suite_that_needs_discovery() -> None:
    """The companion probe deliberately calls plugin discovery.

    Corpus fixtures no longer discover plugins during collection, so a
    purpose-built, non-default-collected probe is the stable way to prove the
    poison is installed. If it passes, the run above proved nothing.

    Asserts `POISON_MESSAGE` appears in the output, not merely that the
    subprocess exits nonzero: pytest exits nonzero for reasons unrelated to
    the poison too (a bad `-p` import, "no tests ran"), and a bare `!= 0`
    check is satisfied by any of them just as well as by the poison firing —
    which is exactly how this test passed for the wrong reason before.
    """
    result = _run_poisoned(('tests/core/discovery_probe.py',))
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert POISON_MESSAGE in output, output


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'built', 'expected'), [
    ('a tool declaring no format carries no namespaces',
     lambda: clt({'x': {'type': 'File'}}, {}), None),
    ('a string format declares its prefix',
     lambda: clt({}, {'f': {'type': 'File', 'format': 'edam:format_2330'}}),
     {'edam': 'https://edamontology.org/'}),
    ('a list format declares its prefix',
     lambda: clt({}, {'f': {'type': 'File', 'format': ['edam:format_3752']}}),
     {'edam': 'https://edamontology.org/'}),
    ('an unprefixed format declares nothing',
     lambda: clt({}, {'f': {'type': 'File', 'format': 'plain'}}), None),
])
def test_the_stub_builder_declares_only_the_prefixes_it_uses(
        claim: str, built: Any, expected: dict[str, str] | None) -> None:
    """`$namespaces` follows the tool's own formats, not a fixed set.

    Four modules shared four builders and only one emitted `edam`; folding them
    into one has to pick a behaviour, and picking "always" would have put the
    namespace on tools that never mention a format. Invisible at run time --
    the compiler adds it anyway and format matching does not read it -- which
    is why it needs a test rather than a reader noticing.
    """
    assert built().get('$namespaces') == expected, claim


@pytest.mark.fast
def test_the_stub_builder_shapes_are_distinct() -> None:
    """Raw, canonical and JavaScript are three different documents.

    `canonical=True` is a no-op for a dict-form `inputs:`, so a caller passing
    it gets the same document back; the difference appears for the list form
    the loader produces, which is what `plugins.py` desugars.
    """
    ports = {'f': {'type': 'File', 'inputBinding': {'position': 1}}}
    raw = clt(ports, {})
    canonical = clt(ports, {}, canonical=True)
    javascript = clt(ports, {}, javascript=True)

    assert 'requirements' not in raw
    assert javascript['requirements'] == {'InlineJavascriptRequirement': {}}
    assert canonical['inputs'] == raw['inputs'], 'dict-form inputs are already canonical'
    assert desugar_into_canonical_normal_form(dict(raw)) == canonical
