"""Normalize the declared part of CWL process interfaces into the IR."""
from copy import deepcopy
from typing import Any

from .types import PortDeclaration, PortType


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
