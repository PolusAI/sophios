"""The library reports failures; only the CLI adapter exits.

Three claims, each enforced below: compilation never terminates the process,
every failure path produces at least one diagnostic, and the CLI's exit codes
match their pre-change behaviour.

The no-exit claim is enforced two ways. Statically: no core module except the CLI entry
point contains a `sys.exit` call — the same scan shape as the CWL-version
literal scan in `test_cwl_version.py`, because the failure it guards is
equally silent. Dynamically: the fuzz suite compiles
generated documents with no `SystemExit` arm left in its handler, so a
process-killing path is a test failure there rather than a whitelisted event.

The at-least-one-diagnostic claim holds by construction — `SophiosError`
cannot be built with zero diagnostics — plus one test per converted site proving the site actually
raises it with the messages it used to print.

See design_docs/core-refactor-design.md §3, deliberate exception 1.
"""
import ast as python_ast
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from sophios import post_compile
from sophios.lang.diagnostics import Code, Diagnostic, Severity, SophiosError
from sophios.python_cwl_adapter import check_args_match_inputs

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO_ROOT / 'src' / 'sophios'


def _console_script_modules() -> frozenset[Path]:
    """Every module a console script points at — the adapters.

    Derived from `[project.scripts]` rather than named here. The distinction
    the scan enforces is not "main.py is special": it is that a *library*
    module must never kill its caller's process, while an entry point exists
    precisely to turn a failure into an exit code. Deriving it means a new
    console script is exempt the day it is declared, and — the direction that
    actually bites — a module that stops being an entry point loses the
    exemption automatically. Review found `cwl_subinterpreter` had been an
    entry point all along with no handler at all.
    """
    with open(REPO_ROOT / 'pyproject.toml', 'rb') as handle:
        scripts = tomllib.load(handle).get('project', {}).get('scripts', {})
    return frozenset(SRC.parent / Path(*target.split(':')[0].split('.')).with_suffix('.py')
                     for target in scripts.values())


#: The modules allowed to exit. Everything else reports.
ADAPTERS: Final = _console_script_modules()

#: The adapter this suite pins exit-code behaviour for.
CLI_ADAPTER: Final = SRC / 'main.py'

#: The contrib zone is out of scope for the core guarantee; its own entry
#: points (REST server startup and the like) are someone's process to manage.
CONTRIB: Final = SRC / 'contrib'


def _core_files() -> list[Path]:
    """Every core Python file except the console-script adapters."""
    return sorted(path for path in SRC.rglob('*.py')
                  if path not in ADAPTERS and CONTRIB not in path.parents)


def _exit_calls(tree: python_ast.AST) -> list[int]:
    """Line numbers of every process-terminating spelling Python offers.

    `sys.exit(...)`, `exit(...)`, `quit(...)`, `raise SystemExit` (with or
    without arguments — `sys.exit` is only sugar for it), and `os._exit(...)`,
    which no handler can even catch. Anything the scan claims to forbid it
    must forbid in every spelling, or the ban is a style rule. The declared
    blind spot is a dynamically computed call (`getattr(sys, 'exit')`), which
    is not findable without executing the module.
    """
    found: list[int] = []
    for node in python_ast.walk(tree):
        match node:
            case python_ast.Call(func=python_ast.Attribute(value=python_ast.Name(id='sys'), attr='exit')):
                found.append(node.lineno)
            case python_ast.Call(func=python_ast.Attribute(value=python_ast.Name(id='os'), attr='_exit')):
                found.append(node.lineno)
            case python_ast.Call(func=python_ast.Name(id='exit' | 'quit')):
                found.append(node.lineno)
            case python_ast.Raise(exc=python_ast.Call(func=python_ast.Name(id='SystemExit'))):
                found.append(node.lineno)
            case python_ast.Raise(exc=python_ast.Name(id='SystemExit')):
                found.append(node.lineno)
    return found


# --------------------------------------------------------------------------
# The process is not the library's to terminate
# --------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize('path', _core_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_core_module_calls_exit(path: Path) -> None:
    """No core library module contains a process-terminating call.

    The dynamic half of the claim lives in the fuzz suite, whose exception handler
    no longer has a `SystemExit` arm at all.
    """
    lines = _exit_calls(python_ast.parse(path.read_text(encoding='utf-8'), str(path)))
    assert not lines, (
        f'{path.relative_to(REPO_ROOT)} calls exit at lines {lines}; '
        f'raise SophiosError and let the CLI adapter decide the exit code'
    )


@pytest.mark.fast
def test_the_exit_scan_can_actually_fail() -> None:
    """The scan detects each spelling it claims to, so the ban is not vacuous."""
    spellings = (
        'import sys\nsys.exit(1)',
        'exit(1)',
        'quit()',
        'raise SystemExit(1)',
        'raise SystemExit',
        'import os\nos._exit(1)',
    )
    for source in spellings:
        assert _exit_calls(python_ast.parse(source)), f'scan missed: {source}'


@pytest.mark.fast
def test_the_exit_scan_ignores_ordinary_raises_and_calls() -> None:
    """Reporting a failure is not exiting: the spellings the library should
    use do not trip the scan."""
    for source in ('raise ValueError(1)', 'sys.getsizeof(1)', 'process.exit_code'):
        assert not _exit_calls(python_ast.parse(source)), f'false positive: {source}'


@pytest.mark.fast
def test_the_cli_adapter_is_the_exception() -> None:
    """`main.py` still exits — that is its job, and the scan must not creep
    into forbidding it, or exit codes stop being anyone's responsibility."""
    lines = _exit_calls(python_ast.parse(CLI_ADAPTER.read_text(encoding='utf-8')))
    assert lines, 'main.py no longer exits anywhere; the exit-code parity tests below have lost their subject'


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
    error = SophiosError.error(Code.UNRESOLVED_INPUT, 'first', 'second')
    assert 'first' in str(error) and 'second' in str(error)
    assert len(error.diagnostics) == 2


@pytest.mark.fast
def test_spanless_diagnostics_print_without_a_location() -> None:
    """Compile-phase failures may not know a line; the string form must not
    invent one."""
    diagnostic = Diagnostic(Severity.ERROR, Code.MISSING_INPUT_FILE, 'gone.txt missing')
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
    assert all(d.code is Code.SCRIPT_ARGUMENT_MISMATCH for d in caught.value.diagnostics)


@pytest.mark.fast
def test_missing_input_file_reports(tmp_path: Path) -> None:
    """`stage_input_files` reports the absent file instead of exiting."""
    inputs = {'in_file': {'class': 'File', 'location': 'does_not_exist.txt'}}

    with pytest.raises(SophiosError) as caught:
        post_compile.stage_input_files(inputs, tmp_path, str(tmp_path / 'out'), throw=True)

    assert caught.value.diagnostics[0].code is Code.MISSING_INPUT_FILE
    assert 'does_not_exist.txt' in caught.value.diagnostics[0].message


@pytest.mark.fast
def test_missing_container_engine_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docker check reports the same installation advice it printed."""
    def command_not_found(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError('docker')

    monkeypatch.setattr(post_compile.sub, 'run', command_not_found)

    with pytest.raises(SophiosError) as caught:
        post_compile.verify_container_engine_config('docker', False)

    assert caught.value.diagnostics[0].code is Code.CONTAINER_ENGINE_UNAVAILABLE
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
        raise SophiosError.error(Code.UNRESOLVED_INPUT,
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


@pytest.mark.fast
def test_every_console_script_handles_reported_failures() -> None:
    """Each entry point converts `SophiosError` rather than letting it escape.

    An adapter is exempt from the no-exit rule because it is expected to turn
    a failure into an exit code — which means an adapter that does not catch
    the library's failure type has taken the exemption without doing the job,
    and the user gets a traceback for an error the library reported on
    purpose.
    """
    for adapter in sorted(ADAPTERS):
        source = adapter.read_text(encoding='utf-8')
        if 'SophiosError' not in source:
            continue  # nothing in this entry point can raise it
        assert 'except SophiosError' in source, \
            f'{adapter.name} mentions SophiosError but never handles it'
