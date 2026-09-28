"""All 36 Phase 3 acceptance cells, each authored with the Python API and run under pinned Nextflow.

Cells are defined in the offline Phase 3 design; the lowerings in
design_docs/sophios_nextflow_backend.md §6. Expected values are what node
computes, printed as cwltool prints them. Compatible cells share one run to
keep the runtime lane lean; ``CELLS`` maps every cell to the test that runs it,
and ``test_every_cell_is_claimed_by_a_runtime_test`` keeps that map total.
"""

# pylint: disable=missing-function-docstring

import inspect
from pathlib import Path
import subprocess
import sys
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


# A1-A10 ------------------------------------------------------------------

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


@pytest.mark.nextflow
@pytest.mark.serial
def test_calculator(tmp_path: Path) -> None:
    tool = CommandLineTool("calc", Inputs(i=Input(cwl.int), x=Input(cwl.float)), Outputs(line=_text_output()))
    tool = tool.base_command("printf").stdout("out.txt").argument("%s " * 14 + "%s", position=0)
    expressions = [text for texts in CALCULATIONS.values() for text in texts]
    for position, text in enumerate(expressions, start=1):
        tool = tool.argument(text, position=position)
    step = Step(tool, step_name="calc")
    step.inputs.i, step.inputs.x = 7, 2.25
    root = Workflow([step], "calc_cells")
    root.outputs.line = step.outputs.line
    result, observed = _run(root, tmp_path, "line")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == [CALCULATED]


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(("binding", "message"), [
    ({"argument": "$(inputs.i / (inputs.i - 7))"}, "(inputs.i / (inputs.i - 7)) is Infinity"),
    ({"value_from": "$(inputs.x / 3)"}, "is not integral but binds an integer port"),
    ({"argument": "$(inputs.x / 1e20)"}, "needs exponent notation, which is not supported"),
])
def test_calculator_failures(binding: dict[str, str], message: str, tmp_path: Path) -> None:
    """A11: each failure is deterministic and named; no task output is produced."""
    ports = {"i": Input(cwl.int), "x": Input(cwl.float)}
    if "value_from" in binding:
        ports["n"] = Input(cwl.int, position=1, value_from=binding["value_from"])
    tool = CommandLineTool("fail", Inputs(**ports), Outputs(out=Output(cwl.file, glob="out.txt")))
    tool = tool.base_command("echo").stdout("out.txt")
    if "argument" in binding:
        tool = tool.argument(binding["argument"], position=2)
    step = Step(tool, step_name="fail")
    step.inputs.i, step.inputs.x = 7, 2.25
    if "value_from" in binding:
        step.inputs.n = 1
    result, _ = _run(Workflow([step], "fail_cells"), tmp_path)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert not _ran(tmp_path, "out.txt")


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
    "$(inputs.maybe === null)": True,                        # W7
    "$(inputs.maybe !== null)": False,                       # W7
}


def _when_tool(name: str) -> CommandLineTool:
    ports = Inputs(
        flag=Input(cwl.boolean), s=Input(cwl.string), n=Input(cwl.int), maybe=Input(cwl.int, required=False)
    )
    tool = CommandLineTool(name, ports, Outputs(out=Output(cwl.file, glob=f"{name}.txt")))
    return tool.base_command("touch").argument(f"{name}.txt", position=1)


@pytest.mark.nextflow
@pytest.mark.serial
def test_predicates(tmp_path: Path) -> None:
    """W1-W7: every predicate kind decides the step, without truthiness."""
    steps = []
    for index, predicate in enumerate(PREDICATES):
        step = Step(_when_tool(f"p{index}"), step_name=f"p{index}")
        step.inputs.flag, step.inputs.s, step.inputs.n = True, "x", 3
        step.when = predicate
        steps.append(step)
    result, _ = _run(Workflow(steps, "when_cells"), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [_ran(tmp_path, f"p{index}.txt") for index in range(len(PREDICATES))] == list(PREDICATES.values())


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


# S1-S14 ------------------------------------------------------------------

@pytest.mark.nextflow
@pytest.mark.serial
def test_dotproduct_alignment_and_broadcast(tmp_path: Path) -> None:
    """S1, S2, S9: two and three inputs pair by index, in invocation order; a scalar broadcasts."""
    two = _scatter_step("TWO", "dotproduct", {"a": ["a", "b"], "b": ["x", "y"]})
    three = _scatter_step("THREE", "dotproduct", {"a": ["a", "b"], "b": ["x", "y"], "c": ["1", "2"]})
    broadcast = _scatter_step("BCAST", "dotproduct", {"a": ["a", "b"], "b": ["x", "y"], "k": "same"})
    root = Workflow([two, three, broadcast], "dot_cells")
    root.outputs.two, root.outputs.three, root.outputs.bcast = (
        two.outputs.line, three.outputs.line, broadcast.outputs.line
    )
    result, observed = _run(root, tmp_path, "two", "three", "bcast")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed == {"two": ["a-x", "b-y"], "three": ["a-x-1", "b-y-2"], "bcast": ["a-x-same", "b-y-same"]}


@pytest.mark.nextflow
@pytest.mark.serial
def test_dotproduct_length_mismatch(tmp_path: Path) -> None:
    """S3: the run fails naming each input and its length; no task runs truncated."""
    step = _scatter_step("PAIR", "dotproduct", {"a": ["a", "b"], "b": ["x"]})
    result, _ = _run(Workflow([step], "mismatch_cells"), tmp_path)
    assert result.returncode != 0
    assert "dotproduct scatter inputs have mismatched lengths" in result.stdout + result.stderr
    assert "=2" in result.stdout + result.stderr and "=1" in result.stdout + result.stderr
    assert not _ran(tmp_path, "out.txt")


@pytest.mark.nextflow
@pytest.mark.serial
def test_empty_scatter_inputs(tmp_path: Path) -> None:
    """S4: empty arrays run zero invocations and the run succeeds."""
    step = _scatter_step("PAIR", "dotproduct", {"a": [], "b": []})
    root = Workflow([step], "empty_cells")
    root.outputs.line = step.outputs.line
    result, observed = _run(root, tmp_path, "line")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == [""]
    assert not _ran(tmp_path, "out.txt")


@pytest.mark.nextflow
@pytest.mark.serial
def test_flat_crossproduct(tmp_path: Path) -> None:
    """S5, S6, S11: first input outermost, product cardinality, every output per invocation."""
    two = _scatter_step("TWO", "flat_crossproduct", {"a": ["a", "b"], "b": ["1", "0"]})
    three = _scatter_step("THREE", "flat_crossproduct", {"a": ["a", "b"], "b": ["x", "y", "z"], "c": ["1"]})
    root = Workflow([two, three], "flat_cells")
    root.outputs.two, root.outputs.three = two.outputs.line, three.outputs.line
    result, observed = _run(root, tmp_path, "two", "three")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["two"] == ["a-1", "a-0", "b-1", "b-0"]
    assert observed["three"] == [f"{a}-{b}-1" for a in "ab" for b in "xyz"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_nested_crossproduct(tmp_path: Path) -> None:
    """S7, S8: one dimension per input in declared order; an empty dimension keeps its outer shape."""
    two = _scatter_step("TWO", "nested_crossproduct", {"a": ["a", "b"], "b": ["x", "y", "z"]})
    three = _scatter_step("THREE", "nested_crossproduct", {"a": ["a", "b"], "b": ["x", "y"], "c": ["1"]})
    empty = _scatter_step("EMPTY", "nested_crossproduct", {"a": ["a", "b"], "b": []})
    root = Workflow([two, three, empty], "nested_cells")
    root.outputs.two, root.outputs.three, root.outputs.empty = two.outputs.line, three.outputs.line, empty.outputs.line
    result, observed = _run(root, tmp_path, "two", "three", "empty")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed == {
        "two": ["[a-x, a-y, a-z]", "[b-x, b-y, b-z]"],
        "three": ["[[a-x-1], [a-y-1]]", "[[b-x-1], [b-y-1]]"],
        "empty": ["[]", "[]"],
    }


def _files(directory: Path, *contents: str, stem: str = "in") -> list[str]:
    paths = []
    for index, content in enumerate(contents):
        path = directory / f"{stem}{index}.txt"
        path.write_text(content, encoding="utf-8")
        paths.append(str(path))
    return paths


@pytest.mark.nextflow
@pytest.mark.serial
def test_file_arrays_scatter_and_a_file_broadcasts(tmp_path: Path) -> None:
    """S10: each File element stages per invocation; an unscattered File reaches every invocation."""
    tool = CommandLineTool(
        "join", Inputs(f=Input(cwl.file, position=1), g=Input(cwl.file, position=2)),
        Outputs(line=Output(cwl.string, glob="joined.txt", load_contents=True, output_eval="$(self[0].contents)")),
    )
    step = Step(tool.base_command("cat").stdout("joined.txt"), step_name="join")
    step.inputs.f = _files(tmp_path, "a\n", "b\n")
    step.inputs.g = _files(tmp_path, "shared\n", stem="shared")[0]
    step.scatter_on(step.inputs.f, method="dotproduct")
    root = Workflow([step], "file_cells")
    root.outputs.line = step.outputs.line
    result, observed = _run(root, tmp_path / "run", "line")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["a\nshared\n", "b\nshared\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_gather_rescatter_and_composition(tmp_path: Path) -> None:
    """S12, S13, S14: calculator binding and dotproduct, gathered into a later scatter over Files,
    gathered again into one step and, independently, into a second consumer; each gather keeps order."""
    pair = CommandLineTool(
        "pair", Inputs(a=Input(cwl.string, position=1), b=Input(cwl.string, position=2), n=Input(cwl.int)),
        Outputs(out=Output(cwl.file, glob="out.txt")),
    ).base_command("echo").stdout("out.txt").argument("$(inputs.n * 10)", position=3)
    join = CommandLineTool(
        "join", Inputs(f=Input(cwl.file, position=1), g=Input(cwl.file, position=2)),
        Outputs(out=Output(cwl.file, glob="joined.txt")),
    ).base_command("cat").stdout("joined.txt")

    def concat(name: str) -> CommandLineTool:
        return CommandLineTool(
            name, Inputs(files=Input(cwl.array(cwl.file), position=1)),
            Outputs(all=Output(cwl.file, glob=f"{name}.txt")),
        ).base_command("cat").stdout(f"{name}.txt")

    first = Step(pair, step_name="pair")
    first.inputs.a, first.inputs.b, first.inputs.n = ["a", "b"], ["x", "y"], 2
    first.scatter_on(first.inputs.a, first.inputs.b, method="dotproduct")
    second = Step(join, step_name="join")
    second.inputs.f = first.outputs.out
    second.inputs.g = _files(tmp_path, "1\n", "2\n")
    second.scatter_on(second.inputs.f, second.inputs.g, method="dotproduct")
    gathered = Step(concat("all"), step_name="all")
    gathered.inputs.files = second.outputs.out
    independent = Step(concat("raw"), step_name="raw")
    independent.inputs.files = first.outputs.out
    result, _ = _run(Workflow([first, second, gathered, independent], "compose_cells"), tmp_path / "run")
    assert result.returncode == 0, result.stdout + result.stderr
    [combined] = list((tmp_path / "run" / "work").glob("*/*/all.txt"))
    [raw] = list((tmp_path / "run" / "work").glob("*/*/raw.txt"))
    assert combined.read_text(encoding="utf-8") == "a x 20\n1\nb y 20\n2\n"
    assert raw.read_text(encoding="utf-8") == "a x 20\nb y 20\n"


CELLS = {
    **{cell: test_calculator for cell in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10")},
    "A11": test_calculator_failures,
    **{f"W{index}": test_predicates for index in range(1, 8)},
    "W8": test_skipped_step_yields_null_and_downstream_completes,
    "W9": test_skipped_step_yields_null_and_downstream_completes,
    "W10": test_per_invocation_when_under_scatter,
    "W11": test_condition_inside_an_inlined_subworkflow,
    "S1": test_dotproduct_alignment_and_broadcast,
    "S2": test_dotproduct_alignment_and_broadcast,
    "S3": test_dotproduct_length_mismatch,
    "S4": test_empty_scatter_inputs,
    "S5": test_flat_crossproduct,
    "S6": test_flat_crossproduct,
    "S7": test_nested_crossproduct,
    "S8": test_nested_crossproduct,
    "S9": test_dotproduct_alignment_and_broadcast,
    "S10": test_file_arrays_scatter_and_a_file_broadcasts,
    "S11": test_flat_crossproduct,
    "S12": test_gather_rescatter_and_composition,
    "S13": test_gather_rescatter_and_composition,
    "S14": test_gather_rescatter_and_composition,
}


@pytest.mark.fast
def test_every_cell_is_claimed_by_a_runtime_test() -> None:
    expected = {f"A{n}" for n in range(1, 12)} | {f"W{n}" for n in range(1, 12)} | {f"S{n}" for n in range(1, 15)}
    assert set(CELLS) == expected
    module = sys.modules[__name__]
    for test in set(CELLS.values()):
        assert getattr(module, test.__name__) is test
        marks = {mark.name for mark in getattr(test, "pytestmark", [])}
        assert {"nextflow", "serial"} <= marks, test.__name__
        assert "tmp_path" in inspect.signature(test).parameters
