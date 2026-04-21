"""Internal runtime helpers for the Python workflow API.

This module keeps filesystem loading, compilation, and execution details out
of `api.py` so the public `Step` and `Workflow` classes stay focused on the
Python-facing DSL.
"""

from __future__ import annotations

# pylint: disable=protected-access
# This module is the private adapter layer between the DSL objects and the
# legacy compiler/runtime internals, so reaching internal state is intentional.

import logging
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Mapping, Protocol, TypeVar

import yaml
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool
from cwl_utils.parser import load_document_by_uri, load_document_by_yaml

from sophios import compiler, input_output, plugins, post_compile as pc, run_local as rl
from sophios.cli import get_dicts_for_compilation, get_known_and_unknown_args
from sophios.utils import convert_args_dict_to_args_list, step_name_str
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import CompilerInfo, Json, RoseTree, StepId, Tool, Tools, YamlTree

from ._errors import InvalidCLTError, InvalidStepError
from ._ports import InputParameter, OutputParameter, ParameterStore
from ._types import ScatterMethod
from ._utils import load_yaml as _load_yaml
from ._api_config import DEFAULT_RUN_ARGS

if TYPE_CHECKING:
    from .api import Step, Workflow


logger = logging.getLogger("WIC Python API")

ParameterT = TypeVar("ParameterT")


class _CWLParameterDefinition(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal structural type shared by parsed CWL input/output parameters."""

    id: Any
    type_: Any


def _parameter_name(parameter_id: Any) -> str:
    """Normalize a CWL parameter id to its public parameter name."""
    text = str(parameter_id)
    return text.rsplit("#", maxsplit=1)[-1].rsplit("/", maxsplit=1)[-1]


def coerce_path(value: str | Path | None, *, field_name: str, allow_none: bool = False) -> Path | None:
    """Normalize string-like path input to `Path`.

    Args:
        value (str | Path | None): Incoming path-like value.
        field_name (str): User-facing parameter name for error messages.
        allow_none (bool): Whether `None` should be accepted.

    Raises:
        TypeError: If the value is neither a `Path`, `str`, nor allowed `None`.

    Returns:
        Path | None: The normalized path or `None`.
    """
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
    """Return a parameter from a named parameter store.

    Args:
        parameters (ParameterStore[ParameterT]): Store holding the available parameters.
        name (str): Requested parameter name.
        owner_name (str): Human-readable process name for error messages.
        kind (str): Parameter kind, such as `"input"` or `"output"`.

    Raises:
        AttributeError: If the parameter does not exist.

    Returns:
        ParameterT: The requested parameter object.
    """
    try:
        return parameters.get(name)
    except KeyError as exc:
        raise AttributeError(f"{owner_name!r} has no {kind} named {name!r}") from exc


def _validate_scatter_assignment(items: list[Any], owner: Any | None = None) -> None:
    """Validate `scatter` assignments on a step."""
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


def _validate_scatter_method_assignment(scatter_method: str) -> None:
    """Validate the `scatterMethod` special step attribute."""
    allowed = {member.value for member in ScatterMethod}
    if scatter_method not in allowed:
        raise ValueError(
            "Invalid value for scatterMethod. "
            f"Valid values are: {', '.join(sorted(allowed))}"
        )


def _validate_when_assignment(condition: str) -> None:
    """Validate the `when` JavaScript expression wrapper."""
    if not condition.startswith("$(") or not condition.endswith(")"):
        raise ValueError("Invalid input to when. The js string must start with '$(' and end with ')'")


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
            _validate_scatter_assignment(items, owner=owner)
        case "scatter", invalid if invalid:
            raise TypeError("scatter must be assigned a list of InputParameter values")
        case "scatterMethod", str() as scatter_method if scatter_method:
            _validate_scatter_method_assignment(scatter_method)
        case "when", str() as condition if condition:
            _validate_when_assignment(condition)
        case "when", invalid if invalid:
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
    yaml_file = yaml.safe_load(yaml.safe_dump(dict(document), sort_keys=False))
    if not isinstance(yaml_file, dict):
        raise TypeError("document must be a mapping of CWL fields")
    try:
        clt = load_document_by_yaml(yaml_file, str(run_path))
    except Exception as exc:
        raise InvalidCLTError(f"invalid cwl document for: {run_path}") from exc
    return clt, yaml_file


def workflow_document(
    workflow: Workflow,
    *,
    inline_subtrees: bool,
    directory: Path | None = None,
    concrete_step_ids: bool = False,
) -> dict[str, Any]:
    """Render a workflow into its in-memory WIC YAML representation.

    Args:
        workflow (Workflow): Workflow to serialize.
        inline_subtrees (bool): Whether nested workflows should be embedded inline.
        directory (Path | None): Output directory for sibling `.wic` files.
        concrete_step_ids (bool): Whether workflow outputs should use the
            compiler's concrete step ids instead of the user-facing step names.

    Returns:
        dict[str, Any]: Serialized workflow document.
    """
    from .api import Workflow  # pylint: disable=import-outside-toplevel

    workflow_inputs: dict[str, dict[str, Any]] = {}
    for parameter in workflow._inputs:
        cwl_type = parameter.cwl_type()
        if cwl_type is None:
            raise InvalidStepError(
                f"workflow input {workflow.process_name}.{parameter.name} has no resolved type"
            )
        workflow_inputs[parameter.name] = {"type": cwl_type}

    compiled_step_ids = (
        {
            step.process_name: step_name_str(
                workflow.process_name,
                index,
                f"{step.process_name}.wic" if isinstance(step, Workflow) else step.process_name,
            )
            for index, step in enumerate(workflow.steps)
        }
        if concrete_step_ids
        else None
    )

    workflow_outputs: dict[str, dict[str, Any]] = {}
    for output_parameter in workflow._outputs:
        workflow_outputs[output_parameter.name] = output_parameter.to_workflow_output(
            step_id_overrides=compiled_step_ids
        )

    steps_yaml = [
        step._as_workflow_step(inline_subtrees=inline_subtrees, directory=directory)
        for step in workflow.steps
    ]
    document: dict[str, Any] = {"steps": steps_yaml}
    if workflow_inputs:
        document["inputs"] = workflow_inputs
    if workflow_outputs:
        document["outputs"] = workflow_outputs
    return document


def write_workflow_ast_to_disk(workflow: Workflow, directory: Path) -> None:
    """Write a workflow tree to disk as `.wic` files.

    Args:
        workflow (Workflow): Workflow to serialize.
        directory (Path): Destination directory.

    Returns:
        None: Files are written to disk as a side effect.
    """
    yaml_contents = workflow_document(workflow, inline_subtrees=False, directory=directory)
    directory.mkdir(exist_ok=True, parents=True)
    output_path = directory / f"{workflow.process_name}.wic"
    with output_path.open(mode="w", encoding="utf-8") as file_handle:
        file_handle.write(yaml.dump(yaml_contents, sort_keys=False, line_break="\n", indent=2))


def _extract_tools_paths_nonportable(steps: list[Step]) -> Tools:
    """Extract concrete tool definitions from instantiated steps.

    Args:
        steps (list[Step]): Steps whose backing CWL tools should be collected.

    Returns:
        Tools: A registry keyed by `StepId` that preserves local, non-portable paths.
    """
    return {StepId(step.process_name, "global"): Tool(str(step.clt_path), step.yaml) for step in steps}


def _step_registries(steps: list[Step]) -> Tools:
    merged_tools: Tools = {}
    for step in steps:
        merged_tools.update(step._tool_registry)
    return merged_tools


def _merged_known_tools(steps: list[Step], tool_registry: Tools | None = None) -> Tools:
    merged_tools = dict(_extract_tools_paths_nonportable(steps))
    merged_tools.update(_step_registries(steps))
    if tool_registry is not None:
        merged_tools.update(tool_registry)
    return merged_tools


def compile_workflow(
    workflow: Workflow,
    *,
    write_to_disk: bool = False,
    tool_registry: Tools | None = None,
) -> CompilerInfo:
    """Compile a Python API workflow into CWL.

    Args:
        workflow (Workflow): Workflow to compile.
        write_to_disk (bool): Whether to also emit generated files under `autogenerated/`.
        tool_registry (Tools | None): Optional tool registry override.

    Returns:
        CompilerInfo: The compiler output for the workflow.
    """
    workflow._validate()

    graph = get_graph_reps(workflow.process_name)
    yaml_tree = YamlTree(
        StepId(workflow.process_name, "global"),
        workflow_document(workflow, inline_subtrees=True, concrete_step_ids=True),
    )
    merged_tools = _merged_known_tools(workflow.flatten_steps(), tool_registry)

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
        input_output.write_to_disk(compiler_info.rose, Path("autogenerated/"), True)

    return compiler_info


def runtime_rose_tree(workflow: Workflow, *, tool_registry: Tools | None = None) -> RoseTree:
    """Compile a workflow and inline runtime tags for local execution.

    Args:
        workflow (Workflow): Workflow to prepare for execution.
        tool_registry (Tools | None): Optional tool registry override.

    Returns:
        RoseTree: Runtime-ready rose tree.
    """
    return pc.cwl_inline_runtag(compile_workflow(workflow, tool_registry=tool_registry).rose)


def compiled_cwl_json(workflow: Workflow, *, tool_registry: Tools | None = None) -> Json:
    """Return the compiled CWL workflow document plus generated inputs.

    Args:
        workflow (Workflow): Workflow to compile.
        tool_registry (Tools | None): Optional tool registry override.

    Returns:
        Json: JSON-serializable compiled workflow payload.
    """
    rose_tree = runtime_rose_tree(workflow, tool_registry=tool_registry)
    sub_node_data = rose_tree.data
    return {
        "name": workflow.process_name,
        "yaml_inputs": sub_node_data.workflow_inputs_file,
        **sub_node_data.compiled_cwl,
    }


def effective_run_args(run_args_dict: dict[str, str] | None = None) -> dict[str, str]:
    """Merge user runtime arguments with the default local-run settings.

    Args:
        run_args_dict (dict[str, str] | None): User-supplied runtime overrides.

    Returns:
        dict[str, str]: Effective runtime argument mapping.
    """
    effective = dict(DEFAULT_RUN_ARGS)
    if run_args_dict:
        effective.update(run_args_dict)
    return effective


def run_workflow(
    workflow: Workflow,
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

    Returns:
        None: The workflow is executed as a side effect.
    """
    logger.info("Running %s", workflow.process_name)
    plugins.logging_filters()

    resolved_run_args = effective_run_args(run_args_dict)
    rose_tree = runtime_rose_tree(workflow, tool_registry=tool_registry)
    pc.find_and_create_output_dirs(rose_tree)
    pc.verify_container_engine_config(resolved_run_args["container_engine"], False)
    input_output.write_to_disk(
        rose_tree,
        Path(basepath),
        True,
        resolved_run_args.get("inputs_file", ""),
    )
    pc.cwl_docker_extract(
        resolved_run_args["container_engine"],
        resolved_run_args["pull_dir"],
        Path(basepath) / f"{workflow.process_name}.cwl",
    )
    if resolved_run_args.get("docker_remove_entrypoints"):
        rose_tree = pc.remove_entrypoints(resolved_run_args["container_engine"], rose_tree)
    user_args = convert_args_dict_to_args_list(resolved_run_args)

    _, unknown_args = get_known_and_unknown_args(workflow.process_name, user_args)
    rl.run_local(
        resolved_run_args,
        False,
        workflow_name=workflow.process_name,
        basepath=basepath,
        passthrough_args=unknown_args,
        user_env_vars=dict(user_env_vars or {}),
    )
