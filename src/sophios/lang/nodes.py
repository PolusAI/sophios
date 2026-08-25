"""Typed AST for the Sophios language.

The nodes here are the executable specification of the syntax layer: what a
well-formed Sophios document may contain, independent of which tools happen to
be installed. See design_docs/core-refactor-design.md, Spec 1.

Every node is frozen and slotted. Frozen because an AST that consumers can
mutate is not a specification of anything; slotted because these are allocated
once per construct in a document and the dict-per-instance overhead is pure
waste.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, TypeAlias

from .spans import SourceSpan


@dataclass(frozen=True, slots=True)
class InlineLiteral:
    """`!ii value` — a literal, never an edge.

    `text` is the literal's source spelling when it was parsed from tagged
    YAML, and None when it was built from the desugared form or the Python
    API. Rendering a parsed literal is transcription of `text`, never
    re-serialisation of `value` — reconstruction is lossy by nature (the
    tagged form has no spelling for the string '0'), and preserving the
    surface is what makes the round-trip exact by construction.
    """

    value: 'OpaqueCwl'
    span: SourceSpan
    text: str | None = None


@dataclass(frozen=True, slots=True)
class EdgeDef:
    """`!& name` — an explicit edge definition site."""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """`!* name` — an explicit edge call site."""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class RawCwlRef:
    """`!cwl expression` — an opaque CWL reference, passed through unresolved.

    The compiler does not attempt to interpret the expression. This is the
    local, visible form of what `--allow_raw_cwl` does globally.
    """

    expression: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UnresolvedName:
    """A bare string that must resolve to a workflow input.

    If it does not, resolution reports a diagnostic naming both remedies:
    `!ii` for a literal, `!cwl` for a raw CWL reference.
    """

    name: str
    span: SourceSpan


#: The complete set of forms a step input may take. Closed by construction:
#: the parser produces nothing outside this union, so exhaustive `match`
#: statements over it stay exhaustive.
InputValue: TypeAlias = InlineLiteral | EdgeDef | EdgeRef | RawCwlRef | UnresolvedName

#: CWL that Sophios does not interpret and passes through unchanged — but no
#: longer `Any`: a closed recursive union of exactly what YAML's safe schema
#: can produce, plus the Sophios constructs the passthrough walk preserves.
#: Closing the type is what lets the writer's match be exhaustiveness-checked
#: (a construct nested in a collection becomes a type error, not a runtime
#: RepresenterError) and what lets the test generator be *derived* from the
#: type instead of hand-written beside it — so the space the properties
#: quantify over and the space the type admits coincide by construction.
#: `date`/`datetime` are members because YAML resolves timestamps; their JSON
#: projection is ISO-8601 text (see `render.to_json`).
OpaqueCwl: TypeAlias = (
    None | bool | int | float | str | date | datetime
    | list['OpaqueCwl'] | dict[str, 'OpaqueCwl'] | InputValue
)


@dataclass(frozen=True, slots=True)
class OutputBinding:
    """One entry of a step's `out:` list.

    Either a bare name (`- file`) or a single-key mapping binding that name to
    an edge definition (`- file: !& file_touch`).
    """

    name: str
    edge_def: EdgeDef | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Step:
    """A single workflow step.

    `interpreted` holds the CWL keys Sophios acts upon (`scatter`,
    `scatterMethod`, `when`, `run`); `passthrough` holds everything else,
    preserved verbatim.
    """

    id: str
    inputs: tuple[tuple[str, InputValue], ...] = ()
    outputs: tuple[OutputBinding, ...] = ()
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None

    def input(self, name: str) -> InputValue | None:
        """Return the value bound to `name`, or None if unbound."""
        return next((v for k, v in self.inputs if k == name), None)


@dataclass(frozen=True, slots=True)
class StepKey:
    """A `wic:` sidecar step key, normalised from its `"(1, name)"` form.

    The surface syntax is retained for compatibility, but nothing downstream
    should ever parse that string again.
    """

    index: int
    name: str

    def __str__(self) -> str:
        return f'({self.index}, {self.name})'


@dataclass(frozen=True, slots=True)
class WicSidecar:
    """The `wic:` metadata block.

    `steps` is normalised to `StepKey`; every other key (`graphviz`,
    `default_implementation`, `namespace`, ...) is retained verbatim, because
    the sidecar's surface is unchanged by this specification.
    """

    steps: tuple[tuple[StepKey, 'WicSidecar'], ...] = ()
    entries: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None

    def entry(self, name: str) -> OpaqueCwl | None:
        """Return a non-`steps` sidecar entry by name."""
        return next((v for k, v in self.entries if k == name), None)


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed Sophios document.

    `passthrough` carries every top-level key Sophios does not interpret —
    `$namespaces`, `$schemas`, `requirements`, `hints`, and anything else —
    preserved so it can be emitted unchanged.
    """

    steps: tuple[Step, ...] = ()
    sidecar: WicSidecar | None = None
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    #: True when `steps:` was written as a mapping rather than a sequence.
    steps_as_mapping: bool = field(default=False)

    def step(self, step_id: str) -> Step | None:
        """Return the first step with the given id, or None."""
        return next((s for s in self.steps if s.id == step_id), None)
