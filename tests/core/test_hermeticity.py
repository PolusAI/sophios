"""The oracle suite is hermetic.

These properties are statements about the compiler. A property whose inputs
come from `get_tools_cwl` is a statement about the compiler *and* about which
plugin repositories the machine has checked out, and when it fails the two
cannot be told apart. See design_docs/core-refactor-design.md §6.1.

Checked by running the oracle suite in a subprocess with plugin discovery
poisoned. CANNOT DETECT: a module the run does not execute, and any environment
dependence that is not an import — reading a file, an environment variable, or
the network.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from .synthetic_tools import STEMS, inputs_of, outputs_of, required_inputs_of

TESTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_ROOT.parent


#: Declarations whose required-ness turns on a default's presence rather than
#: its truthiness, beside every synthetic input.
_DEFAULTED: Final = (
    ({'type': 'boolean', 'default': False}, False),
    ({'type': 'string', 'default': ''}, False),
    ({'type': 'File[]', 'default': []}, False),
    ({'type': 'File', 'default': None}, True),
    ({'type': ['null', 'File'], 'default': None}, False),
)


@pytest.mark.fast
@pytest.mark.parametrize('raw, expected', [
    *((inputs_of(stem)[name], name in required_inputs_of(stem))
      for stem in STEMS for name in inputs_of(stem)),
    *_DEFAULTED,
])
def test_required_inputs_agree_with_the_compilers_own_rule(raw: Any, expected: bool) -> None:
    """`required_inputs_of`, the generator's independent model, and the
    compiler's `declarations.required` agree. A present default satisfies an
    input however falsy; `null` satisfies only a type that admits it."""
    from sophios.ir.declarations import port_declaration, required  # pylint: disable=import-outside-toplevel

    assert required(port_declaration(raw)) is expected


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
    assert any(len(outputs_of(s)) > 1 for s in STEMS), 'no tool promotes several outputs'
    assert any(i.get('type') == 'Directory' for s in STEMS for i in inputs_of(s).values()), \
        'no Directory input: its job value is never coerced'


#: Test files whose passing constitutes "the oracle suite ran with plugin
#: discovery disabled".
ORACLE_FILES: tuple[str, ...] = (
    'tests/core/test_generators.py',
    'tests/core/test_equivalence.py',
    'tests/core/test_equivalences.py',
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


@pytest.mark.slow
@pytest.mark.serial
def test_the_oracle_suite_passes_with_plugin_discovery_disabled() -> None:
    """Runtime half. `HOME` is redirected too: `get_config` writes
    ~/wic/global_config.json when it is missing, so a suite that reads the
    config is not merely environment-dependent, it provisions the environment.
    """
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


def _budgets_under(scale: str | None) -> subprocess.CompletedProcess[str]:
    """Import the budgets in a fresh interpreter with `scale` set, or unset."""
    env = {key: value for key, value in os.environ.items() if key != 'SOPHIOS_PROPERTY_SCALE'}
    if scale is not None:
        env['SOPHIOS_PROPERTY_SCALE'] = scale
    env['PYTHONPATH'] = os.pathsep.join((str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')))
    probe = ('from core import hermetic, compile_harness as c; '
             'print(hermetic.COVERAGE.max_examples, hermetic.ORACLE.max_examples, '
             'hermetic.PARTITION.max_examples, c.FAST.max_examples, c.COMPILED.max_examples)')
    return subprocess.run([sys.executable, '-c', probe], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, check=False)


@pytest.mark.fast
def test_the_property_scale_multiplies_every_budget_and_is_one_by_default() -> None:
    """An ordinary run draws exactly the examples it always did; the weekly
    lane's knob multiplies every shared budget; a knob that is not a positive
    integer stops the run rather than silently drawing nothing."""
    assert _budgets_under(None).stdout.split() == ['500', '100', '50', '200', '100']
    assert _budgets_under('3').stdout.split() == ['1500', '300', '150', '600', '300']
    refused = _budgets_under('0')
    assert refused.returncode != 0 and 'SOPHIOS_PROPERTY_SCALE' in refused.stderr
