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
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis.strategies import SearchStrategy

from sophios.lang import Document, EdgeRef, Step, parse
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .hermetic import COVERAGE, compile_hermetic_cwl


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
def test_the_compilable_subset_still_reaches_every_construct_it_does_not_exclude() -> None:
    """The companion `NOT_YET_COMPILABLE` claims, and did not have.

    `compilable_documents()` is what Tasks 3-7 quantify over, and its filter is
    the one place a construct can leave the oracle's reach without anything
    going red — `documents()` keeps producing the whole language, so P26 stays
    green no matter what the filter removes. Found by mutation: an exclusion
    predicate of `lambda d: True` empties the strategy entirely and every test
    in this file passed.

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
        f'so every Task 3-7 property is silent about them. Active exclusions: '
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


@pytest.mark.slow
def test_the_workflows_strategy_produces_documents_the_compiler_accepts() -> None:
    """`workflows()` is what Tasks 3-7 quantify over, and nothing exercised it.

    Every claim `documents()` and `compilable_documents()` make is about the
    AST; `workflows()` is `to_yml` on top, and `to_yml` is the only part of
    this generator that must satisfy the *compiler* rather than the grammar.
    Its docstring says so in a warning — "Do not remove this call to 'simplify'
    `to_yml`" — about the `desugar_into_canonical_normal_form` that mapping-form
    documents need to survive `_compile_workflow`'s per-step loop. Nothing held
    it: delete that call and every mapping-form document raises `KeyError: 0`,
    with no test to notice.

    Both surface forms are required to reach a successful compilation, which is
    what pins the desugaring specifically. The overall bar is a bare majority,
    not everything: the module docstring's pending finding — `!ii` puts no
    constraint relating a literal to the CWL type it binds, so
    `populate_scalar_val` raises a bare `ValueError` — is a real and declared
    residual (measured at about one document in ten), and a threshold that
    pretended otherwise would be a flaky test rather than a stricter one.

    Drawn from `workflows_with_documents()`, which is the strategy `workflows()`
    itself is a projection of, so the composition under test is the one that
    ships. An earlier version rebuilt `compilable_documents().map(to_yml)` here
    by hand — needing the `Document` for `steps_as_mapping` and having no way
    to get it — which left `workflows()` with zero call sites in the tree while
    this test's own messages claimed to be covering it. Replacing its body with
    `st.none()` left the suite green.
    """
    compiled: Counter[str] = Counter()

    @settings(max_examples=50, suppress_health_check=list(HealthCheck), deadline=None)
    @given(strat.workflows_with_documents())
    def _collect(case: tuple[Document, Yaml]) -> None:
        document, yml = case
        form = 'mapping' if document.steps_as_mapping else 'sequence'
        compiled['drawn'] += 1
        try:
            compile_hermetic_cwl(yml, 'oracle')
        except ValueError:  # the declared `!ii` residual; see the docstring
            return
        compiled[form] += 1

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `case`

    for form in ('mapping', 'sequence'):
        assert compiled[form], (
            f'no {form}-form document from workflows_with_documents() compiled, so every '
            f'Task 3-7 property is quantifying over the other form alone: {dict(compiled)}')
    assert compiled['mapping'] + compiled['sequence'] > compiled['drawn'] // 2, (
        f'fewer than half the documents workflows() is a projection of compile: '
        f'{dict(compiled)}')


@pytest.mark.fast
def test_every_edge_a_document_references_is_defined_in_that_document() -> None:
    """An `!*` with no `!&` is not a document, and the compiler will not say so.

    `compile_hermetic` passes `testing=True`, and `compiler.py:784` raises for
    a dangling edge only when `not testing`; under this suite it falls through
    and adds a CWL input "for testing only" where the edge should have been. So
    a leaked edge name does not produce a failure, it produces a *different
    workflow* — one with one fewer internal edge and one more workflow input —
    and every Task 3-7 property then quantifies over documents other than the
    ones the generator believes it built.

    That is what discarding a duplicate-stem step used to do: the discarded
    step's `!&` names stayed in the document-scoped `defined_edges`, and a
    later step could reference them. Nothing noticed, because the one test that
    compiles generated documents attributes every `ValueError` to the declared
    `!ii` residual — so the dangling-edge error of any non-`testing` caller
    would have been swallowed there under the wrong name.

    This is the *invariant*, not the detector. Measured against the discarding
    generator, a leak that a later step then referenced landed in about one
    document in five hundred (4 in 2000), so at this sample the check catches
    that regression only about half the time. What catches it every time is
    `test_no_step_the_generator_draws_is_discarded` below, which pins the fix
    rather than its consequence. Both are here because either alone reads as
    arbitrary: this one says what must never be true of a document, that one
    says why the generator cannot produce it.
    """
    @COVERAGE
    @given(strat.documents())
    def _check(document: Document) -> None:
        defined = {binding.edge_def.name for step in document.steps
                   for binding in step.outputs if binding.edge_def is not None}
        referenced = {value.name for step in document.steps
                      for _, value in step.inputs if isinstance(value, EdgeRef)}
        assert referenced <= defined, (
            f'!* references an edge nothing in the document defines: '
            f'{sorted(referenced - defined)}\n{strat.render(document)}')

    _check()  # pylint: disable=no-value-for-parameter  # @given supplies `document`


@pytest.mark.fast
def test_no_step_the_generator_draws_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step drawn reaches the document, which is the whole of the fix.

    Mapping form cannot repeat a step name, and the earlier draft satisfied
    that by drawing a step and dropping it on a collision — keeping the edge
    names it had already defined (the invariant above) and quietly costing the
    sample its size, so a document asking for four steps often got two.
    Drawing the stems unique up front removes the discard instead of repairing
    after it.

    Recorded at the drawing site rather than read off the document, because
    every consequence of a discard is *rare* while the discard itself is
    common: a mapping-form collision happens in a large fraction of documents,
    but only a fifth of steps define an edge at all, so a leaked name that a
    later step then referenced was measured at 4 documents in 2000, and a gap
    in the edge numbering at 0 in 2000. A detector built on either would have
    reported this generator healthy about as often as not. Nothing but the
    difference between what was drawn and what survived sees it every time,
    and only the drawing side knows the first half.
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


@pytest.mark.fast
def test_workflows_is_the_pairs_second_half_and_nothing_else() -> None:
    """The one step between `workflows()` and the test above.

    That test draws `workflows_with_documents()`, so `workflows()` is a single
    `map` away from anything covered — and a single `map` is exactly where
    `st.none()` fits. A handful of draws closes it: the projection has to be
    the compiler input, not the document and not nothing.
    """
    @settings(max_examples=5, suppress_health_check=list(HealthCheck), deadline=None)
    @given(strat.workflows())
    def _check(yml: Yaml) -> None:
        assert isinstance(yml, dict), f'workflows() yielded {type(yml).__name__}'
        assert yml.get('steps'), f'workflows() yielded a document with no steps: {yml}'

    _check()  # pylint: disable=no-value-for-parameter  # @given supplies `yml`


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
