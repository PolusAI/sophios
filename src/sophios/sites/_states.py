"""A run's states, as its folder says them (design Appendix B)."""
from dataclasses import dataclass
from typing import Final

from ..runtime.status import RunState


#: How long a run may go unclaimed after its submit before `status` decides its job never started, in minutes.
UNCLAIMED_MINUTES: Final = 10
_NEVER_CLAIMED: Final = f'its job never claimed the run within {UNCLAIMED_MINUTES} minutes of the submit'


@dataclass(frozen=True, slots=True)
class Folder:
    """What a run's folder says, read before anything else is asked: the claim, whether its leader runs, `exit`,
    `reason`, and how long ago the run was submitted."""

    #: `leader`'s target: the leader's pid, `cancelled` or `none`; '' while nothing has claimed the run.
    leader: str
    #: The claimed leader runs `<run>/job.sh`, read before `exit`, so a leader found gone has left its `exit`.
    alive: bool
    #: What `exit` holds: the job script's exit status, written last; None while there is none.
    exit: int | None
    #: What `reason` holds: why the run failed, written by the status that found its leader killed outright; None
    #: while there is none.
    reason: str | None = None
    #: Seconds since the submit, on the site's clock.
    age: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    """A run's state and why, and what `status` must do there for it to hold."""

    state: RunState
    reason: str | None = None
    #: Claim the run `none`: no job of it will ever claim it.
    claim_none: bool = False
    #: Empty the outputs folder and write `reason`: the leader was killed before its EXIT trap could empty it.
    empty: bool = False


# pylint: disable-next=too-many-return-statements  # the design's table: the first row that applies
def decide(folder: Folder, scratch: str | None) -> Decision:
    """A run's state and why (design Appendix B): the first row that applies. `scratch` is the folder a hard kill
    leaves (Toil's `<run>/toil`), None for a runner that leaves none."""
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
    if folder.exit is not None:
        return Decision(RunState.CANCELLED, 'cancelled') if folder.exit == 143 else Decision(
            RunState.FAILED, _killed(folder.exit))
    if not folder.leader:
        if folder.age < 60 * UNCLAIMED_MINUTES:
            return Decision(RunState.QUEUED)
        return Decision(RunState.FAILED, _NEVER_CLAIMED, claim_none=True)
    if folder.alive:
        return Decision(RunState.RUNNING)
    return Decision(RunState.FAILED, 'its job was killed before it recorded how the run ended (SIGKILL, out of '
                    f'memory, or the computer stopped){_left(scratch)}', empty=True)


def _killed(code: int) -> str:
    return f'the runner ended with status {code}, a kill by signal {code - 128}: out of memory, or by hand'


def _left(scratch: str | None) -> str:
    return f'; {scratch} is left: rm -rf {scratch} removes it' if scratch else ''
