"""How a run is, and how it ended: what `Workflow.start()`'s handle reports.

The states and the status are those the compute service reports for a run (its contract 2.0.0),
so a run reads the same wherever it runs. `WorkflowRunError` is what the handle's `collect()`
raises for a run that did not complete.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import Json


class RunState(Enum):
    """Where a run is: the states the compute service reports, too.

    QUEUED and RUNNING may change; COMPLETED, FAILED and CANCELLED never do. A local
    run is never seen QUEUED: `Workflow.start()` returns once its runner has started,
    or could not be started.
    """

    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'


@dataclass(frozen=True, slots=True)
class RunResult:
    """A run that COMPLETED, and where it left its outputs and its log: what `RunHandle.collect()` returns."""

    #: COMPLETED: a run that ends any other way is raised, as `WorkflowRunError`.
    state: RunState
    #: The workflow's output object, or None when the runner gave none.
    outputs: Json | None
    #: The directory the runner moved the workflow's outputs to.
    outdir: Path
    #: Both of the runner's streams, as they came: `<basepath>/run_<workflow>.log`.
    log_file: Path
    #: The runner's exit code, 0.
    exit_code: int


@dataclass(frozen=True, slots=True)
class StepStatus:
    """A step of the run that failed: how it ended, by exit status or by signal, when the runner said."""

    name: str
    state: RunState
    exit_status: int | None = None
    signal: str | None = None


@dataclass(frozen=True, slots=True)
class RunStatus:  # pylint: disable=too-many-instance-attributes
    """A run's status: where it is, and how it ended once it has."""

    #: Where the run is.
    state: RunState
    #: Why it is in its state, when there is something to say.
    reason: str | None = None
    #: When the runner started; None until then.
    started_at: datetime | None = None
    #: When the run ended; None until then.
    ended_at: datetime | None = None
    #: The steps that failed, in the order they failed.
    steps: tuple[StepStatus, ...] = ()
    #: The workflow's output object, once the run COMPLETED.
    outputs: Json | None = None
    #: The runner's exit code, once it exited.
    exit_code: int | None = None


class WorkflowRunError(SophiosError):
    """A local run that did not complete: it FAILED, or was CANCELLED.

    Raised by `RunHandle.collect()`, and so by `Workflow.run()`, instead of returning,
    so a caller cannot miss a failed run. `exit_code` is the runner's, and `reason` the
    run status's: how the run ended. The lines printed before this error say why.
    """

    code = SophiosErrorCode.WORKFLOW_RUN_FAILED

    def __init__(self, workflow_name: str, exit_code: int, reason: str) -> None:
        super().__init__(Diagnostic(Severity.ERROR, self.code, message) for message in (
            f'workflow {workflow_name!r} did not complete: {reason}',
            'the lines printed above this error say why'))
        self.workflow_name = workflow_name
        self.exit_code = exit_code
        self.reason = reason

    def __reduce__(self) -> tuple[type['WorkflowRunError'], tuple[str, int, str]]:
        # The message is built from the arguments, so they, not `args`, are what a
        # copy or a trip between processes has to be rebuilt from.
        return type(self), (self.workflow_name, self.exit_code, self.reason)
