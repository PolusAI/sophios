"""Normalize the declared part of CWL process interfaces into the IR."""
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final

from ..lang.nodes import OpaqueCwl
from .types import BoundaryDeclaration, Direction, Port, PortDeclaration, PortName, PortType, StepNode

#: What a workflow boundary port may state, by direction: the fields the CWL
#: v1.2 schema gives `WorkflowOutputParameter` and `WorkflowInputParameter`.
#: An allowlist, not a denylist, because a tool's port record is a superset of a
#: workflow's (`inputBinding` is a `CommandLineBinding` on a tool and an
#: `InputBinding` on a workflow input, so a promoted `position` fails
#: validation; `outputBinding` has no workflow counterpart). Only an input may
#: ask for `loadContents` and `loadListing`.
_OUTPUT_BOUNDARY_FIELDS: Final = frozenset({'type', 'format', 'label', 'doc', 'secondaryFiles', 'streamable'})
_INPUT_BOUNDARY_FIELDS: Final = _OUTPUT_BOUNDARY_FIELDS | {'loadContents', 'loadListing'}

#: What makes a CWL string an expression.
_EXPRESSION_MARKERS: Final = ('$(', '${')


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


def _evaluated(entry: OpaqueCwl) -> bool:
    """Whether a `secondaryFiles` entry holds an expression, as its pattern or as its `required`."""
    texts = entry.values() if isinstance(entry, dict) else (entry,)
    return any(isinstance(text, str) and any(marker in text for marker in _EXPRESSION_MARKERS)
               for text in texts)


def _statable_secondary_files(value: OpaqueCwl) -> OpaqueCwl:
    """The `secondaryFiles` entries a workflow boundary can state.

    An expression is evaluated by the tool that wrote it, against that tool's
    `inputs` and `expressionLib`. The boundary has neither, so it would
    evaluate the expression differently or fail on it at run time; the tool
    still declares the entry and keeps evaluating it.

    A bare pattern or a single mapping is stated as a one-element list: it is
    the same declaration, and cwltool's checker reads every entry as a mapping
    at workflow level, so it fails on the bare form.
    """
    entries = value if isinstance(value, list) else [] if value is None else [value]
    return [entry for entry in entries if not _evaluated(entry)]


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
    allowed = _INPUT_BOUNDARY_FIELDS if port.id.direction is Direction.INPUT else _OUTPUT_BOUNDARY_FIELDS
    passthrough: list[tuple[str, OpaqueCwl]] = []
    for name, value in declaration.passthrough:
        if name not in allowed:
            continue
        if name == 'secondaryFiles':
            value = _statable_secondary_files(value)
            if not value:
                continue
        passthrough.append((name, deepcopy(value)))
    return BoundaryDeclaration(replace(declaration, type=layered(declaration.type, rank),
                                       passthrough=tuple(passthrough), shorthand=False,
                                       default=None, has_default=False))
