"""The Sophios renderer inverts parsing: a document reads the same once rendered, rendering settles at once, a value
built in Python gets a spelling that reads back as itself, and a document renders as tests/core/data/render states."""
import json
import math
from typing import Any

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st

from sophios.lang import Document, InlineLiteral, Step, parse, to_json
from sophios.lang.render import render
from sophios.lang.spans import SourceSpan

from .budgets import FAST, budget
from .cases import DATA
from .strategies import documents, identifiers, pinned, tools
from .test_lang_parser import reading
from .wic_reading import YamlWithSophiosTagsOpaque

# Literals are drawn as YAML source text rather than converted from Python values: writing the spelling out keeps
# the awkward cases visible, and leaves the test no quoting logic of its own to agree with the renderer's. Included on
# purpose: the falsy values a naive `x or ''` would destroy, and the characters that force the renderer off plain style.
literal_texts = st.sampled_from([
    '0', '1', '-42', '3.5', 'true', 'false', 'null', 'plain_word', 'a/path/to.txt', "''", "'0'", "'false'",
    "'hello world'", "'has: colon'", "'#hash'", "'-dash'", "'{brace}'", "'has ''quote'''", "'trailing '''",
])


@st.composite
def awkward_literal_documents(draw: st.DrawFn) -> str:
    """Documents whose `!ii` payloads are the renderer's hard cases, to hammer quoting."""
    lines = ['steps:']
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        lines += [f'- id: {draw(identifiers)}', '  in:']
        for name in draw(st.lists(identifiers, min_size=1, max_size=3, unique=True)):
            lines.append(f'    {name}: !ii {draw(literal_texts)}')
    return '\n'.join(lines) + '\n'


@pytest.mark.fast
@given(st.one_of(documents(), awkward_literal_documents(), tools()))
# Hypothesis's failure database does not travel to CI, so what it once found is pinned.
@example('')                                                      # nothing at all
@example('steps:\n- id: t\n  in:\n    f: !ii {a: !* e}\n')      # a construct inside a collection literal
@example("steps:\n- id: t\n  in:\n    f: {wic_inline_input: '0'}\n")     # text that spells a number
@example('steps:\n- id: t\n  in:\n    f: !ii .nan\n')           # NaN, which equals nothing
@example('steps:\n- id: t\n  in:\n    f: !cwl {source: [a], linkMerge: merge_nested}\n')  # one source, still a list
@FAST
def test_a_rendered_document_reads_the_same_and_renders_the_same(source: str) -> None:
    """Parsing a rendered document reproduces the document, and rendering it again reproduces the text: a formatter
    that never settled would churn every file it touched. The rendered text is also YAML that `yaml.SafeLoader` loads
    with the Sophios tags opaque, since a renderer writing what YAML rejects would be writing a dialect. A tool's
    command typed as a line stays a line, and its projection to JSON reads as the same tool."""
    first = parse(source, 'a.wic')
    written = first.document or first.tool
    assert not first.diagnostics.has_errors and written is not None, [str(d) for d in first.diagnostics]
    rendered = render(written)
    second = parse(rendered, 'b.wic')
    again = second.document or second.tool
    assert not second.diagnostics.has_errors and again is not None, [str(d) for d in second.diagnostics]
    assert reading(again) == reading(written)
    assert render(again) == rendered
    loaded = yaml.load(rendered, Loader=YamlWithSophiosTagsOpaque)
    if first.tool is not None:
        if 'command' in loaded:
            assert isinstance(loaded['command'], str) or not isinstance(yaml.safe_load(source)['command'], str)
        assert reading(parse(json.dumps(to_json(written)), 'c.wic').tool) == reading(written)


#: A value the renderer must spell as an `!ii` payload, as the Python API hands it over: no source text behind it.
#: A parsed literal remembers how it was written and is transcribed verbatim, so the round trip above never reaches
#: the code that has to *choose* a spelling.
synthesized = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
                           lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3),
                           max_leaves=6)


@pytest.mark.fast
@given(synthesized)
# A NaN is the one value not equal to itself; an infinity; and strings a plain spelling would not read back as
# themselves, since YAML types the first row, cannot scan the second plain, as a scalar or at all, and would read
# the third as a structure: `!ii 0` is the number 0, `!ii '0'` the text.
@pinned(math.nan, math.inf, 'plain', '0', '007', '1:30', 'true', 'null', '~', '', '@', '*', '%', '&', ',', ':', '...',
        '#x', 'a: b', '{', '[')
@budget(100)
def test_a_synthesized_literal_gets_a_spelling_that_survives(value: Any) -> None:
    """The renderer picks a tagged spelling that YAML loads, and reparsing it returns the value, of its type."""
    span = SourceSpan('synth.wic', 1, 1, 1, 1)
    text = render(Document(steps=(Step(id='s', inputs=(('a', InlineLiteral(value, span)),)),)))
    assert '!ii' in text, text
    yaml.load(text, Loader=YamlWithSophiosTagsOpaque)
    result = parse(text, 'synth.wic')
    assert not result.diagnostics.has_errors and result.document is not None, [str(d) for d in result.diagnostics]
    reparsed = dict(result.document.steps[0].inputs).get('a')
    assert isinstance(reparsed, InlineLiteral) and repr(reparsed.value) == repr(value), text


@pytest.mark.fast
def test_a_document_renders_as_stated() -> None:
    """tests/core/data/render/written.wic, in every spelling, renders as rendered.wic, byte for byte: each construct
    in the tagged spelling the .wic reference tells people to write; each literal transcribed as it was spelled, since
    `!ii 0777` is 511 and `!ii '1:30'` text where `!ii 1:30` is the number 90; each step's `wic:` block in the wrapper
    it was read through, which the parser tolerates the loss of, so no round trip sees it go; and an empty one as `{}`,
    since a null sails past every `.get(key, {})` downstream. rendered.wic reads as written.wic and renders as itself;
    and nothing renders as nothing."""
    rendered = (DATA / 'render' / 'rendered.wic').read_text(encoding='utf-8')
    written, again = (parse((DATA / 'render' / name).read_text(encoding='utf-8'), name).document
                      for name in ('written.wic', 'rendered.wic'))
    assert written is not None and again is not None
    assert render(written) == rendered and render(again) == rendered and reading(again) == reading(written)
    nothing = parse('', 'empty.wic').document
    assert nothing is not None and render(nothing) == ''
