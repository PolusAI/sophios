"""Parameter and namespace helpers for the Python workflow API."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar, cast

from sophios.ir.declarations import layered
from sophios.ir.types import PortType
from sophios.lang import CwlRecord, EdgeRef, InlineLiteral, InputValue, UnresolvedName

from ._errors import InvalidLinkError
from ._utils import (contains_any_type,
                     infer_literal_parameter_type,
                     is_array_type,
                     normalize_parameter_name,
                     normalize_parameter_type,
                     serialize_value,
                     validate_python_identifier_name)

if TYPE_CHECKING:
    from .workflow import Workflow


ParameterT = TypeVar("ParameterT")
ViewT = TypeVar("ViewT")


@dataclass(frozen=True, slots=True)
class InputBinding:
    """Bound input value, upstream alias, workflow input reference, or step-input record.

    A `record` binding's `value` is the language's `CwlRecord` and its `source` the
    port objects the record names, each a step `OutputParameter` or a workflow `InputParameter`.
    """

    kind: str
    value: Any
    source: Any = None

    @property
    def linked(self) -> bool:
        """Return whether this binding references another parameter rather than an inline literal."""
        return self.kind != "inline"

    def legacy_value(self) -> Any:
        """Return this binding's value in the legacy `.value` compatibility shape."""
        if self.kind == "alias":
            return {"wic_alias": serialize_value(self.value)}
        return self.value

    def to_input_value(self) -> InputValue:
        """Return this binding as the language's input construct."""
        match self.kind:
            case "inline":
                return InlineLiteral(serialize_value(self.value))
            case "alias":
                return EdgeRef(self.value)
            case "record":
                return cast(CwlRecord, self.value)
            case _:
                return UnresolvedName(self.value)


@dataclass(frozen=True, slots=True)
class OutputSourceBinding:
    """Source exposed as a formal workflow output."""

    step_id: str | None
    source_name: str

    def to_output_source(self, steps: Sequence[Any], source_parameter: Any = None) -> str:
        """Render the authored `outputSource`: `<owner step>/<port>`, or the input name.

        Resolution goes through the source parameter's live owner rather than
        `step_id`, which is a snapshot of a mutable display name: renaming a
        step after binding an output leaves the snapshot stale while the object
        graph stays valid. `step_id` is retained because it is what the user
        wrote, and it names the step in the error below. Step names are unique
        within a workflow, so the owner's name is its address.

        Args:
            steps (Sequence[Any]): The workflow's own steps.
            source_parameter (Any): The bound source parameter, whose
                `parent_obj` is the owning step.

        Raises:
            InvalidLinkError: If the source is not one of the workflow's steps.

        Returns:
            str: The serialized CWL `outputSource` value.
        """
        if self.step_id is None:
            return self.source_name
        owner = getattr(source_parameter, "parent_obj", None)
        if owner is None or not any(owner is step for step in steps):
            raise InvalidLinkError(
                f"workflow output source {self.step_id}/{self.source_name} is not one of this "
                "workflow's steps; bind it to a step the workflow was constructed with"
            )
        step = f"{owner.process_name}.wic" if type(owner).__name__ == "Workflow" else owner.process_name
        return f"{step}/{self.source_name}"


@dataclass(slots=True)
class ParameterStore(Generic[ParameterT]):
    """Ordered name -> parameter mapping.

    Python dicts preserve insertion order, so one mapping is enough to support
    both explicit lookup and list-like indexing for the `.inputs[...]` style.
    """

    parameters: dict[str, ParameterT] = field(default_factory=dict)

    def add(self, parameter: ParameterT, *, name: str | None = None) -> ParameterT:
        """Store `parameter` under `name` (or its own `.name`) and return it."""
        self.parameters[name or getattr(parameter, "name")] = parameter
        return parameter

    def get(self, name: str) -> ParameterT:
        """Return the stored parameter with the given name."""
        return self.parameters[name]

    def ensure(self, name: str, factory: Callable[[str], ParameterT]) -> ParameterT:
        """Return the existing parameter named `name`, creating it via `factory` if needed."""
        if name not in self.parameters:
            self.parameters[name] = factory(name)
        return self.parameters[name]

    def __contains__(self, name: object) -> bool:
        return name in self.parameters

    def __iter__(self) -> Iterator[ParameterT]:
        return iter(self.parameters.values())

    def __len__(self) -> int:
        return len(self.parameters)

    def __getitem__(self, index: object) -> ParameterT:
        if not isinstance(index, int):
            raise TypeError("parameter collections support integer indexing only; use attribute access for names")
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
        self.name = _validate_namespace_name(
            validate_python_identifier_name(
                normalize_parameter_name(self.name),
                context="CWL parameter name",
            ),
            context="CWL parameter name",
        )

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

    def as_type(self, parameter_type: Any) -> "_ParameterBase":
        """Assign a CWL type to this parameter and return it."""
        self.set_parameter_type(parameter_type)
        return self


@dataclass(slots=True)
class InputParameter(_ParameterBase):
    """Input parameter of a CWL `CommandLineTool` or `Workflow`.

    `declared` is False for a step input the tool does not declare, made by
    binding a `StepInput` to it; only a `StepInput` may bind such an input.
    """

    declared: bool = field(default=True, init=False)
    _binding: InputBinding | None = field(default=None, init=False, repr=False)

    @property
    def value(self) -> Any:
        """Return the bound value in the legacy compatibility shape."""
        return None if self._binding is None else self._binding.legacy_value()

    def _set_binding(self, binding: InputBinding | None) -> None:
        self._binding = binding
        self.linked = False if binding is None else binding.linked

    def record_sources(self) -> tuple[Any, ...]:
        """The port objects a record binding names; none for any other binding."""
        match self._binding:
            case InputBinding(kind="record", source=sources):
                return tuple(sources)
            case _:
                return ()

    def source_outputs(self) -> "tuple[OutputParameter, ...]":
        """The upstream outputs this input is bound to: an alias's one, or a record's."""
        match self._binding:
            case InputBinding(kind="alias", source=source):
                return (source,)
            case InputBinding(kind="record"):
                return tuple(source for source in self.record_sources() if isinstance(source, OutputParameter))
            case _:
                return ()

    def effective_source_type(self) -> Any:
        """The type the bound value carries when the workflow runs, computed now, not
        at bind time: a source step scattered after the bind lifts it an array level
        per layer, as its owner's `_output_rank()` says."""
        if self._binding is None:
            return None
        match self._binding.kind:
            case "inline":
                return infer_literal_parameter_type(self._binding.value)
            case "alias":
                return self._binding.source.effective_type()
            case "record":
                return _record_type(self._binding.value, self._binding.source)
            case _:
                return self._binding.source.parameter_type

    def is_scatterable(self) -> bool:
        """Whether the bound value is array-valued when the workflow runs."""
        if self._binding is None:
            return False
        if self._binding.kind == "inline" and isinstance(self._binding.value, (list, tuple)):
            return True
        effective = self.effective_source_type()
        return is_array_type(effective) or contains_any_type(effective)

    def is_bound(self) -> bool:
        """Return whether this input currently has a bound value."""
        return self._binding is not None


@dataclass(slots=True)
class OutputParameter(_ParameterBase):
    """Output parameter of a CWL `CommandLineTool` or `Workflow`."""

    _anchor_name: str | None = field(default=None, init=False, repr=False)
    _source: OutputSourceBinding | None = field(default=None, init=False, repr=False)
    _source_parameter: Any = field(default=None, init=False, repr=False)

    @property
    def value(self) -> Any:
        """Return the anchor reference for this output, if one has been assigned."""
        return None if self._anchor_name is None else {"wic_anchor": self._anchor_name}

    def ensure_anchor(self, suggested_name: str) -> str:
        """Return this output's anchor name, assigning `suggested_name` if none exists yet."""
        if self._anchor_name is None:
            self._anchor_name = suggested_name
        self.linked = True
        return self._anchor_name

    def bind_source(self, source: OutputSourceBinding, source_parameter: Any = None) -> None:
        """Bind this output to an upstream source and mark it as linked."""
        self._source = source
        self._source_parameter = source_parameter
        self.linked = True

    def has_source(self) -> bool:
        """Return whether this output is bound to a source."""
        return self._source is not None

    def effective_type(self) -> Any:
        """The type this output carries when the workflow runs, computed now, not at
        bind time. A step's output is lifted an array level per layer of its step's
        scatter. A workflow's output is its declared type, else its source's type,
        read through the same rule, so a scatter inside a subworkflow lifts it too."""
        if self._source_parameter is not None and self.parameter_type is None:
            match self._source_parameter:
                case OutputParameter() as source:
                    return source.effective_type()
                case source:
                    return source.cwl_type()
        declared = self.cwl_type()
        if declared is None:
            return None
        rank = self.parent_obj._output_rank()  # pylint: disable=protected-access
        return layered(PortType(declared), rank).canonical

    def to_workflow_output(self, steps: Sequence[Any]) -> dict[str, Any]:
        """Serialize this workflow output parameter to CWL.

        Args:
            steps (Sequence[Any]): The workflow's own steps.

        Raises:
            ValueError: If the output has no source or no resolved type.

        Returns:
            dict[str, Any]: Serialized CWL workflow output definition.
        """
        if self._source is None:
            raise ValueError(f"workflow output {self.name!r} has no source binding")
        cwl_type = self.effective_type()
        if cwl_type is None:
            raise ValueError(f"workflow output {self.name!r} has no resolved type")
        return {
            "type": cwl_type,
            "outputSource": self._source.to_output_source(steps, self._source_parameter),
        }


def _produced_type(source: Any) -> Any:
    """The type `source`, a step or subworkflow output or a workflow input, carries when
    the workflow runs."""
    if isinstance(source, OutputParameter):
        return source.effective_type()
    return source.parameter_type


# The `pickValue` methods that deliver one value, not a list.
_PICKS_ONE: Final = ("first_non_null", "the_only_non_null")


def _record_type(record: CwlRecord, sources: tuple[Any, ...]) -> Any:
    """The type a record delivers, from its first source: one source as it is, or,
    when sources are merged, the list CWL's `linkMerge` makes of them; a `pickValue`
    that picks one value then delivers one element of that list."""
    if not sources:
        return None
    first = _produced_type(sources[0])
    if first is None:
        return None
    fields = dict(record.fields)
    link_merge = fields.get("linkMerge")
    if len(sources) == 1 and link_merge is None:
        merged = first
    elif link_merge == "merge_flattened" and is_array_type(first):
        merged = first
    else:
        merged = {"type": "array", "items": first}
    match merged:
        case {"type": "array", "items": items} if fields.get("pickValue") in _PICKS_ONE:
            return items
        case _:
            return merged


@dataclass(frozen=True, slots=True)
class WorkflowInputReference:
    """Symbolic reference to a workflow input variable."""

    workflow: "Workflow"
    name: str
    implicit: bool = False

    def as_type(self, parameter_type: Any) -> "WorkflowInputReference":
        """Declare this workflow input's type and return the reference."""
        self.workflow._ensure_input(self.name, parameter_type, implicit=False)  # pylint: disable=protected-access
        return self


class ParameterNamespace(Generic[ParameterT, ViewT]):
    """List-like attribute namespace for input and output parameters.

    The "magic" lives here: `step.inputs.foo`, `workflow.inputs.foo`, and
    `step.outputs.bar` all route through the same compact proxy instead of four
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

    def __getitem__(self, index: object) -> ParameterT:
        if not isinstance(index, int):
            raise TypeError("port namespaces support integer indexing only; use attribute access for names")
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


def _validate_namespace_name(name: str, *, context: str) -> str:
    if name.startswith("_") or name in dir(ParameterNamespace):
        raise ValueError(
            f"{context} {name!r} is reserved by port namespaces; choose a different name"
        )
    return name
