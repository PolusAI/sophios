# pylint: disable=logging-fstring-interpolation,too-many-lines,protected-access
"""Python API for building Sophios workflows that compile to CWL."""

import logging
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, cast, overload

import graphviz
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool

from sophios.lang import CwlRecord, EdgeRef, InputValue, OpaqueCwl, UnresolvedName, nodes
from sophios.lang.compatibility import TypeRelation, reference_relation
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.lang.names import check_written_name
from sophios.lang.spans import SourceSpan
from sophios.lang.versions import KNOWN_VERSIONS
from sophios.nf_types import ExecutableNextflowWorkflow
from sophios.run_status import RunResult, RunState, RunStatus, StepStatus
from sophios.wic_types import DEFAULT_BASEPATH, Tools

from ._compiled import CompiledWorkflow
from ._inference import Inference
from ._errors import (
    ApiError,
    InvalidCLTError,
    InvalidInferenceError,
    InvalidInputValueError,
    InvalidLinkError,
    InvalidStepError,
    WorkflowRunError,
)
from ._ports import (
    InputParameter,
    InputBinding,
    OutputSourceBinding,
    OutputParameter,
    ParameterNamespace,
    ParameterStore,
    WorkflowInputReference,
    _record_type,
)
from ._utils import (
    caller_span as _caller_span,
    load_yaml as _load_yaml,
    serialize_value as _serialize_value,
)
from ._from_wic import workflow_from_wic as _workflow_from_wic
from ._types import ScatterMethod
from ._workflow_runtime import (
    RunHandle,
    RunOptions as _RunOptions,
    coerce_path as _coerce_path,
    compiled_workflow as _compiled_workflow,
    draw_workflow as _draw_workflow,
    load_clt_document as _load_clt_document,
    load_clt as _load_clt,
    lookup_parameter as _lookup_parameter,
    normalize_workflow_name as _normalize_workflow_name,
    nextflow_workflow as _nextflow_workflow,
    populate_parameters as _populate_parameters,
    run_workflow as _run_workflow,
    run_workflow_async as _run_workflow_async,
    silence_autodiscovery_logging as _silence_autodiscovery_logging,
    start_workflow as _start_workflow,
    step_run as _step_run,
    validate_step_assignment as _validate_step_assignment,
    write_workflow_wic as _write_workflow_wic,
    write_nextflow_workflow as _write_nextflow_workflow,
)


logger = logging.getLogger("Sophios Python API")
_silence_autodiscovery_logging()


StrPath = str | Path
#: Re-exported so callers can catch structured compile and validation failures
#: without importing `sophios.lang.diagnostics`. Their `.diagnostics` carry
#: `wic0NN` when a document is wrong and `api0NN` when a reported API operation
#: fails. Ordinary Python call-contract errors remain built-in exceptions.
__all__ = [
    "ApiError",
    "CompiledWorkflow",
    "ExecutableNextflowWorkflow",
    "Inference",
    "InvalidCLTError",
    "InvalidInferenceError",
    "InvalidInputValueError",
    "InvalidLinkError",
    "InvalidStepError",
    "RunHandle",
    "RunResult",
    "RunState",
    "RunStatus",
    "SophiosError",
    "SophiosErrorCode",
    "Step",
    "StepInput",
    "StepStatus",
    "Workflow",
    "WorkflowRunError",
]


def _written_name(name: str, what: str) -> str:
    """`name`, which a step or workflow will be called in the compiled CWL, if a document may write it."""
    if (problem := check_written_name(name)) is not None:
        raise ValueError(f"{what} {name!r} {problem}")
    return name


def _tool_builder_source_name(value: Any) -> str | None:
    """Return the default step name for CommandLineTool-like objects."""
    match getattr(value, "name", None), getattr(value, "to_cwl_document", None):
        case str() as name, to_cwl_document if callable(to_cwl_document):
            return name
        case _:
            return None


def _python_api_types_match(parameter_type: Any, candidate_type: Any) -> bool:
    """Whether an eager API binding is not disjoint in every known version.

    A ``Workflow`` object has no resolved language version yet.  Rejecting only
    when every supported version proves disjoint keeps this convenience check
    from preempting the authoritative, resolved-version compiler judgment.

    It also has no *scatter* yet: ``scatter_on()`` runs after the binding, and
    lifts the sink to an array.  So the sink's declared type is not necessarily
    its effective one, and a pair disjoint as written may be exactly right once
    scattered.  Disjointness is only proven here when it holds for the sink as
    declared *and* for the sink lifted one array level — that is, when no
    scatter arrangement could make the binding valid.  Anything else is left to
    the compiler, which knows the scatter.
    """
    def proven_disjoint(sink_type: Any) -> bool:
        return all(
            reference_relation(candidate_type, sink_type, lang_version=lang_version)
            is TypeRelation.DISJOINT
            for lang_version in KNOWN_VERSIONS
        )

    return not (proven_disjoint(parameter_type)
                and proven_disjoint({'type': 'array', 'items': parameter_type}))


def _resolve_parameter_type(
    parameter: InputParameter | OutputParameter,
    candidate_type: Any,
    *,
    context: str,
) -> None:
    """Infer or validate a parameter type against a new candidate, as it is bound."""
    if candidate_type is None:
        return
    if parameter.parameter_type is None:
        parameter.set_parameter_type(candidate_type)
        return
    _check_declared_type(parameter, candidate_type, context=context)


def _check_declared_type(
    parameter: InputParameter | OutputParameter,
    candidate_type: Any,
    *,
    context: str,
) -> None:
    """Validate a parameter's declared type, if it has one, against a candidate. A workflow
    output calls this alone: undeclared, it takes its source's type when read."""
    if parameter.parameter_type is not None and candidate_type is not None \
            and not _python_api_types_match(parameter.parameter_type, candidate_type):
        raise InvalidLinkError(
            f"{context} has incompatible types: expected {parameter.parameter_type!r}, got {candidate_type!r}",
            span=_caller_span(),
        )


def _warn_implicit_workflow_parameter(workflow: "Workflow", name: str, kind: str) -> None:
    """Warn when compatibility syntax implicitly declares workflow interface."""
    prefer = (
        f"workflow.inputs.{name}, or workflow.inputs.{name}.as_type(...) to declare it"
        if kind == "input" else f"workflow.outputs.{name} = ..."
    )
    warnings.warn(
        (
            f"Implicitly declaring workflow {kind} {name!r} on {workflow.process_name!r}. "
            f"Prefer {prefer} so interface drift is easier to spot."
        ),
        UserWarning,
        stacklevel=3,
    )


_UNSET: Final = object()


@dataclass(frozen=True, slots=True)
class StepInput:  # pylint: disable=too-many-instance-attributes
    """A step input written as CWL's ``WorkflowStepInput``: the Python twin of ``!cwl {...}``.

    ``source`` is one step output or workflow input, or a list of them; the
    other fields are written to the emitted step verbatim, in CWL spelling.
    Bind it like any value:
    ``step.inputs.x = StepInput(source=[a.outputs.f, b.outputs.f], link_merge='merge_flattened')``.
    A ``StepInput`` may be bound to a name the tool does not declare, which is
    how ``when`` and ``value_from`` read an extra input.

    Raises:
        InvalidInputValueError: If a ``source`` entry is not a step output or a
            workflow input; a string is never a reference.
    """

    source: Any = None
    link_merge: str | None = None
    pick_value: str | None = None
    value_from: str | None = None
    default: Any = _UNSET
    load_contents: bool | None = None
    load_listing: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        self.sources()

    def sources(self) -> tuple[OutputParameter | WorkflowInputReference, ...]:
        """The source entries, each a step output or a workflow input."""
        if self.source is None:
            return ()
        entries = tuple(self.source) if isinstance(self.source, (list, tuple)) else (self.source,)
        for entry in entries:
            if not isinstance(entry, (OutputParameter, WorkflowInputReference)):
                raise InvalidInputValueError(
                    f"StepInput.source entries are step outputs or workflow inputs, not {type(entry).__name__}",
                    span=_caller_span())
        return entries

    def fields(self) -> tuple[tuple[str, Any], ...]:
        """The ``WorkflowStepInput`` fields set, other than ``source``, in CWL spelling."""
        found = (("linkMerge", self.link_merge), ("pickValue", self.pick_value), ("valueFrom", self.value_from),
                 ("loadContents", self.load_contents), ("loadListing", self.load_listing), ("label", self.label))
        fields = tuple((key, value) for key, value in found if value is not None)
        if self.default is not _UNSET:
            fields += (("default", _serialize_value(self.default)),)
        return fields


def _bind_process_input(process_self: Any, input_name: str, value: Any) -> None:
    input_port = process_self._get_input(input_name)
    span = _caller_span()

    # This is the central compatibility switchboard for the Python API:
    # - workflow.input_name means "formal workflow parameter"
    # - step.output_name means "link to upstream step output"
    # - everything else is treated as a literal inline value
    match value:
        case WorkflowInputReference(workflow=workflow, name=name, implicit=implicit):
            workflow_input = workflow._ensure_input(name, implicit=implicit)
            port_type = _boundary_type(input_port.parameter_type)
            if workflow_input.parameter_type is None:
                workflow_input.set_parameter_type(port_type)
            elif port_type is not None and not _python_api_types_match(port_type, workflow_input.parameter_type):
                # The workflow input is the source and the port the sink, which a scatter may lift.
                raise InvalidLinkError(
                    f"{process_self.process_name}.{input_name} has incompatible types: expected {port_type!r}, "
                    f"got {workflow.process_name}.inputs.{name} of type {workflow_input.parameter_type!r}",
                    span=span)
            input_port._set_binding(InputBinding("workflow", name, workflow_input, span))
        case StepInput() as record:
            _bind_record(process_self, input_name, input_port, record, span)
        case list() | tuple() | dict() if _holds_a_port(value):
            raise InvalidInputValueError(
                f"{process_self.process_name}.{input_name}: a port object or a StepInput is never a literal; "
                "bind a port directly, or several sources with StepInput(source=[...])", span=span)
        case OutputParameter() as output:
            _resolve_parameter_type(
                input_port,
                output.effective_type(),
                context=f"{process_self.process_name}.{input_name}",
            )
            anchor_name = output.ensure_anchor(f"{input_name}{process_self.process_name}")
            input_port._set_binding(InputBinding("alias", anchor_name, output, span))
        case _:
            input_port._set_binding(InputBinding("inline", value, span=span))


def _holds_a_port(value: Any) -> bool:
    """Whether a would-be literal holds a step output, workflow input or `StepInput` at any depth."""
    match value:
        case OutputParameter() | WorkflowInputReference() | StepInput():
            return True
        case list() | tuple():
            return any(_holds_a_port(item) for item in value)
        case dict():
            return any(_holds_a_port(item) for item in value.values())
        case _:
            return False


def _bind_record(process_self: Any, input_name: str, input_port: InputParameter, record: StepInput,
                 span: SourceSpan) -> None:
    """Bind `record` to `input_port`, at the Python line `span`: the language's record, over the
    port objects it names."""
    entries = record.sources()
    # Each source is its own edge, so each gets its own name. The `/` keeps it apart from
    # a plain link's `{input}{step}` name, such as that of the same input on a step `sink1`.
    built = CwlRecord(tuple(
        UnresolvedName(entry.name, span) if isinstance(entry, WorkflowInputReference)
        else EdgeRef(entry.ensure_anchor(f"{input_name}{process_self.process_name}/{position}"), span)
        for position, entry in enumerate(entries, start=1)), record.fields(), span)
    # A workflow input the record delivers as it is to a declared input carries that input's type,
    # as a plain binding's does. Otherwise the record gives it no type: one that nothing else types is `Any`.
    delivered = (_boundary_type(input_port.parameter_type)
                 if built.delivers_its_source and input_port.declared else None)
    ports = tuple(entry.workflow._ensure_input(entry.name, parameter_type=delivered, implicit=entry.implicit)
                  if isinstance(entry, WorkflowInputReference) else entry for entry in entries)
    if record.value_from is None and input_port.declared:
        _check_declared_type(input_port, _record_type(built, ports),
                             context=f"{process_self.process_name}.{input_name}")
    input_port._set_binding(InputBinding("record", built, ports, span))


def _boundary_type(parameter_type: Any) -> Any:
    """A tool's `stdin`/`stdout`/`stderr` shorthand as the `File` a workflow boundary must declare."""
    return 'File' if parameter_type in ('stdin', 'stdout', 'stderr') else parameter_type


def _bind_workflow_output(workflow: "Workflow", output_name: str, value: Any) -> None:
    output_parameter = workflow._add_output(output_name, implicit=True)
    span = _caller_span()
    match value:
        case OutputParameter(parent_obj=Step(process_name=process_name) | Workflow(process_name=process_name),
                             name=name) as source:
            _check_declared_type(
                output_parameter,
                source.effective_type(),
                context=f"{workflow.process_name}.outputs.{output_name}",
            )
            output_parameter.bind_source(OutputSourceBinding(process_name, name), source_parameter=source,
                                         span=span)
            source.linked = True
        case WorkflowInputReference(workflow=source_workflow, name=name, implicit=implicit) \
                if source_workflow is workflow:
            input_parameter = workflow._ensure_input(name, implicit=implicit)
            _check_declared_type(
                output_parameter,
                input_parameter.cwl_type(),
                context=f"{workflow.process_name}.outputs.{output_name}",
            )
            output_parameter.bind_source(OutputSourceBinding(None, name), source_parameter=input_parameter,
                                         span=span)
        case _:
            raise InvalidLinkError(
                "workflow outputs must be bound to a step output or a workflow input reference", span=span)


class _ProcessBase:  # pylint: disable=too-few-public-methods
    """Shared helpers for `Step`/`Workflow`; not meant to grow public surface of its own."""

    process_name: str
    _inputs: ParameterStore[InputParameter]

    def _lookup_input(self, name: str) -> InputParameter:
        return _lookup_parameter(self._inputs, name, owner_name=self.process_name, kind="input")

    def _bound_inputs(self) -> tuple[tuple[str, InputValue], ...]:
        return tuple((port.name, port._binding.to_input_value())
                     for port in self._inputs if port._binding is not None)

    def _output_rank(self) -> int:
        """The array layers this process adds to each of its outputs: none for a `Workflow`,
        whose outputs carry their sources' types, layers included."""
        return 0


class Step(_ProcessBase):
    """A workflow step backed by a CWL ``CommandLineTool``.

    The canonical binding surface is explicit: values enter through
    ``step.inputs.*`` and leave through ``step.outputs.*``. Attribute sugar
    (``step.x = ...``) binds the input of that name; there is no string-keyed method.
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
        "_span",
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
    _span: SourceSpan
    inputs: ParameterNamespace[InputParameter, InputParameter]
    outputs: ParameterNamespace[OutputParameter, OutputParameter]
    scatter: list[InputParameter]
    scatterMethod: str
    when: str

    @overload
    def __init__(
        self,
        source: StrPath,
        step_inputs_file: StrPath | None = None,
        *,
        clt_path: None = None,
        step_name: str | None = None,
        tool_registry: Tools | None = None,
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        source: None = None,
        step_inputs_file: StrPath | None = None,
        *,
        clt_path: StrPath,
        step_name: str | None = None,
        tool_registry: Tools | None = None,
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        source: Any,
        step_inputs_file: None = None,
        *,
        clt_path: None = None,
        step_name: str | None = None,
        tool_registry: Tools | None = None,
    ) -> None:
        ...

    def __init__(
        self,
        source: Any | None = None,
        step_inputs_file: StrPath | None = None,
        *,
        clt_path: StrPath | None = None,
        step_name: str | None = None,
        tool_registry: Tools | None = None,
    ) -> None:
        """Create a ``Step`` from a CWL file or CommandLineTool-like object.

        Args:
            source (Any | None): CommandLineTool-like object, or a positional
                CWL path retained for compatibility.
            clt_path (StrPath | None): Explicit path to a CWL tool definition.
                This is the preferred file-backed constructor spelling.
            step_inputs_file (StrPath | None): A YAML file that maps this step's input
                names to values, each bound as it is written. For a local run, a
                relative path in it is read from the working directory, as a path
                bound in Python is.
            step_name (str | None): Optional workflow step name override.
            tool_registry (Tools | None): Optional fallback registry for known tools.
                CommandLineTool-like objects must expose ``name`` and
                ``to_cwl_document()``, such as
                ``tool_builder.CommandLineTool``.

        Raises:
            TypeError: If the source or step_inputs_file uses an unsupported type.
            ValueError: If the step's name has a character other than letters, digits,
                ``_``, ``.`` and ``-``, or starts with ``.``.
            InvalidCLTError: If the CWL tool cannot be loaded from disk or the registry.

        Returns:
            None: The step is initialized in place.
        """
        resolved_registry = {} if tool_registry is None else tool_registry
        if source is not None and clt_path is not None:
            raise TypeError("Step accepts either source or clt_path, not both")
        if source is None:
            source = clt_path

        match source:
            case str() | Path() as path:
                clt_path_ = _coerce_path(path, field_name="clt_path")
                step_inputs_file_ = _coerce_path(step_inputs_file, field_name="step_inputs_file", allow_none=True)
                assert clt_path_ is not None
                clt, yaml_file = _load_clt(clt_path_, resolved_registry)
                cfg_yaml = _load_yaml(step_inputs_file_) if step_inputs_file_ is not None else {}

                self._initialize_loaded_tool(
                    clt=clt,
                    yaml_file=yaml_file,
                    clt_path=clt_path_,
                    cfg_yaml=cfg_yaml,
                    tool_registry=resolved_registry,
                    process_name=step_name,
                )
            case _ if (tool_name := _tool_builder_source_name(source)) is not None:
                if step_inputs_file is not None:
                    raise TypeError("step_inputs_file is only supported when Step is created from a CWL file path")
                assert source is not None
                resolved_name = step_name or tool_name
                run_path = Path(f"{resolved_name}.cwl")
                match source.to_cwl_document():
                    case Mapping() as document:
                        clt, yaml_file = _load_clt_document(document, run_path=run_path)
                    case _:
                        raise TypeError(
                            "CommandLineTool-like Step source must return a mapping from to_cwl_document()"
                        )

                self._initialize_loaded_tool(
                    clt=clt,
                    yaml_file=yaml_file,
                    clt_path=run_path,
                    cfg_yaml={},
                    tool_registry=resolved_registry,
                    process_name=resolved_name,
                )
            case _:
                raise TypeError("Step requires clt_path or a CommandLineTool-like source")

    @classmethod
    def from_cwl_document(
        cls,
        document: Mapping[str, Any],
        *,
        process_name: str | None = None,
        run_path: StrPath | None = None,
        step_inputs: Mapping[str, Any] | None = None,
        tool_registry: Tools | None = None,
    ) -> "Step":
        # pylint: disable=too-many-arguments
        """Create a ``Step`` from an in-memory CWL CommandLineTool document.

        Args:
            document (Mapping[str, Any]): Parsed CWL CommandLineTool fields.
            process_name (str | None): Optional step name override.
            run_path (StrPath | None): Optional virtual ``.cwl`` path for compiler bookkeeping.
            step_inputs (Mapping[str, Any] | None): Input values to bind on the step, by input
                name, as written.
            tool_registry (Tools | None): Optional tool registry retained on the step.

        Raises:
            TypeError: If ``run_path`` uses an unsupported type.
            ValueError: If ``process_name`` has a character other than letters, digits,
                ``_``, ``.`` and ``-``, or starts with ``.``.
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
            cfg_yaml=dict(step_inputs or {}),
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
        """Populate a step from an already parsed CLT and optional step inputs."""
        resolved_name = _written_name(process_name, "step name") if process_name else clt_path.stem

        self.clt = clt
        self.clt_path = clt_path
        self.process_name = resolved_name
        self.cwl_version = cast(str, clt.cwlVersion)
        self.yaml = yaml_file
        self.cfg_yaml = dict(cfg_yaml)
        self._tool_registry = tool_registry
        self._span = _caller_span()
        self._inputs = ParameterStore()
        self._outputs = ParameterStore()
        # This proxy is the main bit of API "magic": it supports both
        # list-style access (`step.inputs[0]`) and named attribute access
        # (`step.inputs.message`) without duplicating wrapper classes.
        self.inputs = ParameterNamespace(self._inputs, self._get_input, self._bind_input, read_only_error="")
        self.outputs = ParameterNamespace(
            self._outputs,
            self._get_output,
            None,
            read_only_error="Step outputs are read-only; cannot set {name!r}",
        )
        self.scatter = []
        self.scatterMethod = ""
        self.when = ""

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
            self._bind_input(name, value)
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

    def _bind_input(self, name: str, value: Any) -> None:
        """Bind a value or upstream output to a named step input parameter.

        A ``StepInput`` bound to a name the tool does not declare creates that
        input, typed ``Any``, as a ``!cwl`` record does in a ``.wic`` step.

        Args:
            name (str): The input parameter name.
            value (Any): A literal value, a workflow input reference, a step output, or a ``StepInput``.

        Raises:
            AttributeError: If the tool does not declare the input and ``value`` is not a ``StepInput``.

        Returns:
            None: The step is mutated in place.
        """
        if isinstance(value, StepInput):
            self._inputs.ensure(name, self._undeclared_input)
        elif not _lookup_parameter(self._inputs, name, owner_name=self.process_name, kind="input").declared:
            raise AttributeError(f"{self.process_name!r} has no input named {name!r}; "
                                 "only a StepInput binds an input the tool does not declare")
        _bind_process_input(self, name, value)

    def _undeclared_input(self, name: str) -> InputParameter:
        """A step input the tool does not declare, for a ``StepInput`` to bind."""
        port = InputParameter(name, "Any", parent_obj=self)
        port.declared = False
        return port

    def scatter_on(
        self,
        *inputs: InputParameter,
        method: str | ScatterMethod = ScatterMethod.dotproduct.value,
    ) -> "Step":
        """Scatter this step over one or more already-bound input parameters.

        Args:
            inputs (InputParameter): Input parameters on this step to scatter over.
            method (str | ScatterMethod): CWL scatter method. Defaults to ``dotproduct``.

        Returns:
            Step: This step, so calls can be chained in small examples.
        """
        scatter_method = method.value if isinstance(method, ScatterMethod) else method
        self.scatter = list(inputs)
        self.scatterMethod = scatter_method
        return self

    def _output_rank(self) -> int:
        """The array layers this step's scatter adds to each of its outputs: none without
        a scatter, one per scattered port under `nested_crossproduct`, else one.
        The Python-side twin of `sophios.ir.declarations.output_rank`."""
        if not self.scatter:
            return 0
        return len(self.scatter) if self.scatterMethod == ScatterMethod.nested_crossproduct.value else 1

    def _get_input(self, name: str) -> InputParameter:
        """Return a named input parameter from this step."""
        return self._lookup_input(name)

    def _get_output(self, name: str) -> OutputParameter:
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
        """Bind each value as it is written: whatever runs the workflow checks the paths it names."""
        for name, value in self.cfg_yaml.items():
            setattr(self, name, value)

    def _validate(self) -> None:
        """Validate step-local settings before compilation.

        Required-but-unbound inputs are intentionally allowed here. The legacy
        Sophios compiler performs edge inference for missing step inputs, just
        as it does for `.wic` workflows.
        """
        return None

    def _flatten_steps(self) -> "list[Step]":
        """Return this step as a single-item list for recursive traversal."""
        return [self]

    def _flatten_subworkflows(self) -> "list[Workflow]":
        """Return an empty subworkflow list because steps do not nest workflows."""
        return []

    def _as_workflow_step(self) -> nodes.Step:
        """Return this step as the language's step node, with `run:` naming its tool."""
        interpreted: list[tuple[str, OpaqueCwl]] = [("run", _step_run(self.clt_path))]
        if self.scatter:
            interpreted += [("scatter", [input_port.name for input_port in self.scatter]),
                            ("scatterMethod", self.scatterMethod or ScatterMethod.dotproduct.value)]
        if self.when:
            interpreted.append(("when", self.when))
        return nodes.Step(
            id=self.process_name,
            inputs=self._bound_inputs(),
            outputs=tuple(nodes.OutputBinding(port.name, nodes.EdgeDef(port._anchor_name, self._span), self._span)
                          for port in self._outputs if port._anchor_name is not None),
            interpreted=tuple(interpreted),
            span=self._span,
        )


class Workflow(_ProcessBase):  # pylint: disable=too-many-instance-attributes
    """A Sophios workflow composed from ``Step`` objects and nested ``Workflow`` objects."""

    _SYSTEM_ATTRS: ClassVar[set[str]] = {
        "steps",
        "process_name",
        "_inputs",
        "_outputs",
        "_span",
        "inputs",
        "outputs",
        "yml_path",
    }

    steps: "list[Step | Workflow]"
    process_name: str
    _inputs: ParameterStore[InputParameter]
    _outputs: ParameterStore[OutputParameter]
    _span: SourceSpan
    inputs: ParameterNamespace[InputParameter, WorkflowInputReference]
    outputs: ParameterNamespace[OutputParameter, OutputParameter]
    yml_path: Path | None

    def __init__(self, steps: "Sequence[Step | Workflow]", workflow_name: str):
        """Create a workflow from steps and/or nested subworkflows.

        Args:
            steps (Sequence[Step | Workflow]): Child workflow nodes in execution order.
            workflow_name (str): User-facing workflow name.

        Raises:
            ValueError: If the name, with ``/`` and spaces made ``_``, has a character other
                than letters, digits, ``_``, ``.`` and ``-``, or starts with ``.``.

        Returns:
            None: The workflow is initialized in place.
        """
        self.steps = list(steps)
        self.process_name = _normalize_workflow_name(workflow_name)
        if (problem := check_written_name(self.process_name)) is not None:
            raise ValueError(f"workflow name {workflow_name!r} {problem}")
        self._span = _caller_span()
        self._inputs = ParameterStore()
        self._outputs = ParameterStore()
        self.inputs = ParameterNamespace(
            self._inputs,
            self._input_reference,
            self._bind_input_from_namespace,
            read_only_error="",
        )
        self.outputs = ParameterNamespace(
            self._outputs,
            self._add_output,
            self._bind_output_from_namespace,
            read_only_error="",
        )
        self.yml_path = None

    def __repr__(self) -> str:
        return f"Workflow(process_name={self.process_name!r}, steps={len(self.steps)})"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._SYSTEM_ATTRS:
            object.__setattr__(self, name, value)
            return

        if "_inputs" in self.__dict__:
            if name in self._outputs:
                self._bind_output(name, value)
                return
            self._bind_input(name, value)
            return

        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in ("yaml", "to_wic_yaml"):
            raise AttributeError(f"Workflow.{name} was removed; use Workflow.write_wic() to write a .wic file")
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

    def _add_output(
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
            self._bind_output(name, source)
        return output_parameter

    def _bind_input(self, name: str, value: Any) -> None:
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

    def _bind_output(self, name: str, value: Any) -> None:
        """Bind a named workflow output to a step output or workflow input.

        Args:
            name (str): The workflow output name.
            value (Any): A step output or workflow input reference to expose.

        Returns:
            None: The workflow is mutated in place.
        """
        _bind_workflow_output(self, name, value)

    def _bind_output_from_namespace(self, name: str, value: Any) -> None:
        self._add_output(name, implicit=False)
        _bind_workflow_output(self, name, value)

    def _get_input(self, name: str) -> InputParameter:
        """Return a named workflow input, creating it if needed."""
        return self._ensure_input(name)

    def _validate_graph_shape(self) -> None:
        names: set[str] = set()
        for child in self.steps:
            if child.process_name in names:
                raise InvalidStepError(
                    f"{self.process_name} has duplicate step name {child.process_name!r}; "
                    "pass step_name=... when reusing the same tool in one workflow", span=child._span)
            names.add(child.process_name)

        prior_children: set[Step | Workflow] = set()
        children = set(self.steps)
        for child in self.steps:
            for input_parameter in child._inputs:
                for source_parameter in input_parameter.source_outputs():
                    source_parent = source_parameter.parent_obj
                    if source_parent is self:
                        raise InvalidLinkError(
                            f"{child.process_name}.{input_parameter.name} is bound to "
                            f"{self.process_name}.outputs.{source_parameter.name}, this workflow's own output; "
                            "a workflow cannot consume what it produces", span=child._span)
                    source_process = getattr(source_parent, "process_name", "<unknown>")
                    if source_parent not in children:
                        raise InvalidStepError(
                            f"{child.process_name}.{input_parameter.name} is linked to "
                            f"{source_process}.{source_parameter.name}, "
                            f"but {source_process!r} is not a child of {self.process_name!r}", span=child._span)
                    if source_parent not in prior_children:
                        raise InvalidStepError(
                            f"{child.process_name}.{input_parameter.name} is linked to "
                            f"{source_process!r}, which must appear earlier in the workflow step list",
                            span=child._span)
            prior_children.add(child)

        for output_parameter in self._outputs:
            match output_parameter._source_parameter:
                case OutputParameter(parent_obj=source_parent) if source_parent is self:
                    raise InvalidLinkError(
                        f"{self.process_name}.outputs.{output_parameter.name} is bound to one of this "
                        "workflow's own outputs", span=output_parameter._span)
                case OutputParameter(parent_obj=source_parent) if source_parent not in children:
                    raise InvalidStepError(
                        f"{self.process_name}.outputs.{output_parameter.name} is linked to "
                        f"{source_parent.process_name!r}, which is not a child of {self.process_name!r}",
                        span=output_parameter._span)
                case _:
                    pass

    def _validate(self) -> None:
        self._validate_graph_shape()
        for output_parameter in self._outputs:
            if not output_parameter.has_source():
                raise InvalidStepError(f"{self.process_name} has unbound output {output_parameter.name!r}",
                                       span=self._span)
        for step in self.steps:
            try:
                step._validate()
            except Exception as exc:
                raise InvalidStepError(f"{step.process_name} is invalid", span=step._span) from exc

    def write_wic(self, path: StrPath | None = None) -> Path:
        """Write this workflow as ``.wic``: the one way the Python API writes it.

        The bundle is the root ``<name>.wic``, one ``<child>.wic`` per nested
        workflow and one ``<stem>.cwl`` per distinct tool. Every step carries
        ``run: <stem>.cwl`` and every workflow output names its authored step.
        A real-time analysis declaration is the exception: it carries
        ``run: cwl_subinterpreter``, the registry's adapter, which is not
        written, so the bundle still declares the analysis. Only the tool
        document is written: files it refers to by relative path (such as a
        ``$import``) are not copied. Raises if ``<stem>.cwl`` already exists in
        the directory with a different tool.

        Args:
            path (StrPath | None): Destination ``.wic`` path or output
                directory. When omitted, writes ``<workflow>.wic`` in the
                current directory.

        Returns:
            Path: The root ``.wic`` file that was written.
        """
        return _write_workflow_wic(self, path)

    @classmethod
    def from_wic(
        cls,
        path: StrPath,
        *,
        tool_registry: Tools | None = None,
        workflow_paths: Mapping[str, Mapping[str, Path]] | None = None,
    ) -> "Workflow":
        """Build ``Workflow`` and ``Step`` objects from a ``.wic`` file.

        The file is read the way ``sophios --yaml`` reads it. A step's tool
        comes from ``tool_registry`` by name, or from what its ``run:`` names;
        a nested ``<child>.wic`` comes from ``workflow_paths``, shaped
        ``{namespace: {stem: path}}`` like ``sophios.plugins.get_yml_paths``.
        Nothing else is read: no config, and nothing beside the document that
        ``run:`` does not name.

        The file is compiled first, and its compile errors are raised as they
        are. Then each step becomes a ``Step`` holding its tool, or a nested
        ``Workflow``; ``!ii`` becomes a literal, ``!*`` a step output, a bare
        name a workflow input and ``!cwl {...}`` a ``StepInput``. An input the
        file leaves to inference stays unbound, and ``compile()`` infers it.
        Every object carries the ``.wic`` line it was read from.

        A construct the Python API has no spelling for, such as a repeated
        step id, an edge from another document or a ``wic:`` namespace, is
        refused with ``api006``; every one in the file is reported at once.
        ``wic: graphviz`` and ``wic: inlineable`` change nothing a Python
        workflow compiles or runs: they are dropped with a ``UserWarning``.

        Args:
            path (StrPath): The root ``.wic`` file.
            tool_registry (Tools | None): The tools a step may name.
            workflow_paths (Mapping[str, Mapping[str, Path]] | None): The
                ``.wic`` files a step may call.

        Raises:
            SophiosError: The file's compile errors, or ``api006``.

        Returns:
            Workflow: The workflow the file describes.
        """
        root = _coerce_path(path, field_name="path")
        assert root is not None
        return _workflow_from_wic(root, tool_registry or {}, workflow_paths or {})

    def _flatten_steps(self) -> list[Step]:
        """Return every concrete step in this workflow tree."""
        return [step for child in self.steps for step in child._flatten_steps()]

    def _flatten_subworkflows(self) -> "list[Workflow]":
        """Return this workflow and all nested subworkflows."""
        return [self, *[workflow for child in self.steps for workflow in child._flatten_subworkflows()]]

    @overload
    def compile(
        self,
        *,
        target: Literal["cwl"] = "cwl",
        tool_registry: Tools | None = None,
        lang_version: str | None = None,
        inference: Inference = Inference(),
    ) -> CompiledWorkflow:
        ...

    @overload
    def compile(
        self,
        *,
        target: Literal["nextflow"],
        tool_registry: Tools | None = None,
        lang_version: str | None = None,
        inference: Inference = Inference(),
    ) -> ExecutableNextflowWorkflow:
        ...

    def compile(
        self,
        *,
        target: Literal["cwl", "nextflow"] = "cwl",
        tool_registry: Tools | None = None,
        lang_version: str | None = None,
        inference: Inference = Inference(),
    ) -> CompiledWorkflow | ExecutableNextflowWorkflow:
        """Compile this workflow into the selected target representation.

        Args:
            target (Literal["cwl", "nextflow"]): Compilation target.
            tool_registry (Tools | None): Optional tool registry override.
            lang_version (str | None): Pin the Sophios language version for
                this compilation; None (the default) infers it. An explicit
                setting beats any file tag. The resolved version is reported
                on the result as ``CompiledWorkflow.lang_version``.
            inference (Inference): How an unbound input is connected to an
                earlier output. Unlike the command line, the Python API
                reads no configuration: renamings and format rules are empty
                unless passed, and ``insert_steps=True`` inserts only
                converters among ``tool_registry``. See :class:`Inference`
                for each option and its flag.

        Returns:
            CompiledWorkflow | ExecutableNextflowWorkflow: Target-specific result.
        """
        if target == "cwl":
            return _compiled_workflow(self, tool_registry=tool_registry, lang_version=lang_version,
                                      inference=inference)
        if target == "nextflow":
            return _nextflow_workflow(self, tool_registry=tool_registry, lang_version=lang_version,
                                      inference=inference)
        raise ValueError(f"unsupported compilation target {target!r}")

    def to_nextflow(
        self,
        outdir: StrPath,
        *,
        tool_registry: Tools | None = None,
        lang_version: str | None = None,
        inference: Inference = Inference(),
    ) -> tuple[Path, Path, Path, Path]:
        """Compile once and write the four supported Nextflow artifacts.

        ``inference`` connects unbound inputs as :meth:`compile` does.
        """
        return _write_nextflow_workflow(self, outdir, tool_registry=tool_registry, lang_version=lang_version,
                                        inference=inference)

    def draw(  # pylint: disable=too-many-arguments
        self,
        *,
        dark: bool = False,
        label_edges: bool = False,
        label_stepname: bool = False,
        inline_depth: int = sys.maxsize,
        inference: Inference = Inference(),
        tool_registry: Tools | None = None,
        lang_version: str | None = None,
    ) -> graphviz.Digraph:
        """Draw this workflow as a graph: a box per step and an edge per dependency.

        The workflow is compiled as :meth:`compile` and :meth:`run` compile it, so
        with the same ``inference``, ``tool_registry`` and ``lang_version`` the
        drawing shows the edges they would build. An edge you wrote is blue; one
        inference added takes the text colour, so you can tell what you said from
        what Sophios decided.

        In a Jupyter notebook, end a cell with the returned object and it
        renders inline as an image. Inline display and ``.render(...)`` both
        need the Graphviz ``dot`` program; without it the notebook shows an
        ``ExecutableNotFound`` traceback instead of the drawing. ``.source`` is
        the DOT text and needs nothing.

        The drawing carries its own opaque background, so it reads in a light or
        a dark notebook, in exported HTML and PDF, and on GitHub. ``dark`` picks
        which one. (The command line's ``--graphviz`` draws on a transparent
        background instead, for slides.)

        Args:
            dark (bool): Draw on a dark background with white text, outlines and
                inferred edges, and a lighter blue for edges you wrote; the
                default is a white background with black text, outlines and
                inferred edges, and blue edges you wrote.
            label_edges (bool): Draw one edge per port, labelled with the port's
                name, instead of one edge per pair of steps.
            label_stepname (bool): Label each box with the step's name in the
                compiled CWL instead of its id.
            inline_depth (int): How many levels of nested workflows to draw
                step by step; a call below that is one box. The default draws
                every level.
            inference (Inference): How an unbound input is connected to an
                earlier output; the Python API reads no configuration, so
                renamings and format rules are empty unless passed. See
                :class:`Inference`.
            tool_registry (Tools | None): Optional tool registry override.
            lang_version (str | None): Pin the Sophios language version for
                this compilation; None (the default) infers it.

        Returns:
            graphviz.Digraph: The drawing. ``.source`` is its DOT text.
        """
        return _draw_workflow(self, dark=dark, label_edges=label_edges, label_stepname=label_stepname,
                              inline_depth=inline_depth, inference=inference,
                              tool_registry=tool_registry, lang_version=lang_version)

    def run(  # pylint: disable=too-many-arguments
        self,
        run_args_dict: dict[str, str] | None = None,
        user_env_vars: dict[str, str] | None = None,
        basepath: str = DEFAULT_BASEPATH,
        tool_registry: Tools | None = None,
        *,
        workflow_paths: Mapping[str, Mapping[str, Path]] | None = None,
        inference: Inference = Inference(),
        target: Literal["cwl", "nextflow"] = "cwl",
    ) -> RunResult | None:
        """Compile and execute this workflow locally, and wait for it: :meth:`start`, then ``wait()``.

        A real-time analysis that is a ``.wic`` file comes from ``workflow_paths``,
        shaped ``{namespace: {stem: path}}`` like ``sophios.plugins.get_yml_paths``.

        A relative File or Directory path bound in Python is read from the working
        directory, as Python reads one.

        The run writes the one document ``compile()`` returns to
        ``<basepath>/<name>.cwl``, with its job file beside it, and runs exactly
        that. With ``target="nextflow"`` it writes what :meth:`to_nextflow`
        writes to ``basepath`` and runs that with Nextflow; the outputs are in
        ``<basepath>/work_<name>``, Nextflow's work directory.

        Args:
            run_args_dict (dict[str, str] | None): Runtime CLI options for local execution,
                by flag name without the dashes. ``quiet`` (``'yes'`` by default) and
                ``no_provenance`` (``'no'`` by default) are yes/no switches. Under
                ``target="nextflow"`` a key only a CWL run reads is refused, and each key
                Sophios does not read reaches ``nextflow run`` as ``-<key> <value>``, or
                ``-<key>`` alone for an empty value:
                ``{'process.executor': 'slurm', 'resume': ''}``.
            user_env_vars (dict[str, str] | None): Environment variables to expose to the run.
            basepath (str): Directory used for generated files and execution artifacts.
            tool_registry (Tools | None): Optional tool registry override.
            workflow_paths (Mapping[str, Mapping[str, Path]] | None): The
                ``.wic`` files a real-time analysis may be.
            inference (Inference): How an unbound input is connected; the run
                infers the same edges ``compile(inference=...)`` does.
            target (Literal["cwl", "nextflow"]): What the workflow is run as: CWL, by
                ``cwl_runner``, or the pipeline the Nextflow target lowers it to, by Nextflow.

        Returns:
            RunResult | None: The completed run: its outputs, output directory and log
            file. None when ``generate_run_script`` wrote ``run.sh`` instead of running.

        Raises:
            SophiosError: The workflow does not compile, or this machine cannot run it.
            ValueError: If a `user_env_vars` key is not an environment variable name, or
                `run_args_dict` names a `cwl_runner` Sophios cannot run, or under
                ``target="nextflow"`` `run_args_dict` has a key only a CWL run reads or
                the workflow has no Nextflow lowering.
            WorkflowRunError: If the runner exits non-zero. Its `exit_code` is the runner's.
            KeyboardInterrupt: On Ctrl-C, after the runner was stopped.
        """
        return _run_workflow(self, _RunOptions(run_args_dict, user_env_vars, basepath, tool_registry,
                                               workflow_paths, inference, target))

    def start(  # pylint: disable=too-many-arguments
        self,
        run_args_dict: dict[str, str] | None = None,
        user_env_vars: dict[str, str] | None = None,
        basepath: str = DEFAULT_BASEPATH,
        tool_registry: Tools | None = None,
        *,
        workflow_paths: Mapping[str, Mapping[str, Path]] | None = None,
        inference: Inference = Inference(),
        target: Literal["cwl", "nextflow"] = "cwl",
    ) -> RunHandle:
        """Compile this workflow, prepare its run, start its runner, and return without waiting for it.

        Everything before the runner starts happens before ``start`` returns, as in
        :meth:`run`: the compile, the checks of this machine and the image pulls, so
        their errors are raised here. The handle it returns is RUNNING, or FAILED when
        the runner could not be started; ``wait()``, ``status()``, ``logs()``,
        ``outputs()`` and ``cancel()`` follow it from there. The run lives as long as
        this Python process: when the interpreter exits with the run still going, the
        run is cancelled.

        Args:
            run_args_dict (dict[str, str] | None): As :meth:`run` takes them, except
                ``generate_run_script``: :meth:`run` and :meth:`run_async` write a run
                script, ``start`` has nothing to start then.
            user_env_vars (dict[str, str] | None): Environment variables to expose to the run.
            basepath (str): Directory used for generated files and execution artifacts.
            tool_registry (Tools | None): Optional tool registry override.
            workflow_paths (Mapping[str, Mapping[str, Path]] | None): The
                ``.wic`` files a real-time analysis may be.
            inference (Inference): How an unbound input is connected.
            target (Literal["cwl", "nextflow"]): As :meth:`run` takes it.

        Returns:
            RunHandle: The started run.

        Raises:
            SophiosError: The workflow does not compile, or this machine cannot run it.
            ValueError: As :meth:`run` raises it, and for ``generate_run_script``.
        """
        return _start_workflow(self, _RunOptions(run_args_dict, user_env_vars, basepath, tool_registry,
                                                 workflow_paths, inference, target))

    async def run_async(  # pylint: disable=too-many-arguments
        self,
        run_args_dict: dict[str, str] | None = None,
        user_env_vars: dict[str, str] | None = None,
        basepath: str = DEFAULT_BASEPATH,
        tool_registry: Tools | None = None,
        *,
        workflow_paths: Mapping[str, Mapping[str, Path]] | None = None,
        inference: Inference = Inference(),
        target: Literal["cwl", "nextflow"] = "cwl",
    ) -> RunResult | None:
        """:meth:`run`, as a coroutine that never blocks the event loop.

        The compile, the checks and the image pulls run in a worker thread, and the
        runner is awaited from one. Cancelling the task that awaits it stops the runner,
        as :meth:`RunHandle.cancel` does, before the cancellation goes on; a runner that
        had already exited is reported on as it ended.

        Real-time analyses never run this way: a workflow that declares one is refused
        with ``api008``, before anything is written or run. :meth:`run` and :meth:`start`
        run them.

        Args:
            run_args_dict (dict[str, str] | None): As :meth:`run` takes them.
            user_env_vars (dict[str, str] | None): Environment variables to expose to the run.
            basepath (str): Directory used for generated files and execution artifacts.
            tool_registry (Tools | None): Optional tool registry override.
            workflow_paths (Mapping[str, Mapping[str, Path]] | None): Unused here: they
                name real-time analyses, which are refused.
            inference (Inference): How an unbound input is connected.
            target (Literal["cwl", "nextflow"]): As :meth:`run` takes it.

        Returns:
            RunResult | None: As :meth:`run` returns it.

        Raises:
            SophiosError: As :meth:`run` raises it, and ``api008`` for real-time analyses.
            ValueError: As :meth:`run` raises it.
            WorkflowRunError: If the runner exits non-zero.
            asyncio.CancelledError: When the awaiting task is cancelled, after the runner was stopped.
        """
        return await _run_workflow_async(self, _RunOptions(run_args_dict, user_env_vars, basepath, tool_registry,
                                                           workflow_paths, inference, target))

    def _as_workflow_step(self) -> nodes.Step:
        """Return this workflow as a step naming its own ``.wic`` document, with its anchored outputs."""
        return nodes.Step(
            id=f"{self.process_name}.wic",
            inputs=self._bound_inputs(),
            outputs=tuple(nodes.OutputBinding(port.name, nodes.EdgeDef(port._anchor_name, port._span), port._span)
                          for port in self._outputs if port._anchor_name is not None),
            span=self._span,
        )
