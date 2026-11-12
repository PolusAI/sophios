"""Closed-world capability analysis: what the Nextflow target cannot run is refused, by code, before any artifact."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from pathlib import Path

import pytest

from sophios.api.python.nextflow import NotLowered
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.wic_types import Yaml

from ..cases import case_dirs
from ..helpers import ADAPTERS
from .test_public_api import supported_workflow
from .testkit import refusal_of


@pytest.mark.fast
@pytest.mark.parametrize("family, expected", case_dirs("nextflow/refused", "roots"))
def test_a_family_of_constructs_is_refused_with_its_codes(family: Path, expected: Yaml) -> None:
    """Each directory of tests/core/data/nextflow/refused holds the constructs of one kind the target refuses, written
    as `.wic` roots and the tools they run. Every root, read as the command line reads a file, is refused in one
    lowering with exactly the refusals expected.yaml lists for it, in order: the code, step and subject of each, never
    its wording. `workflow.wic` holds every construct whose refusal is reported beside the others."""
    for root, refusals in expected["roots"].items():
        refused = refusal_of(family / root)
        assert [[refusal.code.value, refusal.step, refusal.subject] for refusal in refused.refusals] == refusals, root


def _not_a_number() -> Workflow:
    """A float given NaN, which the parameters' JSON cannot hold."""
    ratio = Step(CommandLineTool("ratio", Inputs(x=Input(cwl.float, position=1)), Outputs()).base_command("echo"))
    ratio.inputs.x = float("nan")
    return Workflow([ratio], "wf")


def _a_file_with_secondary_files() -> Workflow:
    """A File value holding secondary files, which the target stages as files of their own or not at all."""
    index = Step(CommandLineTool("index", Inputs(f=Input(cwl.file, position=1)), Outputs()).base_command("cat"))
    index.inputs.f = {"class": "File", "path": "reference.fa",
                      "secondaryFiles": [{"class": "File", "path": "reference.fa.fai"}]}
    return Workflow([index], "wf")


def _an_optional_pattern_output() -> Workflow:
    """An optional output of a pattern, which is checked where it reaches a workflow output, and reaches none."""
    logs = CommandLineTool("logs", Inputs(), Outputs(log=Output(cwl.file, glob="*.log", required=False)))
    workflow = Workflow([*supported_workflow().steps, Step(logs.base_command("true"), step_name="logs")], "wf")
    workflow.outputs.file = workflow.steps[0].outputs.file
    return workflow


def _a_written_value_of_any() -> Workflow:
    """A value written for an input typed Any, which the compiler makes a workflow input of."""
    pick = Step(clt_path=ADAPTERS / "array_indices.cwl", step_name="pick")
    pick.inputs.input_indices = [0]
    pick.inputs.input_array = ["a", "b"]
    return Workflow([pick], "flow")


@pytest.mark.fast
@pytest.mark.parametrize("build, refusals", [
    (_not_a_number, [("nf-s015", "ratio", "ratio~x")]),
    (_a_file_with_secondary_files, [("nf-s015", "index", "index~f")]),
    (_an_optional_pattern_output, [("nf-s030", None, None)]),
    (_a_written_value_of_any, [("nf-s033", "pick", "array_indices.cwl: outputs.output_array.outputBinding"),
                               ("nf-s048", "pick", "pick~input_array")]),
], ids=["not a number", "secondary files", "an optional pattern output", "a written value of Any"])
def test_a_refusal_only_python_reaches_names_the_line_python_wrote(
        build: Callable[[], Workflow], refusals: list[tuple[str, str | None, str | None]]) -> None:
    """What no `.wic` file can write is refused as the rest are, by code, step and subject, at the line of the
    Python that built it: a NaN and a File's secondary files, values JSON parameters cannot hold; an optional pattern
    output no workflow output takes, which the file door refuses
    sooner; a value written for an input of a type the target does not lower, at the line the value was written."""
    with pytest.raises(NotLowered) as refused:
        build().compile(target="nextflow")
    assert [(refusal.code.value, refusal.step, refusal.subject) for refusal in refused.value.refusals] == refusals
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    for refusal, diagnostic in zip(refused.value.refusals, refused.value.diagnostics, strict=True):
        if ".cwl: " not in (refusal.subject or ""):  # a tool's refusal is at the tool's line
            assert diagnostic.span is not None and Path(diagnostic.span.file) == Path(__file__), diagnostic
            assert lines[diagnostic.span.start_line - 1].lstrip().startswith(
                ("ratio.inputs.x", "index.inputs.f", "workflow = ", "pick.inputs.input_array")), diagnostic
