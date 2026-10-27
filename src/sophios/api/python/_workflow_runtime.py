"""Internal runtime helpers for the Python workflow API.

This module keeps filesystem loading, compilation, and execution details out
of the public workflow module so `Step` and `Workflow` stay focused on
Python-facing workflow authoring.
"""

# pylint: disable=protected-access
# This module is the private adapter layer between workflow objects and the
# compiler/runtime boundary, so reaching internal state is intentional.

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

import graphviz
import yaml
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool
from cwl_utils.parser import load_document_by_uri, load_document_by_yaml

from sophios import backends, compiler, drawing, input_output, plugins, post_compile as pc, preflight, realtime
from sophios import run_local, run_setup
from sophios.run_local import RunHandle
from sophios.run_status import RunResult
from sophios.ir.artifacts import CompilationArtifact, CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.locations import absolute_paths
from sophios.ir.realtime import ADAPTER, Declaration
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import CwlRecord, Diagnostic, Diagnostics, Document, ParseResult, Severity, SophiosError, render
from sophios.lang import Step as LangStep
from sophios.lang.error_codes import SophiosErrorCode
from sophios.cli import default_compilation_settings, get_known_and_unknown_args
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.utils import convert_args_dict_to_args_list
from sophios.utils_cwl import tool_process
from sophios.wic_types import DEFAULT_BASEPATH, CompilerOptions, GraphSettings, StepId, Tool, Tools

from ._inference import Inference
from ._errors import InvalidCLTError, InvalidStepError
from ._compiled import CompiledWorkflow
from ._ports import InputParameter, OutputParameter, ParameterStore
from ._types import ScatterMethod
from ._utils import load_yaml as _load_yaml
from ._api_config import DEFAULT_RUN_ARGS

if TYPE_CHECKING:
    from .workflow import Step, Workflow


logger = logging.getLogger("Sophios Python API")

ParameterT = TypeVar("ParameterT")

_RUN_ARG_BOOLEAN_FLAGS = {
    "docker_remove_entrypoints",
    "generate_run_script",
    "no_provenance",
    "quiet",
}


def silence_autodiscovery_logging() -> None:
    """Suppress noisy autodiscovery logs during Python API imports."""
    logging.getLogger("wicautodiscovery").disabled = True


class _CWLParameterDefinition(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal structural type shared by parsed CWL input/output parameters."""

    id: Any
    type_: Any


def _parameter_name(parameter_id: Any) -> str:
    """The public name of a CWL parameter id (its last `#` or `/` segment)."""
    return str(parameter_id).rsplit("#", maxsplit=1)[-1].rsplit("/", maxsplit=1)[-1]


def coerce_path(value: str | Path | None, *, field_name: str, allow_none: bool = False) -> Path | None:
    """Normalize string-like path input to `Path`. `field_name` names the
    parameter in the `TypeError` the last branch raises."""
    match value:
        case Path() as path:
            return path
        case str() as path_str:
            return Path(path_str)
        case None if allow_none:
            return None
        case _:
            allowed = "Path or str, or None" if allow_none else "Path or str"
            raise TypeError(f"{field_name} must be a {allowed}")


def normalize_workflow_name(workflow_name: str) -> str:
    """Convert a user-facing workflow name into a filesystem-safe id.

    Args:
        workflow_name (str): Original workflow name.

    Returns:
        str: Normalized workflow id used by the Python API.
    """
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


def _validate_scatter(items: list[Any], owner: Any | None) -> None:
    """Raise unless `items` are distinct, bound, array-valued inputs of `owner`."""
    if not all(isinstance(item, InputParameter) for item in items):
        raise TypeError("all scatter inputs must be InputParameter type")
    if len({id(item) for item in items}) != len(items):
        raise ValueError("scatter inputs must be unique")
    if owner is None:
        return
    for item in items:
        if item.parent_obj is not owner:
            raise ValueError("scatter inputs must belong to the same step")
        if not item.is_bound():
            raise ValueError("scatter inputs must be bound before scattering")
        if not item.is_scatterable():
            raise ValueError("scatter inputs must be bound to array-valued data")


def validate_step_assignment(name: str, value: Any, *, owner: Any | None = None) -> None:
    """Validate assignments to special step attributes.

    Args:
        name (str): Attribute name being assigned.
        value (Any): Candidate value for that attribute.
        owner (Any | None): Optional `Step` owning the assignment.

    Raises:
        TypeError: If `scatter` is not a list of `InputParameter` values.
        ValueError: If `scatterMethod` or `when` receive invalid values.

    Returns:
        None: Validation happens for its side effect of raising on invalid input.
    """
    match name, value:
        case "scatter", list() as items:
            _validate_scatter(items, owner)
        case "scatter", invalid if invalid:
            raise TypeError("scatter must be assigned a list of InputParameter values")
        case "scatterMethod", str() as scatter_method if scatter_method:
            allowed = {member.value for member in ScatterMethod}
            if scatter_method not in allowed:
                raise ValueError(
                    "Invalid value for scatterMethod. "
                    f"Valid values are: {', '.join(sorted(allowed))}"
                )
        case "when", condition if condition and not (
                isinstance(condition, str) and condition.startswith("$(") and condition.endswith(")")):
            raise ValueError("Invalid input to when. The js string must start with '$(' and end with ')'")


def populate_parameters(
    cwl_parameters: list[_CWLParameterDefinition],
    store: ParameterStore[Any],
    parameter_cls: type[InputParameter] | type[OutputParameter],
    *,
    parent: Any,
) -> None:
    """Populate a parameter store from CWL input or output declarations.

    Args:
        cwl_parameters (list[_CWLParameterDefinition]): Parsed CWL parameters.
        store (ParameterStore[Any]): Destination store for Python API parameter wrappers.
        parameter_cls (type[InputParameter] | type[OutputParameter]): Wrapper type to instantiate.
        parent (Any): Owning `Step` or `Workflow`.

    Returns:
        None: The destination store is populated in place.
    """
    for parameter in cwl_parameters:
        store.add(parameter_cls(_parameter_name(parameter.id), parameter.type_, parent_obj=parent))


def load_clt(clt_path: Path, tool_registry: Tools) -> tuple[CWLCommandLineTool, dict[str, Any]]:
    """Load a CWL CommandLineTool from disk or a fallback registry.

    Args:
        clt_path (Path): Filesystem path to the CWL tool.
        tool_registry (Tools): Registry used when the file is unavailable on disk.

    Raises:
        InvalidCLTError: If the tool cannot be loaded from disk or the registry.

    Returns:
        tuple[CWLCommandLineTool, dict[str, Any]]: Parsed CWL object and raw YAML.
    """
    stepid = StepId(clt_path.stem, "global")

    if clt_path.exists():
        try:
            # The process first: a packed document with no `main` is refused by name, not as "invalid".
            yaml_file = tool_process(_load_yaml(clt_path), str(clt_path))
            clt = load_document_by_uri(clt_path)
        except SophiosError:
            raise
        except Exception as exc:
            raise InvalidCLTError(f"invalid cwl file: {clt_path}") from exc
        tool_registry[stepid] = Tool(str(clt_path), yaml_file)
        return clt, yaml_file

    if stepid in tool_registry:
        tool = tool_registry[stepid]
        logger.info("%s does not exist, but %s was found in the provided tool registry.", clt_path, clt_path.stem)
        logger.info("Using file contents from %s", tool.run_path)
        yaml_file = tool.cwl
        clt = load_document_by_yaml(yaml_file, tool.run_path)
        return clt, yaml_file

    logger.warning("Warning! %s does not exist, and", clt_path)
    logger.warning("%s was not found in the provided tool registry.", clt_path.stem)
    raise InvalidCLTError(f"invalid cwl file: {clt_path}")


def load_clt_document(
    document: Mapping[str, Any],
    *,
    run_path: Path,
) -> tuple[CWLCommandLineTool, dict[str, Any]]:
    """Load an in-memory CWL CommandLineTool document.

    Args:
        document (Mapping[str, Any]): Parsed CWL document.
        run_path (Path): Virtual run path used as the tool base URI.

    Raises:
        TypeError: If `document` does not normalize to a mapping.
        InvalidCLTError: If the CWL document cannot be parsed.

    Returns:
        tuple[CWLCommandLineTool, dict[str, Any]]: Parsed CWL object and normalized YAML.
    """
    match yaml.safe_load(yaml.safe_dump(dict(document), sort_keys=False)):
        case dict() as loaded:
            yaml_file = tool_process(loaded, str(run_path))
        case _:
            raise TypeError("document must be a mapping of CWL fields")
    try:
        clt = load_document_by_yaml(yaml_file, str(run_path))
    except Exception as exc:
        raise InvalidCLTError(f"invalid cwl document for: {run_path}") from exc
    return clt, yaml_file


def workflow_document(workflow: "Workflow") -> Document:
    """Build a workflow's language document.

    Steps name their tools (a step renamed from its tool carries `run:`), nested
    workflows appear as steps naming their own `.wic` documents, and every
    workflow output is addressed by its authored step name.

    Args:
        workflow (Workflow): Workflow to serialize.

    Returns:
        Document: The workflow's document.
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

    workflow_outputs: dict[str, Any] = {parameter.name: parameter.to_workflow_output(workflow.steps)
                                        for parameter in workflow._outputs}
    return Document(
        steps=tuple(_with_defaults_resolved(step._as_workflow_step(), Path.cwd()) for step in workflow.steps),
        passthrough=tuple((key, value) for key, value in
                          (("inputs", workflow_inputs), ("outputs", workflow_outputs)) if value),
        span=workflow._span,
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


def _wic_output_path(workflow: "Workflow", path: str | Path | None) -> Path:
    """Resolve user-provided `.wic` output destinations."""
    if path is None:
        return Path(f"{workflow.process_name}.wic")

    output_path = Path(path)
    if output_path.suffix == ".wic":
        return output_path
    if output_path.suffix:
        raise ValueError("path must be a .wic file or a directory")
    return output_path / f"{workflow.process_name}.wic"


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

    Args:
        workflow (Workflow): Workflow to serialize.
        path (str | Path | None): Destination `.wic` path or output directory.
            When omitted, writes `<workflow>.wic` in the current directory.

    Returns:
        Path: The path to the root `.wic` file that was written.
    """
    workflow._validate()
    root = _wic_output_path(workflow, path)
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


def compiler_options(inference: Inference, lang_version: str | None = None) -> CompilerOptions:
    """The options the command line builds from its inference flags, from `inference`. No configuration file is read."""
    options: CompilerOptions = {
        **default_compilation_settings(),
        'inference_disable': not inference.enabled,
        'inference_strict': inference.strict,
        'inference_use_naming_conventions': inference.naming_conventions,
        'insert_steps_automatically': inference.insert_steps,
        'inference_rules': dict(inference.format_rules),
        'renaming_conventions': list(inference.renamings),
    }
    if lang_version is not None:
        options['lang_version'] = lang_version
    return options


def compile_workflow_result(
    workflow: "Workflow",
    *,
    tool_registry: Tools | None = None,
    lang_version: str | None = None,
    inference: Inference = Inference(),
) -> CompilationResult:
    """Compile a Python API workflow to the graph-derived internal result.

    Args:
        workflow (Workflow): Workflow to compile.
        lang_version (str | None): Pin the Sophios language version for this
            compilation; None (the default) infers it. An explicit setting
            beats any file tag.
        tool_registry (Tools | None): Optional tool registry override.
        inference (Inference): How unbound inputs are connected.

    Returns:
        CompilationResult: The typed compiler output for the workflow.
    """
    return _compile_bundle(workflow, tool_registry, lang_version, inference)[1]


def _compile_bundle(
    workflow: "Workflow",
    tool_registry: Tools | None,
    lang_version: str | None,
    inference: Inference,
) -> tuple[SourceBundle, CompilationResult]:
    """The documents `workflow` is compiled from, and the result of compiling them."""
    workflow._validate()

    merged_tools = _merged_known_tools(workflow, tool_registry)

    options = compiler_options(inference, lang_version)
    bundle = SourceBundle(
        ParseResult(workflow_document(workflow), Diagnostics()),
        Path(workflow.process_name).stem,
        RegistrySnapshot.from_tools(merged_tools, workflows=_nested_documents(workflow)))
    return bundle, compiler.compile_source(bundle, options, testing=False)


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
    bundle, result = _compile_bundle(workflow, tool_registry, lang_version, inference)
    settings: GraphSettings = {
        "graph_dark_theme": dark,
        "graph_inline_depth": inline_depth,
        "graph_label_edges": label_edges,
        "graph_label_stepname": label_stepname,
        "graph_show_inputs": False,
        "graph_show_outputs": False,
    }
    background = _DARK_BACKGROUND if dark else _LIGHT_BACKGROUND
    return drawing.draw(result, bundle, settings, bundle.name, background=background)


def _one_document(workflow: "Workflow", result: CompilationResult) -> CompilationArtifact:
    """The one document `compile()` returns and `run()` runs: every process embedded,
    and only the outputs the workflow declares, when it declares any."""
    artifact = pc.inline_artifact_runs(result.artifact)
    if not workflow._outputs:
        return artifact
    match artifact.cwl.get("outputs"):
        case dict() as outputs:
            declared = {output.name: outputs[output.name] for output in workflow._outputs if output.name in outputs}
            return replace(artifact, cwl={**artifact.cwl, "outputs": declared})
    return artifact


def compiled_workflow_from_result(
    workflow: "Workflow",
    result: CompilationResult,
) -> CompiledWorkflow:
    """Build the public boundary from the typed internal result."""
    artifact = _one_document(workflow, result)
    return CompiledWorkflow(
        name=workflow.process_name,
        cwl_workflow=normalize_artifact_cwl(artifact),
        cwl_job_inputs=normalize_artifact_job_inputs(artifact, artifact.job_inputs),
        lang_version=result.lang_version,
        diagnostics=tuple(str(diagnostic) for diagnostic in result.diagnostics),
    )


def compiled_workflow(
    workflow: "Workflow",
    *,
    tool_registry: Tools | None = None,
    lang_version: str | None = None,
    inference: Inference = Inference(),
) -> CompiledWorkflow:
    """Compile a workflow into the public compiled-workflow boundary object.

    Args:
        workflow (Workflow): Workflow to compile.
        tool_registry (Tools | None): Optional tool registry override.
        lang_version (str | None): Pin the Sophios language version for this
            compilation; None infers it. An explicit setting beats file tags.
        inference (Inference): How unbound inputs are connected.

    Returns:
        CompiledWorkflow: Compiled CWL workflow plus generated job inputs.
    """
    result = compile_workflow_result(
        workflow,
        tool_registry=tool_registry,
        lang_version=lang_version,
        inference=inference,
    )
    return compiled_workflow_from_result(workflow, result)


def _run_args(overrides: dict[str, str] | None) -> dict[str, str]:
    """The default local-run settings with `overrides` applied.

    `quiet` is on unless the caller turns it off. It, `no_provenance` and
    `generate_run_script` are normalised to `yes`/`no`.
    """
    resolved = {**DEFAULT_RUN_ARGS, **(overrides or {})}
    for switch in ("quiet", "no_provenance", "generate_run_script"):
        if switch in resolved:
            resolved[switch] = "yes" if _enabled(resolved[switch]) else "no"
    return resolved


def _enabled(value: Any) -> bool:
    """Whether a yes/no style runtime option is on."""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class RunOptions:  # pylint: disable=too-many-instance-attributes
    """What `Workflow.run()`, `start()` and `run_async()` are given, besides the workflow: see `Workflow.run`."""

    run_args_dict: dict[str, str] | None = None
    user_env_vars: dict[str, str] | None = None
    basepath: str = DEFAULT_BASEPATH
    tool_registry: Tools | None = None
    workflow_paths: Mapping[str, Mapping[str, Path]] | None = None
    inference: Inference = Inference()


def _realtime_refused(declarations: tuple[Declaration, ...]) -> SophiosError:
    """`api008`, at each real-time analysis declaration: `run_async` never runs them."""
    return SophiosError(Diagnostic(
        Severity.ERROR, SophiosErrorCode.REALTIME_NOT_ASYNC,
        f"this step declares the real-time analysis {declaration.analysis!r}, and run_async() never runs "
        "real-time analyses", declaration.span) for declaration in declarations)


def _run_request(workflow: "Workflow", options: RunOptions, *, refuse_realtime: bool) -> backends.RunRequest:
    """Compile `workflow`, check this machine, write what the runner reads and pull its images: the run, ready.

    Raises:
        SophiosError: The workflow does not compile, this machine cannot run it, or it declares real-time
            analyses and `refuse_realtime` is set (`api008`).
        ValueError: `run_args_dict` names a `cwl_runner` Sophios cannot run, or a `user_env_vars` key is not
            an environment variable name.
    """
    logger.info("Running %s", workflow.process_name)
    plugins.logging_filters()

    resolved_run_args = _run_args(options.run_args_dict)
    basepath = options.basepath
    result = compile_workflow_result(workflow, tool_registry=options.tool_registry, inference=options.inference)
    if refuse_realtime and result.realtime:
        raise _realtime_refused(result.realtime)
    analyses = realtime.compile_analyses(
        result.realtime, {namespace: dict(paths) for namespace, paths in (options.workflow_paths or {}).items()},
        _merged_known_tools(workflow, options.tool_registry), compiler_options(options.inference))
    if (problem := preflight.unwritable(Path(basepath), "the workflow and its run",
                                        "pass run() a basepath you can write to")) is not None:
        raise SophiosError([problem])
    outdir = resolved_run_args.get("outdir")
    writes = ((Path(outdir), "the run's outputs", "give run() an outdir you can write to"),) if outdir else ()
    # One document, the workflow's and each analysis's, as `compile()` returns it.
    prepared = backends.backend("cwl", resolved_run_args["cwl_runner"]).prepare(
        _one_document(workflow, result),
        tuple(replace(analysis, artifact=pc.inline_artifact_runs(analysis.artifact)) for analysis in analyses),
        Path(basepath),
        run_setup.Settings(
            preflight.RunSettings(resolved_run_args["container_engine"], resolved_run_args["pull_dir"],
                                  runner=resolved_run_args["cwl_runner"],
                                  run_script=_enabled(resolved_run_args.get("generate_run_script", "no")),
                                  writes=writes),
            Path.cwd(), resolved_run_args.get("inputs_file", ""), inputs_file_named="inputs_file",
            remove_entrypoints=_enabled(resolved_run_args.get("docker_remove_entrypoints"))))
    user_args = convert_args_dict_to_args_list(
        resolved_run_args,
        boolean_flags=_RUN_ARG_BOOLEAN_FLAGS,
    )

    _, unknown_args = get_known_and_unknown_args(workflow.process_name, user_args)
    return backends.RunRequest(
        workflow.process_name, Path(basepath), runner=resolved_run_args["cwl_runner"],
        container_engine=resolved_run_args["container_engine"], cachedir=resolved_run_args.get("cachedir", ""),
        outdir=resolved_run_args.get("outdir", ""), quiet=resolved_run_args.get("quiet", "yes") == "yes",
        provenance=resolved_run_args.get("no_provenance", "no") != "yes",
        passthrough_args=tuple(unknown_args), env=dict(options.user_env_vars or {}), realtime_plans=prepared.plans,
        generate_run_script=resolved_run_args.get("generate_run_script", "no") == "yes")


def start_workflow(workflow: "Workflow", options: RunOptions) -> RunHandle:
    """Compile and prepare `workflow`, start its runner, and return the run's handle: see `Workflow.start`."""
    if _enabled(_run_args(options.run_args_dict).get("generate_run_script", "no")):
        raise ValueError("generate_run_script writes run.sh instead of starting the run: call run() for it")
    return run_local.start(_run_request(workflow, options, refuse_realtime=False))


def run_workflow(workflow: "Workflow", options: RunOptions) -> RunResult | None:
    """Compile and run `workflow`, and wait for it: see `Workflow.run`."""
    request = _run_request(workflow, options, refuse_realtime=False)
    if request.generate_run_script:
        run_local.run(request)
        return None
    return run_local.start(request).collect()


async def run_workflow_async(workflow: "Workflow", options: RunOptions) -> RunResult | None:
    """`run_workflow`, awaited: see `Workflow.run_async`."""
    request = await asyncio.to_thread(_run_request, workflow, options, refuse_realtime=True)
    if request.generate_run_script:
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
