"""The types a compiled workflow is made of.

Each names something the compiler already manipulates, so the checker can
verify what it means rather than only what it computes. Frozen and slotted,
holding no mutable container that any invariant depends on: a check in
`__post_init__` is worth having only if it cannot be invalidated afterwards.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType, TypeAlias

from ..lang.diagnostics import Diagnostics
from ..lang.nodes import InputValue, OpaqueCwl
from ..lang.spans import SourceSpan


class Direction(StrEnum):
    """Which side of a step a port is on; part of a port's identity because
    CWL puts inputs and outputs in separate namespaces.
    """

    INPUT = 'input'
    OUTPUT = 'output'


@dataclass(frozen=True, slots=True)
class Namespace:
    """Where a step sits in the nesting of subworkflows: the step occurrences
    that enclose it, outermost first."""

    parts: tuple['StepId', ...] = ()

    def child(self, step: 'StepId') -> 'Namespace':
        """This namespace with `step` appended.

        Args:
            step (StepId): The subworkflow call to descend into.

        Returns:
            Namespace: The nested namespace.
        """
        return Namespace(self.parts + (step,))


@dataclass(frozen=True, slots=True, order=True)
class RegistryKey:
    """A process name in one plugin namespace, kept as a pair rather than a
    joined string so no reader has to take it apart again.
    """

    namespace: str
    name: str


@dataclass(frozen=True, slots=True)
class StepId:
    """One *occurrence* of a step, which is not the same as the tool it runs:
    `name` is the authored id and may repeat; `index` plus `name` is the identity.
    """

    namespace: Namespace
    index: int
    name: str
    #: `hash((namespace, index, name))`, computed once. A namespace holds the step
    #: ids that enclose it and each of those holds its own namespace, so hashing
    #: on demand re-walks the whole chain and the cost doubles per level.
    _hash: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, '_hash', hash((self.namespace, self.index, self.name)))

    def __hash__(self) -> int:
        return self._hash


#: A name as its author wrote it: a tool's port, a workflow input, a step key.
#: Distinct from `str` so that a rendered name cannot stand in for one -- the
#: checker refuses a plain string where an identity is expected.
AuthoredName = NewType('AuthoredName', str)


@dataclass(frozen=True, slots=True)
class DerivedName:
    """A name the compiler makes by exposing `port` of `step` one level up;
    `ir.names` alone decides how it is written.
    """

    step: StepId
    port: 'PortName'
    #: `hash((step, port))`, computed once: `port` may itself be derived, so
    #: hashing on demand walks every level the name was exposed through.
    _hash: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, '_hash', hash((self.step, self.port)))

    def __hash__(self) -> int:
        return self._hash


#: What a port or workflow boundary is called: written, or derived from one.
PortName: TypeAlias = AuthoredName | DerivedName


@dataclass(frozen=True, slots=True)
class PortId:
    """A port's identity: which step occurrence, which side, and which port."""

    step: StepId
    direction: Direction
    port: PortName

    def __post_init__(self) -> None:
        """Reject a port with no name, which a tool may declare."""
        if not self.port:
            raise ValueError('a port must have a name')


@dataclass(frozen=True, slots=True)
class PortType:
    """What a port carries. `declared` keeps the CWL verbatim, for Emit;
    `canonical` is the one parse of it every other reader uses: CWL's `T?` and
    `T[]` shorthand expanded, through array items and every union member, so
    `string?[]` is an array of nullable strings and not a nullable array, and
    `[File?, string]` accepts null in either spelling of that member.
    `stdin`, `stdout` and `stderr` canonicalise to `File`.
    """

    declared: OpaqueCwl
    canonical: OpaqueCwl = field(init=False)

    def __post_init__(self) -> None:
        """Parse `declared` once, here, so no port type exists unparsed."""
        object.__setattr__(self, 'canonical', _canonical(deepcopy(self.declared)))

    @property
    def optional(self) -> bool:
        """Whether the port accepts `null` itself, not merely in its items."""
        return isinstance(self.canonical, list) and 'null' in self.canonical


def _canonical(raw: OpaqueCwl) -> OpaqueCwl:
    """Expand `T?` and `T[]` wherever they are written, including inside a member."""
    if raw in ('stdin', 'stdout', 'stderr'):
        # A tool's own shorthand for "the stream, as a File". Only a
        # CommandLineTool port may say it; a workflow boundary promoted from
        # one must say `File`, which is what cwltool makes of it.
        return 'File'
    if isinstance(raw, str) and raw.endswith('?'):
        return ['null', _canonical(raw[:-1])]
    if isinstance(raw, str) and raw.endswith('[]'):
        return {'type': 'array', 'items': _canonical(raw[:-2])}
    if isinstance(raw, dict) and raw.get('type') == 'array' and 'items' in raw:
        return {**raw, 'items': _canonical(raw['items'])}
    if isinstance(raw, list):
        # A member's own shorthand, expanded: `null` once, first; no duplicates.
        members: list[OpaqueCwl] = []
        for item in raw:
            expanded = _canonical(item)
            for member in expanded if isinstance(expanded, list) else [expanded]:
                if member not in members:
                    members.append(member)
        return ['null'] * ('null' in members) + [item for item in members if item != 'null']
    return raw


@dataclass(frozen=True, slots=True)
class PortDeclaration:  # pylint: disable=too-many-instance-attributes
    """The complete declaration of one process or workflow port. The fields
    beside `type` preserve emission facts without inviting Link or Infer to
    interpret arbitrary CWL; `has_default` distinguishes an authored
    `default: null` from no default at all.
    """

    type: PortType
    format: OpaqueCwl = None
    has_format: bool = False
    default: OpaqueCwl = None
    has_default: bool = False
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    shorthand: bool = False


#: A `PortDeclaration` reduced to what a workflow boundary may state. Only
#: `declarations.boundary_declaration` produces one.
BoundaryDeclaration = NewType('BoundaryDeclaration', PortDeclaration)


@dataclass(frozen=True, slots=True)
class WorkflowPort:
    """A port on the workflow boundary, including its CWL declaration."""

    name: PortName
    declaration: BoundaryDeclaration
    #: A resolved producer, or the authored text when Link could not resolve it.
    output_source: 'StepOutputRef | OpaqueCwl' = None
    has_output_source: bool = False
    #: Where the port was written, when it was.
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class JobBinding:
    """One concrete value in the job input document projected from a graph."""

    name: PortName
    value: OpaqueCwl


@dataclass(frozen=True, slots=True)
class StepOutputRef:
    """An output of a step in the same document: CWL's `step/port`."""

    step: StepId
    port: PortName


@dataclass(frozen=True, slots=True)
class Source:
    """A step input bound to a workflow input or to a step's output.

    `shorthand` is CWL's spelling choice and nothing more, the same
    distinction `PortDeclaration.shorthand` records: `in: {x: src}` and
    `in: {x: {source: src}}` mean one thing and are written two ways.
    """

    ref: PortName | StepOutputRef
    shorthand: bool = False


@dataclass(frozen=True, slots=True)
class Expression:
    """Raw CWL the document supplied for a binding, emitted verbatim."""

    text: str


@dataclass(frozen=True, slots=True)
class Record:
    """A step input spelled as CWL's WorkflowStepInput: its resolved sources, in
    the authored order, and the other fields verbatim."""

    sources: tuple['PortName | StepOutputRef', ...]
    fields: tuple[tuple[str, OpaqueCwl], ...]


#: What a step's `in:` entry can be: a closed set of the shapes CWL has a
#: field for, distinct from the authored `InputValue` union.
EmittedValue: TypeAlias = Source | Expression | Record


@dataclass(frozen=True, slots=True)
class Port:
    """One port of one step: its identity, its type, and where it was written."""

    id: PortId
    type: PortType
    declaration: PortDeclaration | None = None
    span: SourceSpan | None = None


class EdgeOrigin(StrEnum):
    """How an edge stored on `WorkflowGraph` was placed there. `stepin`
    reads this to decide `shorthand`: an inferred edge was never authored,
    so it must emit as one; a composed edge stands for something the
    document itself said, so it must not.
    """

    COMPOSED = 'composed'
    INFERRED = 'inferred'


@dataclass(frozen=True, slots=True)
class Edge:
    """A value flowing from one port to another.

    Between *ports*, not steps: two edges into one step are different bindings,
    and a step pair would lose which input each satisfies.

    `origin` is `None` for an edge that lives in `Binding.resolution`: a
    binding is already provenance, an authored `in:` entry, so nothing reads
    its origin.
    """

    source: PortId
    sink: PortId
    span: SourceSpan | None = None
    origin: EdgeOrigin | None = None


@dataclass(frozen=True, slots=True)
class DeferredObligation:
    """An input whose producer is not in this document, satisfied as the
    compiler's recursion unwinds; naming it lets `Link` say whether every one
    was discharged.
    """

    sink: PortId
    name: str
    span: SourceSpan | None = None


#: What a binding resolved to, or None when the value needs no producer -- a
#: literal, a workflow parameter, a raw CWL reference. `Binding.value` says
#: which of those it is; this says where it comes from. A step-input record
#: resolves to a tuple, what each `source` entry resolved to, in order: an edge,
#: an obligation, or None for a workflow input relayed by name.
Resolution: TypeAlias = Edge | DeferredObligation | tuple[Edge | DeferredObligation | None, ...] | None


@dataclass(frozen=True, slots=True)
class Binding:
    """One authored input binding, and what it resolved to; every `in:` entry
    becomes one of these, whatever was written, so the graph reproduces the
    document rather than only its edges.
    """

    sink: PortId
    value: InputValue
    resolution: Resolution = None


@dataclass(frozen=True, slots=True)
class StepNode:  # pylint: disable=too-many-instance-attributes
    """A step occurrence, with the ports it exposes and what its inputs bind
    to. `interpreted` holds the CWL keys Sophios acts upon (including
    `scatter`, `scatterMethod` and `when`, read verbatim by `emit`) and
    `passthrough` the rest.
    """

    id: StepId
    #: The process this step executes, by its resolved identity. The file it is written to is an embedding
    #: choice Emit makes, and the graph of a called workflow is this step's `WorkflowGraph.child`.
    run: RegistryKey
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    bindings: tuple[Binding, ...] = ()
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    #: The ports `interpreted['scatter']` names, resolved where it is read.
    scatter_ports: tuple[PortName, ...] = ()
    inference_rules: tuple[tuple[str, str], ...] = ()
    synthesized: bool = False


#: The three mappings the compiler threads through its recursion, as pairs
#: rather than dicts: a frozen graph holding a mutable mapping can have its
#: invariants invalidated after the constructor has checked them.
#: `explicit_edge_defs` is keyed by authored edge labels; the other two by the
#: boundary name a port is exposed under.
EdgeMapping: TypeAlias = tuple[tuple[str, PortId], ...]
PortMapping: TypeAlias = tuple[tuple[PortName, PortId], ...]
InputMapping: TypeAlias = tuple[tuple[PortName, tuple[PortId, ...]], ...]


@dataclass(frozen=True, slots=True)
class WorkflowGraph:  # pylint: disable=too-many-instance-attributes
    """A whole workflow: its steps, what their inputs bind to, and what it owes.

    The three mappings are fields, not arguments. Threaded through a call stack
    they are state every function must be handed and can quietly disagree about.
    """

    namespace: Namespace
    steps: tuple[StepNode, ...] = ()
    explicit_edge_defs: EdgeMapping = ()
    input_mapping: InputMapping = ()
    output_mapping: PortMapping = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    name: str = ''
    lang_version: str = ''
    workflow_inputs: tuple[WorkflowPort, ...] = ()
    workflow_outputs: tuple[WorkflowPort, ...] = ()
    job_bindings: tuple[JobBinding, ...] = ()
    requirements: tuple[tuple[str, OpaqueCwl], ...] = ()
    namespaces: tuple[tuple[str, OpaqueCwl], ...] = ()
    schemas: tuple[OpaqueCwl, ...] = ()
    children: tuple['WorkflowGraph', ...] = ()
    #: Every edge Link or Infer placed here (not a binding's own resolution),
    #: distinguished by `Edge.origin`.
    linked_edges: tuple[Edge, ...] = ()
    #: `input_mapping` names emitted in shorthand: a step's own lifted input,
    #: recorded where Complete or Infer creates it (a name cannot tell).
    shorthand_relays: tuple[PortName, ...] = ()
    #: False when the workflow is marked `wic: inlineable: false`, in its own file or
    #: at the call site (read from the child's sidecar once call-site keys are folded
    #: in): a caller asking to flatten its calls keeps this one nested.
    inlineable: bool = True

    def __post_init__(self) -> None:
        """Reject a graph naming a port no step of its tree declares, checked over every
        reference the graph holds, not only its edges.
        """
        for step in self.steps:
            if step.id.namespace != self.namespace:
                raise ValueError(
                    f'{step.id} sits in {step.id.namespace.parts}, not this graph\'s '
                    f'{self.namespace.parts}')
        known = self.port_ids
        for where, name, port_id in self._references():
            if port_id not in known:
                # Spelled only on failure: a derived name's repr prints every
                # step id it was exposed through, and each of those prints its
                # own namespace, so writing it out for every reference is the
                # cost that made nesting exponential.
                label = where if name is None else f'{where} {name!r}'
                raise ValueError(f'{label} names a port no step declares: {port_id}')
        recursive_steps = [step.id for step in self.all_steps]
        if len(recursive_steps) != len(set(recursive_steps)):
            raise ValueError('step identities must be injective across a composed graph')

    def _references(self) -> tuple[tuple[str, str | PortName | None, PortId], ...]:
        """Every port identity this graph holds, with where it came from and
        the name it is held under there, when it is held under one."""
        found: list[tuple[str, str | PortName | None, PortId]] = []
        for step in self.steps:
            for binding in step.bindings:
                found.append(('a binding', None, binding.sink))
        for edge in self.edges:
            found += [('an edge', None, edge.source), ('an edge', None, edge.sink)]
        for name, port_id in self.explicit_edge_defs:
            found.append(('mapping', name, port_id))
        for _name, port_id in self.output_mapping:
            found.append(('an output mapping', None, port_id))
        for boundary, port_ids in self.input_mapping:
            found.extend(('input mapping', boundary, port_id) for port_id in port_ids)
        return tuple(found)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """The edges this graph owns: its own bindings, and what Link or
        Infer placed here. Local, not tree-wide.
        """
        local = tuple(item for s in self.steps for b in s.bindings
                      for item in resolved_items(b.resolution) if isinstance(item, Edge))
        return local + self.linked_edges

    @property
    def obligations(self) -> tuple[DeferredObligation, ...]:
        """Every binding in this graph tree that its own document cannot satisfy."""
        local = tuple(item for s in self.steps for b in s.bindings
                      for item in resolved_items(b.resolution) if isinstance(item, DeferredObligation))
        return local + tuple(obligation for child in self.children for obligation in child.obligations)

    @property
    def all_steps(self) -> tuple[StepNode, ...]:
        """Every step in this graph tree, preserving authored traversal order."""
        return self.steps + tuple(step for child in self.children for step in child.all_steps)

    @property
    def port_ids(self) -> frozenset[PortId]:
        """Every port identity in this graph tree."""
        return frozenset(port.id for step in self.all_steps for port in step.inputs + step.outputs)

    def child(self, step: StepId) -> 'WorkflowGraph | None':
        """The graph of the workflow `step` calls, or None for a step that runs a tool.

        Args:
            step (StepId): A step of this graph.

        Returns:
            WorkflowGraph | None: The child whose namespace `step` opens.
        """
        return next((child for child in self.children if child.namespace.parts[-1] == step), None)


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """What the front end, Lower or Link hands on: a graph when it accepted its input, and every diagnostic
    either way."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics


def resolved_items(resolution: Resolution) -> tuple[Edge | DeferredObligation, ...]:
    """The edges and obligations one binding resolved to: none, one, or a record's several."""
    items = resolution if isinstance(resolution, tuple) else (resolution,)
    return tuple(item for item in items if item is not None)


#: A `WorkflowGraph` that states a whole CWL document. Distinct from
#: `WorkflowGraph` so `emit` can ask for one. Only `emit.surface` produces one.
EmissionDocument = NewType('EmissionDocument', WorkflowGraph)
