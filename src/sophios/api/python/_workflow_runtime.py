"""Internal runtime helpers for the Python workflow API: loading, compiling and running, kept out of `workflow`."""

# pylint: disable=protected-access
# This module is the private adapter layer between workflow objects and the
# compiler/runtime boundary, so reaching internal state is intentional.

import argparse
import asyncio
import copy
import logging
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Final, NamedTuple, TypeVar

import graphviz
import yaml

from sophios import backends, cli, compiler, drawing, input_output, post_compile as pc, preflight, realtime
from sophios import run_local, run_setup, targets
from sophios.input_output import SHIPPED_RENAMINGS
from sophios.run_local import RunHandle
from sophios.run_status import RunResult
from sophios.ir.artifacts import CompilationResult
from sophios.ir.complete import coerce_job_value
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.infer import InferencePolicy
from sophios.ir.locations import absolute_paths
from sophios.ir.policy import RunPolicy
from sophios.ir.realtime import ADAPTER, Declaration
from sophios.ir.resolve import RegistrySnapshot
from sophios.ir.refusals import NotLowered
from sophios.ir.resolved import ResolvedWorkflow, bind
from sophios.ir.types import AuthoredName
from sophios.lang import CwlRecord, Diagnostic, Diagnostics, Document, ParseResult, Severity, SophiosError, render
from sophios.lang.cwl import Process, SCATTER_METHODS, ports_named, tool_process
from sophios.lang.diagnostics import report
from sophios.lang import Step as LangStep
from sophios.lang.error_codes import SophiosErrorCode
# Re-exported by `workflow`, which reaches the run settings only through this adapter.
from sophios.runners import Cwltool, Nextflow, Runner, RunSettings, Toil  # pylint: disable=unused-import
from sophios.runtime_inputs import output_target_inputs, resolve_file_formats, runner_job_inputs
from sophios.input_output_nf import nextflow_params, render_nextflow
from sophios.nf_types import ExecutableNextflowWorkflow
from sophios.utils_nf import nextflow_names
from sophios.wic_types import DEFAULT_BASEPATH, GraphSettings, Json, StepId, Tool, Tools, Yaml

from ._inference import Inference
from ._errors import InvalidCLTError, InvalidStepError
from ._compiled import CompiledNextflowWorkflow, CompiledWorkflow, _artifact_path
from ._ports import InputParameter, OutputParameter, ParameterStore
from ._utils import load_yaml as _load_yaml, serialize_value

if TYPE_CHECKING:
    from .workflow import Step, Workflow


logger = logging.getLogger("Sophios Python API")

ParameterT = TypeVar("ParameterT")

#: A run with every default: what the keywords of `Workflow.run` read theirs from.
RUN_DEFAULTS: Final = RunSettings(Cwltool())


def normalize_workflow_name(workflow_name: str) -> str:
    """Convert a user-facing workflow name into a filesystem-safe id."""
    normalized_name = workflow_name.lstrip("/").lstrip(" ")
    parts = PurePath(normalized_name).parts
    return "_".join(part for part in parts if part).lstrip("_").replace(" ", "_")


def lookup_parameter(
    parameters: ParameterStore[ParameterT],
    name: str,
    *,
    owner_name: str,
    kind: str,
) -> ParameterT:
    """Return a parameter from a named parameter store. `owner_name` and
    `kind` appear only in the `AttributeError` raised when it is absent."""
    try:
        return parameters.get(name)
    except KeyError as exc:
        raise AttributeError(f"{owner_name!r} has no {kind} named {name!r}") from exc


def _validate_scatter(items: list[Any], owner: Any) -> None:
    """Raise unless `items` are distinct, bound, array-valued inputs of `owner`."""
    if not all(isinstance(item, InputParameter) for item in items):
        raise TypeError("all scatter inputs must be InputParameter type")
    if len({id(item) for item in items}) != len(items):
        raise ValueError("scatter inputs must be unique")
    for item in items:
        if item.parent_obj is not owner:
            raise ValueError("scatter inputs must belong to the same step")
        if not item.is_bound():
            raise ValueError("scatter inputs must be bound before scattering")
        if not item.is_scatterable():
            raise ValueError("scatter inputs must be bound to array-valued data")


def validate_step_assignment(name: str, value: Any, *, owner: Any) -> None:
    """Validate assignments to special step attributes.

    Raises:
        TypeError: If `scatter` is not a list of `InputParameter` values.
        ValueError: If `scatterMethod` or `when` receive invalid values.
    """
    match name, value:
        case "scatter", list() as items:
            _validate_scatter(items, owner)
        case "scatter", invalid if invalid:
            raise TypeError("scatter must be assigned a list of InputParameter values")
        case "scatterMethod", str() as scatter_method if scatter_method:
            if scatter_method not in SCATTER_METHODS:
                raise ValueError(
                    "Invalid value for scatterMethod. "
                    f"Valid values are: {', '.join(sorted(SCATTER_METHODS))}"
                )
        case "when", condition if condition and not (
                isinstance(condition, str) and condition.startswith("$(") and condition.endswith(")")):
            raise ValueError("Invalid input to when. The js string must start with '$(' and end with ')'")


def populate_parameters(
    cwl_parameters: list[Any],
    store: ParameterStore[Any],
    parameter_cls: type[InputParameter] | type[OutputParameter],
    *,
    parent: Any,
) -> None:
    """Populate a parameter store from CWL input or output declarations."""
    for name, parameter in ports_named(cwl_parameters).items():
        store.add(parameter_cls(name, parameter.type_, parent_obj=parent))


def _loaded(tool: Tool) -> tuple[Process, dict[str, Any]]:
    """Load `tool` through the one cached tool loader (`Tool.loaded`), raising `InvalidCLTError` on `api004`."""
    try:
        process, _ = tool.loaded
        return process, tool.cwl
    except SophiosError as exc:
        if all(item.code is SophiosErrorCode.INVALID_TOOL for item in exc.diagnostics):
            raise InvalidCLTError(*(item.message for item in exc.diagnostics)) from exc
        raise


def load_clt(clt_path: Path, tool_registry: Tools) -> tuple[Process, dict[str, Any]]:
    """Load a tool, written in CWL or in `.wic`, from disk or a fallback registry, which then holds it under its stem.

    Raises:
        InvalidCLTError: If the tool cannot be loaded from disk or the registry.
        SophiosError: What the tool file is refused for, such as a packed `$graph` with no `main`.
    """
    stepid = StepId(clt_path.stem, "global")

    if clt_path.exists():
        tool = Tool(str(clt_path), from_file=clt_path.suffix != '.wic')
        loaded = _loaded(tool)
        # A registry key is fixed when its tool is found: a `.wic` never takes the key of a tool discovery named.
        if tool.document is None:
            tool_registry[stepid] = tool
        else:
            tool_registry.setdefault(stepid, tool)
        return loaded

    if stepid in tool_registry:
        tool = tool_registry[stepid]
        logger.info("%s does not exist, but %s was found in the provided tool registry.", clt_path, clt_path.stem)
        logger.info("Using file contents from %s", tool.run_path)
        return _loaded(tool)

    logger.warning("Warning! %s does not exist, and", clt_path)
    logger.warning("%s was not found in the provided tool registry.", clt_path.stem)
    raise InvalidCLTError(f"invalid cwl file: {clt_path}")


def load_clt_document(
    document: Mapping[str, Any],
    *,
    run_path: Path,
) -> tuple[Process, dict[str, Any]]:
    """Load an in-memory CWL CommandLineTool document.

    Raises:
        InvalidCLTError: If the CWL document cannot be parsed.
    """
    yaml_file = tool_process(yaml.safe_load(yaml.safe_dump(dict(document), sort_keys=False)), str(run_path))
    return _loaded(Tool(str(run_path), yaml_file))


def workflow_document(workflow: "Workflow") -> Document:
    """Build a workflow's language document.

    Steps name their tools (a step renamed from its tool carries `run:`), nested
    workflows appear as steps naming their own `.wic` documents, and every
    workflow output is addressed by its authored step name.
    """
    workflow_inputs: dict[str, Any] = {}
    for parameter in workflow._inputs:
        cwl_type = parameter.cwl_type()
        if cwl_type is None and _read_by_a_record(workflow, parameter):
            cwl_type = "Any"
        if cwl_type is None:
            raise InvalidStepError(
                f"workflow input {workflow.process_name}.{parameter.name} has no resolved type"
            )
        workflow_inputs[parameter.name] = {"type": cwl_type}

    workflow_outputs: dict[str, Any] = {parameter.name: parameter.to_workflow_output()
                                        for parameter in workflow._outputs}
    return Document(
        steps=tuple(_with_defaults_resolved(step._as_workflow_step(), Path.cwd()) for step in workflow.steps),
        passthrough=tuple((key, value) for key, value in
                          (("inputs", workflow_inputs), ("outputs", workflow_outputs)) if value),
        span=workflow._span,
        port_spans=tuple((section, parameter.name, parameter._span)
                         for section, parameters in (("inputs", workflow._inputs), ("outputs", workflow._outputs))
                         for parameter in parameters if parameter._span is not None),
    )


def _with_defaults_resolved(step: LangStep, base: Path) -> LangStep:
    """`step` with each relative File or Directory location in a `StepInput(default=...)` read against
    `base` and written absolute, as a bound path is: the document is written elsewhere."""
    return replace(step, inputs=tuple(
        (name, replace(value, fields=tuple(
            (key, absolute_paths({"default": field}, base)["default"] if key == "default" else field)
            for key, field in value.fields)) if isinstance(value, CwlRecord) else value)
        for name, value in step.inputs))


def _read_by_a_record(workflow: "Workflow", parameter: InputParameter) -> bool:
    """Whether a `StepInput` on one of `workflow`'s steps names `parameter` as a source."""
    return any(source is parameter
               for step in workflow.steps for step_input in step._inputs
               for source in step_input.record_sources())


def step_run(clt_path: Path) -> str:
    """What a step's `run:` names: `<stem>.cwl`, the tool written beside the document.

    The real-time analysis adapter is named by its registry stem instead, and not
    written: a step is a declaration only when it runs the adapter the registry
    holds, and a `run:` path would make it an ordinary tool step.
    """
    return ADAPTER if clt_path.stem == ADAPTER else f"{clt_path.stem}.cwl"


def _bundle_tools(workflow: "Workflow") -> dict[str, "Step"]:
    """Every tool the workflow tree runs, by the file stem a document names it with."""
    tools: dict[str, "Step"] = {}
    for step in workflow._flatten_steps():
        known = tools.setdefault(step.clt_path.stem, step)
        if known.yaml != step.yaml:
            raise InvalidStepError(
                f"steps {known.process_name!r} and {step.process_name!r} run different tools that "
                f"share the file stem {step.clt_path.stem!r}; a document names a tool by its stem"
            )
    return tools


def write_workflow_wic(workflow: "Workflow", path: str | Path | None = None) -> Path:
    """Write a workflow as a self-contained bundle in one directory.

    The bundle is the root `.wic`, one `<name>.wic` per nested workflow and one
    `<stem>.cwl` per distinct tool. Only a tool's own document is written: files
    it refers to by relative path (such as a `$import`) are not copied. A tool
    already on disk as its target is left alone, and a different file there is
    an error.
    """
    workflow._validate()
    root = _artifact_path(path, default_name=f"{workflow.process_name}.wic", suffixes=(".wic",))
    missing_tools: dict[Path, "Step"] = {}
    for stem, step in _bundle_tools(workflow).items():
        if step_run(step.clt_path) != f"{stem}.cwl":
            continue
        target = root.parent / f"{stem}.cwl"
        if not target.exists():
            missing_tools[target] = step
        elif _load_yaml(target) != step.yaml:
            raise InvalidStepError(f"{target} already exists and is not the tool of step {step.process_name!r}")
    root.parent.mkdir(exist_ok=True, parents=True)
    for nested in workflow._flatten_subworkflows():
        target = root if nested is workflow else root.parent / f"{nested.process_name}.wic"
        target.write_text(render(workflow_document(nested)), encoding="utf-8")
    for target, step in missing_tools.items():
        target.write_text(input_output.dump_wic_yaml(step.yaml), encoding="utf-8")
    return root


def _merged_known_tools(workflow: "Workflow", tool_registry: Tools | None = None) -> Tools:
    """Merge known tools: step tools by stem, then step registries, then the explicit registry."""
    steps = workflow._flatten_steps()
    merged_tools: Tools = {StepId(stem, "global"): Tool(str(step.clt_path), step.yaml)
                           for stem, step in _bundle_tools(workflow).items()}
    for step in steps:
        merged_tools.update(step._tool_registry)
    if tool_registry is not None:
        merged_tools.update(tool_registry)
    return merged_tools


def _nested_documents(workflow: "Workflow") -> dict[tuple[str, str], ParseResult]:
    """Every nested workflow's document, keyed as Resolve looks it up."""
    from .workflow import Workflow  # pylint: disable=import-outside-toplevel

    documents: dict[tuple[str, str], ParseResult] = {}
    for step in workflow.steps:
        if isinstance(step, Workflow):
            documents |= _nested_documents(step)
            documents[("global", step.process_name)] = ParseResult(
                workflow_document(step), Diagnostics())
    return documents


def compile_settings(inference: Inference, lang_version: str | None = None,
                     policy: RunPolicy = RunPolicy()) -> compiler.CompileSettings:
    """The settings the command line builds from its inference and partial-failure flags, from `inference` and
    `policy`. No configuration file is read."""
    return compiler.CompileSettings(
        InferencePolicy(disabled=not inference.enabled,
                        use_naming_conventions=inference.naming_conventions,
                        renaming_conventions=tuple(
                            SHIPPED_RENAMINGS if inference.renamings is None and inference.naming_conventions
                            else inference.renamings or ()),
                        insert_steps_automatically=inference.insert_steps,
                        format_rules=tuple(inference.format_rules.items()),
                        strict=inference.strict),
        policy,
        lang_version)


def compile_workflow_result(  # pylint: disable=too-many-arguments
    workflow: "Workflow",
    *,
    tool_registry: Tools | None = None,
    lang_version: str | None = None,
    inference: Inference = Inference(),
    policy: RunPolicy = RunPolicy(),
    quiet: bool = False,
) -> CompilationResult:
    """Compile a Python API workflow to the graph-derived internal result."""
    workflow._validate()

    merged_tools = _merged_known_tools(workflow, tool_registry)

    settings = compile_settings(inference, lang_version, policy)
    bundle = SourceBundle(
        ParseResult(workflow_document(workflow), Diagnostics()),
        Path(workflow.process_name).stem,
        RegistrySnapshot.from_tools(merged_tools, workflows=_nested_documents(workflow)))
    result = compiler.compile_source(bundle, settings, quiet=quiet)
    _bound_values(workflow, compiler.resolved_view(result))  # refuses a value that does not coerce
    return result


def _bound_values(workflow: "Workflow", view: ResolvedWorkflow) -> dict[str, Any]:
    """Each value bound on one of the workflow's own inputs, as an inputs file gives it, in the form a value written
    on a step takes (design §4.3)."""
    declarations = {port.name: port.declaration for port in view.inputs}
    return {parameter.name: coerce_job_value(AuthoredName(parameter.name), declarations[parameter.name],
                                             serialize_value(binding.value), span=binding.span)
            for parameter in workflow._inputs if (binding := parameter._binding) is not None and not binding.linked}


def _view(workflow: "Workflow", result: CompilationResult) -> ResolvedWorkflow:
    """The view a target lowers, whichever target: the workflow's bound values, and the outputs it declares, when it
    declares any."""
    view = compiler.resolved_view(result)
    return bind(view, values=_bound_values(workflow, view),
                outputs=[output.name for output in workflow._outputs] or None)


#: The background of a drawing from Python: opaque, so it reads in a light or a dark notebook,
#: in exported HTML and PDF, and on GitHub.
_LIGHT_BACKGROUND: Final = "white"
_DARK_BACKGROUND: Final = "#1e1e1e"


# pylint: disable-next=too-many-arguments
def draw_workflow(
    workflow: "Workflow",
    *,
    dark: bool,
    label_edges: bool,
    label_stepname: bool,
    inline_depth: int,
    inference: Inference,
    tool_registry: Tools | None,
    lang_version: str | None,
) -> graphviz.Digraph:
    """Compile `workflow` once and draw the result: see `Workflow.draw`."""
    result = compile_workflow_result(workflow, tool_registry=tool_registry, lang_version=lang_version,
                                     inference=inference)
    settings: GraphSettings = {
        "graph_dark_theme": dark,
        "graph_inline_depth": inline_depth,
        "graph_label_edges": label_edges,
        "graph_label_stepname": label_stepname,
        "graph_show_inputs": False,
        "graph_show_outputs": False,
    }
    background = _DARK_BACKGROUND if dark else _LIGHT_BACKGROUND
    return drawing.draw(result, settings, result.graph.name, background=background)


class ServiceDocument(NamedTuple):
    """What a remote service is sent to run `workflow`: see `service_document`."""

    #: The one document, every process embedded, and only the outputs the workflow declares, when it declares any.
    cwl: Json
    #: Its job inputs, every path as written; only each File's list of possible formats is resolved.
    job_inputs: Json
    #: The real-time analyses the workflow declares, which no service runs.
    realtime: tuple[Declaration, ...]


def service_document(workflow: "Workflow", *, tool_registry: Tools | None = None,
                     inference: Inference = Inference()) -> ServiceDocument:
    """Compile `workflow` for a service that runs it elsewhere: what `compile()` returns, without the local run's
    Directory literals for output directories, which only a local runner reads."""
    result = compile_workflow_result(workflow, tool_registry=tool_registry, inference=inference)
    artifact = pc.inline_artifact_runs(targets.TARGETS["cwl"].lower(_view(workflow, result)).model)
    return ServiceDocument(copy.deepcopy(artifact.cwl), resolve_file_formats(artifact.job_inputs), result.realtime)


class ServicePipeline(NamedTuple):
    """What a remote service is sent to run `workflow` on Nextflow: see `service_pipeline`."""

    #: The model the pipeline is rendered from.
    nextflow_workflow: ExecutableNextflowWorkflow
    #: The pipeline in its service form (design_docs/sophios_nextflow_backend.md §4.5), one file.
    script: str
    #: Its parameters, as `nextflow_params.json` holds them: every path as written.
    params: Json
    #: The real-time analyses the workflow declares, which no service runs.
    realtime: tuple[Declaration, ...]


def service_pipeline(workflow: "Workflow", *, tool_registry: Tools | None = None,
                     inference: Inference = Inference()) -> ServicePipeline:
    """Compile `workflow` to the pipeline a service runs on Nextflow, which publishes every output under its own name.

    Raises:
        SophiosError: The workflow does not compile, or an output's name is not one Nextflow keeps (`wic061`): a
            client that attaches to the run later reads the outputs by the names the pipeline published. A workflow
            that declares no output has its steps' outputs, each named `<step>~<port>`, which Nextflow never keeps.
        NotLowered: The Nextflow target cannot run the workflow, as `compile(target="nextflow")` raises it.
    """
    result = compile_workflow_result(workflow, tool_registry=tool_registry, inference=inference)
    view = _view(workflow, result)
    executable = targets.TARGETS["nextflow"].lower(view).model
    renamed = {name: published for published, name in nextflow_names(view)["outputs"].items() if published != name}

    def refusal(name: str, published: str) -> str:
        if "~" not in name:
            return (f"workflow output {name!r} would be published as {published!r}, since Nextflow does not keep its "
                    f"name, and a service reports each output by its published name; rename it, as to {published!r}")
        step, port = name.rsplit("~", 1)
        return (f"the workflow declares no output, so it returns the output {port!r} of its step {step!r} as {name!r}, "
                f"a name Nextflow does not keep, and a service reports each output by its published name; declare "
                f"each output to return, under a name Nextflow keeps, such as {published!r}")
    if renamed:
        raise SophiosError(Diagnostic(Severity.ERROR, SophiosErrorCode.NEXTFLOW_OUTPUT_RENAMED, refusal(*names))
                           for names in renamed.items())
    return ServicePipeline(executable, render_nextflow(executable, service=True), nextflow_params(executable),
                           result.realtime)


def compiled_workflow_from_result(
    workflow: "Workflow",
    result: CompilationResult,
) -> CompiledWorkflow:
    """Build the public boundary from the typed internal result, with the notes the compile and the CWL target
    made."""
    view = _view(workflow, result)
    lowered = targets.TARGETS["cwl"].lower(view)
    artifact = pc.inline_artifact_runs(lowered.model)
    return CompiledWorkflow(
        name=workflow.process_name,
        cwl_workflow=copy.deepcopy(artifact.cwl),
        cwl_job_inputs=runner_job_inputs(artifact.job_inputs, output_target_inputs(view)),
        lang_version=result.lang_version,
        diagnostics=tuple(Diagnostics([*result.diagnostics, *lowered.notes])),
    )


def _compiled_nextflow(workflow: "Workflow", result: CompilationResult) -> CompiledNextflowWorkflow:
    """`result` lowered to Nextflow, with every note the compile and the lowering made."""
    lowered = targets.TARGETS["nextflow"].lower(_view(workflow, result))
    return CompiledNextflowWorkflow(
        nextflow_workflow=lowered.model,
        lang_version=result.lang_version,
        diagnostics=(*result.diagnostics, *lowered.notes),
    )


#: The object `Workflow.compile` returns for each target, as its overloads say.
_COMPILED: Final[Mapping[str, Callable[["Workflow", CompilationResult],
                                       CompiledWorkflow | CompiledNextflowWorkflow]]] = {
    "cwl": compiled_workflow_from_result, "nextflow": _compiled_nextflow}


def compiled(  # pylint: disable=too-many-arguments
    workflow: "Workflow",
    *,
    target: str,
    tool_registry: Tools | None,
    lang_version: str | None,
    inference: Inference,
    policy: RunPolicy,
) -> CompiledWorkflow | CompiledNextflowWorkflow:
    """Compile `workflow` for `target` once through the core semantic pipeline, and print every note the compile and
    the target made: see `Workflow.compile`.

    Raises:
        ValueError: No target has that name, before anything is compiled.
        NotLowered: The target cannot run the workflow.
    """
    row = targets.target(target)
    compiled_one = _COMPILED[row.name](workflow, compile_workflow_result(
        workflow, tool_registry=tool_registry, lang_version=lang_version, inference=inference, policy=policy))
    report(compiled_one.diagnostics)
    return compiled_one


def check_workflow(workflow: "Workflow", options: "RunOptions") -> tuple[Diagnostic, ...]:
    """Every note compiling `workflow` to the target of `options.run` makes, and every refusal of the target as an
    error, after the checks of this machine a run makes before it starts; nothing is written, pulled or printed: see
    `Workflow.check`."""
    row = targets.target(options.run.runner.target)
    result, analyses = _compiled_for_run(workflow, options, refuse_realtime=False, check=True)
    try:
        lowered, bound = _lowered_for_run(row, workflow, result)
    except NotLowered as refused:
        return (*result.diagnostics, *refused.diagnostics)
    _prepared(workflow, options, lowered=lowered, bound=bound, analyses=analyses, check=True)
    return (*result.diagnostics, *lowered.notes)


def _enabled(value: Any) -> bool:
    """Whether a yes/no style `run_args_dict` value is on."""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _from_run_args_dict(target: str, run_args_dict: Mapping[str, Any], given: RunSettings) -> RunSettings:
    """The deprecated `run_args_dict` of a run of `target` as a run: the keys the command line declares are set on
    its parsed arguments, which `cli.run_settings_from_args` reads as it reads the command line's, over the settings
    the keywords gave (`given`), but for `target`, which `target=` alone chooses. A key the command line does not
    declare becomes a flag of the runner, `--<key> <value>` for a CWL runner and `-<key> <value>` for Nextflow.

    Raises:
        ValueError: `cwl_runner` names no runner of the target, or the target is not CWL and a key only a CWL run
            reads is given.
    """
    declared = {option.lstrip("-"): action for action in cli.parser._actions for option in action.option_strings}
    cwl_only = {action.dest for action in cli.target_options["cwl"]._group_actions}
    if target != "cwl" and (refused := [key for key in run_args_dict if key in cwl_only]):
        raise ValueError(f"target={target!r} runs no CWL runner, so it takes none of the run_args_dict keys "
                         f"only a CWL run reads: {', '.join(map(repr, refused))}")
    args = cli.get_args()
    vars(args).update(target=target, outdir=given.outdir, inputs_file=given.inputs_file, quiet=given.quiet,
                      generate_run_script=given.generate_run_script)
    flags: list[str] = []
    for key, value in run_args_dict.items():
        if (action := declared.get(key)) is not None:
            if action.dest != "target":  # target= alone chooses the target, as it did before the dict was deprecated
                setattr(args, action.dest,
                        _enabled(value) if isinstance(action, argparse._StoreTrueAction) else str(value))
        elif target == "cwl":
            flags += [f"--{key}", str(value)]
        else:
            flags += [f"-{key}", *([str(value)] if value else [])]
    return cli.run_settings_from_args(args, flags)


def run_settings(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    target: str, runner: Runner | None, run_args_dict: Mapping[str, Any] | None, outdir: str, inputs_file: str,
    quiet: bool, generate_run_script: bool,
) -> RunSettings:
    """The run `Workflow.run` and its siblings are asked for, as the one value the run path takes.

    Raises:
        ValueError: No target has that name; `runner` is none of its runners, or `run_args_dict` is given with it,
            names a `cwl_runner` the target lacks, or has a key only a CWL run reads; or, for the target `nextflow`,
            `outdir` or `inputs_file` is given.
    """
    targets.target(target)  # the name is checked before anything is read from `run_args_dict`
    if runner is not None and run_args_dict is not None:
        raise ValueError("pass runner= or run_args_dict, not both: run_args_dict is deprecated and names its "
                         "runner as cwl_runner")
    settings = RunSettings(targets.resolve(target, runner), outdir, inputs_file, quiet, generate_run_script)
    if run_args_dict is not None:
        warnings.warn("run_args_dict is deprecated: pass runner=Cwltool(...), Toil(...) or Nextflow(...), and "
                      "outdir=, inputs_file=, quiet= and generate_run_script= to run() instead",
                      DeprecationWarning, stacklevel=3)
        settings = _from_run_args_dict(target, run_args_dict, settings)
    given = [name for name, value in (("outdir", settings.outdir), ("inputs_file", settings.inputs_file)) if value]
    if settings.runner.target != "cwl" and given:
        raise ValueError(f"target={target!r} runs no CWL runner, so it takes none of the run() settings only a CWL "
                         f"run reads: {', '.join(map(repr, given))}")
    return settings


@dataclass(frozen=True, slots=True)
class RunOptions:  # pylint: disable=too-many-instance-attributes
    """What `Workflow.run()`, `start()` and `run_async()` are given, besides the workflow: see `Workflow.run`."""

    run: RunSettings
    user_env_vars: dict[str, str] | None = None
    basepath: str = DEFAULT_BASEPATH
    tool_registry: Tools | None = None
    workflow_paths: Mapping[str, Mapping[str, Path]] | None = None
    inference: Inference = Inference()
    policy: RunPolicy = RunPolicy()
    lang_version: str | None = None


def _realtime_refused(declarations: tuple[Declaration, ...]) -> SophiosError:
    """`api008`, at each real-time analysis declaration: `run_async` never runs them."""
    return SophiosError(Diagnostic(
        Severity.ERROR, SophiosErrorCode.REALTIME_NOT_ASYNC,
        f"this step declares the real-time analysis {declaration.analysis!r}, and run_async() never runs "
        "real-time analyses", declaration.span) for declaration in declarations)


def _run_request(workflow: "Workflow", options: RunOptions, *, refuse_realtime: bool) -> backends.RunRequest:
    """Compile `workflow`, check this machine, write what the runner reads and pull its images: the run, ready.
    Its notes are printed on stderr, as the command line prints them, and the request carries them.

    Raises:
        SophiosError: The workflow does not compile, this machine cannot run it, or it declares real-time
            analyses and `refuse_realtime` is set (`api008`).
        NotLowered: The target cannot run the workflow.
        ValueError: A `user_env_vars` key is not an environment variable name.
    """
    row = targets.target(options.run.runner.target)
    logger.info("Running %s", workflow.process_name)
    result, analyses = _compiled_for_run(workflow, options, refuse_realtime=refuse_realtime, check=False)
    lowered, bound = _lowered_for_run(row, workflow, result)
    report(lowered.notes)
    request = _prepared(workflow, options, lowered=lowered, bound=bound, analyses=analyses, check=False)
    return replace(request, diagnostics=(*result.diagnostics, *lowered.notes))


def _compiled_for_run(workflow: "Workflow", options: RunOptions, *, refuse_realtime: bool,
                      check: bool) -> tuple[CompilationResult, tuple[realtime.Analysis, ...]]:
    """What a run and a check share before the target lowers anything: the compile, whose notes a run prints, the
    real-time analyses, and the base path that can be written.

    Raises:
        SophiosError: The workflow does not compile, a real-time analysis does not, the base path cannot be
            written, or it declares real-time analyses and `refuse_realtime` is set (`api008`).
    """
    result = compile_workflow_result(workflow, tool_registry=options.tool_registry, inference=options.inference,
                                     policy=options.policy, lang_version=options.lang_version, quiet=check)
    if not check:
        report(result.diagnostics)
    if refuse_realtime and result.realtime:
        raise _realtime_refused(result.realtime)
    analyses = realtime.compile_analyses(
        result.realtime, {namespace: dict(paths) for namespace, paths in (options.workflow_paths or {}).items()},
        _merged_known_tools(workflow, options.tool_registry),
        compile_settings(options.inference, policy=options.policy))
    if (problem := preflight.unwritable(Path(options.basepath), "the workflow and its run",
                                        "pass run() a basepath you can write to")) is not None:
        raise SophiosError([problem])
    return result, analyses


def _lowered_for_run(row: targets.Target[Any], workflow: "Workflow",
                     result: CompilationResult) -> tuple[run_setup.Lowered[Any], Yaml]:
    """`result` lowered by `row` for a runner, which does not run where the workflow's relative paths are read, and
    the values bound in the workflow as the author wrote their paths."""
    view = _view(workflow, result)
    return row.lower(targets.for_run(row, view, Path.cwd())), row.run_values(view)


# pylint: disable-next=too-many-arguments
def _prepared(workflow: "Workflow", options: RunOptions, *,
              lowered: run_setup.Lowered[Any], bound: Yaml, analyses: tuple[realtime.Analysis, ...],
              check: bool) -> backends.RunRequest:
    """Write what `options.run`'s runner reads and make the machine ready for it: the run's request.

    A CWL runner runs the one document `compile()` returns, and each real-time analysis's too.
    """
    run = options.run
    if run.runner.target == "cwl":
        lowered = replace(lowered, model=pc.inline_artifact_runs(lowered.model))
        analyses = tuple(replace(analysis, artifact=pc.inline_artifact_runs(analysis.artifact))
                         for analysis in analyses)
    prepared = targets.backend(run.runner).prepare(
        lowered, analyses, Path(options.basepath),
        run_setup.Settings(run, Path.cwd(), bound, inputs_file_named="inputs_file", check=check))
    return backends.RunRequest(workflow.process_name, Path(options.basepath), run,
                               env=dict(options.user_env_vars or {}), realtime_plans=prepared.plans)


def start_workflow(workflow: "Workflow", options: RunOptions) -> RunHandle:
    """Compile and prepare `workflow`, start its runner, and return the run's handle: see `Workflow.start`."""
    if options.run.generate_run_script:
        raise ValueError("generate_run_script writes run.sh instead of starting the run: call run() for it")
    request = _run_request(workflow, options, refuse_realtime=False)
    return run_local.start(request)


def run_workflow(workflow: "Workflow", options: RunOptions) -> RunResult | None:
    """Compile and run `workflow`, and wait for it: see `Workflow.run`."""
    request = _run_request(workflow, options, refuse_realtime=False)
    if request.run.generate_run_script:
        run_local.run(request)
        return None
    return run_local.start(request).collect()


async def run_workflow_async(workflow: "Workflow", options: RunOptions) -> RunResult | None:
    """`run_workflow`, awaited: see `Workflow.run_async`."""
    request = await asyncio.to_thread(_run_request, workflow, options, refuse_realtime=True)
    if request.run.generate_run_script:
        run_local.run(request)
        return None
    handle = run_local.start(request)
    waiting = asyncio.ensure_future(asyncio.to_thread(handle.wait))
    try:
        await asyncio.shield(waiting)
    except asyncio.CancelledError:
        # The runner is stopped before the cancellation goes on, so nothing outlives the task.
        await asyncio.to_thread(handle.cancel)
        await waiting
        raise
    return handle.collect()   # the run has ended, so this returns, or raises, at once
