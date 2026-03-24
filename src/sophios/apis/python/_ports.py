"""Port and collection models for the Python API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Optional, Union

from ._utils import normalize_port_name, normalize_port_type, serialize_value

if TYPE_CHECKING:
    from .api import Step, Workflow


@dataclass(frozen=True)
class InlineBinding:
    value: Any


@dataclass(frozen=True)
class AliasBinding:
    alias: Any


@dataclass(frozen=True)
class WorkflowBinding:
    name: str


InputBinding = Union[InlineBinding, AliasBinding, WorkflowBinding]


class ProcessInput:
    """Input of a CWL CommandLineTool or Workflow."""

    inp_type: Any
    name: str
    parent_obj: Any
    required: bool
    linked: bool
    _binding: Optional[InputBinding]

    def __init__(self, name: str, inp_type: Any, parent_obj: Any = None) -> None:
        normalized_type, required = normalize_port_type(inp_type)
        self.inp_type = normalized_type
        self.name = normalize_port_name(name)
        self.parent_obj = parent_obj
        self.required = required
        self.linked = False
        self._binding = None

    def __repr__(self) -> str:
        return f"ProcessInput(name={self.name!r}, inp_type={self.inp_type!r})"

    @property
    def value(self) -> Any:
        """Compatibility view of the current binding."""
        match self._binding:
            case None:
                return None
            case InlineBinding(value=value):
                return value
            case AliasBinding(alias=alias):
                return {"wic_alias": serialize_value(alias)}
            case WorkflowBinding(name=name):
                return name
        return None

    def _set_value(self, value: Any, linked: bool = False) -> None:
        """Compatibility helper used by older internal code paths."""
        match value:
            case {"wic_alias": alias} if linked:
                self._binding = AliasBinding(alias)
                self.linked = True
            case {"wic_inline_input": inline_value} if not linked:
                self._binding = InlineBinding(inline_value)
                self.linked = False
            case str() as workflow_name if linked:
                self._binding = WorkflowBinding(workflow_name)
                self.linked = True
            case _:
                self._binding = InlineBinding(value)
                self.linked = linked

    def _set_binding(self, binding: Optional[InputBinding]) -> None:
        self._binding = binding
        match binding:
            case AliasBinding() | WorkflowBinding():
                self.linked = True
            case _:
                self.linked = False

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
        return None


class ProcessOutput:
    """Output of a CWL CommandLineTool or Workflow."""

    out_type: Any
    name: str
    parent_obj: Any
    required: bool
    linked: bool
    _anchor_name: Optional[str]

    def __init__(self, name: str, out_type: Any, parent_obj: Any = None) -> None:
        normalized_type, required = normalize_port_type(out_type)
        self.out_type = normalized_type
        self.name = normalize_port_name(name)
        self.parent_obj = parent_obj
        self.required = required
        self.linked = False
        self._anchor_name = None

    def __repr__(self) -> str:
        return f"ProcessOutput(name={self.name!r}, out_type={self.out_type!r})"

    @property
    def value(self) -> Any:
        if self._anchor_name is None:
            return None
        return {"wic_anchor": self._anchor_name}

    def ensure_anchor(self, suggested_name: str) -> str:
        if self._anchor_name is None:
            self._anchor_name = suggested_name
        self.linked = True
        return self._anchor_name

    def _set_value(self, value: Any, linked: bool = False) -> None:
        match value:
            case {"wic_anchor": anchor_name}:
                self._anchor_name = str(anchor_name)
            case str() as anchor_name:
                self._anchor_name = anchor_name
            case None:
                self._anchor_name = None
        self.linked = linked or self._anchor_name is not None


class WorkflowInputReference:
    """A symbolic reference to a workflow input variable."""

    workflow: Workflow
    name: str

    def __init__(self, workflow: Workflow, name: str) -> None:
        self.workflow = workflow
        self.name = name

    def __repr__(self) -> str:
        return f"WorkflowInputReference(workflow={self.workflow.process_name!r}, name={self.name!r})"


class StepInputs:
    """List-like view of a Step's inputs with explicit named access."""

    _step: Step

    def __init__(self, step: Step) -> None:
        object.__setattr__(self, "_step", step)

    def __iter__(self) -> Iterator[ProcessInput]:
        return iter(self._step._inputs)

    def __len__(self) -> int:
        return len(self._step._inputs)

    def __getitem__(self, index: int) -> ProcessInput:
        return self._step._inputs[index]

    def __getattr__(self, name: str) -> ProcessInput:
        return self._step.get_inp_attr(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_step":
            object.__setattr__(self, name, value)
            return
        self._step.bind_input(name, value)

    def __repr__(self) -> str:
        return repr(self._step._inputs)


class StepOutputs:
    """List-like view of a Step's outputs with explicit named access."""

    _step: Step

    def __init__(self, step: Step) -> None:
        object.__setattr__(self, "_step", step)

    def __iter__(self) -> Iterator[ProcessOutput]:
        return iter(self._step._outputs)

    def __len__(self) -> int:
        return len(self._step._outputs)

    def __getitem__(self, index: int) -> ProcessOutput:
        return self._step._outputs[index]

    def __getattr__(self, name: str) -> ProcessOutput:
        return self._step.get_output(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Step outputs are read-only; cannot set {name!r}")

    def __repr__(self) -> str:
        return repr(self._step._outputs)


class WorkflowInputs:
    """List-like view of a Workflow's inputs with explicit named access."""

    _workflow: Workflow

    def __init__(self, workflow: Workflow) -> None:
        object.__setattr__(self, "_workflow", workflow)

    def __iter__(self) -> Iterator[ProcessInput]:
        return iter(self._workflow._inputs)

    def __len__(self) -> int:
        return len(self._workflow._inputs)

    def __getitem__(self, index: int) -> ProcessInput:
        return self._workflow._inputs[index]

    def __getattr__(self, name: str) -> WorkflowInputReference:
        return self._workflow._ensure_input_reference(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_workflow":
            object.__setattr__(self, name, value)
            return
        self._workflow.bind_input(name, value)

    def __repr__(self) -> str:
        return repr(self._workflow._inputs)


class WorkflowOutputs:
    """List-like view of a Workflow's declared outputs."""

    _workflow: Workflow

    def __init__(self, workflow: Workflow) -> None:
        object.__setattr__(self, "_workflow", workflow)

    def __iter__(self) -> Iterator[ProcessOutput]:
        return iter(self._workflow._outputs)

    def __len__(self) -> int:
        return len(self._workflow._outputs)

    def __getitem__(self, index: int) -> ProcessOutput:
        return self._workflow._outputs[index]

    def __getattr__(self, name: str) -> ProcessOutput:
        return self._workflow.add_output(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Workflow outputs are read-only; cannot set {name!r}")

    def __repr__(self) -> str:
        return repr(self._workflow._outputs)
