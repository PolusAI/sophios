"""The generator is adequate, and its failures are usable.

A property is only as strong as its generator, and a generator that stops
producing a construct disables every property depending on it with nothing
else noticing. Unconstrained `st.text()` essentially never forms valid YAML
with an alias, which is why every strategy here draws from a named alphabet --
`edge_names` is `st.text('abcdefgh', ...)` rather than bare text. A document
generator drawing only mapping-form `in:`-only documents makes outputs,
sequence steps and nested sidecars structurally invisible. So adequacy is
itself a property, checked at a bounded sample.
"""
import re
from collections import Counter
from typing import Any

import pytest
from hypothesis import find, given, settings
from hypothesis.strategies import SearchStrategy

from sophios.lang import Document, Step, parse

from . import ast_strategies as strat
from .hermetic import COVERAGE


@pytest.mark.fast
def test_every_construct_appears_within_a_bounded_sample() -> None:
    """500 documents reach every construct kind in `CONSTRUCTS`.

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

    CANNOT GENERATE (declared, per the negative-testing rules):
    `python_script` steps, whose module definition belongs to the registry;
    multi-document YAML streams; merge keys.
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
def test_the_compilable_subset_still_reaches_every_construct_it_does_not_exclude() -> None:
    """The companion `NOT_YET_COMPILABLE` claims, and did not have.

    `compilable_documents()` is what the compile-driving properties quantify
    over, and its filter is the one place a construct can leave the oracle's
    reach without anything going red — `documents()` keeps producing the whole
    language, so coverage stays green no matter what the filter removes. An
    exclusion predicate of `lambda d: True` empties the strategy entirely, and
    without the check below every test in this file would still pass.

    So the filter is held to the same standard as the generator. An exclusion
    that costs a construct has to be argued for by whoever adds it, here, in
    this test — that is the decision `NOT_YET_COMPILABLE` exists to make
    visible, and a test that pre-authorised it by subtracting the exclusions
    from the expected set would authorise the silent narrowing too.

    `excluded_documents` is called for each entry as well: it is the only
    caller that keeps `_EXCLUSION_PREDICATES` keyed identically to
    `NOT_YET_COMPILABLE` at runtime rather than only at import.
    """
    seen: Counter[str] = Counter()

    @COVERAGE
    @given(strat.compilable_documents())
    def _collect(document: Document) -> None:
        seen.update(strat.constructs_in(document))

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `document`

    for name in strat.NOT_YET_COMPILABLE:
        assert strat.excluded_documents(name) is not None

    missing = [c for c in strat.CONSTRUCTS if not seen[c]]
    assert not missing, (
        f'{missing} never appeared in 500 documents drawn from compilable_documents(), '
        f'so every compile-driving property is silent about them. Active exclusions: '
        f'{sorted(strat.NOT_YET_COMPILABLE)}.\nSeen: {dict(seen)}'
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
def test_no_step_the_generator_draws_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step drawn reaches the document.

    Mapping form cannot repeat a step name, so its stems are drawn unique up
    front. A step drawn and then dropped on a collision would keep the edge
    names it defined (a dangling `!*`, `wic025`) and shrink the sample, so a
    document asking for four steps gets two. The dangling edge is rare (4
    documents in 2000); the discard is common and only the drawing site sees
    it, so it is recorded there.
    """
    drawn: list[str] = []
    real = strat._step  # pylint: disable=protected-access  # the drawing site is the subject

    def _recording(stem: str, defined_edges: list[tuple[str, Any]],
                   referenced_inputs: set[str]) -> SearchStrategy[Step]:
        drawn.append(stem)
        return real(stem, defined_edges, referenced_inputs)

    monkeypatch.setattr(strat, '_step', _recording)

    @COVERAGE
    @given(strat.documents())
    def _check(document: Document) -> None:
        # The *tail* of `drawn`, not all of it: Hypothesis abandons a partly
        # drawn example whenever a draw comes back unusable — the unique list
        # of stems is one such draw — and those abandoned steps are recorded
        # too, with no document to compare them against. The steps of the
        # example that survived are the last ones drawn, and under this
        # generator they are exactly as many as the document keeps.
        kept = [step.id for step in document.steps if not step.id.endswith('.wic')]
        assert drawn[-len(kept):] == kept, (
            f'the generator drew {drawn[-len(kept) - 2:]} and the document kept {kept}. A '
            f'discarded step takes its !& names with it and shrinks the sample silently')
        drawn.clear()

    _check()  # pylint: disable=no-value-for-parameter  # @given supplies `document`


@pytest.mark.slow
def test_an_injected_fault_shrinks_to_a_small_workflow() -> None:
    """A failure arrives as something a person can read.

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


@pytest.mark.fast
def test_the_construct_inventory_accounts_for_every_input_kind() -> None:
    """Every member of the `InputValue` union is drawn, or declared undrawable.

    The coverage test above quantifies over `CONSTRUCTS`, so a kind missing from
    that tuple is invisible to it -- the inventory would shrink and the sample
    would still be "complete". Deriving the kinds from the union instead means
    adding an AST node fails here until someone either generates it or writes
    down why they cannot.
    """
    import typing  # pylint: disable=import-outside-toplevel

    from sophios.lang.nodes import InputValue  # pylint: disable=import-outside-toplevel

    def snake(name: str) -> str:
        return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()

    kinds = {snake(member.__name__) for member in typing.get_args(InputValue)}
    accounted = set(strat.CONSTRUCTS) | set(strat.NOT_GENERATED)
    missing = kinds - accounted
    assert not missing, (
        f'{sorted(missing)} are `InputValue` members that the generator neither draws nor '
        f'declares undrawable. Add the kind to CONSTRUCTS, or to NOT_GENERATED with a reason.')
    assert not (set(strat.NOT_GENERATED) & set(strat.CONSTRUCTS)), (
        'a kind cannot be both drawn and declared undrawable')
