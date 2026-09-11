"""Compiled real-workflow fixtures shared across the Nextflow backend suites."""

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.wic_types import RoseTree

from .testkit import REPO_ROOT


@pytest.fixture(scope="session")
def unsupported_real_linear_rose() -> RoseTree:
    """Compile the reviewer's real shell/primitive-output workflow."""
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    append = Step(clt_path=REPO_ROOT / "cwl_adapters" / "append.cwl")
    append.inputs.str = "Hello"
    cat = Step(clt_path=REPO_ROOT / "cwl_adapters" / "cat.cwl")
    return Workflow([touch, append, cat], "wf")._compile().rose


@pytest.fixture(scope="session")
def real_supported_rose() -> RoseTree:
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

    copy_tool = (
        CommandLineTool(
            "copy_file",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="copy.txt")),
        )
        .base_command("cp")
        .argument("copy.txt", position=2)
    )
    copy_step = Step(copy_tool, step_name="copy")
    copy_step.inputs.source = touch.outputs.result
    return Workflow([touch, copy_step], "wf")._compile().rose


@pytest.fixture(scope="session")
def real_scattered_rose() -> RoseTree:
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
    return Workflow([echo], "wf")._compile().rose


@pytest.fixture(scope="session")
def real_nested_rose() -> RoseTree:
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

    copy_tool = (
        CommandLineTool(
            "copy_file",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="copy.txt")),
        )
        .base_command("cp")
        .argument("copy.txt", position=2)
    )
    inner_copy = Step(copy_tool, step_name="inner_copy")
    child = Workflow([inner_copy], "child")
    inner_copy.inputs.source = child.inputs.source
    child.inputs.source = write.outputs.result

    return Workflow([write, child], "root")._compile().rose
