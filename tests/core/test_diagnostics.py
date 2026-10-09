"""The library reports failures; only the CLI adapter exits.

Three claims, each enforced below: compilation never terminates the process,
every failure path produces at least one diagnostic, and the CLI's exit codes
match their pre-change behaviour.

The no-exit claim's dynamic half lives in the generator properties
(`test_pipeline.py`, `test_emit.py` and the rest of the oracle suite), which
compile generated workflows with no `SystemExit` arm in any handler, so a
process-killing path fails them; the weekly property lane runs them deep.

The at-least-one-diagnostic claim holds by construction — `SophiosError`
cannot be built with zero diagnostics — plus one test per converted site proving the site actually
raises it with the messages it used to print.
"""
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from sophios import main as cli
from sophios import preflight, run_local
from sophios.ir.complete import coerce_job_value
from sophios.ir.declarations import port_declaration
from sophios.ir.types import AuthoredName
from sophios.lang import InlineLiteral, parse
from sophios.lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from sophios.lang.error_codes import EXPLANATIONS, SophiosErrorCode
from sophios.lang.spans import SourceSpan
from sophios.python_cwl_adapter import check_args_match_inputs
from sophios.wic_types import StepId, Tool

from .hermetic import compile_hermetic, compile_hermetic_source
from .provocations import _provoke_duplicate_document_name
from .synthetic_tools import SYNTHETIC_NS, clt


# --------------------------------------------------------------------------
# A failure always has something to say
# --------------------------------------------------------------------------


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


@pytest.mark.fast
def test_spanless_diagnostics_print_without_a_location() -> None:
    """Compile-phase failures may not know a line; the string form must not
    invent one."""
    diagnostic = Diagnostic(Severity.ERROR, SophiosErrorCode.MISSING_INPUT_FILE, 'gone.txt missing')
    assert str(diagnostic) == 'error [wic016] gone.txt missing'


@pytest.mark.fast
def test_a_note_is_reported_without_being_an_error() -> None:
    """A note says something the reader should know; it does not fail anything."""
    diagnostics = Diagnostics()
    diagnostics.note(SophiosErrorCode.UNRESOLVED_INPUT, 'worth knowing')
    assert not diagnostics.has_errors
    assert [str(d) for d in diagnostics] == ['note [wic011] worth knowing']


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


@pytest.mark.fast
def test_literal_type_mismatch_reports() -> None:
    """The typed job boundary reports a literal that will not coerce to its
    input's declared type instead of letting `int()`/`float()` raise a bare
    `ValueError`. `!ii` places no constraint relating a literal to the
    declared CWL type of the input it binds, so this is reachable from real
    documents (e.g. `in: {n: !ii _}` against a tool declaring `n: int`).

    The message must name all three of the input, the declared type, and the
    offending literal, since the user's next move is to fix one of them.
    """
    with pytest.raises(SophiosError) as caught:
        coerce_job_value(AuthoredName('n'), port_declaration({'type': 'int'}), '_')

    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    message = caught.value.diagnostics[0].message
    assert 'n' in message
    assert 'int' in message
    assert '_' in message


@pytest.mark.fast
def test_literal_type_mismatch_reports_a_null_array_element() -> None:
    """A null inside an array literal is diagnosed, not raised as a bare
    `TypeError`.

    `populate_input_value`'s null guard inspects only the top-level value, so
    `[1, null]` against an `int[]` passes it and reaches `populate_scalar_val`
    one element down, where `int(None)` is a `TypeError` rather than the
    `ValueError` a non-numeric string gives. Both are the same user error and
    get the same diagnostic.
    """
    with pytest.raises(SophiosError) as caught:
        coerce_job_value(
            AuthoredName('xs'), port_declaration({'type': {'type': 'array', 'items': 'int'}}),
            [1, None])

    assert len(caught.value.diagnostics) == 1
    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    message = caught.value.diagnostics[0].message
    assert 'xs' in message
    assert 'int' in message
    assert 'None' in message


def _job_value(declared: object, literal: object) -> object:
    """The job value `!ii literal` gives the one input of a tool that declares `declared`."""
    tool = Tool('/synthetic/probe.cwl',
                clt({'x': {'type': declared, 'inputBinding': {'position': 1}}}, {}, canonical=True))
    compiled = compile_hermetic({'steps': [{'id': 'probe', 'in': {'x': {'wic_inline_input': literal}}}]},
                                tools={StepId('probe', SYNTHETIC_NS): tool})
    (value,) = compiled.artifact.job_inputs.values()
    return value


def _job_value_written(declared: object, spelling: str) -> object:
    """The job value `!ii <spelling>`, written in a `.wic` file, gives the one input of a tool that declares
    `declared`. Only the text can say whether the literal is quoted."""
    tool = Tool('/synthetic/probe.cwl',
                clt({'x': {'type': declared, 'inputBinding': {'position': 1}}}, {}, canonical=True))
    compiled = compile_hermetic_source(f'steps:\n- id: probe\n  in:\n    x: !ii {spelling}\n',
                                       tools={StepId('probe', SYNTHETIC_NS): tool})
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


@pytest.mark.fast
@pytest.mark.parametrize('declared, literal', [
    ('int', 2.9), ('int', '007'), ('int', True), ('long', 2.9),
    ('boolean', 'yes'), ('boolean', 2),
    ('float', '1e3'), ('float', True), ('double', 'x'),
    ('float', 2**53 + 1), ('float', 10**400),
    ('string', {'seen': datetime.date(2024, 1, 15)}), ({'type': 'array', 'items': 'string'}, ['a', None]),
], ids=['float-into-int', 'text-into-int', 'bool-into-int', 'float-into-long',
        'text-into-bool', 'int-into-bool',
        'text-into-float', 'bool-into-float', 'text-into-double',
        'int-a-float-rounds', 'int-a-float-overflows',
        'date-in-a-mapping-into-string', 'null-in-a-list-into-string'])
def test_a_literal_of_the_wrong_type_is_wic020_not_converted(declared: object, literal: object) -> None:
    """`int('007')` and `bool('yes')` used to succeed, and the job carried a value
    the author never wrote."""
    with pytest.raises(SophiosError) as caught:
        _job_value(declared, literal)
    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH


@pytest.mark.fast
@pytest.mark.parametrize('declared, literal, expected', [
    ('string', 20, '20'), ('string', 1.5, '1.5'), ('string', True, 'true'), ('string', False, 'false'),
    ('string', datetime.date(2024, 1, 15), '2024-01-15'),
    ('string', {'seen': 'a'}, '{"seen": "a"}'),
    ('float', 1, 1.0), ('double', 2**53, float(2**53)), ('float', 1.0e-5, 1.0e-5),
    ('int', 3, 3), ('long', 3, 3), ('boolean', False, False),
])
def test_the_lossless_conversions_still_hold(declared: object, literal: object, expected: object) -> None:
    """YAML reads an unquoted 2024-01-15 as a date; bound to a string it is the text the author wrote."""
    value = _job_value(declared, literal)
    assert (value, type(value)) == (expected, type(expected))


@pytest.mark.fast
def test_a_float_port_takes_not_a_number() -> None:
    """`!ii .nan` is a float, and NaN is not equal to itself, so equality cannot be what accepts it."""
    value = _job_value('float', float('nan'))
    assert isinstance(value, float) and math.isnan(value)


def _parsed_literal(text: str) -> object:
    """The value the parser gives `!ii <text>`."""
    document = parse(f'steps:\n- id: probe\n  in:\n    x: !ii {text}\n', 'probe.wic').document
    assert document is not None
    literal = dict(document.steps[0].inputs)['x']
    assert isinstance(literal, InlineLiteral)
    return literal.value


@pytest.mark.fast
@pytest.mark.parametrize('text', ['1e-5', '1E3', '2.5e10'])
def test_scientific_notation_yaml_reads_as_text_says_how_to_write_a_float(text: str) -> None:
    """YAML reads a float with no signed exponent as text, so it is wic020. The message
    gives the spelling the parser does read as a float, with the value the author meant."""
    assert _parsed_literal(text) == text
    with pytest.raises(SophiosError) as caught:
        _job_value('float', text)
    message = caught.value.diagnostics[0].message
    advised = re.search(r'Write (\S+)\.$', message)
    assert advised is not None, message
    assert _parsed_literal(advised[1]) == float(text)


@pytest.mark.fast
def test_a_literal_of_another_python_type_says_which_python_type_the_port_wants() -> None:
    """`b'abc'` is a YAML binary scalar; the message names the Python type the port holds."""
    with pytest.raises(SophiosError) as caught:
        _job_value('string', b'abc')
    assert caught.value.diagnostics[0].message.endswith("its literal b'abc' is of type bytes, not a Python str.")


@pytest.mark.fast
def test_a_float_the_literal_cannot_hold_exactly_says_so() -> None:
    """An int above 2**53 used to round silently; the message names why it is rejected."""
    with pytest.raises(SophiosError) as caught:
        _job_value('float', 2**53 + 1)
    assert 'cannot hold exactly' in caught.value.diagnostics[0].message


# --------------------------------------------------------------------------
# The CLI still speaks exit codes
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_cli_converts_a_report_to_exit_1(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """A reported failure leaves the CLI with exit code 1 and no traceback, and
    the messages on **stderr**, with their code."""

    def reports(*_args: object, **_kwargs: object) -> None:
        raise SophiosError.error(SophiosErrorCode.UNRESOLVED_INPUT,
                                 'Did you forget to use !ii before x?',
                                 'If you want to compile the workflow anyway, use --allow_raw_cwl')

    monkeypatch.setattr('sys.argv', ['sophios'])
    monkeypatch.setattr(cli, '_main', reports)

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    printed = capsys.readouterr().err
    assert 'Did you forget to use !ii' in printed
    assert '--allow_raw_cwl' in printed
    assert '[wic011]' in printed


@pytest.mark.fast
def test_cli_reports_a_workflow_that_does_not_compile_on_stderr_with_its_position(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The failure a scientist sees names the file, line, column and code, all on stderr."""
    workflow = tmp_path / 'bad.wic'
    workflow.write_text('steps:\n- id: ""\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow'])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert f'Failed to compile {workflow}' in captured.err
    assert 'bad.wic:2:7: error [wic007]' in captured.err
    assert 'Failed to compile' not in captured.out
    assert 'wic007' not in captured.out


@pytest.mark.fast
def test_cli_reports_a_crash_in_one_line_and_keeps_the_whole_traceback(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure Sophios did not expect says so in one line; the file holds the traceback with its frames."""
    workflow = tmp_path / 'crash.wic'
    workflow.write_text('steps:\n- id: touch\n', encoding='utf-8')

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(cli.compiler, 'compile_source', crash)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow'])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert 'Sophios stopped on an unexpected RuntimeError: boom. The traceback is in error_crash.txt' in captured.err
    assert 'Traceback' not in captured.err and 'boom' not in captured.out
    kept = (tmp_path / 'error_crash.txt').read_text(encoding='utf-8')
    assert 'Traceback (most recent call last)' in kept and 'in crash' in kept


_TWO_TOUCHES = ('steps:\n- id: touch\n  in:\n    filename: !ii a.txt\n'
                '- id: touch\n  in:\n    filename: !ii b.txt\n- id: cat\n')


@pytest.mark.fast
def test_cli_prints_an_inference_note_on_stderr_and_succeeds(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A choice between equals is said, with its code, and the compile still succeeds."""
    workflow = tmp_path / 'two_touches.wic'
    workflow.write_text(_TWO_TOUCHES, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow'])

    cli.main()  # returning, rather than raising SystemExit, is the first assertion

    captured = capsys.readouterr()
    assert 'note [wic043]' in captured.err
    assert "step 1 'touch' output 'file'" in captured.err
    assert 'wic043' not in captured.out


@pytest.mark.fast
def test_cli_with_inference_strict_refuses_a_choice_between_equals(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--inference_strict` makes the same note an error and the exit code says so."""
    workflow = tmp_path / 'two_touches.wic'
    workflow.write_text(_TWO_TOUCHES, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow',
                                     '--inference_strict'])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    assert 'error [wic043]' in capsys.readouterr().err


@pytest.mark.fast
def test_cli_success_does_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean run returns instead of raising, exactly as before."""
    monkeypatch.setattr('sys.argv', ['sophios'])
    monkeypatch.setattr(cli, '_main', lambda *_args: None)
    cli.main()  # returning, rather than raising SystemExit, is the assertion


# --------------------------------------------------------------------------
# The public surface is actually public
# --------------------------------------------------------------------------


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


@pytest.fixture(name='cli_on_helloworld')
def _cli_on_helloworld(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., None]:
    """Run the CLI on the helloworld tutorial from a scratch directory.

    What would reach for a container engine is replaced; compiling, argument handling and the
    exit code are the CLI's own.
    """
    monkeypatch.setattr(preflight, 'prepare', lambda *_a, **_k: None)
    monkeypatch.chdir(tmp_path)
    workflow = Path(__file__).resolve().parents[2] / 'docs' / 'tutorials' / 'helloworld.wic'

    def run(*flags: str) -> None:
        monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), *flags])
        cli.main()
    return run


@pytest.mark.fast
@pytest.mark.parametrize('mode', ['--generate_cwl_workflow', '--generate_run_script'])
def test_unknown_cli_flags_are_rejected(mode: str, cli_on_helloworld: Callable[..., None],
                                        capsys: pytest.CaptureFixture[str]) -> None:
    """A flag Sophios does not know is an error, not a silent gift to the runner."""
    with pytest.raises(SystemExit) as caught:
        cli_on_helloworld(mode, '--no_such_flag')
    assert caught.value.code == 2
    assert '--no_such_flag' in capsys.readouterr().err


@pytest.mark.fast
def test_passthrough_flags_yes_sends_unrecognised_arguments_to_the_runner(
        cli_on_helloworld: Callable[..., None]) -> None:
    cli_on_helloworld('--generate_run_script', '--passthrough_flags', 'yes', '--debug')
    assert '--debug' in Path('run.sh').read_text(encoding='utf-8').split()


@pytest.mark.fast
def test_a_runner_flag_is_not_read_as_an_abbreviated_sophios_flag(cli_on_helloworld: Callable[..., None]) -> None:
    """cwltool's `--validate` reaches the runner; it is not a prefix of `--validate_plugins`."""
    cli_on_helloworld('--generate_run_script', '--passthrough_flags', 'yes', '--validate')
    assert '--validate' in Path('run.sh').read_text(encoding='utf-8').split()


@pytest.mark.fast
def test_passthrough_flags_need_a_command_that_runs_the_runner(cli_on_helloworld: Callable[..., None],
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    """`--passthrough_flags yes` sends arguments to the runner; with no runner to send them to, they are an error."""
    with pytest.raises(SystemExit) as caught:
        cli_on_helloworld('--generate_cwl_workflow', '--passthrough_flags', 'yes', '--debug')
    assert caught.value.code == 2
    assert '--run_local' in capsys.readouterr().err


@pytest.mark.fast
def test_run_local_exits_with_the_runners_exit_code(monkeypatch: pytest.MonkeyPatch,
                                                    cli_on_helloworld: Callable[..., None]) -> None:
    import sophios.run_local as rl
    monkeypatch.setattr(rl, 'run_local', lambda *_a, **_k: 3)
    with pytest.raises(SystemExit) as caught:
        cli_on_helloworld('--run_local')
    assert caught.value.code == 3


@pytest.mark.fast
@pytest.mark.parametrize(('flags', 'quiet'), [([], False), (['--quiet'], True)])
def test_cli_asks_the_runner_to_be_quiet_only_with_quiet(cli_on_helloworld: Callable[..., None],
                                                         flags: list[str], quiet: bool) -> None:
    """Without `--quiet` the runner keeps its own log level, so `--debug` can be heard."""
    cli_on_helloworld('--generate_run_script', *flags)
    assert ('--quiet' in Path('run.sh').read_text(encoding='utf-8').split()) is quiet


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32', reason='Ctrl-C reaches the run as a POSIX process-group SIGINT')
@pytest.mark.parametrize('cwl_runner', ['cwltool', 'toil-cwl-runner'])
def test_ctrl_c_during_run_local_exits_130(monkeypatch: pytest.MonkeyPatch,
                                           cli_on_helloworld: Callable[..., None], cwl_runner: str) -> None:
    """The CLI turns the interrupt `run_local` lets through into the shell's SIGINT code, whichever runner ran."""
    import sophios.run_local as rl

    def interrupted(_args: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(rl.cwltool.main, 'main', interrupted)
    monkeypatch.setattr(rl.toil.cwl.cwltoil, 'main', interrupted)
    # KeyboardInterrupt is listed so a regression fails this test instead of aborting the session.
    with pytest.raises((SystemExit, KeyboardInterrupt)) as caught:
        cli_on_helloworld('--run_local', '--cwl_runner', cwl_runner)
    assert isinstance(caught.value, SystemExit)
    assert caught.value.code == 130


@pytest.mark.fast
@pytest.mark.parametrize('flags', [[], ['--cachedir', 'mycache']])
def test_a_runner_that_raises_says_why_with_or_without_a_cachedir(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        cli_on_helloworld: Callable[..., None], flags: list[str]) -> None:
    """The traceback goes to error_<name>.txt; the message itself is printed, whatever the cache."""
    import sophios.run_local as rl

    def broken(_args: list[str]) -> int:
        raise RuntimeError('the runner broke')

    monkeypatch.setattr(rl.cwltool.main, 'main', broken)
    with pytest.raises(SystemExit) as caught:
        cli_on_helloworld('--run_local', *flags)
    assert caught.value.code == 1
    assert 'the runner broke' in capsys.readouterr().out


# --------------------------------------------------------------------------
# Compile diagnostics name where the author wrote the problem
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_an_unresolved_input_names_its_line_and_step() -> None:
    """wic011 carried a message and nothing else; the parser had the span all along."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': 'undeclared'}}]})
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.UNRESOLVED_INPUT
    assert diagnostic.span is not None and diagnostic.span.file == 'oracle.wic'
    assert diagnostic.locator == Locator(step='mk_file', index=1, port='name')


@pytest.mark.fast
def test_a_literal_type_mismatch_names_its_line_and_port() -> None:
    """wic020 names the line of the `!ii` literal and the step and port it binds."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [{'id': 'scale', 'in': {'n': {'wic_inline_input': 'x'}}}]})
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    assert diagnostic.span is not None and diagnostic.span.file == 'oracle.wic'
    assert diagnostic.span.start_line > 0
    assert diagnostic.locator == Locator(step='scale', index=1, port='n')


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
    """wic031 has no port to point at, so it points at the document that spells two ports alike."""
    with pytest.raises(SophiosError) as caught:
        _provoke_duplicate_document_name()
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.DUPLICATE_DOCUMENT_NAME
    assert diagnostic.span is not None and diagnostic.span.file == 'provoke.wic'


@pytest.mark.fast
def test_an_unresolved_input_error_does_not_call_itself_a_warning() -> None:
    """wic011 is an error: its message says what to write, with no `Warning!` in front."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': 'x'}}]})
    first = caught.value.diagnostics[0]
    assert first.code is SophiosErrorCode.UNRESOLVED_INPUT
    assert first.message == 'Did you forget to use !ii before x?'


# --------------------------------------------------------------------------
# Every code says what it means and what to do
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_every_code_is_explained() -> None:
    """A code an agent cannot look up is a code it cannot act on."""
    assert set(EXPLANATIONS) == set(SophiosErrorCode)
    for code, explanation in EXPLANATIONS.items():
        text = explanation.meaning + explanation.fix
        assert explanation.meaning and explanation.fix and '|' not in text and '\n' not in text, code


@pytest.mark.fast
def test_explain_prints_what_a_code_means_and_the_fix(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """`--explain` answers from the catalog, with no workflow, and exits 0."""
    monkeypatch.setattr('sys.argv', ['sophios', '--explain', 'WIC011'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    printed = capsys.readouterr().out
    assert printed.startswith('wic011 (document): ') and '\nFix: ' in printed and '!ii' in printed


@pytest.mark.fast
def test_explain_names_a_code_that_does_not_exist(monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """A code Sophios does not report is a usage error that names it."""
    monkeypatch.setattr('sys.argv', ['sophios', '--explain', 'wic999'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    assert 'wic999 is not a Sophios error code' in capsys.readouterr().err


_ROW = re.compile(r'^\| `(?P<code>[a-z]{3}\d{3})` \| (?P<kind>[a-z]+) \| (?P<meaning>.+) \| (?P<fix>.+) \|$')


@pytest.mark.fast
def test_the_error_codes_page_says_what_the_code_says() -> None:
    """docs/error_codes.md is the catalog, written out."""
    page = Path(__file__).resolve().parents[2] / 'docs' / 'error_codes.md'
    rows = {found['code']: (found['kind'], found['meaning'], found['fix'])
            for found in map(_ROW.match, page.read_text(encoding='utf-8').splitlines()) if found}
    assert rows == {str(code): (str(e.kind), e.meaning, e.fix) for code, e in EXPLANATIONS.items()}


@pytest.mark.fast
def test_to_json_has_every_key_when_nothing_is_known() -> None:
    """A reader never tests for a key: an unknown value is null."""
    found = Diagnostic(Severity.ERROR, SophiosErrorCode.MISSING_INPUT_FILE, 'gone.txt').to_json()
    assert set(found) == {'severity', 'code', 'kind', 'message', 'fix', 'file', 'line', 'column',
                          'end_line', 'end_column', 'step', 'index', 'port'}
    assert found['kind'] == 'machine' and found['file'] is None and found['step'] is None


@pytest.mark.fast
def test_to_json_carries_the_position_the_step_and_the_catalog_fix() -> None:
    """The object holds where, which step and port, and what to do."""
    span = SourceSpan('w.wic', 2, 7, 2, 9)
    found = Diagnostic(Severity.NOTE, SophiosErrorCode.INFERENCE_RECENCY, 'took the latest', span,
                       Locator(step='cat', index=3, port='file')).to_json()
    assert (found['severity'], found['code'], found['file'], found['line'], found['column'],
            found['end_line'], found['end_column'], found['step'], found['index'], found['port']) == (
        'note', 'wic043', 'w.wic', 2, 7, 2, 9, 'cat', 3, 'file')
    assert found['fix'] == SophiosErrorCode.INFERENCE_RECENCY.explanation.fix


@pytest.mark.fast
def test_json_diagnostics_are_one_object_per_line_with_position_kind_and_fix(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A compile error is one JSON line on stderr."""
    workflow = tmp_path / 'bad.wic'
    workflow.write_text('steps:\n- id: ""\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow',
                                     '--diagnostics', 'json'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    objects = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{')]
    assert [(o['severity'], o['code'], o['kind'], o['line'], o['column']) for o in objects] == [
        ('error', 'wic007', 'document', 2, 7)]
    assert objects[0]['file'].endswith('bad.wic')
    assert objects[0]['fix'] == SophiosErrorCode.EMPTY_STEP_ID.explanation.fix


@pytest.mark.fast
def test_a_json_note_names_its_step_and_port_and_the_compile_succeeds(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A note is a JSON line too, and the compile still succeeds."""
    workflow = tmp_path / 'two_touches.wic'
    workflow.write_text(_TWO_TOUCHES, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow',
                                     '--diagnostics', 'json'])
    cli.main()
    note, = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{')]
    assert (note['severity'], note['code'], note['step'], note['index'], note['port']) == (
        'note', 'wic043', 'cat', 3, 'file')


@pytest.mark.fast
@pytest.mark.parametrize('flag, message', [('--yaml', 'no workflow file at'), ('--inputs_file', 'no inputs file at')])
def test_a_named_file_that_does_not_exist_is_a_usage_error(
        flag: str, message: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped `--yaml` or `--inputs_file` is one usage line and exit 2, not a traceback."""
    workflow = Path(__file__).resolve().parents[2] / 'docs' / 'tutorials' / 'helloworld.wic'
    argv = {'--yaml': ['--yaml', str(tmp_path / 'nope.wic')],
            '--inputs_file': ['--yaml', str(workflow), '--inputs_file', str(tmp_path / 'nope.yml')]}[flag]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', *argv, '--generate_cwl_workflow'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    printed = capsys.readouterr().err
    assert message in printed and 'Traceback' not in printed


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0,
                    reason='directory modes do not stop Windows or root')
def test_a_directory_sophios_cannot_write_is_wic021(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """A working directory where `autogenerated/` cannot be created is one `wic021` line, not a PermissionError."""
    workflow = Path(__file__).resolve().parents[2] / 'docs' / 'tutorials' / 'helloworld.wic'
    locked = tmp_path / 'locked'
    locked.mkdir(mode=0o555)
    monkeypatch.chdir(locked)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    printed = capsys.readouterr().err
    assert 'error [wic021] Sophios writes the compiled workflow to' in printed
    assert 'run Sophios from a directory you can write to' in printed


# --------------------------------------------------------------------------
# The container engine is checked when a step runs in a container, and the line says why it cannot be used
# --------------------------------------------------------------------------

_TOUCH = 'steps:\n- id: touch\n  in:\n    filename: !ii a.txt\n'   # touch.cwl runs in docker.io/bash:4.4


@pytest.fixture(name='machine')
def _machine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[object]]:
    """Replace `subprocess.run` with a machine whose `docker` is 'running', 'missing' or answers an exit status,
    with `processes` Docker Desktop processes, and on which the programs in `missing` are not on PATH.
    Returns the list of commands it was given."""
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


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str | None, *flags: str,
         mode: str = '--generate_run_script') -> None:
    """`sophios --yaml <source> <mode> <flags>` from tmp_path; helloworld when source is None."""
    if source is None:
        workflow = Path(__file__).resolve().parents[2] / 'docs' / 'tutorials' / 'helloworld.wic'
    else:
        workflow = tmp_path / 'w.wic'
        workflow.write_text(source, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), mode, *flags])
    cli.main()


def _wic015(err: str) -> list[str]:
    return [line for line in err.splitlines() if '[wic015]' in line]


@pytest.mark.fast
def test_a_workflow_without_containers_needs_no_engine(machine: Callable[..., list[object]],
                                                       monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = machine('missing')
    _cli(monkeypatch, tmp_path, None)          # returning is the assertion: helloworld runs echo on the host
    assert (tmp_path / 'run.sh').exists()
    assert ['docker', 'info'] not in calls


@pytest.mark.fast
def test_a_missing_engine_names_the_image_that_needs_it(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    calls = machine('missing')
    with pytest.raises(SystemExit) as caught:
        _cli(monkeypatch, tmp_path, _TOUCH)
    assert caught.value.code == 1
    line, = _wic015(capsys.readouterr().err)
    assert ('docker is not installed (it is not on PATH), and this workflow runs tools in containers '
            '(docker.io/bash:4.4): install docker') in line
    assert not _pulled(calls)


@pytest.mark.fast
def test_an_engine_whose_socket_is_missing_says_so_and_quotes_the_engine(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    calls = machine('fails', said=b'failed to connect to the docker API\nsecond line')
    monkeypatch.setenv('DOCKER_HOST', 'unix:///nonexistent/sophios.sock')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH)
    line, = _wic015(capsys.readouterr().err)
    assert 'its engine is not reachable: the socket /nonexistent/sophios.sock does not exist ' in line
    assert '(failed to connect to the docker API): start the engine' in line
    assert not _pulled(calls)


@pytest.mark.fast
def test_a_stopped_engine_with_no_default_socket_is_not_blamed_on_permissions(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('fails', said=b'Is the docker daemon running?')
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    monkeypatch.setattr(preflight, 'DEFAULT_DOCKER_SOCKET', tmp_path / 'absent.sock')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH)
    line, = _wic015(capsys.readouterr().err)
    assert 'usermod' not in line
    assert '`docker info` exited with status 1 (Is the docker daemon running?)' in line


@pytest.mark.fast
def test_a_file_uri_in_the_inputs_file_is_read_as_this_platforms_path(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `file:` location names the file in this platform's own spelling: on Windows a drive path, not `/C:/...`."""
    machine('running')
    monkeypatch.setattr(run_local, 'RUNNER_UNAVAILABLE', None)
    data = tmp_path / 'in x.txt'
    data.write_text('x', encoding='utf-8')
    (tmp_path / 'job.yml').write_text(f"file:\n  class: File\n  location: '{data.as_uri()}'\n", encoding='utf-8')
    _cli(monkeypatch, tmp_path, _CAT_INPUT, '--inputs_file', str(tmp_path / 'job.yml'),
         mode='--check')   # returns: the file is found where the URI names it


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
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH)
    line, = _wic015(capsys.readouterr().err)
    assert f'you may not use its socket {socket} (denied): add your user to the docker group' in line


@pytest.mark.fast
def test_an_engine_that_fails_for_another_reason_is_quoted_with_its_status(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('fails', said=b'  whatever the engine said\n')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://engine.invalid:2375')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH)
    line, = _wic015(capsys.readouterr().err)
    assert '`docker info` exited with status 1 (whatever the engine said): run `docker info` to see why' in line


@pytest.mark.fast
def test_too_many_docker_processes_is_one_line_unless_ignored(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('running', processes=1001)
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH)
    line, = _wic015(capsys.readouterr().err)
    assert '1001 docker processes are running' in line and '--ignore_docker_processes' in line
    assert 'Warning' not in line
    _cli(monkeypatch, tmp_path, _TOUCH, '--ignore_docker_processes')   # returns


@pytest.mark.fast
def test_ignore_docker_install_skips_the_engine_check_and_still_pulls(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = machine('fails', said=b'stopped')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://engine.invalid:2375')
    _cli(monkeypatch, tmp_path, _TOUCH, '--ignore_docker_install')    # returns
    assert _pulled(calls)


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


# --------------------------------------------------------------------------
# Input paths are checked, with the input's name, before anything is pulled
# --------------------------------------------------------------------------

_CAT = 'steps:\n- id: cat\n  in:\n    file: !ii\n      class: File\n      location: {}\n'
_CAT_INPUT = 'inputs:\n  file: File\nsteps:\n- id: cat\n  in:\n    file: file\n'


def _wic016(err: str) -> list[str]:
    return [line for line in err.splitlines() if '[wic016]' in line]


@pytest.mark.fast
def test_a_missing_input_is_named_before_any_image_is_pulled(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    calls = machine('running')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _CAT.format('absent.txt'))
    line, = _wic016(capsys.readouterr().err)
    assert "input 'cat/file' (from the workflow, whose relative paths are read from " in line
    assert "names 'absent.txt', which does not exist at " in line and 'create the file' in line
    assert not _pulled(calls)


@pytest.mark.fast
def test_a_missing_path_in_the_inputs_file_is_named(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('running')
    (tmp_path / 'job.yml').write_text('file:\n  class: File\n  location: absent.txt\nn: 3\n', encoding='utf-8')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _CAT_INPUT, '--inputs_file', str(tmp_path / 'job.yml'))
    line, = _wic016(capsys.readouterr().err)
    assert "input 'file' (from --inputs_file, whose relative paths are read from " in line


@pytest.mark.fast
def test_a_relative_path_in_the_inputs_file_is_read_beside_the_inputs_file(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CWL v1.2 section 5.1.5: the base IRI of the inputs document, not the working directory, at any depth."""
    machine('running')
    jobs = tmp_path / 'jobs'
    (jobs / 'data').mkdir(parents=True)
    (jobs / 'data' / 'a.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'cwd').mkdir()
    (jobs / 'job.yml').write_text(
        'file:\n  class: File\n  location: data/a.txt\n  secondaryFiles:\n  - class: File\n    path: data/a.txt\n',
        encoding='utf-8')
    _cli(monkeypatch, tmp_path / 'cwd', _CAT_INPUT, '--inputs_file', str(jobs / 'job.yml'))   # returns: nothing missing
    written = yaml.safe_load((tmp_path / 'cwd' / 'autogenerated' / 'w_inputs.yml').read_text(encoding='utf-8'))
    assert written['file']['location'] == str(jobs / 'data' / 'a.txt')
    assert written['file']['secondaryFiles'][0]['path'] == str(jobs / 'data' / 'a.txt')


@pytest.mark.fast
def test_an_output_target_directory_is_not_a_missing_input(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The runner gets it as the name of a directory the step creates, which does not exist yet."""
    machine('running')
    (tmp_path / 'write_dir.cwl').write_text(
        'cwlVersion: v1.2\nclass: CommandLineTool\nrequirements:\n  InitialWorkDirRequirement:\n'
        '    listing:\n    - entry: $(inputs.outDir)\n      writable: true\n  InlineJavascriptRequirement: {}\n'
        'baseCommand: [mkdir]\ninputs:\n  outDir: Directory\n'
        'outputs:\n  outDir:\n    type: Directory\n    outputBinding:\n      glob: $(inputs.outDir.basename)\n',
        encoding='utf-8')
    (tmp_path / 'config.json').write_text(json.dumps({'search_paths_cwl': {'global': [str(tmp_path)], 'gpu': []},
                                                      'search_paths_wic': {'global': [str(tmp_path)]}}),
                                          encoding='utf-8')
    _cli(monkeypatch, tmp_path, 'steps:\n- id: write_dir\n  in:\n    outDir: !ii result.outDir\n',
         '--config_file', str(tmp_path / 'config.json'))   # returns: nothing missing


@pytest.mark.fast
def test_an_inputs_file_that_is_not_a_mapping_is_one_clear_error(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('running')
    (tmp_path / 'job.yml').write_text('- a\n- b\n', encoding='utf-8')
    with pytest.raises(SystemExit) as caught:
        _cli(monkeypatch, tmp_path, _CAT_INPUT, '--inputs_file', str(tmp_path / 'job.yml'))
    assert caught.value.code == 1
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
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _CAT.format('locked.txt'))
    line, = _wic016(capsys.readouterr().err)
    assert 'which you may not read' in line and 'chmod u+r' in line


@pytest.mark.fast
def test_a_missing_input_and_a_stopped_engine_are_reported_together(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('fails', said=b'stopped')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://engine.invalid:2375')
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _CAT.format('absent.txt'))
    err = capsys.readouterr().err
    assert len(_wic016(err)) == 1 and len(_wic015(err)) == 1


@pytest.mark.fast
def test_a_directory_beside_the_workflow_reaches_the_run_where_it_is(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The job names it beside the workflow, so the run reads it in place, however large; nothing is copied."""
    machine('running')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'a.txt').write_text('x', encoding='utf-8')
    _cli(monkeypatch, tmp_path, 'steps:\n- id: subdirectory\n  in:\n    directory: !ii\n      class: Directory\n'
         '      location: data\n    glob_pattern: !ii a.txt\n')
    job = yaml.safe_load((tmp_path / 'autogenerated' / 'w_inputs.yml').read_text(encoding='utf-8'))
    directory, = [value for value in job.values() if isinstance(value, dict)]
    assert directory['location'] == str(tmp_path / 'data')
    assert not (tmp_path / 'autogenerated' / 'data').exists()


# --------------------------------------------------------------------------
# A program the run calls that is missing is wic029
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_a_run_script_whose_runner_is_missing_is_wic029(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine(missing=('cwltool_filterlog',))
    with pytest.raises(SystemExit) as caught:
        _cli(monkeypatch, tmp_path, None)
    assert caught.value.code == 1
    assert ('error [wic029] run.sh calls cwltool_filterlog, which is not on PATH: install Sophios'
            in capsys.readouterr().err)
    assert not (tmp_path / 'run.sh').exists()


@pytest.mark.fast
def test_a_runner_that_cannot_run_here_is_named_before_the_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(run_local, 'RUNNER_UNAVAILABLE', 'no pwd module', raising=False)
    monkeypatch.setattr(run_local.cwltool.main, 'main', lambda _args: 0)
    workflow = Path(__file__).resolve().parents[2] / 'docs' / 'tutorials' / 'helloworld.wic'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--run_local'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert 'error [wic029] cwltool cannot run here (no pwd module): run Sophios inside WSL' in capsys.readouterr().err


@pytest.mark.fast
def test_a_missing_image_puller_is_wic029(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine(missing=('cwl-docker-extract',))
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, _TOUCH, '--container_engine', 'singularity')
    assert 'error [wic029] pulling the images for singularity needs cwl-docker-extract' in capsys.readouterr().err


# --------------------------------------------------------------------------
# --check compiles and runs the pre-flight, then stops
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_check_compiles_and_checks_then_stops_without_pulling_or_running(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    calls = machine('running')
    monkeypatch.setattr(run_local.cwltool.main, 'main', lambda _args: pytest.fail('--check must not run the workflow'))
    _cli(monkeypatch, tmp_path, _TOUCH, mode='--check')   # returns: the machine is fine
    assert 'Checked ' in capsys.readouterr().out
    assert (tmp_path / 'autogenerated' / 'w.cwl').exists()
    assert not (tmp_path / 'run.sh').exists()
    assert ['docker', 'info'] in calls and not _pulled(calls)


@pytest.mark.fast
def test_check_reports_what_a_run_would_hit(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine('missing')
    with pytest.raises(SystemExit) as caught:
        _cli(monkeypatch, tmp_path, _CAT.format('absent.txt'), mode='--check')
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert len(_wic016(err)) == 1 and len(_wic015(err)) == 1


@pytest.mark.fast
def test_generate_run_script_still_writes_run_sh_after_the_same_checks(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = machine('running')
    _cli(monkeypatch, tmp_path, _TOUCH)
    assert (tmp_path / 'run.sh').exists() and _pulled(calls)


@pytest.mark.fast
def test_graphviz_without_dot_is_a_note(
        machine: Callable[..., list[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    machine(missing=('dot',))
    _cli(monkeypatch, tmp_path, None, '--graphviz', mode='--generate_cwl_workflow')
    captured = capsys.readouterr()
    assert 'note [wic029] --graphviz needs the dot program' in captured.err
    assert 'Warning: Cannot generate graphviz' not in captured.out


# --------------------------------------------------------------------------
# The upfront image fetch is complete: podman pulls with podman, loaded and imported images are fetched,
# and a failure is one wic037
# --------------------------------------------------------------------------


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


@pytest.mark.fast
@pytest.mark.parametrize('engine_name', ['docker', 'podman'])
def test_images_are_pulled_with_the_chosen_engine(engine: Callable[..., list[list[str]]], tmp_path: Path,
                                                  engine_name: str) -> None:
    """Podman pulls with podman: the engine reaches cwl-docker-extract."""
    calls = engine()
    tool = _tool(tmp_path, {'dockerPull': 'docker.io/bash:4.4'})
    _prepare(tool, tmp_path, engine_name)
    assert ['cwl-docker-extract', '--force-download', '--container-engine', engine_name, str(tool)] in calls
    assert not [cmd for cmd in calls if cmd[:2] in ([engine_name, 'load'], [engine_name, 'import'])]


@pytest.mark.fast
@pytest.mark.parametrize('engine_name', ['docker', 'podman'])
def test_a_docker_load_image_is_loaded_before_the_run(engine: Callable[..., list[list[str]]], tmp_path: Path,
                                                      engine_name: str) -> None:
    """A dockerLoad file is loaded with the chosen engine."""
    calls = engine()
    archive = tmp_path / 'image.tar'
    archive.write_bytes(b'')
    _prepare(_tool(tmp_path, {'dockerLoad': str(archive)}), tmp_path, engine_name)
    assert [engine_name, 'load', '-i', str(archive)] in calls


@pytest.mark.fast
def test_a_docker_import_image_is_imported_under_its_image_id(engine: Callable[..., list[list[str]]],
                                                              tmp_path: Path) -> None:
    """A dockerImport is imported as its dockerImageId."""
    calls = engine()
    _prepare(_tool(tmp_path, {'dockerImport': 'https://example.org/rootfs.tar', 'dockerImageId': 'rootfs:1'}),
             tmp_path, 'podman')
    assert ['podman', 'import', 'https://example.org/rootfs.tar', 'rootfs:1'] in calls


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
def test_a_requirement_with_only_a_docker_pull_loads_and_imports_nothing(
        engine: Callable[..., list[list[str]]], tmp_path: Path) -> None:
    """A dockerPull needs the pull and nothing else."""
    calls = engine()
    _prepare(_tool(tmp_path, {'dockerPull': 'docker.io/bash:4.4', 'dockerImageId': 'bash:4.4'}), tmp_path)
    assert not [cmd for cmd in calls if len(cmd) > 1 and cmd[1] in ('load', 'import')]


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
