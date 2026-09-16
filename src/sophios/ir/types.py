"""The types a compiled workflow is made of.

Each names something the compiler already manipulates and currently spells as a
string or threads through a call stack: a namespace, a port's identity, a
step's place in a graph, an edge, and a reference whose producer is not in this
document. Giving them types is what lets the checker verify what the compiler
means rather than only what it computes.

Frozen and slotted throughout, matching `sophios.lang.nodes`. Construction
validates: an invariant enforced in `__post_init__` cannot be skipped by a
caller who forgot to run a checker.
"""
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from ..lang.nodes import OpaqueCwl
from ..lang.spans import SourceSpan

#: How namespaces are joined when a port identity is flattened for emission.
#: The compiler splits on this to recover the parts, so a namespace containing
#: it would be unrecoverable.
NAMESPACE_SEPARATOR: Final = '___'


@dataclass(frozen=True, slots=True)
class Namespace:
    """Where a step sits in the nesting of subworkflows.

    A path, outermost first. The compiler spells this as a joined string and
    splits it back apart when it needs the parts; holding the parts means the
    splitting has one home and a namespace that cannot round-trip cannot be
    built.
    """

    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a part that would not survive being joined and split.

        Raises:
            ValueError: If a part is empty or contains the separator.
        """
        for part in self.parts:
            if not part:
                raise ValueError('a namespace part cannot be empty')
            if NAMESPACE_SEPARATOR in part:
                raise ValueError(
                    f'a namespace part cannot contain {NAMESPACE_SEPARATOR!r}: {part!r}')

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
class PortId:
    """A port's identity: which step, and which port on it.

    A distinct type from a step's identity on purpose. The compiler holds both
    as `str`, so nothing stops one being passed where the other is meant; the
    checker can stop it once they are different types.
    """

    namespace: Namespace
    step: str
    port: str

    def __post_init__(self) -> None:
        """Reject an identity that names nothing.

        Raises:
            ValueError: If the step or port name is empty.
        """
        if not self.step:
            raise ValueError('a port must belong to a named step')
        if not self.port:
            raise ValueError('a port must have a name')


@dataclass(frozen=True, slots=True)
class PortType:
    """What a port carries, in the algebra the compiler reasons within.

    The declared CWL type is kept verbatim in `declared`, because passthrough
    is open and the compiler must emit what it was given. The fields beside it
    are the part Sophios interprets: whether the value may be absent, how many
    array levels wrap it, and which alternatives a union admits. A type the
    compiler does not interpret has `declared` and nothing else — the leak
    boundary, expressed as a type rather than as a convention.
    """

    declared: OpaqueCwl
    optional: bool = False
    array_depth: int = 0
    union: tuple['PortType', ...] = ()

    def __post_init__(self) -> None:
        """Reject a depth that cannot describe a real type.

        Raises:
            ValueError: If `array_depth` is negative.
        """
        if self.array_depth < 0:
            raise ValueError('array_depth counts wrappers and cannot be negative')


@dataclass(frozen=True, slots=True)
class Port:
    """One port of one step: its identity, its type, and where it was written."""

    id: PortId
    type: PortType
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class StepNode:
    """A step, with the tool it runs and the ports it exposes.

    `interpreted` holds the CWL keys Sophios acts upon and `passthrough`
    everything else, matching the split the AST already makes — the boundary is
    the same one, carried forward rather than redrawn.
    """

    namespace: Namespace
    name: str
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    interpreted: tuple[tuple[str, OpaqueCwl], ...] = ()
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        """Reject a step whose ports do not belong to it.

        A port carrying another step's identity would produce an edge into a
        node that does not exist, which is the failure `Edge`'s own check
        cannot see because by then the port is only an identity.

        Raises:
            ValueError: If the step is unnamed, or a port names another step.
        """
        if not self.name:
            raise ValueError('a step must be named')
        for port in self.inputs + self.outputs:
            if port.id.step != self.name:
                raise ValueError(
                    f'port {port.id.port!r} names step {port.id.step!r} '
                    f'but belongs to {self.name!r}')


@dataclass(frozen=True, slots=True)
class Edge:
    """A value flowing from one port to another.

    An edge is between *ports*, not between steps: two edges into the same step
    are different bindings, and collapsing them to a step pair would lose which
    input each satisfies.
    """

    source: PortId
    sink: PortId
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        """Reject an edge from a port to itself.

        Raises:
            ValueError: If source and sink are the same port.
        """
        if self.source == self.sink:
            raise ValueError(f'an edge cannot join a port to itself: {self.source}')


@dataclass(frozen=True, slots=True)
class DeferredObligation:
    """An input whose producer is not in this document.

    A subworkflow may bind an input to something a parent supplies. The
    compiler carries that as an intermediate input created on the way down and
    satisfied as the recursion unwinds; naming it means the state has a type
    rather than a convention, and `Link` can say whether every one was
    discharged.
    """

    sink: PortId
    name: str
    type: PortType
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        """Reject an obligation with nothing to satisfy.

        Raises:
            ValueError: If the awaited name is empty.
        """
        if not self.name:
            raise ValueError('a deferred obligation must name what it awaits')


#: The four mappings the compiler threads through its recursion, which §7.1
#: makes fields of the graph "because that is what they always were".
EdgeDefinitions: TypeAlias = dict[str, PortId]
EdgeCalls: TypeAlias = dict[str, PortId]
InputMapping: TypeAlias = dict[str, tuple[PortId, ...]]
OutputMapping: TypeAlias = dict[str, PortId]


@dataclass(frozen=True, slots=True)
class WorkflowGraph:  # pylint: disable=too-many-instance-attributes
    """A whole workflow: its steps, the edges between them, and what it owes.

    The four mappings are fields rather than arguments. Threaded through a call
    stack they were state every function had to be handed and could quietly
    disagree about; here they belong to the graph they describe.
    """

    namespace: Namespace
    steps: tuple[StepNode, ...] = ()
    edges: tuple[Edge, ...] = ()
    obligations: tuple[DeferredObligation, ...] = ()
    explicit_edge_defs: EdgeDefinitions = field(default_factory=dict)
    explicit_edge_calls: EdgeCalls = field(default_factory=dict)
    input_mapping: InputMapping = field(default_factory=dict)
    output_mapping: OutputMapping = field(default_factory=dict)
    passthrough: tuple[tuple[str, OpaqueCwl], ...] = ()

    def __post_init__(self) -> None:
        """Reject a graph whose edges do not connect ports that exist.

        This is the invariant that makes a malformed graph unbuildable rather
        than merely detectable: an edge naming a port no step declares would
        otherwise survive to emission and become a dangling `source:`.

        Raises:
            ValueError: If a step name repeats, or an edge names a port that
                no step in this graph declares.
        """
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            repeated = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f'a step name identifies one step: {repeated}')

        known = {port.id for step in self.steps for port in step.inputs + step.outputs}
        for edge in self.edges:
            for role, port_id in (('source', edge.source), ('sink', edge.sink)):
                if port_id not in known:
                    raise ValueError(
                        f'edge {role} names a port no step declares: '
                        f'{port_id.step}/{port_id.port}')

    def step(self, name: str) -> StepNode | None:
        """The step called `name`, or None.

        Args:
            name (str): The step's name within this graph's namespace.

        Returns:
            StepNode | None: The step, if this graph has one by that name.
        """
        return next((s for s in self.steps if s.name == name), None)
