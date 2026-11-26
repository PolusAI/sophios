"""A command word, an output's glob and what is made of its file are Sophios's own kinds; any other text is unread."""
from typing import Any

import pytest

from sophios.lang.cwl_tool import read_text, read_word, write_word
from sophios.lang.tools import Collect, Computed, Part, Reference, Word
from sophios.wic_types import Tool

_FILE = Reference('f', Part.PATH)

_WORDS = (
    ('plain', Word(('plain',))), ('', Word(('',))), ('a\\b', Word(('a\\b',))), ('a$b', Word(('a$b',))),
    ('$(inputs.n)', Word(('', Reference('n'), ''))), (' $(inputs.n) ', Word(('', Reference('n'), ''))),
    ('--$(inputs.f.path)=$(inputs.g.basename)',
     Word(('--', _FILE, '=', Reference('g', Part.BASENAME), ''))),
    ('\\$(inputs.n)', Word(('', '$(', 'inputs.n)'))),
    # an expression CWL would evaluate, and text no program takes, hold no word
    ('$(inputs.n.length)', None), ('$(inputs["n"])', None), ('${ return 1; }', None), ('a\x00b', None),
)


@pytest.mark.fast
@pytest.mark.parametrize('text, word', _WORDS)
def test_a_cwl_string_is_literal_text_and_references_or_it_holds_no_word(text: str, word: Word | None) -> None:
    """The text cwltool interpolates is read as its literal parts and the references it names, and any other text,
    which a target cannot lower, is none."""
    assert read_word(text) == word


@pytest.mark.fast
@pytest.mark.parametrize('word, text', [
    (Word(('echo',)), 'echo'), (Word(('a\\b',)), 'a\\b'), (Word(('$(x)',)), '\\$(x)'),
    (Word(('', Reference('n'), '')), '$(inputs.n)'), (Word(('', Reference('f', Part.BASENAME), '.idx')),
                                                      '$(inputs.f.basename).idx'),
])
def test_a_word_is_written_as_cwl_reads_it_back(word: Word, text: str) -> None:
    """Literal text is escaped only where the word holds an expression or a reference, as cwltool unescapes it."""
    assert write_word(word) == text


def _tool(binding: dict[str, Any], output: dict[str, Any]) -> Tool:
    return Tool('t.cwl', {'class': 'CommandLineTool', 'cwlVersion': 'v1.2', 'baseCommand': 'x',
                          'inputs': {'n': {'type': 'string', 'inputBinding': binding}},
                          'outputs': {'o': {'type': 'File', 'outputBinding': output}}})


@pytest.mark.fast
def test_a_binding_and_an_output_binding_are_read_into_words_and_kinds() -> None:
    """What a `valueFrom` says is a word, what an output's glob says is a word and what its `outputEval` says is the
    first match or the text; anything else is kept as written, unread, beside the field that holds none of it."""
    body = _tool({'valueFrom': '$(inputs.n)'}, {'glob': '$(inputs.n).txt', 'outputEval': ' $(self[0].contents) '}
                 ).node.body()
    placement, capture = body.inputs[0].placement, body.outputs[0].capture
    assert placement is not None and placement.value_from == Word(('', Reference('n'), ''))
    assert capture is not None and capture.glob == Word(('', Reference('n'), '.txt')) \
        and capture.output_eval is Collect.TEXT and (capture.unread_glob, capture.unread_eval) == (None, None)

    body = _tool({'valueFrom': '$(inputs.n.length)'}, {'glob': ['a', 'b'], 'outputEval': '$(self[0].dirname)'}
                 ).node.body()
    placement, capture = body.inputs[0].placement, body.outputs[0].capture
    assert placement is not None and placement.value_from == Computed('$(inputs.n.length)', True)
    assert capture is not None and (capture.glob, capture.output_eval) == (None, None) \
        and (capture.unread_glob, capture.unread_eval) == (['a', 'b'], '$(self[0].dirname)') \
        and capture.eval_reason is not None


@pytest.mark.fast
def test_text_no_word_holds_is_kept_as_written_with_the_inputs_it_names() -> None:
    """An expression, or text no program takes, is `Computed`: as written, flagged an expression or not, and with
    every input it refers to by `$(inputs.name)`, wherever in it."""
    assert read_text('$(inputs.n) $(inputs.n.length)') == Computed(
        '$(inputs.n) $(inputs.n.length)', True, (Reference('n'),))
    assert read_text('a\x00b') == Computed('a\x00b', False)
    assert read_text('$(inputs.f.path)') == Word(('', _FILE, ''))
