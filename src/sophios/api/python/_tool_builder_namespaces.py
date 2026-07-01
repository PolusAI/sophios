"""Private namespace objects for the public Tool Builder.

The builder surface is intentionally small:

- ``cwl`` is the only CWL vocabulary namespace
- ``Field``, ``Input``, and ``Output`` are the actual spec classes
- ``Inputs`` and ``Outputs`` are named collections that derive parameter names
  from Python keyword arguments
"""

from typing import Any, Iterator, Mapping, TypeVar

from ._tool_builder_specs import FieldSpec, InputSpec, OutputSpec
from ._tool_builder_support import (
    _canonicalize_type,
    _merge_if_set,
    _record_type_payload,
    _validate_api_name,
)


class _CWLNamespace:
    """Namespace for CWL type vocabulary and composite types."""

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
        """Wrap a CWL type in a nullable union."""
        canonical = _canonicalize_type(type_)
        match canonical:
            case list() as items if self.null in items:
                return items
            case _:
                return [self.null, canonical]

    def array(self, items: Any) -> dict[str, Any]:
        """Create a CWL array type."""
        return {"type": "array", "items": _canonicalize_type(items)}

    def enum(self, *symbols: str, name: str | None = None) -> dict[str, Any]:
        """Create a CWL enum type."""
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return payload

    def record(
        self,
        fields: Mapping[str, FieldSpec] | list[FieldSpec | dict[str, Any]],
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create a CWL record type."""
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

    def to_dict(self) -> dict[str, Any]:
        """Render the named collection into a CWL parameter mapping."""
        return {name: spec.to_dict() for name, spec in self._items.items()}


class Inputs(_NamedCollection[InputSpec]):
    """Named CLT inputs. Names come from Python keyword arguments."""


class Outputs(_NamedCollection[OutputSpec]):
    """Named CLT outputs. Names come from Python keyword arguments."""


class Fields(_NamedCollection[FieldSpec]):
    """Named CWL record fields. Names come from Python keyword arguments."""

    def to_list(self) -> list[dict[str, Any]]:
        """Render record fields in CWL's list-of-fields shape."""
        return [spec.to_dict() for spec in self._items.values()]
