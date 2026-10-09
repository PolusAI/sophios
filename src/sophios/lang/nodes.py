"""Typed AST for the Sophios language.

The nodes here are the executable specification of the syntax layer: what a
well-formed Sophios document may contain, independent of which tools happen
to be installed. Every node is frozen (an AST consumers can mutate is not a
specification of anything) and slotted (allocated once per construct).
"""
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from enum import StrEnum
from collections.abc import Mapping
from typing import Any, Final, TypeAlias

from ..utils_yaml import Key
from .spans import SourceSpan
from .support import STEP_INPUT_RECORD_KEYS


class Shape(StrEnum):
    """What a field looks like in the YAML surface.

    Semantic, not serialisation-specific: says *what kind of thing* a field
    is, and downstream consumers decide how to express it.
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
    #: The `wic:` block's value keys: a closed set, each with a declared
    #: value shape (`Grammar.SIDECAR_VALUES`, language guide §7).
    SIDECAR_ENTRIES = 'sidecar_entries'
    #: Any key not claimed above: CWL, copied through untouched.
    PASSTHROUGH = 'passthrough'


@dataclass(frozen=True, slots=True)
class Surface:
    """How one AST field appears in the language's YAML surface.

    Declared beside the field it describes, so the mapping between the AST
    and the syntax lives in exactly one place.
    """

    shape: Shape
    #: The surface key this field occupies, when it occupies exactly one.
    key: str | None = None


def surface(shape: Shape, key: str | None = None, **kwargs: Any) -> Any:
    """Declare a field's surface form. Thin wrapper over `dataclasses.field`."""
    # pylint: disable=invalid-field-call
    return field(metadata={'surface': Surface(shape, key)}, **kwargs)


def surface_of(node_type: type, field_name: str) -> Surface:
    """The declared surface form of one field.

    Raises if the field was never declared: an undeclared field is a hole in
    the specification and should stop the build, not silently widen the
    language.
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

    `text` is the literal's source spelling when parsed from a plain tagged
    scalar (`!ii 0777`, whose value 511 knows nothing of its spelling), and
    None when parsed from a quoted or block scalar, whose value is its text,
    or built from the desugared form or the Python API. Rendering a parsed
    literal transcribes `text` rather than re-serialising `value`, since
    reconstruction is lossy by nature.
    """

    value: 'OpaqueCwl' = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)
    text: str | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class EdgeDef:
    """`!& name` — an explicit edge definition site.

    Legal only where a value comes into being: an output. Reachable only
    through `OutputBinding.edge_def`, not a member of `InputValue`. `!&`
    written in input position is reported as `wic019` instead.
    """

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """`!* name` — an explicit edge call site."""

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class RawCwlRef:
    """`!cwl expression` — an opaque CWL reference, passed through unresolved.

    The compiler does not attempt to interpret the expression. This is the
    local, visible form of what `--allow_raw_cwl` does globally.
    """

    expression: str = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class UnresolvedName:
    """A bare string that must resolve to a workflow input.

    If it does not, resolution reports a diagnostic naming both remedies:
    `!ii` for a literal, `!cwl` for a raw CWL reference.
    """

    name: str = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class CwlRecord:
    """`!cwl {source: ..., linkMerge: ...}` — CWL's WorkflowStepInput, at an `in:` position.

    `sources` are the Sophios references its `source` names, in order; each is
    an edge (`!*`) or a workflow input (a bare name). `fields` are the other
    WorkflowStepInput fields, verbatim. The record is the one place a step
    input may say more than where its value comes from.
    """

    sources: tuple[EdgeRef | UnresolvedName, ...] = surface(Shape.IDENTITY, default=())
    fields: tuple[tuple[str, 'OpaqueCwl'], ...] = surface(Shape.IDENTITY, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)

    @property
    def delivers_its_source(self) -> bool:
        """Whether the step input receives its one source's value as it is:
        no merge, no pick and no `valueFrom` stands between them."""
        return len(self.sources) == 1 and not {'linkMerge', 'pickValue', 'valueFrom'} & dict(self.fields).keys()


#: The complete set of forms a step input may take: a literal, an edge
#: reference, a raw CWL reference, an unresolved name, or a step-input
#: record. `EdgeDef` is deliberately not a member — an edge is defined on an
#: output, reachable only through `OutputBinding.edge_def`. Closed by
#: construction, so exhaustive `match` statements over it stay exhaustive.
InputValue: TypeAlias = InlineLiteral | EdgeRef | RawCwlRef | UnresolvedName | CwlRecord

#: CWL that Sophios does not interpret and passes through unchanged: a closed
#: recursive union of what YAML's safe schema can produce, plus the Sophios
#: constructs the passthrough walk preserves. `date`/`datetime` are members
#: because YAML resolves timestamps; their JSON projection is ISO-8601 text
#: (see `render.to_json`).
OpaqueCwl: TypeAlias = (
    None | bool | int | float | str | date | datetime
    | list['OpaqueCwl'] | dict[str, 'OpaqueCwl'] | InputValue
)

#: The WorkflowStepInput fields a record may carry, besides `source`.
RECORD_FIELDS: Final = STEP_INPUT_RECORD_KEYS - {'source'}


def _holds_construct(value: OpaqueCwl) -> bool:
    """Whether a Sophios construct sits anywhere inside `value`."""
    match value:
        case dict():
            return any(_holds_construct(item) for item in value.values())
        case list():
            return any(_holds_construct(item) for item in value)
        case InlineLiteral() | EdgeRef() | RawCwlRef() | UnresolvedName() | CwlRecord():
            return True
        case _:
            return False


def cwl_record(mapping: Mapping[str, OpaqueCwl], span: SourceSpan | None) -> tuple[CwlRecord, tuple[str, ...]]:
    """Build a record from its desugared mapping; also return the keys it may not carry.

    `source` entries are `!*` or `{wic_alias: name}` (an edge), a bare string
    (a workflow input), or a list of those. Anything else in `source` is not a
    reference, so `'source'` is returned among the bad keys, as is a field
    holding a Sophios construct: every field but `source` is CWL, copied out.
    """
    sources: list[EdgeRef | UnresolvedName] = []
    bad: list[str] = [key for key, value in mapping.items()
                      if key != 'source' and (key not in RECORD_FIELDS or _holds_construct(value))]
    raw = mapping.get('source', [])
    for entry in raw if isinstance(raw, list) else [raw]:
        match entry:
            case EdgeRef() | UnresolvedName():
                sources.append(entry)
            case {Key.ALIAS: str() as name, **rest} if not rest:
                sources.append(EdgeRef(name, span))
            case str() as name:
                sources.append(UnresolvedName(name, span))
            case _:
                bad.append('source')
    carried = tuple((key, value) for key, value in mapping.items() if key in RECORD_FIELDS and key not in bad)
    return CwlRecord(tuple(sources), carried, span), tuple(dict.fromkeys(bad))


@dataclass(frozen=True, slots=True)
class OutputBinding:
    """One entry of a step's `out:` list.

    Either a bare name (`- file`) or a single-key mapping binding that name to
    an edge definition (`- file: !& file_touch`).
    """

    name: str = surface(Shape.IDENTITY)
    edge_def: EdgeDef | None = surface(Shape.IDENTITY)
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class Step:
    """A single workflow step.

    `interpreted` holds the CWL keys Sophios acts upon (`scatter`,
    `scatterMethod`, `when`, `run`); `passthrough` holds everything else,
    preserved verbatim, bar the list form of `requirements` and `hints`: the
    parser reads it as a mapping, unless the list holds an `$import` or
    `$include`, which is held as written.
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
    """A `wic:` sidecar step key: `"(1, name)"` for the step at position 1,
    or a bare `"name"` for the one step with that id (`index` is None)."""

    index: int | None = surface(Shape.IDENTITY)
    name: str = surface(Shape.IDENTITY)

    def __str__(self) -> str:
        return self.name if self.index is None else f'({self.index}, {self.name})'


@dataclass(frozen=True, slots=True)
class WicSidecar:
    """The `wic:` metadata block.

    `steps` is normalised to `StepKey`; every other key (`graphviz`,
    `default_implementation`, `namespace`, ...) is retained verbatim, once the
    parser has checked it is a key the block has, with a value of its shape.
    """

    steps: tuple[tuple[StepKey, 'WicSidecar'], ...] = surface(Shape.SIDECAR_STEPS, 'steps', default=())
    entries: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.SIDECAR_ENTRIES, default=())
    #: Each `implementations:` body, parsed. Internal, not a second surface:
    #: the bodies stay in `entries` and are spelled from there, so rendering
    #: is unchanged.
    implementations: tuple[tuple[str, 'Document'], ...] = surface(Shape.INTERNAL, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed Sophios document.

    `passthrough` carries every top-level key Sophios does not interpret —
    `$namespaces`, `$schemas`, `requirements`, `hints`, and anything else —
    preserved so it can be emitted unchanged, bar the list form of `inputs`,
    `outputs`, `requirements` and `hints`: the parser reads it as a mapping,
    unless the list holds an `$import` or `$include`, which is held as written.
    """

    steps: tuple[Step, ...] = surface(Shape.STEPS, 'steps', default=())
    sidecar: WicSidecar | None = surface(Shape.SIDECAR, 'wic', default=None)
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.PASSTHROUGH, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)
    #: True when `steps:` was written as a mapping rather than a sequence.
    steps_as_mapping: bool = surface(Shape.INTERNAL, default=False)
