"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
from collections.abc import Callable
from dataclasses import replace
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
from sophios.ir.resolved import NotLowered
from sophios.lang import Diagnostics, EdgeDef, EdgeRef, OutputBinding, ParseResult
from sophios.nf_types import NfProcessConnection, NfWorkflowInputConnection
from sophios.utils_nf import NEXTFLOW
from sophios.wic_types import Yaml

from ..cases import cases, match
from ..helpers import ADAPTERS, adapter_step
from .testkit import lowered, lowered_case, refusal_lines


@pytest.mark.fast
def test_the_shipped_touch_append_cat_workflow_compiles(real_linear_result: CompilationResult) -> None:
    """The tutorials' tools: append's quoted message and its writable entry, cat's file-text capture."""
    executable = lowered(real_linear_result)
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
    assert list(lowered(result).params.values()) == [{"class": "File", "path": str(source)}]


@pytest.mark.fast
def test_rejects_compiledworkflow_substitution() -> None:
    empty = {"class": "Workflow", "cwlVersion": "v1.2", "inputs": {}, "outputs": {}, "steps": []}
    compiled = CompiledWorkflow("wf", empty, {})
    with pytest.raises(TypeError, match="resolved view"):
        NEXTFLOW.lower(cast(Any, compiled))


def _findings(source: CompilationResult) -> list[str]:
    """The refusals of one rejected lowering, as they print, less their spans."""
    with pytest.raises(NotLowered) as error:
        lowered(source)
    return refusal_lines(error.value)


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
    return compiler.compile_source(bundle, compiler_options, quiet=True)


def _nested_artifacts(result: CompilationResult) -> list[Any]:
    return [artifact for artifact in result.artifact.children if artifact.graph is not None]


@pytest.mark.fast
def test_an_empty_resolved_subworkflow_projects_to_no_processes() -> None:
    result = compile_workflow_result(Workflow([Workflow([], "child")], "root"))
    assert lowered(result).processes == ()


@pytest.mark.fast
def test_two_resolved_subworkflow_instances_do_not_collide() -> None:
    workflow = lowered(_nested_result(siblings=2))
    child_names = [process.name for process in workflow.processes if "inner_copy" in process.name]
    assert len(child_names) == 2
    assert len(set(child_names)) == 2


@pytest.mark.fast
def test_a_resolved_subworkflow_output_reaches_a_downstream_outer_step() -> None:
    workflow = lowered(_nested_downstream_result())
    connection = next(
        edge
        for edge in workflow.connections
        if isinstance(edge, NfProcessConnection) and edge.to_process.endswith("consume")
    )
    assert connection.from_process.endswith("inner_copy")


@pytest.mark.fast
def test_resolved_nesting_deeper_than_one_level_projects_recursively() -> None:
    workflow = lowered(_deep_nested_result())
    assert [process.name for process in workflow.processes] == ["write", "middle_leaf_inner_copy"]


def _echo(name: str = "echo") -> Step:
    return adapter_step("echo", name, message="Hello")


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
    plain = lowered(_echo_with(None))
    extended = lowered(_echo_with(extend))
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

    assert _findings(_echo_with(scattered)) == [
        "workflow 'flow': workflow input 'loud': required workflow input value is missing",
        "step 'echo': input 'enabled': scatter over an input the step's tool does not declare is not supported",
    ]


@pytest.mark.fast
def test_a_refused_input_made_from_a_written_value_is_refused_where_the_value_was_written() -> None:
    """A workflow input the compiler made from a value has no line of its own: the step owns its refusal, at the line
    the value was written."""
    pick = Step(clt_path=ADAPTERS / "array_indices.cwl", step_name="pick")
    pick.inputs.input_indices = [0]
    pick.inputs.input_array = ["a", "b"]
    written = Path(__file__).read_text(encoding="utf-8").splitlines().index(
        '    pick.inputs.input_array = ["a", "b"]') + 1
    with pytest.raises(NotLowered) as raised:
        lowered(compile_workflow_result(Workflow([pick], "flow")))
    assert f"{__file__}:{written}:1: step 'pick': workflow input 'pick~input_array': supplied value does not match " \
        "its supported CWL type" in [str(refusal) for refusal in raised.value.refusals]


@pytest.mark.fast
def test_inner_input_source_is_taken_from_the_resolved_graph(
    real_nested_result: CompilationResult,
) -> None:
    result = copy.deepcopy(real_nested_result)
    nested = _nested_artifacts(result)[0]
    nested.cwl["steps"][0]["in"]["source"] = "ABSENT/result"
    workflow = lowered(result)
    assert any(
        isinstance(edge, NfProcessConnection) and edge.from_process.endswith("write")
        for edge in workflow.connections
    )


@pytest.mark.fast
@pytest.mark.parametrize("requirement", ["SubworkflowFeatureRequirement", "MultipleInputFeatureRequirement"])
def test_accepts_an_inert_nested_workflow_feature_requirement(requirement: str) -> None:
    write = {"id": "write", "baseCommand": ["echo", "hi"], "stdout": "message.txt",
             "outputs": {"result": {"type": "File", "outputBinding": {"glob": "message.txt"}}}}
    child = {"requirements": {requirement: {}}, "steps": [{"id": "write"}]}
    workflow, _notes = lowered_case({"workflow": {"steps": [{"id": "child.wic", "subtree": child}]}, "tools": [write]})
    assert [process.name for process in workflow.processes] == ["child_write"]


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
    workflow = lowered(
        compile_workflow_result(Workflow([child], "root"))
    )
    assert any(
        isinstance(edge, NfWorkflowInputConnection) and edge.adapter == "dotproduct"
        for edge in workflow.connections
    )


@pytest.mark.fast
@pytest.mark.parametrize("case", cases(
    "nextflow/rejected", "tool", "out", "job", "workflow", "tools", "error", "findings"))
def test_a_rejected_source_is_refused_as_its_case_says(case: Yaml) -> None:
    """A case's source, its documents left as they were, is refused with exactly its `findings`, or with refusals
    whose lines `error` matches (tests/core/cases.py reads both). A refusal is read without its span: a case's
    document is written out by `hermetic.bundle`, which decides its lines."""
    documents = copy.deepcopy(case)
    with pytest.raises(NotLowered) as raised:
        lowered_case(case)
    assert case == documents
    lines = refusal_lines(raised.value)
    if "error" in case:
        assert match(case["error"], "\n".join(lines)), lines
    else:
        assert match(case["findings"], lines), lines
