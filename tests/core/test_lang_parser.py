"""Properties of the Sophios syntax layer (CR-101).

Acceptance here is property-based. The unit tests at the end are sanity checks
on wiring and obvious errors; they are not evidence that the parser is correct.

Properties covered:
  P03  every AST node carries a resolvable source span
  P04  parsing never raises; it returns an AST or diagnostics
  P05  InputValue is closed — no value escapes the five forms
  P06  every diagnostic span indexes real source text
  P07  every corpus document parses

See design_docs/core-refactor-design.md, Spec 1.
"""
import time
from pathlib import Path
from typing import Any, get_args

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

from .wic_corpus import CORPUS, corpus_id

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

identifiers = st.text('abcdefghijklmnopqrstuvwxyz_', min_size=1, max_size=8)
scalars = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.text('abcXYZ 0123', min_size=0, max_size=10),
)

#: Scalar payloads as YAML source text, spelled to include exactly what a
#: [a-z_]-only alphabet can never produce: type-ambiguous quoted strings,
#: exponent floats and specials, null, dates. The #383 review traced four of
#: its findings to the old alphabet's blind spot; these are its complement.
scalar_payload_texts = st.sampled_from([
    'x', '0', '-3', '1.5', '1.0e+300', '.inf', '.nan',
    'true', 'false', 'null', 'on', '2020-01-01',
    "'0'", "'true'", "'null'", "'on'", "''", "'a b'",
])

#: Construct leaves that may appear nested inside an `!ii` payload.
construct_payload_texts = st.sampled_from(['!* e', '!& d', '!cwl a/b', '{wic_anchor: n}'])

#: Recursive payload text over the closed OpaqueCwl union: scalars, nested
#: constructs, and flow collections of both. Derived from what the type
#: admits, not from what the tests happened to imagine.
_payload_collections = st.recursive(
    st.one_of(scalar_payload_texts, construct_payload_texts),
    lambda children: st.one_of(
        st.lists(children, min_size=0, max_size=3).map(lambda xs: '[' + ', '.join(xs) + ']'),
        st.lists(children, min_size=1, max_size=3).map(
            lambda xs: '{' + ', '.join(f'k{i}: {x}' for i, x in enumerate(xs)) + '}'),
    ),
    max_leaves=6,
).filter(lambda s: s.startswith(('[', '{')))

#: Direct payload of a tagged `!ii`: a scalar or a collection — a construct
#: leaf cannot be the whole payload, since two tags on one node is not YAML.
opaque_payload_texts = st.one_of(scalar_payload_texts, _payload_collections)

#: Desugared payloads can additionally carry a construct directly:
#: `{wic_inline_input: !* e}` is well-formed, and the writer must respell it.
desugared_payload_texts = st.one_of(opaque_payload_texts, construct_payload_texts)


@st.composite
def input_lines(draw: st.DrawFn, name: str | None = None, indent: str = '      ') -> str:  # pylint: disable=too-many-return-statements
    """One `in:` binding, in any of the forms the language admits.

    Both spellings of each construct are generated — the tagged form people
    write and the desugared form tooling emits — so the properties quantify
    over the language, not over the half of it the tests happened to spell.
    """
    name = draw(identifiers) if name is None else name
    form = draw(st.sampled_from(['ii', 'anchor', 'alias', 'cwl', 'bare',
                                 'ii_desugared', 'anchor_desugared', 'alias_desugared', 'cwl_desugared']))
    match form:
        case 'ii':
            return f'{indent}{name}: !ii {draw(opaque_payload_texts)}'
        case 'anchor':
            return f'{indent}{name}: !& {draw(identifiers)}'
        case 'alias':
            return f'{indent}{name}: !* {draw(identifiers)}'
        case 'cwl':
            return f'{indent}{name}: !cwl {draw(identifiers)}/{draw(identifiers)}'
        case 'ii_desugared':
            return f'{indent}{name}: {{wic_inline_input: {draw(desugared_payload_texts)}}}'
        case 'anchor_desugared':
            return f'{indent}{name}: {{wic_anchor: {draw(identifiers)}}}'
        case 'alias_desugared':
            return f'{indent}{name}: {{wic_alias: {draw(identifiers)}}}'
        case 'cwl_desugared':
            return f'{indent}{name}: {{wic_raw_cwl: {draw(identifiers)}/{draw(identifiers)}}}'
        case _:
            return f'{indent}{name}: {draw(identifiers)}'


@st.composite
def _out_lines(draw: st.DrawFn, indent: str) -> list[str]:
    """An `out:` block: bare names and `!&`-bound names, in both spellings."""
    lines = [f'{indent}out:']
    for _ in range(draw(st.integers(min_value=1, max_value=2))):
        name = draw(identifiers)
        match draw(st.sampled_from(['bare', 'edge', 'edge_desugared'])):
            case 'bare':
                lines.append(f'{indent}- {name}')
            case 'edge':
                lines.append(f'{indent}- {name}: !& {draw(identifiers)}')
            case _:
                lines.append(f'{indent}- {name}: {{wic_anchor: {draw(identifiers)}}}')
    return lines


@st.composite
def documents(draw: st.DrawFn) -> str:
    """A syntactically well-formed Sophios document.

    Generates both step surface forms (mapping and sequence-with-id), inputs
    in every admitted spelling, `out:` blocks, and nested `wic: steps:`
    sidecars. Review of this PR found the previous generator quantified over
    mapping-form `in:`-only documents, leaving outputs, sequence steps, and
    depth-2 sidecars structurally invisible to every property fed by it.
    """
    lines = ['steps:']
    sequence_form = draw(st.booleans())
    names = draw(st.lists(identifiers, min_size=1, max_size=4, unique=True))
    for step in names:
        if sequence_form:
            lines.append(f'- id: {step}')
            body_indent = '  '
        else:
            lines.append(f'  {step}:')
            body_indent = '    '
        lines.append(f'{body_indent}in:')
        # Unique: binding the same input twice is a diagnosed error, not a
        # well-formed document (see wic010).
        for name in draw(st.lists(identifiers, min_size=1, max_size=3, unique=True)):
            lines.append(draw(input_lines(name, indent=body_indent + '  ')))
        if draw(st.booleans()):
            lines.extend(draw(_out_lines(body_indent)))
    if draw(st.booleans()):
        lines += ['wic:', '  graphviz:', f'    label: {draw(identifiers)}']
        if draw(st.booleans()):
            # A nested sidecar: the (index, name) key wraps a wic: block.
            lines += ['  steps:', f'    (1, {names[0]}):', '      wic:',
                      '        steps:', f'          (1, {draw(identifiers)}):',
                      f'            label: {draw(identifiers)}']
    return '\n'.join(lines) + '\n'


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
def test_p04_parsing_is_total_for_arbitrary_text(text: str) -> None:
    """P04: parsing never raises, whatever it is handed."""
    result = parse(text, 'fuzz.wic')
    assert result.document is not None or len(result.diagnostics) > 0


@pytest.mark.fast
@given(documents())
@FAST
def test_p04_well_formed_documents_parse(source: str) -> None:
    """P04: a syntactically valid document yields a document and no errors."""
    result = parse(source, 'gen.wic')
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
@given(documents())
@FAST
def test_p03_every_node_carries_a_resolvable_span(source: str) -> None:
    """P03: every span in the tree points at real source."""
    result = parse(source, 'gen.wic')
    assert result.document is not None
    spans = _spans(result.document)
    assert spans, 'no spans collected; the walk is not reaching the tree'
    for span in spans:
        assert _resolvable(span, source), f'unresolvable span {span}'


@pytest.mark.fast
@given(documents())
@FAST
def test_p05_input_values_are_closed(source: str) -> None:
    """P05: every input value is one of the five declared forms."""
    permitted = get_args(InputValue)
    result = parse(source, 'gen.wic')
    assert result.document is not None
    for step in result.document.steps:
        for name, value in step.inputs:
            assert isinstance(value, permitted), f'{name} produced {type(value).__name__}'


@pytest.mark.fast
@given(st.text(max_size=400))
@FAST
def test_p06_diagnostic_spans_index_real_source(text: str) -> None:
    """P06: a diagnostic never points outside the document it describes."""
    for diagnostic in parse(text, 'fuzz.wic').diagnostics:
        assert diagnostic.span.file == 'fuzz.wic'
        assert diagnostic.span.start_line >= 1
        assert diagnostic.span.start_column >= 1
        assert diagnostic.span.start_line <= max(len(text.splitlines()), 1) + 1


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_p07_corpus_files_parse(path: Path) -> None:
    """P07: every `.wic` in the repository corpus parses without error.

    This is what makes "the specification accepts what exists today"
    falsifiable rather than an aspiration.
    """
    source = path.read_text(encoding='utf-8')
    result = parse(source, str(path))
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
def test_corpus_is_not_empty() -> None:
    """Discovery must find workflows, or P07 passes vacuously.

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


@pytest.mark.fast
def test_tags_map_to_their_forms() -> None:
    """Each surface tag produces its corresponding AST node."""
    source = (
        'steps:\n  s:\n    in:\n'
        '      a: !ii 5\n'
        '      b: !& anchor_name\n'
        '      c: !* anchor_name\n'
        '      d: !cwl other/out\n'
        '      e: plain\n'
    )
    document = parse(source, 't.wic').document
    assert document is not None
    step = document.steps[0]
    literal = step.input('a')
    assert isinstance(literal, InlineLiteral) and literal.value == 5
    assert isinstance(step.input('b'), EdgeDef)
    assert isinstance(step.input('c'), EdgeRef)
    assert isinstance(step.input('d'), RawCwlRef)
    assert isinstance(step.input('e'), UnresolvedName)


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
def test_wic_step_keys_are_normalised() -> None:
    """`"(1, name)"` sidecar keys become structured, not strings."""
    doc = parse('wic:\n  steps:\n    (1, alpha):\n      wic:\n        graphviz: {}\n', 'w.wic').document
    assert doc is not None and doc.sidecar is not None
    key, _ = doc.sidecar.steps[0]
    assert (key.index, key.name) == (1, 'alpha')


@pytest.mark.fast
def test_bare_wic_is_an_empty_sidecar_not_an_error() -> None:
    """A `wic:` key with nothing under it is well-formed.

    This is the shape that used to crash the CLI with a raw traceback.
    """
    result = parse('wic:\nsteps:\n  s:\n    in:\n      a: !ii 1\n', 'b.wic')
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None and result.document.sidecar is not None


@pytest.mark.fast
def test_malformed_yaml_reports_a_located_diagnostic() -> None:
    """Broken YAML yields a diagnostic with a position, not an exception."""
    result = parse('steps:\n  - [unclosed\n', 'bad.wic')
    assert result.document is None
    assert len(result.diagnostics) == 1
    first: Diagnostic = result.diagnostics[0]
    # pylint: disable-next=no-member  # pylint picks the slice overload for [0]
    assert first.span is not None and first.span.start_line >= 1


@pytest.mark.fast
def test_repeated_input_is_reported_not_silently_resolved() -> None:
    """An input bound twice is an error naming the input (§4.2).

    YAML leaves repeated keys undefined, so keeping either binding would mean
    choosing silently on the writer's behalf.
    """
    result = parse('steps:\n- id: s\n  in:\n    f: !ii a\n    f: !ii b\n', 'dup.wic')

    assert [d.code for d in result.diagnostics] == [Code.DUPLICATE_KEY]
    assert "'f'" in result.diagnostics[0].message
    assert result.diagnostics[0].span.start_line == 5


@pytest.mark.fast
def test_passthrough_keys_are_retained() -> None:
    """Top-level keys the language does not interpret survive parsing."""
    doc = parse('$namespaces:\n  edam: http://example\nsteps:\n  s: {}\n', 'p.wic').document
    assert doc is not None
    assert dict(doc.passthrough)['$namespaces'] == {'edam': 'http://example'}


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
def test_parser_is_not_more_permissive_than_the_loader(text: str) -> None:
    """Any document the parser accepts without diagnostics, the loader loads.

    The reverse is allowed — the parser recovers where the loader raises — but
    this direction is the specification's half of the bargain.
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
def test_collection_keys_are_reported_not_stringified() -> None:
    """`? [a, b]` as a step key is a diagnostic, not a repr of node objects."""
    result = parse('steps:\n  ? [a, b]\n  : {}\n', 'x.wic')
    assert any(d.code is Code.EXPECTED_SCALAR for d in result.diagnostics)


@pytest.mark.fast
def test_untagged_collection_inputs_are_inline_literals() -> None:
    """An untagged mapping in input position is `!ii` by definition (§4.1)."""
    result = parse('steps:\n- id: s\n  in:\n    a: {some: mapping}\n', 'x.wic')
    assert result.ok and result.document is not None
    value = result.document.steps[0].inputs[0][1]
    assert isinstance(value, InlineLiteral)
    assert value.value == {'some': 'mapping'}


@pytest.mark.fast
def test_diagnostics_slices_are_diagnostics() -> None:
    """Slicing honours the Sequence contract instead of degrading to list."""
    result = parse('steps:\n- id: s\n  in:\n    a: !foo x\n    b: !bar y\n', 'x.wic')
    assert isinstance(result.diagnostics[0:1], Diagnostics)
    assert len(result.diagnostics[0:1]) == 1

# --------------------------------------------------------------------------
# Offline review round two (P1, P2, P3, P5, N1)
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_sidecar_nesting_is_normalised_at_every_depth() -> None:
    """P1: `(index, name)` keys are StepKeys at depth two, not opaque strings.

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
    """P2: `!cwl` is registered with the loader like its three siblings.

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
def test_p04_alias_expansion_is_bounded() -> None:
    """P3: a widening alias chain is cut off by budget, quickly, once."""
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
    """P5: a non-!& out value is reported; both edge spellings are honoured."""
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
# RecursionError and widening chains exhausted memory while P04 stayed green.
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
def test_p04_parsing_is_total_for_adversarial_structure(text: str) -> None:
    """P04, the half the review proved missing: totality over real YAML
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


#: One attack per code. A member missing here fails the meta-test below, so a
#: diagnostic cannot be declared without the input that fires it — the dead
#: UNKNOWN_TAG of the #382 cycle becomes structurally impossible.
PROVOCATIONS: dict[Code, str] = {
    Code.INVALID_YAML: 'steps:\n  - [unclosed\n',
    Code.NOT_A_MAPPING: '- just\n- a list\n',
    Code.EXPECTED_MAPPING: 'steps: 3\n',
    Code.EXPECTED_SEQUENCE: 'steps:\n- id: s\n  out: 3\n',
    Code.EXPECTED_SCALAR: 'steps:\n  ? [a, b]\n  : {}\n',
    Code.MISSING_STEP_ID: 'steps:\n- {a: 1, b: 2}\n',
    Code.EMPTY_STEP_ID: "steps:\n- id: ''\n",
    Code.MALFORMED_WIC_STEP_KEY: 'wic:\n  steps:\n    nope:\n      x: 1\n',
    Code.UNKNOWN_TAG: 'top: !foo bar\n',
    Code.DUPLICATE_KEY: 'steps:\n- id: s\n  in:\n    f: !ii a\n    f: !ii b\n',
    Code.RECURSIVE_ALIAS: 'top: &a [*a]\n',
}


@pytest.mark.fast
@pytest.mark.parametrize('code', list(Code), ids=lambda c: c.name)
def test_every_code_is_provocable(code: Code) -> None:
    """Each declared diagnostic has a registered input that fires it."""
    assert code in PROVOCATIONS, f'{code.name} has no registered provocation — dead on arrival'
    result = parse(PROVOCATIONS[code], 'provoke.wic')
    assert any(d.code is code for d in result.diagnostics), f'{code.name} did not fire'


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
