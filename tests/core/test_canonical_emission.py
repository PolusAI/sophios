"""Canonical emission: no `set` iteration order reaches the compiled CWL.

`set` iteration is hash-seeded, and three sites once let that order out: the
emitted `requirements:` key order (eight seeds, eight orders), a step's `out:`
list order (six seeds, three orders), and the speculative-insertion list,
deduplicated through one behind a default-off flag. The first two sites were
`utils_cwl`'s `maybe_add_requirements` and `add_yamldict_keyval_out`, retired
with the legacy compiler; `ir/complete.py` now sorts `requirements:` and takes
a step's `out:` from the resolved interface. These properties therefore guard
against reintroduction rather than against a live escape, which is why the
adequacy companions below matter more, not less.

Stated as sortedness rather than as agreement across interpreters: the hash
seed is the symptom, and sortedness is checkable per example at 0.1s without a
second process. `Strength.IDENTICAL` is the one strength that compares key
order, and nothing could satisfy it twice for one input until this held.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given

from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.wic_types import Cwl, StepId, Tool, Tools, Yaml

from . import ast_strategies as strat
from .source_scan import REPO_ROOT
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic_cwl, subworkflow_step
from .synthetic_tools import STEMS, SYNTHETIC_NS, SYNTHETIC_TOOLS, outputs_of
from .test_equivalences import _hits_the_scalar_coercion_gap


#: A tool this suite owns, not one of `synthetic_tools.STEMS`: reaching the
#: `out:`-order site with more than one element needs a step with two or more
#: outputs, and every stock synthetic tool has at most one (verified below by
#: `test_the_out_order_property_needs_this_tool_to_mean_anything`). Three, to
#: match the multi-entry case the brief names, rather than the minimum of two.
#: Shared by the property's adequacy companion below and by the four-seed
#: regression in section 3, rather than defined twice.
_MULTI_TOOL_CWL: Final[Cwl] = desugar_into_canonical_normal_form({
    'cwlVersion': CWL_VERSION,
    'class': 'CommandLineTool',
    'baseCommand': 'true',
    'inputs': {'seed': {'type': 'int', 'inputBinding': {'position': 1}}},
    'outputs': {
        'first': {'type': 'File', 'outputBinding': {'glob': 'a'}},
        'second': {'type': 'File', 'outputBinding': {'glob': 'b'}},
        'third': {'type': 'File', 'outputBinding': {'glob': 'c'}},
    },
})

_MULTI_STEP_ID: Final = StepId('multi', SYNTHETIC_NS)

#: `SYNTHETIC_TOOLS` plus the one extra tool this file owns.
_TOOLS_WITH_MULTI: Final[Tools] = {**SYNTHETIC_TOOLS, _MULTI_STEP_ID: Tool('/synthetic/multi.cwl', _MULTI_TOOL_CWL)}

# --------------------------------------------------------------------------
# 1. The property: every set-derived collection in the output is sorted
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows().filter(lambda w: not _hits_the_scalar_coercion_gap(w)))
@ORACLE
def test_no_set_iteration_order_reaches_the_output(yml: Yaml) -> None:
    """Every collection in the emitted CWL that came from a set is canonical.

    Stated as sortedness rather than as agreement across two interpreters.
    The seed is the symptom; the defect is that a set's iteration order is
    load-bearing in the output at all, and sortedness catches that pointwise,
    on every example, without a second process.

    CANNOT DETECT (declared): nondeterminism that is not set-order — wall
    clock, uuid, filesystem iteration. `python_script` steps are excluded from
    the generator precisely because they are uuid-named. A set whose order is
    accidentally sorted on this run also escapes; the static scan below is the
    total guard, and this property is the end-to-end proof it is true.
    `_hits_the_scalar_coercion_gap` excludes documents the compiler refuses:
    a `!ii` literal that does not coerce to its argument's declared type is
    diagnosed as `wic020`, which is the compiler correctly rejecting an
    ill-typed document rather than a defect. This property quantifies over
    emitted output, so a document that produces none has nothing to say about
    it. Reused from `test_equivalences.py` rather than re-derived, so the two
    exclusions cannot drift apart.

    The `out:` assertion below is real but, over this generator alone, weak:
    see `test_the_out_order_property_needs_this_tool_to_mean_anything` and
    `test_out_order_is_canonical_for_a_multi_output_tool`, its companion.
    """
    compiled = compile_hermetic_cwl(yml, 'canon')

    requirements = list(compiled.get('requirements', {}))
    assert requirements == sorted(requirements), 'requirements key order is not canonical'

    for step in compiled['steps']:
        outs = step.get('out', [])
        assert outs == sorted(outs), f"{step['id']}: out: order is not canonical"


@pytest.mark.fast
def test_the_out_order_property_needs_this_tool_to_mean_anything() -> None:
    """Generator-adequacy check for the `out:` half of the property above.

    Confirmed by reverting the fix and re-running that property: it still
    passed, at 100 examples. Every stock synthetic tool
    (`synthetic_tools.STEMS`) has at most one output, so a step's `out:` never
    holds more than one element for
    any workflow that property's generator (`strat.workflows()`) can produce
    — a one-element (or empty) list is trivially sorted regardless of `list`
    vs. `sorted`, so that half of the assertion is guaranteed true by the
    tool registry, not by the fix. `test_out_order_is_canonical_for_a_multi_
    output_tool` is what actually exercises the site, using `_MULTI_TOOL_CWL`.
    """
    assert all(len(outputs_of(stem)) <= 1 for stem in STEMS), (
        'a stock synthetic tool now has 2+ outputs; the property above no '
        'longer needs this adequacy companion for its out: half')


@pytest.mark.fast
def test_out_order_is_canonical_for_a_multi_output_tool() -> None:
    """The `out:`-order site, actually exercised: a step whose tool has three
    outputs and no explicit `out:` of its own, so the emitted `out:` carries
    all three tool output names — see the module docstring's second finding."""
    yml = {'steps': [{'id': 'multi', 'in': {'seed': {'wic_inline_input': '1'}}}]}
    compiled = compile_hermetic_cwl(yml, 'canon', tools=_TOOLS_WITH_MULTI)
    for step in compiled['steps']:
        outs = step.get('out', [])
        assert outs == sorted(outs), f"{step['id']}: out: order is not canonical"
        if step['id'].endswith('multi'):
            # The closed shape, not just `== sorted(outs)`: the exact list the
            # canonical order must produce, so the assertion cannot be
            # satisfied by whatever the code happened to emit.
            assert outs == ['first', 'second', 'third'], 'out: is not the canonical order for the 3-output tool'


@pytest.mark.fast
def test_step_id_sorts_lexicographically() -> None:
    """`compiler.py`'s `sorted(set(insertions))` assumes `StepId` — a
    `(stem, plugin_ns)` NamedTuple — has a total order. It does, for free,
    because `NamedTuple` inherits tuple comparison and both fields are plain
    strings; asserted here rather than assumed, since a fix built on an
    ordering that turned out partial would be silently wrong."""
    ids = [StepId('b', 'x'), StepId('a', 'y'), StepId('a', 'x'), StepId('b', 'a')]
    assert sorted(ids) == [StepId('a', 'x'), StepId('a', 'y'), StepId('b', 'a'), StepId('b', 'x')]


# --------------------------------------------------------------------------
# 3. One hand-built workflow, compiled under four hash seeds
# --------------------------------------------------------------------------

#: Hand-built to reach both sites a hermetic compilation can: a subworkflow
#: (`sub.wic`) sets `SubworkflowFeatureRequirement`; `mk_text`'s `scatter:`
#: sets `ScatterFeatureRequirement`; `multi`'s raw `valueFrom` sets both
#: `StepInputExpressionRequirement` and `InlineJavascriptRequirement` —
#: four distinct requirements from three constructs. `multi`'s three outputs
#: give the `out:` site a multi-entry list to reorder.
#:
#: `compiler.py`'s `insertions` site is deliberately not reached here either:
#: every input below is fully provided, so inference never runs, matching the
#: module docstring's second finding — no hermetic compilation reaches it
#: without deliberately naming `insert_steps_automatically_*` tools, which
#: would be a second, unrelated mechanism bolted onto this one example. The
#: static scan above is that site's guard.
_FOUR_SEED_WORKFLOW: Final[Yaml] = {
    'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b.txt'}}, 'scatter': ['name']},
        {'id': 'multi', 'in': {'seed': {'wic_inline_input': '1'}}},
        subworkflow_step('sub.wic', {'steps': [
            {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'c.txt'}}},
        ]}),
    ],
}


def _compile_four_seed_workflow() -> Yaml:
    """Compile `_FOUR_SEED_WORKFLOW` and return the emitted CWL.

    Module-level, not a closure: the subprocess this file's regression test
    spawns imports this module and calls this function by name.
    """
    return compile_hermetic_cwl(_FOUR_SEED_WORKFLOW, 'canon', tools=_TOOLS_WITH_MULTI)


def _four_seed_compilations(seeds: tuple[int, ...]) -> list[Yaml]:
    """Compile `_FOUR_SEED_WORKFLOW` once per seed, each in its own fresh
    interpreter, and return the parsed CWL in seed order.

    A fresh process per seed, not a shared one with `PYTHONHASHSEED` patched
    in between: the compiler mutates the `tools` dict it is handed and several
    structures threaded through the recursion, so seeded compilations sharing
    an interpreter could agree — or disagree — for a reason that has nothing
    to do with `PYTHONHASHSEED`. Four processes cost about 3s; a shared
    interpreter would be faster and would not be evidence of anything.

    Transported as JSON, not compared as Python objects in-process: `dict`
    equality in Python ignores key order, which is exactly the thing under
    test, so the parent process must see the bytes each child actually
    produced. `json.loads` preserves the key order it reads, same as
    `yaml.safe_load`, so the round trip does not itself erase the ordering.
    """
    python_path = os.pathsep.join((str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')))
    outputs = []
    for seed in seeds:
        env = {**os.environ, 'HOME': str(Path(tempfile.mkdtemp())),
               'PYTHONPATH': python_path, 'PYTHONHASHSEED': str(seed)}
        result = subprocess.run(
            [sys.executable, '-c',
             'import json, core.test_canonical_emission as m; '
             'print(json.dumps(m._compile_four_seed_workflow()))'],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f'seed {seed} failed:\n{result.stdout}{result.stderr}'
        outputs.append(json.loads(result.stdout))
    return outputs


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.serial
def test_one_workflow_compiles_identically_under_four_hash_seeds() -> None:
    """The literal claim the property register used to make, on one input.

    A fresh interpreter per seed — see `_four_seed_compilations` for why a
    worker pool would be cheaper and worse. `equivalent(..., Strength.
    IDENTICAL)` is used rather than `==`: plain dict equality does not see a
    key-order difference, which is precisely what this regression exists to
    catch (`test_only_identical_compares_key_order` in test_equivalence.py).

    The workflow is hand-built to reach two of the three known sites: a
    subworkflow, a scattered step, and a `valueFrom` give four distinct
    requirements, and a three-output tool gives a multi-entry `out:`. See
    `_FOUR_SEED_WORKFLOW`'s docstring for why the third (`insertions`) is not
    attempted here.

    Measured before the fix: four seeds, four distinct outputs. After it, one.
    """
    outputs = _four_seed_compilations((0, 1, 2, 3))
    for seed, later in zip((1, 2, 3), outputs[1:]):
        found = equivalent(outputs[0], later, Strength.IDENTICAL)
        assert found is None, f'seed 0 vs seed {seed}: {found}'
