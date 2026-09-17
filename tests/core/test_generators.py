"""The generator is adequate, and its failures are usable.

A property is only as strong as its generator, and a generator that stops
producing a construct disables every property depending on it with nothing
else noticing. `st.text()` essentially never forms valid YAML with an alias;
a document generator drawing only mapping-form `in:`-only documents makes
outputs, sequence steps and nested sidecars structurally invisible. So
adequacy is itself a property, checked at a bounded sample.
"""
import re
from collections import Counter
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis.strategies import SearchStrategy

from sophios.lang import Code, Document, EdgeRef, SophiosError, Step, parse
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .hermetic import COVERAGE, compile_hermetic_cwl


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

    CANNOT GENERATE (declared, per the negative-testing rules): `!cwl`
    (RawCwlRef) — specified but not compilable until the front end is wired in, so a compilable
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


@pytest.mark.slow
def test_the_workflows_strategy_produces_documents_the_compiler_accepts() -> None:
    """`workflows()` is what the compile-driving properties quantify over.

    `to_yml` is the only part of this generator that must satisfy the *compiler*
    rather than the grammar. Delete its `desugar_into_canonical_normal_form`
    call and every mapping-form document raises `KeyError: 0` in
    `compile_workflow_once`'s per-step loop -- which is why both surface forms
    must reach a successful compilation.

    The bar is a bare majority, not everything: `!ii` constrains no literal to
    the CWL type it binds, so roughly one draw in ten binds `'_'` to an `int`
    and is refused as `LITERAL_TYPE_MISMATCH`. The generator draws well-formed
    documents, not well-typed ones, so those are skipped; a threshold demanding
    every draw compile would be flaky rather than stricter.

    Drawn from `workflows_with_documents()`, the strategy `workflows()` is a
    projection of, so the composition under test is the one that ships.
    Rebuilding `compilable_documents().map(to_yml)` here by hand leaves
    `workflows()` with no call site at all -- replace its body with `st.none()`
    and the suite stays green.
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
        except SophiosError as diagnosed:
            # Only the ill-typed `!ii` literal the docstring describes. Every other
            # diagnosis — a dangling edge, a missing required input — is a real
            # failure of the generator's claim, so it must not be skipped here.
            if any(d.code is not Code.LITERAL_TYPE_MISMATCH for d in diagnosed.diagnostics):
                raise
            return
        compiled[form] += 1

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `case`

    for form in ('mapping', 'sequence'):
        assert compiled[form], (
            f'no {form}-form document from workflows_with_documents() compiled, so every '
            f'compile-driving property is quantifying over the other form alone: {dict(compiled)}')
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
    and every compile-driving property then quantifies over documents other
    than the ones the generator believes it built.

    That is what discarding a duplicate-stem step used to do: the discarded
    step's `!&` names stayed in the document-scoped `defined_edges`, and a
    later step could reference them. Nothing noticed, because the one test that
    compiles generated documents skips every `SophiosError` as an ill-typed
    `!ii` literal — so the dangling-edge error of any non-`testing` caller
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

    Mapping form cannot repeat a step name. Satisfying that by drawing a step
    and dropping it on a collision keeps the edge names it had already defined
    (the invariant above) and quietly costs the sample its size, so a document
    asking for four steps often gets two. Drawing the stems unique up front
    removes the discard instead of repairing after it.

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
