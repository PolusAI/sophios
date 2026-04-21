# pylint: disable=logging-fstring-interpolation,too-many-lines,protected-access
"""Python API for building CWL/WIC workflows."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Mapping

from cwl_utils.parser import CommandLineTool as CWLCommandLineTool

from sophios.inference import types_match
from sophios.wic_types import CompilerInfo, Json, Tools

from ._errors import (
    InvalidLinkError,
    InvalidStepError,
    MissingRequiredValueError,
)
from ._ports import (
    AliasBinding as _AliasBinding,
    InputParameter,
    InlineBinding as _InlineBinding,
    OutputSourceBinding,
    OutputParameter,
    ParameterNamespace,
    ParameterStore,
    WorkflowBinding as _WorkflowBinding,
    WorkflowInputReference,
)
from ._utils import (
    infer_literal_parameter_type as _infer_literal_parameter_type,
    get_value_from_cfg as _get_value_from_cfg,
    load_yaml as _load_yaml,
)
from ._types import ScatterMethod
from ._workflow_runtime import (
    coerce_path as _coerce_path,
    compile_workflow as _compile_workflow,
    load_clt_document as _load_clt_document,
    load_clt as _load_clt,
    lookup_parameter as _lookup_parameter,
    normalize_workflow_name as _normalize_workflow_name,
    populate_parameters as _populate_parameters,
    run_workflow as _run_workflow,
    validate_step_assignment as _validate_step_assignment,
    workflow_document as _workflow_document,
    compiled_cwl_json as _compiled_cwl_json,
    write_workflow_ast_to_disk as _write_workflow_ast_to_disk,
)


logger = logging.getLogger("WIC Python API")


class DisableEverythingFilter(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        return False


# Based on user feedback,
# disable any and all warnings coming from autodiscovery.
logger_wicad = logging.getLogger("wicautodiscovery")
logger_wicad.addFilter(DisableEverythingFilter())


StrPath = str | Path


def _parameter_namespace(
    store: ParameterStore[Any],
    getter: Any,
    setter: Any,
    *,
    read_only_error: str,
) -> ParameterNamespace[Any, Any]:
    """Create the list-like attribute proxy used for `.inputs` and `.outputs`."""
    return ParameterNamespace(store, getter, setter, read_only_error=read_only_error)


def _resolve_parameter_type(
    parameter: InputParameter | OutputParameter,
    candidate_type: Any,
    *,
    context: str,
) -> None:
    """Infer or validate a parameter type against a new candidate."""
    if candidate_type is None:
        return
    if parameter.parameter_type is None:
        parameter.set_parameter_type(candidate_type)
        return
    if not types_match(parameter.parameter_type, candidate_type):
        raise InvalidLinkError(
            f"{context} has incompatible types: expected {parameter.parameter_type!r}, got {candidate_type!r}"
        )


def _warn_implicit_workflow_parameter(workflow: Workflow, name: str, kind: str) -> None:
    """Warn when compatibility syntax implicitly declares workflow interface."""
    warnings.warn(
        (
            f"Implicitly declaring workflow {kind} {name!r} on {workflow.process_name!r}. "
            f"Prefer explicit {kind}s via workflow.add_{kind}(...), workflow.{kind}s.{name}, "
            f"or typed bindings so interface drift is easier to spot."
        ),
        UserWarning,
        stacklevel=3,
    )


def _bind_process_input(process_self: Any, input_name: str, value: Any) -> None:
    input_port = process_self.get_inp_attr(input_name)

    # This is the central compatibility switchboard for the Python API:
    # - workflow.input_name means "formal workflow parameter"
    # - step.output_name means "link to upstream step output"
    # - everything else is treated as a literal inline value
    match value:
        case WorkflowInputReference(workflow=workflow, name=name, implicit=implicit):
            workflow_input = workflow._ensure_input(name, parameter_type=input_port.parameter_type, implicit=implicit)
            input_port._set_binding(_WorkflowBinding(name))
            input_port.set_bound_parameter_type(workflow_input.parameter_type)
        case OutputParameter(parent_obj=Workflow(), name=name):
            raise InvalidLinkError(
                f"Workflow output {name!r} cannot be bound as an input. "
                f"Use workflow.inputs.{name} for formal inputs or workflow.outputs.{name} = ... for outputs."
            )
        case OutputParameter() as output:
            _resolve_parameter_type(
                input_port,
                output.parameter_type,
                context=f"{process_self.process_name}.{input_name}",
            )
            anchor_name = output.ensure_anchor(f"{input_name}{process_self.process_name}")
            input_port._set_binding(_AliasBinding(anchor_name))
            input_port.set_bound_parameter_type(output.parameter_type)
        case _:
            input_port._set_binding(_InlineBinding(value))
            input_port.set_bound_parameter_type(_infer_literal_parameter_type(value))


def _bind_workflow_output(workflow: Workflow, output_name: str, value: Any) -> None:
    output_parameter = workflow.add_output(output_name, implicit=True)
    match value:
        case OutputParameter(parent_obj=Step(process_name=process_name), name=name) as source:
            _resolve_parameter_type(
                output_parameter,
                source.parameter_type,
                context=f"{workflow.process_name}.outputs.{output_name}",
            )
            output_parameter.bind_source(OutputSourceBinding(process_name, name))
            source.linked = True
        case WorkflowInputReference(workflow=source_workflow, name=name) if source_workflow is workflow:
            input_parameter = workflow._ensure_input(name)
            _resolve_parameter_type(
                output_parameter,
                input_parameter.parameter_type,
                context=f"{workflow.process_name}.outputs.{output_name}",
            )
            output_parameter.bind_source(OutputSourceBinding(None, name))
        case _:
            raise InvalidLinkError(
                "workflow outputs must be bound to a step output or a workflow input reference"
            )


class Step:
    """A workflow step backed by a CWL `CommandLineTool`.

    Attribute writes like `step.message = "hi"` bind named step inputs.
    Attribute reads like `step.output_file` resolve named step outputs. The
    same ports are also available through the explicit `step.inputs.*` and
    `step.outputs.*` namespaces.
    """

    _SYSTEM_ATTRS: ClassVar[set[str]] = {
        "clt",
        "clt_path",
        "process_name",
        "cwl_version",
        "yaml",
        "cfg_yaml",
        "_tool_registry",
        "_inputs",
        "_outputs",
        "inputs",
        "outputs",
        "scatter",
        "scatterMethod",
        "when",
    }

    clt: CWLCommandLineTool
    clt_path: Path
    process_name: str
    cwl_version: str
    yaml: dict[str, Any]
    cfg_yaml: dict[str, Any]
    _tool_registry: Tools
    _inputs: ParameterStore[InputParameter]
    _outputs: ParameterStore[OutputParameter]
    inputs: ParameterNamespace[InputParameter, InputParameter]
    outputs: ParameterNamespace[OutputParameter, OutputParameter]
    scatter: list[InputParameter]
    scatterMethod: str
    when: str

    def __init__(
        self,
        clt_path: StrPath,
        config_path: StrPath | None = None,
        *,
        tool_registry: Tools | None = None,
    ):
        """Create a `Step` from a CWL CommandLineTool file.

        Args:
            clt_path (StrPath): Path to the CWL tool definition.
            config_path (StrPath | None): Optional YAML config used to pre-bind inputs.
            tool_registry (Tools | None): Optional fallback registry for known tools.

        Raises:
            TypeError: If `clt_path` or `config_path` uses an unsupported type.
            InvalidCLTError: If the CWL tool cannot be loaded from disk or the registry.

        Returns:
            None: The step is initialized in place.
        """
        clt_path_ = _coerce_path(clt_path, field_name="clt_path")
        config_path_ = _coerce_path(config_path, field_name="config_path", allow_none=True)
        assert clt_path_ is not None
        resolved_registry = {} if tool_registry is None else tool_registry
        clt, yaml_file = _load_clt(clt_path_, resolved_registry)
        cfg_yaml = _load_yaml(config_path_) if config_path_ is not None else {}

        self._initialize_loaded_tool(
            clt=clt,
            yaml_file=yaml_file,
            clt_path=clt_path_,
            cfg_yaml=cfg_yaml,
            tool_registry=resolved_registry,
        )

    @classmethod
    def from_cwl(
        cls,
        document: Mapping[str, Any],
        *,
        process_name: str | None = None,
        run_path: StrPath | None = None,
        config: Mapping[str, Any] | None = None,
        tool_registry: Tools | None = None,
    ) -> Step:
        # pylint: disable=too-many-arguments
        """Create a `Step` from an in-memory CWL CommandLineTool document.

        Args:
            document (Mapping[str, Any]): Parsed CWL CommandLineTool fields.
            process_name (str | None): Optional step name override.
            run_path (StrPath | None): Optional virtual `.cwl` path for compiler bookkeeping.
            config (Mapping[str, Any] | None): Optional input values to pre-bind.
            tool_registry (Tools | None): Optional tool registry retained on the step.

        Raises:
            TypeError: If `run_path` uses an unsupported type.
            InvalidCLTError: If the CWL document cannot be parsed.

        Returns:
            Step: A fully initialized step backed by the in-memory tool.
        """
        default_name = process_name or str(document.get("id") or "in_memory_tool")
        run_path_value = run_path or f"{default_name}.cwl"
        clt_path = _coerce_path(run_path_value, field_name="run_path")
        assert clt_path is not None
        resolved_registry = {} if tool_registry is None else tool_registry
        clt, yaml_file = _load_clt_document(document, run_path=clt_path)

        step = cls.__new__(cls)
        step._initialize_loaded_tool(
            clt=clt,
            yaml_file=yaml_file,
            clt_path=clt_path,
            cfg_yaml=dict(config or {}),
            tool_registry=resolved_registry,
            process_name=process_name,
        )
        return step

    def _initialize_loaded_tool(
        self,
        *,
        clt: CWLCommandLineTool,
        yaml_file: dict[str, Any],
        clt_path: Path,
        cfg_yaml: Mapping[str, Any],
        tool_registry: Tools,
        process_name: str | None = None,
    ) -> None:
        # pylint: disable=too-many-arguments
        """Populate a step from an already parsed CLT and optional config.

        Args:
            clt (CWLCommandLineTool): Parsed CWL tool object.
            yaml_file (dict[str, Any]): Raw CWL document.
            clt_path (Path): Filesystem or virtual path representing the tool.
            cfg_yaml (Mapping[str, Any]): Optional input bindings to apply.
            tool_registry (Tools): Tool registry preserved on the step.
            process_name (str | None): Optional explicit step name override.

        Returns:
            None: The step is initialized in place.
        """
        resolved_name = process_name or clt_path.stem

        object.__setattr__(self, "clt", clt)
        object.__setattr__(self, "clt_path", clt_path)
        object.__setattr__(self, "process_name", resolved_name)
        object.__setattr__(self, "cwl_version", clt.cwlVersion)
        object.__setattr__(self, "yaml", yaml_file)
        object.__setattr__(self, "cfg_yaml", dict(cfg_yaml))
        object.__setattr__(self, "_tool_registry", tool_registry)
        object.__setattr__(self, "_inputs", ParameterStore())
        object.__setattr__(self, "_outputs", ParameterStore())
        # This proxy is the main bit of API "magic": it supports both
        # list-style access (`step.inputs[0]`) and named attribute access
        # (`step.inputs.message`) without duplicating wrapper classes.
        object.__setattr__(
            self,
            "inputs",
            _parameter_namespace(self._inputs, self.get_inp_attr, self.bind_input, read_only_error=""),
        )
        object.__setattr__(
            self,
            "outputs",
            _parameter_namespace(
                self._outputs,
                self.get_output,
                None,
                read_only_error="Step outputs are read-only; cannot set {name!r}",
            ),
        )
        object.__setattr__(self, "scatter", [])
        object.__setattr__(self, "scatterMethod", "")
        object.__setattr__(self, "when", "")

        _populate_parameters(clt.inputs, self._inputs, InputParameter, parent=self)
        _populate_parameters(clt.outputs, self._outputs, OutputParameter, parent=self)

        if self.cfg_yaml:
            self._set_from_io_cfg()

    def __repr__(self) -> str:
        return f"Step(clt_path={self.clt_path!r})"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._SYSTEM_ATTRS:
            _validate_step_assignment(name, value, owner=self)
            object.__setattr__(self, name, value)
            return

        # Legacy sugar is intentionally preserved: assigning to a known input
        # parameter name binds that input instead of setting a plain attribute.
        if "_inputs" in self.__dict__ and name in self._inputs:
            self.bind_input(name, value)
            return
        if "_outputs" in self.__dict__ and name in self._outputs:
            raise AttributeError(f"Step outputs are read-only; cannot set {name!r}")
        raise AttributeError(
            f"{self.process_name!r} has no input named {name!r}. "
            "Use step.inputs.<name> for declared inputs only."
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in self._outputs:
            return self._outputs.get(name)
        raise AttributeError(f"{self.__class__.__name__!s} has no attribute {name!r}")

    def bind_input(self, name: str, value: Any) -> None:
        """Bind a value or upstream output to a named step input parameter.

        Args:
            name (str): The input parameter name.
            value (Any): A literal value, a workflow input reference, or a step output.

        Raises:
            AttributeError: If the named input does not exist on the step.

        Returns:
            None: The step is mutated in place.
        """
        _lookup_parameter(self._inputs, name, owner_name=self.process_name, kind="input")
        _bind_process_input(self, name, value)

    def get_inp_attr(self, name: str) -> InputParameter:
        """Return a named input parameter from this step.

        Args:
            name (str): The input parameter name.

        Raises:
            AttributeError: If the input does not exist.

        Returns:
            InputParameter: The requested step input parameter.
        """
        return _lookup_parameter(self._inputs, name, owner_name=self.process_name, kind="input")

    def get_output(self, name: str) -> OutputParameter:
        """Return a named output parameter from this step.

        Args:
            name (str): The output parameter name.

        Raises:
            AttributeError: If the output does not exist.

        Returns:
            OutputParameter: The requested step output parameter.
        """
        return _lookup_parameter(self._outputs, name, owner_name=self.process_name, kind="output")

    def _set_from_io_cfg(self) -> None:
        for name, value in self.cfg_yaml.items():
            setattr(self, name, _get_value_from_cfg(value))

    def _validate(self) -> None:
        for input_port in self._inputs:
            if input_port.required and not input_port.is_bound():
                raise MissingRequiredValueError(f"{input_port.name} is required")

    def flatten_steps(self) -> list[Step]:
        """Return this step as a single-item list for recursive traversal."""
        return [self]

    def flatten_subworkflows(self) -> list[Workflow]:
        """Return an empty subworkflow list because steps do not nest workflows."""
        return []

    def _as_workflow_step(self, *, inline_subtrees: bool, directory: Path | None = None) -> dict[str, Any]:
        del inline_subtrees, directory
        return self._yml

    @property
    def _yml(self) -> dict[str, Any]:
        """Return the internal WIC step representation for this step."""
        step_yaml: dict[str, Any] = {
            "id": self.process_name,
            "in": {port.name: port.to_yaml_value() for port in self._inputs if port.is_bound()},
            "out": [{port.name: port.value} for port in self._outputs if port.value is not None],
        }

        if self.scatter:
            step_yaml["scatter"] = [input_port.name for input_port in self.scatter]
            step_yaml["scatterMethod"] = self.scatterMethod or ScatterMethod.dotproduct.value

        if self.when:
            step_yaml["when"] = self.when

        return step_yaml


class Workflow:
    """A WIC workflow composed from `Step` objects and nested `Workflow`s."""

    _SYSTEM_ATTRS: ClassVar[set[str]] = {
        "steps",
        "process_name",
        "_inputs",
        "_outputs",
        "inputs",
        "outputs",
        "yml_path",
    }

    steps: list[Step | Workflow]
    process_name: str
    _inputs: ParameterStore[InputParameter]
    _outputs: ParameterStore[OutputParameter]
    inputs: ParameterNamespace[InputParameter, WorkflowInputReference]
    outputs: ParameterNamespace[OutputParameter, OutputParameter]
    yml_path: Path | None

    def __init__(self, steps: Sequence[Step | Workflow], workflow_name: str):
        """Create a workflow from steps and/or nested subworkflows.

        Args:
            steps (Sequence[Step | Workflow]): Child workflow nodes in execution order.
            workflow_name (str): User-facing workflow name.

        Returns:
            None: The workflow is initialized in place.
        """
        object.__setattr__(self, "steps", list(steps))
        object.__setattr__(self, "process_name", _normalize_workflow_name(workflow_name))
        object.__setattr__(self, "_inputs", ParameterStore())
        object.__setattr__(self, "_outputs", ParameterStore())
        object.__setattr__(
            self,
            "inputs",
            _parameter_namespace(
                self._inputs,
                self._input_reference,
                self._bind_input_from_namespace,
                read_only_error="",
            ),
        )
        object.__setattr__(
            self,
            "outputs",
            _parameter_namespace(
                self._outputs,
                self.add_output,
                self._bind_output_from_namespace,
                read_only_error="",
            ),
        )
        object.__setattr__(self, "yml_path", None)

    def __repr__(self) -> str:
        return f"Workflow(process_name={self.process_name!r}, steps={len(self.steps)})"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._SYSTEM_ATTRS:
            object.__setattr__(self, name, value)
            return

        if "_inputs" in self.__dict__:
            if name in self._outputs:
                self.bind_output(name, value)
                return
            self.bind_input(name, value)
            return

        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self._input_reference(name, implicit=True)

    def _ensure_input(self, name: str, parameter_type: Any = None, *, implicit: bool = True) -> InputParameter:
        def create_input(parameter_name: str) -> InputParameter:
            logger.warning("Adding a new input %s to workflow %s", parameter_name, self.process_name)
            if implicit:
                _warn_implicit_workflow_parameter(self, parameter_name, "input")
            return InputParameter(parameter_name, parameter_type, parent_obj=self)

        input_parameter = self._inputs.ensure(name, create_input)
        _resolve_parameter_type(
            input_parameter,
            parameter_type,
            context=f"{self.process_name}.inputs.{name}",
        )
        return input_parameter

    def _input_reference(self, name: str, *, implicit: bool = False) -> WorkflowInputReference:
        return WorkflowInputReference(self, name, implicit=implicit)

    def add_input(self, name: str, parameter_type: Any = None) -> InputParameter:
        """Declare a workflow input explicitly.

        Args:
            name (str): The workflow input name.
            parameter_type (Any): Optional CWL type expression for the input.

        Returns:
            InputParameter: The created or existing workflow input parameter.
        """
        return self._ensure_input(name, parameter_type=parameter_type, implicit=False)

    def add_output(
        self,
        name: str,
        source: Any = None,
        *,
        parameter_type: Any = None,
        implicit: bool = False,
    ) -> OutputParameter:
        """Declare a workflow output explicitly.

        Args:
            name (str): The workflow output name.
            source (Any): Optional step output or workflow input reference to expose.
            parameter_type (Any): Optional CWL type expression for the output.

        Returns:
            OutputParameter: The created or existing workflow output parameter.
        """
        def create_output(parameter_name: str) -> OutputParameter:
            logger.warning("Adding a new output %s to workflow %s", parameter_name, self.process_name)
            if implicit:
                _warn_implicit_workflow_parameter(self, parameter_name, "output")
            return OutputParameter(parameter_name, parameter_type, parent_obj=self)

        output_parameter = self._outputs.ensure(name, create_output)
        _resolve_parameter_type(
            output_parameter,
            parameter_type,
            context=f"{self.process_name}.outputs.{name}",
        )
        if source is not None:
            self.bind_output(name, source)
        return output_parameter

    def bind_input(self, name: str, value: Any) -> None:
        """Bind a literal value or upstream output to a workflow input.

        Args:
            name (str): The workflow input name.
            value (Any): A literal value, workflow reference, or step output.

        Returns:
            None: The workflow is mutated in place.
        """
        self._ensure_input(name)
        _bind_process_input(self, name, value)

    def _bind_input_from_namespace(self, name: str, value: Any) -> None:
        self._ensure_input(name, implicit=False)
        _bind_process_input(self, name, value)

    def bind_output(self, name: str, value: Any) -> None:
        """Bind a named workflow output to a step output or workflow input.

        Args:
            name (str): The workflow output name.
            value (Any): A step output or workflow input reference to expose.

        Returns:
            None: The workflow is mutated in place.
        """
        _bind_workflow_output(self, name, value)

    def _bind_output_from_namespace(self, name: str, value: Any) -> None:
        self.add_output(name, implicit=False)
        _bind_workflow_output(self, name, value)

    def get_inp_attr(self, name: str) -> InputParameter:
        """Return a named workflow input, creating it if needed.

        Args:
            name (str): The workflow input name.

        Returns:
            InputParameter: The created or existing workflow input parameter.
        """
        return self._ensure_input(name)

    def append(self, step_: Any) -> None:
        """Append a step or nested workflow to this workflow.

        Args:
            step_ (Any): The `Step` or `Workflow` to append.

        Raises:
            TypeError: If `step_` is neither a `Step` nor a `Workflow`.

        Returns:
            None: The workflow is mutated in place.
        """
        match step_:
            case Step() | Workflow():
                self.steps.append(step_)
            case _:
                raise TypeError("step must be either a Step or a Workflow")

    def _validate(self) -> None:
        for output_parameter in self._outputs:
            if not output_parameter.has_source():
                raise InvalidStepError(f"{self.process_name} has unbound output {output_parameter.name!r}")
        for step in self.steps:
            try:
                step._validate()
            except Exception as exc:
                raise InvalidStepError(f"{step.process_name} is missing required inputs") from exc

    @property
    def yaml(self) -> dict[str, Any]:
        """Return the in-memory WIC YAML representation of this workflow.

        Returns:
            dict[str, Any]: A WIC-compatible YAML tree represented as a Python dict.
        """
        return _workflow_document(self, inline_subtrees=True)

    def write_ast_to_disk(self, directory: Path) -> None:
        """Write this workflow tree to disk as `.wic` files.

        Args:
            directory (Path): Directory where the workflow AST should be written.

        Returns:
            None: Files are written to disk as a side effect.
        """
        _write_workflow_ast_to_disk(self, directory)

    def flatten_steps(self) -> list[Step]:
        """Return every concrete step in this workflow tree.

        Returns:
            list[Step]: All `Step` instances reachable from this workflow.
        """
        return [step for child in self.steps for step in child.flatten_steps()]

    def flatten_subworkflows(self) -> list[Workflow]:
        """Return this workflow and all nested subworkflows.

        Returns:
            list[Workflow]: This workflow followed by nested subworkflows.
        """
        return [self, *[workflow for child in self.steps for workflow in child.flatten_subworkflows()]]

    def compile(self, write_to_disk: bool = False, *, tool_registry: Tools | None = None) -> CompilerInfo:
        """Compile this workflow into CWL.

        Args:
            write_to_disk (bool): Whether to also write generated CWL to `autogenerated/`.
            tool_registry (Tools | None): Optional tool registry override.

        Returns:
            CompilerInfo: The compiler result tree for this workflow.
        """
        return _compile_workflow(self, write_to_disk=write_to_disk, tool_registry=tool_registry)

    def get_cwl_workflow(self, *, tool_registry: Tools | None = None) -> Json:
        """Return the compiled CWL workflow JSON and generated input object.

        Args:
            tool_registry (Tools | None): Optional tool registry override.

        Returns:
            Json: A JSON-serializable representation of the compiled CWL workflow.
        """
        return _compiled_cwl_json(self, tool_registry=tool_registry)

    def run(
        self,
        run_args_dict: dict[str, str] | None = None,
        user_env_vars: dict[str, str] | None = None,
        basepath: str = "autogenerated",
        tool_registry: Tools | None = None,
    ) -> None:
        """Compile and execute this workflow locally.

        Args:
            run_args_dict (dict[str, str] | None): Runtime CLI options for local execution.
            user_env_vars (dict[str, str] | None): Environment variables to expose to the run.
            basepath (str): Directory used for generated files and execution artifacts.
            tool_registry (Tools | None): Optional tool registry override.

        Returns:
            None: The workflow is executed as a side effect.
        """
        _run_workflow(
            self,
            run_args_dict=run_args_dict,
            user_env_vars=user_env_vars,
            basepath=basepath,
            tool_registry=tool_registry,
        )

    def _as_workflow_step(self, *, inline_subtrees: bool, directory: Path | None = None) -> dict[str, Any]:
        # Nested workflows are serialized in one of two ways:
        # 1. inline during in-memory compilation (`subtree`)
        # 2. as sibling `.wic` files when writing an AST to disk
        bound_inputs = {port.name: port.to_yaml_value() for port in self._inputs if port.is_bound()}
        parentargs = {"in": bound_inputs} if bound_inputs else {}
        if inline_subtrees:
            return {"id": f"{self.process_name}.wic", "subtree": self.yaml, "parentargs": parentargs}
        if directory is None:
            raise ValueError("directory is required when serializing subworkflows to disk")
        self.write_ast_to_disk(directory)
        return {"id": f"{self.process_name}.wic", **parentargs}
