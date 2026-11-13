"""A run on a site: its status, its logs, waiting for it and cancelling it."""
# pylint: disable=protected-access  # a run calls its site's scheduler
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .. import targets
from ..backends import RunRequest
from ..run_status import RunState, RunStatus, StepStatus
from ..wic_types import Json

from ._ssh import _PUT, _Said, _alive, _call, _q
from ._states import Decision, Folder, decide

if TYPE_CHECKING:
    from ._site import _Site


#: How often `wait` reads a run's status in its first minute, in seconds; every minute after.
POLL: Final = 5.0
#: How long `cancel` waits for the run to end, in seconds.
CANCEL_WAIT: Final = 120.0
_ENDED: Final = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)


def _run_folder(root: str, run_id: str) -> str:
    return f'{root}/sophios/runs/{run_id}'


class Run:
    """A run on a site, made by `submit` and `attach`, never by calling the class.

    Followed as the local `RunHandle` is, `collect` aside: `status`, `wait`, `outputs`, `logs` and `cancel`; and `id`.
    Its run outlives ssh and Python, and `repr(run)` is the line that follows it from any computer. When the site
    or its scheduler cannot be asked, a method raises `wic057` and decides nothing.
    """

    def __init__(self, site: '_Site', record: Json) -> None:
        self._site, self._record = site, record
        #: The run's id: `<workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>`, the time UTC.
        self.id: str = record['id']
        self._folder = _run_folder(site.root, self.id)
        self._backend = targets.backend(record['target'], record['runner'][0])
        self._log = b''
        self._ended: RunStatus | None = None

    def __repr__(self) -> str:
        return f'{self._site!r}.attach({self.id!r})'

    # pylint: disable-next=too-many-locals  # what the one call printed
    def status(self) -> RunStatus:
        """Where the run is now, read from its folder, and from Slurm only when the folder has not decided (design
        Appendix B); once it has ended, as it ended, read once.

        A run whose job has not claimed it within `UNCLAIMED_MINUTES` of the submit, and of which Slurm lists no job
        that may still claim it, is claimed `none`, and FAILED. On Slurm, a run one of whose jobs Slurm will never
        start is stopped, its `reason` naming the setting to change; the step jobs a killed runner left are cancelled.

        Raises:
            SophiosError: `wic057`: the site, or its Slurm, could not be asked.
        """
        if self._ended is not None:
            return self._ended
        run, site = _q(self._folder), self._site
        said = _Said(_call(site.host, '\n'.join([
            f'cd {run} || exit 1', _PUT, _alive(self._folder),
            'mtime() { stat -c %Y "$1" 2> /dev/null || stat -f %m "$1" 2> /dev/null || echo; }',
            'leader=$(readlink leader 2> /dev/null || true)',
            'echo "$(alive "$leader" && echo yes || echo no) $(date +%s) $(mtime run.json) $leader"',
            *site._asked(self.id), 'echo "$(mtime leader) $(mtime exit)"', 'put exit 0', 'put reason 0',
            f'put run.log {len(self._log)}', 'if [ -f exit ]; then put stdout 0; fi'])))
        alive, now, submitted, leader = said.line().split(' ', 3)
        queue = site._queue(self.id, said, int(now))
        claimed, ended = said.line().split(' ')
        code, written = said.file(), said.file()
        self._log += said.file() or b''
        exit_code = None if code is None else int(code)
        scratch = f'{self._folder}/toil' if self._backend.program == 'toil-cwl-runner' else None
        decision = decide(Folder(leader, alive == 'yes', exit_code, None if written is None else written.decode(),
                                 int(now) - int(submitted)), scratch, queue)
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
        """What `decision` says `status` must do there: claim the run `none`, stop it as `cancel` does and then write
        its `reason`, or cancel the step jobs a killed runner left. A `scancel` Slurm refuses ends the call, which
        raises, the `reason` unwritten. Returns the claim then, '' when nothing was done."""
        acts = [*(['ln -s none leader 2> /dev/null || true'] if decision.claim_none else []),
                *([*self._site._cancelling(self._folder), f'printf %s {_q(decision.reason or "")} > reason.partial',
                   'mv reason.partial reason'] if decision.stop else []),
                *([f'{shlex.join(["scancel", *decision.cancel])} || exit 1'] if decision.cancel else [])]
        if not acts:
            return ''
        return _call(self._site.host, '\n'.join([f'cd {_q(self._folder)} || exit 1', *acts,
                                                 'readlink leader || true'])).decode().strip()

    def _outcome(self, exit_code: int, stdout: bytes, completed: bool) -> tuple[tuple[StepStatus, ...], Json | None]:
        """The failed steps and the outputs the backend's own outcome reads from what the run left, as here."""
        request = RunRequest(self._record['workflow'], Path(self._folder), runner=self._record['runner'][0],
                             target=self._record['target'])
        outcome = self._backend.outcome(request, exit_code, stdout.decode('utf-8', 'replace'),
                                        self._log.decode('utf-8', 'replace'), self._read)
        steps = tuple(StepStatus(name, RunState.FAILED, ended if isinstance(ended, int) else None,
                                 ended if isinstance(ended, str) else None) for name, ended in outcome.failed.items())
        return steps, outcome.outputs if completed else None

    def _read(self, name: str) -> str | None:
        """The text of the file `name` in the run's folder there, or None: how the backend's outcome reads it."""
        found = _Said(_call(self._site.host, f'cd {_q(self._folder)} || exit 1\n{_PUT}\nput {_q(name)} 0')).file()
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
        """Claim the run `cancelled`, so a job that has not started never will, and stop its leader: on a machine its
        process group, with TERM, once claimed; on Slurm every job named after the run, with `scancel`, before the
        claim. Then wait up to `CANCEL_WAIT` seconds for it to end.

        Returns:
            bool: True when the run ended CANCELLED, by this call or before it; False when it ended COMPLETED or
            FAILED, which it stays, or had not ended within `CANCEL_WAIT`.

        Raises:
            SophiosError: As `status` raises it; `wic057` too when Slurm refuses the `scancel`, nothing written.
        """
        _call(self._site.host, '\n'.join([f'cd {_q(self._folder)} || exit 1', *self._site._cancelling(self._folder)]))
        deadline = time.monotonic() + CANCEL_WAIT
        while (status := self.status()).state not in _ENDED and time.monotonic() < deadline:
            time.sleep(POLL)
        return status.state is RunState.CANCELLED


def _when(seconds: str) -> datetime | None:
    return datetime.fromtimestamp(int(seconds)) if seconds.isdigit() else None
