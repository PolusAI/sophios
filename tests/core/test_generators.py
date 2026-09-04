"""P26 and P27: the generator is adequate, and its failures are usable.

A property is only as strong as its generator. Spec 1's review cycle traced
every High-severity finding to a blind spot: totality quantified over
`st.text()`, which essentially never forms valid YAML with an alias; a
document generator that produced only mapping-form `in:`-only documents made
outputs, sequence steps and nested sidecars structurally invisible to every
property it fed. A generator that silently stops producing a construct
disables every property depending on it and nothing else notices — so
adequacy is itself a property, checked at a bounded sample.
"""
from collections import Counter

import pytest
from hypothesis import find, given, settings

from sophios.lang import Code, Document, SophiosError, parse

from . import ast_strategies as strat
from .hermetic import COVERAGE, ORACLE, compile_hermetic_cwl


@pytest.mark.fast
def test_every_construct_appears_within_a_bounded_sample() -> None:
    """P26: 500 documents reach every construct kind the language has.

    Stated over one drawn batch rather than as a per-example property, because
    the claim is about the *sample*, not about any document: no single document
    contains every construct, and a per-example property saying so would be
    false by construction.

    Not `@given(st.data())` drawing 500 documents inside one execution, despite
    that being the natural reading of "one drawn batch": Hypothesis's engine
    always runs one deterministic "simplest choice" trial first — every
    `sampled_from`/`lists`/`booleans` draw takes its minimum — and with
    `st.data()` that trial draws the *same* trivial one-step document 500 times
    over. A batch that uniform is missing thirteen-plus constructs by
    construction, so a coverage assertion evaluated on it fails for every
    generator regardless of quality (verified empirically before writing this
    fix), and Hypothesis then spends minutes shrinking a interactively-drawn
    500-step failure that can never get smaller. Accumulating across 500
    independently-generated Hypothesis examples — only one of which is that
    trivial trial — and asserting once after generation completes keeps the
    claim about the sample, not any example, while never asking a single
    execution to be internally diverse.

    CANNOT GENERATE (declared, per the negative-testing rules): `!cwl`
    (RawCwlRef) — specified but not compilable until Spec 3, so a compilable
    generator must exclude it; `python_script` steps — named with a uuid4, so
    nothing over them is deterministic; multi-document YAML streams; merge keys.
    """
    seen: Counter[str] = Counter()

    @COVERAGE
    @given(strat.documents())
    def _collect(document: Document) -> None:
        seen.update(strat.constructs_in(document))

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `document`

    missing = [c for c in strat.CONSTRUCTS if not seen[c]]
    assert not missing, (
        f'{missing} never appeared in 500 documents. Every property fed by this '
        f'generator is silent about them.\nSeen: {dict(seen)}'
    )


@pytest.mark.fast
@given(strat.hostile_documents())
@COVERAGE
def test_every_ill_formed_document_earns_its_own_diagnostic(case: tuple[str, object]) -> None:
    """The hostile sibling's contract: rejected, with the right code, and never
    silently normalised or repaired into something that compiles."""
    source, expected = case
    result = parse(source, 'hostile.wic')
    codes = [d.code for d in result.diagnostics]
    assert expected in codes, f'expected {expected}, got {codes}\n{source}'


@pytest.mark.fast
@given(strat.excluded_documents('edge_def_in_input'))
@ORACLE
def test_edge_def_in_input_still_earns_wic011(document: Document) -> None:
    """`NOT_YET_COMPILABLE`'s expiry mechanism for CE-13, not just its docstring.

    Every document with an `!& name` bound to a step input must still
    provably fail with exactly the diagnostic the exclusion's reason names —
    "some diagnostic fired" would be satisfied by the wrong one. The day
    `compiler.py` grows a `wic_anchor` case for `in:`, this goes red for the
    right reason (a document that now compiles, not one that fails
    differently), and whoever is standing there removes `edge_def_in_input`
    from `NOT_YET_COMPILABLE` instead of it quietly outliving the defect that
    justified it. Asserts `strat._binds_edge_def_as_input` too, as a check on
    the check: this test is worthless if `excluded_documents` ever stops
    actually drawing from the construct it claims to.
    """
    assert strat._binds_edge_def_as_input(document)  # pylint: disable=protected-access
    with pytest.raises(SophiosError) as caught:
        compile_hermetic_cwl(strat.to_yml(document), 'excluded')
    assert caught.value.diagnostics[0].code == Code.UNRESOLVED_INPUT, (
        f"failed with {caught.value.diagnostics[0].code}, not wic011 — CE-13's reason no "
        f'longer describes reality:\n{strat.render(document)}')


@pytest.mark.slow
def test_an_injected_fault_shrinks_to_a_small_workflow() -> None:
    """P27: a failure arrives as something a person can read.

    Runs a nested Hypothesis search for a document that trips a deliberately
    planted fault, and asserts the reported minimal case is under the bound.
    Without this, a generator that shrinks badly turns every real
    counterexample into a forty-line document nobody triages, and the suite
    quietly stops paying for itself.

    A plain function, not `@given`-decorated: `find` runs its own Hypothesis
    search internally, and calling it from inside an already-running `@given`
    test trips `HealthCheck.nested_given` (quadratic generation/shrinking) —
    the `st.data()` parameter the brief specified was never drawn from, so
    dropping the decorator changes nothing about what this test exercises.
    """
    minimal = find(strat.documents(), lambda d: len(d.steps) >= 2,
                   settings=settings(max_examples=50, deadline=None))
    rendered = strat.render(minimal)
    assert len(rendered.splitlines()) <= 12, (
        f'shrunk case is {len(rendered.splitlines())} lines; a counterexample '
        f'this size will not be read:\n{rendered}')
