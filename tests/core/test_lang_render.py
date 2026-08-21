"""Properties of the Sophios renderer (CR-101, T1.3).

Rendering is the inverse of parsing. Having both is what makes the syntax layer
checkable, because a round-trip either reproduces the document or proves one of
the two wrong about the language.

Properties covered:
  P02  parse(render(ast)) == ast

See design_docs/core-refactor-design.md, Spec 1.
"""
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sophios.lang import (
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    OutputBinding,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
)
from sophios.lang.render import render

from .wic_corpus import CORPUS, corpus_id

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)


# --------------------------------------------------------------------------
# Span-insensitive comparison
# --------------------------------------------------------------------------
#
# A round-tripped document cannot carry the original spans: rendering produces
# new text, so positions legitimately move. The property is therefore about
# structure, and comparison strips spans rather than pretending they survive.


def _shape(node: Any) -> Any:  # pylint: disable=too-many-return-statements
    """Reduce an AST to its structure, discarding source positions.

    One return per node kind. Collapsing them into a lookup would hide which
    fields each kind contributes, which is the only thing this function says.
    """
    match node:
        case Document():
            return ('doc',
                    tuple(_shape(s) for s in node.steps),
                    _shape(node.sidecar) if node.sidecar is not None else None,
                    tuple((k, _shape(v)) for k, v in node.passthrough))
        case Step():
            return ('step', node.id,
                    tuple((k, _shape(v)) for k, v in node.inputs),
                    tuple(_shape(o) for o in node.outputs),
                    tuple((k, _shape(v)) for k, v in node.interpreted),
                    tuple((k, _shape(v)) for k, v in node.passthrough))
        case OutputBinding():
            return ('out', node.name, _shape(node.edge_def) if node.edge_def else None)
        case WicSidecar():
            return ('wic',
                    tuple(((k.index, k.name), _shape(v)) for k, v in node.steps),
                    tuple((k, _shape(v)) for k, v in node.entries))
        case InlineLiteral():
            return ('lit', _shape(node.value))
        case EdgeDef():
            return ('def', node.name)
        case EdgeRef():
            return ('ref', node.name)
        case RawCwlRef():
            return ('raw', node.expression)
        case UnresolvedName():
            return ('name', node.name)
        case dict():
            return tuple((k, _shape(v)) for k, v in node.items())
        case list():
            return tuple(_shape(v) for v in node)
        case _:
            return node


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

identifiers = st.text('abcdefghijklmnopqrstuvwxyz_', min_size=1, max_size=8)

# Literals are drawn as YAML source text rather than converted from Python
# values: writing the spelling out keeps the awkward cases visible, and leaves
# the test with no quoting logic of its own to agree with the renderer's.
# Included on purpose: the falsy values a naive `x or ''` would destroy, and
# the characters that force the renderer off plain style.
literal_texts = st.sampled_from([
    '0', '1', '-42', '3.5',
    'true', 'false', 'null',
    'plain_word', 'a/path/to.txt',
    "''", "'0'", "'false'",
    "'hello world'", "'has: colon'", "'#hash'", "'-dash'", "'{brace}'",
    "'has ''quote'''", "'trailing '''",
])


@st.composite
def documents(draw: st.DrawFn) -> str:
    """A well-formed Sophios document exercising every input form."""
    lines = ['steps:']
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        lines.append(f'- id: {draw(identifiers)}')
        lines.append('  in:')
        # Unique: binding the same input twice is a diagnosed error, not a
        # well-formed document (see wic010).
        names = draw(st.lists(identifiers, min_size=1, max_size=3, unique=True))
        for name in names:
            match draw(st.sampled_from(['ii', 'anchor', 'alias', 'cwl', 'bare'])):
                case 'ii':
                    lines.append(f'    {name}: !ii {draw(literal_texts)}')
                case 'anchor':
                    lines.append(f'    {name}: !& {draw(identifiers)}')
                case 'alias':
                    lines.append(f'    {name}: !* {draw(identifiers)}')
                case 'cwl':
                    lines.append(f'    {name}: !cwl {draw(identifiers)}/{draw(identifiers)}')
                case _:
                    lines.append(f'    {name}: {draw(identifiers)}')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(documents())
@FAST
def test_p02_round_trip_preserves_structure(source: str) -> None:
    """P02: parsing a rendered document reproduces the document."""
    first = parse(source, 'a.wic')
    assert first.ok and first.document is not None

    second = parse(render(first.document), 'b.wic')
    assert second.ok, [str(d) for d in second.diagnostics]
    assert second.document is not None

    assert _shape(second.document) == _shape(first.document)


@pytest.mark.fast
@given(documents())
@FAST
def test_rendering_is_idempotent(source: str) -> None:
    """Rendering a round-tripped document produces identical text.

    Round-trip stability matters more than matching the original byte for byte:
    a formatter that never settles would churn every file it touched.
    """
    document = parse(source, 'a.wic').document
    assert document is not None

    once = render(document)
    reparsed = parse(once, 'b.wic').document
    assert reparsed is not None
    assert once == render(reparsed)


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_corpus_round_trips(path: Path) -> None:
    """Every corpus file survives a render/parse cycle unchanged in structure."""
    source = path.read_text(encoding='utf-8')
    first = parse(source, str(path))
    assert first.ok and first.document is not None

    second = parse(render(first.document), str(path))
    assert second.ok, [str(d) for d in second.diagnostics]
    assert _shape(second.document) == _shape(first.document)


@pytest.mark.fast
def test_render_emits_the_tagged_spelling() -> None:
    """Output uses the spelling the language reference tells people to write."""
    document = parse('steps:\n- id: s\n  in:\n    f:\n      wic_inline_input: x\n', 'a.wic').document
    assert document is not None
    text = render(document)
    assert '!ii' in text
    assert 'wic_inline_input' not in text


@pytest.mark.fast
def test_render_preserves_the_steps_surface_form() -> None:
    """A mapping-form document does not come back as a sequence, or vice versa."""
    as_map = parse('steps:\n  touch:\n    in:\n      f: !ii x\n', 'm.wic').document
    as_seq = parse('steps:\n- id: touch\n  in:\n    f: !ii x\n', 's.wic').document
    assert as_map is not None and as_seq is not None

    assert render(as_map).startswith('steps:\n  touch:')
    assert render(as_seq).startswith('steps:\n- id: touch')


@pytest.mark.fast
def test_empty_document_renders_empty() -> None:
    """A document carrying nothing renders to nothing, and reparses."""
    document = parse('', 'e.wic').document
    assert document is not None
    assert render(document) == ''
    assert parse(render(document), 'e.wic').ok
