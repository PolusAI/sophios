"""Structured diagnostics.

Parsing reports problems as values rather than raising, so a caller can decide
what to do with them and a malformed document can yield several errors in one
pass instead of one per run. See design_docs/core-refactor-design.md, Spec 1.
"""
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import overload

from .spans import SourceSpan


class Severity(StrEnum):
    """How much a diagnostic matters."""

    ERROR = 'error'
    WARNING = 'warning'


class Code(StrEnum):
    """Stable identifiers for diagnostics.

    Codes are part of the contract: they can be matched on, suppressed, and
    documented, whereas message wording is free to improve.
    """

    INVALID_YAML = 'wic001'
    NOT_A_MAPPING = 'wic002'
    EXPECTED_MAPPING = 'wic003'
    EXPECTED_SEQUENCE = 'wic004'
    EXPECTED_SCALAR = 'wic005'
    MISSING_STEP_ID = 'wic006'
    EMPTY_STEP_ID = 'wic007'
    MALFORMED_WIC_STEP_KEY = 'wic008'
    UNKNOWN_TAG = 'wic009'
    DUPLICATE_KEY = 'wic010'
    RECURSIVE_ALIAS = 'wic030'


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single problem, located in source."""

    severity: Severity
    code: Code
    message: str
    span: SourceSpan

    def __str__(self) -> str:
        return f'{self.span}: {self.severity} [{self.code}] {self.message}'


class Diagnostics(Sequence[Diagnostic]):
    """An ordered, append-only collection of diagnostics.

    Implemented as a `Sequence` so callers can index, iterate, and take a
    length without reaching for an attribute.
    """

    __slots__ = ('_items',)

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        self._items: list[Diagnostic] = list(items)

    def error(self, code: Code, message: str, span: SourceSpan) -> None:
        """Record an error."""
        self._append(Diagnostic(Severity.ERROR, code, message, span))

    def warn(self, code: Code, message: str, span: SourceSpan) -> None:
        """Record a warning."""
        self._append(Diagnostic(Severity.WARNING, code, message, span))

    def _append(self, diagnostic: Diagnostic) -> None:
        """Append, dropping exact duplicates.

        Several parse paths legitimately visit the same node — a key is read
        once to find `id:` and again to build the body — and a diagnostic-
        emitting helper called twice would otherwise report the same problem
        twice at the same position. `Diagnostic` is frozen, so identity is
        equality of all four fields; a repeat adds nothing a reader could use.
        """
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
