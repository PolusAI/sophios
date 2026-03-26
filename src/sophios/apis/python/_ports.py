"""Parameter and namespace helpers for the Python workflow API."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Generic, Iterator, TypeVar

from ._utils import (infer_literal_parameter_type,
                     is_array_type,
                     normalize_parameter_name,
                     normalize_parameter_type,
                     serialize_value)

if TYPE_CHECKING:
    from .api import Workflow


ParameterT = TypeVar("ParameterT")
ViewT = TypeVar("ViewT")


@dataclass(frozen=True, slots=True)
class InlineBinding:
    """Inline literal bound to an input parameter."""

    value: Any


@dataclass(frozen=True, slots=True)
class AliasBinding:
    """Reference to an upstream step output anchor."""

    alias: Any


@dataclass(frozen=True, slots=True)
class WorkflowBinding:
    """Reference to a formal workflow input."""

    name: str


InputBinding = InlineBinding | AliasBinding | WorkflowBinding


@dataclass(frozen=True, slots=True)
class OutputSourceBinding:
    """Source exposed as a formal workflow output."""

    step_id: str | None
    source_name: str

    def to_output_source(self, step_id_overrides: Mapping[str, str] | None = None) -> str:
        """Render the CWL `outputSource` string for a workflow output.

        Args:
            step_id_overrides (Mapping[str, str] | None): Optional mapping from
                user-facing step names to compiler-assigned concrete step ids.

        Returns:
            str: The serialized CWL `outputSource` value.
        """
        if self.step_id is None:
            return self.source_name
        resolved_step_id = step_id_overrides.get(self.step_id, self.step_id) if step_id_overrides else self.step_id
        return f"{resolved_step_id}/{self.source_name}"


@dataclass(slots=True)
class ParameterStore(Generic[ParameterT]):
    """Ordered name -> parameter mapping.

    Python dicts preserve insertion order, so one mapping is enough to support
    both explicit lookup and list-like indexing for the `.inputs[...]` style.
    """

    parameters: dict[str, ParameterT] = field(default_factory=dict)

    def add(self, parameter: ParameterT, *, name: str | None = None) -> ParameterT:
        self.parameters[name or getattr(parameter, "name")] = parameter
        return parameter

    def get(self, name: str) -> ParameterT:
        return self.parameters[name]

    def ensure(self, name: str, factory: Callable[[str], ParameterT]) -> ParameterT:
        if name not in self.parameters:
            self.parameters[name] = factory(name)
        return self.parameters[name]

    def __contains__(self, name: object) -> bool:
        return name in self.parameters

    def __iter__(self) -> Iterator[ParameterT]:
        return iter(self.parameters.values())

    def __len__(self) -> int:
        return len(self.parameters)

    def __getitem__(self, index: int) -> ParameterT:
        return tuple(self.parameters.values())[index]

    def __repr__(self) -> str:
        return repr(tuple(self.parameters.values()))


@dataclass(slots=True)
class _ParameterBase:
    """Shared state for named workflow/tool interface parameters."""

    name: str
    parameter_type: Any
    parent_obj: Any = None
    required: bool = field(init=False)
    linked: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.set_parameter_type(self.parameter_type)
        self.name = normalize_parameter_name(self.name)

    def set_parameter_type(self, value: Any) -> None:
        """Normalize and assign a parameter type expression."""
        self.parameter_type, self.required = normalize_parameter_type(value)

    def cwl_type(self) -> Any:
        """Return the CWL type expression including optionality."""
        if self.parameter_type is None:
            return None
        if self.required:
            return serialize_value(self.parameter_type)
        match self.parameter_type:
            case list() as options if "null" in options:
                return serialize_value(options)
            case list() as options:
                return ["null", *serialize_value(options)]
            case _:
                return ["null", serialize_value(self.parameter_type)]


@dataclass(slots=True)
class InputParameter(_ParameterBase):
    """Input parameter of a CWL `CommandLineTool` or `Workflow`."""

    _binding: InputBinding | None = field(default=None, init=False, repr=False)
    _bound_parameter_type: Any = field(default=None, init=False, repr=False)

    @property
    def value(self) -> Any:
        """Return the bound value in the legacy compatibility shape."""
        match self._binding:
            case None:
                return None
            case InlineBinding(value=value):
                return value
            case AliasBinding(alias=alias):
                return {"wic_alias": serialize_value(alias)}
            case WorkflowBinding(name=name):
                return name

    def _set_value(self, value: Any, linked: bool = False) -> None:
        """Translate legacy serialized values into the internal binding model."""
        match value:
            case {"wic_alias": alias} if linked:
                self._set_binding(AliasBinding(alias))
            case {"wic_inline_input": inline_value}:
                self._set_binding(InlineBinding(inline_value))
                self.set_bound_parameter_type(infer_literal_parameter_type(inline_value))
            case str() as workflow_name if linked:
                self._set_binding(WorkflowBinding(workflow_name))
            case _:
                self._set_binding(InlineBinding(value))
                self.set_bound_parameter_type(infer_literal_parameter_type(value))
                self.linked = linked

    def _set_binding(self, binding: InputBinding | None) -> None:
        self._binding = binding
        self.linked = isinstance(binding, (AliasBinding, WorkflowBinding))

    def set_bound_parameter_type(self, value: Any) -> None:
        """Record the type of the bound value when it is known."""
        normalized, _required = normalize_parameter_type(value)
        self._bound_parameter_type = normalized

    def is_scatterable(self) -> bool:
        """Return whether the current binding can be scattered safely."""
        match self._binding:
            case InlineBinding(value=list() | tuple()):
                return True
            case None:
                return False
            case _:
                return is_array_type(self._bound_parameter_type)

    def is_bound(self) -> bool:
        return self._binding is not None

    def to_yaml_value(self) -> Any:
        match self._binding:
            case None:
                return None
            case InlineBinding(value=value):
                return {"wic_inline_input": serialize_value(value)}
            case AliasBinding(alias=alias):
                return {"wic_alias": serialize_value(alias)}
            case WorkflowBinding(name=name):
                return name


@dataclass(slots=True)
class OutputParameter(_ParameterBase):
    """Output parameter of a CWL `CommandLineTool` or `Workflow`."""

    _anchor_name: str | None = field(default=None, init=False, repr=False)
    _source: OutputSourceBinding | None = field(default=None, init=False, repr=False)

    @property
    def value(self) -> Any:
        return None if self._anchor_name is None else {"wic_anchor": self._anchor_name}

    def ensure_anchor(self, suggested_name: str) -> str:
        if self._anchor_name is None:
            self._anchor_name = suggested_name
        self.linked = True
        return self._anchor_name

    def bind_source(self, source: OutputSourceBinding) -> None:
        self._source = source
        self.linked = True

    def has_source(self) -> bool:
        return self._source is not None

    def to_workflow_output(
        self,
        *,
        step_id_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Serialize this workflow output parameter to CWL.

        Args:
            step_id_overrides (Mapping[str, str] | None): Optional mapping from
                user-facing step names to compiler-assigned concrete step ids.

        Raises:
            ValueError: If the output has no source or no resolved type.

        Returns:
            dict[str, Any]: Serialized CWL workflow output definition.
        """
        if self._source is None:
            raise ValueError(f"workflow output {self.name!r} has no source binding")
        cwl_type = self.cwl_type()
        if cwl_type is None:
            raise ValueError(f"workflow output {self.name!r} has no resolved type")
        return {
            "type": cwl_type,
            "outputSource": self._source.to_output_source(step_id_overrides),
        }

    def _set_value(self, value: Any, linked: bool = False) -> None:
        match value:
            case {"wic_anchor": anchor_name}:
                self._anchor_name = str(anchor_name)
            case str() as anchor_name:
                self._anchor_name = str(anchor_name)
            case None:
                self._anchor_name = None
        self.linked = linked or self._anchor_name is not None


@dataclass(frozen=True, slots=True)
class WorkflowInputReference:
    """Symbolic reference to a workflow input variable."""

    workflow: Workflow
    name: str
    implicit: bool = False


class ParameterNamespace(Generic[ParameterT, ViewT]):
    """List-like attribute namespace for input and output parameters.

    The "magic" lives here: `step.inputs.foo`, `workflow.inputs.foo`, and
    `step.outputs.bar` all route through the same tiny proxy instead of four
    near-duplicate wrapper classes.
    """

    _store: ParameterStore[ParameterT]
    _getter: Callable[[str], ViewT]
    _setter: Callable[[str, Any], None] | None
    _read_only_error: str

    def __init__(
        self,
        store: ParameterStore[ParameterT],
        getter: Callable[[str], ViewT],
        setter: Callable[[str, Any], None] | None,
        *,
        read_only_error: str,
    ) -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_getter", getter)
        object.__setattr__(self, "_setter", setter)
        object.__setattr__(self, "_read_only_error", read_only_error)

    def __iter__(self) -> Iterator[ParameterT]:
        return iter(self._store)

    def __len__(self) -> int:
        return len(self._store)

    def __getitem__(self, index: int) -> ParameterT:
        return self._store[index]

    def __getattr__(self, name: str) -> ViewT:
        return self._getter(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if self._setter is None:
            raise AttributeError(self._read_only_error.format(name=name))
        self._setter(name, value)

    def __repr__(self) -> str:
        return repr(self._store)
