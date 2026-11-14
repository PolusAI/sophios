"""The one run path: the runner is a child process whose output, failures and interruptions Sophios reports. Each run
starts a stand-in runner (`fake_runner`) through a public front door, so all between is the code users run."""
import _thread
import asyncio
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sophios import backends, preflight, run_local
from sophios.api.python.workflow import RunResult, RunState, StepStatus, Workflow, WorkflowRunError
from sophios.lang.diagnostics import Diagnostic, Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import Yaml

from .cases import cases
from .fake_runner import POSIX_ONLY, FakeRunner, install
from .helpers import ADAPTERS, HELLOWORLD, REPO_ROOT, echo_workflow, live_workflow, no_preflight, \
    python_process, run_cli
from .nextflow.test_public_api import CUDA_NOTES, gpu_workflow
from .test_backends import _Frozen

pytestmark = POSIX_ONLY


@pytest.fixture(name='offline')
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from `tmp_path`, with nothing pulled, no container engine asked, and nothing asked of Nextflow's own
    program before the stand-in starts."""
    monkeypatch.chdir(tmp_path)
    no_preflight(monkeypatch)
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])


@pytest.fixture(name='sophios')
def _sophios(monkeypatch: pytest.MonkeyPatch, offline: None) -> Callable[..., int]:
    """`sophios --yaml helloworld.wic <flags>`; returns its exit code."""
    del offline

    def run(*flags: str) -> int:
        return int(run_cli(monkeypatch, '--yaml', HELLOWORLD, *flags) or 0)
    return run


def _script(directory: Path) -> list[str]:
    """The command run.sh in `directory` starts."""
    return shlex.split((directory / 'run.sh').read_text(encoding='utf-8').splitlines()[-1])


@pytest.mark.fast
@pytest.mark.parametrize(('door', 'flags', 'given', 'withheld'), [
    ('command line', ['--passthrough_flags', 'yes', '--debug', '--validate'], ['--debug', '--validate'], ['--quiet']),
    ('command line', ['--quiet'], ['--quiet'], []),
    ('python', {'quiet': 'no', 'cachedir': 'cache', 'logLevel': 'INFO'}, ['--cachedir'], ['--quiet']),
], ids=['passthrough', 'quiet', 'python'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_a_run_starts_what_run_sh_holds_and_echoes_and_keeps_what_the_runner_prints(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sophios: Callable[..., int],
        capsys: pytest.CaptureFixture[str], door: str, flags: Any, given: list[str], withheld: list[str]) -> None:
    """A run starts what run.sh holds, from either door: the runner gets a flag it is passed, and `--quiet` only when
    the run is quiet, so `--debug` can be heard, and cwltool's `--validate` is not a prefix of `--validate_plugins`.
    The user's variables reach it exactly as given, through its environment and no shell, and `os.environ` is not
    touched. It starts in its own session from the working directory, so a terminal's Ctrl-C reaches Sophios and not
    the runner; both its streams go to this process's own, so a notebook and pytest show them, and to the run log, and
    the Python door prints what the command line prints."""
    fake = install(monkeypatch, tmp_path, stdout=['said on stdout'], stderr=['said on stderr'])
    monkeypatch.setattr(backends, 'datetime', _Frozen)
    secret = "p@ss$word! a&b 'single' \"double\" ;|<>()`x`\nline two"
    monkeypatch.delenv('SOPHIOS_TEST_TOKEN', raising=False)
    if door == 'command line':
        assert sophios(*flags, '--generate_run_script') == 0
        assert sophios(*flags, '--run_local') == 0
        name = 'helloworld'
    else:
        echo_workflow().run(run_args_dict={**flags, 'generate_run_script': 'yes'})
        echo_workflow().run(run_args_dict=flags, user_env_vars={'SOPHIOS_TEST_TOKEN': secret})
        name = 'wf'
        assert fake.record()['env']['SOPHIOS_TEST_TOKEN'] == secret
        assert 'SOPHIOS_TEST_TOKEN' not in os.environ
    script, started = _script(tmp_path), fake.record()
    # The stand-in is a script, so its own argv[0] is its path: the arguments are compared.
    assert script[0] == 'cwltool_filterlog' and started['argv'][1:] == script[1:]
    assert all(flag in script for flag in given) and not [flag for flag in withheld if flag in script]
    assert started['sid'] == started['pid'] and Path(started['cwd']) == tmp_path.resolve()
    captured = capsys.readouterr()
    assert 'said on stdout\n' in captured.out and 'said on stdout' not in captured.err
    assert 'said on stderr\n' in captured.err and 'said on stderr' not in captured.out
    assert f'Final output json metadata blob is in output_{name}.json' in captured.out
    assert 'Running cwltool_filterlog ' in captured.out and 'Success! Runner outputs are under ' in captured.out
    log = (tmp_path / 'autogenerated' / f'run_{name}.log').read_text(encoding='utf-8')
    assert 'said on stdout\n' in log and 'said on stderr\n' in log


@pytest.mark.fast
@pytest.mark.usefixtures('offline')
@pytest.mark.parametrize(('front_door', 'refusal', 'writes_nothing'), [
    (lambda workflow: workflow.run(user_env_vars={'OK_NAME': '1', 'MY-TOKEN': 'secret'}), "'MY-TOKEN'", False),
    (lambda workflow: workflow.run(run_args_dict={'cwl_runner': 'nope'}),
     "No backend runs cwl with 'nope'; the runners are cwltool", False),
    (lambda workflow: workflow.start(run_args_dict={'generate_run_script': 'yes'}), 'call run', True),
    (lambda workflow: workflow.run(target='nextflow', basepath='refused',
                                   run_args_dict={'cachedir': 'cache', 'resume': '', 'quiet': 'no'}),
     "run_args_dict keys only a CWL run reads: 'cachedir', 'quiet'$", True),
], ids=['variable-name', 'unknown-runner', 'run-script', 'cwl-only-for-nextflow'])
def test_a_refused_argument_is_refused_before_the_runner_starts(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, front_door: Callable[[Workflow], object], refusal: str,
        writes_nothing: bool) -> None:
    """A key that is not an environment variable name is an error naming it, not a variable silently dropped; a
    Python caller gets an exception naming the runners, not the command line's exit; only `run()` writes run.sh, so
    `start()` says so before it compiles, pulls or writes anything; and an option only a CWL run reads stops a Nextflow
    run before anything is written."""
    fake = install(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=refusal):
        front_door(echo_workflow())
    assert not fake.record_path.exists()
    assert not (writes_nothing and ((tmp_path / 'autogenerated').exists() or (tmp_path / 'refused').exists()))


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('runner-transcripts', 'stdout', 'stderr', 'exit', 'flags', 'failures',
                                       'summary'))
def test_a_run_says_what_its_runners_transcript_says(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                     sophios: Callable[..., int],
                                                     capsys: pytest.CaptureFixture[str], case: Yaml) -> None:
    """Each case of data/runner-transcripts: Sophios exits with the runner's code, echoes its stdout, prints the
    `failures` lines, and writes `summary` to output_helloworld.json, or no such file."""
    stdout = case.get('stdout', [])
    install(monkeypatch, tmp_path, stdout=stdout, stderr=case.get('stderr', []), exit_code=case['exit'])
    assert sophios('--run_local', *case.get('flags', [])) == case['exit']
    out, err = capsys.readouterr()
    assert [line for line in err.splitlines() if line.startswith('Failure!')] == case.get('failures', [])
    assert ''.join(f'{line}\n' for line in stdout) in out
    summary = tmp_path / 'autogenerated' / 'output_helloworld.json'
    assert (json.loads(summary.read_text(encoding='utf-8')) if summary.exists() else None) == case.get('summary')
    assert 'summary' not in case or 'Final output json metadata blob is in output_helloworld.json' in out


def _interrupt_once_started(fake: FakeRunner) -> threading.Thread:
    """Press Ctrl-C, as a terminal would, once the stand-in runner has started."""
    def press() -> None:
        fake.wait_until_started()
        _thread.interrupt_main()
    thread = threading.Thread(target=press, daemon=True)
    thread.start()
    return thread


@pytest.mark.fast
@pytest.mark.serial
@pytest.mark.parametrize(('runner', 'behaviour', 'flags', 'code'), [
    ('cwltool', {'exit_code': 3}, [], 3),
    ('cwltool', {'sleep': 60}, [], 130),
    ('toil-cwl-runner', {'sleep': 60}, [], 130),
    ('none', {}, [], 1),
    ('none', {}, ['--cachedir', ''], 1),
], ids=['exit-code', 'ctrl-c-cwltool', 'ctrl-c-toil', 'cannot-start', 'cannot-start-no-cachedir'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_the_command_line_exits_as_its_run_ended(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sophios: Callable[..., int],
        capsys: pytest.CaptureFixture[str], runner: str, behaviour: dict[str, Any], flags: list[str],
        code: int) -> None:
    """With the runner's exit code; on Ctrl-C, whichever runner ran, the runner is sent SIGTERM, on which cwltool stops
    the containers it started, and the exit code is 130. A runner that cannot start says why, whatever the cache, and
    its traceback goes to error_<name>.txt. Nothing in the working directory is deleted: a run used to `rmtree` every
    `<cachedir>*` beside it, and `--cachedir ''` matched the whole directory."""
    (tmp_path / 'cachedir_results').mkdir()
    (tmp_path / 'notes.txt').write_text('mine', encoding='utf-8')
    fake = install(monkeypatch, tmp_path, **behaviour)
    if runner == 'none':
        monkeypatch.setattr(preflight, 'runner_program', lambda _name: str(tmp_path / 'no_such_runner'))
    pressed = _interrupt_once_started(fake) if code == 130 else None
    try:
        assert sophios('--run_local', '--cwl_runner', 'cwltool' if runner == 'none' else runner, *flags) == code
    except KeyboardInterrupt:
        pytest.fail('the interrupt escaped the command line')
    out, err = capsys.readouterr()
    if pressed is not None:
        pressed.join()
        assert fake.record()['signal'] == 15
        assert "Interrupted; the runner's processes were terminated." in err
    if runner == 'none':
        assert 'no_such_runner' in out and 'See error_helloworld.txt for detailed technical information.' in out
        assert 'or the error the runner itself raised' in err
        kept = (tmp_path / 'error_helloworld.txt').read_text(encoding='utf-8')
        assert 'Traceback (most recent call last)' in kept and 'FileNotFoundError' in kept
    assert {'cachedir_results', 'notes.txt'} <= {path.name for path in tmp_path.iterdir()}


#: Sophios, in a process of its own, starting a run of the stand-in and then waiting on it (`wait`), waiting
#: from another thread (`thread`), holding it as a notebook does between cells (`hold`), or waiting after
#: forking a child that outlives it and so holds everything it inherited (`fork`, as `multiprocessing` does).
_SOPHIOS = """
import os, signal, sys, threading, time
from pathlib import Path
from sophios import preflight, run_local
from sophios.backends import RunRequest
program, basepath, mode, grace, nohup = sys.argv[1], Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]), sys.argv[5]
if nohup == 'nohup':
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # as `nohup` leaves it for the program it starts
preflight.runner_program = lambda _name: program
run_local.STOP_GRACE_SECONDS = grace
handle = run_local.start(RunRequest('wf', basepath))
if mode == 'fork':
    if (child := os.fork()) == 0:
        time.sleep(30)
        os._exit(0)
    (basepath.parent / 'forked.pid').write_text(str(child))
if mode in ('wait', 'fork'):
    handle.wait()
elif mode == 'thread':
    waiter = threading.Thread(target=handle.wait)
    waiter.start()
    waiter.join()
else:
    time.sleep(120)
"""


def _gone(pid: int, within: float) -> bool:
    """Whether process `pid` has ended within `within` seconds; it is killed if not."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    return False


@pytest.mark.fast
@pytest.mark.parametrize(('mode', 'signame', 'runner', 'ended', 'recorded'), [
    ('wait', 'SIGTERM', {}, {}, signal.SIGTERM), ('wait', 'SIGHUP', {}, {}, signal.SIGTERM),
    ('fork', 'SIGKILL', {}, {}, signal.SIGTERM), ('thread', 'SIGTERM', {}, {}, signal.SIGTERM),
    ('hold', 'SIGTERM', {}, {}, signal.SIGTERM), ('wait', 'SIGKILL', {'ignore_sigterm': True}, {'grace': 1.0}, None),
    ('wait', 'SIGHUP', {'sleep': 1}, {'nohup': True}, None),
], ids=['wait-SIGTERM', 'wait-SIGHUP', 'fork-SIGKILL', 'thread-SIGTERM', 'hold-SIGTERM', 'ignores-SIGTERM', 'nohup'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_the_runner_does_not_outlive_sophios(tmp_path: Path, mode: str, signame: str, runner: dict[str, Any],
                                             ended: dict[str, Any], recorded: int | None) -> None:
    """However Sophios ends, its runner is sent SIGTERM, on which cwltool stops its containers, or is killed if it
    ignores that; a child Sophios forked, which holds the watcher's pipe too, does not keep it going; and `nohup`
    keeps a run alive after logout: Sophios ignores the hangup, and the run finishes."""
    fake = FakeRunner(tmp_path, **{'sleep': 60, **runner})
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(REPO_ROOT / 'src'), os.environ.get('PYTHONPATH', '')])}
    argv = [sys.executable, '-c', _SOPHIOS, str(fake.write()), str(tmp_path / 'b'), mode,
            str(ended.get('grace', 30.0)), 'nohup' if ended.get('nohup') else '-']
    with subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        fake.wait_until_started()
        os.kill(process.pid, getattr(signal, signame))
        assert process.wait(30) == (0 if ended.get('nohup') else -getattr(signal, signame))
    try:
        assert _gone(int(fake.record()['pid']), within=10)
        assert fake.record().get('signal') == recorded
        if mode == 'fork':
            os.kill(int((tmp_path / 'forked.pid').read_text()), 0)   # still alive, still holding the pipe
    finally:
        if mode == 'fork':
            os.kill(int((tmp_path / 'forked.pid').read_text()), signal.SIGKILL)


_STARTS_AND_EXITS = '''
import time
from pathlib import Path
from sophios import preflight
preflight.prepare = lambda *_a, **_k: None
preflight.runner_program = lambda _name: {program!r}
from sophios.api.python.workflow import Step, Workflow
echo = Step(clt_path=Path({echo!r}))
echo.inputs.message = 'hello'
Workflow([echo], 'wf').start(run_args_dict={{'cwl_runner': 'toil-cwl-runner'}})
while not Path({record!r}).exists():
    time.sleep(0.02)
'''

#: Appended to `_STARTS_AND_EXITS`: the script exits only once the runner has.
_AFTER_THE_RUNNER_EXITS = '''
import json, subprocess
pid = json.loads(Path({record!r}).read_text(encoding='utf-8'))['pid']
def state():
    return subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
while state()[:1] not in ('Z', ''):
    time.sleep(0.02)
'''


@pytest.mark.fast
@pytest.mark.parametrize('runner_exits_first', [False, True], ids=['runner-running', 'runner-exited'])
def test_a_run_nobody_waited_for_ends_with_the_python_process(tmp_path: Path, runner_exits_first: bool) -> None:
    """A script that starts a run and exits without waiting leaves no runner behind; a runner that exited before the
    script did is settled at exit: its outputs are written, its end printed."""
    outputs = {'out': 3}
    fake = FakeRunner(tmp_path, **({'stdout': json.dumps(outputs).splitlines()} if runner_exits_first
                                   else {'sleep': 60}))
    script = (_STARTS_AND_EXITS + _AFTER_THE_RUNNER_EXITS * runner_exits_first).format(
        program=str(fake.write()), echo=str(ADAPTERS / 'echo.cwl'), record=str(fake.record_path))
    try:
        exited = python_process('-c', script, cwd=tmp_path, capture_output=True, timeout=120)
        assert exited.returncode == 0, exited.stderr
        assert ('Python is exiting before the run ended' in exited.stderr) is not runner_exits_first
        if runner_exits_first:
            assert 'Success! Runner outputs are under' in exited.stdout
            written = tmp_path / 'autogenerated' / 'output_wf.json'
            assert json.loads(written.read_text(encoding='utf-8')) == outputs
        else:
            assert fake.record().get('signal') == 15
    finally:   # a runner the script left behind stops here, not with the test session
        if fake.record_path.exists() and not runner_exits_first:
            try:
                os.kill(fake.record()['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass


#: cwltool's lines for a failed job, coloured as its log colours them.
_EXITED_127 = ['\x1b[1;30mWARNING\x1b[0m \x1b[33m[job echo_2] exited with status: 127\x1b[0m',
               '\x1b[1;30mWARNING\x1b[0m \x1b[33m[job echo_2] completed permanentFail\x1b[0m']


def _wait_until_exited(pid: int) -> None:
    """Block until the process `pid` has exited, leaving it for its parent to reap."""
    deadline = time.monotonic() + 30
    while subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True,
                         check=False).stdout.strip()[:1] not in ('Z', ''):
        assert time.monotonic() < deadline, f'process {pid} never exited'
        time.sleep(0.02)


@pytest.mark.fast
@pytest.mark.usefixtures('offline')
@pytest.mark.parametrize('ending', ['completes', 'fails', 'exits-before-cancel', 'leaves-a-process-behind',
                                    'no-watcher'])
# pylint: disable-next=too-many-locals
def test_a_started_run_is_followed_by_its_handle_to_how_it_ended(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], ending: str) -> None:
    """The handle is RUNNING as soon as `start` returns, and `wait` and `collect` time out while it is; `wait` raises
    nothing for a run that failed, and ends with the run's status, the same one each time after; `cancel` on an ended
    run cancels nothing. `status` answers at once, even while a process the runner left behind holds its streams
    open, which keeps the run RUNNING. A runner that exited 0 before `cancel` is a COMPLETED run, with its outputs
    written. Without the watcher that stops the runner should Sophios end, the run does not go on."""
    outputs = {'out': 1}
    printed = json.dumps(outputs).splitlines()
    behaviour: dict[str, dict[str, Any]] = {
        'completes': {'stdout': printed, 'stderr': ['toil said this'], 'sleep': 1.0},
        'fails': {'stderr': _EXITED_127, 'exit_code': 1}, 'exits-before-cancel': {'stdout': printed},
        'leaves-a-process-behind': {'leave_behind': 10}, 'no-watcher': {'sleep': 60}}
    fake = install(monkeypatch, tmp_path, **behaviour[ending])
    if ending == 'no-watcher':
        monkeypatch.setattr(run_local.sys, 'executable', '')   # as in some embedded interpreters
    toil = ending in ('completes', 'exits-before-cancel')   # Toil prints its output object
    handle = echo_workflow().start(run_args_dict={'cwl_runner': 'toil-cwl-runner' if toil else 'cwltool'})
    if ending == 'completes':
        assert handle.status().state is RunState.RUNNING and handle.outputs() is None
        for waits in (handle.wait, handle.collect):
            with pytest.raises(TimeoutError):
                waits(timeout=0.05)
    if ending == 'leaves-a-process-behind':   # the runner has exited, but what it started holds its streams open
        fake.wait_until_started()
        until = time.monotonic() + 1.5
        while time.monotonic() < until:
            asked = time.monotonic()
            assert handle.status().state is RunState.RUNNING
            assert time.monotonic() - asked < 0.5
            time.sleep(0.1)
        handle.cancel()   # ends what it left behind
    if ending == 'exits-before-cancel':
        fake.wait_until_started()
        _wait_until_exited(fake.record()['pid'])
        assert not handle.cancel()
    status = handle.wait()
    assert handle.wait() is status is handle.status() and not handle.cancel()
    if ending == 'fails':
        assert (status.state, status.outputs, status.exit_code) == (RunState.FAILED, None, 1)
        assert status.steps == (StepStatus('echo_2', RunState.FAILED, exit_status=127),)
    elif ending == 'leaves-a-process-behind':   # its runner exited 0 before
        assert (status.state, status.outputs, status.exit_code) == (RunState.COMPLETED, None, 0)
    elif ending == 'no-watcher':
        assert status.state is RunState.FAILED
        assert 'the watcher that stops it if Sophios ends could not start' in (status.reason or '')
    else:
        assert status.state is RunState.COMPLETED and status.outputs == outputs == handle.outputs()
        assert status.started_at is not None and status.ended_at is not None and status.started_at <= status.ended_at
        result = handle.collect()
        log = tmp_path / 'autogenerated' / 'run_wf.log'
        assert result == RunResult(RunState.COMPLETED, outputs, result.outdir, log, 0)
        assert result.outdir.parent == tmp_path / 'autogenerated' and result.outdir.name.startswith('outdir_toil_wf_')
        assert handle.logs() == log.read_text(encoding='utf-8')
        assert json.loads((tmp_path / 'autogenerated' / 'output_wf.json').read_text(encoding='utf-8')) == outputs
    assert ending != 'completes' or 'toil said this' in handle.logs()
    assert 'Interrupted' not in capsys.readouterr().err


@pytest.mark.fast
@pytest.mark.serial
@pytest.mark.usefixtures('offline')
@pytest.mark.parametrize('how', ['here', 'from-another-thread', 'ignoring-sigterm'])
def test_a_cancelled_run_stays_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str], how: str) -> None:
    """`cancel` stops the runner with SIGTERM, from any thread, and kills it and its session after the grace when
    SIGTERM is not enough. A second `cancel`, a later `wait` and `collect` change nothing, `wait` reports the
    cancellation and not the exit status the runner stopped with, and `collect` raises the run as cancelled, not as
    failed."""
    fake = install(monkeypatch, tmp_path, sleep=60, ignore_sigterm=how == 'ignoring-sigterm')
    monkeypatch.setattr(run_local, 'STOP_GRACE_SECONDS', 0.5)
    handle = echo_workflow().start()
    fake.wait_until_started()
    if how == 'from-another-thread':
        canceller = threading.Thread(target=handle.cancel)
        canceller.start()
        assert handle.wait().state is RunState.CANCELLED
        canceller.join()
    else:
        assert handle.cancel()
    assert handle.cancel()
    status = handle.status()
    assert handle.wait() is status and status.state is RunState.CANCELLED
    assert status.exit_code == -9 if how == 'ignoring-sigterm' else fake.record()['signal'] == 15
    with pytest.raises(WorkflowRunError, match='did not complete: cancelled: the runner was stopped') as caught:
        handle.collect()
    assert 'fail' not in str(caught.value) and handle.outputs() is None
    assert 'Failure!' not in capsys.readouterr().err


@pytest.mark.fast
@pytest.mark.usefixtures('offline')
@pytest.mark.parametrize('door', ['run', 'start-collect', 'run_async'])
def test_each_python_door_returns_a_completed_runs_result_and_raises_a_failed_ones(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], door: str) -> None:
    """`run()` returns what `start().collect()` returns, and so does `run_async`, which leaves the event loop free
    while the runner runs: a completed run's outputs, its output directory and its log. A failed run raises with the
    runner's exit code, so it cannot be missed. Cancelling the `run_async` task stops the runner: it is sent SIGTERM
    and has exited before the task's cancellation is seen."""
    outputs = {'out': 'x'}
    fake = install(monkeypatch, tmp_path, stdout=json.dumps(outputs).splitlines(), sleep=1.0)
    run_args = {'cwl_runner': 'toil-cwl-runner', 'outdir': str(tmp_path / 'outdir')}
    ticks = 0

    def through(workflow: Workflow) -> RunResult | None:
        if door == 'run':
            return workflow.run(run_args_dict=run_args)
        if door == 'start-collect':
            return workflow.start(run_args_dict=run_args).collect()

        async def main() -> RunResult | None:
            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += fake.record_path.exists()
                    await asyncio.sleep(0.01)
            ticker = asyncio.create_task(tick())
            result = await workflow.run_async(run_args_dict=run_args)
            ticker.cancel()
            return result
        return asyncio.run(main())

    result = through(echo_workflow())
    assert result is not None and (result.state, result.outputs, result.exit_code) == (RunState.COMPLETED, outputs, 0)
    assert result.log_file.is_file() and result.outdir == tmp_path / 'outdir'
    assert door != 'run_async' or ticks >= 10
    (tmp_path / 'failing').mkdir()
    install(monkeypatch, tmp_path / 'failing', exit_code=7)
    with pytest.raises(WorkflowRunError) as raised:
        through(echo_workflow('failing'))
    assert raised.value.exit_code == 7
    assert str(raised.value) == ("error [api005] workflow 'failing' did not complete: the runner exited with status 7"
                                 '\nerror [api005] the lines printed above this error say why')
    if door != 'run_async':
        return
    (tmp_path / 'cancelled').mkdir()
    stopping = install(monkeypatch, tmp_path / 'cancelled', sleep=60)

    async def cancelled() -> None:
        task = asyncio.create_task(echo_workflow().run_async())
        while not stopping.record_path.exists():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    stopped = stopping.record()
    assert stopped['signal'] == 15
    with pytest.raises(ProcessLookupError):
        os.kill(stopped['pid'], 0)
    assert "Interrupted; the runner's processes were terminated." in capsys.readouterr().err


@pytest.mark.fast
@pytest.mark.needs_cwltool
def test_the_runner_prints_no_rdflib_encoding_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwltool writes provenance as N-Triples naming no encoding; rdflib's warning about it is noise."""
    import cwltool.main  # pylint: disable=import-outside-toplevel
    import rdflib  # pylint: disable=import-outside-toplevel

    def provenance_run(argv: list[str]) -> int:
        del argv
        rdflib.Graph().serialize(destination=io.BytesIO(), format='nt', encoding=None)
        return 0

    monkeypatch.setattr(cwltool.main, 'run', provenance_run)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        assert run_local.cwltool_main() == 0
    assert not [w for w in caught if 'NTSerializer' in str(w.message)]


#: The record a stand-in Nextflow prints for the echo workflow's one output, and what the run reports for it.
_ECHOED = '/w/ab/stdout'
_RECORDED = 'sophios-output ' + json.dumps(['echo_stdout', {'class': 'File', 'path': _ECHOED}])
_ECHO_OUTPUTS = {'echo~stdout': {'class': 'File', 'location': 'file:///w/ab/stdout', 'path': _ECHOED,
                                 'basename': 'stdout'}}


def _nextflow_argv(base: Path, *passthrough: str, name: str = 'wf') -> list[str]:
    """The arguments Nextflow is started with for the workflow `name` in `base`, after its program."""
    run = base.resolve() / f'nextflow_{name}'
    return ['-C', f'{run}/nextflow.config', 'run', f'{run}/workflow.nf', '-params-file',
            f'{run}/nextflow_params.json', '-work-dir', f'{run}/work', '-ansi-log', 'false', *passthrough]


@pytest.mark.fast
@pytest.mark.filterwarnings('error::UserWarning')
@pytest.mark.parametrize('door', ['run', 'start', 'run_async', 'command line'])
# pylint: disable-next=too-many-locals
def test_each_door_runs_the_nextflow_target_and_reports_its_recorded_outputs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sophios: Callable[..., int],
        capsys: pytest.CaptureFixture[str], door: str) -> None:
    """Nextflow starts from the workflow's own directory on the pipeline Sophios wrote there, with the passthrough
    arguments, and the outputs it recorded come back under their Sophios names, also in `output_<name>.json`; the
    command line's run is what its run script holds. Each note the lowering made is a line on stderr, as the command
    line prints it, and never a warning, and the result of the run carries the same notes."""
    fake = install(monkeypatch, tmp_path, stdout=['[PROCESS ab/cdef12] wf:echo', _RECORDED])
    base = (tmp_path / 'autogenerated').resolve()
    if door == 'command line':
        flags = ('--target', 'nextflow', '--passthrough_flags', 'yes', '-resume')
        assert sophios(*flags, '--generate_run_script') == 0
        assert sophios(*flags, '--run_local') == 0
        argv = _nextflow_argv(base, '-resume', name='helloworld')
        assert _script(tmp_path) == ['nextflow', *argv] and fake.record()['argv'][1:] == argv
        assert json.loads((base / 'output_helloworld.json').read_text(encoding='utf-8')) == _ECHO_OUTPUTS
        return
    through: dict[str, Callable[[Workflow], RunResult | None]] = {
        'run': lambda workflow: workflow.run(target='nextflow'),
        'start': lambda workflow: workflow.start(target='nextflow').collect(),
        'run_async': lambda workflow: asyncio.run(workflow.run_async(target='nextflow'))}
    result = through[door](echo_workflow())
    assert result is not None and (result.state, result.outputs) == (RunState.COMPLETED, _ECHO_OUTPUTS)
    assert result.outdir == base / 'nextflow_wf' / 'work'
    assert json.loads((base / 'output_wf.json').read_text(encoding='utf-8')) == _ECHO_OUTPUTS
    assert fake.record()['argv'][1:] == _nextflow_argv(base) and Path(fake.record()['cwd']) == base / 'nextflow_wf'
    capsys.readouterr()
    (tmp_path / 'gpu').mkdir()
    install(monkeypatch, tmp_path / 'gpu')
    noted = through[door](gpu_workflow())
    assert noted is not None
    printed = [line for line in capsys.readouterr().err.splitlines() if '[wic046]' in line]
    assert [(str(note.severity), str(note.code), note.locator and note.locator.step) for note in noted.diagnostics
            if note.code is SophiosErrorCode.UNCHECKED_CUDA_FIELD] == CUDA_NOTES
    assert [str(note) for note in noted.diagnostics if note.code is SophiosErrorCode.UNCHECKED_CUDA_FIELD] == printed
    if door != 'run':
        return
    monkeypatch.setenv('SOPHIOS_TEST_KEPT', 'kept')
    monkeypatch.setenv('NXF_SYNTAX_PARSER', 'v1')
    (tmp_path / 'options').mkdir()
    fake = install(monkeypatch, tmp_path / 'options', stdout=[_RECORDED])
    workflow = echo_workflow()
    workflow.outputs.echo_stdout = workflow.steps[0].outputs.stdout
    declared = workflow.run(target='nextflow', run_args_dict={'process.executor': 'slurm', 'resume': ''},
                            user_env_vars={'SOPHIOS_TEST_TOKEN': 'x'})
    assert declared is not None and declared.outputs == {'echo_stdout': _ECHO_OUTPUTS['echo~stdout']}
    assert fake.record()['argv'][1:] == _nextflow_argv(tmp_path / 'autogenerated', '-process.executor', 'slurm',
                                                       '-resume')
    env = fake.record()['env']
    assert (env['SOPHIOS_TEST_TOKEN'], env['SOPHIOS_TEST_KEPT'], env['NXF_SYNTAX_PARSER']) == ('x', 'kept', 'v2')
    (tmp_path / 'live').mkdir()
    install(monkeypatch, tmp_path / 'live')
    live_workflow('live').run(target='nextflow')
    assert 'Real-time analysis needs cwltool; nextflow runs the workflow without it.' in capsys.readouterr().err
    missing = Diagnostic(Severity.ERROR, SophiosErrorCode.PROGRAM_MISSING, 'nextflow is not here.')
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [missing])
    with pytest.raises(SophiosError) as raised:
        echo_workflow().run(target='nextflow')
    assert list(raised.value.diagnostics) == [missing]
