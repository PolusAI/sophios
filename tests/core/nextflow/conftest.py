"""Compiled real-workflow fixtures shared across the Nextflow backend suites."""

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.workflow import Step, Workflow
from sophios.ir.artifacts import CompilationResult

from ..helpers import adapter_step


def _copy_step(step_name: str) -> Step:
    tool = CommandLineTool("copy_file", Inputs(source=Input(cwl.file, position=1)),
                           Outputs(result=Output(cwl.file, glob="copy.txt")))
    return Step(tool.base_command("cp").argument("copy.txt", position=2), step_name=step_name)


@pytest.fixture(scope="session")
def real_linear_result() -> CompilationResult:
    """Compile the shipped touch, append and cat tools as one workflow."""
    steps = [adapter_step("touch", filename="empty.txt"), adapter_step("append", str="Hello"), adapter_step("cat")]
    return compile_workflow_result(Workflow(steps, "wf"))


@pytest.fixture(scope="session")
def real_supported_result() -> CompilationResult:
    """Compile a wholly supported two-process workflow through the real API."""
    touch_tool = (
        CommandLineTool(
            "touch_file",
            Inputs(filename=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="$(inputs.filename)")),
        )
        .base_command("touch")
    )
    touch = Step(touch_tool, step_name="touch")
    touch.inputs.filename = "message.txt"

    copy_step = _copy_step("copy")
    copy_step.inputs.source = touch.outputs.result
    return compile_workflow_result(Workflow([touch, copy_step], "wf"))


@pytest.fixture(scope="session")
def real_scattered_result() -> CompilationResult:
    """Compile a real single-input scatter over an array-typed workflow input."""
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
    echo.inputs.item = ["alpha", "beta"]
    echo.scatter_on(echo.inputs.item)
    return compile_workflow_result(Workflow([echo], "wf"))


@pytest.fixture(scope="session")
def real_nested_result() -> CompilationResult:
    """Compile a real one-level subworkflow whose input comes from an outer step."""
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
    inner_copy = _copy_step("inner_copy")
    child = Workflow([inner_copy], "child")
    inner_copy.inputs.source = child.inputs.source
    child.inputs.source = write.outputs.result

    return compile_workflow_result(Workflow([write, child], "root"))
