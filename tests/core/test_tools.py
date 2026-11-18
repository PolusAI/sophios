"""A tool written in `.wic`: a document, or a step's inline `run:` body, that says `command:` or `script:`. Each word is
read where it is written. What a tool cannot say is stated, with its place, in tests/core/data/parse/tools."""
import json
from typing import Any

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from sophios.lang import ToolDocument, parse

from .budgets import FAST

#: Input names as a tool may write them: a CWL id holds more than an identifier does.
NAMES = ['v', 'x_1', 'in-file', 'in.file', '1st', 'naïve', 'Sample', 'a+b']
#: Text a word carries unread: what CWL would evaluate, what a shell would read, braces, backslashes.
TEXTS = ['x', 'a b', '|', '$', '-v', '{lit}', '--', '$(x)', '${y}', 'a\\b', 'a\\\\b', '\\$(x)', '*.txt']
#: A type of input, and the values a job may give it.
VALUES = {'string': st.sampled_from(['v', 'two words', '-dash', '$(x)', '{b}']), 'int': st.integers(-5, 5),
          'string?': st.none() | st.just('opt'), 'boolean': st.booleans(),
          'string[]': st.lists(st.sampled_from(['p', 'q r']), max_size=3)}


def _braced(text: str) -> str:
    """`text` as a line writes it: a brace that is text is written twice."""
    return text.replace('{', '{{').replace('}', '}}')


@st.composite  # pylint: disable-next=dangerous-default-value,too-many-locals
def commands(draw: st.DrawFn, names: list[str] = NAMES) -> tuple[
        list[str], list[str] | None, dict[str, Any], dict[str, Any], list[str]]:
    """A command line, as the words of a typed line and as the words of a list (None when a quoted dash word stands
    before a value, which a list cannot say); its inputs, each a type or a port with a binding of its own; a job; and
    the words the line runs for that job. It starts with a program of some words, or with none: an image's entry point
    runs it, and the line starts with a flag, of the entry point's own or of a value. Then come words of text, and
    values, each bare, behind a flag, or bound as its port says."""
    program = draw(st.lists(st.sampled_from(['grep', 'wc', 'python3']), max_size=2))
    program += draw(st.lists(st.just('--verbose'), max_size=int(not program)))
    typed, listed, argv = [*program], [*program], [*program]
    ports: dict[str, Any] = {}
    job: dict[str, Any] = {}
    sayable = True
    for name in draw(st.lists(st.sampled_from(names), min_size=int(not program), max_size=4, unique=True)):
        kind = draw(st.sampled_from(sorted(VALUES)))
        flag = draw(st.lists(st.sampled_from(['-n', '--out', '-k']), max_size=1, min_size=int(
            kind == 'boolean' or typed[:1] in ([], ['--verbose']) and not ports)))
        prefix = draw(st.sampled_from(['', '--p=', '-p'])) if not flag and kind != 'string[]' else ''
        for text in draw(st.lists(st.sampled_from(TEXTS), max_size=int(typed[:1] not in ([], ['--verbose'])))):
            typed, listed, argv = [*typed, f"'{_braced(text)}'"], [*listed, _braced(text)], [*argv, text]
            sayable = sayable and (bool(flag) or not text.strip('-') or text[0] != '-')
        ports[name] = {'type': kind, 'inputBinding': {'prefix': prefix, 'separate': not prefix.endswith('=')}} \
            if prefix else kind
        job[name] = value = draw(VALUES[kind])
        typed, listed = [*typed, *flag, f'{{{name}}}'], [*listed, *flag, f'{{{name}}}']
        values = [] if value is None else value if isinstance(value, list) else [value]
        if kind == 'boolean':
            argv += flag * bool(value)
        elif prefix.endswith('='):
            argv += [f'{prefix}{each}' for each in values]
        elif values:
            argv += [*flag, *[prefix] * bool(prefix), *map(str, values)]
    return typed, listed if sayable else None, ports, job, argv


#: Each way YAML writes a command line, as the text of its `command:`: plain, quoted either way, a block kept or
#: folded, and a list of the words.
_SPELLINGS = {
    'plain': ' '.join, 'single': lambda line: "'" + ' '.join(line).replace("'", "''") + "'",
    'double': lambda line: json.dumps(' '.join(line), ensure_ascii=False),
    'kept': lambda line: '|\n  ' + '\n  '.join(line), 'folded': lambda line: '>-\n  ' + '\n  '.join(line),
    'list': lambda line: json.dumps(line, ensure_ascii=False)}
#: What YAML rewrites in a scalar of each spelling that is read as it is written otherwise; a block is never.
_REWRITTEN = {'plain': '', 'single': "'", 'double': '"\\', 'list': '"\\'}


@pytest.mark.fast
@given(commands(), st.sampled_from(sorted(_SPELLINGS)), st.booleans(), st.booleans())
@example((['grep', "'a\\\\b'", '-n', '{v}', "'a\\b'", '-n', '{x_1}'], None, {'v': 'boolean', 'x_1': 'boolean'}, {}, []),
         'double', False, False)  # an escaped word reads as a later word is written
@FAST  # pylint: disable-next=too-many-locals
def test_a_word_is_read_where_it_is_written(case: tuple[list[str], list[str] | None, Any, Any, Any],
                                            spelling: str, inline: bool, listed_ports: bool) -> None:
    """Each word of a command line, and each placeholder of an output line, carries the place it is written at: the
    text at its span is the word. That holds in a document and in a step's inline body, with the ports as a mapping
    or as CWL's list, for an output's `from:` beside a `doc:` that repeats its placeholder, and wherever YAML reads
    the scalar as it is written: plain, between quotes, or a word of a list. In a scalar YAML rewrites (a block, a
    folded line, a quote doubled, a backslash escaped) no word is given a place that may be another's: each stands
    at the scalar it is in."""
    typed, listed, ports, _job, _argv_ = case
    line = listed if spelling == 'list' else typed
    # A list cannot say a quoted dash word, and a plain scalar cannot start with a brace or a quote.
    assume(line is not None)
    assert line is not None
    assume(not (spelling == 'plain' and line[0][0] in '{\''))
    named = [f'{{{name}}}' for name in [*ports][:1]]
    outputs = {f'o{index}': {'doc': f'the {name} file', 'from': f'x_{name}.txt'} for index, name in enumerate(named)}
    body = f'command: {_SPELLINGS[spelling](line)}\n' + ''.join(
        f'{key}: {json.dumps(value, ensure_ascii=False)}\n' for key, value in {
            'inputs': [{'id': name, 'type': 'string'} for name in ports] if listed_ports
            else dict.fromkeys(ports, 'string'),
            'outputs': [{'id': name, **port} for name, port in outputs.items()] if listed_ports else outputs}.items())
    source = 'steps:\n- id: s\n  run:\n' + ''.join(f'    {row}\n' for row in body.splitlines()) if inline else body
    parsed = parse(source, 'placed.wic')
    tool = parsed.document.steps[0].tool if parsed.document else parsed.tool
    assert isinstance(tool, ToolDocument) and tool.command is not None and not parsed.diagnostics, [
        str(found) for found in parsed.diagnostics]
    rows = source.splitlines()
    # Each word with the text of the scalar it is in, and what YAML rewrites there: an output line is JSON's.
    written = [*((word, text, text if spelling == 'list' else ' '.join(line), _REWRITTEN.get(spelling))
                 for word, text in zip(tool.command, line)),
               *((part, name, name, '"\\') for port, name in zip(tool.outputs, named)
                 for part in port.line or () if part.input is not None)]
    for word, text, scalar, rewritten in written:
        assert word.span is not None
        at = rows[word.span.start_line - 1][word.span.start_column - 1:word.span.end_column - 1]
        if rewritten is not None and not any(mark in scalar for mark in rewritten):
            assert at == text, (text, at, word.span)
        elif spelling == 'list':
            assert json.loads(at) == text, (text, at, word.span)
        else:
            starts = rows[word.span.start_line - 1][word.span.start_column - 1:][:1]
            assert (word.span.start_line, starts) == (4 if inline else 1, _SPELLINGS[spelling](line)[:1]), (
                text, at, word.span)
