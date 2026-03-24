from pathlib import Path

import pytest

from sophios.apis.python.api import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTERS = REPO_ROOT / "cwl_adapters"


def _adapter(name: str) -> Path:
    return ADAPTERS / f"{name}.cwl"


@pytest.mark.fast
def test_explicit_step_ports_match_legacy_yaml() -> None:
    touch_legacy = Step(_adapter("touch"))
    touch_legacy.filename = "empty.txt"
    append_legacy = Step(_adapter("append"))
    append_legacy.file = touch_legacy.file
    append_legacy.str = "Hello"

    touch_explicit = Step(_adapter("touch"))
    touch_explicit.inputs.filename = "empty.txt"
    append_explicit = Step(_adapter("append"))
    append_explicit.inputs.file = touch_explicit.outputs.file
    append_explicit.inputs.str = "Hello"

    legacy_yaml = Workflow([touch_legacy, append_legacy], "wf").yaml
    explicit_yaml = Workflow([touch_explicit, append_explicit], "wf").yaml

    assert legacy_yaml == explicit_yaml


@pytest.mark.fast
def test_falsey_inline_values_are_preserved() -> None:
    echo = Step(_adapter("echo"))
    echo.inputs.message = ""

    workflow_yaml = Workflow([echo], "wf").yaml
    assert workflow_yaml["steps"][0]["in"]["message"] == {"wic_inline_input": ""}


@pytest.mark.fast
def test_subworkflow_inputs_use_child_workflow_name_and_formal_parameters() -> None:
    touch = Step(_adapter("touch"))
    touch.inputs.filename = "empty.txt"

    sub_step = Step(_adapter("append"))
    subworkflow = Workflow([sub_step], "child")
    sub_step.inputs.file = subworkflow.inputs.file
    sub_step.inputs.str = subworkflow.inputs.str

    subworkflow.inputs.file = touch.outputs.file
    subworkflow.inputs.str = "Hello"

    root_yaml = Workflow([touch, subworkflow], "root").yaml
    subworkflow_step = root_yaml["steps"][1]

    assert subworkflow_step["id"] == "child.wic"
    assert subworkflow_step["subtree"]["steps"][0]["in"]["file"] == "file"
    assert subworkflow_step["subtree"]["steps"][0]["in"]["str"] == "str"
