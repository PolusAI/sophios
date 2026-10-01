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

    def __post_init__(self) -> None:
        """Reject an occurrence that names nothing or sits nowhere."""
        if not self.name:
            raise ValueError('a step must be named')
        if self.index < 1:
            raise ValueError(f'a step occurrence is 1-based, not {self.index}')


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


#: What a port or workflow boundary is called: written, or derived from one.
PortName: TypeAlias = AuthoredName | DerivedName


@dataclass(frozen=True, slots=True)
class PortId:
    """A port's identity: which step occurrence, which side, and which port."""

    step: StepId
    direction: Direction
    port: PortName

    def __post_init__(self) -> None:
        """Reject a port with no name."""
        if not self.port:
            raise ValueError('a port must have a name')


@dataclass(frozen=True, slots=True)
class PortType:
    """What a port carries. `declared` keeps the CWL verbatim, for Emit;
    `canonical` is the one parse of it every other reader uses: CWL's `T?` and
    `T[]` shorthand expanded, through array items and every union member, so
    `string?[]` is an array of nullable strings and not a nullable array, and
    `[File?, string]` accepts null in either spelling of that member.
    `stdout` and `stderr` canonicalise to `File`.
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
    if raw in ('stdout', 'stderr'):
        # A tool's own shorthand for "the captured stream, as a File". Only a
        # CommandLineTool output may say it; a workflow boundary promoted from
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
    #: The port this one was derived from, when its name was built by joining a
    #: step id to a port name rather than written by hand. Recorded because the
    #: two halves are facts here and a guess once they are one string.
    origin: PortId | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('a workflow port must be named')


@dataclass(frozen=True, slots=True)
class JobBinding:
    """One concrete value in the job input document projected from a graph."""

    name: PortName
    value: OpaqueCwl

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('a job binding must be named')


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


#: What a step's `in:` entry can be: a closed set of the shapes CWL has a
#: field for, distinct from the authored `InputValue` union.
EmittedValue: TypeAlias = Source | Expression


@dataclass(frozen=True, slots=True)
class ProcessRun:
    """What a step executes: `target` is transported exactly as CWL (usually a
    relative path, but an inline process object is legal), and `process_id` is
    the resolved logical identity, kept separate since a path is an embedding
    choice, not a tool identity.
    """

    target: OpaqueCwl
    process_id: RegistryKey
    child: 'WorkflowGraph | None' = None

    def __post_init__(self) -> None:
        if not self.process_id.name:
            raise ValueError('a resolved process must have an identity')


@dataclass(frozen=True, slots=True)
class Port:
    """One port of one step: its identity, its type, and where it was written."""

    id: PortId
    type: PortType
    declaration: PortDeclaration | None = None
    span: SourceSpan | None = None
    #: The port this one re-exports, when a parent exposed a child's interface
    #: under a joined name. `None` for a port a process declares itself.
    origin: PortId | None = None


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

    def __post_init__(self) -> None:
        """Reject an edge that does not run from an output to an input."""
        if self.source == self.sink:
            raise ValueError(f'an edge cannot join a port to itself: {self.source}')
        if self.source.direction is not Direction.OUTPUT:
            raise ValueError(f'an edge must leave an output, not {self.source.direction}')
        if self.sink.direction is not Direction.INPUT:
            raise ValueError(f'an edge must arrive at an input, not {self.sink.direction}')


@dataclass(frozen=True, slots=True)
class DeferredObligation:
    """An input whose producer is not in this document, satisfied as the
    compiler's recursion unwinds; naming it lets `Link` say whether every one
    was discharged.
    """

    sink: PortId
    name: str
    type: PortType
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        """Reject an obligation with nothing to satisfy."""
        if not self.name:
            raise ValueError('a deferred obligation must name what it awaits')
        if self.sink.direction is not Direction.INPUT:
            raise ValueError('only an input can await a value')


#: What a binding resolved to, or None when the value needs no producer -- a
#: literal, a workflow parameter, a raw CWL reference. `Binding.value` says
#: which of those it is; this says where it comes from.
Resolution: TypeAlias = Edge | DeferredObligation | None


@dataclass(frozen=True, slots=True)
class Binding:
    """One authored input binding, and what it resolved to; every `in:` entry
    becomes one of these, whatever was written, so the graph reproduces the
    document rather than only its edges.
    """

    sink: PortId
    value: InputValue
    resolution: Resolution = None

    def __post_init__(self) -> None:
        """Reject a resolution attached to the wrong port."""
        if self.resolution is not None and self.resolution.sink != self.sink:
            raise ValueError(f'binding for {self.sink} resolved against {self.resolution.sink}')


@dataclass(frozen=True, slots=True)
class StepNode:  # pylint: disable=too-many-instance-attributes
    """A step occurrence, with the ports it exposes and what its inputs bind
    to. `interpreted` holds the CWL keys Sophios acts upon (including
    `scatter`, `scatterMethod` and `when`, read verbatim by `emit`) and
    `passthrough` the rest.
    """

    id: StepId
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    bindings: tuple[Binding, ...] = ()
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    #: What this step executes; `None` only before Lower or Infer attaches it.
    run: ProcessRun | None = None
    #: The ports `interpreted['scatter']` names, resolved where it is read.
    scatter_ports: tuple[PortName, ...] = ()
    inference_rules: tuple[tuple[str, str], ...] = ()
    synthesized: bool = False

    def __post_init__(self) -> None:
        """Reject ports or bindings that belong to another step.

        `Edge` cannot see this: by the time it holds a port, the port is only an
        identity, so a port carrying another occurrence's id would build an edge
        into a node that does not exist.
        """
        for port in self.inputs:
            if port.id.step != self.id or port.id.direction is not Direction.INPUT:
                raise ValueError(f'{port.id} is not an input of {self.id}')
        for port in self.outputs:
            if port.id.step != self.id or port.id.direction is not Direction.OUTPUT:
                raise ValueError(f'{port.id} is not an output of {self.id}')
        declared = {port.id for port in self.inputs}
        for binding in self.bindings:
            if binding.sink not in declared:
                raise ValueError(f'{binding.sink} is bound but not declared by {self.id}')


#: The four mappings the compiler threads through its recursion, as pairs
#: rather than dicts: a frozen graph holding a mutable mapping can have its
#: invariants invalidated after the constructor has checked them.
#: `explicit_edge_*` are keyed by authored edge labels; the other two by the
#: boundary name a port is exposed under.
EdgeMapping: TypeAlias = tuple[tuple[str, PortId], ...]
PortMapping: TypeAlias = tuple[tuple[PortName, PortId], ...]
InputMapping: TypeAlias = tuple[tuple[PortName, tuple[PortId, ...]], ...]


@dataclass(frozen=True, slots=True)
class WorkflowGraph:  # pylint: disable=too-many-instance-attributes
    """A whole workflow: its steps, what their inputs bind to, and what it owes.

    The four mappings are fields, not arguments. Threaded through a call stack
    they are state every function must be handed and can quietly disagree about.
    """

    namespace: Namespace
    steps: tuple[StepNode, ...] = ()
    explicit_edge_defs: EdgeMapping = ()
    explicit_edge_calls: EdgeMapping = ()
    input_mapping: InputMapping = ()
    output_mapping: PortMapping = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    name: str = ''
    lang_version: str = ''
    cwl_version: str = ''
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
    discharged_obligations: tuple[PortId, ...] = ()
    #: `input_mapping` names emitted in shorthand: a step's own lifted input,
    #: recorded where Complete or Infer creates it (a name cannot tell).
    shorthand_relays: tuple[PortName, ...] = ()

    def __post_init__(self) -> None:
        """Reject a graph naming a port no step declares, checked over every
        reference the graph holds, not only its edges.
        """
        occurrences = [step.id for step in self.steps]
        if len(occurrences) != len(set(occurrences)):
            raise ValueError('a step occurrence appears twice')
        for step in self.steps:
            if step.id.namespace != self.namespace:
                raise ValueError(
                    f'{step.id} sits in {step.id.namespace.parts}, not this graph\'s '
                    f'{self.namespace.parts}')

        known = {port.id for step in self.steps for port in step.inputs + step.outputs}
        recursive_known = self.port_ids
        recursive_wheres = {'an edge source', 'an output mapping'}
        for where, port_id in self._references():
            # An input mapping may legitimately relay a boundary name to a
            # sink several levels below it, outside this graph's own steps,
            # the same way an output mapping already may.
            recursive = where in recursive_wheres or where.startswith('input mapping ')
            allowed = recursive_known if recursive else known
            if port_id not in allowed:
                raise ValueError(f'{where} names a port no step declares: {port_id}')
        for edge in self.linked_edges:
            if edge.source not in recursive_known or edge.sink not in recursive_known:
                raise ValueError(f'a linked edge names a port outside this graph tree: {edge}')
        for sink in self.discharged_obligations:
            if sink not in recursive_known:
                raise ValueError(f'a discharged obligation names no port in this graph tree: {sink}')
        recursive_steps = [step.id for step in self.all_steps]
        if len(recursive_steps) != len(set(recursive_steps)):
            raise ValueError('step identities must be injective across a composed graph')

    def _references(self) -> tuple[tuple[str, PortId], ...]:
        """Every port identity this graph holds, with where it came from."""
        found: list[tuple[str, PortId]] = []
        for step in self.steps:
            for binding in step.bindings:
                found.append(('a binding', binding.sink))
                if isinstance(binding.resolution, Edge):
                    found.append(('an edge source', binding.resolution.source))
                elif isinstance(binding.resolution, DeferredObligation):
                    found.append(('an obligation', binding.resolution.sink))
        for name, port_id in (*self.explicit_edge_defs, *self.explicit_edge_calls):
            found.append((f'mapping {name!r}', port_id))
        for _name, port_id in self.output_mapping:
            found.append(('an output mapping', port_id))
        for boundary, port_ids in self.input_mapping:
            found.extend((f'input mapping {boundary!r}', port_id) for port_id in port_ids)
        return tuple(found)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """The edges this graph owns: its own bindings, and what Link or
        Infer placed here. Local, not tree-wide.
        """
        local = tuple(b.resolution for s in self.steps for b in s.bindings
                      if isinstance(b.resolution, Edge))
        return local + self.linked_edges

    @property
    def obligations(self) -> tuple[DeferredObligation, ...]:
        """Every binding this document cannot satisfy on its own."""
        local = tuple(b.resolution for s in self.steps for b in s.bindings
                      if isinstance(b.resolution, DeferredObligation))
        nested = tuple(obligation for child in self.children for obligation in child.obligations)
        discharged = set(self.discharged_obligations)
        return tuple(obligation for obligation in local + nested
                     if obligation.sink not in discharged)

    @property
    def all_steps(self) -> tuple[StepNode, ...]:
        """Every step in this graph tree, preserving authored traversal order."""
        return self.steps + tuple(step for child in self.children for step in child.all_steps)

    @property
    def port_ids(self) -> frozenset[PortId]:
        """Every port identity in this graph tree."""
        return frozenset(port.id for step in self.all_steps for port in step.inputs + step.outputs)

    def step(self, name: str) -> StepNode | None:
        """The first occurrence called `name`, or None.

        Args:
            name (str): The step's authored id, which may repeat.

        Returns:
            StepNode | None: The first occurrence, if this graph has one.
        """
        return next((s for s in self.steps if s.id.name == name), None)


#: A `WorkflowGraph` that states a whole CWL document: both versions set and
#: every step carrying a `run`. Distinct from `WorkflowGraph` so `emit` can
#: ask for one; a graph between phases satisfies neither. Only
#: `emit.surface` produces one.
EmissionDocument = NewType('EmissionDocument', WorkflowGraph)
