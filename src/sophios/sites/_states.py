"""A run's states, as its folder and, on Slurm, its jobs say them (design Appendix B)."""
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ..run_status import RunState


#: How long a run may go unclaimed after its submit before `status` decides its job never started, in minutes.
UNCLAIMED_MINUTES: Final = 10
#: How long after Slurm says a leader ended `status` waits for its `exit`, in seconds: NFS caches entries up to 60 s.
ENDED_GRACE: Final = 120
_NEVER_CLAIMED: Final = f'its job never claimed the run within {UNCLAIMED_MINUTES} minutes of the submit'
#: Why Slurm will never start a pending job, as squeue says it, and the setting that would let it start: a per-job
#: limit, which no wait clears (Slurm's job reason codes). A group limit, a drained node or a partition set down is
#: not among them: each clears in time.
_NEVER: Final = (
    (re.compile(r'PartitionTimeLimit|(QOS|Assoc)MaxWallDuration\w*'), 'give a shorter time='),
    (re.compile(r'InvalidAccount|AccountNotAllowed'), 'name an account you may use with account='),
    (re.compile(r'InvalidQOS|QOSNotAllowed'), 'name a QOS you may use with SBATCH_QOS in your login profile there'),
    (re.compile(r'(QOS|Assoc)Max\w*Per(Job|Node)\w*|QOSMin\w*|MaxMemPerLimit|BadConstraints'),
     "ask for no more than the partition and your limits allow: a step asks for its tool's resources"))
#: What a leader's end says, by the state Slurm ended it in, when it ended by no `exit` of its own.
_CAUSES: Final[Mapping[str, str]] = {
    'TIMEOUT': 'it reached its time limit: give a longer time=', 'OUT_OF_MEMORY': 'it ran out of memory',
    'PREEMPTED': 'Slurm preempted it', 'NODE_FAIL': 'its node failed'}


@dataclass(frozen=True, slots=True)
class Folder:
    """What a run's folder says, read before anything else is asked: the claim, whether its leader runs, `exit`,
    `reason`, and how long ago the run was submitted."""

    #: `leader`'s target: the leader's pid or Slurm job id, `cancelled` or `none`; '' while nothing has claimed the run.
    leader: str
    #: On a machine, the claimed leader runs `<run>/job.sh`, read before `exit`, so a leader found gone has left its
    #: `exit`.
    alive: bool
    #: What `exit` holds: the job script's exit status, written last; None while there is none.
    exit: int | None
    #: What `reason` holds: why `status` stopped a run Slurm would never start; None while there is none.
    reason: str | None = None
    #: Seconds since the submit, on the site's clock.
    age: int = 0


@dataclass(frozen=True, slots=True)
class Job:
    """A job of a run on Slurm: one squeue lists with the run's comment, or a leader sacct lists by the run's name."""

    id: str
    #: Named after the run: a leader, of which a submit made again may have made two; else a step job of its runner's.
    leader: bool
    #: Slurm's state: PENDING, RUNNING, ...; as sacct ends it, COMPLETED, `CANCELLED by 1000`, TIMEOUT, ...
    state: str
    #: Why a pending job waits, as squeue says: Priority, Dependency, PartitionTimeLimit, ...
    pending: str = ''
    #: Seconds since it ended, as sacct says; None while it has not.
    ended: int | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """A run's state and why, and what `status` must do there for it to hold."""

    state: RunState
    reason: str | None = None
    #: Claim the run `none`: no job of it will ever claim it.
    claim_none: bool = False
    #: Write `reason` and stop the run as `cancel` does: Slurm will never start one of its jobs.
    stop: bool = False
    #: The step jobs a killed runner left in Slurm's queue, which `status` cancels.
    cancel: tuple[str, ...] = ()


# pylint: disable-next=too-many-return-statements  # the design's table: the first row that applies
def decide(folder: Folder, scratch: str | None, queue: Callable[[], Sequence[Job]] | None = None) -> Decision:
    """A run's state and why (design Appendix B): the first row that applies.

    The folder decides first; `queue`, the run's jobs as Slurm lists them, is asked only when it has not, and raises
    when Slurm does not answer, deciding nothing. It is None on a machine, where `folder.alive` says whether the leader
    runs. `scratch` is the folder a hard kill leaves (Toil's `<run>/toil`), None for a runner that leaves none.
    """
    if folder.reason is not None:
        return Decision(RunState.FAILED, folder.reason)
    if folder.exit == 0:
        return Decision(RunState.COMPLETED)
    if folder.leader == 'cancelled':
        return Decision(RunState.CANCELLED, 'cancelled')
    if folder.leader == 'none':
        return Decision(RunState.FAILED, _NEVER_CLAIMED)
    if folder.exit is not None and folder.exit <= 128:
        return Decision(RunState.FAILED, f'the runner exited with status {folder.exit}')
    if queue is not None:
        return _on_slurm(folder, scratch, queue())
    if folder.exit is not None:
        return Decision(RunState.CANCELLED, 'cancelled') if folder.exit == 143 else Decision(
            RunState.FAILED, _killed(folder.exit))
    if not folder.leader:
        return _unclaimed(folder, None)
    if folder.alive:
        return Decision(RunState.RUNNING)
    return Decision(RunState.FAILED, 'its job was killed before it recorded how the run ended (SIGKILL, out of '
                    f'memory, or the computer stopped){_left(scratch)}')


def _on_slurm(folder: Folder, scratch: str | None, jobs: Sequence[Job]) -> Decision:
    """`decide` for a run on Slurm whose folder has not decided: by the run's jobs, as Slurm lists them."""
    never = next((job for job in jobs if job.state == 'PENDING' and _remedy(job.pending)
                  and folder.exit is None), None)
    if not folder.leader:
        if never is not None and never.leader:
            return _stopped(never)
        live = [job for job in jobs if job.leader and (job.ended is None or job.ended < ENDED_GRACE)]
        return _unclaimed(folder, next((f"waiting in Slurm's queue ({job.pending})" for job in live
                                        if job.state == 'PENDING'), '' if live else None))
    leader = next((job for job in jobs if job.id == folder.leader), None)
    if leader is None:   # Slurm may list it only later; an `exit` decides alone
        running = folder.exit is None
    else:
        running = leader.ended is None or (folder.exit is None and leader.ended < ENDED_GRACE)
    if running:
        return Decision(RunState.RUNNING) if never is None else _stopped(never)
    left = tuple(job.id for job in jobs if not job.leader and job.ended is None)
    ended = leader.state.split(' ')[0] if leader is not None else 'no longer lists its job'
    if ended == 'CANCELLED' and folder.exit in (143, None):
        return Decision(RunState.CANCELLED, 'cancelled' if folder.exit else
                        f'cancelled; its cleanup was cut short{_left(scratch)}', cancel=left)
    told = (_killed(folder.exit) if folder.exit is not None else
            'its job was killed before it recorded how the run ended')
    cause = f"Slurm ended the run's job {ended}: {_CAUSES[ended]}" if ended in _CAUSES else f'{told} (Slurm: {ended})'
    return Decision(RunState.FAILED, cause + ('' if folder.exit is not None else _left(scratch)), cancel=left)


def _unclaimed(folder: Folder, waiting: str | None) -> Decision:
    """An unclaimed run: QUEUED while a job of it may still claim it (`waiting`, '' or Slurm's reason, when one is
    listed) or within `UNCLAIMED_MINUTES` of its submit; then claimed `none`."""
    if waiting is not None or folder.age < 60 * UNCLAIMED_MINUTES:
        return Decision(RunState.QUEUED, waiting or None)
    return Decision(RunState.FAILED, _NEVER_CLAIMED, claim_none=True)


def _killed(code: int) -> str:
    return f'the runner ended with status {code}, a kill by signal {code - 128}: out of memory, or by hand'


def _left(scratch: str | None) -> str:
    return f'; {scratch} is left: rm -rf {scratch} removes it' if scratch else ''


def _remedy(pending: str) -> str | None:
    """The setting that would let a job Slurm will never start start, by why it pends; None for one that may start."""
    return next((remedy for pattern, remedy in _NEVER if pattern.fullmatch(pending)), None)


def _stopped(job: Job) -> Decision:
    who = 'the run' if job.leader else f'its step job {job.id}'
    return Decision(RunState.FAILED, f'Slurm will never start {who} ({job.pending}): {_remedy(job.pending)}', stop=True)


def _jobs(run_id: str, listed: str, accounted: str, now: int) -> tuple[Job, ...]:
    """The run's jobs: each `squeue` lists with the run's comment (`%i|%T|%r|%k|%j`), then each leader `sacct` lists
    by the run's name that squeue does not (`JobID|State|End`, End in seconds)."""
    found = [Job(job, name == run_id, state, pending)
             for job, state, pending, comment, name in (line.split('|', 4) for line in listed.splitlines()
                                                        if line.count('|') >= 4)
             if comment == f'sophios:{run_id}']
    known = {job.id for job in found}
    return (*found, *(Job(job, True, state, '', now - int(end) if end.isdigit() else None)
                      for job, state, end in (line.split('|') for line in accounted.splitlines()
                                              if line.count('|') == 2) if job not in known))
