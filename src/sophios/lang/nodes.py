"""Typed AST for the Sophios language.

The nodes here are the executable specification of the syntax layer: what a
well-formed Sophios document may contain, independent of which tools happen
to be installed. Every node is frozen (an AST consumers can mutate is not a
specification of anything) and slotted (allocated once per construct).
"""
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, TypeAlias

from .needs import Inherited, split
from .spans import SourceSpan
from .types import Merge, Pick


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
    #: value shape (`Grammar.SIDECAR_VALUES`, .wic reference §7).
    SIDECAR_ENTRIES = 'sidecar_entries'
    #: One of a tool's own keys: of a closed set, each with a declared value
    #: shape (`Grammar.TOOL_VALUES`, .wic reference §2.4).
    TOOL_VALUE = 'tool_value'
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
    def merge(self) -> Merge | None:
        """How its sources join, from `linkMerge`; None when it says none, or says one the standard does not have,
        which stays in `passthrough` for a target to refuse by name."""
        return next((way for way in Merge if way == dict(self.fields).get('linkMerge')), None)

    @property
    def pick(self) -> Pick | None:
        """Which of the non-null values it takes, from `pickValue`; None as `merge` is."""
        return next((way for way in Pick if way == dict(self.fields).get('pickValue')), None)

    @property
    def passthrough(self) -> tuple[tuple[str, 'OpaqueCwl'], ...]:
        """The fields no meaning above holds (`valueFrom`, `label`, `doc`, a merge or a pick the standard does not
        have, ...), verbatim, in written order."""
        held = {'default', *(key for key, way in (('linkMerge', self.merge), ('pickValue', self.pick))
                             if way is not None)}
        return tuple((key, value) for key, value in self.fields if key not in held)

    @property
    def computed(self) -> bool:
        """Whether a `valueFrom` is written, whatever its value."""
        return 'valueFrom' in dict(self.fields)

    @property
    def computes(self) -> bool:
        """Whether a `valueFrom` is written that is not null."""
        return dict(self.fields).get('valueFrom') is not None

    @property
    def default(self) -> 'OpaqueCwl':
        """The `default` as written."""
        return dict(self.fields).get('default')

    @property
    def has_default(self) -> bool:
        """Whether a `default` is written, whatever its value."""
        return 'default' in dict(self.fields)

    @property
    def changes(self) -> bool:
        """Whether something stands between its sources and the step input: not one source, or a merge, a pick or
        a `valueFrom`, as written."""
        return len(self.sources) != 1 or bool({'linkMerge', 'pickValue', 'valueFrom'} & dict(self.fields).keys())

    def map_default(self, resolve: Callable[['OpaqueCwl'], 'OpaqueCwl']) -> 'CwlRecord':
        """This record with its `default`, if it has one, replaced by `resolve` of it."""
        return replace(self, fields=tuple((key, resolve(value) if key == 'default' else value)
                                          for key, value in self.fields))


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
    #: An inline `run:` body that says `command:` or `script:`, read as the tool it is. Internal, not a second
    #: surface: the body stays in `interpreted`, from which it is spelled.
    tool: 'ToolDocument | None' = surface(Shape.INTERNAL, default=None)
    #: What it asks of the tools beneath it (the requirements, then the hints, it wrote) and the names of its other
    #: keys that nothing reads, derived from `passthrough`.
    needs: tuple[Inherited, ...] = surface(Shape.INTERNAL, init=False, compare=False, repr=False, default=())
    unread: tuple[str, ...] = surface(Shape.INTERNAL, init=False, compare=False, repr=False, default=())

    def __post_init__(self) -> None:
        needs, unread = split(self.passthrough)
        object.__setattr__(self, 'needs', needs)
        object.__setattr__(self, 'unread', unread)


@dataclass(frozen=True, slots=True)
class StepKey:
    """A `wic:` sidecar step key: `"(1, name)"` for the step at position 1,
    or a bare `"name"` for the one step with that id (`index` is None)."""

    index: int | None = surface(Shape.IDENTITY)
    name: str = surface(Shape.IDENTITY)

    def __str__(self) -> str:
        return self.name if self.index is None else f'({self.index}, {self.name})'


@dataclass(frozen=True, slots=True)
class GraphvizEntry:
    """A `wic: graphviz:` mapping as the parser admits it (`Grammar.SIDECAR_VALUES`): a cluster's or box's label and
    style, and the steps that share a rank (None when the mapping has no `ranksame:`). A `ranksame` entry that is
    no step key was refused when parsing."""

    label: str | None = None
    style: str | None = None
    ranksame: tuple[StepKey, ...] | None = None

    def merged_over(self, own: 'GraphvizEntry') -> 'GraphvizEntry':
        """`own` with what this entry says over it, key by key."""
        return GraphvizEntry(self.label if self.label is not None else own.label,
                             self.style if self.style is not None else own.style,
                             self.ranksame if self.ranksame is not None else own.ranksame)


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
    #: The `graphviz` entry, read once by the parser; it stays in `entries` too, from which it is spelled.
    graphviz: GraphvizEntry = surface(Shape.INTERNAL, default=GraphvizEntry())
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
    #: Where each entry of `inputs:` and `outputs:` written as a mapping was written: (`inputs` or `outputs`,
    #: its name, its span).
    port_spans: tuple[tuple[str, str, SourceSpan], ...] = surface(Shape.INTERNAL, default=())
    #: What it asks of the tools beneath it (the requirements, then the hints, it wrote), the names of the other keys
    #: it carries that nothing reads, and every key it carries beyond its boundary, derived from `passthrough`.
    needs: tuple[Inherited, ...] = surface(Shape.INTERNAL, init=False, compare=False, repr=False, default=())
    unread: tuple[str, ...] = surface(Shape.INTERNAL, init=False, compare=False, repr=False, default=())
    carried: tuple[tuple[str, 'OpaqueCwl'], ...] = surface(
        Shape.INTERNAL, init=False, compare=False, repr=False, default=())

    def __post_init__(self) -> None:
        carried = tuple((key, value) for key, value in self.passthrough
                        if key not in ('inputs', 'outputs', '$namespaces', 'cwlVersion', 'class'))
        needs, unread = split(carried)
        object.__setattr__(self, 'needs', needs)
        object.__setattr__(self, 'unread', unread)
        object.__setattr__(self, 'carried', carried)


@dataclass(frozen=True, slots=True)
class Port:
    """An input or an output of a tool, under its name.

    `fields` is the CWL port it writes, kept as CWL: an input written as its type alone is its `type` and nothing
    more. An output that says where it is (`stdout`, `stderr` or a pattern, alone or as its `from:`) has that `line`,
    its runs of text and its inputs, and `fields` is what is written beside it.
    """

    name: str
    fields: tuple[tuple[str, OpaqueCwl], ...] = ()
    line: 'tuple[Word, ...] | None' = None
    span: SourceSpan | None = None

    @property
    def type(self) -> OpaqueCwl:
        """The type the port writes, if it writes one."""
        return dict(self.fields).get('type')


@dataclass(frozen=True, slots=True)
class Word:
    """A word of a tool's command line, or a part of an output line: text, or the value of one input of the tool
    (`{name}`). `quoted` is a word of text written between quotes, which is never read as a flag."""

    text: str = ''
    input: Port | None = None
    quoted: bool = False
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class ToolDocument:  # pylint: disable=too-many-instance-attributes
    """A parsed tool: a document, or a step's inline `run:` body, that says what runs with `command:` or `script:`,
    as one that says `steps:` is a workflow.

    Each of its own keys is read into what it says, and Python builds the same node from its objects; `passthrough`
    holds every other key, CWL that stays beside the lines the tool is read from (`sophios.lang.wic_tool`) and
    written with (`sophios.cwl.lowering`).
    """

    #: The words of `command:`, each text or one input; None when the tool says no command.
    command: tuple[Word, ...] | None = surface(Shape.TOOL_VALUE, 'command', default=None)
    script: str | None = surface(Shape.TOOL_VALUE, 'script', default=None)
    inputs: tuple[Port, ...] = surface(Shape.TOOL_VALUE, 'inputs', default=())
    outputs: tuple[Port, ...] = surface(Shape.TOOL_VALUE, 'outputs', default=())
    docker: str | None = surface(Shape.TOOL_VALUE, 'docker', default=None)
    #: `cores` and `ram`, as written: `ram` may be a size (`64G`).
    needs: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.TOOL_VALUE, 'needs', default=())
    sidecar: WicSidecar | None = surface(Shape.SIDECAR, 'wic', default=None)
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = surface(Shape.PASSTHROUGH, default=())
    span: SourceSpan | None = surface(Shape.INTERNAL, default=None)
    #: Where each key is written: (the key, its span).
    keys: tuple[tuple[str, SourceSpan], ...] = surface(Shape.INTERNAL, default=())

    def at(self, key: str) -> SourceSpan | None:
        """Where `key` is written; else where the tool is."""
        return dict(self.keys).get(key, self.span)
