"""P24c: every rewrite `transformations` names preserves the meaning it claims.

One property, parameterised over `Transformation`, rather than a bespoke test
per rewrite. P28 (partition independence) and P29 (its nested case) are named
instances of it — `split` at one level and `split` composed with itself — not
separate tests with their own idea of "the same workflow", which is how the
two regression tests already in this tree ended up with two.
"""
import copy
from collections import Counter
from typing import Any, Final

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from sophios.wic_types import NodeData, RoseTree, Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import PARTITION, compile_hermetic, subworkflow_step
from .synthetic_tools import STEMS, inputs_of
from .transformations import TRANSFORMATIONS, Transformation, split, split_transformations

#: The name both sides compile under. Identical on purpose: `identity` and
#: `text_roundtrip` claim IDENTICAL, and `equivalent(..., Strength.IDENTICAL)`
#: compares `steps[].id` literally (see `equivalence.py`'s own docstring —
#: IDENTICAL forgives nothing) — so compiling "before" and "after" under
#: different names would make every namespaced id disagree regardless of the
#: rewrite, and IDENTICAL would never hold for anything. UP_TO_RENAMING
#: ignores names either way, so the same choice costs those rewrites nothing.
_COMPILE_NAME: Final = 'oracle'


def _source_of(value: Any) -> str | None:
    """The `source` one `in:` binding names, in whichever of the two surface
    forms the compiler emitted (`{name: source}` or `{name: {source: ...}}`)."""
    source = value.get('source') if isinstance(value, dict) else value
    return source if isinstance(source, str) else None


def _set_source(bindings: Yaml, name: str, source: str) -> None:
    """Rewrites one binding's source in place, keeping its own surface form."""
    value = bindings[name]
    if isinstance(value, dict):
        value['source'] = source
    else:
        bindings[name] = source


def _flatten(rose: RoseTree) -> Yaml:
    """The compiled document with every subworkflow step inlined, so a split
    and an unsplit compilation of the same steps can be compared directly.

    `sophios.inlineing.inline_subworkflow_cwl` already does this job — for a
    `steps:` represented as a dict keyed by step id. `compile_workflow` emits
    `steps:` as a *list* (`type(compiled_cwl['steps']) is list`, checked
    directly against this oracle's own output), so that function's
    `list(steps.keys())` raises `AttributeError` on exactly the shape this
    task's compiler produces, and nothing in the tree calls it. That is a
    finding about `src/sophios/inlineing.py`, not something this task's own
    files can work around by patching — `src/` is out of scope here — so this
    is a from-scratch replacement, scoped to what `equivalent()` at
    UP_TO_RENAMING actually forgives: it needs the DAG's topology and every
    step's body right, and it does not need `run:` paths, `steps[].id`, or
    workflow-level port *names* right at all, because those are exactly what
    that strength already ignores (`equivalence.py`).

    Recognising a subworkflow step by its own sub-tree's `class` (`Workflow`,
    not `CommandLineTool`) rather than by an `id` suffix: `rose.sub_trees` has
    one entry per step regardless of kind (verified directly — a flat,
    two-`CommandLineTool` compile still has two leaf sub-trees, one per step),
    so the id is not what marks a step as needing to be inlined here; its own
    compiled class is.
    """
    node_data: NodeData = rose.data
    document: Yaml = copy.deepcopy(node_data.compiled_cwl)
    steps: list[Yaml] = document.get('steps', [])
    sub_trees: list[RoseTree] = rose.sub_trees
    flat_steps: list[Yaml] = []
    #: `{wrapper_id}/{wrapper_out_port} -> real producer's own source string`,
    #: for every subworkflow step this pass inlines away.
    redirects: dict[str, str] = {}
    #: Requirements the inlined subworkflows themselves declared (their own
    #: `SubworkflowFeatureRequirement` already stripped, recursively, by the
    #: nested `_flatten` call below) — e.g. `ScatterFeatureRequirement`, for a
    #: `scatter:` that lived inside the subworkflow and is now a step directly
    #: in *this* document. A CWL document declares only the features its own
    #: `steps:` uses, so once a step moves in here, whatever requirement it
    #: needed moves with it.
    inherited_requirements: dict[str, Yaml] = {}
    inlined_any = False

    for step, sub in zip(steps, sub_trees):
        sub_data: NodeData = sub.data
        if sub_data.compiled_cwl.get('class') != 'Workflow':
            flat_steps.append(step)
            continue
        inlined_any = True
        inner = _flatten(sub)
        inner_requirements = inner.get('requirements')
        if isinstance(inner_requirements, dict):
            inherited_requirements.update(inner_requirements)
        # The wrapper's own `in:` maps the subworkflow's formal parameter
        # names (its inner workflow-level `inputs:` keys) to the sources that
        # actually feed them at *this* level.
        formal_to_actual = {name: _source_of(value) for name, value in step.get('in', {}).items()}
        for inner_step in inner.get('steps', []):
            for name, value in inner_step.get('in', {}).items():
                source = _source_of(value)
                if source is not None and source in formal_to_actual:
                    actual = formal_to_actual[source]
                    if actual is not None:
                        _set_source(inner_step['in'], name, actual)
            flat_steps.append(inner_step)
        wrapper_id = step['id']
        # The subworkflow's own `outputs:` map its output port names to
        # `outputSource: '<inner step>/<port>'`; anything outside referred to
        # `{wrapper_id}/{that port name}`, so redirect that reference straight
        # to the real producer, which is now itself one of `flat_steps`.
        for out_name, out_value in inner.get('outputs', {}).items():
            redirects[f'{wrapper_id}/{out_name}'] = out_value['outputSource']

    for flat_step in flat_steps:
        for name, value in flat_step.get('in', {}).items():
            source = _source_of(value)
            if source is not None and source in redirects:
                _set_source(flat_step['in'], name, redirects[source])
    document['steps'] = flat_steps

    for out_value in document.get('outputs', {}).values():
        out_source = out_value.get('outputSource')
        if isinstance(out_source, str) and out_source in redirects:
            out_value['outputSource'] = redirects[out_source]

    if inlined_any:
        # A flattened document contains no subworkflow step, so the
        # requirement that declares one no longer applies — left in place, it
        # would make `_requirement_names` (equivalence.py) diverge on every
        # split for a reason that has nothing to do with the workflow's
        # meaning. What each inlined subworkflow's *own* requirements needed
        # does still apply, now that their steps are directly in `steps:`
        # here, so those are merged in rather than dropped with the rest.
        requirements = document.get('requirements')
        merged = dict(requirements) if isinstance(requirements, dict) else {}
        merged.pop('SubworkflowFeatureRequirement', None)
        merged.update(inherited_requirements)
        document['requirements'] = merged

    return document


def _compile_flat(yml: Yaml) -> Yaml:
    """Compile hermetically under the shared name and flatten the result."""
    info = compile_hermetic(yml, _COMPILE_NAME)
    return _flatten(info.rose)


def _hits_the_scalar_coercion_gap(document: Yaml) -> bool:
    """Whether compiling `document` would hit `ast_strategies.py`'s own
    documented PENDING FINDING, rather than this property's own claim.

    `populate_scalar_val` (src/sophios/compiler.py:1170,1173) calls
    `int(value)`/`float(value)` on a `!ii` literal with no `try`/`except`, so a
    literal that does not coerce to the bound argument's type crashes
    compilation with a bare `ValueError` instead of a diagnostic.
    `ast_strategies.py` documents this and deliberately does *not* filter it
    out of `workflows()` (unlike CE-13, it is not a single AST-shape
    predicate). Left unfiltered here, every draw that hits it would abort
    *both* compiles this property needs before `equivalent()` is ever called —
    smothering the property this task is actually responsible for under a
    finding Task 2 already made and recorded. Excluded the same way
    `compilable_documents()` excludes CE-13: narrowly, by the exact predicate
    that names the gap, not by weakening what `workflows()` generates.
    """
    for step in document.get('steps', []):
        if not isinstance(step, dict):
            continue
        stem = step.get('id')
        if stem not in STEMS:
            continue  # a subworkflow step, or something else this check does not model
        declared = inputs_of(stem)
        for name, value in step.get('in', {}).items():
            if not (isinstance(value, dict) and 'wic_inline_input' in value):
                continue
            arg_type = declared.get(name, {}).get('type')
            literal = value['wic_inline_input']
            if arg_type not in ('int', 'float'):
                continue
            coerce = int if arg_type == 'int' else float
            try:
                coerce(literal)
            except (TypeError, ValueError):
                return True
    return False


@pytest.mark.slow
@pytest.mark.parametrize('constant', [*TRANSFORMATIONS, None],
                         ids=[rewrite.name for rewrite in TRANSFORMATIONS] + ['split'])
@given(st.data())
@PARTITION
def test_a_meaning_preserving_rewrite_preserves_meaning(constant: Transformation | None,
                                                        data: st.DataObject) -> None:
    """P28, P29, and every equivalence law in Spec 2, as one statement.

    Parameterised by transformation, so a failure names which rewrite broke
    rather than which file it was written in — and so adding a rewrite is one
    entry in a table instead of a new test module with a new idea of equality.

    Parametrised, not drawn. Drawing the rewrite alongside the document made
    the property's strength per rewrite a matter of Hypothesis's weighting:
    `identity` took about half the fifty examples and the thinnest real rewrite
    could reach zero (see `split_transformations`). Each kind now gets the
    whole budget, and `split`'s partitioning is what is drawn inside it.

    CANNOT GENERATE (declared): rewrites that change step order, which this
    language's inference is not invariant under (see `transformations.py`'s
    module docstring); `!cwl` and `python_script` steps, which `workflows()`
    itself cannot generate (`ast_strategies.py`); partitionings that are not
    contiguous, which the language cannot express (`ast_strategies.partitionings`).
    A transformation only ever regroups or re-derives a document's existing
    steps, so a document that avoids `ast_strategies.py`'s own documented
    scalar-coercion gap before any rewrite is applied still avoids it after —
    see `_hits_the_scalar_coercion_gap`.
    """
    yml = data.draw(strat.workflows().filter(
        lambda w: len(w['steps']) >= 2 and not _hits_the_scalar_coercion_gap(w)))
    rewrite = constant if constant is not None else data.draw(split_transformations())
    transformed = rewrite.apply(copy.deepcopy(yml))

    before = _compile_flat(yml)
    after = _compile_flat(transformed)

    found = equivalent(before, after, rewrite.preserves)
    assert found is None, (
        f'{rewrite.name} changed the meaning of the workflow.\n'
        f'It claims to preserve {rewrite.preserves.name} because: {rewrite.rationale}\n\n'
        f'{found}\n\n'
        f'--- before ---\n{yaml.safe_dump(yml, sort_keys=False)}\n'
        f'--- after ---\n{yaml.safe_dump(transformed, sort_keys=False)}')


def _nesting_depth(document: Yaml) -> int:
    """How many subworkflow layers a document goes down.

    A step carrying a `subtree` is a subworkflow (`hermetic.subworkflow_step`
    builds the shape `read_ast_from_disk` produces), so depth 2 means a
    subworkflow that itself contains one — the shape P29 is about.
    """
    return max((1 + _nesting_depth(step['subtree']) for step in document.get('steps', [])
                if isinstance(step, dict) and isinstance(step.get('subtree'), dict)),
               default=0)


@pytest.mark.fast
def test_split_reaches_a_document_that_is_already_nested() -> None:
    """P29's half of the property above, which nothing checked.

    The module docstring calls P29 "`split` composed with itself", and nothing
    composes anything: one rewrite is applied once. The nested case is reached
    when `workflows()` happens to draw a document that already contains a
    subworkflow step and `split` wraps it — true today, but a fact about
    `ast_strategies.documents()` rather than about anything in this file. If
    that generator's subworkflow branch narrowed, P29's coverage would go to
    zero with every test here still green.

    Which rewrite gets drawn is no longer a question — the property parametrises
    over all five — so this measures the one thing left to chance: how often the
    document underneath is deep enough for `split` to nest. Drawn exactly as the
    property draws, filter included.
    """
    depths: Counter[int] = Counter()

    @PARTITION
    @given(st.data())
    def _collect(data: st.DataObject) -> None:
        yml = data.draw(strat.workflows().filter(
            lambda w: len(w['steps']) >= 2 and not _hits_the_scalar_coercion_gap(w)))
        rewrite = data.draw(split_transformations())
        depths[_nesting_depth(rewrite.apply(copy.deepcopy(yml)))] += 1

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `data`

    assert sum(count for depth, count in depths.items() if depth >= 2), (
        'no drawn split reached a document that already contained a subworkflow, so P29 — '
        f'nesting, not just partitioning — went untested. Depths drawn: {dict(depths)}')


#: A document every constant transformation but `identity` must genuinely
#: change: a plain step plus an existing subworkflow, so `inline_all` has
#: something to inline (a document with no subworkflow gives it nothing to
#: do) and `rename_workflow`/`split`-style wrapping has more than one step to
#: gather.
_NOOP_GUARD_FIXTURE: Final[Yaml] = {
    'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        subworkflow_step('sub0.wic', {'steps': [
            {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b.txt'}}},
        ]}),
    ],
}


@pytest.mark.fast
@pytest.mark.parametrize('rewrite', TRANSFORMATIONS, ids=lambda t: t.name)
def test_every_transformation_actually_transforms(rewrite: Transformation) -> None:
    """The tautology check, per rewrite.

    A transformation that returned its input unchanged would satisfy the
    equivalence property for free, and the property would then be a statement
    about `identity` wearing four other names. `identity` is exempt and says
    so: it is the one rewrite whose whole point is changing nothing, and what
    it tests is that *the compiler* does not.

    IDENTICAL-claiming rewrites need a different check than the rest, and that
    is not a loophole: `text_roundtrip` is, by construction, value-preserving
    (checked directly — `yaml.safe_dump` then `wic_loader` round-trips this
    fixture to something `==` its input), so `apply(yml) != yml` would fail
    for a *correct* implementation just as loudly as for a broken one, and
    could never tell them apart. What a no-op could fake there is skipping the
    round trip entirely and handing back the very object it was given, so
    that is what is checked: a fresh, value-equal object, not a different one.

    `apply` is handed `yml` itself and the comparison is against a pristine
    `expected`, which is the whole of what makes that check real. An earlier
    version passed `apply` a fresh `copy.deepcopy(yml)` and then asserted
    `result is not yml` — true for *every* implementation, `lambda w: w`
    included, because the identity being compared was the copy's and not the
    one `apply` received. Replacing `_text_roundtrip`'s body with
    `return document` left this test green. Comparing against `expected`
    rather than `yml` matters for the same reason on the other branch: a
    rewrite that mutated in place would otherwise be compared against itself.
    """
    yml = copy.deepcopy(_NOOP_GUARD_FIXTURE)
    if rewrite.name == 'identity':
        pytest.skip('identity changes the input by definition; see the module docstring')
    expected = copy.deepcopy(yml)
    result = rewrite.apply(yml)
    if rewrite.preserves is Strength.IDENTICAL:
        assert result is not yml, f'{rewrite.name} returned its input unchanged, doing no work'
        assert result == expected, f'{rewrite.name} claims IDENTICAL but changed the value'
    else:
        assert result != expected, f'{rewrite.name} is a no-op'


@pytest.mark.fast
def test_splitting_really_renames() -> None:
    """`split` claims only UP_TO_RENAMING. If it renamed nothing, that claim
    would be free and P28 would assert nothing beyond IDENTICAL.

    Compiles both sides and checks both halves of the claim directly: IDENTICAL
    must *not* hold (there is really something for a renaming to forgive) and
    UP_TO_RENAMING must (the DAG that remains, once names are ignored, is the
    same DAG).
    """
    yml: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b.txt'}}},
    ]}
    rewrite = split(((0,), (1,)))
    before = _compile_flat(yml)
    after = _compile_flat(rewrite.apply(copy.deepcopy(yml)))

    assert equivalent(before, after, Strength.IDENTICAL) is not None, (
        'split produced a byte-identical compilation; UP_TO_RENAMING would be a vacuous claim')
    found = equivalent(before, after, Strength.UP_TO_RENAMING)
    assert found is None, found


@pytest.mark.fast
def test_the_property_fails_for_a_rewrite_that_changes_meaning() -> None:
    """A deliberately dishonest transformation — one that drops a step while
    claiming to preserve UP_TO_RENAMING — must be caught. Without this, a
    mis-wired property (comparing a document with itself, say) is green."""
    liar = Transformation(
        name='drops_a_step', preserves=Strength.UP_TO_RENAMING,
        apply=lambda w: {**w, 'steps': w['steps'][:-1]},
        rationale='none; this rewrite is wrong on purpose')
    yml: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b.txt'}}},
    ]}
    before = _compile_flat(yml)
    after = _compile_flat(liar.apply(copy.deepcopy(yml)))
    assert equivalent(before, after, liar.preserves) is not None
