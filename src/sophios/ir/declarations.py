"""Normalize the declared part of CWL process interfaces into the IR."""
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final

from .types import BoundaryDeclaration, Port, PortDeclaration, PortName, PortType, StepNode

#: What a workflow boundary port may state. An allowlist, not a denylist,
#: because a tool's input record is a strict superset of a workflow input's
#: (e.g. `inputBinding` is a `CommandLineBinding` on a tool, an `InputBinding`
#: on a workflow input, so a promoted `position` fails validation).
_BOUNDARY_FIELDS: Final = frozenset({'type', 'format', 'label', 'doc'})


def port_declaration(raw: Any, *, output: bool = False) -> PortDeclaration:
    """Preserve one CWL port declaration and expose its recognized fields."""
    if not isinstance(raw, dict):
        return PortDeclaration(PortType(deepcopy(raw)), shorthand=True)
    reserved = {'type', 'format', 'default'}
    if output:
        reserved.add('outputSource')
    return PortDeclaration(
        type=PortType(deepcopy(raw.get('type'))),
        format=deepcopy(raw.get('format')),
        has_format='format' in raw,
        default=deepcopy(raw.get('default')),
        has_default='default' in raw,
        passthrough=tuple((key, deepcopy(value)) for key, value in raw.items()
                          if key not in reserved),
    )


def required(declaration: PortDeclaration | None) -> bool:
    """Whether a port must be given a value: no non-null default, not optional."""
    return declaration is None or not (
        (declaration.has_default and declaration.default is not None)
        or declaration.type.optional)


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


def layered(port_type: PortType, rank: int) -> PortType:
    """`port_type`'s canonical type inside `rank` array layers."""
    raw = port_type.canonical
    for _ in range(rank):
        raw = {'type': 'array', 'items': raw}
    return PortType(raw)


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
        workflow boundary may say, its type canonical and layered.
    """
    declaration = port.declaration or port_declaration(port.type.declared)
    passthrough = tuple((name, deepcopy(value)) for name, value in declaration.passthrough
                        if name in _BOUNDARY_FIELDS)
    return BoundaryDeclaration(replace(declaration, type=layered(declaration.type, rank),
                                       passthrough=passthrough, shorthand=False,
                                       default=None, has_default=False))
