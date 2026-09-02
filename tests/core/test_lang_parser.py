"""Properties of the Sophios syntax layer.

Acceptance here is property-based. The unit tests at the end are sanity checks
on wiring and obvious errors; they are not evidence that the parser is correct.

The claims under test, one property each: every AST node carries a resolvable
source span; parsing never raises — it returns an AST or diagnostics;
InputValue is closed, so no value escapes the five forms; every diagnostic
span indexes real source text; and every corpus document parses.

See design_docs/core-refactor-design.md, Spec 1.
"""
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, NamedTuple, get_args

import pytest
import yaml
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from sophios.lang import (
    Code,
    Diagnostic,
    Diagnostics,
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    InputValue,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
)
from sophios.lang.spans import SourceSpan
from sophios.utils_yaml import wic_loader

from . import provocations
from .wic_corpus import CORPUS, corpus_id
from .strategies import documents, identifiers

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _spans(node: Any) -> list[SourceSpan]:
    """Collect every span reachable from an AST node."""
    found: list[SourceSpan] = []
    match node:
        case Document():
            found += _opt(node.span)
            for step in node.steps:
                found += _spans(step)
            if node.sidecar is not None:
                found += _spans(node.sidecar)
        case Step():
            found += _opt(node.span)
            for _, value in node.inputs:
                found += _spans(value)
            for binding in node.outputs:
                found.append(binding.span)
                if binding.edge_def is not None:
                    found.append(binding.edge_def.span)
        case WicSidecar():
            found += _opt(node.span)
            for _, child in node.steps:
                found += _spans(child)
        case InlineLiteral() | EdgeDef() | EdgeRef() | RawCwlRef() | UnresolvedName():
            found.append(node.span)
    return found


def _opt(span: SourceSpan | None) -> list[SourceSpan]:
    return [span] if span is not None else []


def _resolvable(span: SourceSpan, text: str) -> bool:
    """Whether a span points at a position that exists in the source."""
    lines = text.splitlines() or ['']
    if not 1 <= span.start_line <= len(lines):
        return False
    return bool(1 <= span.start_column <= len(lines[span.start_line - 1]) + 1)


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(st.text(max_size=400))
@FAST
def test_parsing_is_total_for_arbitrary_text(text: str) -> None:
    """Parsing never raises, whatever it is handed."""
    result = parse(text, 'fuzz.wic')
    assert result.document is not None or len(result.diagnostics) > 0


@pytest.mark.fast
@given(documents())
@FAST
def test_well_formed_documents_parse(source: str) -> None:
    """A syntactically valid document yields a document and no errors."""
    result = parse(source, 'gen.wic')
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
@given(documents())
@FAST
def test_every_node_carries_a_resolvable_span(source: str) -> None:
    """Every span in the tree points at real source."""
    result = parse(source, 'gen.wic')
    assert result.document is not None
    spans = _spans(result.document)
    assert spans, 'no spans collected; the walk is not reaching the tree'
    for span in spans:
        assert _resolvable(span, source), f'unresolvable span {span}'


@pytest.mark.fast
@given(documents())
@FAST
def test_input_values_are_closed(source: str) -> None:
    """Every input value is one of the five declared forms."""
    permitted = get_args(InputValue)
    result = parse(source, 'gen.wic')
    assert result.document is not None
    for step in result.document.steps:
        for name, value in step.inputs:
            assert isinstance(value, permitted), f'{name} produced {type(value).__name__}'


@pytest.mark.fast
@given(st.text(max_size=400))
@FAST
def test_diagnostic_spans_index_real_source(text: str) -> None:
    """A parse diagnostic always has a span, inside the document.

    The `Diagnostic` type allows a spanless entry because compile-phase
    failures may not know a position — but the *parser* always does, and this
    is where that stronger guarantee is enforced.
    """
    for diagnostic in parse(text, 'fuzz.wic').diagnostics:
        assert diagnostic.span is not None, f'parse diagnostic without a span: {diagnostic}'
        assert diagnostic.span.file == 'fuzz.wic'
        assert diagnostic.span.start_line >= 1
        assert diagnostic.span.start_column >= 1
        assert diagnostic.span.start_line <= max(len(text.splitlines()), 1) + 1


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_corpus_files_parse(path: Path) -> None:
    """Every `.wic` in the repository corpus parses without error.

    This is what makes "the specification accepts what exists today"
    falsifiable rather than an aspiration.
    """
    source = path.read_text(encoding='utf-8')
    result = parse(source, str(path))
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
def test_corpus_is_not_empty() -> None:
    """Discovery must find workflows, or the corpus property passes vacuously.

    The corpus is whatever `search_paths_wic` reaches — the same discovery the
    compiler uses, so there is nothing separate to keep complete. An empty
    result does not mean a small corpus; it means discovery itself broke, and
    that is a failure, not a skip. CI provisions the config that reaches the
    sibling repositories, and CI is the arbiter of full coverage.
    """
    assert CORPUS, 'get_yml_paths found no workflows; check search_paths_wic'


@pytest.mark.fast
@given(
    st.sampled_from([('!ii', 'wic_inline_input', InlineLiteral),
                     ('!&', 'wic_anchor', EdgeDef),
                     ('!*', 'wic_alias', EdgeRef),
                     ('!cwl', 'wic_raw_cwl', RawCwlRef)]),
    identifiers,
)
@FAST
def test_surface_forms_are_equivalent(form: tuple[str, str, type], payload: str) -> None:
    """Tagged and desugared spellings of a construct parse to the same node.

    Humans write `!ii x`; the Python API emits `{wic_inline_input: x}`, because
    a constructor that re-emitted its own tag would fire again on reload. Both
    are the same language, so both must produce the same AST — otherwise the
    two surfaces have quietly diverged.
    """
    tag, key, expected = form
    tagged = parse(f'steps:\n- id: s\n  in:\n    f: {tag} {payload}\n', 'a.wic')
    sugared = parse(f'steps:\n- id: s\n  in:\n    f:\n      {key}: {payload}\n', 'b.wic')
    assert tagged.ok and sugared.ok
    assert tagged.document is not None and sugared.document is not None

    left = tagged.document.steps[0].inputs[0][1]
    right = sugared.document.steps[0].inputs[0][1]
    assert isinstance(left, expected) and isinstance(right, expected)
    assert _payload(left) == _payload(right)


def _payload(value: InputValue) -> Any:
    """The carried value of an input node, whatever its form."""
    match value:
        case InlineLiteral():
            return value.value
        case RawCwlRef():
            return value.expression
        case EdgeDef() | EdgeRef() | UnresolvedName():
            return value.name


# --------------------------------------------------------------------------
# Sanity checks (not acceptance evidence)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Accepted shapes, and reported ones
# --------------------------------------------------------------------------
#
# Every row is the same three steps — parse, check the verdict, inspect one
# thing — so only the source and the claim are worth reading. Each row is
# still a named case in the test report.


class Accepted(NamedTuple):
    """Source the language admits, and what the document must then hold."""

    claim: str
    source: str
    holds: Callable[[Document], bool]


ACCEPTED: Final[tuple[Accepted, ...]] = (
    Accepted('a bare name is an unresolved name',
             'steps:\n  s:\n    in:\n      e: plain\n',
             lambda d: isinstance(d.steps[0].input('e'), UnresolvedName)),
    Accepted('an untagged mapping input is an inline literal (§4.1)',
             'steps:\n- id: s\n  in:\n    a: {some: mapping}\n',
             lambda d: isinstance(d.steps[0].inputs[0][1], InlineLiteral)
             and d.steps[0].inputs[0][1].value == {'some': 'mapping'}),
    Accepted('sidecar step keys are structured, not strings',
             'wic:\n  steps:\n    (1, alpha):\n      wic:\n        graphviz: {}\n',
             lambda d: d.sidecar is not None
             and (d.sidecar.steps[0][0].index, d.sidecar.steps[0][0].name) == (1, 'alpha')),
    Accepted('a bare wic: is an empty sidecar, not an error',
             'wic:\nsteps:\n  s:\n    in:\n      a: !ii 1\n',
             lambda d: d.sidecar is not None),
    Accepted('a step with no body at all is well-formed',
             'steps:\n  some_step.wic:\n',
             lambda d: d.steps[0].id == 'some_step.wic' and d.steps[0].inputs == ()),
    Accepted('uninterpreted top-level keys survive',
             '$namespaces:\n  edam: http://example\nsteps:\n  s: {}\n',
             lambda d: dict(d.passthrough)['$namespaces'] == {'edam': 'http://example'}),
)


class Reported(NamedTuple):
    """Source the language diagnoses, and what it must say about it."""

    claim: str
    source: str
    code: Code
    message_contains: str = ''
    #: Whether a document survives alongside the diagnostic. Recovery is the
    #: norm; only unparsable YAML leaves nothing to return.
    recovers: bool = True


REPORTED: Final[tuple[Reported, ...]] = (
    Reported('a collection step key is reported, not stringified',
             'steps:\n  ? [a, b]\n  : {}\n', Code.EXPECTED_SCALAR,
             message_contains='mapping keys must be scalars'),
    Reported('an input bound twice names the input (§4.2)',
             'steps:\n- id: s\n  in:\n    f: !ii a\n    f: !ii b\n',
             Code.DUPLICATE_KEY, message_contains="'f'"),
    Reported('malformed YAML is located, not raised',
             'steps:\n  - [unclosed\n', Code.INVALID_YAML, recovers=False),
)


@pytest.mark.fast
@pytest.mark.parametrize('case', ACCEPTED, ids=[case.claim for case in ACCEPTED])
def test_accepted_shapes(case: Accepted) -> None:
    """Each admitted shape parses, and the AST carries what it should."""
    result = parse(case.source, 'accepted.wic')
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None
    assert case.holds(result.document), case.claim


@pytest.mark.fast
@pytest.mark.parametrize('case', REPORTED, ids=[case.claim for case in REPORTED])
def test_reported_shapes(case: Reported) -> None:
    """Each diagnosed shape reports its code, with a span, and recovers or not."""
    result = parse(case.source, 'reported.wic')
    matching = [d for d in result.diagnostics if d.code is case.code]
    assert matching, f'{case.code} did not fire: {[str(d) for d in result.diagnostics]}'
    assert all(d.span is not None for d in matching), 'diagnostic without a span'
    if case.message_contains:
        assert any(case.message_contains in d.message for d in matching)
    assert (result.document is not None) is case.recovers


@pytest.mark.fast
def test_sequence_and_mapping_steps_agree() -> None:
    """Both `steps:` surface forms produce the same AST shape."""
    as_map = parse('steps:\n  touch:\n    in:\n      f: !ii x\n', 'm.wic').document
    as_seq = parse('steps:\n- id: touch\n  in:\n    f: !ii x\n', 's.wic').document
    assert as_map is not None and as_seq is not None
    assert [s.id for s in as_map.steps] == [s.id for s in as_seq.steps] == ['touch']
    from_map, from_seq = as_map.steps[0].inputs[0][1], as_seq.steps[0].inputs[0][1]
    assert isinstance(from_map, InlineLiteral) and isinstance(from_seq, InlineLiteral)
    assert from_map.value == from_seq.value


@pytest.mark.fast
def test_python_api_emits_documents_this_parser_accepts() -> None:
    """The Python API is the second surface of the same language.

    Whatever it emits must parse, or the two surfaces have diverged and the
    language reference is describing something that does not exist. See
    docs/sophios_language_reference.md, section 6.
    """
    # Imported here: the API pulls in the whole compiler, which the syntax
    # layer deliberately does not depend on.
    # pylint: disable=import-outside-toplevel
    from sophios.api.python.workflow import Step as ApiStep
    from sophios.api.python.workflow import Workflow

    from .test_python_api import _adapter

    touch = ApiStep(clt_path=_adapter('touch'))
    touch.inputs.filename = 'empty.txt'
    workflow = Workflow([touch], 'adherence')

    result = parse(workflow.to_wic_yaml(), 'api_emitted.wic')

    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None
    emitted = result.document.steps[0].input('filename')
    assert isinstance(emitted, InlineLiteral)
    assert emitted.value == 'empty.txt'

# --------------------------------------------------------------------------
# The parser is never more permissive than the loader (PR #382 review)
# --------------------------------------------------------------------------
#
# The loader rejects unknown tags with ConstructorError; a syntax layer that
# silently accepted them would specify a superset of the language. Both
# escapes the review found are pinned, plus the agreement property.


@pytest.mark.fast
@pytest.mark.parametrize('source', [
    'steps:\n- id: s\n  in:\n    a: !foo bar\n',
    'top: !foo bar\n',
    'top: !foo {a: 1}\n',
    'top: !foo [1, 2]\n',
    'steps:\n- id: s\n  in:\n    a: !foo {wic_anchor: x}\n',
], ids=['input-pos', 'scalar', 'mapping', 'sequence', 'over-desugared'])
def test_unknown_tags_report_wic009(source: str) -> None:
    """The code contract: an unknown tag reports wic009, in every position.

    *Detection* is the adversarial property's job (with @example pins for
    determinism); this asserts only the stable code the reference documents.
    """
    result = parse(source, 'x.wic')
    assert any(d.code is Code.UNKNOWN_TAG for d in result.diagnostics), source
    assert not result.ok


@pytest.mark.fast
@given(st.text('abcdefghijklmnopqrstuvwxyz!&*i {}[]:#-', max_size=120))
@FAST
@example('!: :')      # unknown tag on a mapping *key* — the position that was missed
@example('!foo x: y')  # the same, spelled legibly
def test_parser_is_not_more_permissive_than_the_loader(text: str) -> None:
    """Any document the parser accepts without diagnostics, the loader loads.

    The reverse is allowed — the parser recovers where the loader raises — but
    this direction is the specification's half of the bargain.

    The pinned cases are tags in key position. The property found `!: :` on
    its own, months after the tag rule was written, because the alphabet only
    rarely composes a tagged key — which is exactly why it is pinned now:
    Hypothesis's failure database does not travel to CI, so a find that came
    from one lucky seed would otherwise be re-discovered by luck or not at all.
    """
    result = parse(text, 'agree.wic')
    if result.ok and result.document is not None:
        yaml.load(text, Loader=wic_loader())  # must not raise


@pytest.mark.fast
@given(st.sampled_from(['010', '0x1A', '2020-01-01', '.inf', '12:30', 'true', 'null', '3.14', 'plain']))
def test_passthrough_scalars_resolve_exactly_as_the_loader(text: str) -> None:
    """Scalar resolution is delegated to PyYAML, not re-implemented.

    A hand-written table diverged on every one of these; delegation makes the
    divergence structurally impossible.
    """
    result = parse(f'top:\n  k: {text}\n', 'x.wic')
    assert result.document is not None
    top = dict(result.document.passthrough)['top']
    assert isinstance(top, dict)  # narrows the closed OpaqueCwl union
    assert top['k'] == yaml.safe_load(text)


@pytest.mark.fast
def test_diagnostics_slices_are_diagnostics() -> None:
    """Slicing honours the Sequence contract instead of degrading to list."""
    result = parse('steps:\n- id: s\n  in:\n    a: !foo x\n    b: !bar y\n', 'x.wic')
    assert isinstance(result.diagnostics[0:1], Diagnostics)
    assert len(result.diagnostics[0:1]) == 1

# --------------------------------------------------------------------------
# Pins from the second offline review round of #382
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_sidecar_nesting_is_normalised_at_every_depth() -> None:
    """`(index, name)` keys are StepKeys at depth two, not opaque strings.

    The child sidecar arrives wrapped in a `wic:` key on the surface; the
    parser descends through it, or `StepKey`'s "nothing downstream should
    ever parse that string again" is false at every depth past the first.
    """
    result = parse(
        'wic:\n  steps:\n    (1, outer):\n      wic:\n        steps:\n'
        '          (1, inner):\n            wic:\n              steps:\n'
        '                (1, innermost):\n                  x: 1\n',
        'nested.wic')
    assert result.ok and result.document is not None and result.document.sidecar is not None

    level1 = result.document.sidecar.steps[0]
    assert (level1[0].index, level1[0].name) == (1, 'outer')
    level2 = level1[1].steps[0]
    assert (level2[0].index, level2[0].name) == (1, 'inner')
    level3 = level2[1].steps[0]
    assert (level3[0].index, level3[0].name) == (1, 'innermost')


@pytest.mark.fast
def test_the_loader_accepts_every_owned_tag() -> None:
    """`!cwl` is registered with the loader like its three siblings.

    The agreement property below quantifies over this; the targeted case is
    pinned so a regression names itself instead of surfacing as a fuzz flake.
    """
    loaded = yaml.load('a: !cwl step/out\n', Loader=wic_loader())
    assert loaded == {'a': {'wic_raw_cwl': 'step/out'}}


@pytest.mark.fast
@pytest.mark.parametrize('source', ['top: &a [*a]\n',
                                    'steps: &a\n- id: s\n  in:\n    x: *a\n',
                                    'wic: &w\n  steps: *w\n'],
                         ids=['passthrough', 'steps', 'sidecar'])
def test_alias_cycles_are_reported(source: str) -> None:
    """The contract: a cycle is a diagnosed error, never a quiet success.

    Totality (no raise) is the adversarial property's job, with these shapes
    pinned on it as @example; this asserts the report the reference promises.
    """
    result = parse(source, 'cycle.wic')
    assert not result.ok, source


@pytest.mark.fast
def test_alias_expansion_is_bounded() -> None:
    """A widening alias chain is cut off by budget, quickly, once."""
    laughs = 'a0: &a0 [x, x, x, x, x, x, x, x, x]\n'
    for i in range(1, 8):
        refs = ', '.join([f'*a{i-1}'] * 9)
        laughs += f'a{i}: &a{i} [{refs}]\n'

    started = time.perf_counter()
    result = parse(laughs, 'laughs.wic')  # must not raise or hang
    assert time.perf_counter() - started < 5.0
    assert not result.ok
    assert any(d.code is Code.RECURSIVE_ALIAS for d in result.diagnostics)


@pytest.mark.fast
def test_out_values_are_never_silently_dropped() -> None:
    """A non-!& out value is reported; both edge spellings are honoured."""
    dropped = parse('steps:\n- id: s\n  out:\n  - file: something\n', 'x.wic')
    assert not dropped.ok
    assert any('file' in d.message for d in dropped.diagnostics)

    tagged = parse('steps:\n- id: s\n  out:\n  - file: !& e\n', 'x.wic')
    desugared = parse('steps:\n- id: s\n  out:\n  - file: {wic_anchor: e}\n', 'x.wic')
    for result in (tagged, desugared):
        assert result.ok and result.document is not None
        binding = result.document.steps[0].outputs[0]
        assert binding.edge_def is not None and binding.edge_def.name == 'e'


# --------------------------------------------------------------------------
# Adversarial structure — the space the review proved the fuzzer never reached
# --------------------------------------------------------------------------
#
# `st.text()` almost never forms valid YAML with aliases, so quantifying
# totality over it left the entire anchor/alias class untested: cycles raised
# RecursionError and widening chains exhausted memory while the totality
# property stayed green.
# These strategies generate *structured* YAML — anchors, aliases (including
# self-referential ones), tags known and unknown, deep nesting — so the
# property covers the inputs that actually break parsers.


_tag_pool = st.sampled_from(['', '!ii ', '!& ', '!* ', '!cwl ', '!foo ', '!x '])


@st.composite
def adversarial_yaml(draw: st.DrawFn) -> str:
    """Structured YAML text with anchors, aliases, tags, and real nesting."""
    fragments = []
    anchors: list[str] = []
    for i in range(draw(st.integers(min_value=1, max_value=6))):
        key = draw(identifiers)
        shape = draw(st.sampled_from(['scalar', 'flow_list', 'flow_map', 'anchor', 'alias', 'self_ref', 'nested']))
        tag = draw(_tag_pool)
        match shape:
            case 'scalar':
                fragments.append(f'{key}: {tag}{draw(identifiers)}')
            case 'flow_list':
                items = ', '.join(draw(st.lists(identifiers, min_size=0, max_size=4)))
                fragments.append(f'{key}: {tag}[{items}]')
            case 'flow_map':
                inner = draw(identifiers)
                fragments.append(f'{key}: {tag}{{{inner}: {draw(identifiers)}}}')
            case 'anchor':
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{draw(identifiers)}]')
            case 'alias' if anchors:
                ref = draw(st.sampled_from(anchors))
                repeats = ', '.join([f'*{ref}'] * draw(st.integers(min_value=1, max_value=6)))
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{repeats}]')
            case 'self_ref':
                # A node that contains itself — legal YAML, lethal to naive walks.
                fragments.append(f'{key}: &s{i} [x, *s{i}]')
            case _:
                depth = draw(st.integers(min_value=1, max_value=5))
                nested = draw(identifiers)
                for _ in range(depth):
                    nested = f'{{n: {nested}}}'
                fragments.append(f'{key}: {nested}')
    return '\n'.join(fragments) + '\n'


@pytest.mark.fast
@given(adversarial_yaml())
# Deterministic pins: Hypothesis is randomised and its failure database does
# not travel to CI, so every counterexample this property has caught (or was
# built to catch) is an @example — same coverage, no separate test functions.
@example('top: &a [*a]\n')                                # alias cycle
@example('steps: &a\n- id: s\n  in:\n    x: *a\n')        # cycle via steps
@example('wic: &w\n  steps: *w\n')                        # cycle via sidecar
@example('_: !& []\n')                                    # CE-06: name tag on a collection
@example('_: !foo bar\n')                                 # unknown tag, passthrough
@example('a: !cwl b\n')                                   # the once-missing loader constructor
@example('_: !ii {k: v}\n')                               # collection literal, single wrap
@FAST
def test_parsing_is_total_for_adversarial_structure(text: str) -> None:
    """The half of totality the review proved missing: totality over real YAML
    structure — aliases, cycles, unknown tags, nesting — not just over text.

    Never raises; never accepts silently what the loader rejects; and never
    reports the same problem twice — several parse paths legitimately visit
    the same node, and duplicate (code, span, message) reports were a
    measured regression once already.
    """
    result = parse(text, 'adversarial.wic')  # must not raise
    assert result.document is not None or len(result.diagnostics) > 0

    reports = [(d.severity, d.code, d.message, d.span) for d in result.diagnostics]
    assert len(reports) == len(set(reports)), f'duplicate diagnostics: {reports}'

    if result.ok and result.document is not None:
        yaml.load(text, Loader=wic_loader())  # agreement holds here too


@pytest.mark.fast
@given(_tag_pool, st.sampled_from(['scalar', 'flow_map']), st.data())
@FAST
def test_tag_decisions_agree_across_positions(tag: str, shape: str, data: st.DataObject) -> None:
    """Input position and passthrough position make the same unknown-tag call.

    The review found `!foo {wic_anchor: x}` reported in passthrough but
    swallowed in input position — two paths, one language, two answers. This
    pins the agreement for every tag and payload shape the generator makes.
    """
    payload = data.draw(identifiers) if shape == 'scalar' else f'{{{data.draw(identifiers)}: x}}'
    value = f'{tag}{payload}'

    in_position = parse(f'steps:\n- id: s\n  in:\n    a: {value}\n', 'pos.wic')
    passthrough = parse(f'top: {value}\n', 'pos.wic')

    reported_in = any(d.code is Code.UNKNOWN_TAG for d in in_position.diagnostics)
    reported_through = any(d.code is Code.UNKNOWN_TAG for d in passthrough.diagnostics)
    assert reported_in == reported_through, f'{value!r}: input={reported_in}, passthrough={reported_through}'


@pytest.mark.fast
def test_inline_literal_collections_wrap_exactly_once() -> None:
    """`!ii {a: 1}` is one InlineLiteral around plain data, in both positions.

    The passthrough walk wraps tagged collections and `_literal`'s caller
    wraps too; without coordination the same node wrapped twice and the AST
    carried InlineLiteral(InlineLiteral(...)).
    """
    in_position = parse('steps:\n- id: s\n  in:\n    a: !ii {k: v}\n', 'x.wic')
    assert in_position.ok and in_position.document is not None
    literal = in_position.document.steps[0].inputs[0][1]
    assert isinstance(literal, InlineLiteral)
    assert literal.value == {'k': 'v'}

    passthrough = parse('top: !ii {k: v}\n', 'x.wic')
    assert passthrough.ok and passthrough.document is not None
    wrapped = dict(passthrough.document.passthrough)['top']
    assert isinstance(wrapped, InlineLiteral)
    assert wrapped.value == {'k': 'v'}

# --------------------------------------------------------------------------
# Every diagnostic is provocable, mechanically (negative-testing rule 1)
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_every_code_has_a_registered_provocation() -> None:
    """Each declared Code appears in exactly one tier of the registry.

    Adding a Code without its attack fails here, in the same commit."""
    registered = set(provocations.PARSE) | set(provocations.COMPILED)
    missing = set(Code) - registered
    doubled = set(provocations.PARSE) & set(provocations.COMPILED)
    assert not missing, f'codes with no registered provocation: {sorted(map(str, missing))}'
    assert not doubled, f'codes registered in both tiers: {sorted(map(str, doubled))}'


@pytest.mark.fast
@pytest.mark.parametrize('code', sorted(provocations.PARSE), ids=lambda c: c.name)
def test_parse_provocations_fire(code: Code) -> None:
    """Every parse-tier provocation actually fires its code."""
    result = parse(provocations.PARSE[code], 'provoke.wic')
    assert any(d.code is code for d in result.diagnostics), f'{code.name} did not fire'


@pytest.mark.skip_pypi_ci
@pytest.mark.parametrize('code', sorted(provocations.COMPILED), ids=lambda c: c.name)
def test_compiled_provocations_fire(code: Code) -> None:
    """Every compiled-tier provocation raises SophiosError carrying its code.

    `SophiosError` is looked up dynamically: it arrives with the diagnostics
    branch, and on branches before it the COMPILED tier is empty so this test
    has no instances — the lookup keeps the file importable stack-wide.
    """
    from sophios.lang import diagnostics as diagnostics_module
    sophios_error: type[Exception] = getattr(diagnostics_module, 'SophiosError', AssertionError)
    with pytest.raises(sophios_error) as caught:
        provocations.COMPILED[code]()
    fired = getattr(caught.value, 'diagnostics', ())
    assert any(d.code is code for d in fired), f'{code.name} did not fire'


# --------------------------------------------------------------------------
# The syntax layer stays standalone (the reviewer's method, made permanent)
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_lang_layer_depends_only_on_stdlib_and_pyyaml() -> None:
    """`sophios.lang` + `utils_yaml` import nothing beyond stdlib and yaml.

    The #383 reviewer staged exactly these files into a bare venv with only
    PyYAML and everything ran — which is what makes the layer reviewable in
    isolation and, eventually, extractable for editor tooling. One stray
    import would end that silently; this makes it a test failure instead.
    """
    import ast as python_ast
    import sys

    lang_dir = Path(__file__).resolve().parents[2] / 'src' / 'sophios' / 'lang'
    files = sorted(lang_dir.glob('*.py')) + [lang_dir.parent / 'utils_yaml.py']
    allowed = set(sys.stdlib_module_names) | {'yaml', 'sophios'}

    for source_file in files:
        tree = python_ast.parse(source_file.read_text(encoding='utf-8'))
        for node in python_ast.walk(tree):
            roots = []
            if isinstance(node, python_ast.Import):
                roots = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, python_ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split('.')[0]]
            for root in roots:
                assert root in allowed, f'{source_file.name} imports {root!r} — the lang layer must stay standalone'
