"""The Phase 3 acceptance cells, each authored with the Python API and run under pinned Nextflow.

Cells are defined in the offline Phase 3 design; the lowerings in design_docs/sophios_nextflow_backend.md §6. The
calculator (A1-A9) and predicate (W1-W7) cells, built here for the import's fixed point (test_import.py), are decided
by node and the pinned Nextflow in test_subset_differential.py, and S1-S14 are examples of test_runtime.py's scatter
property. A10, a computed ``valueFrom`` reaching a tool argument, has no runtime test: the backend rejects it.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path
import subprocess
from typing import Any

import pytest

from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import execute_nextflow


def _text_output() -> Output:
    """A string output holding the contents of out.txt."""
    return Output(cwl.string, glob="out.txt", load_contents=True, output_eval="$(self[0].contents)")


def _run(
    root: Workflow, directory: Path, *emits: str
) -> tuple["subprocess.CompletedProcess[str]", dict[str, list[str]]]:
    """Lower, render, observe each named workflow output in arrival order, and run."""
    workflow = compiled_source_to_nextflow(compile_workflow_result(root))
    write_nextflow_artifacts(workflow, directory)
    script = directory / "workflow.nf"
    text = script.read_text(encoding="utf-8")
    sinks = "".join(
        f"\n    {workflow.name}.out.{emit}.toList().subscribe {{ list -> "
        f"new File('{emit}.sink').text = list.join('\\u0001') }}\n"
        for emit in emits
    )
    script.write_text(text[:-2] + sinks + "}\n", encoding="utf-8")
    result = execute_nextflow(directory)
    observed = {
        emit: (directory / f"{emit}.sink").read_text(encoding="utf-8").split("\x01")
        for emit in emits
        if (directory / f"{emit}.sink").exists()
    }
    return result, observed


def _ran(directory: Path, name: str) -> bool:
    return bool(list((directory / "work").glob(f"*/*/{name}")))


def _printf(name: str, *ports: str) -> CommandLineTool:
    """printf the hyphen-joined ports, captured as the step's text output."""
    declared = {port: Input(cwl.string, position=index) for index, port in enumerate(ports, start=1)}
    tool = CommandLineTool(name, Inputs(**declared), Outputs(line=_text_output()))
    return tool.base_command("printf").stdout("out.txt").argument("-".join(["%s"] * len(ports)), position=0)


def _scatter_step(name: str, method: str, values: dict[str, Any]) -> Step:
    step = Step(_printf(name, *values), step_name=name.lower())
    for port, value in values.items():
        setattr(step.inputs, port, value)
    step.scatter_on(*(getattr(step.inputs, port) for port in values if isinstance(values[port], list)), method=method)
    return step


# A1-A9 -------------------------------------------------------------------

CALCULATIONS = {
    "A1": ["$(inputs.i + 0.5)", "$(3)"],
    "A2": ["$(inputs.i - inputs.x + 1)"],
    "A3": ["$(inputs.i * inputs.x / 4)"],
    "A4": ["$(-7 % 3)", "$(inputs.i % -4)"],
    "A5": ["$(Math.pow(inputs.i, 2))", "$(Math.pow(inputs.x, -1))"],
    "A6": ["$(-(inputs.i + 2) * +3)"],
    "A7": ["$(Math.sqrt(inputs.x * 4) + Math.abs(-inputs.i))"],
    "A8": ["$(Math.floor(inputs.x) + Math.ceil(inputs.x))", "$(Math.round(-2.5))", "$(Math.round(1e19))"],
    "A9": ["$(Math.min(inputs.i, 1))", "$(Math.max(1, 2, inputs.x))"],
}
CALCULATED = "7.5 3 5.75 3.9375 -1 3 49 0.4444444444444444 -27 10 5 -2 10000000000000000000 1 2.25"


def _calc_tool(name: str) -> CommandLineTool:
    tool = CommandLineTool(name, Inputs(i=Input(cwl.int), x=Input(cwl.float)),
                           Outputs(out=Output(cwl.file, glob=f"{name}.txt")))
    return tool.base_command("touch").argument(f"{name}.txt", position=1)


def _calc_workflow(whens: list[str], name: str) -> Workflow:
    steps = []
    for index, when in enumerate(whens):
        step = Step(_calc_tool(f"c{index}"), step_name=f"c{index}")
        step.inputs.i, step.inputs.x = 7, 2.25
        step.when = when
        steps.append(step)
    return Workflow(steps, name)


def calculator_workflow() -> Workflow:
    """A1-A9: a step for each calculation compared in a when with what node computes, then with twice that."""
    expressions = [text for texts in CALCULATIONS.values() for text in texts]
    whens = [
        f"$(({text[2:-1]}) === {value}{scale})"
        for scale in ("", " * 2") for text, value in zip(expressions, CALCULATED.split(), strict=True)
    ]
    return _calc_workflow(whens, "calc_cells")


@pytest.mark.nextflow
@pytest.mark.serial
def test_calculator_failures(tmp_path: Path) -> None:
    """A11: a non-finite subexpression fails the run, named; no task output is produced."""
    result, _ = _run(_calc_workflow(["$(inputs.i / (inputs.i - 7) > 0)"], "fail_cells"), tmp_path)
    assert result.returncode != 0
    assert "Sophios expression c0 when ((inputs.i / (inputs.i - 7)) > 0): (inputs.i / (inputs.i - 7)) is Infinity" in (
        result.stdout + result.stderr
    )
    assert not _ran(tmp_path, "c0.txt")


# W1-W11 ------------------------------------------------------------------

PREDICATES = {
    "$(true)": True,                                         # W1
    "$(false)": False,                                       # W1
    "$(inputs.flag)": True,                                  # W2
    "$(inputs.n > 2 && inputs.n <= 3)": True,                # W3
    "$(inputs.s === 'x' && inputs.s != 'y')": True,          # W4
    "$(inputs.s == 'y')": False,                             # W4
    "$(inputs.flag && !(inputs.n < 0) || false)": True,      # W5
    "$(Math.round(inputs.n / 2) >= 2)": True,                # W6
}


def _when_tool(name: str) -> CommandLineTool:
    ports = Inputs(flag=Input(cwl.boolean), s=Input(cwl.string), n=Input(cwl.int))
    tool = CommandLineTool(name, ports, Outputs(out=Output(cwl.file, glob=f"{name}.txt")))
    return tool.base_command("touch").argument(f"{name}.txt", position=1)


def predicates_workflow() -> Workflow:
    """W1-W6: a step for each predicate."""
    steps = []
    for index, predicate in enumerate(PREDICATES):
        step = Step(_when_tool(f"p{index}"), step_name=f"p{index}")
        step.inputs.flag, step.inputs.s, step.inputs.n = True, "x", 3
        step.when = predicate
        steps.append(step)
    return Workflow(steps, "when_cells")


@pytest.mark.nextflow
@pytest.mark.serial
def test_skipped_step_yields_null_and_downstream_completes(tmp_path: Path) -> None:
    """W8, W9: no task, a null workflow output, and an admitted optional consumer still runs."""
    produce = CommandLineTool("produce", Inputs(n=Input(cwl.int)), Outputs(result=_text_output()))
    produce = produce.base_command("echo").stdout("out.txt").argument("produced", position=1)
    consume = CommandLineTool(
        "consume", Inputs(maybe=Input(cwl.string, required=False)), Outputs(done=Output(cwl.file, glob="done.txt"))
    )
    consume = consume.base_command("touch").argument("done.txt", position=1)
    first = Step(produce, step_name="produce")
    first.inputs.n = 0
    first.when = "$(inputs.n > 0)"
    second = Step(consume, step_name="consume")
    second.inputs.maybe = first.outputs.result
    root = Workflow([first, second], "null_cells")
    root.outputs.result = first.outputs.result
    result, observed = _run(root, tmp_path, "result")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["result"] == ["[]"]
    assert not _ran(tmp_path, "out.txt")
    assert _ran(tmp_path, "done.txt")


@pytest.mark.nextflow
@pytest.mark.serial
def test_per_invocation_when_under_scatter(tmp_path: Path) -> None:
    """W10: the predicate runs per invocation; the gather keeps null at the skipped position."""
    step = _scatter_step("PAIR", "dotproduct", {"a": ["a", "b", "c"], "b": ["x", "y", "z"]})
    step.when = "$(inputs.a !== 'b')"
    root = Workflow([step], "scatter_when_cells")
    root.outputs.line = step.outputs.line
    result, observed = _run(root, tmp_path, "line")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["a-x", "[]", "c-z"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_condition_inside_an_inlined_subworkflow(tmp_path: Path) -> None:
    """W11: the inlined inner step keeps its condition; the outer step still runs."""
    inner = Step(_when_tool("inner"), step_name="inner")
    inner.inputs.flag, inner.inputs.s, inner.inputs.n = True, "x", 3
    inner.when = "$(inputs.n > 5)"
    child = Workflow([inner], "child")
    outer = Step(_when_tool("outer"), step_name="outer")
    outer.inputs.flag, outer.inputs.s, outer.inputs.n = True, "x", 3
    result, _ = _run(Workflow([child, outer], "inline_cells"), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (_ran(tmp_path, "inner.txt"), _ran(tmp_path, "outer.txt")) == (False, True)
