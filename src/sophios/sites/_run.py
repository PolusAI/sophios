"""A run on a site: its status, its logs, waiting for it and cancelling it."""
# pylint: disable=protected-access  # a run calls its site's scheduler
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .. import targets
from ..runtime.backends import RunRequest
from ..runtime.runners import RunSettings, Toil, build
from ..runtime.status import RunState, RunStatus, StepStatus
from ..wic_types import Json

from ._runners import _emptied
from ._ssh import _ALIVE, _PUT, _Said, _call, _q
from ._states import Decision, Folder, decide

if TYPE_CHECKING:
    from ._site import _Site


#: How often `wait` reads a run's status in its first minute, in seconds; every minute after.
POLL: Final = 5.0
#: How long `cancel` waits for the run to end, in seconds.
CANCEL_WAIT: Final = 120.0
_ENDED: Final = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)


def _run_folder(runs: str, run_id: str) -> str:
    return f'{runs}/{run_id}'


class Run:
    """A run on a site, made by `submit` and `attach`, never by calling the class.

    Followed as the local `RunHandle` is, `collect` aside: `status`, `wait`, `outputs`, `logs` and `cancel`; and `id`.
    Its run outlives ssh and Python, and `repr(run)` is the line that follows it from any computer. When the site
    cannot be asked, a method raises `wic062` and decides nothing.
    """

    def __init__(self, site: '_Site', record: Json) -> None:
        self._site, self._record = site, record
        #: The run's id: `<workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>`, the time UTC.
        self.id: str = record['id']
        self._folder = _run_folder(site.runs, self.id)
        self._runner = build(record['target'], record['runner'][0], ())
        self._log = b''
        self._ended: RunStatus | None = None

    def __repr__(self) -> str:
        return f'{self._site!r}.attach({self.id!r})'

    # pylint: disable-next=too-many-locals  # what the one call printed
    def status(self) -> RunStatus:
        """Where the run is now, read from its folder (design Appendix B); once it has ended, as it ended, read once.

        A run whose job has not claimed it within `UNCLAIMED_MINUTES` of the submit is claimed `none`, and FAILED. A
        run whose leader was killed outright, before its EXIT trap ran, has its outputs folder emptied here, and its
        reason written, so that every later status reads the same end.

        Raises:
            SophiosError: `wic062`: the site could not be asked.
        """
        if self._ended is not None:
            return self._ended
        run, site = _q(self._folder), self._site
        said = _Said(_call(site.url, '\n'.join([
            f'cd {run} || exit 1', _PUT, _ALIVE,
            # GNU's `stat` or BSD's, chosen once: GNU's `-f` is the file system's, which it would print for a file
            # that appeared after `-c` had missed it.
            'stat -c %Y . > /dev/null 2>&1 && form="-c %Y" || form="-f %m"',
            'mtime() { stat $form "$1" 2> /dev/null || echo; }',
            'leader=$(readlink leader 2> /dev/null || true)',
            f'echo "$(alive "$leader" {_q(self.id)} && echo yes || echo no) $(date +%s) '
            '$(mtime run.json) $leader"',
            *site._asked(self.id), 'echo "$(mtime leader) $(mtime exit)"', 'put exit 0', 'put reason 0',
            f'put run.log {len(self._log)}', 'if [ -f exit ]; then put stdout 0; fi'])))
        alive, now, submitted, leader = said.line().split(' ', 3)
        claimed, ended = said.line().split(' ')
        code, written = said.file(), said.file()
        self._log += said.file() or b''
        exit_code = None if code is None else int(code)
        scratch = f'{self._folder}/toil' if self._runner.name == Toil.name else None
        decision = decide(Folder(leader, alive == 'yes', exit_code, None if written is None else written.decode(),
                                 int(now) - int(submitted)), scratch)
        if self._act(decision) != 'none' and decision.claim_none:   # a leader claimed it first, or no claim is written
            decision = Decision(RunState.QUEUED)
        state = decision.state
        steps, outputs = (self._outcome(exit_code, said.file() or b'', state is RunState.COMPLETED)
                          if exit_code is not None and state is not RunState.CANCELLED else ((), None))
        status = RunStatus(state, decision.reason, _when(claimed) if leader.isdigit() else None, _when(ended), steps,
                           outputs, exit_code)
        if state in _ENDED:
            self._ended = status
        return status

    def _act(self, decision: Decision) -> str:
        """What `decision` says `status` must do there: claim the run `none`, or empty the outputs folder of a run whose
        leader was killed outright and then write its `reason`, unless a status has written it since `decision` was
        read: until then no other run may take the folder (`_Site._unfinished`), so it is still this run's. Returns
        the claim then, '' when nothing was done."""
        acts = [*(['ln -s none leader 2> /dev/null || true'] if decision.claim_none else []),
                *([f'if [ ! -e reason ]; then {_emptied(self._record["outdir"][0])}; '
                   f'printf %s {_q(decision.reason or "")} > reason.partial; mv reason.partial reason; fi']
                  if decision.empty else [])]
        if not acts:
            return ''
        return _call(self._site.url, '\n'.join([f'cd {_q(self._folder)} || exit 1', *acts,
                                                'readlink leader || true'])).decode().strip()

    def _outcome(self, exit_code: int, stdout: bytes, completed: bool) -> tuple[tuple[StepStatus, ...], Json | None]:
        """The failed steps and the outputs the backend's own outcome reads from what the run left, as here."""
        request = RunRequest(self._record['workflow'], Path(self._folder), RunSettings(self._runner))
        outcome = targets.backend(self._runner).outcome(request, exit_code, stdout.decode('utf-8', 'replace'),
                                                        self._log.decode('utf-8', 'replace'), self._read)
        steps = tuple(StepStatus(name, RunState.FAILED, ended if isinstance(ended, int) else None,
                                 ended if isinstance(ended, str) else None) for name, ended in outcome.failed.items())
        return steps, outcome.outputs if completed else None

    def _read(self, name: str) -> str | None:
        """The text of the file `name` in the run's folder there, or None: how the backend's outcome reads it."""
        found = _Said(_call(self._site.url, f'cd {_q(self._folder)} || exit 1\n{_PUT}\nput {_q(name)} 0')).file()
        return None if found is None else found.decode('utf-8', 'replace')

    def wait(self, timeout: float | None = None) -> RunStatus:
        """Read the status every `POLL` seconds for a minute, then every minute, until the run ends, printing what
        `run.log` gained and each change; a run that FAILED or was CANCELLED is returned, not raised.

        Raises:
            TimeoutError: It had not ended within `timeout` seconds; it goes on.
            KeyboardInterrupt: On Ctrl-C, having printed how to follow it again; it goes on.
            SophiosError: As `status` raises it.
        """
        began, shown = time.monotonic(), None
        try:
            while True:
                before = len(self._log)
                status = self.status()
                sys.stdout.write(self._log[before:].decode('utf-8', 'replace'))
                if (status.state, status.reason) != shown:
                    shown = (status.state, status.reason)
                    print(f'{self.id}: {status.state.value}' + (f' ({status.reason})' if status.reason else ''),
                          flush=True)
                if status.state in _ENDED:
                    return status
                waited = time.monotonic() - began
                if timeout is not None and waited >= timeout:
                    raise TimeoutError(f'run {self.id} has not ended within {timeout:g} s; it goes on, and '
                                       f'{self!r}.wait() waits again')
                time.sleep(min(POLL if waited < 60 else 60.0, float('inf') if timeout is None else timeout - waited))
        except KeyboardInterrupt:
            print(f'\nRun {self.id} goes on: {self!r} follows it again, and its cancel() stops it.', file=sys.stderr)
            raise

    def outputs(self) -> Json | None:
        """The workflow's output object, once the run COMPLETED; else None."""
        return self.status().outputs

    def logs(self) -> str:
        """`run.log` so far: the job script's and the runner's two streams, as they came."""
        self.status()
        return self._log.decode('utf-8', 'replace')

    def cancel(self) -> bool:
        """Claim the run `cancelled`, so a job that has not started never will, and stop its leader's process group,
        with TERM, once claimed. Then wait up to `CANCEL_WAIT` seconds for it to end.

        Returns:
            bool: True when the run ended CANCELLED, by this call or before it; False when it ended COMPLETED or
            FAILED, which it stays, or had not ended within `CANCEL_WAIT`.

        Raises:
            SophiosError: As `status` raises it.
        """
        _call(self._site.url, '\n'.join([f'cd {_q(self._folder)} || exit 1', *self._site._cancelling(self._folder)]))
        deadline = time.monotonic() + CANCEL_WAIT
        while (status := self.status()).state not in _ENDED and time.monotonic() < deadline:
            time.sleep(POLL)
        return status.state is RunState.CANCELLED


def _when(seconds: str) -> datetime | None:
    return datetime.fromtimestamp(int(seconds)) if seconds.isdigit() else None
