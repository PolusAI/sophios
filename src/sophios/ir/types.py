"""The types a compiled workflow is made of.

Each names something the compiler already manipulates and spells as a string or
threads through a call stack, so the checker can verify what it means rather
than only what it computes.

Frozen and slotted, as `sophios.lang.nodes` is, and holding no mutable
container: an invariant checked in `__post_init__` is worth having only if it
cannot be invalidated afterwards.
"""
from dataclasses import dataclass, field
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
class Port:
    """One port of one step: its identity, its type, and where it was written."""

    id: PortId
    type: PortType
    span: SourceSpan | None = None


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
class StepNode:
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
class WorkflowGraph:
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

        known = {port.id for step in self.steps for port in step.inputs + step.outputs}
        for where, port_id in self._references():
            if port_id not in known:
                raise ValueError(f'{where} names a port no step declares: {port_id}')

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
        for name, port_id in (*self.explicit_edge_defs, *self.explicit_edge_calls, *self.output_mapping):
            found.append((f'mapping {name!r}', port_id))
        for name, port_ids in self.input_mapping:
            found.extend((f'input mapping {name!r}', port_id) for port_id in port_ids)
        return tuple(found)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """Every resolved edge, derived from the bindings that produced them."""
        return tuple(b.resolution for s in self.steps for b in s.bindings
                     if isinstance(b.resolution, Edge))

    @property
    def obligations(self) -> tuple[DeferredObligation, ...]:
        """Every binding this document cannot satisfy on its own."""
        return tuple(b.resolution for s in self.steps for b in s.bindings
                     if isinstance(b.resolution, DeferredObligation))

    def step(self, name: str) -> StepNode | None:
        """The first occurrence called `name`, or None.

        Args:
            name (str): The step's authored id, which may repeat.

        Returns:
            StepNode | None: The first occurrence, if this graph has one.
        """
        return next((s for s in self.steps if s.id.name == name), None)
