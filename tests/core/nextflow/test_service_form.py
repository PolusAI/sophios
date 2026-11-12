"""The service form of a pipeline (design §4.5), the one file a compute service is sent."""

from functools import partial
from pathlib import Path

import pytest

from sophios.api.python._workflow_runtime import service_pipeline
from sophios.api.python.tool_builder import CommandLineTool, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.lang.diagnostics import SophiosError
from sophios.nf_reader import read_nextflow

from ..cases import DATA
from ..helpers import autoseg


#: The service form (design §4.5) of the autoseg shape: a containerized GPU process with no base command, a
#: writable-staged output folder and a renamed `./models`, scattered over the names of its output folders.
SERVICE_AUTOSEG = DATA / "nextflow" / "service" / "autoseg.nf"
_AUTOSEG = partial(autoseg, "input_directory", ["segmentation-a", "segmentation-b"], "model_directory", False)


@pytest.mark.fast
def test_the_service_form_of_the_autoseg_shape_is_its_golden(tmp_path: Path) -> None:
    """design §4.5: the service form is the pipeline with containers under Singularity and its outputs published,
    its parameters every path as written; the import refuses it, at the first line a local pipeline does not have."""
    pipeline = service_pipeline(_AUTOSEG())
    assert pipeline.script == SERVICE_AUTOSEG.read_text(encoding="utf-8")
    assert pipeline.params == {
        "autoseg_input": {"class": "Directory", "path": "input_directory"},
        "autoseg_model": {"class": "Directory", "path": "model_directory"},
        "autoseg_output": [{"class": "Directory", "path": "segmentation-a"},
                           {"class": "Directory", "path": "segmentation-b"}],
    }
    write_nextflow_artifacts(pipeline.nextflow_workflow, tmp_path)
    (tmp_path / "workflow.nf").write_text(pipeline.script, encoding="utf-8")
    with pytest.raises(SophiosError) as refused:
        read_nextflow(tmp_path)
    assert [str(diagnostic.code) for diagnostic in refused.value.diagnostics] == ["wic047"]
    # Only a process that asks for GPUs has them bound.
    script = service_pipeline(_AUTOSEG(gpu=False)).script
    assert "    container 'vjaganat90/ichnaea-segmentation-plugin:devn'\n" in script
    assert "containerOptions" not in script


@pytest.mark.fast
def test_a_service_refuses_an_output_nextflow_would_rename() -> None:
    """design §4.5: a client attaching to a run later reads its outputs by the names the pipeline published. A
    workflow that declares no output returns its steps', named `<step>~<port>`, which the user declares instead, a
    subworkflow's step's too, under a name Nextflow keeps."""
    tool = CommandLineTool("make", Inputs(), Outputs(made=Output(cwl.file, glob="made.txt")))
    step = Step(tool.base_command("touch", "made.txt"), step_name="make")
    workflow = Workflow([step], "renamed")
    nested = Workflow([Workflow([Step(tool, step_name="inner")], "innerwf"), Step(tool, step_name="outer")], "outerwf")
    for undeclared, published in ((workflow, ["make_made"]), (nested, ["innerwf_inner_made", "outer_made"])):
        with pytest.raises(SophiosError) as refused:
            service_pipeline(undeclared)
        assert [(str(diagnostic.code), diagnostic.message.split("; ")[-1])
                for diagnostic in refused.value.diagnostics] == [
            ("wic063", f"declare each output to return, under a name Nextflow keeps, such as {name!r}")
            for name in published]
    workflow.outputs.Result = step.outputs.made
    workflow.outputs.made = step.outputs.made
    with pytest.raises(SophiosError) as refused:
        service_pipeline(workflow)
    assert [(str(diagnostic.code), diagnostic.message.split(",")[0]) for diagnostic in refused.value.diagnostics] == [
        ("wic063", "workflow output 'Result' would be published as '_Result'")]
