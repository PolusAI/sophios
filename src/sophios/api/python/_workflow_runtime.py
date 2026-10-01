"""Internal runtime helpers for the Python workflow API.

This module keeps filesystem loading, compilation, and execution details out
of the public workflow module so `Step` and `Workflow` stay focused on
Python-facing workflow authoring.
"""

# pylint: disable=protected-access
# This module is the private adapter layer between workflow objects and the
# compiler/runtime boundary, so reaching internal state is intentional.

import logging
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import yaml
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool
from cwl_utils.parser import load_document_by_uri, load_document_by_yaml

from sophios import compiler, input_output, plugins, post_compile as pc, run_local as rl
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import Diagnostics, Document, ParseResult, render
from sophios.ir.names import render_step_id
from sophios.cli import default_compilation_settings, get_known_and_unknown_args
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.utils import convert_args_dict_to_args_list
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import StepId, Tool, Tools

from ._errors import InvalidCLTError, InvalidStepError, WorkflowRunError
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
    "copy_output_files",
    "docker_remove_entrypoints",
    "generate_run_script",
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
            clt = load_document_by_uri(clt_path)
        except Exception as exc:
            raise InvalidCLTError(f"invalid cwl file: {clt_path}") from exc
        yaml_file = _load_yaml(clt_path)
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
        case dict() as yaml_file:
            pass
        case _:
            raise TypeError("document must be a mapping of CWL fields")
    try:
        clt = load_document_by_yaml(yaml_file, str(run_path))
    except Exception as exc:
        raise InvalidCLTError(f"invalid cwl document for: {run_path}") from exc
    return clt, yaml_file


def workflow_document(
    workflow: "Workflow",
    *,
    inline_subtrees: bool,
    directory: Path | None = None,
    document_stem: str | None = None,
) -> Document:
    """Build a workflow's language document.

    A workflow output's `outputSource` is always written in the compiler's
    concrete step-id spelling, because the compiler boundary consumes an
    explicit `outputSource` verbatim. There is no flag: a second spelling
    would be a second language, selectable per caller.

    Args:
        workflow (Workflow): Workflow to serialize.
        inline_subtrees (bool): Whether nested workflows should be embedded inline.
        directory (Path | None): Output directory for sibling `.wic` files.

    Returns:
        Document: The workflow's document; nested workflows appear as steps
        naming them, their bodies under `subtree` when inlined.
    """
    from .workflow import Workflow  # pylint: disable=import-outside-toplevel

    workflow_inputs: dict[str, Any] = {}
    for parameter in workflow._inputs:
        cwl_type = parameter.cwl_type()
        if cwl_type is None:
            raise InvalidStepError(
                f"workflow input {workflow.process_name}.{parameter.name} has no resolved type"
            )
        workflow_inputs[parameter.name] = {"type": cwl_type}

    # The compiler takes the step-id prefix from the *path it loads*, not from
    # process_name, so a document saved under another name must be spelled for
    # that name or its outputSource points at steps that do not exist.
    stem = document_stem if document_stem is not None else workflow.process_name
    # Keyed by object identity, not by process_name: a step renamed after an
    # output was bound to it still is the step the output names.
    compiled_step_ids = {
        id(step): render_step_id(
            stem,
            index,
            f"{step.process_name}.wic" if isinstance(step, Workflow) else step.process_name,
        )
        for index, step in enumerate(workflow.steps, start=1)
    }

    workflow_outputs: dict[str, Any] = {}
    for output_parameter in workflow._outputs:
        workflow_outputs[output_parameter.name] = output_parameter.to_workflow_output(
            step_ids=compiled_step_ids
        )

    return Document(
        steps=tuple(step._as_workflow_step(inline_subtrees=inline_subtrees, directory=directory)
                    for step in workflow.steps),
        passthrough=tuple((key, value) for key, value in
                          (("inputs", workflow_inputs), ("outputs", workflow_outputs)) if value),
    )


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


def workflow_wic_yaml(workflow: "Workflow", *, inline_subworkflows: bool = True) -> str:
    """Render a workflow as `.wic` YAML text.

    The text compiles correctly only when saved as `<process_name>.wic`. The
    compiler derives step ids from the name of the file it loads, and an
    explicit `outputSource` is consumed verbatim, so a document saved under
    another name names steps that do not exist. There is no destination here to
    spell them for; `write_workflow_wic` takes one and does.

    Args:
        workflow (Workflow): Workflow to serialize.
        inline_subworkflows (bool): Whether nested workflows should be embedded
            in the returned document. When false, nested workflows are expected
            to be written as sibling `.wic` files by `write_workflow_wic`.

    Returns:
        str: The serialized `.wic` YAML text.
    """
    from .workflow import Workflow  # pylint: disable=import-outside-toplevel

    workflow._validate()
    if not inline_subworkflows and any(isinstance(step, Workflow) for step in workflow.steps):
        raise ValueError(
            "to_wic_yaml(inline_subworkflows=False) cannot emit sibling files; "
            "use write_wic(..., inline_subworkflows=False) instead"
        )
    return render(workflow_document(workflow, inline_subtrees=inline_subworkflows))


def write_workflow_wic(
    workflow: "Workflow",
    path: str | Path | None = None,
    *,
    inline_subworkflows: bool = True,
) -> Path:
    """Write a workflow as a `.wic` file.

    Args:
        workflow (Workflow): Workflow to serialize.
        path (str | Path | None): Destination `.wic` path or output directory.
            When omitted, writes `<workflow>.wic` in the current directory.
        inline_subworkflows (bool): Whether nested workflows should be embedded
            in the root `.wic` file. When false, nested workflows are written as
            sibling `.wic` files beside the root document.

    Returns:
        Path: The path to the root `.wic` file that was written.
    """
    workflow._validate()
    output_path = _wic_output_path(workflow, path)
    output_path.parent.mkdir(exist_ok=True, parents=True)
    document = workflow_document(
        workflow,
        inline_subtrees=inline_subworkflows,
        directory=output_path.parent if not inline_subworkflows else None,
        document_stem=output_path.stem,
    )
    output_path.write_text(render(document), encoding="utf-8")
    return output_path


def _merged_known_tools(steps: list["Step"], tool_registry: Tools | None = None) -> Tools:
    """Merge known tools: step tools, then step registries, then the explicit registry."""
    merged_tools: Tools = {StepId(step.process_name, "global"): Tool(str(step.clt_path), step.yaml) for step in steps}
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
                workflow_document(step, inline_subtrees=False), Diagnostics())
    return documents


def compile_workflow_result(
    workflow: "Workflow",
    *,
    write_to_disk: bool = False,
    tool_registry: Tools | None = None,
    lang_version: str | None = None,
) -> CompilationResult:
    """Compile a Python API workflow to the graph-derived internal result.

    Args:
        workflow (Workflow): Workflow to compile.
        write_to_disk (bool): Whether to also emit generated files under `autogenerated/`.
        lang_version (str | None): Pin the Sophios language version for this
            compilation; None (the default) infers it. An explicit setting
            beats any file tag.
        tool_registry (Tools | None): Optional tool registry override.

    Returns:
        CompilationResult: The typed compiler output for the workflow.
    """
    workflow._validate()

    graph = get_graph_reps(workflow.process_name)
    merged_tools = _merged_known_tools(workflow._flatten_steps(), tool_registry)

    compiler_options, graph_settings, yaml_tag_paths = default_compilation_settings()
    if lang_version is not None:
        compiler_options = {**compiler_options, 'lang_version': lang_version}
    bundle = SourceBundle(
        ParseResult(workflow_document(workflow, inline_subtrees=False), Diagnostics()),
        Path(workflow.process_name).stem,
        RegistrySnapshot.from_tools(merged_tools, workflows=_nested_documents(workflow)))
    result = compiler.compile_source(
        bundle, compiler_options, graph_settings, yaml_tag_paths,
        relative_run_path=True, testing=False, graph_target=graph)
    if write_to_disk:
        input_output.write_artifacts_to_disk(result.artifact, Path("autogenerated/"), True)

    return result


def compiled_workflow_from_result(
    workflow: "Workflow",
    result: CompilationResult,
) -> CompiledWorkflow:
    """Build the public boundary from the typed internal result."""
    artifact = pc.inline_artifact_runs(result.artifact)
    cwl_workflow = normalize_artifact_cwl(artifact)
    if workflow._outputs:
        match cwl_workflow.get("outputs"):
            case dict() as outputs:
                cwl_workflow["outputs"] = {
                    output.name: outputs[output.name]
                    for output in workflow._outputs
                    if output.name in outputs
                }
    return CompiledWorkflow(
        name=workflow.process_name,
        cwl_workflow=cwl_workflow,
        cwl_job_inputs=normalize_artifact_job_inputs(artifact, artifact.job_inputs),
        lang_version=result.lang_version,
    )


def compiled_workflow(
    workflow: "Workflow",
    *,
    tool_registry: Tools | None = None,
    lang_version: str | None = None,
) -> CompiledWorkflow:
    """Compile a workflow into the public compiled-workflow boundary object.

    Args:
        workflow (Workflow): Workflow to compile.
        tool_registry (Tools | None): Optional tool registry override.
        lang_version (str | None): Pin the Sophios language version for this
            compilation; None infers it. An explicit setting beats file tags.

    Returns:
        CompiledWorkflow: Compiled CWL workflow plus generated job inputs.
    """
    result = compile_workflow_result(
        workflow,
        tool_registry=tool_registry,
        lang_version=lang_version,
    )
    return compiled_workflow_from_result(workflow, result)


def _run_args(overrides: dict[str, str] | None) -> dict[str, str]:
    """The default local-run settings with `overrides` applied.

    `quiet` is on unless the caller turns it off, and is normalised to the `yes`/`no`
    that `run_local` reads.
    """
    resolved = {**DEFAULT_RUN_ARGS, **(overrides or {})}
    resolved["quiet"] = "yes" if _enabled(resolved["quiet"]) else "no"
    return resolved


def _enabled(value: Any) -> bool:
    """Whether a yes/no style runtime option is on."""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def run_workflow(
    workflow: "Workflow",
    *,
    run_args_dict: dict[str, str] | None = None,
    user_env_vars: dict[str, str] | None = None,
    basepath: str = "autogenerated",
    tool_registry: Tools | None = None,
) -> None:
    """Compile and execute a workflow locally.

    Args:
        workflow (Workflow): Workflow to execute.
        run_args_dict (dict[str, str] | None): Runtime CLI options for local execution.
        user_env_vars (dict[str, str] | None): Environment variables to expose to the run.
        basepath (str): Directory used for generated files and execution artifacts.
        tool_registry (Tools | None): Optional tool registry override.

    Raises:
        WorkflowRunError: If the runner exits non-zero.
    """
    logger.info("Running %s", workflow.process_name)
    plugins.logging_filters()

    resolved_run_args = _run_args(run_args_dict)
    result = compile_workflow_result(workflow, tool_registry=tool_registry)
    artifact = pc.inline_artifact_runs(result.artifact)
    pc.verify_container_engine_config(resolved_run_args["container_engine"], False)
    input_output.write_artifacts_to_disk(
        artifact,
        Path(basepath),
        True,
        resolved_run_args.get("inputs_file", ""),
    )
    pc.cwl_docker_extract(
        resolved_run_args["container_engine"],
        resolved_run_args["pull_dir"],
        Path(basepath) / f"{workflow.process_name}.cwl",
    )
    if _enabled(resolved_run_args.get("docker_remove_entrypoints")):
        artifact = pc.remove_artifact_entrypoints(
            resolved_run_args["container_engine"], artifact)
    user_args = convert_args_dict_to_args_list(
        resolved_run_args,
        boolean_flags=_RUN_ARG_BOOLEAN_FLAGS,
    )

    _, unknown_args = get_known_and_unknown_args(workflow.process_name, user_args)
    retval = rl.run_local(
        resolved_run_args,
        False,
        workflow_name=workflow.process_name,
        basepath=basepath,
        passthrough_args=unknown_args,
        user_env_vars=dict(user_env_vars or {}),
        output_directories=rl.output_directories(result.graph),
    )
    if retval != 0:
        raise WorkflowRunError(workflow.process_name, retval)
