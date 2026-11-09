"""The library reports failures; only the CLI adapter exits: compilation never terminates the process, every failure
yields a diagnostic, exit codes are unchanged. The no-exit claim's dynamic half is test_pipeline.py's."""
import datetime
import json
import math
import os
import re
import shutil
import string
import subprocess
import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NamedTuple

import pytest
import yaml
from hypothesis import assume, example, given, strategies as st

from sophios import main as cli
from sophios import preflight, run_local
from sophios.ir.complete import coerce_job_value
from sophios.ir.declarations import port_declaration
from sophios.ir.types import AuthoredName
from sophios.lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from sophios.lang.error_codes import EXPLANATIONS, Kind, SophiosErrorCode
from sophios.lang.spans import SourceSpan
from sophios.python_cwl_adapter import check_args_match_inputs

from .budgets import budget
from .cases import match
from .fake_runner import POSIX_ONLY
from .helpers import HELLOWORLD, REPO_ROOT, no_preflight, run_cli, search_config, write_tree
from .hermetic import compile_hermetic, compile_hermetic_source
from .provocations import _provoke_duplicate_document_name
from .strategies import author_names, pinned
from .synthetic_tools import clt, registered
from .test_lang_parser import FAST


@pytest.mark.fast
def test_an_error_with_no_diagnostics_is_unrepresentable() -> None:
    """By construction: the exception cannot exist empty."""
    with pytest.raises(ValueError):
        SophiosError([])


@pytest.mark.fast
def test_str_carries_every_message() -> None:
    """What an embedder logs by default includes each diagnostic, so catching
    without inspecting `.diagnostics` still loses nothing."""
    error = SophiosError.error(SophiosErrorCode.UNRESOLVED_INPUT, 'first', 'second')
    assert 'first' in str(error) and 'second' in str(error)
    assert len(error.diagnostics) == 2


# --- the converted sites, one by one ---------------------------------------


@pytest.mark.fast
def test_script_argument_mismatch_reports(tmp_path: Path) -> None:
    """`python_cwl_adapter` reports the mismatch it used to print-and-exit."""
    module = ModuleType('fake_workflow_script')
    module.inputs = {'expected_arg': int}  # type: ignore[attr-defined]

    with pytest.raises(SophiosError) as caught:
        check_args_match_inputs(module, {'unexpected_arg': 1}, check=True)

    messages = [d.message for d in caught.value.diagnostics]
    assert any('unexpected_arg' in m for m in messages)
    assert any('expected_arg' in m for m in messages)
    assert all(d.code is SophiosErrorCode.SCRIPT_ARGUMENT_MISMATCH for d in caught.value.diagnostics)


_TYPES: Final = {'int': int, 'long': int, 'float': float, 'double': float, 'boolean': bool, 'string': str}
_TEXTS = st.text('07.e+-:axy', max_size=4)
_READ_BACK: Final[dict[type, Callable[[str], Any]]] = {str: str, int: int, float: float,
                                                       datetime.date: datetime.date.fromisoformat}
_OWN: Final = {int: st.integers(-2**70, 2**70), float: st.floats(), bool: st.booleans(), str: _TEXTS}
_LITERALS = st.recursive(
    st.one_of(st.integers(-2**70, 2**70), st.floats(), st.booleans(), _TEXTS, st.dates(), st.none()),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(['k', 'seen']), inner, max_size=2),
    max_leaves=4)
_PORTS_AND_LITERALS = st.sampled_from(sorted(_TYPES)).flatmap(lambda name: st.tuples(
    st.sampled_from([name, {'type': 'array', 'items': name}]),
    _OWN[_TYPES[name]] | st.lists(_OWN[_TYPES[name]] | st.none(), max_size=3) | _LITERALS))


def _binds(name: str, value: Any) -> bool:
    """Whether a port of the scalar CWL type `name` takes `value`: the JSON type `_TYPES` gives (a bool is no int), an
    int for a float held exactly, and for a string any scalar but null, or a JSON list or mapping."""
    wanted = _TYPES[name]
    if wanted is float and value.__class__ is int:
        return -2**1024 < value < 2**1024 and float(value) == value
    if wanted is str and isinstance(value, (list, dict)):
        try:
            return bool(json.dumps(value))
        except TypeError:
            return False
    return value.__class__ is wanted or wanted is str and isinstance(value, (int, float, datetime.date))


@pytest.mark.fast
@given(_PORTS_AND_LITERALS)
@pinned(('int', 2.9), ('int', '007'), ('int', True), ('long', 2.9), ('boolean', 'yes'), ('boolean', 2),
        ('float', '1e3'), ('float', True), ('double', 'x'), ('float', 2**53 + 1), ('float', 10**400),
        ('string', {'seen': datetime.date(2024, 1, 15)}), ({'type': 'array', 'items': 'string'}, ['a', None]),
        ('string', 20), ('string', 1.5), ('string', True), ('string', False), ('string', datetime.date(2024, 1, 15)),
        ('string', {'seen': 'a'}), ('float', 1), ('double', 2**53), ('float', 1.0e-5), ('int', 3), ('long', 3),
        ('boolean', False), ('float', math.nan), ({'type': 'array', 'items': 'int'}, [1, None]))
@FAST
def test_a_literal_binds_only_as_the_value_it_is(case: Any) -> None:
    """`int('007')` and `bool('yes')` used to succeed, an int above 2**53 rounded silently, and a null inside an array
    raised a bare `TypeError`. A literal binds only as the port type's own value, an exact int into a float, or a
    string's text that reads back to it; else one wic020 names the input, the type and the literal (null, wic012)."""
    declared, value = case
    name = declared['items'] if isinstance(declared, dict) else declared
    items = value if isinstance(declared, dict) and isinstance(value, list) else [value]
    refused = [item for item in items if not _binds(name, item)]
    try:
        bound: Any = _job_value(declared, value)
    except SophiosError as caught:
        [diagnostic] = list(caught.diagnostics)
        assert refused and diagnostic.code is (SophiosErrorCode.LITERAL_TYPE_MISMATCH if value is not None
                                               else SophiosErrorCode.MISSING_REQUIRED_INPUT), diagnostic
        assert value is None or all(repr(part) in diagnostic.message for part in ('x', name, refused[0])), diagnostic
        assert (refused[0].__class__ is not int or _TYPES[name] is not float
                or 'cannot hold exactly' in diagnostic.message), diagnostic
        return
    assert value is not None and not refused, bound
    for item, held in zip(items, bound if isinstance(declared, dict) else [bound]):
        if _TYPES[name] is str:
            assert isinstance(held, str) and repr(_READ_BACK.get(type(item), json.loads)(held)) == repr(item), held
        else:
            assert held.__class__ is _TYPES[name] and repr(held) == repr(_TYPES[name](item)), held


def _job_value(declared: object, literal: object) -> object:
    """The job value `!ii literal` gives the one input of a tool that declares `declared`."""
    tool = clt({'x': {'type': declared, 'inputBinding': {'position': 1}}}, {}, canonical=True)
    compiled = compile_hermetic({'steps': [{'id': 'probe', 'in': {'x': {'wic_inline_input': literal}}}]},
                                tools=registered({'probe': tool}, {}))
    (value,) = compiled.artifact.job_inputs.values()
    return value


def _job_value_written(declared: object, spelling: str) -> object:
    """The job value `!ii <spelling>`, written in a `.wic` file, gives the one input of a tool that declares
    `declared`. Only the text can say whether the literal is quoted."""
    tool = clt({'x': {'type': declared, 'inputBinding': {'position': 1}}}, {}, canonical=True)
    compiled = compile_hermetic_source(f'steps:\n- id: probe\n  in:\n    x: !ii {spelling}\n',
                                       tools=registered({'probe': tool}, {}))
    (value,) = compiled.artifact.job_inputs.values()
    return value


@pytest.mark.fast
@pytest.mark.parametrize('spelling', ["'7'", '"7"', "'007'"], ids=['single', 'double', 'octal'])
def test_a_quoted_number_on_an_int_input_is_wic020(spelling: str) -> None:
    """A quoted `!ii` scalar is its text, and text is not an int; `!ii 7` is what binds the port."""
    with pytest.raises(SophiosError) as caught:
        _job_value_written('int', spelling)
    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH


@pytest.mark.fast
@pytest.mark.parametrize('spelling, expected', [("'7'", '7'), ("'007'", '007'), ('7', '7'), ('007', '7')],
                         ids=['quoted', 'quoted-octal', 'plain', 'plain-octal'])
def test_a_number_on_a_string_input_is_its_text_quoted_or_not(spelling: str, expected: str) -> None:
    """Quoted, the literal is the text as written; plain, it is the number YAML read, as text."""
    assert _job_value_written('string', spelling) == expected


_ENUM: Final = {'type': 'enum', 'symbols': ['a', 'b']}
_BOOL_OR_STRINGS: Final = ['null', 'boolean', {'type': 'array', 'items': 'string'}]


@pytest.mark.fast
@pytest.mark.parametrize('declared, literal, expected', [
    (_BOOL_OR_STRINGS, False, False),
    (['null', 'int', {'type': 'array', 'items': 'int'}], 3, 3),
    (['null', 'string', {'type': 'array', 'items': 'string'}], 'a', 'a'),
    (['null', 'File', {'type': 'array', 'items': 'File'}], {'class': 'File', 'location': 'x'},
     {'class': 'File', 'location': 'x'}),
    (['null', 'float', {'type': 'array', 'items': 'int'}], 3, [3]),
    (['null', 'string', {'type': 'array', 'items': 'int'}], 3, [3]),
    (['null', 'string', {'type': 'array', 'items': {'type': 'array', 'items': 'int'}}], 3, [[3]]),
    (['null', _ENUM, {'type': 'array', 'items': _ENUM}], 'a', ['a']),
], ids=['bool-stays-a-bool', 'int-stays-an-int', 'text-stays-text', 'file-stays-a-file',
        'int-is-not-widened-to-the-float', 'int-is-not-made-text', 'int-is-not-made-text-nested',
        'enum-member-is-not-a-type-name'])
def test_a_scalar_given_to_a_union_with_an_array_takes_the_scalar_member(
        declared: object, literal: object, expected: object) -> None:
    """biobb's `pdb` takes `filter` as `boolean | string[]`; `filter: !ii False`
    reached it as `['false']`, so it kept only the lines starting with "false"
    and wrote a PDB with no coordinates. A scalar takes the scalar member whose
    type it already is; any other scalar is wrapped into the array, as before."""
    value = _job_value(declared, literal)
    assert (value, type(value)) == (expected, type(expected))


@pytest.mark.fast
@given(st.from_regex(r'[-+]?[0-9]+(\.[0-9]*)?[eE][-+]?[0-9]+', fullmatch=True))
@pinned('1e-5', '1E3', '2.5e10')
@example('.5e3').xfail(raises=AssertionError, reason='no advice for a mantissa with no digit before its point')
@example('1_0e3').xfail(raises=AssertionError, reason='no advice for a mantissa with an underscore')
@FAST
def test_scientific_notation_yaml_reads_as_text_says_how_to_write_a_float(text: str) -> None:
    """YAML reads a float with no signed exponent as text, so it is wic020. The message gives the spelling YAML, and
    so the parser, reads as a float, with the value the author meant: the same float, infinity included."""
    assume(isinstance(yaml.safe_load(text), str))
    with pytest.raises(SophiosError) as caught:
        _job_value('float', text)
    advised = re.search(r'Write (\S+)\.$', caught.value.diagnostics[0].message)
    assert advised is not None and yaml.safe_load(advised[1]) == float(text), caught.value.diagnostics[0].message


@pytest.mark.fast
def test_a_literal_of_another_python_type_says_which_python_type_the_port_wants() -> None:
    """`b'abc'` is a YAML binary scalar; the message names the Python type the port holds."""
    with pytest.raises(SophiosError) as caught:
        _job_value('string', b'abc')
    assert caught.value.diagnostics[0].message.endswith("its literal b'abc' is of type bytes, not a Python str.")


_BAD = 'steps:\n- id: ""\n'
_TWO_TOUCHES = ('steps:\n- id: touch\n  in:\n    filename: !ii a.txt\n'
                '- id: touch\n  in:\n    filename: !ii b.txt\n- id: cat\n')
_JSON = ('--diagnostics', 'json')


class _CliRow(NamedTuple):
    source: str
    name: str
    flags: tuple[str, ...]
    code: int | None
    expected: list[Any]
    draws: bool | None


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    pytest.param(_CliRow(_BAD, 'bad.wic', (), 1,
                 [{'re': 'Failed to compile .*bad[.]wic'}, {'re': r'bad[.]wic:2:7: error \[wic007\]'}], None),
                 id='error'),
    pytest.param(_CliRow(_BAD, 'bad.wic', _JSON, 1,
                 [{'severity': 'error', 'code': 'wic007', 'kind': 'document', 'line': 2, 'column': 7,
                   'file': {'re': 'bad[.]wic$'}, 'fix': SophiosErrorCode.EMPTY_STEP_ID.explanation.fix}], None),
                 id='error-json'),
    pytest.param(_CliRow(_TWO_TOUCHES, 'two_touches.wic', (), None,
                 [{'re': '^' + re.escape('note [wic043] 1 edge was guessed; draw the workflow to check it '
                                         '(--graphviz, or Workflow.draw() in Python)') + '$'},
                  {'re': r'^  .*two_touches\.wic:8:3  cat\.file <- touch@2\.file \(1 other fits\)$'}], True),
                 id='note'),
    pytest.param(_CliRow(_TWO_TOUCHES, 'two_touches.wic', ('--inference_strict',), 1,
                 [{'re': 'Failed to compile .*two_touches[.]wic'},
                  {'re': r'^.*error \[wic043\] cat\.file <- touch@2\.file was guessed, '
                         '1 other fits; write the edge it should read$'}], False),
                 id='strict'),
    pytest.param(_CliRow(_TWO_TOUCHES, 'two_touches.wic', _JSON, None,
                 [{'severity': 'note', 'code': 'wic043', 'message': 'cat.file <- touch@2.file (1 other fits)',
                   'step': 'cat', 'index': 3, 'port': 'file'}], None),
                 id='note-json')])
def test_the_cli_reports_each_diagnostic_on_stderr(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], row: _CliRow) -> None:
    """The failure a scientist sees names the file, line, column and code on stderr, as text or one JSON object a
    line. A guessed edge is a note and the compile succeeds; `--inference_strict` makes it an error, with nothing to
    draw. `code` is None where main returns; `draws` is None where unchecked."""
    path = write_tree(tmp_path, {row.name: row.source}) / row.name
    monkeypatch.chdir(tmp_path)
    assert run_cli(monkeypatch, '--yaml', path, '--generate_cwl_workflow', *row.flags) == row.code
    out, err = capsys.readouterr()
    assert row.code != 1 or row.flags == _JSON or f'Failed to compile {path}' in err
    if row.flags == _JSON:
        found = [json.loads(line) for line in err.splitlines() if line.startswith('{')]
    else:
        found = [line for line in err.splitlines() if re.search(r'\[wic|^Failed to compile| <- ', line)]
    assert match(row.expected, found), found
    assert 'wic0' not in out and 'Failed to compile' not in out
    if row.draws is not None:
        assert ('draw the workflow' in err) is row.draws


@pytest.mark.fast
def test_cli_reports_a_crash_in_one_line_and_keeps_the_whole_traceback(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure Sophios did not expect says so in one line; the file holds the traceback with its frames."""
    workflow = write_tree(tmp_path, {'crash.wic': 'steps:\n- id: touch\n'}) / 'crash.wic'

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(cli.compiler, 'compile_source', crash)
    monkeypatch.chdir(tmp_path)

    assert run_cli(monkeypatch, '--yaml', workflow, '--generate_cwl_workflow') == 1
    captured = capsys.readouterr()
    assert 'Sophios stopped on an unexpected RuntimeError: boom. The traceback is in error_crash.txt' in captured.err
    assert 'Traceback' not in captured.err and 'boom' not in captured.out
    kept = (tmp_path / 'error_crash.txt').read_text(encoding='utf-8')
    assert 'Traceback (most recent call last)' in kept and 'in crash' in kept


@pytest.mark.fast
def test_the_failure_type_is_importable_from_the_package() -> None:
    """`from sophios.lang import SophiosError` works.

    This is the type an embedder writes an `except` clause against, so it is
    the one name in the layer that must be reachable from the package root.
    It was reachable only as `sophios.lang.diagnostics.SophiosError`, which
    asks callers to import from a private module for the sake of the one
    thing the refactor exists to give them.
    """
    from sophios.lang import SophiosError as exported
    assert exported is SophiosError


@pytest.mark.fast
def test_every_exported_name_resolves() -> None:
    """Everything `__all__` promises can be imported.

    `__all__` is a claim about the package's surface, and nothing checks a
    claim like that until a caller writes the import and it fails.
    """
    import sophios.lang as lang

    missing = [name for name in lang.__all__ if not hasattr(lang, name)]
    assert not missing, f'exported but not importable: {missing}'


_Cli = Callable[..., int | str | None]


@pytest.fixture(name='cli_on_helloworld')
def _cli_on_helloworld(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Cli:
    """Run the CLI on the helloworld tutorial from a scratch directory, container engine replaced; compiling, arguments
    and exit code are real."""
    no_preflight(monkeypatch)
    monkeypatch.chdir(tmp_path)
    return partial(run_cli, monkeypatch, '--yaml', HELLOWORLD)


@pytest.mark.fast
@pytest.mark.parametrize(('argv', 'said'), [
    (['--yaml', '{hello}', '--generate_cwl_workflow', '--no_such_flag'], '--no_such_flag'),
    (['--yaml', '{hello}', '--generate_run_script', '--no_such_flag'], '--no_such_flag'),
    (['--yaml', '{hello}', '--generate_cwl_workflow', '--passthrough_flags', 'yes', '--debug'], '--run_local'),
    (['--yaml', '{tmp}/nope.wic', '--generate_cwl_workflow'], 'no workflow file at'),
    (['--yaml', '{hello}', '--inputs_file', '{tmp}/nope.yml', '--generate_cwl_workflow'], 'no inputs file at'),
    (['--explain', 'wic999'], 'wic999 is not a Sophios error code'),
], ids=['an unknown flag compiling', 'an unknown flag writing run.sh', 'passthrough flags with no runner',
        'no such workflow file', 'no such inputs file', 'no such code'])
def test_a_usage_error_exits_2_naming_it_without_a_traceback(
        argv: list[str], said: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A flag Sophios does not know is an error, not a silent gift to the runner, and `--passthrough_flags yes`
    with no runner to send the arguments to is one too. A mistyped `--yaml` or `--inputs_file`, or a code Sophios
    does not report, is a usage error that names it and exit 2, not a traceback."""
    monkeypatch.chdir(tmp_path)
    code = run_cli(monkeypatch, *(arg.format(hello=HELLOWORLD, tmp=tmp_path) for arg in argv))
    printed = capsys.readouterr().err
    assert code == 2 and said in printed and 'Traceback' not in printed


@pytest.mark.fast
@pytest.mark.parametrize(('flags', 'token', 'present'), [
    (['--passthrough_flags', 'yes', '--debug'], '--debug', True),
    (['--passthrough_flags', 'yes', '--validate'], '--validate', True),
    ([], '--quiet', False),
    (['--quiet'], '--quiet', True),
], ids=['passthrough sends an unrecognised argument to the runner',
        'runner --validate is not a prefix of --validate_plugins', 'no --quiet', '--quiet'])
def test_run_sh_carries_a_runner_flag_exactly_when_asked(cli_on_helloworld: _Cli, flags: list[str],
                                                         token: str, present: bool) -> None:
    """cwltool's `--validate` reaches the runner; it is not a prefix of `--validate_plugins`. Without `--quiet` the
    runner keeps its own log level, so `--debug` can be heard."""
    cli_on_helloworld('--generate_run_script', *flags)
    assert (token in Path('run.sh').read_text(encoding='utf-8').split()) is present


@pytest.mark.fast
@pytest.mark.parametrize(('outcome', 'cwl_runner', 'code'), [
    (3, 'cwltool', 3), (KeyboardInterrupt, 'cwltool', 130), (KeyboardInterrupt, 'toil-cwl-runner', 130),
], ids=['the runners exit code', 'ctrl-c under cwltool', 'ctrl-c under toil'])
def test_run_local_exits_with_the_runners_code_or_130_on_ctrl_c(
        monkeypatch: pytest.MonkeyPatch, cli_on_helloworld: _Cli, outcome: object, cwl_runner: str,
        code: int) -> None:
    """The CLI turns the interrupt the run lets through into the shell's SIGINT code, whichever runner ran.

    KeyboardInterrupt is listed so a regression fails this test instead of aborting the session."""
    def run(_request: object) -> object:
        if outcome is KeyboardInterrupt:
            raise KeyboardInterrupt
        return outcome

    monkeypatch.setattr(run_local, 'run', run)
    try:
        assert cli_on_helloworld('--run_local', '--cwl_runner', cwl_runner) == code
    except KeyboardInterrupt:
        pytest.fail('the interrupt escaped the CLI')


@pytest.mark.fast
@POSIX_ONLY
@pytest.mark.parametrize('flags', [[], ['--cachedir', 'mycache']])
def test_a_runner_that_cannot_start_says_why_with_or_without_a_cachedir(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
        cli_on_helloworld: _Cli, flags: list[str]) -> None:
    """The traceback goes to error_<name>.txt; the message itself is printed, whatever the cache."""
    monkeypatch.setattr(preflight, 'runner_program', lambda _name: str(tmp_path / 'no_such_runner'))
    assert cli_on_helloworld('--run_local', *flags) == 1
    out, err = capsys.readouterr()
    assert 'no_such_runner' in out and 'See error_helloworld.txt for detailed technical information.' in out
    assert 'or the error the runner itself raised' in err
    kept = (tmp_path / 'error_helloworld.txt').read_text(encoding='utf-8')
    assert 'Traceback (most recent call last)' in kept and 'FileNotFoundError' in kept


@pytest.mark.fast
@pytest.mark.parametrize('step, code, message', [
    ({'id': 'mk_file', 'in': {'name': 'x'}}, SophiosErrorCode.UNRESOLVED_INPUT, 'Did you forget to use !ii before x?'),
    ({'id': 'scale', 'in': {'n': {'wic_inline_input': 'x'}}}, SophiosErrorCode.LITERAL_TYPE_MISMATCH, None)],
    ids=['unresolved input', 'literal type mismatch'])
def test_a_compile_error_names_its_line_step_and_port(step: dict[str, Any], code: SophiosErrorCode,
                                                      message: str | None) -> None:
    """wic011 carried a message and nothing else, the parser had the span all along; it is one line, with no
    `Warning!` in front and no advice to compile anyway. wic020 names the line of the `!ii` literal and the step
    and port it binds."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [step]})
    first = caught.value.diagnostics[0]
    assert first.code is code
    assert first.span is not None and first.span.file == 'oracle.wic' and first.span.start_line > 0
    assert first.locator == Locator(step=step['id'], index=1, port=next(iter(step['in'])))
    if message is not None:
        assert [(d.code, d.message) for d in caught.value.diagnostics] == [(code, message)]


@pytest.mark.fast
def test_a_missing_required_job_value_carries_its_position() -> None:
    """wic012 is raised where the value is coerced, at the span and step its caller names."""
    span = SourceSpan('x.wic', 3, 1, 3, 1)
    locator = Locator(step='scale', index=2, port='n')
    with pytest.raises(SophiosError) as caught:
        coerce_job_value(AuthoredName('n'), port_declaration({'type': 'int'}), None,
                         span=span, locator=locator)
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.MISSING_REQUIRED_INPUT
    assert (diagnostic.span, diagnostic.locator) == (span, locator)
    assert str(diagnostic).startswith('x.wic:3:1: ')


@pytest.mark.fast
def test_a_duplicate_document_name_names_its_document() -> None:
    """wic031 has no port to point at, so it points at the document that binds a prefix twice."""
    with pytest.raises(SophiosError) as caught:
        _provoke_duplicate_document_name()
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.DUPLICATE_DOCUMENT_NAME
    assert diagnostic.span is not None and diagnostic.span.file == 'provoke.wic'


@pytest.mark.fast
def test_every_code_is_explained() -> None:
    """A code an agent cannot look up is a code it cannot act on.

    A document's mistake carries a language code and a call's mistake does not,
    so a caller can route on `is_language` and land on the right reference.
    """
    assert set(EXPLANATIONS) == set(SophiosErrorCode)
    for code, explanation in EXPLANATIONS.items():
        text = explanation.meaning + explanation.fix
        assert explanation.meaning and explanation.fix and '|' not in text and '\n' not in text, code
        if explanation.kind in (Kind.DOCUMENT, Kind.CALL):
            assert code.is_language == (explanation.kind is Kind.DOCUMENT), code


@pytest.mark.fast
def test_explain_prints_what_a_code_means_and_the_fix(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """`--explain` answers from the catalog, with no workflow, and exits 0."""
    assert run_cli(monkeypatch, '--explain', 'WIC011') == 0
    printed = capsys.readouterr().out
    assert printed.startswith('wic011 (document): ') and '\nFix: ' in printed and '!ii' in printed


_ROW = re.compile(r'^\| `(?P<code>[a-z]{3}\d{3})` \| (?P<kind>[a-z]+) \| (?P<meaning>.+) \| (?P<fix>.+) \|$')


@pytest.mark.fast
def test_the_error_codes_page_says_what_the_code_says() -> None:
    """docs/error_codes.md is the catalog, written out."""
    page = REPO_ROOT / 'docs' / 'error_codes.md'
    rows = {found['code']: (found['kind'], found['meaning'], found['fix'])
            for found in map(_ROW.match, page.read_text(encoding='utf-8').splitlines()) if found}
    assert rows == {str(code): (str(e.kind), e.meaning, e.fix) for code, e in EXPLANATIONS.items()}


_SPANS = st.builds(SourceSpan, st.text('aw.', min_size=1, max_size=4), *[st.integers(1, 9)] * 4)


@pytest.mark.fast
@given(st.sampled_from(Severity), st.sampled_from(SophiosErrorCode), st.text(max_size=8), st.none() | _SPANS,
       st.none() | st.builds(Locator, st.text('ab', min_size=1, max_size=2), st.none() | st.integers(1, 9),
                             st.none() | st.text('fp', min_size=1, max_size=2)))
@example(Severity.ERROR, SophiosErrorCode.MISSING_INPUT_FILE, 'gone.txt missing', None, None)
@example(Severity.NOTE, SophiosErrorCode.UNRESOLVED_INPUT, 'worth knowing', None, None)
@example(Severity.NOTE, SophiosErrorCode.INFERENCE_GUESS, 'cat.file <- touch.file (1 other fits)',
         SourceSpan('w.wic', 2, 7, 2, 9), Locator(step='cat', index=3, port='file'))
@FAST
def test_a_diagnostic_is_one_json_object_it_can_be_rebuilt_from(
        severity: Severity, code: SophiosErrorCode, message: str, span: SourceSpan | None,
        locator: Locator | None) -> None:
    """The object `--diagnostics json` prints has every key docs/cli-reference.md lists, null where Sophios does not
    know the value, so a reader never tests for a key; it carries the position, the step and port, and the catalog's
    kind and fix, so a reader rebuilds the diagnostic from it. The text form reads where, how serious, the code and
    the message, and a compile-phase failure that knows no line invents none. A note does not fail anything."""
    diagnostics = Diagnostics()
    (diagnostics.error if severity is Severity.ERROR else diagnostics.note)(code, message, span, locator)
    [found] = list(diagnostics)
    written = json.loads(json.dumps(found.to_json()))
    assert set(written) == {'severity', 'code', 'kind', 'message', 'fix', 'file', 'line', 'column', 'end_line',
                            'end_column', 'step', 'index', 'port'}
    assert (written['kind'], written['fix']) == (str(code.explanation.kind), code.explanation.fix)
    where = [written[key] for key in ('file', 'line', 'column', 'end_line', 'end_column')]
    assert Diagnostic(Severity(written['severity']), SophiosErrorCode(written['code']), written['message'],
                      None if where[0] is None else SourceSpan(*where),
                      None if written['step'] is None else Locator(written['step'], written['index'], written['port'])
                      ) == found
    said = f'{where[0]}:{where[1]}:{where[2]}: ' * (span is not None) + f'{severity} [{code}] {message}'
    assert str(found) == said if locator is None else str(found).startswith(said)
    assert diagnostics.has_errors is (severity is Severity.ERROR)


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0,
                    reason='directory modes do not stop Windows or root')
def test_a_directory_sophios_cannot_write_is_wic021(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """A working directory where `autogenerated/` cannot be created is one `wic021` line, not a PermissionError."""
    locked = tmp_path / 'locked'
    locked.mkdir(mode=0o555)
    monkeypatch.chdir(locked)
    assert run_cli(monkeypatch, '--yaml', HELLOWORLD, '--generate_cwl_workflow') == 1
    printed = capsys.readouterr().err
    assert 'error [wic021] Sophios writes the compiled workflow to' in printed
    assert 'give --basepath a directory you can write to' in printed


# touch.cwl runs in docker.io/library/debian:bookworm-slim
_TOUCH = 'steps:\n- id: touch\n  in:\n    filename: !ii a.txt\n'


@pytest.fixture(name='machine')
def _machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., list[object]]:
    """Replace `subprocess.run` with a machine whose `docker` is 'running', 'missing' or answers an exit status, with
    `processes` Docker Desktop processes and the programs in `missing` off PATH; returns the commands it was given."""
    monkeypatch.chdir(tmp_path)

    def install(engine: str = 'running', processes: int = 0, said: bytes = b'',
                missing: tuple[str, ...] = ()) -> list[object]:
        calls: list[object] = []

        def run(cmd: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(cmd)
            if cmd[0] == 'pgrep':
                return subprocess.CompletedProcess(cmd, 0, stdout=b'1\n' * processes, stderr=b'')
            if cmd[0] == 'docker' and engine == 'missing':
                raise FileNotFoundError('docker')
            if cmd[0] == 'docker' and engine == 'fails':
                return subprocess.CompletedProcess(cmd, 1, stdout=b'', stderr=said)
            return subprocess.CompletedProcess(cmd, 0, stdout=b'', stderr=b'')
        monkeypatch.setattr(subprocess, 'run', run)
        monkeypatch.setattr(sys, 'platform', 'linux')
        which = shutil.which
        monkeypatch.setattr(shutil, 'which', lambda name, *a, **k: None if name in missing else which(name, *a, **k))
        return calls
    return install


def _pulled(calls: Sequence[object]) -> bool:
    return any(isinstance(cmd, list) and cmd[0] == 'cwl-docker-extract' for cmd in calls)


def _w(tmp_path: Path, source: str | None) -> Path:
    """The workflow to run: helloworld when source is None, else source written as tmp_path/w.wic."""
    return HELLOWORLD if source is None else write_tree(tmp_path, {'w.wic': source}) / 'w.wic'


def _wic015(err: str) -> list[str]:
    return [line for line in err.splitlines() if '[wic015]' in line]


class _ModeRow(NamedTuple):
    engine: str
    source: str | None
    mode: str
    written: Any
    asked: bool | None
    pulled: bool


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    pytest.param(_ModeRow('missing', None, '--generate_run_script', {'contains': ['run.sh']}, False, False),
                 id='no-containers'),
    pytest.param(_ModeRow('running', _TOUCH, '--generate_run_script', {'contains': ['run.sh']}, None, True),
                 id='run-script'),
    pytest.param(_ModeRow('running', _TOUCH, '--check', ['w.wic'], True, False), id='check')])
def test_what_each_mode_does_to_the_machine(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str], row: _ModeRow) -> None:
    """A workflow without containers needs no engine, and still gets its run.sh. With containers,
    `--generate_run_script` asks the engine and pulls the images; `--check` asks it, then stops without writing,
    pulling or running. `asked` is None where the engine is not checked."""
    calls = machine(row.engine)
    if row.mode == '--check':
        monkeypatch.setattr(run_local, 'run', lambda _request: pytest.fail('--check must not run the workflow'))
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, row.source), row.mode) is None
    assert match(row.written, sorted(path.name for path in tmp_path.iterdir()))
    assert row.asked is None or (['docker', 'info'] in calls) is row.asked
    assert _pulled(calls) is row.pulled
    assert row.mode != '--check' or 'Checked w: ' in capsys.readouterr().out


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    ({'engine': 'missing'}, None, None, ['docker is not installed (it is not on PATH), and this workflow runs tools in '
                                         'containers (docker.io/library/debian:bookworm-slim): install docker'], []),
    ({'engine': 'fails', 'said': b'failed to connect to the docker API\nsecond line'},
     'unix:///nonexistent/sophios.sock', None,
     ['its engine is not reachable: the socket /nonexistent/sophios.sock does not exist ',
      '(failed to connect to the docker API): start the engine'], []),
    ({'engine': 'fails', 'said': b'Is the docker daemon running?'}, None, None,
     ['`docker info` exited with status 1 (Is the docker daemon running?)'], ['usermod']),
    ({'engine': 'fails', 'said': b'  whatever the engine said\n'}, 'tcp://engine.invalid:2375',
     '--ignore_docker_install',
     ['`docker info` exited with status 1 (whatever the engine said): run `docker info` to see why'], []),
    ({'processes': 1001}, None, '--ignore_docker_processes',
     ['1001 docker processes are running', '--ignore_docker_processes'], ['Warning']),
], ids=['a missing engine names the image that needs it', 'a missing socket is named and the engine quoted',
        'a stopped engine with no default socket is not blamed on permissions',
        'an engine that fails for another reason is quoted with its status, unless ignored',
        'too many docker processes is one line, unless ignored'])
def test_an_engine_that_cannot_be_used_is_one_wic015_line(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str], row: tuple[Any, ...]) -> None:
    """A step running in a container needs a usable engine; the line says why this one is not, and nothing is pulled.
    The flag that waives the check skips it, and the images are still pulled."""
    machine_is, host, waiver, says, never = row
    calls = machine(**machine_is)
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    if host:
        monkeypatch.setenv('DOCKER_HOST', host)
    monkeypatch.setattr(preflight, 'DEFAULT_DOCKER_SOCKET', tmp_path / 'absent.sock')
    code = run_cli(monkeypatch, '--yaml', _w(tmp_path, _TOUCH), '--generate_run_script')
    line, = _wic015(capsys.readouterr().err)
    assert code == 1 and not _pulled(calls)
    assert all(text in line for text in says) and not any(text in line for text in never), line
    if waiver:
        assert run_cli(monkeypatch, '--yaml', _w(tmp_path, _TOUCH), '--generate_run_script', waiver) is None
        assert _pulled(calls)


@pytest.mark.fast
def test_a_file_uri_in_the_inputs_file_is_read_as_this_platforms_path(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `file:` location names the file in this platform's own spelling: on Windows a drive path, not `/C:/...`."""
    machine('running')
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', None)
    data = tmp_path / 'in x.txt'
    write_tree(tmp_path, {data.name: 'x', 'job.yml': f"file:\n  class: File\n  location: '{data.as_uri()}'\n"})
    # returns: the file is found where the URI names it
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, _CAT_INPUT),
                   '--check', '--inputs_file', tmp_path / 'job.yml') is None


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='file modes do not stop Windows or root')
def test_an_engine_whose_socket_is_not_yours_says_so(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('fails', said=b'denied')
    socket = tmp_path / 'docker.sock'
    socket.write_text('', encoding='utf-8')
    socket.chmod(0)
    monkeypatch.setenv('DOCKER_HOST', f'unix://{socket}')
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, _TOUCH), '--generate_run_script') == 1
    line, = _wic015(capsys.readouterr().err)
    assert f'you may not use its socket {socket} (denied): add your user to the docker group' in line


@pytest.mark.skipif(shutil.which('docker') is None, reason='needs the docker CLI')
def test_the_docker_cli_with_no_daemon_is_named_not_reachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool = tmp_path / 'tool.cwl'
    tool.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nrequirements:\n  DockerRequirement:\n'
                    '    dockerPull: docker.io/bash:4.4\nbaseCommand: [echo]\ninputs: {}\noutputs: {}\n',
                    encoding='utf-8')
    # short: socket paths max out near 100 bytes
    monkeypatch.setenv('DOCKER_HOST', 'unix:///nonexistent/no-daemon.sock')
    with pytest.raises(SophiosError) as caught:
        preflight.check(preflight.needs([tool]), preflight.RunSettings('docker', str(tmp_path), ignore_processes=True))
    found, = caught.value.diagnostics
    assert found.code is SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE
    assert 'the socket /nonexistent/no-daemon.sock does not exist' in found.message


_CAT = 'steps:\n- id: cat\n  in:\n    file: !ii\n      class: File\n      location: {}\n'
_CAT_INPUT = 'inputs:\n  file: File\nsteps:\n- id: cat\n  in:\n    file: file\n'


def _wic016(err: str) -> list[str]:
    return [line for line in err.splitlines() if '[wic016]' in line]


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    (_CAT.format('absent.txt'), None, ["input 'cat~file' (from the workflow, whose relative paths are read from ",
                                       "names 'absent.txt', which does not exist at ", 'create the file']),
    (_CAT_INPUT, 'file:\n  class: File\n  location: absent.txt\nn: 3\n',
     ["input 'file' (from --inputs_file, whose relative paths are read from "]),
], ids=['a missing input is named before any image is pulled', 'a missing path in the inputs file is named'])
def test_a_missing_input_is_one_wic016_line_naming_where_it_was_read(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str], row: tuple[Any, ...]) -> None:
    source, job, said = row
    calls = machine('running')
    if job:
        (tmp_path / 'job.yml').write_text(job, encoding='utf-8')
    code = run_cli(monkeypatch, '--yaml', _w(tmp_path, source), '--generate_run_script',
                   *(('--inputs_file', tmp_path / 'job.yml') if job else ()))
    line, = _wic016(capsys.readouterr().err)
    assert code == 1 and all(text in line for text in said) and not _pulled(calls)


@pytest.mark.fast
def test_a_relative_path_in_the_inputs_file_is_read_beside_the_inputs_file(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CWL v1.2 section 5.1.5: the base IRI of the inputs document, not the working directory, at any depth."""
    machine('running')
    jobs = tmp_path / 'jobs'
    write_tree(jobs, {'data/a.txt': 'x', 'job.yml': 'file:\n  class: File\n  location: data/a.txt\n'
                      '  secondaryFiles:\n  - class: File\n    path: data/a.txt\n'})
    (tmp_path / 'cwd').mkdir()
    monkeypatch.chdir(tmp_path / 'cwd')
    # returns: nothing missing
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path / 'cwd', _CAT_INPUT), '--generate_run_script',
                   '--inputs_file', jobs / 'job.yml') is None
    written = yaml.safe_load((tmp_path / 'cwd' / 'autogenerated' / 'w_inputs.yml').read_text(encoding='utf-8'))
    assert written['file']['location'] == (jobs / 'data' / 'a.txt').as_uri()
    assert written['file']['secondaryFiles'][0]['path'] == str(jobs / 'data' / 'a.txt')


def _output_target_project(tmp_path: Path) -> Path:
    """A config whose search path holds `write_dir`, a tool that creates the directory `outDir` names."""
    write_tree(tmp_path, {'write_dir.cwl': (
        'cwlVersion: v1.2\nclass: CommandLineTool\nrequirements:\n  InitialWorkDirRequirement:\n'
        '    listing:\n    - entry: $(inputs.outDir)\n      writable: true\n  InlineJavascriptRequirement: {}\n'
        'baseCommand: [mkdir]\ninputs:\n  outDir: Directory\n'
        'outputs:\n  outDir:\n    type: Directory\n    outputBinding:\n      glob: $(inputs.outDir.basename)\n')})
    return search_config(tmp_path / 'config.json', [tmp_path], [tmp_path])


@pytest.mark.fast
def test_an_output_target_directory_is_not_a_missing_input(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The runner gets it as the name of a directory the step creates, which does not exist yet."""
    machine('running')
    source = 'steps:\n- id: write_dir\n  in:\n    outDir: !ii result.outDir\n'
    # returns: nothing missing
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, source), '--generate_run_script',
                   '--config_file', _output_target_project(tmp_path)) is None


@pytest.mark.fast
def test_an_output_target_directory_in_the_inputs_file_is_a_name(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Written as CWL writes a Directory, it still names the directory the step creates: it is not made
    absolute against the inputs file, and it is not looked for."""
    machine('running')
    (tmp_path / 'job.yml').write_text(
        'write_dir~outDir: {class: Directory, location: result.outDir}\n', encoding='utf-8')
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, 'steps:\n- id: write_dir\n'), '--generate_run_script',
                   '--config_file', _output_target_project(tmp_path), '--inputs_file', tmp_path / 'job.yml') is None
    job = yaml.safe_load((tmp_path / 'autogenerated' / 'w_inputs.yml').read_text(encoding='utf-8'))
    assert job['write_dir~outDir'] == 'result.outDir'


@pytest.mark.fast
def test_an_inputs_file_that_is_not_a_mapping_is_one_clear_error(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('running')
    (tmp_path / 'job.yml').write_text('- a\n- b\n', encoding='utf-8')
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, _CAT_INPUT),
                   '--generate_run_script', '--inputs_file', tmp_path / 'job.yml') == 1
    err = capsys.readouterr().err
    assert 'must be a mapping of input names to values, not a list' in err and 'Traceback' not in err


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='file modes do not stop Windows or root')
def test_an_unreadable_input_is_named_with_the_fix(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('running')
    locked = tmp_path / 'locked.txt'
    locked.write_text('x', encoding='utf-8')
    locked.chmod(0)
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, _CAT.format('locked.txt')), '--generate_run_script') == 1
    line, = _wic016(capsys.readouterr().err)
    assert 'which you may not read' in line and 'chmod u+r' in line


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    ({'engine': 'fails', 'said': b'stopped'}, 'tcp://engine.invalid:2375', '--generate_run_script'),
    ({'engine': 'missing'}, None, '--check'),
], ids=['a missing input and a stopped engine are reported together', 'check reports what a run would hit'])
def test_a_missing_input_and_an_unusable_engine_are_reported_in_one_pass(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str], row: tuple[Any, ...]) -> None:
    """Both problems are one line each, under --check as under a run."""
    machine_is, host, mode = row
    machine(**machine_is)
    if host:
        monkeypatch.setenv('DOCKER_HOST', host)
    code = run_cli(monkeypatch, '--yaml', _w(tmp_path, _CAT.format('absent.txt')), mode)
    err = capsys.readouterr().err
    assert code == 1 and len(_wic016(err)) == 1 and len(_wic015(err)) == 1


@pytest.mark.fast
def test_a_directory_beside_the_workflow_reaches_the_run_where_it_is(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The job names it beside the workflow, so the run reads it in place, however large; nothing is copied."""
    machine('running')
    write_tree(tmp_path, {'data/a.txt': 'x'})
    source = ('steps:\n- id: subdirectory\n  in:\n    directory: !ii\n      class: Directory\n'
              '      location: data\n    glob_pattern: !ii a.txt\n')
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, source), '--generate_run_script') is None
    job = yaml.safe_load((tmp_path / 'autogenerated' / 'w_inputs.yml').read_text(encoding='utf-8'))
    directory, = [value for value in job.values() if isinstance(value, dict)]
    assert directory['location'] == (tmp_path / 'data').as_uri()
    assert not (tmp_path / 'autogenerated' / 'data').exists()


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    ('cwltool_filterlog', None, '--generate_run_script', (),
     'error [wic029] run.sh calls cwltool_filterlog, which is not on PATH: install Sophios'),
    ('toil-cwl-runner', None, '--run_local', ('--cwl_runner', 'toil-cwl-runner'),
     'error [wic029] the run calls toil-cwl-runner, which is neither in this Python environment nor on PATH: '
     'install Toil'),
    ('cwl-docker-extract', _TOUCH, '--generate_run_script', ('--container_engine', 'singularity'),
     'error [wic029] pulling the images for singularity needs cwl-docker-extract'),
], ids=['a run script whose runner is missing', 'a run whose runner is missing', 'a missing image puller'])
def test_a_program_the_run_calls_that_is_missing_is_wic029(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str], row: tuple[Any, ...]) -> None:
    """The runner is a program: one that is not installed is named before anything is pulled or started."""
    program, source, mode, flags, said = row
    calls = machine(missing=(program,))
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', None)
    code = run_cli(monkeypatch, '--yaml', _w(tmp_path, source), mode, *flags)
    assert code == 1 and said in capsys.readouterr().err
    assert not (tmp_path / 'run.sh').exists() and not _pulled(calls)


class _CheckRow(NamedTuple):
    engine: str
    source: str
    flags: tuple[str, ...]
    exit_code: int | None


@pytest.mark.fast
@pytest.mark.parametrize('row', [
    _CheckRow('running', _TOUCH, (), None),                     # the check passes
    _CheckRow('missing', _CAT.format('absent.txt'), (), 1),     # it finds what a run would hit
    _CheckRow('running', _TOUCH, ('--graphviz',), 2),           # a drawing would be written, so it is refused
], ids=['passes', 'fails', 'refused'])
def test_a_check_leaves_an_earlier_compile_as_it_was(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        row: _CheckRow) -> None:
    """Passing, failing or refused, --check writes nothing into the base path."""
    machine(row.engine)
    basepath = tmp_path / 'autogenerated'
    basepath.mkdir()
    for name in ('w.cwl', 'w_inputs.yml'):
        (basepath / name).write_text(f'an earlier {name}\n', encoding='utf-8')
    before = {path: path.read_bytes() for path in basepath.rglob('*')}
    assert run_cli(monkeypatch, '--yaml', _w(tmp_path, row.source), '--check', *row.flags) == row.exit_code
    assert {path: path.read_bytes() for path in basepath.rglob('*')} == before


@pytest.mark.fast
def test_graphviz_without_dot_is_a_note(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine(missing=('dot',))
    assert run_cli(monkeypatch, '--yaml', HELLOWORLD, '--generate_cwl_workflow', '--graphviz') is None
    captured = capsys.readouterr()
    assert 'note [wic029] --graphviz needs the dot program' in captured.err
    assert 'Warning: Cannot generate graphviz' not in captured.out


# The upfront image fetch is complete: podman pulls with podman, loaded and imported images are fetched,
# and a failure is one wic037


def _tool(directory: Path, docker: dict[str, str]) -> Path:
    """A CWL tool in `directory` whose DockerRequirement is `docker`."""
    path = directory / 'tool.cwl'
    path.write_text(json.dumps({'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'true',
                                'inputs': [], 'outputs': [], 'requirements': {'DockerRequirement': docker}}),
                    encoding='utf-8')
    return path


@pytest.fixture(name='engine')
def _engine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """A machine on which every command succeeds, except those starting with `failing`, which exit 1 and say
    `said` on stderr. Returns the list of commands it was given."""
    def install(failing: tuple[str, ...] = (), said: str = '') -> list[list[str]]:
        calls: list[list[str]] = []

        def run(cmd: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if failing and tuple(cmd[:len(failing)]) == failing:
                return subprocess.CompletedProcess(cmd, 1, stdout='', stderr=said)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        monkeypatch.setattr(subprocess, 'run', run)
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setattr(shutil, 'which', lambda name, *a, **k: f'/bin/{name}')
        return calls
    return install


def _prepare(tool: Path, tmp_path: Path, engine: str = 'docker') -> None:
    """The pre-flight a local run makes: check, then fetch."""
    preflight.prepare([tool], preflight.RunSettings(engine, str(tmp_path)))


_PULL: Final = ['cwl-docker-extract', '--force-download', '--container-engine', '{engine}', '{tool}']


@pytest.mark.fast
@pytest.mark.parametrize(('docker', 'engine_name', 'fetched'), [
    ({'dockerPull': 'docker.io/bash:4.4'}, 'docker', [_PULL]),
    ({'dockerPull': 'docker.io/bash:4.4'}, 'podman', [_PULL]),
    ({'dockerPull': 'docker.io/bash:4.4', 'dockerImageId': 'bash:4.4'}, 'docker', []),
    ({'dockerLoad': '{archive}'}, 'docker', [['docker', 'load', '-i', '{archive}']]),
    ({'dockerLoad': '{archive}'}, 'podman', [['podman', 'load', '-i', '{archive}']]),
    ({'dockerImport': 'https://example.org/rootfs.tar', 'dockerImageId': 'rootfs:1'}, 'podman',
     [['podman', 'import', 'https://example.org/rootfs.tar', 'rootfs:1']]),
], ids=['pull with docker', 'pull with podman', 'a pull loads and imports nothing', 'load with docker',
        'load with podman', 'import under its image id'])
def test_images_are_fetched_with_the_chosen_engine(engine: Callable[..., list[list[str]]], tmp_path: Path,
                                                   docker: dict[str, str], engine_name: str,
                                                   fetched: list[list[str]]) -> None:
    """Podman pulls with podman: the engine reaches cwl-docker-extract. A dockerLoad file is loaded, and a
    dockerImport imported as its dockerImageId, with the chosen engine; a dockerPull needs the pull and nothing else."""
    calls = engine()
    archive = tmp_path / 'image.tar'
    archive.write_bytes(b'')
    tool = _tool(tmp_path, {key: value.format(archive=archive) for key, value in docker.items()})
    _prepare(tool, tmp_path, engine_name)
    fetched = [[part.format(engine=engine_name, tool=tool, archive=archive) for part in cmd] for cmd in fetched]
    assert all(cmd in calls for cmd in fetched)
    loaded_or_imported = [cmd for cmd in calls if cmd[1:2] in (['load'], ['import'])]
    assert loaded_or_imported == [cmd for cmd in fetched if cmd[0] != 'cwl-docker-extract']


@pytest.mark.fast
def test_a_docker_load_url_is_downloaded_then_loaded(engine: Callable[..., list[list[str]]],
                                                     monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dockerLoad URL is downloaded, then loaded from the file."""
    calls = engine()

    class Response:
        def __enter__(self) -> 'Response':
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def raise_for_status(self) -> None:
            """A successful response."""
            return None

        def iter_content(self, _size: int) -> list[bytes]:
            """The body in one chunk."""
            return [b'image']

    monkeypatch.setattr(preflight.requests, 'get', lambda *_a, **_k: Response())
    _prepare(_tool(tmp_path, {'dockerLoad': 'https://example.org/image.tar'}), tmp_path)
    loads = [cmd for cmd in calls if cmd[:3] == ['docker', 'load', '-i']]
    assert len(loads) == 1


@pytest.mark.fast
@pytest.mark.parametrize('docker, failing, doing', [
    ({'dockerPull': 'docker.io/nope:1'}, ('cwl-docker-extract',), 'pull docker.io/nope:1'),
    ({'dockerLoad': '{archive}'}, ('docker', 'load'), 'load {archive}'),
    ({'dockerImport': 'rootfs.tar', 'dockerImageId': 'rootfs:1'}, ('docker', 'import'),
     'import rootfs:1 from rootfs.tar'),
])
def test_a_failed_pull_load_or_import_is_one_wic037_quoting_the_engine(
        engine: Callable[..., list[list[str]]], tmp_path: Path, docker: dict[str, str], failing: tuple[str, ...],
        doing: str) -> None:
    """One wic037 names what failed and quotes the last stderr line."""
    archive = tmp_path / 'image.tar'
    archive.write_bytes(b'')
    docker = {key: value.format(archive=archive) for key, value in docker.items()}
    engine(failing, said='first line\nError: pull access denied\n')
    tool = _tool(tmp_path, docker)
    with pytest.raises(SophiosError) as raised:
        _prepare(tool, tmp_path)
    [diagnostic] = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.IMAGE_UNAVAILABLE
    assert doing.format(archive=archive) in diagnostic.message
    assert 'Error: pull access denied' in diagnostic.message and 'first line' not in diagnostic.message


@pytest.mark.fast
def test_a_failed_pull_quotes_the_engines_error_line_not_the_extractors_traceback(
        engine: Callable[..., list[list[str]]], tmp_path: Path) -> None:
    """cwl-docker-extract re-raises the engine's output as `SubprocessError(<bytes>)`; wic037 quotes the
    engine's `Error:` line, decoded."""
    traceback = ('Traceback (most recent call last):\n  File "extract.py", line 1, in <module>\n'
                 "subprocess.SubprocessError: b'Trying to pull docker.io/nope:1... \\r\\n"
                 "Error: requested access to the resource is denied \\r\\n'\n")
    engine(('cwl-docker-extract',), said=traceback)
    tool = _tool(tmp_path, {'dockerPull': 'docker.io/nope:1'})
    with pytest.raises(SophiosError) as raised:
        _prepare(tool, tmp_path)
    [diagnostic] = raised.value.diagnostics
    assert 'Error: requested access to the resource is denied)' in diagnostic.message
    assert 'SubprocessError' not in diagnostic.message and "b'" not in diagnostic.message


@pytest.mark.fast
def test_check_loads_and_imports_nothing(engine: Callable[..., list[list[str]]], tmp_path: Path) -> None:
    """--check checks and fetches nothing."""
    calls = engine()
    tool = _tool(tmp_path, {'dockerLoad': str(tmp_path / 'image.tar'), 'dockerImageId': 'x:1'})
    preflight.check(preflight.needs([tool]), preflight.RunSettings('docker', str(tmp_path)))
    assert not [cmd for cmd in calls if len(cmd) > 1 and cmd[1] in ('load', 'import')]
    assert not _pulled(calls)


#: Where a document writes a name, as text with a `{name}` slot, and what the refusal calls it.
_NAMED: Final = {
    'step id': "steps:\n- id: mk_file\n  in:\n    name: !ii a\n- id: '{name}'\n  run: mk_file\n  in:\n"
               "    name: !ii b\n",
    'mapping step id': "steps:\n  mk_file:\n    in:\n      name: !ii a\n  '{name}':\n    run: mk_file\n    in:\n"
                       "      name: !ii b\n",
    'workflow input': "inputs:\n  '{name}': string\nsteps:\n- id: mk_file\n  in:\n    name: !ii a\n",
    'workflow output': "outputs:\n  '{name}': {{outputSource: mk_file/file}}\nsteps:\n- id: mk_file\n  in:\n"
                       "    name: !ii a\n",
    'workflow name': "steps:\n- id: mk_file\n  in:\n    name: !ii a\n",
}


@pytest.mark.fast
@given(st.tuples(st.sampled_from(sorted(_NAMED)), author_names))
@pinned(*[(where, name) for where in ('step id', 'mapping step id')
          for name in ['a~b', 'echo@2', 'a/b', 'a b', 'a#b', '.', '..', '.hidden', 'con', 'aux', 'nul', 'com1']],
        *[(where, name) for where in ('workflow input', 'workflow output', 'workflow name')
          for name in ['a~b', '.hidden']])
@budget(100)
def test_a_step_id_outside_the_allowed_characters_is_refused_at_its_line(case: tuple[str, str]) -> None:
    """A step id is part of the compiled ids, of the names made with `~` and `@`, and of a file name; a workflow input
    or output is an id of the compiled CWL as a step is, and a workflow's name the stem of the files it is written to.
    So each is letters, digits, `_`, `.` and `-`, not starting with `.` (docs/wic-reference.md), or one wic045 at the
    step's line naming it; else it compiles as written. A step named like a Windows device (`con`, `aux`) compiles:
    its file name starts with its workflow's."""
    where, name = case
    try:
        compiled = compile_hermetic_source(_NAMED[where].format(name=name), name if where == 'workflow name' else 'w')
    except SophiosError as caught:
        [found] = list(caught.diagnostics)
        assert name[0] == '.' or set(name) - set(string.ascii_letters + string.digits + '_.-'), name
        assert found.code is SophiosErrorCode.INVALID_NAME, found
        assert f"{where.removeprefix('mapping ')} {name!r} is not a name Sophios can write" in found.message, found
        assert found.span is not None and found.span.file.endswith('.wic'), found
        assert where not in ('step id', 'mapping step id') or (found.span.start_line, found.span.start_column) == (
            5, 3), found
        return
    assert name[0] != '.' and not set(name) - set(string.ascii_letters + string.digits + '_.-'), name
    assert where not in ('step id', 'mapping step id') or [
        step['id'] for step in compiled.artifact.cwl['steps']] == ['mk_file', name]
