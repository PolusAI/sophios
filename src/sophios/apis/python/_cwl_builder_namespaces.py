"""Private namespace objects for the public CWL builder.

The builder surface is intentionally small:

- ``cwl`` is the only CWL vocabulary namespace
- ``Field``, ``Input``, and ``Output`` are the actual spec classes
- ``Inputs`` and ``Outputs`` are named collections that derive parameter names
  from Python keyword arguments
"""

from typing import Any, Iterator, Mapping, TypeVar

from ._cwl_builder_specs import FieldSpec, InputSpec, OutputSpec
from ._cwl_builder_support import _canonicalize_type, _merge_if_set, _record_type_payload


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
        if isinstance(canonical, list) and self.null in canonical:
            return canonical
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


SpecT = TypeVar("SpecT", InputSpec, OutputSpec)


class _NamedCollection(Mapping[str, SpecT]):
    _items: dict[str, SpecT]

    def __init__(self, **specs: SpecT) -> None:
        self._items = {name: spec.named(name) for name, spec in specs.items()}

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
