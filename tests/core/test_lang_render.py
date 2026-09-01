"""Properties of the Sophios renderer.

Rendering is the inverse of parsing. Having both is what makes the syntax layer
checkable, because a round-trip either reproduces the document or proves one of
the two wrong about the language.

Properties covered:
  parse(render(ast)) == ast

See design_docs/core-refactor-design.md, Spec 1.
"""
import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, example, given, settings
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
from sophios.utils_yaml import wic_loader

from .strategies import documents, scalar_payload_texts

from .wic_corpus import CORPUS, corpus_id

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)

#: The spellings whose source text IS the scalar's content — every payload
#: above except the quoted ones, where the quotes belong to the YAML syntax
#: rather than to the scalar, and the tagged form cannot carry them at all
#: (the string '0' is the standing example; it renders desugared).
unquoted_spellings = scalar_payload_texts.filter(lambda text: not text.startswith("'"))


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
        case float() if math.isnan(node):
            # NaN != NaN would fail the comparison exactly when the round
            # trip is correct; normalise to a sentinel before comparing.
            return 'NaN'
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
def awkward_literal_documents(draw: st.DrawFn) -> str:
    """Documents whose `!ii` payloads are the renderer's hard cases.

    A narrow, deliberate complement to the shared `documents()` strategy: it
    exists only to hammer quoting. The structural round-trip quantifies over
    the full language via the parser suite's generator — a second, narrower
    `documents()` here is exactly how outputs and nested sidecars once became
    invisible to the round-trip property (see the PR #382 review cycle).
    """
    lines = ['steps:']
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        lines.append(f'- id: {draw(identifiers)}')
        lines.append('  in:')
        for name in draw(st.lists(identifiers, min_size=1, max_size=3, unique=True)):
            lines.append(f'    {name}: !ii {draw(literal_texts)}')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(st.one_of(documents(), awkward_literal_documents()))
# The #383 review's counterexamples, pinned deterministically (Hypothesis's
# failure database does not travel to CI). Duplicate-key cases are absent
# because they are parse errors now, pinned on the parser's provocations.
@example('steps:\n- t:\n    in:\n      f: !ii {a: !* e}\n')      # construct inside collection literal
@example('steps:\n- t: {}\nmeta: !ii {a: 1}\n')                   # collection literal in passthrough
@example("steps:\n- t:\n    in:\n      f: {wic_inline_input: '0'}\n")     # string that spells an int
@example("steps:\n- t:\n    in:\n      f: {wic_inline_input: 'true'}\n")  # string that spells a bool
@example('steps:\n- t:\n    in:\n      f: !ii 1.0e+300\n')       # exponent float
@example('steps:\n- t:\n    in:\n      f: !ii .inf\n')           # YAML float special
@example('steps:\n- t:\n    in:\n      f: !ii .nan\n')           # NaN, comparator-normalised
@example('wic:\n  steps:\n    (1, o):\n      wic:\n        namespace: dna\n')  # nested wrapper
@example('wic:\n  steps:\n    (1, o):\n      wic: {}\n')          # empty child sidecar: {} not null
@FAST
def test_round_trip_preserves_structure(source: str) -> None:
    """Parsing a rendered document reproduces the document.

    Quantified over the shared full-language generator (both step forms,
    both spellings, outputs, nested sidecars) plus the quoting-hostile
    literals — and the rendered text must also load through `wic_loader`,
    since a renderer that emits what the loader rejects would be writing a
    dialect (the differential-oracle lesson from the #382 cycle).
    """
    first = parse(source, 'a.wic')
    assert first.ok and first.document is not None

    rendered = render(first.document)
    second = parse(rendered, 'b.wic')
    assert second.ok, [str(d) for d in second.diagnostics]
    assert second.document is not None

    assert _shape(second.document) == _shape(first.document)
    if rendered:
        yaml.load(rendered, Loader=wic_loader())  # the loader agrees too


@pytest.mark.fast
@given(st.one_of(documents(), awkward_literal_documents()))
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


@pytest.mark.fast
def test_nested_sidecar_wrapper_survives_rendering() -> None:
    """The renderer re-wraps what the parser unwraps — asymmetry test.

    The round-trip property is structurally blind here: the parser tolerates
    the missing wrapper
    in the renderer's own output, so the round-trip holds even when rendering
    is a semantic edit. Every downstream consumer reads through the wrapper
    explicitly, which is why its loss changes what a workflow means.
    """
    source = 'wic:\n  steps:\n    (1, o):\n      wic:\n        namespace: dna\n'
    document = parse(source, 'x.wic').document
    assert document is not None
    rendered = render(document)
    assert rendered == source  # byte-exact, wrapper included


@pytest.mark.fast
def test_empty_sidecar_renders_as_mapping_not_null() -> None:
    """`{}` — a present-None sails past every `.get(key, {})` downstream."""
    document = parse('wic:\n  steps:\n    (1, o):\n      wic: {}\n', 'x.wic').document
    assert document is not None
    rendered = render(document)
    assert 'null' not in rendered and '~' not in rendered


# --------------------------------------------------------------------------
# Transcription: a parsed literal is emitted as it was written
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(unquoted_spellings)
@FAST
@example('0777')   # octal: value 511 knows nothing of how it was spelled
@example('1.50')   # trailing zero
@example('yes')    # YAML 1.1 boolean
@example('0x1f')   # hexadecimal
@example('1:30')   # sexagesimal
def test_a_tagged_literal_keeps_its_source_spelling(spelling: str) -> None:
    """`!ii <spelling>` renders back as `!ii <spelling>`, character for
    character.

    This is the renderer's founding claim — transcription, not
    reconstruction — and the only claim that distinguishes the two. The
    round-trip property cannot: it compares parsed values, and every spelling
    here parses to a value that re-serialises to *some* valid spelling, just
    not the one the author wrote. So when the parser silently stopped
    recording the source text, every property stayed green while `!ii 0777`
    began compiling as `!ii 511`.
    """
    source = f'steps:\n- id: s\n  in:\n    f: !ii {spelling}\n'
    result = parse(source, 'transcribe.wic')
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None

    payload = render(result.document).split('!ii ', 1)[1].rstrip('\n')
    # Up to quoting, which the emitter adds when a plain scalar would be
    # unsafe (`1:30` carries a colon). Quoting a tagged payload changes
    # nothing: the composer strips the quotes before the content is
    # re-resolved, which is why `!ii '1:30'` and `!ii 1:30` mean the same
    # thing and why neither can be confused with the reconstructed `!ii 90`.
    assert payload in (spelling, f"'{spelling}'"), f'{spelling!r} came back as {payload!r}'


@pytest.mark.fast
@given(unquoted_spellings)
@FAST
def test_a_parsed_literal_records_the_text_it_came_from(spelling: str) -> None:
    """The mechanism behind the claim above: a literal parsed from tagged
    YAML carries its spelling. Stated separately so a regression names the
    cause rather than only the symptom."""
    source = f'steps:\n- id: s\n  in:\n    f: !ii {spelling}\n'
    document = parse(source, 'transcribe.wic').document
    assert document is not None
    literal = dict(document.steps[0].inputs)['f']
    assert isinstance(literal, InlineLiteral)
    assert literal.text == spelling
