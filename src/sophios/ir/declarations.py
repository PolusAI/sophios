"""Normalize the declared part of CWL process interfaces into the IR."""
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final

from .types import BoundaryDeclaration, PortDeclaration, PortType

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


def boundary_declaration(declaration: PortDeclaration) -> BoundaryDeclaration:
    """`declaration` as a workflow boundary may state it.

    Every phase that promotes a step's port to a workflow input goes through
    here, so a promoted port always emits valid CWL regardless of which phase
    promoted it.

    Args:
        declaration (PortDeclaration): A port declaration, usually a step's.

    Returns:
        PortDeclaration: The same declaration reduced to what a workflow
        boundary may say.
    """
    passthrough = tuple((name, deepcopy(value)) for name, value in declaration.passthrough
                        if name in _BOUNDARY_FIELDS)
    return BoundaryDeclaration(replace(declaration, passthrough=passthrough, shorthand=False,
                                       default=None, has_default=False))
