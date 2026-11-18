"""A tool written in `.wic`: a document, or a step's inline `run:` body, that says `command:` or `script:`. Each word is
read where it is written; a command line means its words, and an output line its file, under cwltool's own
evaluators; a script is run as written; a line adds CWL and changes none; and a tool is one tool through every door
that reaches it. What a tool cannot say is stated, with its place, in tests/core/data/parse/tools, and what each means
in tests/core/data/tool_lowering.yaml."""
import inspect
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from cwl_utils.expression import do_eval
from hypothesis import assume, example, given
from hypothesis import strategies as st

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs
from sophios.api.python.workflow import Step, Workflow
from sophios.compiler import CompileSettings, compile_source
from sophios.input_output import dump_wic_yaml
from sophios.ir.frontdoor import bundle_from_disk
from sophios.lang import SophiosErrorCode, ToolDocument, parse
from sophios.lang.cwl import canonical_type
from sophios.lang.diagnostics import SophiosError
from sophios.plugins import get_tools_cwl
from sophios.python_cwl_adapter import generate_CWL_CommandLineTool, import_python_file
from sophios.utils_cwl import make_tool, validate

from .budgets import FAST, budget
from .cases import cases
from .helpers import REPO_ROOT
from .hermetic import cwl_tree
from .strategies import pinned

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


def _tool(document: dict[str, Any]) -> dict[str, Any]:
    """The CommandLineTool the tool `document` means when it is written as `.wic`, which the parser reads without a
    diagnostic and lowers once, the CWL kept on the tool."""
    parsed = parse(dump_wic_yaml(document), 'tool.wic')
    assert parsed.tool is not None and parsed.tool.cwl is not None and not parsed.diagnostics, [
        str(found) for found in parsed.diagnostics]
    return parsed.tool.cwl


def _argv(tool: dict[str, Any], job: dict[str, Any]) -> list[str]:
    """What cwltool would run: the command line its own job builder makes of `tool` for `job`."""
    from cwltool.context import RuntimeContext  # pylint: disable=import-outside-toplevel  # no Windows
    process = make_tool('file:///tool.cwl', dump_wic_yaml(tool))
    assert process is not None
    with tempfile.TemporaryDirectory() as scratch:
        context = RuntimeContext({'basedir': scratch, 'outdir': scratch, 'tmpdir': scratch,
                                  'tmpdir_prefix': f'{scratch}/', 'tmp_outdir_prefix': f'{scratch}/'})
        job_made, = filter(None, process.job(job, lambda *_: None, context))
    return list(getattr(job_made, 'command_line'))


def _by_class(section: Any) -> dict[str, Any]:
    """The requirements or hints of a tool by their class, whether written as a mapping or as a list."""
    if isinstance(section, list):
        return {entry['class']: {key: value for key, value in entry.items() if key != 'class'} for entry in section}
    return dict(section)


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('tool_lowering.yaml', 'wic', 'cwl'))
def test_a_tool_means_the_cwl_its_row_states(case: dict[str, Any]) -> None:
    """A tool as `.wic` text is the CommandLineTool its row states, key for key and in the order of every list."""
    parsed = parse(case['wic'], 'tool.wic')
    assert parsed.tool is not None and not parsed.diagnostics, [str(found) for found in parsed.diagnostics]
    assert parsed.tool.cwl == case['cwl']


#: A value that is not there: its flag goes with it, and a dash word that is text stays.
_ABSENT = ({'v': 'string?'}, {'v': None})


@pytest.mark.needs_cwltool
@pytest.mark.fast
@given(commands(), st.booleans())
@example((['wc', "'-w'", '{v}'], None, *_ABSENT, ['wc', '-w']), False)  # a dash word between quotes is text,
@example((['grep', '--', '{v}'], ['grep', '--', '{v}'], *_ABSENT, ['grep', '--']), False)  # and so are dashes alone,
@example((['grep', '--', '{v}'], ['grep', '--', '{v}'], *_ABSENT, ['grep', '--']), True)  # as a word of a list too
@example((['cat', '{v}', "'$(x)'"], ['cat', '{v}', '$(x)'], {'v': 'string'}, {'v': 'a'}, ['cat', 'a', '$(x)']),
         False)  # text CWL would evaluate stays text, after a value too
@example((['grep', '{v}'], ['grep', '{v}'],
          {'v': {'type': 'string', 'inputBinding': {'prefix': '--p=', 'separate': False}}}, {'v': 'a'},
          ['grep', '--p=a']), False)  # the port says how its own value is written
@budget(60)
def test_a_command_line_means_its_words(case: tuple[list[str], list[str] | None, dict[str, Any], dict[str, Any],
                                                    list[str]], as_list: bool) -> None:
    """The command line cwltool builds from the tool a `.wic` means is the words of the line with the job's values in
    their places: a flag goes with its value and with nothing else; a value is one word whatever its input is called
    and however it looks; an input's own binding says how and the line says where; and text stays text, which CWL
    would otherwise evaluate. The words before the first value or flag are the tool's `baseCommand`, and a line that
    starts with a flag or a value writes none: the image's entry point runs it."""
    typed, listed, ports, job, argv = case
    if as_list and listed is None:
        return
    lowered = _tool({'command': listed if as_list else ' '.join(typed), 'inputs': ports})
    assert _argv(lowered, job) == argv
    words = [*typed, '{']
    leading = 0 if typed[0][0] in '-{' else next(
        index for index, word in enumerate(words)
        if word[0] == '{' or word[0] == '-' and word.strip('-') and words[index + 1][0] == '{')
    assert lowered.get('baseCommand', []) == (argv[0] if leading == 1 else argv[:leading])


@pytest.mark.fast
@given(st.lists(st.sampled_from(['.txt', '*.out', 'x y', '$(z)', 'a\\\\b', '\\', '{{}}', '{s}', '{n}', '{in-file}',
                                 '{d.1}', '${{y}}']), min_size=1, max_size=4))
@example(['{d.1}'])  # one Directory input's own name is a Directory
@example(['${{y}}', '{s}'])  # text that would start a function body is text
@budget(100)
def test_an_output_line_names_the_file_it_says(pieces: list[str]) -> None:
    """The glob of an output line, as CWL's own evaluator reads it for a job, is the line with each input's name in
    its place (a File's or a Directory's base name), whatever the input is called; text stays text however it looks,
    beside an input too, where the evaluator would unescape it; and the output is a Directory only when the line is
    one Directory input's own name."""
    job: Any = {'s': 'v w', 'n': 3, 'in-file': {'class': 'File', 'location': 'file:///x/a.bam', 'basename': 'a.bam'},
                'd.1': {'class': 'Directory', 'location': 'file:///x/dir', 'basename': 'dir'}}
    names = {'{s}': 'v w', '{n}': '3', '{in-file}': 'a.bam', '{d.1}': 'dir', '{{}}': '{}', '${{y}}': '${y}'}
    output = _tool({'command': 'echo', 'outputs': {'o': ''.join(pieces)}, 'inputs': {
        's': 'string', 'n': 'int', 'in-file': 'File', 'd.1': 'Directory'}})['outputs']['o']
    assert output['type'] == ('Directory' if pieces == ['{d.1}'] else 'File')
    assert str(do_eval(output['outputBinding']['glob'], job, [], None, None, {})) == ''.join(
        names.get(piece, piece) for piece in pieces)


@pytest.mark.fast
@given(st.lists(st.one_of(st.sampled_from(['--color=never', '$(runtime.cores)']), st.fixed_dictionaries(
    {'valueFrom': st.sampled_from(['mid', '$(inputs.a)'])},
    optional={'position': st.integers(-3, 9), 'prefix': st.just('-e')})), max_size=3),
    st.sampled_from([[{'class': 'InitialWorkDirRequirement', 'listing': [{'entry': 'x', 'entryname': 'y'}]}],
                     {'InitialWorkDirRequirement': {'listing': [{'entry': 'x', 'entryname': 'y'}]}},
                     [{'class': 'EnvVarRequirement', 'envDef': {'E': 'v'}}]]),
    st.lists(st.sampled_from(['hints', 'baseCommand', 'stdin']), unique=True))
@budget(100)
def test_a_line_adds_cwl_and_changes_none(arguments: list[Any], requirements: Any, unsaid: list[str]) -> None:
    """The CWL an author writes beside `command:` stands as written, where CWL puts it: every argument and requirement
    of his is in the tool unchanged and in its order, and each line adds its own. A key written with no value says
    nothing, so it is no place already written."""
    tool = {'command': 'echo first {a} last', 'inputs': {'a': 'string'}, 'requirements': requirements,
            'needs': {'cores': 2}, 'docker': 'a:1'}
    assert _tool(tool | dict.fromkeys(unsaid)) == _tool(tool)
    lowered = _tool(tool | {'arguments': arguments})
    assert [each for each in lowered['arguments'] if each in arguments] == arguments
    theirs, after = _by_class(requirements), _by_class(lowered['requirements'])
    assert all(after[name] == body for name, body in theirs.items())
    assert {'ResourceRequirement'} <= set(after) and _by_class(lowered['hints']) == {
        'DockerRequirement': {'dockerPull': 'a:1'}}


#: An input of a script tool by what it is: its port; a job's value for it; and the words cwltool puts on the command
#: line for it, `{}` its name, or None where the port puts words that are no variable of the script.
_RECORD: dict[str, Any] = {'type': 'record', 'fields': [{'name': 'f', 'type': 'string'}]}
_SCRIPT_INPUTS: dict[str, tuple[Any, Any, list[str] | None]] = {
    'string': ('string', 'v', ['{}=v']), 'array': ('string[]', ['p', 'q r'], ['{}=p q r']),
    'unset': ('int?', None, []), 'true': ({'type': 'boolean'}, True, ['{}=']),
    'bound': ({'type': 'string', 'inputBinding': {'position': 2}}, 'v', None),
    'empty binding': ({'type': 'string[]', 'inputBinding': {}}, ['a'], None),
    'bound array': ({'type': {'type': 'array', 'items': 'string', 'inputBinding': {'valueFrom': 'hello'}}}, ['a'],
                    None),
    'bound items': ({'type': {'type': 'array', 'items': {'type': 'array', 'items': 'string',
                                                         'inputBinding': {'prefix': 'hello'}}}}, [['a', 'b']], None),
    'bound field': ({'type': {**_RECORD, 'fields': [{**_RECORD['fields'][0],
                                                     'inputBinding': {'position': -1, 'valueFrom': 'hello'}}]}},
                    {'f': 'a'}, None),
    'record': ({'type': _RECORD}, {'f': 'a'}, None)}
_STAGED = ({'entryname': '.script.sh', 'entry': 'hi'}, {'class': 'File', 'basename': '.script.sh', 'contents': 'hi'},
           {'entryname': './.script.sh', 'entry': 'hi'})


@pytest.mark.needs_cwltool
@pytest.mark.fast
@given(st.dictionaries(st.sampled_from(['a', 'b', 'c']), st.sampled_from(sorted(_SCRIPT_INPUTS)), max_size=3),
       st.lists(st.sampled_from(['hello', {'valueFrom': 'x', 'position': 1}]), max_size=1),
       st.sampled_from([None, 'other', *_STAGED]))
@example({'a': 'bound array'}, [], None)
@example({'a': 'bound items'}, [], None)
@example({'a': 'bound field'}, [], None)
@example({'a': 'string'}, [], _STAGED[0])
@example({'a': 'string'}, [], _STAGED[2])
@budget(100)
def test_a_script_owns_its_command_line(inputs: dict[str, str], arguments: list[Any], staged: Any) -> None:
    """Beside `script:`, whatever CWL could put a word on the command line is refused (`wic058`) where it is written:
    `arguments:`, an input's binding at any depth (the port's own, its type's, an array's items, a record's field), a
    record input, and a listing entry for the script's own file. A script with none of them runs the command line
    cwltool's own job builder makes of it, exactly `env name=value ... bash .script.sh`, with nothing between."""
    entry = staged if isinstance(staged, dict) else {'entryname': staged, 'entry': 'x'}
    tool: dict[str, Any] = {
        'script': 'echo "$a"', 'inputs': {name: _SCRIPT_INPUTS[kind][0] for name, kind in inputs.items()},
        'outputs': {}, 'arguments': arguments}
    if staged:
        tool['requirements'] = [{'class': 'InitialWorkDirRequirement', 'listing': [entry]}]
    written = dump_wic_yaml(tool)
    parsed = parse(written, 'script.wic')
    refused = [written.splitlines()[found.span.start_line - 1].split(':')[0].strip() for found in parsed.diagnostics
               if found.code is SophiosErrorCode.TOOL_DOCUMENT and found.span]
    assert len(refused) == len(parsed.diagnostics)
    assert sorted(refused) == sorted([name for name, kind in inputs.items() if _SCRIPT_INPUTS[kind][2] is None]
                                     + ['arguments'] * bool(arguments)
                                     + ['requirements'] * isinstance(staged, dict))
    if not refused:
        assert parsed.tool is not None and parsed.tool.cwl is not None
        job = {name: _SCRIPT_INPUTS[kind][1] for name, kind in sorted(inputs.items())}
        assert _argv(parsed.tool.cwl, job) == [
            'env', *(word.format(name) for name, kind in sorted(inputs.items())
                     for word in _SCRIPT_INPUTS[kind][2] or ()), 'bash', '.script.sh']


@pytest.mark.needs_cwltool
@pytest.mark.slow
@given(st.lists(st.sampled_from(['$(inputs.s)', '${return 1;}', '\\$(x)', '\\\\', '{s}', '{{b}}', '"', "'", '`id`',
                                 '$(id)', '\t', '\r', 'plain']), max_size=6), st.booleans(),
       st.text(st.sampled_from(list('ab $(){}`"\'\\\n\t;|*')), max_size=8),
       st.lists(st.text(st.sampled_from(list('ab $(){}`"\'\\;|*')), max_size=4), max_size=3), st.booleans(),
       st.sampled_from(['string[]', 'string[]?', {'type': 'string[]'}]))
@budget(8)  # pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_a_script_runs_as_written_with_each_input_in_its_variable(
        hostile: list[str], newline: bool, text: str, items: list[str], flag: bool, array: Any) -> None:
    """A `script:` tool run by cwltool, whose one output is what the script prints: each of its variables and then its
    own file, each ended by a NUL, which no value holds. The file is the script as written, byte for byte, whatever
    it holds that CWL, YAML or the line grammar would read; and each input is in the variable of its name as the
    characters it is: an array, however its type is written, as its items between spaces, a true boolean set and
    empty, a false one and a null unset."""
    import cwltool.main  # pylint: disable=import-outside-toplevel  # no Windows
    script = '\n'.join(['printf \'%s\\0\' "$s" "$xs" "${b-unset}" "${o-unset}"', 'cat .script.sh',
                        '# ' + ' '.join(hostile)]) + '\n' * newline
    tool = _tool({'script': script, 'inputs': {'s': 'string', 'xs': array, 'b': 'boolean', 'o': 'string?'},
                  'outputs': {'printed': 'stdout'}})
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch)
        (base / 'tool.cwl').write_text(dump_wic_yaml(tool), encoding='utf-8')
        (base / 'job.json').write_text(json.dumps({'s': text, 'xs': items, 'b': flag}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        code = cwltool.main.main(['--quiet', '--outdir', str(base / 'out'), str(base / 'tool.cwl'),
                                  str(base / 'job.json')], stdout=out, stderr=err)
        assert code == 0, err.getvalue()
        printed = json.loads(out.getvalue())['printed']
        assert Path(printed['path']).read_bytes().split(b'\0') == [
            text.encode(), ' '.join(items).encode(), b'' if flag else b'unset', b'unset', script.encode()]


def _said(tool: dict[str, Any]) -> dict[str, Any]:
    """What a CommandLineTool says, however it is spelled: its ports and its requirements and hints by name, whether
    written as a list or as a mapping, each input's type in full; its `id` and `cwlVersion` set aside."""
    said = {key: value for key, value in tool.items() if key not in ('id', 'cwlVersion')}
    for section, name in (('inputs', 'id'), ('outputs', 'id'), ('requirements', 'class'), ('hints', 'class')):
        if isinstance(said.get(section), list):
            said[section] = {entry[name]: {key: value for key, value in entry.items() if key != name}
                             for entry in said[section]}
    return said | {'inputs': {name: port | {'type': canonical_type(port['type'])}
                              for name, port in said.get('inputs', {}).items()}}


#: A tool, the version it pins, and the code of what it cannot say.
_TOOLS = st.sampled_from([
    ('command: grep {pattern} {file}\ninputs: {pattern: string, file: File}\noutputs: {matches: stdout}\n', None, None),
    ('script: echo "$text"\ninputs: {text: string}\noutputs: {said: stdout}\n', None, None),
    ('command: [echo, "{n}"]\ninputs: {n: {type: int, default: 3}}\noutputs: {o: "{n}.txt"}\ndocker: a:1\n', None,
     None),
    ('command: grep {patern} | wc\ninputs: {pattern: string}\n', None, SophiosErrorCode.TOOL_DOCUMENT),
    ('command: 3\n', None, SophiosErrorCode.MALFORMED_TOOL_VALUE),
    ('command: echo\nwic: {lang_version: 9.9.9}\n', '9.9.9', None),
    ('command: echo\nwic: {lang_version: 0.0.1}\n', '0.0.1', None)])


def _version_refusal(version: str) -> SophiosErrorCode | None:
    """What a compile under `version` says of a tool: no version is 9.9.9, and tools are syntax in none before 0.1.0."""
    return (SophiosErrorCode.UNKNOWN_LANG_VERSION if version == '9.9.9'
            else SophiosErrorCode.LANG_VERSION_TOO_OLD if version == '0.0.1' else None)


@pytest.mark.fast
@given(_TOOLS, st.sampled_from([None, '0.0.1', '0.1.0', '9.9.9']))
@budget(30)  # pylint: disable-next=too-many-locals
def test_a_tool_is_one_tool_through_every_door(case: tuple[str, str | None, SophiosErrorCode | None],
                                               setting: str | None) -> None:
    """A tool named as the root, by `run:`, written as an inline body, found by its stem on the tool search paths
    (written with the suffix or without), given to a Python `Step` by its path or by its name, and read by
    `Workflow.from_wic` as the root, by `run:`, as an inline body and by its stem (with the suffix or without), is each
    time the same CommandLineTool, or is refused with the same code; as the root its ports are the workflow's inputs,
    by name, type and default, each at the line of the tool that declares it. Nothing tells one door's tool from
    another's: the registry discovery gives holds each tool as a file it reads when a step runs it, whatever it is
    written in. The version is chosen as for a workflow: a setting that is no version is refused before anything is
    read, and a tool is refused for what it cannot say before its version is held to the setting or to the pin the
    tool writes (a tool needs 0.1.0). Python reads a file when it is given one, and compiles under the setting."""
    text, pin, fault = case
    expected = _version_refusal(setting) if setting == '9.9.9' else fault or _version_refusal(setting or pin or '0.1.0')
    in_python = fault or _version_refusal(setting or pin or '0.1.0')
    from_wic = fault or _version_refusal(pin or '0.1.0') or _version_refusal(setting or '0.1.0')
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch)
        (base / 'adapters').mkdir()
        (base / 'adapters' / 'tool.cwl').write_text(  # the stem of the root: its own file is the tool it means
            'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: from-the-index\ninputs: {}\noutputs: {}\n',
            encoding='utf-8')
        for path in (base / 'tool.wic', base / 'adapters' / 'named.wic'):
            path.write_text(text, encoding='utf-8')
        (base / 'run.wic').write_text('steps:\n- id: tool\n  run: tool.wic\n', encoding='utf-8')
        (base / 'stem.wic').write_text('steps:\n- id: named\n', encoding='utf-8')
        (base / 'suffixed.wic').write_text('steps:\n- id: named.wic\n', encoding='utf-8')
        (base / 'inline.wic').write_text('steps:\n- id: tool\n  run:\n' + ''.join(
            f'    {line}\n' for line in text.splitlines()), encoding='utf-8')

        def index() -> Any:
            return get_tools_cwl({'search_paths_cwl': {'global': [str(base / 'adapters')]}})

        def read(root: str) -> Workflow:
            return Workflow.from_wic(base / root, tool_registry=index())
        doors: dict[str, Any] = {root: (lambda root=root: compile_source(
            bundle_from_disk(base / root, {}, index()), CompileSettings(lang_version=setting), quiet=True))
            for root in ('tool.wic', 'run.wic', 'stem.wic', 'suffixed.wic', 'inline.wic')}
        doors |= {
            'Step': lambda: Workflow([Step(clt_path=base / 'adapters' / 'named.wic')], 'w').compile(
                lang_version=setting),
            'Step, by its name': lambda: Workflow([Step(clt_path=Path('named.cwl'), tool_registry=index())],
                                                  'w').compile(lang_version=setting, tool_registry=index()),
            'from_wic, by run:': lambda: read('run.wic').compile(lang_version=setting),
            'from_wic, an inline body': lambda: read('inline.wic').compile(lang_version=setting),
            'from_wic, by its stem': lambda: read('stem.wic').compile(lang_version=setting, tool_registry=index()),
            'from_wic, by its suffixed stem': lambda: read('suffixed.wic').compile(lang_version=setting,
                                                                                   tool_registry=index())}
        if 'default' not in text:  # a Python workflow port carries only a type (api006)
            doors['from_wic'] = lambda: read('tool.wic').compile(lang_version=setting)
        tools, codes = {}, {}
        for door, compiled in doors.items():
            try:
                made = compiled()
                tools[door] = _said(cwl_tree(made).children[0].cwl if door.endswith('.wic')
                                    else made.cwl_workflow['steps'][0]['run'])
                if door == 'tool.wic':
                    ports = {port.name: port for port in parse(text, door).tool.inputs}  # type: ignore[union-attr]
                    assert {name: (port['type'], port.get('default'))
                            for name, port in cwl_tree(made).cwl['inputs'].items()} \
                        == {name: (port.type, dict(port.fields).get('default')) for name, port in ports.items()}
            except SophiosError as error:
                codes[door] = {found.code for found in error.diagnostics}
    wanted = {door: expected if door.endswith('.wic') else from_wic if door.startswith('from_wic') else in_python
              for door in doors}
    assert codes == {door: {code} for door, code in wanted.items() if code is not None}, (codes, tools)
    assert all(tool == next(iter(tools.values())) for tool in tools.values()), tools


@pytest.mark.fast
def test_a_tool_found_once_is_the_tool_of_its_stem_for_every_door(tmp_path: Path) -> None:
    """Discovery names each tool once, and gives a stem to its `.cwl` when a `.wic` has it too. A `.wic` of that stem
    reached any other way, by a Python `Step` given its path or by a step that names it, is never a second tool of
    the stem: a step that names the stem still runs the `.cwl`, whatever came first. The root is the one file that
    is read as itself."""
    (tmp_path / 'adapters').mkdir()
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'adapters' / 'grep.cwl').write_text(
        'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: echo-from-cwl\ninputs: {}\noutputs: {}\n',
        encoding='utf-8')
    (tmp_path / 'elsewhere' / 'grep.wic').write_text('command: echo-from-wic\n', encoding='utf-8')
    (tmp_path / 'suffixed.wic').write_text('steps:\n- id: grep.wic\n', encoding='utf-8')

    def registry() -> Any:
        return get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path / 'adapters')]}})

    def command(compiled: Any) -> Any:
        return compiled.cwl_workflow['steps'][0]['run']['baseCommand']
    shared = registry()
    by_path = Workflow([Step(clt_path=tmp_path / 'elsewhere' / 'grep.wic', tool_registry=shared)], 'w')
    by_name = Workflow([Step(clt_path=Path('grep.cwl'), tool_registry=shared)], 'w')
    assert command(by_name.compile(tool_registry=shared)) == 'echo-from-cwl'
    assert command(by_path.compile(tool_registry=shared)) == 'echo-from-cwl'
    suffixed = compile_source(bundle_from_disk(tmp_path / 'suffixed.wic',
                              {}, registry()), CompileSettings(), quiet=True)
    assert cwl_tree(suffixed).children[0].cwl['baseCommand'] == 'echo-from-cwl'
    root = compile_source(bundle_from_disk(tmp_path / 'elsewhere' / 'grep.wic', {}, registry()), CompileSettings(),
                          quiet=True)
    assert cwl_tree(root).children[0].cwl['baseCommand'] == 'echo-from-wic'


@pytest.mark.fast
@given(st.sampled_from(['', '.txt', '.bam.bai', '*.out', '$(x)']))
@budget(20)
def test_an_output_line_names_its_file_whatever_says_what_runs(after: str) -> None:
    """`Output.of()` is the file its pieces name, whether the tool says what runs with `command()` or only with
    `base_command()`, which says no line for the tool to lower."""
    said_ports, base_ports = Inputs(v=Input('string')), Inputs(v=Input('string', position=1))
    said = CommandLineTool('t', said_ports, Outputs(o=Output.of(said_ports.v, after))).command(
        'echo', said_ports.v).to_cwl_document()
    base = CommandLineTool('t', base_ports, Outputs(o=Output.of(base_ports.v, after))).base_command(
        'echo').to_cwl_document()
    assert base['outputs'] == said['outputs'] != {}


def _cookbook() -> list[tuple[dict[str, Any], CommandLineTool, bool, int]]:
    """Each tool the cookbook shows twice: a `.py` whose function is `tool()`, and the `.wic` of its stem."""
    return [(yaml.safe_load(path.with_suffix('.wic').read_text(encoding='utf-8')), module.tool(), False, 0)
            for path in sorted((REPO_ROOT / 'docs' / 'cookbook').glob('*.py'))
            if hasattr(module := import_python_file(f'cookbook_tool_{path.stem}', path), 'tool')]


def _directory_twin() -> tuple[dict[str, Any], CommandLineTool, bool, int]:
    """A tool whose output is the Directory its one input is: the output line names an input that is a Directory,
    which is its own name, and a Directory itself."""
    ports = Inputs(d=Input('Directory'))
    return ({'command': 'ls {d}', 'inputs': {'d': 'Directory'}, 'outputs': {'o': '{d}'}},
            CommandLineTool('t', ports, Outputs(o=Output.of(ports.d))).command('ls', ports.d), True, 0)


@st.composite  # pylint: disable-next=too-many-locals
def _twins(draw: st.DrawFn) -> tuple[dict[str, Any], CommandLineTool, bool, int]:
    """One tool as a `.wic` document and as the calls that build it; whether it holds an expression; and the line of
    this file on which the calls say what runs. Its command is any the command-line property draws, over names
    Python can declare, or a script over the same inputs; its output is a stream or a line; and beside them stand a
    container hint, what it needs as a number or a size, and CWL of the author's."""
    typed, listed, declared, _job, _argv_ = draw(commands(['v', 'x_1', 'sample', 'out']).filter(  # pylint: disable=no-member
        lambda case: case[1] is not None))
    script = draw(st.booleans())
    kinds = {name: port['type'] if isinstance(port, dict) else port for name, port in declared.items()}
    bound = {} if script else {name: port['inputBinding'] for name, port in declared.items() if isinstance(port, dict)}
    ports = Inputs(**{name: Input(kind, flag=bound[name]['prefix'], separate=bound[name]['separate'])
                      if name in bound else Input(kind) for name, kind in kinds.items()})
    named = draw(st.sampled_from([None, *(name for name, kind in kinds.items() if kind in ('string', 'int'))]))
    after = draw(st.sampled_from(['', '.txt', '}', '\\\\x']))
    tool: dict[str, Any] = {'script': 'echo "$v"\n'} if script else {'command': ' '.join(typed)}
    tool |= {'inputs': {name: {'type': kind, 'inputBinding': bound[name]} if name in bound else kind
                        for name, kind in kinds.items()},
             'outputs': {'out': f'{{{named}}}{_braced(after)}' if named else 'stdout'}}
    built = CommandLineTool('t', ports, Outputs(out=Output.of(ports[named], after) if named else Output.stdout()))
    words = [ports[word[1:-1]] if word[0] == '{' and word[1:-1] in kinds else word.replace('{{', '{').replace(
        '}}', '}') for word in listed or ()]
    said_at = inspect.currentframe().f_lineno + 1  # type: ignore[union-attr]
    built = built.script('echo "$v"\n') if script else built.command(*words)
    if image := draw(st.sampled_from([None, 'debian:bookworm-slim'])):
        tool['docker'], built = image, built.docker(image, as_hint=True)
    if needs := draw(st.sampled_from([None, (4, 64, 64), (2, '512M', 512), (1, '02G', 2048), (8, '1T', 1048576)])):
        tool['needs'], built = {'cores': needs[0], 'ram': needs[1]}, built.resources(cores=needs[0], ram=needs[2])
    if not script and (argument := draw(st.sampled_from([None, '$(runtime.cores)', 'plain']))):
        tool['arguments'], built = [argument], built.argument(argument)
    if declares := draw(st.sampled_from([None, 'requirements', 'hints'])):
        tool[declares] = [{'class': 'InlineJavascriptRequirement'}]
        built = built.inline_javascript(as_hint=declares == 'hints')
    return tool, built, bool(declares or named) or any(mark in json.dumps(tool) for mark in ('$(', '${')), said_at


@pytest.mark.needs_cwltool
@pytest.mark.fast
@given(_twins())
@pinned(*_cookbook(), _directory_twin())
@budget(100)
def test_a_tool_in_wic_and_in_python_is_one_tool(case: tuple[dict[str, Any], CommandLineTool, bool, int]) -> None:
    """A tool written as `.wic` and built with `command()` or `script()` and `Output.of()` is one CommandLineTool,
    which cwltool accepts, with one rule for the JavaScript requirement: a tool that holds an expression declares it
    once, as a requirement or as a hint. The cookbook's tool pairs are held to it. A place both surfaces say twice
    (`baseCommand` beside a command, `arguments` beside a script) is refused by both with the tool's code: the `.wic`
    at the key written twice, Python at the line that says what runs."""
    tool, built, javascript, said_at = case
    lowered = _tool(tool)
    assert _said(lowered) == _said(built.to_cwl_document())
    validate('tool.cwl', lowered)
    if not said_at:
        return  # a pair the cookbook states holds no place to say twice
    declared = [*_said(lowered).get('requirements', {}), *_said(lowered).get('hints', {})]
    assert declared.count('InlineJavascriptRequirement') == int(javascript)
    key, twice = ('arguments', ['x']) if 'script' in tool else ('baseCommand', 'ls')
    written = dump_wic_yaml(tool | {key: twice})
    assert [(found.code, found.span and found.span.start_line) for found in parse(written, 'twice.wic').diagnostics] \
        == [(SophiosErrorCode.TOOL_DOCUMENT, 1 + [row.partition(':')[0] for row in written.splitlines()].index(key))]
    with pytest.raises(SophiosError) as refused:
        (built.argument('x') if 'script' in tool else built.base_command('ls')).to_cwl_document()
    found, = refused.value.diagnostics
    assert found.code is SophiosErrorCode.TOOL_DOCUMENT and found.span is not None
    assert (found.span.file, found.span.start_line) == (str(Path(__file__).resolve()), said_at)


@pytest.mark.needs_cwltool
@pytest.mark.fast
@given(st.dictionaries(st.sampled_from(['plain', 'in-file', 'in.file', '1st', 'a b', 'a$b', 'x{y}', 'naïve', "it's"]),
                       st.sampled_from([
                           {'type': 'string'}, {'type': 'int'}, {'type': 'boolean'}, {'type': 'string[]'},
                           {'type': 'string?', 'inputBinding': {'position': 9, 'prefix': '-x'}}]), max_size=4),
       st.booleans())
@budget(50)
def test_a_python_script_tool_passes_each_input_as_its_flag(inputs: dict[str, Any], docker: bool) -> None:
    """The tool of a `python_script` step runs `python3` on the driver, the types and the script, then `--name value`
    for each input the script declares, whatever the input is called and whatever binding its declaration carries:
    the tool is built from the ports themselves, never from text that is read again."""
    values = {'string': 'v', 'int': 3, 'boolean': True, 'string[]': ['p', 'q'], 'string?': 'o'}
    tool = generate_CWL_CommandLineTool(inputs, {'out': ('*.txt', {'type': 'File'})}, 'img:1' if docker else '')
    argv = ['python3', '/python_cwl_driver.py', '/workflow_types.py', 's.py']
    for name, port in inputs.items():
        value = values[port['type']]
        argv += [f'--{name}'] if value is True else [f'--{name}', *map(str, value if isinstance(value, list)
                                                                       else [value])]
    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / 's.py').write_text('', encoding='utf-8')
        line = _argv(tool, {name: values[port['type']] for name, port in inputs.items()} | {'script': {
            'class': 'File', 'location': (Path(scratch) / 's.py').as_uri(),
            'format': 'https://edamontology.org/format_2330'}})
    assert [*line[:3], Path(line[3]).name, *line[4:]] == argv
    assert tool['outputs'] == {'out': {'type': 'File', 'outputBinding': {'glob': '*.txt'}}}
