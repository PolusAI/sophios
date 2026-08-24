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
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sophios.lang import (
    Code,
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

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIRS = (REPO_ROOT / 'docs' / 'tutorials', REPO_ROOT / 'examples')

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
CORPUS = sorted(p for d in CORPUS_DIRS if d.is_dir() for p in d.rglob('*.wic'))


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

identifiers = st.text('abcdefghijklmnopqrstuvwxyz_', min_size=1, max_size=8)
scalars = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.text('abcXYZ 0123', min_size=0, max_size=10),
)


@st.composite
def input_lines(draw: st.DrawFn) -> str:
    """One `in:` binding, in any of the forms the language admits."""
    name = draw(identifiers)
    form = draw(st.sampled_from(['ii', 'anchor', 'alias', 'cwl', 'bare']))
    match form:
        case 'ii':
            return f'      {name}: !ii {draw(scalars)}'
        case 'anchor':
            return f'      {name}: !& {draw(identifiers)}'
        case 'alias':
            return f'      {name}: !* {draw(identifiers)}'
        case 'cwl':
            return f'      {name}: !cwl {draw(identifiers)}/{draw(identifiers)}'
        case _:
            return f'      {name}: {draw(identifiers)}'


@st.composite
def documents(draw: st.DrawFn) -> str:
    """A syntactically well-formed Sophios document, in mapping-form steps."""
    lines = ['steps:']
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        lines.append(f'  {draw(identifiers)}:')
        lines.append('    in:')
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            lines.append(draw(input_lines()))
    if draw(st.booleans()):
        lines += ['wic:', '  graphviz:', f'    label: {draw(identifiers)}']
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
    return 1 <= span.start_column <= len(lines[span.start_line - 1]) + 1


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
@pytest.mark.parametrize('path', CORPUS, ids=lambda p: p.name)
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
    """The corpus scan must find files, or P07 passes vacuously."""
    assert CORPUS, 'no corpus .wic files discovered'


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
    two front-ends have quietly diverged.
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
    assert as_map.steps[0].inputs[0][1].value == as_seq.steps[0].inputs[0][1].value  # type: ignore[union-attr]


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
    assert result.diagnostics[0].span.start_line >= 1


@pytest.mark.fast
def test_passthrough_keys_are_retained() -> None:
    """Top-level keys the language does not interpret survive parsing."""
    doc = parse('$namespaces:\n  edam: http://example\nsteps:\n  s: {}\n', 'p.wic').document
    assert doc is not None
    assert dict(doc.passthrough)['$namespaces'] == {'edam': 'http://example'}


@pytest.mark.fast
def test_python_api_emits_documents_this_parser_accepts() -> None:
    """The Python API is a second front-end over the same language.

    Whatever it emits must parse, or the two front-ends have diverged and the
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
def test_unknown_tag_in_input_position_is_reported() -> None:
    """`!foo bar` as an input value is a diagnostic, not UnresolvedName('bar')."""
    result = parse('steps:\n- id: s\n  in:\n    a: !foo bar\n', 'x.wic')
    assert any(d.code is Code.UNKNOWN_TAG for d in result.diagnostics)
    assert not result.ok


@pytest.mark.fast
def test_unknown_tag_in_passthrough_is_reported() -> None:
    """The tag is not silently dropped from passthrough content either."""
    for source in ('top: !foo bar\n', 'top: !foo {a: 1}\n', 'top: !foo [1, 2]\n'):
        result = parse(source, 'x.wic')
        assert any(d.code is Code.UNKNOWN_TAG for d in result.diagnostics), source


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
    parsed = dict(result.document.passthrough)['top']['k']
    assert parsed == yaml.safe_load(text)


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
    from sophios.lang import Diagnostics
    assert isinstance(result.diagnostics[0:1], Diagnostics)
    assert len(result.diagnostics[0:1]) == 1
