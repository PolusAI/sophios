"""Normalize the declared part of CWL process interfaces into the IR."""
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final

from .types import BoundaryDeclaration, PortDeclaration, PortType

#: What a workflow boundary port may state. An allowlist, not a list of
#: things to drop: CWL gives a tool's input and a workflow's input different
#: records, and the tool's is the larger one, so naming what survives is the
#: only version that stays correct as declarations grow. `inputBinding` is the
#: one that bites -- a workflow input's is an `InputBinding`, a tool's is a
#: `CommandLineBinding`, so a promoted `position` fails validation.
_BOUNDARY_FIELDS: Final = frozenset({'type', 'format', 'label', 'doc'})


def port_type(raw: Any) -> PortType:
    """Build the conservative type algebra while preserving ``raw`` exactly."""
    optional = False
    depth = 0
    members: tuple[PortType, ...] = ()
    current = raw
    if isinstance(current, str):
        optional = current.endswith('?')
        if optional:
            current = current[:-1]
        while current.endswith('[]'):
            depth += 1
            current = current[:-2]
    elif isinstance(current, list):
        optional = 'null' in current
        members = tuple(port_type(item) for item in current if item != 'null')
    elif isinstance(current, dict) and current.get('type') == 'array' and 'items' in current:
        item = port_type(current['items'])
        optional = item.optional
        depth = item.array_depth + 1
        members = item.union
    return PortType(deepcopy(raw), optional=optional, array_depth=depth, union=members)


def port_declaration(raw: Any, *, output: bool = False) -> PortDeclaration:
    """Preserve one CWL port declaration and expose its recognized fields."""
    if not isinstance(raw, dict):
        return PortDeclaration(port_type(raw), shorthand=True)
    reserved = {'type', 'format', 'default'}
    if output:
        reserved.add('outputSource')
    return PortDeclaration(
        type=port_type(raw.get('type')),
        format=deepcopy(raw.get('format')),
        has_format='format' in raw,
        default=deepcopy(raw.get('default')),
        has_default='default' in raw,
        passthrough=tuple((key, deepcopy(value)) for key, value in raw.items()
                          if key not in reserved),
        field_order=tuple(raw),
    )


def boundary_declaration(declaration: PortDeclaration) -> BoundaryDeclaration:
    """`declaration` as a workflow boundary may state it.

    Every phase that promotes a step's port to a workflow input goes through
    here. Each used to carry its own version -- an allowlist in Complete, a
    two-name denylist in Infer, nothing at all in Link -- so whether a
    promoted port emitted valid CWL depended on which phase promoted it.

    Args:
        declaration (PortDeclaration): A port declaration, usually a step's.

    Returns:
        PortDeclaration: The same declaration reduced to what a workflow
        boundary may say.
    """
    passthrough = tuple((name, deepcopy(value)) for name, value in declaration.passthrough
                        if name in _BOUNDARY_FIELDS)
    order = tuple(name for name in declaration.field_order if name in _BOUNDARY_FIELDS)
    if 'type' not in order:
        order = ('type', *order)
    return BoundaryDeclaration(
        replace(declaration, passthrough=passthrough, field_order=order, shorthand=False))
