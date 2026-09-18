"""What the Python API raises when a call cannot be carried out.

One type escapes this package: `SophiosError`. These four are subclasses of it,
so a caller writes one `except` and reads `.diagnostics` whatever went wrong --
a document the language rejects and a tool that will not load arrive the same
way, which is what the CLI has always done and the API never did.

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
    """An output bound to a source that is not one of the workflow's steps."""

    code = SophiosErrorCode.INVALID_LINK


class InvalidCLTError(ApiError):
    """A CWL tool that could not be loaded or parsed."""

    code = SophiosErrorCode.INVALID_TOOL
