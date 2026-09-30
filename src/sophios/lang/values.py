"""The shape a value must have, declared once for two readers.

The parser checks a value against its shape and reports each mismatch at the
node that holds it; the schema generator states the same shape in JSON Schema.
Both read one declaration, so the editor and the parser cannot disagree about
what a key admits.

A shape checks the materialised value and walks the YAML node beside it, so a
nested mismatch is reported at its own line rather than at its parent's.
Patterns follow JSON Schema: they match anywhere in the text, unanchored.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

import yaml

from .nodes import OpaqueCwl

#: One mismatch: what is wrong, and the node it is wrong at.
Problem: TypeAlias = tuple[str, yaml.nodes.Node]


class ValueShape(Protocol):
    """What a declared value shape offers each of its two readers."""

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        """Each way ``value``, read from ``node``, fails this shape."""

    def json(self) -> dict[str, Any]:
        """This shape as a JSON Schema fragment, fresh on every call."""


def _found(value: OpaqueCwl) -> str:
    match value:
        case dict():
            return 'a mapping'
        case list():
            return 'a list'
        case None | bool() | int() | float() | str():
            return repr(value)
        case _:
            return f'a {type(value).__name__}'


@dataclass(frozen=True, slots=True)
class Anything(ValueShape):
    """Any value. The key is declared; its value is not the language's to check."""

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        del value, node, where
        return iter(())

    def json(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True, slots=True)
class Text(ValueShape):
    """A string, at least ``min_length`` long, containing ``pattern`` if one is given."""

    min_length: int = 0
    pattern: str | None = None

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, str):
            yield f'{where} must be a string, found {_found(value)}', node
        elif len(value) < self.min_length:
            yield f'{where} must not be empty', node
        elif self.pattern is not None and re.search(self.pattern, value) is None:
            yield f'{where} must match {self.pattern}, found {value!r}', node

    def json(self) -> dict[str, Any]:
        fragment: dict[str, Any] = {'type': 'string'}
        if self.min_length:
            fragment['minLength'] = self.min_length
        if self.pattern is not None:
            fragment['pattern'] = self.pattern
        return fragment


@dataclass(frozen=True, slots=True)
class Flag(ValueShape):
    """`true` or `false`."""

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, bool):
            yield f'{where} must be true or false, found {_found(value)}', node

    def json(self) -> dict[str, Any]:
        return {'type': 'boolean'}


@dataclass(frozen=True, slots=True)
class OneOf(ValueShape):
    """Exactly one of a closed set of strings."""

    choices: tuple[str, ...]

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, str) or value not in self.choices:
            yield f'{where} must be one of {", ".join(self.choices)}, found {_found(value)}', node

    def json(self) -> dict[str, Any]:
        return {'enum': list(self.choices)}


@dataclass(frozen=True, slots=True)
class AnyMapping(ValueShape):
    """A mapping, with whatever keys and values."""

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, dict):
            yield f'{where} must be a mapping, found {_found(value)}', node

    def json(self) -> dict[str, Any]:
        return {'type': 'object'}


@dataclass(frozen=True, slots=True)
class ListOf(ValueShape):
    """A list, each item of which has the same shape."""

    item: ValueShape

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, list) or not isinstance(node, yaml.nodes.SequenceNode):
            yield f'{where} must be a list, found {_found(value)}', node
            return
        for index, (item, item_node) in enumerate(zip(value, node.value)):
            yield from self.item.problems(item, item_node, f'{where}[{index}]')

    def json(self) -> dict[str, Any]:
        return {'type': 'array', 'items': self.item.json()}


@dataclass(frozen=True, slots=True)
class Record(ValueShape):
    """A mapping with a closed set of keys, each with its own shape. Every key is optional."""

    fields: tuple[tuple[str, ValueShape], ...]

    def problems(self, value: OpaqueCwl, node: yaml.nodes.Node, where: str) -> Iterator[Problem]:
        if not isinstance(value, dict) or not isinstance(node, yaml.nodes.MappingNode):
            yield f'{where} must be a mapping, found {_found(value)}', node
            return
        declared = dict(self.fields)
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key not in declared:
                yield (f'{where} has no key {key!r}; its keys are: {", ".join(sorted(declared))}',
                       key_node)
            elif key in value:
                yield from declared[key].problems(value[key], value_node, f'{where}: {key}')

    def json(self) -> dict[str, Any]:
        return {'type': 'object',
                'properties': {key: shape.json() for key, shape in self.fields},
                'additionalProperties': False}
