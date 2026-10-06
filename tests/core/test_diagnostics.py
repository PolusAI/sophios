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
import math
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from sophios import main as cli
from sophios import post_compile
from sophios.ir.complete import coerce_job_value
from sophios.ir.declarations import port_declaration
from sophios.ir.types import AuthoredName
from sophios.lang import InlineLiteral, parse
from sophios.lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from sophios.lang.error_codes import EXPLANATIONS, SophiosErrorCode
from sophios.lang.spans import SourceSpan
from sophios.python_cwl_adapter import check_args_match_inputs
from sophios.wic_types import StepId, Tool

from .hermetic import compile_hermetic
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
def test_missing_input_file_reports(tmp_path: Path) -> None:
    """`stage_input_files` reports the absent file instead of exiting."""
    inputs = {'in_file': {'class': 'File', 'location': 'does_not_exist.txt'}}

    with pytest.raises(SophiosError) as caught:
        post_compile.stage_input_files(inputs, tmp_path, str(tmp_path / 'out'), throw=True)

    assert caught.value.diagnostics[0].code is SophiosErrorCode.MISSING_INPUT_FILE
    assert 'does_not_exist.txt' in caught.value.diagnostics[0].message


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


@pytest.mark.fast
def test_missing_container_engine_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docker check reports the same installation advice it printed."""
    def command_not_found(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError('docker')

    monkeypatch.setattr(post_compile.sub, 'run', command_not_found)

    with pytest.raises(SophiosError) as caught:
        post_compile.verify_container_engine_config('docker', False)

    assert caught.value.diagnostics[0].code is SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE
    assert any('--ignore_docker_install' in d.message for d in caught.value.diagnostics)


@pytest.mark.fast
def test_ignored_container_check_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch still works: --ignore_docker_install means no report."""
    def command_not_found(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError('docker')

    monkeypatch.setattr(post_compile.sub, 'run', command_not_found)
    post_compile.verify_container_engine_config('docker', True)  # must not raise


def _docker_with_processes(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """A working docker engine that reports `count` running docker processes."""
    def probe(cmd: str | list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if isinstance(cmd, list):
            return subprocess.CompletedProcess(cmd, 0, stdout=b'Hello from Docker!')
        return subprocess.CompletedProcess(cmd, 0, stdout=f'{count}\n'.encode())

    monkeypatch.setattr(post_compile.sub, 'run', probe)
    monkeypatch.setattr(post_compile.sys, 'platform', 'linux')


@pytest.mark.fast
def test_too_many_docker_processes_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-count check fires unless --ignore_docker_processes is given."""
    _docker_with_processes(monkeypatch, 1001)

    with pytest.raises(SophiosError) as caught:
        post_compile.verify_container_engine_config('docker', False, ignore_container_processes=False)

    assert caught.value.diagnostics[0].code is SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE
    assert any('--ignore_docker_processes' in d.message for d in caught.value.diagnostics)


@pytest.mark.fast
def test_ignored_docker_process_check_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ignore_docker_processes alone silences the process-count check, and
    --ignore_docker_install does not."""
    _docker_with_processes(monkeypatch, 1001)
    post_compile.verify_container_engine_config('docker', False, ignore_container_processes=True)  # must not raise

    with pytest.raises(SophiosError):
        post_compile.verify_container_engine_config('docker', True, ignore_container_processes=False)


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
def test_cli_points_a_compiler_crash_at_its_error_file_on_stderr(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure that is not a reported diagnostic keeps its traceback in `error_<stem>.txt` and says so on stderr."""
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
    assert f'Failed to compile {workflow}' in captured.err
    assert 'See error_crash.txt for detailed technical information.' in captured.err
    assert 'Failed to compile' not in captured.out
    assert 'boom' in (tmp_path / 'error_crash.txt').read_text(encoding='utf-8')


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
    monkeypatch.setattr(cli, '_main', lambda: None)
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
    import sophios.post_compile as pc
    monkeypatch.setattr(pc, 'verify_container_engine_config', lambda *_a, **_k: None)
    monkeypatch.setattr(pc, 'cwl_docker_extract', lambda *_a, **_k: None)
    monkeypatch.setattr(pc, 'stage_input_files', lambda *_a, **_k: None)
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


@pytest.mark.fast
def test_a_container_engine_error_does_not_call_itself_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error report states the problem; `Warning!` is for the stderr lines that do not stop the compile."""
    _docker_with_processes(monkeypatch, 1001)
    with pytest.raises(SophiosError) as caught:
        post_compile.verify_container_engine_config('docker', False, ignore_container_processes=False)
    assert caught.value.diagnostics[0].message == 'There are 1001 running docker processes.'

    def command_not_found(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError('docker')

    monkeypatch.setattr(post_compile.sub, 'run', command_not_found)
    with pytest.raises(SophiosError) as caught:
        post_compile.verify_container_engine_config('docker', False)
    assert caught.value.diagnostics[0].message == 'The docker command does not appear to be installed.'


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
