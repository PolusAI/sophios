"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
import re
from typing import Any, cast

import pytest

from sophios import compiler
from sophios.api.python import _workflow_runtime as workflow_runtime
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.tool_builder import (
    CommandLineTool,
    Input,
    Inputs,
    Output,
    Outputs,
    cwl,
)
from sophios.api.python.workflow import CompiledWorkflow, Step, StepInput, Workflow
from sophios.cli import default_compilation_settings
from sophios.input_output_nf import render_nextflow
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import Diagnostics, EdgeDef, EdgeRef, OutputBinding, ParseResult
from sophios.nf_types import NfPort, NfProcessConnection, NfWorkflowInputConnection
from sophios.utils_nf import (
    _ADMITTED_OUTPUT_EVAL,
    _NAMED_OUTPUT_EVAL_REJECTIONS,
    _output_capture,
    CompiledNextflowSource,
    compiled_source_to_nextflow,
)
from sophios.wic_types import Yaml

from ..cases import cases, match
from .testkit import case_source, REPO_ROOT, workflow_doc


@pytest.mark.fast
def test_the_shipped_touch_append_cat_workflow_compiles(real_linear_result: CompilationResult) -> None:
    """The tutorials' tools: append's quoted message and its writable entry, cat's file-text capture."""
    executable = compiled_source_to_nextflow(real_linear_result)
    assert [process.name for process in executable.processes] == ["touch", "append", "cat"]


@pytest.mark.fast
def test_a_literal_file_into_a_format_declaring_input_lowers_to_its_path(tmp_path: Path) -> None:
    """Sophios writes the input's `format` on the File value; like the input's, it is compile-time (design §3)."""
    source = tmp_path / "in.mdin"
    source.write_text("x", encoding="utf-8")
    fmt = CommandLineTool(
        "fmt",
        Inputs(src=Input(cwl.file, position=1, format="edam:format_2330")),
        Outputs(out=Output(cwl.file, glob="out.txt")),
    ).base_command("cat")
    step_ = Step(fmt, step_name="fmt")
    step_.inputs.src = str(source)
    result = compile_workflow_result(Workflow([step_], "wf"))
    assert list(result.artifact.job_inputs.values()) == [
        {"class": "File", "location": str(source), "format": "edam:format_2330"}
    ]
    assert list(compiled_source_to_nextflow(result).params.values()) == [{"class": "File", "path": str(source)}]


@pytest.mark.fast
def test_rejects_compiledworkflow_substitution() -> None:
    compiled = CompiledWorkflow("wf", workflow_doc([]), {})
    with pytest.raises(TypeError, match="CompilationResult"):
        compiled_source_to_nextflow(cast(Any, compiled))


def _findings(source: CompiledNextflowSource | CompilationResult) -> list[str]:
    """Return the exact aggregated finding lines of one rejected conversion."""
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(source)
    header, *lines = str(error.value).splitlines()
    assert header == "Nextflow capability analysis failed:"
    return [line.removeprefix("- ") for line in lines]


def _copy_tool(name: str = "copy.txt") -> CommandLineTool:
    return (
        CommandLineTool(
            "copy_file",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob=name)),
        )
        .base_command("cp")
        .argument(name, position=2)
    )


def _write_step() -> Step:
    write_tool = (
        CommandLineTool(
            "write_message",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="message.txt")),
        )
        .base_command("echo")
        .stdout("message.txt")
    )
    write = Step(write_tool, step_name="write")
    write.inputs.message = "nested composition"
    return write


def _copy_child(name: str, source: Any) -> Workflow:
    inner = Step(_copy_tool(), step_name="inner_copy")
    child = Workflow([inner], name)
    inner.inputs.source = child.inputs.source
    child.outputs.result = inner.outputs.result
    child.inputs.source = source
    return child


def _nested_result(*, siblings: int = 1) -> CompilationResult:
    write = _write_step()
    children = [_copy_child(f"child_{index}", write.outputs.result) for index in range(siblings)]
    steps: list[Step | Workflow] = [write, *children]
    return compile_workflow_result(Workflow(steps, "root"))


def _deep_nested_result() -> CompilationResult:
    write = _write_step()
    inner = Step(_copy_tool(), step_name="inner_copy")
    leaf = Workflow([inner], "leaf")
    inner.inputs.source = leaf.inputs.source
    middle = Workflow([leaf], "middle")
    leaf.inputs.source = middle.inputs.source
    middle.inputs.source = write.outputs.result
    return compile_workflow_result(Workflow([write, middle], "root"))


def _nested_downstream_result() -> CompilationResult:
    """Compile a child-output edge the Python surface deliberately cannot author yet."""
    write = _write_step()
    child = _copy_child("child", write.outputs.result)
    consume = Step(_copy_tool("consumed.txt"), step_name="consume")
    consume.inputs.source = "placeholder.txt"
    root = Workflow([write, child, consume], "root")
    document = workflow_runtime.workflow_document(root)
    steps = list(document.steps)
    steps[1] = replace(steps[1], outputs=(OutputBinding("result", EdgeDef("childout")),))
    steps[2] = replace(steps[2], inputs=(("source", EdgeRef("childout")),))
    compiler_options = default_compilation_settings()
    tools = workflow_runtime._merged_known_tools(root)
    bundle = SourceBundle(
        ParseResult(replace(document, steps=tuple(steps)), Diagnostics()),
        "root",
        RegistrySnapshot.from_tools(tools, workflows={
            ("global", child.process_name): ParseResult(
                workflow_runtime.workflow_document(child), Diagnostics()
            ),
        }),
    )
    return compiler.compile_source(bundle, compiler_options, testing=True)


def _nested_artifacts(result: CompilationResult) -> list[Any]:
    return [artifact for artifact in result.artifact.children if artifact.graph is not None]


def _workflow_call_index(result: CompilationResult) -> int:
    return next(
        index
        for index, step_node in enumerate(result.graph.steps)
        if step_node.run is not None and step_node.run.child is not None
    )


def _with_wrapper_semantic(
    result: CompilationResult,
    **changes: Any,
) -> CompilationResult:
    """A copy of `result` whose emitted workflow-call step carries `changes`."""
    changed = copy.deepcopy(result)
    changed.artifact.cwl["steps"][_workflow_call_index(changed)].update(changes)
    return changed


@pytest.mark.fast
def test_an_empty_resolved_subworkflow_projects_to_no_processes() -> None:
    result = compile_workflow_result(Workflow([Workflow([], "child")], "root"))
    assert compiled_source_to_nextflow(result).processes == ()


@pytest.mark.fast
def test_two_resolved_subworkflow_instances_do_not_collide() -> None:
    workflow = compiled_source_to_nextflow(_nested_result(siblings=2))
    child_names = [process.name for process in workflow.processes if "inner_copy" in process.name]
    assert len(child_names) == 2
    assert len(set(child_names)) == 2


@pytest.mark.fast
def test_a_resolved_subworkflow_output_reaches_a_downstream_outer_step() -> None:
    workflow = compiled_source_to_nextflow(_nested_downstream_result())
    connection = next(
        edge
        for edge in workflow.connections
        if isinstance(edge, NfProcessConnection) and edge.to_process.endswith("consume")
    )
    assert connection.from_process.endswith("inner_copy")


@pytest.mark.fast
def test_resolved_nesting_deeper_than_one_level_projects_recursively() -> None:
    workflow = compiled_source_to_nextflow(_deep_nested_result())
    assert [process.name for process in workflow.processes] == ["write", "middle_leaf_inner_copy"]


@pytest.mark.fast
def test_a_nested_workflow_steps_input_takes_one_source_as_it_is() -> None:
    """The core compiler joins one source to each input a nested workflow step binds."""
    result = _nested_result()
    wrapper = result.artifact.cwl["steps"][_workflow_call_index(result)]
    name, definition = next(iter(wrapper["in"].items()))
    source = definition["source"] if isinstance(definition, Mapping) else definition
    record = {"source": [source, source], "linkMerge": "merge_nested", "pickValue": "first_non_null", "default": 1}
    findings = _findings(_with_wrapper_semantic(result, **{"in": {**wrapper["in"], name: record}}))
    assert [finding for finding in findings if "takes one source" in finding] == [
        f"workflow.steps[1].in.{name}.{field}: a nested workflow step's input takes one source as it is; "
        "merge on a step inside the nested workflow"
        for field in ("default", "linkMerge", "pickValue", "source")
    ]


def _echo(name: str = "echo") -> Step:
    echo = Step(clt_path=REPO_ROOT / "cwl_adapters" / "echo.cwl", step_name=name)
    echo.inputs.message = "Hello"
    return echo


def _echo_with(extend: Callable[[Workflow, Step], None] | None) -> CompilationResult:
    """One echo step, which `extend(flow, echo)` gives a step input its tool does not declare."""
    echo = _echo()
    flow = Workflow([echo], "flow")
    if extend is not None:
        extend(flow, echo)
    return compile_workflow_result(flow)


def _unread_from_a_workflow_input(flow: Workflow, echo: Step) -> None:
    echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type("boolean?"))


def _unread_from_merged_workflow_inputs(flow: Workflow, echo: Step) -> None:
    echo.inputs.enabled = StepInput(
        source=[flow.inputs.loud.as_type("boolean?"), flow.inputs.quiet.as_type("boolean?")],
        link_merge="merge_flattened", pick_value="first_non_null", default=True,
    )


def _unread_without_a_source(_flow: Workflow, echo: Step) -> None:
    echo.inputs.enabled = StepInput(default=True)


@pytest.mark.fast
@pytest.mark.parametrize("extend, workflow_inputs", [
    pytest.param(_unread_from_a_workflow_input, ["loud"], id="workflow_input"),
    pytest.param(_unread_from_merged_workflow_inputs, ["loud", "quiet"], id="merged_workflow_inputs_with_a_default"),
    pytest.param(_unread_without_a_source, [], id="no_source"),
])
def test_a_step_input_its_tool_does_not_declare_and_nothing_reads_generates_nothing(
    extend: Callable[[Workflow, Step], None], workflow_inputs: list[str],
) -> None:
    """CWL: a step input the tool does not declare, and that nothing reads, is no error; it changes no run."""
    plain = compiled_source_to_nextflow(_echo_with(None))
    extended = compiled_source_to_nextflow(_echo_with(extend))
    assert extended.processes == plain.processes
    assert extended.connections == plain.connections
    # The workflow inputs it names stay parameters of the workflow, each checked like any other.
    plain_lines, extended_lines = render_nextflow(plain).splitlines(), render_nextflow(extended).splitlines()
    added = [line for line in extended_lines if line not in plain_lines]
    assert [line for line in plain_lines if line not in extended_lines] == []
    assert [re.fullmatch(r"    __sophios_input_\w+\('(\w+)', params\.\1, 'boolean\?'\)", line) is not None
            for line in added] == [True] * len(workflow_inputs)
    assert sorted(set(extended.params) - set(plain.params)) == workflow_inputs


@pytest.mark.fast
def test_a_scatter_over_an_input_its_tool_does_not_declare_is_rejected_by_name() -> None:
    def scattered(flow: Workflow, echo: Step) -> None:
        echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type("boolean[]"))
        echo.scatter_on(echo.inputs.enabled)

    assert [line for line in _findings(_echo_with(scattered)) if line.startswith("steps[0].in.")] == [
        "steps[0].in.enabled: scatter over an input the step's tool does not declare is not supported"
    ]


@pytest.mark.fast
def test_inner_input_source_is_taken_from_the_resolved_graph(
    real_nested_result: CompilationResult,
) -> None:
    result = copy.deepcopy(real_nested_result)
    nested = _nested_artifacts(result)[0]
    nested.cwl["steps"][0]["in"]["source"] = "ABSENT/result"
    workflow = compiled_source_to_nextflow(result)
    assert any(
        isinstance(edge, NfProcessConnection) and edge.from_process.endswith("write")
        for edge in workflow.connections
    )


@pytest.mark.fast
@pytest.mark.parametrize("requirement", ["SubworkflowFeatureRequirement", "MultipleInputFeatureRequirement"])
def test_accepts_an_inert_nested_workflow_feature_requirement(requirement: str) -> None:
    result = copy.deepcopy(_nested_result())
    _nested_artifacts(result)[0].cwl["requirements"] = {requirement: {}}
    assert compiled_source_to_nextflow(result).processes


@pytest.mark.fast
def test_a_scattered_step_inside_a_subworkflow_uses_the_outer_scatter_contract() -> None:
    echo_tool = (
        CommandLineTool(
            "echo_item",
            Inputs(item=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="out.txt")),
        )
        .base_command("echo")
        .stdout("out.txt")
    )
    echo = Step(echo_tool, step_name="echo_item")
    echo.inputs.item = ["a", "b"]
    echo.scatter_on(echo.inputs.item)
    child = Workflow([echo], "child")
    workflow = compiled_source_to_nextflow(
        compile_workflow_result(Workflow([child], "root"))
    )
    assert any(
        isinstance(edge, NfWorkflowInputConnection) and edge.adapter == "dotproduct"
        for edge in workflow.connections
    )


def _read_cwl_adapter(name: str) -> Yaml:
    """Load one real repository CWL adapter document."""
    import yaml

    return cast(Yaml, yaml.safe_load(
        (REPO_ROOT / "cwl_adapters" / name).read_text(encoding="utf-8")
    ))


@pytest.mark.fast
def test_the_admitted_output_eval_table_is_closed_in_both_directions() -> None:
    """Widening the admitted set without a design revision must fail here."""
    assert dict(_ADMITTED_OUTPUT_EVAL) == {
        "$(self[0])": "single",
        "$(self[0].contents)": "text",
    }
    assert set(_ADMITTED_OUTPUT_EVAL.values()) == set(NfPort.CAPTURE_QUALIFIERS)
    assert not set(_ADMITTED_OUTPUT_EVAL) & set(_NAMED_OUTPUT_EVAL_REJECTIONS)
    assert set(_NAMED_OUTPUT_EVAL_REJECTIONS) == {
        "$(self[0].checksum)",
        "$(self[0].dirname)",
        "$(self[0].format)",
        "$(self[0].location)",
        "$(self[0].path)",
        "$(self[0].secondaryFiles)",
    }


@pytest.mark.fast
def test_the_real_cat_adapter_output_is_the_admitted_file_text_capture() -> None:
    """cwl_adapters/cat.cwl is exactly this shape; it must lower, not reject."""
    document = _read_cwl_adapter("cat.cwl")

    assert document["outputs"]["output"]["outputBinding"] == {
        "glob": "output",
        "loadContents": True,
        "outputEval": "$(self[0].contents)",
    }
    assert _output_capture("output", document["outputs"]["output"]) == "text"


def _with_child(mutate: Callable[[Yaml], Any]) -> CompilationResult:
    """A copy of the nested result whose child workflow document `mutate` changes."""
    result = copy.deepcopy(_nested_result())
    mutate(_nested_artifacts(result)[0].cwl)
    return result


def _read_by_when(flow: Workflow, echo: Step) -> None:
    """The cookbook's when.wic: a step-only input that a when reads."""
    echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type("boolean?"))
    echo.when = "$(inputs.enabled)"


def _fed_by_a_step_output() -> CompilationResult:
    first, second = _echo("first"), _echo("second")
    second.inputs.after = StepInput(source=first.outputs.stdout)
    return compile_workflow_result(Workflow([first, second], "flow"))


def _nested_input_fed_by_a_step_output() -> CompilationResult:
    result = _nested_result()
    wrapper = result.artifact.cwl["steps"][_workflow_call_index(result)]
    return _with_wrapper_semantic(result, **{"in": {**wrapper["in"], "after": "write/result"}})


def _unconsumed_port_fields(child: Yaml) -> None:
    next(iter(child["inputs"].values()))["secondaryFiles"] = [".fai"]
    next(iter(child["outputs"].values()))["pickValue"] = "first_non_null"
    for section in ("inputs", "outputs"):
        next(iter(child[section].values()))["type"] = {"type": "array", "items": "File", "name": section}


def _value_from_on_a_call() -> CompilationResult:
    result = _nested_result()
    wrapper = result.artifact.cwl["steps"][_workflow_call_index(result)]
    return _with_wrapper_semantic(result, **{"in": {
        name: {**definition, "valueFrom": "$(self)"} if isinstance(definition, Mapping)
        else {"source": definition, "valueFrom": "$(self)"} for name, definition in wrapper["in"].items()}})


def _hinted_calls() -> CompilationResult:
    result = copy.deepcopy(_nested_result(siblings=2))
    for child in _nested_artifacts(result):
        child.cwl["hints"] = {"ResourceRequirement": {}}
    return result


def _only(finding: str) -> list[str]:
    """The pattern of a refusal whose one finding is `finding`."""
    return [r"\A" + re.escape("Nextflow capability analysis failed:\n- " + finding) + r"\Z"]


_FROM_A_STEP_OUTPUT = (
    "an input the step's {} does not declare is not supported from a step output: CWL runs this step "
    "only after that step finishes, and the Nextflow target would not wait for it"
)
_HINTS = "workflow hints are not supported"

#: Each compilation the target cannot lower, built through the Python API when its row runs, and the patterns its
#: refusal matches, one per cause.
API_REFUSALS: dict[str, tuple[Callable[[], CompilationResult], list[str]]] = {
    "a_when_that_reads_an_input_its_tool_does_not_declare": (partial(_echo_with, _read_by_when), _only(
        "steps[0].in.enabled: the step's when reads an input its tool does not declare, which is not supported")),
    "an_input_its_tool_does_not_declare_fed_by_a_step_output": (_fed_by_a_step_output, _only(
        "steps[1].in.after: " + _FROM_A_STEP_OUTPUT.format("tool"))),
    "a_nested_workflow_steps_undeclared_input_fed_by_a_step_output": (_nested_input_fed_by_a_step_output, _only(
        "workflow.steps[1].in.after: " + _FROM_A_STEP_OUTPUT.format("workflow"))),
    "unconsumed_nested_workflow_port_fields": (partial(_with_child, _unconsumed_port_fields), [
        "secondaryFiles are not supported", "a workflow output's pickValue is not supported",
        *(rf"\.run\.{section}\.\w+\.type\.name: an array schema's name" for section in ("inputs", "outputs"))]),
    "unconsumed_workflow_call_input_semantics": (_value_from_on_a_call, ["a step input's valueFrom is not supported"]),
    # One finding per call, and no third.
    "nested_findings_aggregate_across_workflow_calls": (_hinted_calls,
                                                        [rf"(?s)\A(?!(.*{_HINTS}){{3}})(.*{_HINTS}){{2}}"]),
    "scatter_on_a_resolved_subworkflow_call": (lambda: _with_wrapper_semantic(_nested_result(), scatter=["source"]),
                                               ["scatter on a nested workflow step"]),
    "when_on_a_resolved_subworkflow_call": (lambda: _with_wrapper_semantic(_nested_result(), when="$(true)"),
                                            ["a when on a nested-workflow step is not supported"]),
    "unsupported_nested_workflow_requirements": (partial(_with_child, lambda cwl: cwl.update(
        requirements={"StepInputExpressionRequirement": {}})), ["StepInputExpressionRequirement is not supported"]),
    "every_unconsumed_nested_workflow_field": (partial(_with_child, lambda cwl: cwl.update(
        hints={"ResourceRequirement": {}})), [rf"\.run\.hints: {_HINTS}"]),
    "a_nested_workflow_input_default_before_projection": (partial(_with_child, lambda cwl: cwl["inputs"][
        "source"].update(default="fallback.txt")), [r"\.run\.inputs\.\w+\.default: a nested workflow input's default"]),
    "long_on_a_nested_workflow_port_is_rejected_naming_the_type-inputs": (partial(_with_child, lambda cwl: cwl[
        "inputs"]["source"].update(type="long")), [re.escape(".run.inputs.source.type: long is not supported")]),
    "long_on_a_nested_workflow_port_is_rejected_naming_the_type-outputs": (partial(_with_child, lambda cwl: cwl[
        "outputs"]["result"].update(type="long")), [re.escape(".run.outputs.result.type: long is not supported")]),
}


@pytest.mark.fast
@pytest.mark.parametrize(("build", "patterns"), API_REFUSALS.values(), ids=API_REFUSALS)
def test_a_compilation_the_target_cannot_lower_is_refused_naming_each_cause(
        build: Callable[[], CompilationResult], patterns: list[str]) -> None:
    with pytest.raises(ValueError) as raised:
        compiled_source_to_nextflow(build())
    assert [pattern for pattern in patterns if not re.search(pattern, str(raised.value))] == []


@pytest.mark.fast
@pytest.mark.parametrize("case", cases(
    "nextflow/rejected", "tool", "out", "job", "workflow", "tools", "error", "findings"))
def test_a_rejected_source_is_refused_as_its_case_says(case: Yaml) -> None:
    """A case's source, its documents left as they were, is refused with exactly its `findings` after the
    aggregated header, or with an `error` that is no aggregate (tests/core/cases.py reads both)."""
    documents = copy.deepcopy(case)
    with pytest.raises(ValueError) as raised:
        compiled_source_to_nextflow(case_source(case))
    assert case == documents
    if "error" in case:
        assert match(case["error"], str(raised.value)), str(raised.value)
    else:
        header, *lines = str(raised.value).splitlines()
        assert header == "Nextflow capability analysis failed:"
        assert match(case["findings"], [line.removeprefix("- ") for line in lines]), lines
