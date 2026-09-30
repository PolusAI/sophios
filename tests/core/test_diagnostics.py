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

See design_docs/core-refactor-design.md §3, deliberate exception 1.
"""
from pathlib import Path
from types import ModuleType

import pytest

from sophios import post_compile
from sophios.ir.complete import coerce_job_value
from sophios.ir.declarations import port_declaration
from sophios.lang.diagnostics import Diagnostic, Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.python_cwl_adapter import check_args_match_inputs


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
        coerce_job_value('n', port_declaration({'type': 'int'}), '_')

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
            'xs', port_declaration({'type': {'type': 'array', 'items': 'int'}}),
            [1, None])

    assert len(caught.value.diagnostics) == 1
    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    message = caught.value.diagnostics[0].message
    assert 'xs' in message
    assert 'int' in message
    assert 'None' in message


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


# --------------------------------------------------------------------------
# The CLI still speaks exit codes
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_cli_converts_a_report_to_exit_1(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """A reported failure leaves the CLI with the exact old behaviour: the
    messages on stdout, exit code 1, no traceback."""
    from sophios import main as cli

    def reports(*_args: object, **_kwargs: object) -> None:
        raise SophiosError.error(SophiosErrorCode.UNRESOLVED_INPUT,
                                 'Warning! Did you forget to use !ii before x in demo.wic?',
                                 'If you want to compile the workflow anyway, use --allow_raw_cwl')

    monkeypatch.setattr(cli, '_main', reports)

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    printed = capsys.readouterr().out
    assert 'Did you forget to use !ii' in printed
    assert '--allow_raw_cwl' in printed


@pytest.mark.fast
def test_cli_success_does_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean run returns instead of raising, exactly as before."""
    from sophios import main as cli
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
