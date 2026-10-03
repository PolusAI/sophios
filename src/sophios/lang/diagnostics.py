"""Structured diagnostics.

Parsing reports problems as values rather than raising, so a caller can
decide what to do with them and a malformed document can yield several
errors in one pass instead of one per run.
"""
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import overload

from . import error_codes as _error_codes
from .spans import SourceSpan


class Severity(StrEnum):
    """How much a diagnostic matters.

    Two members: a note is reported and does not fail a compile; an error
    does. There is no warning: a severity no code path can produce is a claim
    no test can provoke.
    """

    ERROR = 'error'
    NOTE = 'note'


@dataclass(frozen=True, slots=True)
class Locator:
    """Where a problem sits in a document's structure, independent of text.

    A file has a line; a workflow assembled in memory does not but still has
    structure, so this names which step and which of its ports instead.
    Independent of `SourceSpan` rather than a substitute for it — a document
    read from disk carries both, and each surface renders the one it can act
    on.
    """

    step: str | None = None
    #: 1-based, matching the position a caller passed the step in.
    index: int | None = None
    port: str | None = None

    def __str__(self) -> str:
        step = self.step if self.step is not None else '?'
        where = f'step {step!r}' if self.index is None else f'step {self.index} {step!r}'
        return where if self.port is None else f'{where}, port {self.port!r}'


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single problem, located in source when a location is known.

    Parse-phase diagnostics always carry a span; compile-phase diagnostics
    may not, since a failure found on the AST often knows which workflow it
    came from but not which line. `span` and `locator` are independent —
    either, both, or neither may be present.
    """

    severity: Severity
    code: _error_codes.SophiosErrorCode
    message: str
    span: SourceSpan | None = None
    locator: 'Locator | None' = None

    def __str__(self) -> str:
        prefix = f'{self.span}: ' if self.span is not None else ''
        suffix = f' ({self.locator})' if self.locator is not None else ''
        return f'{prefix}{self.severity} [{self.code}] {self.message}{suffix}'


class Diagnostics(Sequence[Diagnostic]):
    """An ordered, append-only collection of diagnostics."""

    __slots__ = ('_items',)

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        self._items: list[Diagnostic] = list(items)

    def error(self, code: _error_codes.SophiosErrorCode,
              message: str, span: SourceSpan | None = None,
              locator: Locator | None = None) -> None:
        """Record an error.

        `span` is optional: a phase after parsing may be handed a node the
        parser never built, and a diagnostic with no position is still
        better than an exception.
        """
        self._append(Diagnostic(Severity.ERROR, code, message, span, locator))

    def note(self, code: _error_codes.SophiosErrorCode,
             message: str, span: SourceSpan | None = None,
             locator: Locator | None = None) -> None:
        """Record a note: something the reader should know that is not wrong."""
        self._append(Diagnostic(Severity.NOTE, code, message, span, locator))

    def _append(self, diagnostic: Diagnostic) -> None:
        """Append, dropping exact duplicates."""
        if diagnostic not in self._items:
            self._items.append(diagnostic)

    @property
    def has_errors(self) -> bool:
        """Whether any recorded diagnostic is an error."""
        return any(d.severity is Severity.ERROR for d in self._items)

    @overload
    def __getitem__(self, index: int) -> Diagnostic: ...

    @overload
    def __getitem__(self, index: slice) -> 'Diagnostics': ...

    def __getitem__(self, index: int | slice) -> 'Diagnostic | Diagnostics':
        if isinstance(index, slice):
            return Diagnostics(self._items[index])
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

    def __repr__(self) -> str:
        return f'Diagnostics({self._items!r})'


class SophiosError(Exception):
    """A failure the library reports, never a process it terminates.

    Carries at least one diagnostic by construction — an error with nothing
    to say is not reportable, so it is unrepresentable.
    """

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        items = Diagnostics(diagnostics)
        if not len(items):
            raise ValueError('SophiosError requires at least one diagnostic')
        super().__init__('\n'.join(str(d) for d in items))
        self.diagnostics: Diagnostics = items

    @classmethod
    def error(cls, code: _error_codes.SophiosErrorCode, *messages: str,
              span: SourceSpan | None = None, locator: Locator | None = None) -> 'SophiosError':
        """Build from one error, spelled as one or more message lines, at `span` and `locator`.

        Multiple lines become multiple diagnostics under the same code and position.
        """
        return cls(Diagnostic(Severity.ERROR, code, message, span, locator) for message in messages)
