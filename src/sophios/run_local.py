"""Running a workflow on this machine: the one path every front door takes.

A front door describes the run as a `backends.RunRequest`; `run` starts the runner its
backend launches as a child process, streams what it prints, and reports how it ended.
`start` returns the `RunHandle` that `run` waits on, which `Workflow.start()` hands to its caller.
"""
import atexit
import os
import shlex
import signal
import stat
import subprocess as sub
import sys
import threading
import time
import traceback
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import IO, Final

from sophios.wic_types import Json
from . import auto_gen_header, backends, preflight, realtime
from .backends import Backend, Launch, RunRequest
from .run_status import RunResult, RunState, RunStatus, StepStatus, WorkflowRunError
from .plugins import logging_filters


def generate_run_script(cmdline: str) -> None:
    """Writes the command used to invoke the cwl-runner to run.sh
    Does not actually invoke ./run.sh

    Args:
        cmdline (str): The command line which invokes the cwl-runner
    """
    with open('run.sh', mode='w', encoding='utf-8') as f:
        f.write('#!/bin/bash -e\n')
        f.write(auto_gen_header)
        f.write(cmdline)
    # chmod +x run.sh
    st = os.stat('run.sh')
    os.chmod('run.sh', st.st_mode | stat.S_IEXEC)


#: What an exit status means on its own (shell conventions), said after it.
_STATUS_MEANS: Final = {
    126: 'its command could not be executed (not executable, or built for another platform)',
    127: "its command was not found: the tool's baseCommand is not installed in its container image, "
         'or on this machine for a tool with none',
    137: 'it was killed, usually for running out of memory: give the container engine more memory '
         '(Docker Desktop: Settings, Resources), or the step less input',
}


def _failure_lines(failed: Mapping[str, int | str | None]) -> list[str]:
    """One line per failed step: who, how it ended, what that means, where its messages are."""
    if not failed:
        return ['Failure! Above, the first ERROR line, or the error the runner itself raised, says why the run failed.']
    lines = []
    for job, ended in failed.items():
        means: str | None
        if isinstance(ended, int):
            how, means = f'exited with status {ended}', _STATUS_MEANS.get(ended)
        elif ended is not None:
            how, means = f'was ended by {ended}', _STATUS_MEANS[137] if ended == 'SIGKILL' else None
        else:
            how, means = 'could not run, or its outputs could not be collected', None
        lines.append(f"Failure! step '{job}' {how}" + (f', which means {means}' if means else '')
                     + '. Its own messages are above, under its name.')
    return lines


def log_path(request: RunRequest) -> Path:
    """The file a run's two streams are written to, as they come: `<basepath>/run_<workflow>.log`."""
    return request.basepath / f'run_{request.workflow_name}.log'


#: How long a runner asked to stop has to stop the containers it started before it is killed.
STOP_GRACE_SECONDS: Final = 30.0


def _spawn(launch: Launch) -> sub.Popen[bytes]:
    """Start `launch` with its streams piped, in its own session, where a terminal's Ctrl-C does not reach it:
    Sophios stops it.

    The program is this Python environment's own when it has one (`preflight.runner_program`). POSIX
    only: no runner runs on Windows, and the pre-flight stops a run there first (`PLATFORM_LACKS_RUNNERS`).
    """
    if sys.platform == 'win32':
        raise AssertionError('the pre-flight stops a run on Windows')
    executable = preflight.runner_program(launch.argv[0])
    return sub.Popen(launch.argv, executable=executable, env=dict(launch.env), cwd=launch.cwd,
                     stdin=sub.DEVNULL, stdout=sub.PIPE, stderr=sub.PIPE, start_new_session=True)


#: Runs beside every runner, in a session of its own, reading a pipe only Sophios writes to. When
#: Sophios ends, however it ends (a signal, `kill -9`, a crash), the pipe closes with nothing written,
#: or, if something Sophios forked still holds the pipe, the watcher's parent is no longer Sophios. Then
#: the watcher stops the runner's session: SIGTERM, then SIGKILL if it has not stopped within the grace.
#: A byte written first means the run is over, and the watcher leaves without touching anything.
_WATCHER: Final = """
import os, select, signal, sys, time
group, grace, sophios = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
while os.getppid() == sophios:
    if select.select([0], [], [], 0.5)[0]:
        if os.read(0, 1):
            sys.exit(0)
        break
try:
    os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        time.sleep(0.1)
        os.killpg(group, 0)
    os.killpg(group, signal.SIGKILL)
except (ProcessLookupError, PermissionError):
    pass
"""


def _watch(runner: sub.Popen[bytes]) -> sub.Popen[bytes]:
    """Start the watcher that stops `runner`'s session if this process ends before the run does."""
    return sub.Popen([sys.executable, '-I', '-c', _WATCHER, str(runner.pid), str(STOP_GRACE_SECONDS), str(os.getpid())],
                     stdin=sub.PIPE, stdout=sub.DEVNULL, stderr=sub.DEVNULL, start_new_session=True)


@dataclass
class _Streams:
    """What the runner printed, as the run goes: echoed, written to the log, and its stdout kept."""

    log: IO[str]
    stdout: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    error: BaseException | None = None

    def pump(self, stream: IO[bytes], name: str) -> None:
        """Copy `stream` line by line to `sys.<name>` and to the log, until the runner closes it."""
        try:
            for raw in iter(stream.readline, b''):
                line = raw.decode('utf-8', errors='replace')
                echo = getattr(sys, name)
                echo.write(line)
                echo.flush()
                with self.lock:
                    self.log.write(line)
                    self.log.flush()
                    if name == 'stdout':
                        self.stdout.append(line)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.error = error
            for _ in iter(stream.readline, b''):   # keep draining, so the runner never blocks on a full pipe
                pass
        finally:
            stream.close()


class RunHandle:  # pylint: disable=too-many-instance-attributes
    """A run whose runner is a child of this process, for as long as this process lives.

    Made started, by `start` (`Workflow.start()`), never by calling the class. There is
    no detaching from it: when the interpreter exits with the run still going, the run
    is cancelled. `wait` blocks until it ends and returns its status, `collect` its result,
    raising for a run that did not complete; `status`, `logs` and `outputs` say where it
    is without blocking; `cancel` stops it, as Ctrl-C during `wait` does. Every method
    may be called from any thread.
    """

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(self, request: RunRequest, backend: Backend, launch: Launch,
                 plans: tuple[realtime.Plan, ...], analysis: realtime.Command | None) -> None:
        """Start the run; ``Workflow.start()`` does this, so a script never calls the class itself."""
        self._request = request
        self._backend = backend
        self._launch = launch
        self._plans = plans
        self._analysis = analysis
        self._log = log_path(request).absolute()
        self._status = RunStatus(RunState.QUEUED)
        self._process: sub.Popen[bytes] | None = None
        self._watcher: sub.Popen[bytes] | None = None
        self._streams: _Streams | None = None
        self._readers: list[threading.Thread] = []
        self._watching: AbstractContextManager[None] | None = None
        self._settle_lock = threading.Lock()
        self._start()

    def _start(self) -> None:
        """Start the runner, and the real-time analyses beside it.

        A runner that cannot be started is a FAILED run, its traceback in `error_<workflow>.txt`.
        """
        print('Running ' + shlex.join(self._launch.argv), flush=True)
        if self._analysis is not None:
            self._watching = realtime.watching(self._plans, Path(self._request.cachedir), self._analysis,
                                               env=self._launch.env)
            self._watching.__enter__()  # pylint: disable=unnecessary-dunder-call
        started_at = datetime.now()
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            self._streams = _Streams(self._log.open('w', encoding='utf-8'))
            self._process = _spawn(self._launch)
            self._watcher = _watch(self._process)
        except Exception as error:  # pylint: disable=broad-exception-caught
            reason = f'the runner could not be started: {error}'
            if self._process is not None:   # started, with nothing to stop it should this process end
                self._kill(self._process)
                self._process.wait()
                self._process = None
                reason = f'the runner was stopped: the watcher that stops it if Sophios ends could not start: {error}'
            self._unsupervised(error)
            if self._streams is not None:
                self._streams.log.close()
            self._stop_watching(cancelled=True)
            for line in _failure_lines({}):
                print(line, file=sys.stderr)
            self._status = RunStatus(RunState.FAILED, reason, started_at, datetime.now(), exit_code=1)
            return
        self._status = RunStatus(RunState.RUNNING, started_at=started_at)
        atexit.register(self._end_with_interpreter)
        try:
            assert self._process.stdout is not None and self._process.stderr is not None
            self._readers = [threading.Thread(target=self._streams.pump, args=(pipe, name), daemon=True)
                             for pipe, name in ((self._process.stdout, 'stdout'), (self._process.stderr, 'stderr'))]
            for reader in self._readers:
                reader.start()
        except KeyboardInterrupt:
            self.cancel()
            raise

    def _end_with_interpreter(self) -> None:
        """As the interpreter exits, cancel the run if its runner is still going, so nothing outlives it;
        report on a runner that has exited, so its analyses end and its log is closed."""
        if self._process is not None and self._process.poll() is None:
            print('Python is exiting before the run ended, so it is cancelled.', file=sys.stderr)
        self.cancel()

    def _unsupervised(self, error: BaseException) -> None:
        """Say that Sophios could not start or follow the runner; keep the traceback in `error_<workflow>.txt`."""
        name = self._request.workflow_name
        print('Failed to execute', self._request.basepath / name)
        print(error)
        print(f'See error_{name}.txt for detailed technical information.')
        # Do not display a nasty stack trace to the user; hide it in a file.
        with open(f'error_{name}.txt', mode='w', encoding='utf-8') as f:
            traceback.print_exception(error, file=f)

    def _stop_watching(self, *, cancelled: bool) -> None:
        """End the real-time analyses: with their final runs, or at once when the run was cancelled."""
        watching, self._watching = self._watching, None
        if watching is None:
            return
        if cancelled:
            interrupt = KeyboardInterrupt()
            watching.__exit__(KeyboardInterrupt, interrupt, None)
        else:
            watching.__exit__(None, None, None)

    def _join_readers(self, deadline: float | None = None) -> None:
        """Wait until the runner's streams are read to their end.

        Raises:
            TimeoutError: `deadline` has passed with the runner exited and its output still being read.
        """
        try:
            for reader in self._readers:
                while reader.is_alive():
                    reader.join(_slice(deadline))
        except TimeoutError:
            raise TimeoutError('the runner has exited and its output is still being read; wait again') from None

    def wait(self, timeout: float | None = None) -> RunStatus:
        """Block until the run ends, and return its status; once it has, the same status at once.

        A run that FAILED or was CANCELLED is returned as one: nothing is raised for it.

        Args:
            timeout: How many seconds to wait at most; None waits as long as the run takes.

        Returns:
            RunStatus: How the run ended: COMPLETED, FAILED or CANCELLED.

        Raises:
            TimeoutError: The run had not ended within `timeout` seconds. It goes on.
            KeyboardInterrupt: On Ctrl-C, after the runner was stopped (`cancel`).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            if self._process is not None:
                while self._process.poll() is None:
                    try:
                        self._process.wait(_slice(deadline))
                    except sub.TimeoutExpired:
                        pass
                self._join_readers(deadline)
        except KeyboardInterrupt:
            self.cancel()
            raise
        return self._settle()

    def collect(self, timeout: float | None = None) -> RunResult:
        """`wait`, then the run's result: a run that did not complete is raised, not returned.

        Args:
            timeout: As `wait` takes it.

        Returns:
            RunResult: The COMPLETED run's outputs, output directory and log file.

        Raises:
            WorkflowRunError: The run FAILED or was CANCELLED, as its `reason` says. Its `exit_code` is the runner's.
            TimeoutError: The run had not ended within `timeout` seconds. It goes on.
            KeyboardInterrupt: On Ctrl-C, after the runner was stopped (`cancel`).
        """
        status = self.wait(timeout)
        assert status.exit_code is not None
        if status.state is not RunState.COMPLETED:
            assert status.reason is not None
            raise WorkflowRunError(self._request.workflow_name, status.exit_code, status.reason)
        return RunResult(status.state, status.outputs, self._launch.cwd / self._launch.outdir, self._log,
                         status.exit_code)

    def status(self) -> RunStatus:
        """Where the run is now, without blocking. A runner that has exited, its output read to the end,
        is reported on, as `wait` does; while something it started still holds its output, it is RUNNING."""
        if self._process is not None and self._process.poll() is not None \
                and not any(reader.is_alive() for reader in self._readers):
            return self._settle()
        return self._status

    def logs(self) -> str:
        """Everything the runner has printed so far, both streams as they came: the run log's text."""
        try:
            return self._log.read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return ''

    def outputs(self) -> Json | None:
        """The workflow's output object, once the run COMPLETED; else None."""
        return self.status().outputs

    def cancel(self) -> bool:
        """Stop the runner and the analyses beside it, and mark the run CANCELLED.

        A runner that has already exited is reported on as it ended, COMPLETED or FAILED, as `wait` does.
        On POSIX the runner is sent SIGTERM, on which cwltool stops the containers it started,
        and killed with everything it started when it has not exited within `STOP_GRACE_SECONDS`.

        Returns:
            bool: True when this call cancelled the run, or it was CANCELLED already; False when it
            had ended COMPLETED or FAILED, which it stays. As `concurrent.futures.Future.cancel`.
        """
        # Held throughout, so a `wait` in another thread reports the run CANCELLED and not FAILED.
        with self._settle_lock:
            process = self._process
            if process is None or self._status.state not in (RunState.QUEUED, RunState.RUNNING):
                return self._status.state is RunState.CANCELLED
            running = process.poll() is None
            if running:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(STOP_GRACE_SECONDS)
                except (sub.TimeoutExpired, KeyboardInterrupt):
                    self._kill(process)
                    process.wait()
            # Whatever the runner left in its session, which would also keep its streams open.
            self._kill(process)
            self._release_watcher()
            self._join_readers()
            if not running:
                self._settle_locked()
                return False
            if self._streams is not None:
                self._streams.log.close()
            self._stop_watching(cancelled=True)
            atexit.unregister(self._end_with_interpreter)
            print("Interrupted; the runner's processes were terminated.", file=sys.stderr)
            self._status = RunStatus(RunState.CANCELLED, 'cancelled: the runner was stopped',
                                     self._status.started_at, datetime.now(), exit_code=process.returncode)
            return True

    def _release_watcher(self) -> None:
        """Tell the watcher the run is over, so it leaves the runner's session alone."""
        watcher, self._watcher = self._watcher, None
        if watcher is None:
            return
        assert watcher.stdin is not None
        try:
            watcher.stdin.write(b'.')
            watcher.stdin.close()
        except OSError:
            pass   # it has gone already
        watcher.wait()

    @staticmethod
    def _kill(process: sub.Popen[bytes]) -> None:
        """Kill the runner and every process left in its session."""
        if sys.platform == 'win32':
            raise AssertionError('the pre-flight stops a run on Windows')
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass   # nothing is left in it

    def _settle(self) -> RunStatus:
        """Report on a runner that has exited, once: its outcome, the analyses' final runs, the summary lines."""
        with self._settle_lock:
            return self._settle_locked()

    def _settle_locked(self) -> RunStatus:
        """`_settle`, with `_settle_lock` held."""
        if self._status.state not in (RunState.QUEUED, RunState.RUNNING) or self._process is None:
            return self._status
        assert self._streams is not None
        exit_code = self._process.returncode
        self._release_watcher()
        self._streams.log.close()
        outcome = self._backend.outcome(self._request, exit_code, ''.join(self._streams.stdout), self._log)
        if outcome.summary is not None:
            print(f'Final output json metadata blob is in output_{self._request.workflow_name}.json')
        self._stop_watching(cancelled=False)
        atexit.unregister(self._end_with_interpreter)
        if self._streams.error is not None:
            self._unsupervised(self._streams.error)
        steps = tuple(StepStatus(name, RunState.FAILED, ended if isinstance(ended, int) else None,
                                 ended if isinstance(ended, str) else None)
                      for name, ended in outcome.failed.items())
        if exit_code == 0:
            print(f'Success! Runner outputs are under {self._launch.outdir}/')
            self._status = RunStatus(RunState.COMPLETED, None, self._status.started_at, datetime.now(),
                                     steps, outcome.outputs, exit_code=0)
        else:
            for line in _failure_lines(outcome.failed):
                print(line, file=sys.stderr)
            self._status = RunStatus(RunState.FAILED, f'the runner exited with status {exit_code}',
                                     self._status.started_at, datetime.now(), steps, exit_code=exit_code)
        return self._status


def _slice(deadline: float | None) -> float:
    """How long to block before looking again: a fifth of a second, or what is left before `deadline`.

    Raises:
        TimeoutError: `deadline` has passed.
    """
    if deadline is None:
        return 0.2
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError('the run has not ended yet; it goes on')
    return min(0.2, left)


def _realtime(request: RunRequest, backend: Backend) -> tuple[RunRequest, realtime.Command | None]:
    """`request` ready to run its real-time analyses, and how each is run; or, when it cannot, a line saying
    why and no analyses. With analyses and no cachedir, cwltool caches in `cachedir/`."""
    if not request.realtime_plans:
        return request, None
    if request.generate_run_script:
        print('Real-time analysis runs only with --run_local; run.sh runs the workflow without it.',
              file=sys.stderr)
        return replace(request, realtime_plans=()), None
    command = backend.realtime_command(request)
    if isinstance(command, str):
        print(command, file=sys.stderr)
        return replace(request, realtime_plans=()), None
    if not request.cachedir:
        request = replace(request, cachedir='cachedir')
        print(f"Real-time analysis watches the runner's cache, so cwltool caches in {request.cachedir}/")
    return request, command


def start(request: RunRequest) -> RunHandle:
    """Start the run `request` describes, and return its handle once the runner has started.

    Raises:
        ValueError: No backend runs the request's target with its runner, or it asks for a run script.
    """
    if request.generate_run_script:
        raise ValueError('a run script is written by run(), not started')
    backend = backends.backend(request.target, request.runner)
    request, analysis = _realtime(request, backend)
    return RunHandle(request, backend, backend.launch(request), request.realtime_plans, analysis)


def run(request: RunRequest) -> int:
    """Run the workflow `request` describes and wait for it, or write run.sh for it.

    Returns:
        int: 0 on success, else the runner's exit code; 1 when the runner could not be started.

    Raises:
        KeyboardInterrupt: On Ctrl-C, after the runner was stopped.
        ValueError: No backend runs the request's target with its runner.
    """
    backend = backends.backend(request.target, request.runner)
    if request.generate_run_script:
        _realtime(request, backend)
        generate_run_script(shlex.join(backend.launch(request).argv))
        return 0  # Do not actually run
    status = start(request).wait()
    assert status.exit_code is not None
    return status.exit_code


def cwltool_main() -> int:
    """Another entrypoint for filtering regular logged output.

    `cwltool.main.run`, not `main`: it stops the containers the run started when the run is sent SIGTERM.
    """
    import cwltool.main  # pylint: disable=import-outside-toplevel
    logging_filters()
    retval: int = cwltool.main.run(sys.argv[1:])
    return retval


def cwltool_main_pf() -> int:
    """Another entrypoint for filtering partial failures logged output"""
    import cwltool.main  # pylint: disable=import-outside-toplevel
    logging_filters(True)
    retval: int = cwltool.main.run(sys.argv[1:])
    return retval
