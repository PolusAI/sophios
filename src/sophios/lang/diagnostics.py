"""Structured diagnostics.

Parsing reports problems as values rather than raising, so a caller can decide
what to do with them and a malformed document can yield several errors in one
pass instead of one per run. See design_docs/core-refactor-design.md, Spec 1.
"""
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import overload

from . import error_codes as _error_codes
from .spans import SourceSpan


class Severity(StrEnum):
    """How much a diagnostic matters.

    One member today, deliberately. Nothing in the library emits a warning,
    and a severity no code path can produce is a claim no test can provoke —
    the same reasoning that keeps unrunnable CWL versions out of `CwlVersion`.
    The axis stays so `WARNING` can return the day the first real warning
    exists, as one line here plus the emitting site that justifies it.
    """

    ERROR = 'error'


@dataclass(frozen=True, slots=True)
class Locator:
    """Where a problem sits in a document's structure, independent of text.

    A file has a line; a workflow assembled in memory does not, and inventing
    one for it would be a guess. What both have is structure, so this is what
    a caller who built a workflow programmatically can be told: which step,
    and which of its ports.

    Independent of `SourceSpan` rather than a substitute for it. A document
    read from disk carries both, and each surface renders the one it can act
    on — an editor jumps to the line, the Python API names the object.
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

    Parse-phase diagnostics always carry a span — the parser worked from
    positions, so it has one to give. Compile-phase diagnostics may not: until
    the compiler runs on the AST, a failure often knows which workflow it came
    from but not which line. An honest `None` beats an invented position.

    `locator` is the other half of that: a phase that knows which step and
    port it is complaining about can say so even when no file exists to point
    into. The two are independent — either, both, or neither may be present.
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
    """An ordered, append-only collection of diagnostics.

    Implemented as a `Sequence` so callers can index, iterate, and take a
    length without reaching for an attribute.
    """

    __slots__ = ('_items',)

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        self._items: list[Diagnostic] = list(items)

    def error(self, code: _error_codes.SophiosErrorCode,
              message: str, span: SourceSpan | None = None,
              locator: Locator | None = None) -> None:
        """Record an error.

        `span` is optional because a phase after parsing can be handed a node
        the parser never built -- a document assembled in memory -- and a
        diagnostic with no position is still better than an exception. That is
        exactly when `locator` earns its place: the structure survives where
        the text does not.
        """
        self._append(Diagnostic(Severity.ERROR, code, message, span, locator))

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


class SophiosError(Exception):
    """A failure the library reports, never a process it terminates.

    This is the deliverable of the design's §3 exception 1: library code used
    to call `sys.exit(1)`, which meant an embedder's process died and the fuzz
    test had to whitelist `SystemExit`. Every former exit site now raises this
    instead, carrying the same messages as structured diagnostics.

    Carries at least one diagnostic by construction — an error with nothing to
    say is not reportable, so it is unrepresentable.
    """

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        items = Diagnostics(diagnostics)
        if not len(items):
            raise ValueError('SophiosError requires at least one diagnostic')
        super().__init__('\n'.join(str(d) for d in items))
        self.diagnostics: Diagnostics = items

    @classmethod
    def error(cls, code: _error_codes.SophiosErrorCode, *messages: str) -> 'SophiosError':
        """Build from one error, spelled as one or more message lines.

        Multiple lines become multiple diagnostics under the same code, so the
        advice text the exit sites used to print survives verbatim.
        """
        return cls(Diagnostic(Severity.ERROR, code, message) for message in messages)
