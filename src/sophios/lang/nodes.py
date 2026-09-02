"""Typed AST for the Sophios language.

The nodes here are the executable specification of the syntax layer: what a
well-formed Sophios document may contain, independent of which tools happen to
be installed. See design_docs/core-refactor-design.md, Spec 1.

Every node is frozen and slotted. Frozen because an AST that consumers can
mutate is not a specification of anything; slotted because these are allocated
once per construct in a document and the dict-per-instance overhead is pure
waste.
"""
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, TypeAlias

from .spans import SourceSpan


class Shape(StrEnum):
    """What a field looks like in the YAML surface.

    Semantic, not serialisation-specific: this says *what kind of thing* a
    field is, and downstream consumers decide how to express it. A JSON Schema
    generator turns `INPUT_BINDINGS` into an object of input values; a
    different consumer could turn it into something else. Neither gets to
    invent the fact that `Step.inputs` is spelled `in:`.
    """

    #: Not surface syntax at all — source spans, surface-form flags.
    INTERNAL = 'internal'
    #: The node's own name, carried by its position rather than a key.
    IDENTITY = 'identity'
    #: `in:` — a mapping of input name to one of the five input forms.
    INPUT_BINDINGS = 'input_bindings'
    #: `out:` — a sequence of bare names or single-key edge bindings.
    OUTPUT_BINDINGS = 'output_bindings'
    #: `steps:` — a mapping keyed by step name, or a sequence.
    STEPS = 'steps'
    #: `wic:` — the metadata sidecar.
    SIDECAR = 'sidecar'
    #: `wic: steps:` — a mapping keyed by `(index, name)`.
    SIDECAR_STEPS = 'sidecar_steps'
    #: A fixed set of CWL keys Sophios reads and acts upon.
    INTERPRETED = 'interpreted'
    #: Any key not claimed above: CWL, copied through untouched.
    PASSTHROUGH = 'passthrough'


@dataclass(frozen=True, slots=True)
class Surface:
    """How one AST field appears in the language's YAML surface.

    Declared beside the field it describes, so the mapping between the AST and
    the syntax lives in exactly one place. Everything that needs to know the
    shape of a document — the exported JSON Schema, the reference table —
    reads this rather than restating it.
    """

    shape: Shape
    #: The surface key this field occupies, when it occupies exactly one.
    key: str | None = None


def surface(shape: Shape, key: str | None = None, **kwargs: Any) -> Any:
    """Declare a field's surface form. Thin wrapper over `dataclasses.field`.

    The `field()` call is made here rather than at each use site so that every
    declaration is one readable line. Pylint expects `field()` to appear
    literally inside a class body and cannot see through the indirection.
    """
    # pylint: disable=invalid-field-call
    return field(metadata={'surface': Surface(shape, key)}, **kwargs)


def surface_of(node_type: type, field_name: str) -> Surface:
    """The declared surface form of one field.

    Raises if the field was never declared. That is the point: a field added
    to a node without saying how it is written is a hole in the specification,
    and it should stop the build rather than silently widen the language.
    """
    for declared in fields(node_type):
        if declared.name == field_name:
            found = declared.metadata.get('surface')
            if found is None:
                raise TypeError(
                    f'{node_type.__name__}.{field_name} has no surface declaration; '
                    f'add one with surface(Shape.…) so downstream consumers can see it'
                )
            return found  # type: ignore[no-any-return]
    raise AttributeError(f'{node_type.__name__} has no field {field_name!r}')


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

    value: 'OpaqueCwl' = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)
    text: str | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class EdgeDef:
    """`!& name` — an explicit edge definition site."""

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """`!* name` — an explicit edge call site."""

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)


@dataclass(frozen=True, slots=True)
class RawCwlRef:
    """`!cwl expression` — an opaque CWL reference, passed through unresolved.

    The compiler does not attempt to interpret the expression. This is the
    local, visible form of what `--allow_raw_cwl` does globally.
    """

    expression: str = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)


@dataclass(frozen=True, slots=True)
class UnresolvedName:
    """A bare string that must resolve to a workflow input.

    If it does not, resolution reports a diagnostic naming both remedies:
    `!ii` for a literal, `!cwl` for a raw CWL reference.
    """

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)


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

    name: str = surface(Shape.IDENTITY)
    edge_def: EdgeDef | None = surface(Shape.IDENTITY)
    span: SourceSpan = surface(Shape.INTERNAL)


@dataclass(frozen=True, slots=True)
class Step:
    """A single workflow step.

    `interpreted` holds the CWL keys Sophios acts upon (`scatter`,
    `scatterMethod`, `when`, `run`); `passthrough` holds everything else,
    preserved verbatim.
    """

    id: str = surface(Shape.IDENTITY, 'id')
    inputs: tuple[tuple[str, InputValue], ...] = surface(Shape.INPUT_BINDINGS, 'in', default=())
    outputs: tuple[OutputBinding, ...] = surface(Shape.OUTPUT_BINDINGS, 'out', default=())
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.INTERPRETED, default=())
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.PASSTHROUGH, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)

    def input(self, name: str) -> InputValue | None:
        """Return the value bound to `name`, or None if unbound."""
        return next((v for k, v in self.inputs if k == name), None)


@dataclass(frozen=True, slots=True)
class StepKey:
    """A `wic:` sidecar step key, normalised from its `"(1, name)"` form.

    The surface syntax is retained for compatibility, but nothing downstream
    should ever parse that string again.
    """

    index: int = surface(Shape.IDENTITY)
    name: str = surface(Shape.IDENTITY)

    def __str__(self) -> str:
        return f'({self.index}, {self.name})'


@dataclass(frozen=True, slots=True)
class WicSidecar:
    """The `wic:` metadata block.

    `steps` is normalised to `StepKey`; every other key (`graphviz`,
    `default_implementation`, `namespace`, ...) is retained verbatim, because
    the sidecar's surface is unchanged by this specification.
    """

    steps: tuple[tuple[StepKey, 'WicSidecar'], ...] = surface(Shape.SIDECAR_STEPS, 'steps', default=())
    entries: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.PASSTHROUGH, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed Sophios document.

    `passthrough` carries every top-level key Sophios does not interpret —
    `$namespaces`, `$schemas`, `requirements`, `hints`, and anything else —
    preserved so it can be emitted unchanged.
    """

    steps: tuple[Step, ...] = surface(Shape.STEPS, 'steps', default=())
    sidecar: WicSidecar | None = surface(Shape.SIDECAR, 'wic', default=None)
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.PASSTHROUGH, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)
    #: True when `steps:` was written as a mapping rather than a sequence.
    steps_as_mapping: bool = surface(Shape.INTERNAL, default=False)
