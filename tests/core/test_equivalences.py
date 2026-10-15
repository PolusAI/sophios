"""Every rewrite `transformations` names preserves the meaning it claims.

One property, parameterised over `Transformation`, rather than a bespoke test
per rewrite. Partition independence and its nested case are named
instances of it — `split` at one level and `split` composed with itself — not
separate tests with their own idea of "the same workflow", which is how the
two regression tests already in this tree ended up with two.
"""
import copy
from collections import Counter
from typing import Final

import pytest
import yaml
from hypothesis import given, reject
from hypothesis import strategies as st

from sophios import post_compile
from sophios.ir.artifacts import CompilationArtifact
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent, flatten_model
from .hermetic import PARTITION, compile_hermetic, subworkflow_step
from .transformations import TRANSFORMATIONS, Transformation, split, split_transformations

#: The name both sides compile under. Identical on purpose: `identity` and
#: `text_roundtrip` claim IDENTICAL, and `equivalent(..., Strength.IDENTICAL)`
#: compares `steps[].id` literally (see `equivalence.py`'s own docstring —
#: IDENTICAL forgives nothing) — so compiling "before" and "after" under
#: different names would make every namespaced id disagree regardless of the
#: rewrite, and IDENTICAL would never hold for anything. UP_TO_RENAMING
#: ignores names either way, so the same choice costs those rewrites nothing.
_COMPILE_NAME: Final = 'oracle'


def _compile_flat(yml: Yaml) -> Yaml:
    """Compile hermetically under the shared name and flatten the result."""
    info = compile_hermetic(yml, _COMPILE_NAME)
    return flatten_model(info.artifact)


@pytest.mark.slow
@pytest.mark.parametrize('constant', [*TRANSFORMATIONS, None],
                         ids=[rewrite.name for rewrite in TRANSFORMATIONS] + ['split'])
@given(st.data())
@PARTITION
def test_a_meaning_preserving_rewrite_preserves_meaning(constant: Transformation | None,
                                                        data: st.DataObject) -> None:
    """Partition independence, its nested case, and every equivalence law, as one statement.

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
    """
    yml = data.draw(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
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


def _closed(artifact: CompilationArtifact) -> bool:
    """Whether every source in every workflow of the tree is a declared input or a step's output.

    A generated child can read a root input by a raw name it never declares. CWL
    rejects that nested document, and flattening leaves such a call nested rather
    than guess what scope the name was meant to be read in; the model dissolves it.
    """
    cwl = artifact.cwl
    if cwl['class'] == 'Workflow':
        known = set(cwl.get('inputs', {})) | {step['id'] for step in cwl['steps']}
        for step in cwl['steps']:
            for value in step.get('in', {}).values():
                source = value.get('source') if isinstance(value, dict) else value
                if isinstance(source, str) and source.partition('/')[0] not in known:
                    return False
    return all(_closed(child) for child in artifact.children)


@pytest.mark.slow
@given(st.data())
@PARTITION
def test_flattening_agrees_with_the_independent_model(data: st.DataObject) -> None:
    """`flatten_subworkflows` and `flatten_model` dissolve a partitioned workflow to the same DAG.

    The model is written separately, in this directory, and knows nothing of
    step ids; those are pinned by `test_inline_flags.py`. What both must agree
    on is the dataflow, and that the root's interface does not move.

    CANNOT GENERATE (declared): a call that carries `scatter` or `when`, which
    `strat.workflows()` cannot draw, so no draw leaves a call nested.
    """
    yml = data.draw(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
    nested = compile_hermetic(data.draw(split_transformations()).apply(yml), _COMPILE_NAME).artifact
    if not _closed(nested):
        reject()
    flat = post_compile.flatten_subworkflows(nested)
    found = equivalent(flatten_model(nested), flat.cwl, Strength.UP_TO_RENAMING)
    assert found is None, found
    assert all(isinstance(step['run'], str) for step in flat.cwl['steps'])
    assert all(child.cwl['class'] != 'Workflow' for child in flat.children)
    assert list(flat.cwl['inputs']) == list(nested.cwl['inputs'])
    assert list(flat.cwl['outputs']) == list(nested.cwl['outputs'])
    assert flat.job_inputs == nested.job_inputs


def _nesting_depth(document: Yaml) -> int:
    """How many subworkflow layers a document goes down.

    A step carrying a `subtree` is a subworkflow (`hermetic.subworkflow_step`
    builds the shape `read_ast_from_disk` produces), so depth 2 means a
    subworkflow that itself contains one — the shape the nested case is about.
    """
    return max((1 + _nesting_depth(step['subtree']) for step in document.get('steps', [])
                if isinstance(step, dict) and isinstance(step.get('subtree'), dict)),
               default=0)


@pytest.mark.fast
def test_split_reaches_a_document_that_is_already_nested() -> None:
    """The nested half of the property above, which nothing checked.

    The nested case is `split` composed with itself, and nothing
    composes anything: one rewrite is applied once. The nested case is reached
    when `workflows()` happens to draw a document that already contains a
    subworkflow step and `split` wraps it — true today, but a fact about
    `ast_strategies.documents()` rather than about anything in this file. If
    that generator's subworkflow branch narrowed, the nested case's coverage would go to
    zero with every test here still green.

    Which rewrite gets drawn is no longer a question — the property parametrises
    over all four — so this measures the one thing left to chance: how often the
    document underneath is deep enough for `split` to nest. Drawn exactly as the
    property draws, filter included.
    """
    depths: Counter[int] = Counter()

    @PARTITION
    @given(st.data())
    def _collect(data: st.DataObject) -> None:
        yml = data.draw(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
        rewrite = data.draw(split_transformations())
        depths[_nesting_depth(rewrite.apply(copy.deepcopy(yml)))] += 1

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `data`

    assert sum(count for depth, count in depths.items() if depth >= 2), (
        'no drawn split reached a document that already contained a subworkflow, so '
        f'nesting, rather than partitioning alone, went untested. Depths drawn: {dict(depths)}')


#: A document every constant transformation but `identity` must genuinely
#: change: a plain step plus an existing subworkflow, so
#: `rename_workflow`/`split`-style wrapping has more than one step to gather.
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
    (checked directly — `yaml.safe_dump` then `yaml.safe_load` round-trips this
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
    would be free and partition independence would assert nothing beyond IDENTICAL.

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
def test_split_uses_distinct_formal_and_actual_input_names() -> None:
    """The split/inline oracle must exercise parameter substitution.

    Identity bindings let an inliner delete the child interface without doing
    any substitution and still pass every partition-independence property.
    """
    yml: Yaml = {
        'inputs': {'actual': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'actual'}}],
    }
    transformed = split(((0,),)).apply(copy.deepcopy(yml))
    wrapper = transformed['steps'][0]
    formal_to_actual = wrapper['parentargs']['in']

    assert formal_to_actual
    assert all(formal != actual for formal, actual in formal_to_actual.items())
    formal = next(iter(formal_to_actual))
    assert wrapper['subtree']['steps'][0]['in']['name'] == formal


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
