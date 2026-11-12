"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
from collections.abc import Callable
from pathlib import Path
import re
from typing import Any, cast

import pytest

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
from sophios.input_output_nf import render_nextflow
from sophios.ir.artifacts import CompilationResult
from sophios.ir.resolved import NotLowered
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


@pytest.mark.fast
def test_an_empty_resolved_subworkflow_projects_to_no_processes() -> None:
    result = compile_workflow_result(Workflow([Workflow([], "child")], "root"))
    assert lowered(result).processes == ()


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
    assert f"{__file__}:{written}:1: error [nf-s048] workflow input 'pick~input_array': type: unsupported CWL type " \
        "for the Nextflow target: 'Any' (step 'pick')" in [str(line) for line in raised.value.diagnostics]


@pytest.mark.fast
@pytest.mark.parametrize("case", cases(
    "nextflow/rejected", "tool", "out", "job", "workflow", "tools", "error", "findings", "codes"))
def test_a_rejected_source_is_refused_as_its_case_says(case: Yaml) -> None:
    """A case's source, its documents left as they were, is refused with exactly its `findings`, or with refusals
    whose lines `error` matches (tests/core/cases.py reads both), each printed with the code of its reason, in order,
    when the case names its `codes`. A refusal is read without its span: a case's document is written out by
    `hermetic.bundle`, which decides its lines."""
    documents = copy.deepcopy(case)
    with pytest.raises(NotLowered) as raised:
        lowered_case(case)
    assert case == documents
    lines = refusal_lines(raised.value)
    if "error" in case:
        assert match(case["error"], "\n".join(lines)), lines
    else:
        assert match(case["findings"], lines), lines
    if "codes" in case:
        assert re.findall(r"\[((?:nf-s|wic)\d{3})\]", str(raised.value)) == case["codes"], str(raised.value)
