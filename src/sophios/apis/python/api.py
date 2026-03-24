# pylint: disable=W1203
"""Python API for building CWL/WIC workflows."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, Iterator, Optional, TypeVar, Union

import cwl_utils.parser as cu_parser
import yaml
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool
from cwl_utils.parser import load_document_by_uri, load_document_by_yaml

from sophios import compiler, input_output, plugins, utils_cwl
from sophios import post_compile as pc
from sophios import run_local as rl
from sophios.cli import get_dicts_for_compilation, get_known_and_unknown_args
from sophios.utils import convert_args_dict_to_args_list
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import CompilerInfo, Json, RoseTree, StepId, Tool, Tools, YamlTree

from ._types import ScatterMethod
from .api_config import default_values


global_config: Tools = {}


logger = logging.getLogger("WIC Python API")


class DisableEverythingFilter(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        return False


# Based on user feedback,
# disable any and all warnings coming from autodiscovery.
logger_wicad = logging.getLogger("wicautodiscovery")
logger_wicad.addFilter(DisableEverythingFilter())


class InvalidInputValueError(Exception):
    pass


class MissingRequiredValueError(Exception):
    pass


class InvalidStepError(Exception):
    pass


class InvalidLinkError(Exception):
    pass


class InvalidCLTError(ValueError):
    pass


CWLInputParameter = Union[
    cu_parser.cwl_v1_0.CommandInputParameter,
    cu_parser.cwl_v1_1.CommandInputParameter,
    cu_parser.cwl_v1_2.CommandInputParameter,
]

CWLOutputParameter = Union[
    cu_parser.cwl_v1_0.CommandOutputParameter,
    cu_parser.cwl_v1_1.CommandOutputParameter,
    cu_parser.cwl_v1_2.CommandOutputParameter,
]

StrPath = TypeVar("StrPath", str, Path)


@dataclass(frozen=True)
class _InlineBinding:
    value: Any


@dataclass(frozen=True)
class _AliasBinding:
    alias: Any


@dataclass(frozen=True)
class _WorkflowBinding:
    name: str


InputBinding = Union[_InlineBinding, _AliasBinding, _WorkflowBinding]


def _default_dict() -> dict[str, Any]:
    return {}


def _normalize_port_name(cwl_id: str) -> str:
    """Return the local port name from a CWL id."""
    return cwl_id.split("#")[-1]


def _normalize_port_type(port_type: Any) -> tuple[Any, bool]:
    """Return the canonicalized port type and whether it is required."""
    canonical = utils_cwl.canonicalize_type(port_type)
    match canonical:
        case list() as options:
            required = "null" not in options
            non_null_types = [entry for entry in options if entry != "null"]
            canonical = non_null_types[0] if non_null_types else options[0]
        case _:
            required = True
    return canonical, required


def _serialize_value(value: Any) -> Any:
    """Convert Path objects into YAML-safe values while preserving structure."""
    match value:
        case Path():
            return str(value)
        case list() as items:
            return [_serialize_value(item) for item in items]
        case tuple() as items:
            return [_serialize_value(item) for item in items]
        case dict() as items:
            return {key: _serialize_value(item) for key, item in items.items()}
        case _:
            return value


def _get_value_from_cfg(value: Any) -> Any:
    match value:
        case dict() as data if "Directory" in data.values():
            try:
                value_ = Path(data["location"])
            except Exception as exc:
                raise InvalidInputValueError() from exc
            if not value_.is_dir():
                raise InvalidInputValueError(f"{str(value_)} is not a directory")
            return value_
        case dict():
            return value
        case _:
            return value


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_handle:
        loaded = yaml.safe_load(file_handle)
    return loaded or {}


def _load_clt(clt_path: Path) -> tuple[CWLCommandLineTool, dict[str, Any]]:
    stepid = StepId(clt_path.stem, "global")

    if clt_path.exists():
        try:
            clt = load_document_by_uri(clt_path)
        except Exception as exc:
            raise InvalidCLTError(f"invalid cwl file: {clt_path}") from exc
        yaml_file = _load_yaml(clt_path)
        global_config[stepid] = Tool(str(clt_path), yaml_file)
        return clt, yaml_file

    if stepid in global_config:
        tool = global_config[stepid]
        logger.info(
            "%s does not exist, but %s was found in the global config.",
            clt_path,
            clt_path.stem,
        )
        logger.info("Using file contents from %s", tool.run_path)
        yaml_file = tool.cwl
        clt = load_document_by_yaml(yaml_file, tool.run_path)
        return clt, yaml_file

    logger.warning("Warning! %s does not exist, and", clt_path)
    logger.warning("%s was not found in the global config.", clt_path.stem)
    raise InvalidCLTError(f"invalid cwl file: {clt_path}")


class ProcessInput:
    """Input of a CWL CommandLineTool or Workflow."""

    inp_type: Any
    name: str
    parent_obj: Any
    required: bool
    linked: bool
    _binding: Optional[InputBinding]

    def __init__(self, name: str, inp_type: Any, parent_obj: Any = None) -> None:
        normalized_type, required = _normalize_port_type(inp_type)
        self.inp_type = normalized_type
        self.name = _normalize_port_name(name)
        self.parent_obj = parent_obj
        self.required = required
        self.linked = False
        self._binding: Optional[InputBinding] = None

    def __repr__(self) -> str:
        return f"ProcessInput(name={self.name!r}, inp_type={self.inp_type!r})"

    @property
    def value(self) -> Any:
        """Compatibility view of the current binding."""
        match self._binding:
            case None:
                return None
            case _InlineBinding(value=value):
                return value
            case _AliasBinding(alias=alias):
                return {"wic_alias": _serialize_value(alias)}
            case _WorkflowBinding(name=name):
                return name
        return None

    def _set_value(self, value: Any, linked: bool = False) -> None:
        """Compatibility helper used by older internal code paths."""
        match value:
            case {"wic_alias": alias} if linked:
                self._binding = _AliasBinding(alias)
                self.linked = True
            case {"wic_inline_input": inline_value} if not linked:
                self._binding = _InlineBinding(inline_value)
                self.linked = False
            case str() as workflow_name if linked:
                self._binding = _WorkflowBinding(workflow_name)
                self.linked = True
            case _:
                self._binding = _InlineBinding(value)
                self.linked = linked

    def _set_binding(self, binding: Optional[InputBinding]) -> None:
        self._binding = binding
        match binding:
            case _AliasBinding() | _WorkflowBinding():
                self.linked = True
            case _:
                self.linked = False

    def is_bound(self) -> bool:
        return self._binding is not None

    def to_yaml_value(self) -> Any:
        match self._binding:
            case None:
                return None
            case _InlineBinding(value=value):
                return {"wic_inline_input": _serialize_value(value)}
            case _AliasBinding(alias=alias):
                return {"wic_alias": _serialize_value(alias)}
            case _WorkflowBinding(name=name):
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
        normalized_type, required = _normalize_port_type(out_type)
        self.out_type = normalized_type
        self.name = _normalize_port_name(name)
        self.parent_obj = parent_obj
        self.required = required
        self.linked = False
        self._anchor_name: Optional[str] = None

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


def _bind_process_input(process_self: Any, input_name: str, value: Any) -> None:
    input_port = process_self.get_inp_attr(input_name)

    match value:
        case WorkflowInputReference(workflow=workflow, name=name):
            workflow._ensure_input(name)
            input_port._set_binding(_WorkflowBinding(name))
        case ProcessOutput(parent_obj=Workflow(), name=name) as output:
            input_port._set_binding(_WorkflowBinding(name))
            output.linked = True
        case ProcessOutput() as output:
            anchor_name = output.ensure_anchor(f"{input_name}{process_self.process_name}")
            input_port._set_binding(_AliasBinding(anchor_name))
        case _:
            input_port._set_binding(_InlineBinding(value))


def set_input_Step_Workflow(process_self: Any, __name: str, __value: Any) -> None:
    """Compatibility wrapper for the legacy helper name."""
    _bind_process_input(process_self, __name, __value)


class Step:
    """A workflow step backed by a CWL CommandLineTool."""

    clt: CWLCommandLineTool
    clt_path: Path
    process_name: str
    cwl_version: str
    yaml: dict[str, Any]
    cfg_yaml: dict[str, Any]
    _inputs: list[ProcessInput]
    _outputs: list[ProcessOutput]
    _input_map: dict[str, ProcessInput]
    _output_map: dict[str, ProcessOutput]
    _input_names: list[str]
    _output_names: list[str]
    inputs: StepInputs
    outputs: StepOutputs
    scatter: list[ProcessInput]
    scatterMethod: str
    when: str

    def __init__(self, clt_path: StrPath, config_path: Optional[StrPath] = None):
        match clt_path:
            case Path():
                clt_path_ = clt_path
            case str():
                clt_path_ = Path(clt_path)
            case _:
                raise TypeError("cwl_path must be a Path or str")
        clt, yaml_file = _load_clt(clt_path_)

        cfg_yaml: dict[str, Any]
        match config_path:
            case Path():
                cfg_yaml = _load_yaml(config_path)
            case str():
                cfg_yaml = _load_yaml(Path(config_path))
            case None:
                cfg_yaml = _default_dict()
            case _:
                raise TypeError("config_path must be a Path, str, or None")

        object.__setattr__(self, "clt", clt)
        object.__setattr__(self, "clt_path", clt_path_)
        object.__setattr__(self, "process_name", clt_path_.stem)
        object.__setattr__(self, "cwl_version", clt.cwlVersion)
        object.__setattr__(self, "yaml", yaml_file)
        object.__setattr__(self, "cfg_yaml", cfg_yaml)
        object.__setattr__(self, "_inputs", [])
        object.__setattr__(self, "_outputs", [])
        object.__setattr__(self, "_input_map", {})
        object.__setattr__(self, "_output_map", {})
        object.__setattr__(self, "_input_names", [])
        object.__setattr__(self, "_output_names", [])
        object.__setattr__(self, "inputs", StepInputs(self))
        object.__setattr__(self, "outputs", StepOutputs(self))
        object.__setattr__(self, "scatter", [])
        object.__setattr__(self, "scatterMethod", "")
        object.__setattr__(self, "when", "")

        self._populate_inputs(clt.inputs)
        self._populate_outputs(clt.outputs)

        if config_path:
            self._set_from_io_cfg()

    def _populate_inputs(self, cwl_inputs: list[CWLInputParameter]) -> None:
        for input_param in cwl_inputs:
            port = ProcessInput(str(input_param.id), input_param.type_, parent_obj=self)
            self._inputs.append(port)
            self._input_map[port.name] = port
            self._input_names.append(port.name)

    def _populate_outputs(self, cwl_outputs: list[CWLOutputParameter]) -> None:
        for output_param in cwl_outputs:
            port = ProcessOutput(str(output_param.id), output_param.type_, parent_obj=self)
            self._outputs.append(port)
            self._output_map[port.name] = port
            self._output_names.append(port.name)

    def __repr__(self) -> str:
        return f"Step(clt_path={self.clt_path!r})"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {
            "clt",
            "clt_path",
            "process_name",
            "cwl_version",
            "yaml",
            "cfg_yaml",
            "_inputs",
            "_outputs",
            "_input_map",
            "_output_map",
            "_input_names",
            "_output_names",
            "inputs",
            "outputs",
            "scatter",
            "scatterMethod",
            "when",
        }:
            match name, value:
                case "scatter", list() as items:
                    if not all(isinstance(item, ProcessInput) for item in items):
                        raise TypeError("all scatter inputs must be ProcessInput type")
                case "scatterMethod", scatter_method if scatter_method:
                    allowed = {member.value for member in ScatterMethod}
                    if scatter_method not in allowed:
                        raise ValueError(
                            "Invalid value for scatterMethod. "
                            f"Valid values are: {', '.join(sorted(allowed))}"
                        )
                case "when", str() as condition if condition:
                    if not condition.startswith("$(") or not condition.endswith(")"):
                        raise ValueError("Invalid input to when. The js string must start with '$(' and end with ')'")
                case "when", value if value:
                    raise ValueError("Invalid input to when. The js string must start with '$(' and end with ')'")
            object.__setattr__(self, name, value)
            return

        if "_input_map" in self.__dict__ and name in self._input_map:
            self.bind_input(name, value)
            return

        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in self._output_map:
            return self._output_map[name]
        raise AttributeError(f"{self.__class__.__name__!s} has no attribute {name!r}")

    def bind_input(self, name: str, value: Any) -> None:
        if name not in self._input_map:
            raise AttributeError(f"{self.process_name!r} has no input named {name!r}")
        _bind_process_input(self, name, value)

    def get_inp_attr(self, name: str) -> ProcessInput:
        if name not in self._input_map:
            raise AttributeError(f"{self.process_name!r} has no input named {name!r}")
        return self._input_map[name]

    def get_output(self, name: str) -> ProcessOutput:
        if name not in self._output_map:
            raise AttributeError(f"{self.process_name!r} has no output named {name!r}")
        return self._output_map[name]

    def _set_from_io_cfg(self) -> None:
        for name, value in self.cfg_yaml.items():
            setattr(self, name, _get_value_from_cfg(value))

    def _validate(self) -> None:
        for input_port in self._inputs:
            if input_port.required and not input_port.is_bound():
                raise MissingRequiredValueError(f"{input_port.name} is required")

    @property
    def _yml(self) -> dict[str, Any]:
        in_dict = {
            input_port.name: input_port.to_yaml_value()
            for input_port in self._inputs
            if input_port.is_bound()
        }

        out_list = [
            {output_port.name: output_port.value}
            for output_port in self._outputs
            if output_port.value is not None
        ]

        step_yaml: dict[str, Any] = {
            "id": self.process_name,
            "in": in_dict,
            "out": out_list,
        }

        if self.scatter:
            step_yaml["scatter"] = [input_port.name for input_port in self.scatter]
            step_yaml["scatterMethod"] = self.scatterMethod or ScatterMethod.dotproduct.value

        if self.when:
            step_yaml["when"] = self.when

        return step_yaml


def extract_tools_paths_NONPORTABLE(steps: list[Step]) -> Tools:
    """Extract the non-portable tool paths from the instantiated steps."""
    return {StepId(step.process_name, "global"): Tool(str(step.clt_path), step.yaml) for step in steps}


class Workflow:
    """A WIC workflow composed from Steps and/or nested Workflows."""

    steps: list[Any]
    process_name: str
    _inputs: list[ProcessInput]
    _outputs: list[ProcessOutput]
    _input_map: dict[str, ProcessInput]
    _output_map: dict[str, ProcessOutput]
    _input_names: list[str]
    _output_names: list[str]
    _input_references: dict[str, WorkflowInputReference]
    inputs: WorkflowInputs
    outputs: WorkflowOutputs
    yml_path: Optional[Path]

    def __init__(self, steps: list[Any], workflow_name: str):
        normalized_name = workflow_name.lstrip("/").lstrip(" ")
        parts = PurePath(normalized_name).parts
        normalized_name = "_".join(part for part in parts if part).lstrip("_").replace(" ", "_")

        object.__setattr__(self, "steps", list(steps))
        object.__setattr__(self, "process_name", normalized_name)
        object.__setattr__(self, "_inputs", [])
        object.__setattr__(self, "_outputs", [])
        object.__setattr__(self, "_input_map", {})
        object.__setattr__(self, "_output_map", {})
        object.__setattr__(self, "_input_names", [])
        object.__setattr__(self, "_output_names", [])
        object.__setattr__(self, "_input_references", {})
        object.__setattr__(self, "inputs", WorkflowInputs(self))
        object.__setattr__(self, "outputs", WorkflowOutputs(self))
        object.__setattr__(self, "yml_path", None)

    def __repr__(self) -> str:
        return f"Workflow(process_name={self.process_name!r}, steps={len(self.steps)})"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {
            "steps",
            "process_name",
            "_inputs",
            "_outputs",
            "_input_map",
            "_output_map",
            "_input_names",
            "_output_names",
            "_input_references",
            "inputs",
            "outputs",
            "yml_path",
        }:
            object.__setattr__(self, name, value)
            return

        if "_input_map" in self.__dict__:
            self.bind_input(name, value)
            return

        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self._ensure_input_reference(name)

    def _ensure_input(self, name: str) -> ProcessInput:
        if name not in self._input_map:
            logger.warning("Adding a new input %s to workflow %s", name, self.process_name)
            port = ProcessInput(name, "Any", parent_obj=self)
            self._inputs.append(port)
            self._input_map[name] = port
            self._input_names.append(name)
        return self._input_map[name]

    def _ensure_input_reference(self, name: str) -> WorkflowInputReference:
        self._ensure_input(name)
        if name not in self._input_references:
            self._input_references[name] = WorkflowInputReference(self, name)
        return self._input_references[name]

    def add_input(self, name: str) -> ProcessInput:
        return self._ensure_input(name)

    def add_output(self, name: str) -> ProcessOutput:
        if name not in self._output_map:
            logger.warning("Adding a new output %s to workflow %s", name, self.process_name)
            port = ProcessOutput(name, "Any", parent_obj=self)
            self._outputs.append(port)
            self._output_map[name] = port
            self._output_names.append(name)
        return self._output_map[name]

    def bind_input(self, name: str, value: Any) -> None:
        self._ensure_input(name)
        _bind_process_input(self, name, value)

    def get_inp_attr(self, name: str) -> ProcessInput:
        return self._ensure_input(name)

    def append(self, step_: Any) -> None:
        match step_:
            case Step() | Workflow():
                self.steps.append(step_)
            case _:
                raise TypeError("step must be either a Step or a Workflow")

    def _validate(self) -> None:
        for step in self.steps:
            try:
                match step:
                    case Step():
                        step._validate()
                    case Workflow():
                        step._validate()
            except Exception as exc:
                raise InvalidStepError(f"{step.process_name} is missing required inputs") from exc

    @property
    def yaml(self) -> dict[str, Any]:
        workflow_inputs = {input_port.name: {"type": "Any"} for input_port in self._inputs}
        steps_yaml = []

        for step in self.steps:
            match step:
                case Step():
                    steps_yaml.append(step._yml)
                case Workflow():
                    bound_inputs = {
                        input_port.name: input_port.to_yaml_value()
                        for input_port in step._inputs
                        if input_port.is_bound()
                    }
                    parentargs = {"in": bound_inputs} if bound_inputs else {}
                    steps_yaml.append(
                        {
                            "id": f"{step.process_name}.wic",
                            "subtree": step.yaml,
                            "parentargs": parentargs,
                        }
                    )

        return {"inputs": workflow_inputs, "steps": steps_yaml} if workflow_inputs else {"steps": steps_yaml}

    def write_ast_to_disk(self, directory: Path) -> None:
        workflow_inputs = {input_port.name: {"type": "Any"} for input_port in self._inputs}
        steps_yaml = []

        for step in self.steps:
            match step:
                case Step():
                    steps_yaml.append(step._yml)
                case Workflow():
                    bound_inputs = {
                        input_port.name: input_port.to_yaml_value()
                        for input_port in step._inputs
                        if input_port.is_bound()
                    }
                    parentargs = {"in": bound_inputs} if bound_inputs else {}
                    step.write_ast_to_disk(directory)
                    steps_yaml.append({"id": f"{step.process_name}.wic", **parentargs})

        yaml_contents = {"inputs": workflow_inputs, "steps": steps_yaml} if workflow_inputs else {"steps": steps_yaml}
        directory.mkdir(exist_ok=True, parents=True)
        output_path = directory / f"{self.process_name}.wic"
        with output_path.open(mode="w", encoding="utf-8") as file_handle:
            file_handle.write(yaml.dump(yaml_contents, sort_keys=False, line_break="\n", indent=2))

    def flatten_steps(self) -> list[Step]:
        steps: list[Step] = []
        for step in self.steps:
            match step:
                case Step():
                    steps.append(step)
                case Workflow():
                    steps.extend(step.flatten_steps())
        return steps

    def flatten_subworkflows(self) -> list[Workflow]:
        subworkflows = [self]
        for step in self.steps:
            match step:
                case Workflow():
                    subworkflows.extend(step.flatten_subworkflows())
        return subworkflows

    def compile(self, write_to_disk: bool = False) -> CompilerInfo:
        self._validate()

        graph = get_graph_reps(self.process_name)
        yaml_tree = YamlTree(StepId(self.process_name, "global"), self.yaml)

        steps_config = extract_tools_paths_NONPORTABLE(self.flatten_steps())
        merged_tools = dict(steps_config)
        merged_tools.update(global_config)

        compiler_options, graph_settings, yaml_tag_paths = get_dicts_for_compilation()
        compiler_info = compiler.compile_workflow(
            yaml_tree,
            compiler_options,
            graph_settings,
            yaml_tag_paths,
            [],
            [graph],
            {},
            {},
            {},
            {},
            merged_tools,
            True,
            relative_run_path=True,
            testing=False,
        )

        if write_to_disk:
            rose_tree: RoseTree = compiler_info.rose
            input_output.write_to_disk(rose_tree, Path("autogenerated/"), True)

        return compiler_info

    def get_cwl_workflow(self) -> Json:
        compiler_info = self.compile(write_to_disk=False)
        rose_tree = compiler_info.rose

        rose_tree = pc.cwl_inline_runtag(rose_tree)
        sub_node_data = rose_tree.data
        cwl_ast = sub_node_data.compiled_cwl
        yaml_inputs = sub_node_data.workflow_inputs_file
        return {"name": self.process_name, "yaml_inputs": yaml_inputs, **cwl_ast}

    def run(
        self,
        run_args_dict: Optional[Dict[str, str]] = None,
        user_env_vars: Optional[Dict[str, str]] = None,
        basepath: str = "autogenerated",
    ) -> None:
        logger.info("Running %s", self.process_name)
        plugins.logging_filters()

        effective_run_args = dict(default_values.default_run_args_dict)
        if run_args_dict:
            effective_run_args.update(run_args_dict)

        effective_env_vars = dict(user_env_vars or {})

        compiler_info = self.compile(write_to_disk=False)
        rose_tree: RoseTree = compiler_info.rose
        rose_tree = pc.cwl_inline_runtag(rose_tree)
        pc.find_and_create_output_dirs(rose_tree)
        pc.verify_container_engine_config(effective_run_args["container_engine"], False)
        input_output.write_to_disk(
            rose_tree,
            Path(basepath),
            True,
            effective_run_args.get("inputs_file", ""),
        )
        pc.cwl_docker_extract(
            effective_run_args["container_engine"],
            effective_run_args["pull_dir"],
            self.process_name,
        )
        if effective_run_args.get("docker_remove_entrypoints"):
            rose_tree = pc.remove_entrypoints(effective_run_args["container_engine"], rose_tree)
        user_args = convert_args_dict_to_args_list(effective_run_args)

        os.environ.update(rl.sanitize_env_vars(effective_env_vars))

        _, unknown_args = get_known_and_unknown_args(self.process_name, user_args)
        rl.run_local(
            effective_run_args,
            False,
            workflow_name=self.process_name,
            basepath=basepath,
            passthrough_args=unknown_args,
        )
