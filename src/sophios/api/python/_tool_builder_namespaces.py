"""Private namespace objects for the public Tool Builder.

- ``Field``, ``Input``, and ``Output`` are the actual spec classes
- ``Inputs``, ``Outputs`` and ``Fields`` are named collections that derive
  parameter names from Python keyword arguments
"""

from collections.abc import Iterator, Mapping
from typing import Any, TypeVar

from ._ports import _validate_namespace_name
from ._tool_builder_specs import FieldSpec, InputSpec, OutputSpec
from ._utils import is_reserved_name, validate_python_identifier_name


# Intentional aliasing: these are the spec classes themselves, not thin
# wrapper namespaces. Making them directly callable keeps the required shape
# obvious: Input(type, ...), Output(type, ...), Field(type, ...).
Field = FieldSpec
Input = InputSpec
Output = OutputSpec


SpecT = TypeVar("SpecT", FieldSpec, InputSpec, OutputSpec)


def _validate_collection_name(name: str, *, owner: type[Any]) -> str:
    valid_name = validate_python_identifier_name(name, context="API name")
    if is_reserved_name(valid_name, owner):
        raise ValueError(
            f"API name {valid_name!r} is reserved by {owner.__name__}; choose a different name"
        )
    # An input or output is a port of each step that runs the tool; a record field is no port.
    return valid_name if owner is Fields else _validate_namespace_name(valid_name, context="API name")


class _NamedCollection(Mapping[str, SpecT]):
    __slots__ = ("_items",)
    _items: dict[str, SpecT]

    def __init__(self, **specs: SpecT) -> None:
        self._items = {
            _validate_collection_name(name, owner=type(self)): spec.named(name)
            for name, spec in specs.items()
        }

    def __getitem__(self, key: str) -> SpecT:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getattr__(self, name: str) -> SpecT:
        try:
            return self._items[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> list[dict[str, Any]]:
        """Render the collection as CWL writes it: a list of parameters, each with its name.

        Returns:
            list[dict[str, Any]]: One mapping per input, output or field, in order.
        """
        return [spec.to_dict() for spec in self._items.values()]


class Inputs(_NamedCollection[InputSpec]):
    """A tool's `inputs`, named by keyword: `Inputs(reads=Input(cwl.file, position=1))`.

    Each keyword is the input's `id`, in the order given. After construction
    `inputs.reads` is the named input, which `CommandLineTool.stage()` takes.

    Args:
        specs (InputSpec): The inputs, each an `Input(...)`.

    Raises:
        ValueError: If a name is not a Python identifier, is a special name such
            as `__len__`, or names an attribute of the collection, such as `keys`,
            or of a step's `inputs`, such as `_store`.
    """


class Outputs(_NamedCollection[OutputSpec]):
    """A tool's `outputs`, named by keyword: `Outputs(table=Output(cwl.file, glob="*.csv"))`.

    Each keyword is the output's `id`, in the order given.

    Args:
        specs (OutputSpec): The outputs, each an `Output(...)`.

    Raises:
        ValueError: If a name is not a Python identifier, is a special name such
            as `__len__`, or names an attribute of the collection, such as `keys`,
            or of a step's `outputs`, such as `_store`.
    """


class Fields(_NamedCollection[FieldSpec]):
    """A record type's `fields`, named by keyword, for `cwl.record()` and `Input.record()`.

    Each keyword is the field's `name`, in the order given.

    Args:
        specs (FieldSpec): The fields, each a `Field(...)`.

    Raises:
        ValueError: If a name is not a Python identifier, is a special name such
            as `__len__`, or names an attribute of the collection, such as `keys`.
    """
