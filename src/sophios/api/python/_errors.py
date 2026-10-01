"""Structured failures from Python API compilation and validation.

These five errors are subclasses of `SophiosError`, so callers handling a
reported API failure can write one `except` and read `.diagnostics` whether a
document is rejected or a tool cannot be loaded. Ordinary Python call-contract
errors such as a wrong argument type remain their usual built-in exceptions.

Each carries an `api0NN` code rather than a `wic0NN` one. The document is not
what is wrong in any of these; the call is.
"""
from typing import ClassVar

from ...lang.diagnostics import Diagnostic, Severity, SophiosError
from ...lang.error_codes import SophiosErrorCode


class ApiError(SophiosError):
    """A call the Python API could not carry out.

    Subclasses name the code; the message is whatever the raise site says, in
    as many lines as it needs -- the same shape as `SophiosError.error`.
    """

    code: ClassVar[SophiosErrorCode]

    def __init__(self, *messages: str) -> None:
        super().__init__(Diagnostic(Severity.ERROR, self.code, message) for message in messages)


class InvalidInputValueError(ApiError):
    """A value bound to a step input that the input cannot take."""

    code = SophiosErrorCode.INVALID_INPUT_VALUE


class InvalidStepError(ApiError):
    """A step used where the workflow does not own it."""

    code = SophiosErrorCode.INVALID_STEP


class InvalidLinkError(ApiError):
    """A binding whose source, ownership, or types do not form a valid link."""

    code = SophiosErrorCode.INVALID_LINK


class InvalidCLTError(ApiError):
    """A CWL tool that could not be loaded or parsed."""

    code = SophiosErrorCode.INVALID_TOOL


class WorkflowRunError(ApiError):
    """A local run that finished with a non-zero exit code.

    Raised by `Workflow.run()` instead of returning, so a caller cannot miss
    a failed run. `exit_code` is the runner's. The runner's own log, printed
    before this error, says which step failed and why.
    """

    code = SophiosErrorCode.WORKFLOW_RUN_FAILED

    def __init__(self, workflow_name: str, exit_code: int) -> None:
        super().__init__(
            f'workflow {workflow_name!r} failed with exit code {exit_code}',
            "the runner's log, printed above this error, says which step failed and why")
        self.workflow_name = workflow_name
        self.exit_code = exit_code

    def __reduce__(self) -> tuple[type['WorkflowRunError'], tuple[str, int]]:
        # The message is built from the arguments, so they, not `args`, are what a
        # copy or a trip between processes has to be rebuilt from.
        return type(self), (self.workflow_name, self.exit_code)
