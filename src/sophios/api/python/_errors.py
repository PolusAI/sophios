"""Structured failures from Python API compilation and validation.

These four errors are subclasses of `SophiosError`, so callers handling a
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
