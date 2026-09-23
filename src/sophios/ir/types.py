"""The types a compiled workflow is made of.

Each names something the compiler already manipulates and spells as a string or
threads through a call stack, so the checker can verify what it means rather
than only what it computes.

Frozen and slotted, as `sophios.lang.nodes` is, and holding no mutable
container that any invariant depends on: a check in `__post_init__` is worth
having only if it cannot be invalidated afterwards. An `OpaqueCwl` payload may
still be a `list` or a `dict` -- nothing reads one, which is the point of the
type, so nothing can be invalidated through it.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from ..lang.nodes import InputValue, OpaqueCwl
from ..lang.spans import SourceSpan

#: How namespaces are joined when a port identity is flattened for emission.
#: Structured identities are carried through the phases and joined only here,
#: so this is the one place the legacy spelling exists.
NAMESPACE_SEPARATOR: Final = '___'


class Direction(StrEnum):
    """Which side of a step a port is on.

    Part of a port's identity because CWL puts inputs and outputs in separate
    namespaces: a tool may declare `file` on both, and an identity without this
    makes the two compare equal and an edge between them look like a self-loop.
    """

    INPUT = 'input'
    OUTPUT = 'output'


@dataclass(frozen=True, slots=True)
class Namespace:
    """Where a step sits in the nesting of subworkflows: a path, outermost
    first. The compiler spells it joined and splits it back; holding the parts
    gives the splitting one home."""

    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject an empty part. A part containing the separator is *allowed*:
        a workflow may legitimately be named that way, and refusing it here
        would narrow the language to suit the emitted spelling."""
        for part in self.parts:
            if not part:
                raise ValueError('a namespace part cannot be empty')

    def child(self, name: str) -> 'Namespace':
        """This namespace with `name` appended.

        Args:
            name (str): The step or subworkflow to descend into.

        Returns:
            Namespace: The nested namespace.
        """
        return Namespace(self.parts + (name,))

    def flatten(self) -> str:
        """The joined spelling the emitted document carries.

        Returns:
            str: The parts joined by the separator.
        """
        return NAMESPACE_SEPARATOR.join(self.parts)


@dataclass(frozen=True, slots=True, order=True)
class RegistryKey:
    """A process name in one plugin namespace.

    An identity, so it lives with the others. A step's resolved process is
    recorded as this pair rather than as a joined string, because every reader
    of a joined string has to take it apart again -- and a name split back out
    of text is a guess where the pair is a fact.
    """

    namespace: str
    name: str


@dataclass(frozen=True, slots=True)
class StepId:
    """One *occurrence* of a step, which is not the same as the tool it runs.

    `docs/tutorials/append_twice.wic` invokes `append` twice, and sequence-form
    `steps:` exists so that it can. `name` is the authored id and repeats with
    the tool; `index` is the occurrence, and the pair is the identity -- which
    is what the compiler already means by `{stem}__step__{i}__{key}`.
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


@dataclass(frozen=True, slots=True)
class PortId:
    """A port's identity: which step occurrence, which side, and which port."""

    step: StepId
    direction: Direction
    port: str

    def __post_init__(self) -> None:
        """Reject a port with no name."""
        if not self.port:
            raise ValueError('a port must have a name')


@dataclass(frozen=True, slots=True)
class PortType:
    """What a port carries, in the algebra the compiler reasons within.

    `declared` keeps the CWL verbatim, because passthrough is open and whatever
    was given must be emitted. The fields beside it are the part Sophios
    interprets. A type it does not interpret has `declared` and nothing else --
    the leak boundary as a type rather than a convention.
    """

    declared: OpaqueCwl
    optional: bool = False
    array_depth: int = 0
    union: tuple['PortType', ...] = ()

    def __post_init__(self) -> None:
        """Reject a depth that cannot describe a real type."""
        if self.array_depth < 0:
            raise ValueError('array_depth counts wrappers and cannot be negative')


@dataclass(frozen=True, slots=True)
class PortDeclaration:  # pylint: disable=too-many-instance-attributes
    """The complete declaration of one process or workflow port.

    ``PortType`` is the deliberately small algebra later phases may reason
    about.  The other fields are emission facts: they preserve declarations
    that affect CWL without inviting Link or Infer to interpret arbitrary CWL.
    ``has_default`` distinguishes an authored ``default: null`` from no
    default at all.
    """

    type: PortType
    format: OpaqueCwl = None
    has_format: bool = False
    default: OpaqueCwl = None
    has_default: bool = False
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    field_order: tuple[str, ...] = ('type',)
    shorthand: bool = False

    def __post_init__(self) -> None:
        if len(self.field_order) != len(set(self.field_order)):
            raise ValueError('a port declaration field order cannot repeat a field')


@dataclass(frozen=True, slots=True)
class WorkflowPort:
    """A port on the workflow boundary, including its CWL declaration."""

    name: str
    declaration: PortDeclaration
    output_source: OpaqueCwl = None
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

    name: str
    value: OpaqueCwl

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('a job binding must be named')


@dataclass(frozen=True, slots=True)
class Source:
    """A step input bound to a name the emitted document defines.

    `shorthand` is CWL's spelling choice and nothing more, the same
    distinction `PortDeclaration.shorthand` records: `in: {x: src}` and
    `in: {x: {source: src}}` mean one thing and are written two ways.
    """

    name: str
    shorthand: bool = False


@dataclass(frozen=True, slots=True)
class Expression:
    """Raw CWL the document supplied for a binding, emitted verbatim."""

    text: str


#: What a step's `in:` entry can be. Three shapes, and the set is closed: a
#: value that is neither is not something CWL has a field for, so the phases
#: cannot express one and Emit cannot be handed one. Authored values are
#: `InputValue`, and the two unions not meeting is the whole point.
EmittedValue: TypeAlias = Source | Expression


@dataclass(frozen=True, slots=True)
class ProcessRun:
    """What a step executes.

    ``target`` is transported exactly as CWL: normally a relative path, but an
    inline process object is legal too.  ``process_id`` is the resolved logical
    identity; it is separate because a path is an embedding choice, not a tool
    identity.  A child graph records a resolved subworkflow without hiding its
    emitted CWL in an opaque value.
    """

    target: OpaqueCwl
    process_id: RegistryKey
    child: 'WorkflowGraph | None' = None

    def __post_init__(self) -> None:
        if not self.process_id.name:
            raise ValueError('a resolved process must have an identity')


@dataclass(frozen=True, slots=True)
class StepEmission:  # pylint: disable=too-many-instance-attributes
    """The CWL surface of a step after semantic phases have finished.

    Known fields are named.  ``passthrough`` is only the open CWL residue, and
    ``field_order`` records canonical byte order without storing a completed
    step dictionary.  Emit is the only phase allowed to traverse the payloads.
    """

    id: str
    inputs: tuple[tuple[str, EmittedValue], ...]
    run: ProcessRun
    outputs: tuple[OpaqueCwl, ...]
    scatter: OpaqueCwl = None
    scatter_method: OpaqueCwl = None
    when: OpaqueCwl = None
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    field_order: tuple[str, ...] = ('id', 'in', 'run', 'out')

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError('an emitted step must have an id')
        if len(self.field_order) != len(set(self.field_order)):
            raise ValueError('an emitted step field order cannot repeat a field')


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


@dataclass(frozen=True, slots=True)
class Edge:
    """A value flowing from one port to another.

    Between *ports*, not steps: two edges into one step are different bindings,
    and a step pair would lose which input each satisfies.
    """

    source: PortId
    sink: PortId
    span: SourceSpan | None = None

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
    """An input whose producer is not in this document.

    A subworkflow may bind an input to something a parent supplies. The compiler
    carries that as an intermediate input made on the way down and satisfied as
    the recursion unwinds (docs/dev/algorithms.md); naming it lets `Link` say
    whether every one was discharged.
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
    """One authored input binding, and what it resolved to.

    Every `in:` entry becomes one of these, whatever was written. Keeping only
    the edges would make two documents differing solely in a literal lower to
    the same graph, so `Emit` would have to read the AST again to tell them
    apart -- and a graph that cannot reproduce its own document is not the
    document's meaning.
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
    """A step occurrence, with the ports it exposes and what its inputs bind to.

    `interpreted` holds the CWL keys Sophios acts upon and `passthrough` the
    rest -- the split the AST already makes, carried forward, not redrawn.
    """

    id: StepId
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    bindings: tuple[Binding, ...] = ()
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None
    emission: StepEmission | None = None
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
PortMapping: TypeAlias = tuple[tuple[str, PortId], ...]
InputMapping: TypeAlias = tuple[tuple[str, tuple[PortId, ...]], ...]


@dataclass(frozen=True, slots=True)
class WorkflowGraph:  # pylint: disable=too-many-instance-attributes
    """A whole workflow: its steps, what their inputs bind to, and what it owes.

    The four mappings are fields, not arguments. Threaded through a call stack
    they are state every function must be handed and can quietly disagree about.
    """

    namespace: Namespace
    steps: tuple[StepNode, ...] = ()
    explicit_edge_defs: PortMapping = ()
    explicit_edge_calls: PortMapping = ()
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
    composition_edges: tuple[Edge, ...] = ()
    inferred_edges: tuple[Edge, ...] = ()
    discharged_obligations: tuple[PortId, ...] = ()
    field_order: tuple[str, ...] = ('steps', 'cwlVersion', 'class', '$namespaces', '$schemas',
                                    'inputs', 'sophios:lang_version', 'outputs')

    def __post_init__(self) -> None:
        """Reject a graph naming a port no step declares, anywhere.

        Checked over every reference the graph holds -- edges, obligations and
        all four mappings -- rather than over the edges alone. An identity that
        resolves to nothing is the same defect wherever it is stored, and it
        reaches emission as a dangling `source:`, which CWL accepts and a runner
        then fails on.
        """
        occurrences = [step.id for step in self.steps]
        if len(occurrences) != len(set(occurrences)):
            raise ValueError('a step occurrence appears twice')
        for step in self.steps:
            if step.id.namespace != self.namespace:
                raise ValueError(
                    f'{step.id} sits in {step.id.namespace.parts}, not this graph\'s '
                    f'{self.namespace.parts}')
        if len(self.field_order) != len(set(self.field_order)):
            raise ValueError('a workflow field order cannot repeat a field')

        known = {port.id for step in self.steps for port in step.inputs + step.outputs}
        recursive_known = self.port_ids
        recursive_wheres = {'an edge source', 'an output mapping'}
        for where, port_id in self._references():
            # An input mapping relays a boundary name down to its consuming
            # port exactly as an output mapping relays one up from its
            # producer (`_redirect_output_mappings`): `_direct_sink` walks
            # that relay one hop at a time through each ancestor's own
            # `input_mapping`, so an entry several levels above the sink
            # legitimately names a port outside this graph's own steps, the
            # same way an output mapping already may.
            recursive = where in recursive_wheres or where.startswith('input mapping ')
            allowed = recursive_known if recursive else known
            if port_id not in allowed:
                raise ValueError(f'{where} names a port no step declares: {port_id}')
        for edge in self.composition_edges:
            if edge.source not in recursive_known or edge.sink not in recursive_known:
                raise ValueError(f'a composition edge names a port outside this graph tree: {edge}')
        for edge in self.inferred_edges:
            if edge.source not in recursive_known or edge.sink not in recursive_known:
                raise ValueError(f'an inferred edge names a port outside this graph tree: {edge}')
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
        for name, port_ids in self.input_mapping:
            found.extend((f'input mapping {name!r}', port_id) for port_id in port_ids)
        return tuple(found)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """The edges this graph owns: its own bindings, and what Link placed here.

        Local, not tree-wide. A child's edge belongs to the child, whose
        indices name nothing at this level -- so a consumer that wants the
        whole tree asks for it by name, as it does for `all_steps`.
        """
        local = tuple(b.resolution for s in self.steps for b in s.bindings
                      if isinstance(b.resolution, Edge))
        return local + self.composition_edges + self.inferred_edges

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
