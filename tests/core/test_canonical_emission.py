"""Canonical emission: no `set` iteration order reaches the compiled CWL.

Delivers T2.4's first half, replacing "output identical across
`PYTHONHASHSEED`" — expensive, and vacuous under a normalising equivalence —
with a claim that is cheaper, broader, and reaches code no generator can.

CE-11 (confirmed, measured at `b00d044`). `set` iteration is hash-seeded, and
three sites in the compile path let that order reach the emitted CWL. Two need
no flag — any workflow with a subworkflow, a scattered step, `when`, or
`valueFrom` takes this path by default:

  * `utils_cwl.py` (`maybe_add_requirements`) — `{r: {} for r in set(reqs)}`
    sets the emitted `requirements:` key order. Eight `PYTHONHASHSEED` values
    produced eight different orders.
  * `utils_cwl.py` (`add_yamldict_keyval_out`) — `list(set(new_strs))` sets a
    step's `out:` list order. Six seeds produced three different orders.

The third needs `--insert_steps_automatically` (default off, nothing in-repo
enables it) *and* two or more tools named `insert_steps_automatically_*`, of
which exactly two exist anywhere (both in `mm-workflows`, both format
converters):

  * `compiler.py` (`compile_workflow_once`) — `list(set(insertions))`, the
    site the design names. No hermetic generator reaches it without
    deliberately naming synthetic tools to match the whitelist.

All three were found by reading the source, in about two seconds. A property
that varied `PYTHONHASHSEED` instead would have found two of the three and
cost 36 seconds of CI. Worse: under a *normalising* equivalence relation
(`.equivalence.Strength.UP_TO_RENAMING`/`UP_TO_EMBEDDING`), `requirements` key
order is exactly the kind of thing that gets normalised away, so a
seed-varying property would have been vacuously true.
`.equivalence.Strength.IDENTICAL` is the one strength that compares key
order — deliberately, see its docstring — and before this fix nothing could
satisfy it twice in a row for the same input.

So the claim is restated as three deliverables:

  1. `test_no_set_iteration_order_reaches_the_output` — a property, over
     generated workflows, that every collection in the emitted CWL that came
     from a `set` is in canonical (sorted) order. Stated as sortedness, not as
     agreement across interpreters: the hash seed is the symptom, and
     sortedness is checkable per example, at 0.1s, without a second process.
  2. `test_no_set_reaches_an_ordered_structure` — a total static scan: outside
     an explicit allowlist, a `set` may not be iterated into an ordered
     structure anywhere in `src/sophios`. This is what reaches
     `compiler.py`'s `insertions`, which the property above cannot.
  3. `test_one_workflow_compiles_identically_under_four_hash_seeds` — one
     hand-built workflow, compiled in four fresh interpreters: the literal
     claim the property register used to make, kept as a separate example
     because the property above deliberately does not assert it end-to-end
     (see its own CANNOT DETECT paragraph).
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given

import sophios.cli
import sophios.compiler
from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import Cwl, StepId, Tool, Tools, Yaml, YamlTree

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic_cwl, subworkflow_step
from .synthetic_tools import STEMS, SYNTHETIC_NS, SYNTHETIC_TOOLS, outputs_of
from .test_equivalences import _hits_the_scalar_coercion_gap

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO_ROOT / 'src' / 'sophios'

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
    `_hits_the_scalar_coercion_gap` excludes `ast_strategies.py`'s own
    documented PENDING FINDING (a `!ii` literal that does not coerce to its
    argument's type crashes compilation with a bare `ValueError`) — Task 2's
    finding, not this property's, and reused from `test_equivalences.py`
    rather than re-derived so the two exclusions cannot drift apart.

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
    (`synthetic_tools.STEMS`) has at most one output, so
    `add_yamldict_keyval_out`'s `set` never holds more than one element for
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
    outputs and no explicit `out:` of its own, so `add_yamldict_keyval_out`
    merges `[]` with all three tool output names — see the module docstring's
    second finding."""
    yml = {'steps': [{'id': 'multi', 'in': {'seed': {'wic_inline_input': '1'}}}]}
    compiled = compile_hermetic_cwl(yml, 'canon', tools=_TOOLS_WITH_MULTI)
    for step in compiled['steps']:
        outs = step.get('out', [])
        assert outs == sorted(outs), f"{step['id']}: out: order is not canonical"
        if step['id'].endswith('multi'):
            assert len(outs) == 3, 'the multi-output tool did not reach add_yamldict_keyval_out with 3 entries'


# --------------------------------------------------------------------------
# 2. The static scan: no set may reach an ordered structure, anywhere
# --------------------------------------------------------------------------


def _is_set_call(node: ast.expr) -> bool:
    """True for a direct `set(...)` call. `s = set(x)` followed by iterating
    `s` on another line is indirection this predicate cannot see — declared
    below, and out of reach of a static, single-pass scan."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'set'


def _set_iteration_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Every place a `set(...)` call is iterated into an ordered structure.

    Total rule, not a shape catalogue — the lesson from #384's review round
    three, where a scan that enumerated the shapes it knew about missed the
    two the PR had just fixed (a `.get()`/`.setdefault()` default, and a
    keyword-only parameter default — neither is an assignment node). Applied
    here the same way `test_cwl_version.py`'s literal scan applies it: walk
    every node with `ast.walk` and match the offending node's own shape,
    never the shape of whatever statement happens to contain it, so a
    `list(set(x))` inside a `return`, a dict value, or a function argument is
    caught exactly like one inside a plain assignment.

    Bans `list(set(...))`, `tuple(set(...))`, `for ... in set(...)`, and any
    comprehension (list/set/dict/generator — all four share the single
    `ast.comprehension` node checked below) iterating a bare `set(...)`.
    `sorted(set(...))` is the fix and the only permitted form: `sorted` is not
    `list`/`tuple`, so it is never matched by the first branch.

    CANNOT DETECT (declared): indirection. `s = set(xs)` followed by `list(s)`
    on another line is invisible here, as is a set arriving through a
    parameter. `test_no_set_iteration_order_reaches_the_output` is the
    end-to-end guard for those.
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('list', 'tuple'):
            if any(_is_set_call(arg) for arg in node.args):
                violations.append((node.lineno, f'{node.func.id}(set(...))'))
        elif isinstance(node, ast.For) and _is_set_call(node.iter):
            violations.append((node.lineno, 'for ... in set(...)'))
        elif isinstance(node, ast.comprehension) and _is_set_call(node.iter):
            # `comprehension` carries no line info of its own (it is neither
            # an `expr` nor a `stmt`); its `.iter` is the `set(...)` call
            # itself, which does.
            violations.append((node.iter.lineno, 'comprehension over set(...)'))
    return violations


#: Pre-existing set-into-ordered-structure calls outside Task 5's three named
#: sites (`utils_cwl.py`'s two and `compiler.py`'s one, all fixed above).
#: Line-pinned rather than a whole-file exemption, so a line moving *or* a new
#: violation landing nearby is caught, not silently covered — see
#: `test_the_allowlist_still_matches_a_real_violation`.
#:
#: Two are the same defect class as the three this task fixes, discovered by
#: this scan but outside its named sites, so left as named, visible
#: exceptions rather than fixed here or quietly dodged by narrowing the scan:
#:
#:   * `plugins.cwl_update_outputs_optional`'s `successCodes` list is reached
#:     only through `sophios.plugins` — FORBIDDEN to the oracle,
#:     `tests/core/test_hermeticity.py`.
#:   * `compile_workflow`'s `vars_workflow_output_internal = list(\n
#:     set(vars_workflow_output_internal))` is a `grep`-shaped scan's blind
#:     spot in the flesh: the call is real but split across two lines, so
#:     `grep -n 'list(set('` (used to survey this file before the AST scan
#:     existed) missed it. Every other use of this variable is membership
#:     testing (`x in vars_workflow_output_internal`), never an emitted
#:     order, but the scan does not know that and is not asked to.
#:
#: Neither is one of the three sites this task's finding names
#: (`utils_cwl.py`'s two, `compiler.py`'s `insertions`, all fixed above), and
#: fixing either here would be a change the finding does not name.
ALLOWED_SET_ITERATIONS: Final[dict[Path, frozenset[int]]] = {
    SRC / 'plugins.py': frozenset({149}),
    SRC / 'compiler.py': frozenset({345}),
}


def _python_files() -> list[Path]:
    """Every Python file under `src/sophios`."""
    return sorted(SRC.rglob('*.py'))


@pytest.mark.fast
def test_the_scan_sees_the_repo() -> None:
    """Zero parametrized cases is a green test that enforces nothing."""
    assert _python_files(), 'no files found under src/sophios; the scan is vacuous'


@pytest.mark.fast
@pytest.mark.parametrize('path', _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_set_reaches_an_ordered_structure(path: Path) -> None:
    """Outside `ALLOWED_SET_ITERATIONS`, no set may reach an ordered structure.

    This is what the property above cannot be: `compiler.py`'s `insertions`
    needs `--insert_steps_automatically` and a two-tool whitelist match that
    no hermetic generator reaches, so nothing dynamic ever executes that line
    during the oracle suite. This scan covers it anyway, because it does not
    run the code — it reads it.
    """
    violations = _set_iteration_violations(ast.parse(path.read_text(encoding='utf-8'), str(path)))
    allowed = ALLOWED_SET_ITERATIONS.get(path, frozenset())
    offenders = [(line, shape) for line, shape in violations if line not in allowed]
    assert not offenders, (
        f'{path.relative_to(REPO_ROOT)} lets a set reach an ordered structure: '
        f'{[f"line {line}: {shape}" for line, shape in offenders]}; use sorted(set(...)) instead'
    )


@pytest.mark.fast
def test_the_allowlist_still_matches_a_real_violation() -> None:
    """The companion for `ALLOWED_SET_ITERATIONS`: every line it excuses must
    still be a real violation, or the exemption has gone stale and is hiding
    nothing rather than something named."""
    for path, lines in ALLOWED_SET_ITERATIONS.items():
        found = {line for line, _ in _set_iteration_violations(
            ast.parse(path.read_text(encoding='utf-8'), str(path)))}
        missing = lines - found
        assert not missing, f'{path}: allowlisted line(s) {missing} no longer contain a violation'


@pytest.mark.fast
def test_the_scan_can_actually_fail() -> None:
    """Every banned shape the scan claims to cover, fed in directly — not just
    the shapes visible at module level, per the #384 lesson above."""
    shapes = [
        'xs = list(set(reqs))',
        'xs = tuple(set(reqs))',
        'for r in set(reqs):\n    pass',
        '[r for r in set(reqs)]',
        '{r: 1 for r in set(reqs)}',
        '{r for r in set(reqs)}',
        '(r for r in set(reqs))',
        'def f():\n    return list(set(reqs))',
        "d = {'r': list(set(reqs))}",
    ]
    for source in shapes:
        assert _set_iteration_violations(ast.parse(source)), f'scan missed: {source}'


@pytest.mark.fast
def test_the_scan_ignores_the_sanctioned_form() -> None:
    """`sorted(set(x))` is the fix, not a finding."""
    assert not _set_iteration_violations(ast.parse('xs = sorted(set(reqs))'))


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
        {'id': 'multi', 'in': {'seed': {'valueFrom': '$(1)'}}},
        subworkflow_step('sub.wic', {'steps': [
            {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'c.txt'}}},
        ]}),
    ],
}


def _compile_four_seed_workflow() -> Yaml:
    """Compile `_FOUR_SEED_WORKFLOW` and return the emitted CWL.

    Module-level, not a closure: the subprocess this file's regression test
    spawns imports this module and calls this function by name.

    Not `compile_hermetic`: that helper does not expose `allow_raw_cwl`, and
    a step's `in:` value is only left un-evaluated (so `multi`'s raw
    `valueFrom` survives to the emitted step) with that flag set
    (`src/sophios/compiler.py`'s `case _:` fallback in the `in:` match).
    Otherwise identical to `compile_hermetic`'s own fourteen-argument call.
    """
    compiler_options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    compiler_options['allow_raw_cwl'] = True
    graph = get_graph_reps('canon')
    info = sophios.compiler.compile_workflow(
        YamlTree(StepId('canon', SYNTHETIC_NS), _FOUR_SEED_WORKFLOW),
        compiler_options, graph_settings, tag_paths,
        [], [graph], {}, {}, {}, {},
        _TOOLS_WITH_MULTI, True, relative_run_path=True, testing=True)
    compiled: Yaml = info.rose.data.compiled_cwl
    return compiled


def _four_seed_compilations(seeds: tuple[int, ...]) -> list[Yaml]:
    """Compile `_FOUR_SEED_WORKFLOW` once per seed, each in its own fresh
    interpreter, and return the parsed CWL in seed order.

    A fresh process per seed, not a shared one with `PYTHONHASHSEED` patched
    in between: the compiler mutates a module global
    (`compiler.inference_rules`), the `tools` dict it is handed, and several
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
    python_path = f'{REPO_ROOT / "src"}:{REPO_ROOT / "tests"}'
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

    The workflow is hand-built to reach two of the three CE-11 sites: a
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
