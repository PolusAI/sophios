"""Ranks, requiredness and boundary declarations of the ports of a process interface."""
from collections.abc import Callable
from dataclasses import replace

from ..lang.cwl import port_declaration
from ..lang.types import BoundaryDeclaration, PortDeclaration, layered, optional, spell
from .types import DerivedName, Direction, Port, PortName, StepNode, WorkflowGraph


def required(declaration: PortDeclaration | None) -> bool:
    """Whether a port must be given a value: no non-null default, not optional."""
    return declaration is None or not (
        (declaration.has_default and declaration.default is not None)
        or optional(declaration.type))


def input_rank(step: StepNode, port: PortName) -> int:
    """The array layers a value feeding `port` of `step` carries: one per time
    `scatter:` names the port."""
    return step.scatter_ports.count(port)


def output_rank(step: StepNode) -> int:
    """The array layers `step`'s scatter adds to each of its outputs: none
    without scatter, one per scattered port for `nested_crossproduct`, else one."""
    if not step.scatter_ports:
        return 0
    nested = dict(step.interpreted).get('scatterMethod') == 'nested_crossproduct'
    return len(step.scatter_ports) if nested else 1


def written_order(graph: WorkflowGraph) -> Callable[[PortName], tuple[int, int]]:
    """The key the CWL target orders `graph`'s boundary names by: the written names, then each name exposing one
    of the graph's own steps, in step order. A name relayed from a step nested further down exposes none of them,
    and sorts with the written names."""
    positions = {step.id: index for index, step in enumerate(graph.steps)}

    def key(name: PortName) -> tuple[int, int]:
        if isinstance(name, DerivedName) and name.step in positions:
            return (1, positions[name.step])
        return (0, 0)
    return key


def feeding_declaration(step: StepNode, port: Port) -> BoundaryDeclaration:
    """The workflow input bound straight into input `port` of `step`."""
    return boundary_declaration(port, input_rank(step, port.id.port))


def produced_declaration(step: StepNode, port: Port) -> BoundaryDeclaration:
    """The workflow output promoted from output `port` of `step`."""
    return boundary_declaration(port, output_rank(step))


def boundary_declaration(port: Port, rank: int) -> BoundaryDeclaration:
    """`port` as a workflow boundary may state it, `rank` array layers deep.

    Every boundary a phase derives from a step's port is built here, so a
    promoted port emits the same valid CWL whichever phase promoted it.
    `feeding_declaration` and `produced_declaration` supply the rank of a
    port on the step itself; Link composes one across nested scatters.

    Args:
        port (Port): A step's port.
        rank (int): The array layers the boundary adds to the port's type.

    Returns:
        BoundaryDeclaration: The port's declaration reduced to what a
        workflow boundary may say, its type layered.
    """
    declaration = port.declaration or port_declaration(spell(port.type), output=port.id.direction is Direction.OUTPUT)
    inside = ('items',) * rank
    return BoundaryDeclaration(replace(
        declaration, type=layered(declaration.type, rank), passthrough=declaration.kept, shorthand=False, default=None,
        has_default=False, unread=tuple(replace(item, path=(*inside, *item.path)) for item in declaration.unread)))
