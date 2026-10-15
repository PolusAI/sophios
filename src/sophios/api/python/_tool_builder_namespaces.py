"""Private namespace objects for the public Tool Builder.

The builder surface is intentionally small:

- ``cwl`` is the only CWL vocabulary namespace
- ``Field``, ``Input``, and ``Output`` are the actual spec classes
- ``Inputs`` and ``Outputs`` are named collections that derive parameter names
  from Python keyword arguments
"""

from collections.abc import Iterator, Mapping
from typing import Any, TypeVar

from ._tool_builder_specs import FieldSpec, InputSpec, OutputSpec
from ._tool_builder_support import (
    _canonicalize_type,
    _merge_if_set,
    _record_type_payload,
    _validate_api_name,
)


class _CWLNamespace:
    """The CWL type vocabulary, as `cwl`: the types an `Input`, `Output` or `Field` takes.

    The attributes are the CWL atomic types, written as their CWL names:
    `cwl.null`, `cwl.boolean`, `cwl.int`, `cwl.long`, `cwl.float`, `cwl.double`,
    `cwl.string`, `cwl.file` (`File`) and `cwl.directory` (`Directory`). The
    methods build the composite types. A CWL type string such as `"File?"`,
    `"string[]"` or `"Any"` is accepted wherever a type is.
    """

    __slots__ = ()

    null = "null"
    boolean = "boolean"
    int = "int"
    long = "long"
    float = "float"
    double = "double"
    string = "string"
    file = "File"
    directory = "Directory"

    def optional(self, type_: Any) -> list[Any]:
        """Make a type optional: the union `["null", type_]`.

        Args:
            type_ (Any): The type; a type that is already optional is returned as it is.

        Returns:
            list[Any]: The union, as CWL writes it.
        """
        canonical = _canonicalize_type(type_)
        match canonical:
            case list() as items if self.null in items:
                return items
            case _:
                return [self.null, canonical]

    def array(self, items: Any) -> dict[str, Any]:
        """Make an array type: `{type: array, items: ...}`, which `"<items>[]"` also spells.

        Args:
            items (Any): The type of each element.

        Returns:
            dict[str, Any]: The array type.
        """
        return {"type": "array", "items": _canonicalize_type(items)}

    def enum(self, *symbols: str, name: str | None = None) -> dict[str, Any]:
        """Make an enum type: `{type: enum, symbols: [...]}`, a string from a fixed set.

        Args:
            symbols (str): The allowed values.
            name (str | None): The type's `name`; a type given to `schema_definitions()`
                needs one.

        Returns:
            dict[str, Any]: The enum type.
        """
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return payload

    def record(
        self,
        fields: Mapping[str, FieldSpec] | list[FieldSpec | dict[str, Any]],
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Make a record type: `{type: record, fields: [...]}`, a mapping with typed fields.

        Args:
            fields (Mapping[str, FieldSpec] | list[FieldSpec | dict[str, Any]]): The
                `fields`: a `Fields(...)` collection or a mapping of names to `Field`
                objects; or a list of named `Field` objects or field mappings.
            name (str | None): The type's `name`; a type given to `schema_definitions()`
                needs one.

        Returns:
            dict[str, Any]: The record type.
        """
        return _record_type_payload(fields, name=name)


cwl = _CWLNamespace()

# Intentional aliasing: these are the real immutable spec objects, not thin
# wrapper namespaces. Making them directly callable keeps the required shape
# obvious: Input(type, ...), Output(type, ...), Field(type, ...).
Field = FieldSpec
Input = InputSpec
Output = OutputSpec


SpecT = TypeVar("SpecT", FieldSpec, InputSpec, OutputSpec)


def _validate_collection_name(name: str, *, owner: type[Any]) -> str:
    valid_name = _validate_api_name(name, context="API name")
    if valid_name.startswith("_") or valid_name in dir(owner):
        raise ValueError(
            f"API name {valid_name!r} is reserved by {owner.__name__}; choose a different name"
        )
    return valid_name


class _NamedCollection(Mapping[str, SpecT]):
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
        ValueError: If a name is not a Python identifier, starts with `_`, or is
            the name of a method of the collection, such as `keys`.
    """


class Outputs(_NamedCollection[OutputSpec]):
    """A tool's `outputs`, named by keyword: `Outputs(table=Output(cwl.file, glob="*.csv"))`.

    Each keyword is the output's `id`, in the order given.

    Args:
        specs (OutputSpec): The outputs, each an `Output(...)`.

    Raises:
        ValueError: If a name is not a Python identifier, starts with `_`, or is
            the name of a method of the collection, such as `keys`.
    """


class Fields(_NamedCollection[FieldSpec]):
    """A record type's `fields`, named by keyword, for `cwl.record()` and `Input.record()`.

    Each keyword is the field's `name`, in the order given.

    Args:
        specs (FieldSpec): The fields, each a `Field(...)`.

    Raises:
        ValueError: If a name is not a Python identifier, starts with `_`, or is
            the name of a method of the collection, such as `keys`.
    """
