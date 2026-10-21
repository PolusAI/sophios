"""Channels and pipeline safety under installed, pinned Nextflow.

design_docs/sophios_nextflow_backend.md §5.2 and §6, Inputs and channels and
Topology; §7 for the stub every process carries.
"""

# pylint: disable=missing-function-docstring

from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import NF_INPUT_FUNCTION, NF_PARSER_MESSAGE, NF_STUB_MESSAGE, write_nextflow_artifacts
from sophios.nf_types import (
    NF_INPUT_HELPER, ExecutableNextflowWorkflow, NfWorkflowInputConnection, NfWorkflowOutputConnection,
)
from sophios.utils_nf import compilation_result_source, compiled_source_to_nextflow

from .testkit import execute_nextflow, flag_workflow, runtime_workflow


def _lower(flow: Workflow, params: dict[str, Any] | None = None) -> ExecutableNextflowWorkflow:
    """Lower ``flow``, with ``params`` among its workflow input values."""
    source = compilation_result_source(compile_workflow_result(flow))
    return compiled_source_to_nextflow(replace(source, params={**source.params, **(params or {})}))


def _run(
    flow: Workflow | ExecutableNextflowWorkflow, directory: Path, *arguments: str, params: dict[str, Any] | None = None
) -> tuple["subprocess.CompletedProcess[str]", dict[str, list[str]]]:
    """Lower ``flow`` with ``params`` as its workflow input values, run it, and read each workflow output.

    Each output is the text of every value it emits, a file's contents for a path.
    """
    workflow = flow if isinstance(flow, ExecutableNextflowWorkflow) else _lower(flow, params)
    write_nextflow_artifacts(workflow, directory)
    script = directory / "workflow.nf"
    emits = [
        connection.to_port for connection in workflow.connections if isinstance(connection, NfWorkflowOutputConnection)
    ]
    sinks = "".join(
        f"\n    {workflow.name}.out.{emit}.toList().subscribe {{ list -> new File('{emit}.sink').text = "
        f"list.collect {{ it instanceof java.nio.file.Path ? it.text : it.toString() }}.join('\\u0001') }}\n"
        for emit in emits
    )
    script.write_text(script.read_text(encoding="utf-8")[:-2] + sinks + "}\n", encoding="utf-8")
    result = execute_nextflow(directory, *arguments)
    observed = {
        emit: (directory / f"{emit}.sink").read_text(encoding="utf-8").split("\x01")
        for emit in emits
        if (directory / f"{emit}.sink").exists()
    }
    return result, observed


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_dry_run_stops_at_its_first_task_and_writes_nothing(tmp_path: Path) -> None:
    write_nextflow_artifacts(runtime_workflow(), tmp_path)
    result = execute_nextflow(tmp_path, "-stub-run")
    assert result.returncode == 1, result.stdout + result.stderr
    assert NF_STUB_MESSAGE in result.stdout + result.stderr
    assert not list((tmp_path / "work").rglob("message.txt"))
    assert not list((tmp_path / "work").rglob("copy.txt"))


def _scattered_and_whole(item: Any, read: str) -> Workflow:
    """One array input scattered at one step and taken whole at another: ``read`` prints a value."""
    each_tool = CommandLineTool(
        "each_value", Inputs(value=Input(item, position=1)), Outputs(line=Output(cwl.file, glob="each.txt"))
    ).base_command(read).stdout("each.txt")
    whole_tool = CommandLineTool(
        "all_values", Inputs(every=Input(cwl.array(item), position=1)), Outputs(line=Output(cwl.file, glob="all.txt"))
    ).base_command(read).stdout("all.txt")
    each, whole = Step(each_tool, step_name="each_value"), Step(whole_tool, step_name="all_values")
    flow = Workflow([each, whole], "shared")
    each.inputs.value = flow.inputs.shared.as_type({"type": "array", "items": item})
    whole.inputs.every = flow.inputs.shared
    each.scatter_on(each.inputs.value)
    flow.outputs.each, flow.outputs.whole = each.outputs.line, whole.outputs.line
    return flow


@pytest.mark.nextflow
@pytest.mark.serial
def test_one_string_array_is_scattered_at_one_step_and_taken_whole_at_another(tmp_path: Path) -> None:
    flow = _scattered_and_whole(cwl.string, "echo")
    result, observed = _run(flow, tmp_path, params={"shared": ["x", "y z"]})
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["each"] == ["x\n", "y z\n"]
    assert observed["whole"] == ["x y z\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_one_file_array_is_scattered_at_one_step_and_taken_whole_at_another(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")
    flow = _scattered_and_whole(cwl.file, "cat")
    files = [str(tmp_path / "a.txt"), {"class": "File", "path": str(tmp_path / "b.txt")}]
    result, observed = _run(flow, tmp_path, params={"shared": files})
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["each"] == ["a.txt\n", "b.txt\n"]
    assert observed["whole"] == ["a.txt\nb.txt\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_relative_file_path_names_a_file_in_the_launch_directory(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("a\nb\n", encoding="utf-8")
    result, observed = _run(flag_workflow(), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["result"] == ["b\na\n"]


class _JsonNumber(str):
    """Number text the params file holds as it is: Python writes no JSON number past the double range."""


class _Missing(str):
    """The path text the check names when it stops the run because nothing of the declared kind is there."""


# (value, declared type, the checked value, or None or _Missing when the check stops the run)
_ENTRY_CASES: list[tuple[Any, str, Any]] = [
    (True, "boolean", True), ("true", "boolean", True), ("false", "boolean", False),
    ("yes", "boolean", None), ("False", "boolean", None), (1, "boolean", None),
    (7, "int", 7), ("10", "int", 10), ("-3", "int", -3), ("2147483647", "int", 2147483647),
    ("2147483648", "int", None), (2147483648, "int", None), ("1.0", "int", None), ("1e1", "int", None),
    (" 1", "int", None), (1.5, "int", None), (True, "int", None),
    (1.5, "float", 1.5), (3, "float", 3), ("1.5e3", "float", 1500.0), (".5", "float", 0.5), ("-2", "float", -2.0),
    ("NaN", "float", None), ("Infinity", "float", None), ("1e400", "float", None), ("0x10", "float", None),
    ("1d", "float", None), (True, "float", None), (_JsonNumber("1e400"), "float", None),
    (_JsonNumber("-1e400"), "float", None),
    ("x", "string", "x"), ("10", "string", "10"), (5, "string", None), (False, "string", None),
    ([], "int?", []), ([], "string?", []), ([], "int", None), ([], "File?", None), (None, "int?", None),
    (["1", 2], "int[]", [1, 2]), ([], "string[]", []), ([], "string[]?", []), ("a", "string[]", None),
    ([1], "string[]", None), (["true"], "boolean[]", [True]),
    ("f.txt", "File", "f.txt"), ({"class": "File", "path": "f.txt"}, "File", "f.txt"), ("d", "File", _Missing("d")),
    ("missing", "File", _Missing("missing")), (5, "File", None), ("d", "Directory", "d"),
    ({"path": "d"}, "Directory", "d"), ({"class": "Directory", "path": "missing"}, "Directory", _Missing("missing")),
    ("f.txt", "Directory", _Missing("f.txt")), (["f.txt"], "File[]", ["f.txt"]),
    (["d", "f.txt"], "Directory[]", _Missing("f.txt")),
    ({"class": "Directory", "path": "f.txt"}, "File", None), ({"class": "File", "path": "d"}, "Directory", None),
    ("", "File", None), ({"path": ""}, "Directory", None),
]


@pytest.mark.nextflow
@pytest.mark.serial
def test_the_entry_check_passes_exact_values_and_stops_every_other(tmp_path: Path) -> None:
    """Every case runs through the generated check under the pinned Nextflow, its value read from JSON."""
    (tmp_path / "f.txt").write_text("f", encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "workflow.nf").write_text(
        f"""{NF_INPUT_FUNCTION}

workflow {{
    new File('checked.json').text = groovy.json.JsonOutput.toJson(params.cases.withIndex().collect {{ c, i ->
        try {{
            return [true, {NF_INPUT_HELPER}('c' + i, c[0], c[1])]
        }}
        catch( Exception e ) {{
            return [false, (e.cause ?: e).message]
        }}
    }})
}}
""",
        encoding="utf-8",
    )
    cases = ", ".join(
        f"[{value if isinstance(value, _JsonNumber) else json.dumps(value)}, {json.dumps(declared)}]"
        for value, declared, _ in _ENTRY_CASES
    )
    (tmp_path / "nextflow_params.json").write_text(f'{{"cases": [{cases}]}}', encoding="utf-8")
    (tmp_path / "nextflow.config").write_text("", encoding="utf-8")
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    checked = json.loads((tmp_path / "checked.json").read_text(encoding="utf-8"))
    for index, ((value, declared, expected), outcome) in enumerate(zip(_ENTRY_CASES, checked, strict=True)):
        if expected is None or isinstance(expected, _Missing):
            # A list's elements are checked one by one, and the message names the one that fails.
            failed = declared.split("[]")[0] if isinstance(value, list) and "[]" in declared else declared
            assert outcome[0] is False, (value, declared)
            reason = f"; no {failed} exists at '{expected}'" if isinstance(expected, _Missing) else ", not "
            assert outcome[1].startswith(f"Sophios workflow input 'c{index}' must be {failed}{reason}"), outcome
        else:
            assert outcome == [True, expected], (value, declared)


@pytest.mark.fast
@pytest.mark.parametrize("declared", ["File?", "Directory?"])
def test_no_model_holds_an_absent_path_the_entry_check_stops(declared: str) -> None:
    """Sophios never writes a parameter value its own entry check stops, read by a step or not."""
    workflow = runtime_workflow()
    with pytest.raises(ValueError, match=re.escape(f"workflow input 'unread' is an absent optional {declared},")):
        replace(workflow, params={**workflow.params, "unread": []},
                param_types={**workflow.param_types, "unread": declared})


def _echo_flow(when: str | None = None) -> Workflow:
    """``echo`` with a ``-n`` flag, a message and an int ``n`` a ``when`` may read."""
    tool = CommandLineTool(
        "say",
        Inputs(trim=Input(cwl.boolean, position=1, flag="-n"), message=Input(cwl.string, position=2), n=Input(cwl.int)),
        Outputs(line=Output(cwl.file, glob="said.txt")),
    ).base_command("echo").stdout("said.txt")
    say = Step(tool, step_name="say")
    say.inputs.trim, say.inputs.message, say.inputs.n = True, "hi", 3
    if when is not None:
        say.when = when
    flow = Workflow([say], "entry")
    flow.outputs.line = say.outputs.line
    return flow


def _param(workflow: ExecutableNextflowWorkflow, port: str) -> str:
    return next(
        connection.from_port for connection in workflow.connections
        if isinstance(connection, NfWorkflowInputConnection) and connection.to_port == port
    )


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_command_line_false_drops_a_flag(tmp_path: Path) -> None:
    workflow = _lower(_echo_flow())
    result, observed = _run(workflow, tmp_path / "default")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["hi"]
    result, observed = _run(workflow, tmp_path / "false", f"--{_param(workflow, 'trim')}", "false")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["hi\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_command_line_int_satisfies_a_when_that_compares_it(tmp_path: Path) -> None:
    workflow = _lower(_echo_flow("$(inputs.n == 10)"))
    n = _param(workflow, "n")
    result, observed = _run(workflow, tmp_path / "default")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["[]"]
    result, observed = _run(workflow, tmp_path / "ten", f"--{n}", "10")
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["line"] == ["hi"]


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize("value", ["ten", "1e1"])
def test_a_mistyped_value_stops_the_run_naming_the_parameter(value: str, tmp_path: Path) -> None:
    workflow = _lower(_echo_flow("$(inputs.n == 10)"))
    n = _param(workflow, "n")
    result, _ = _run(workflow, tmp_path, f"--{n}", value)
    assert result.returncode != 0
    assert f"Sophios workflow input '{n}' must be int, not '{value}'" in result.stdout + result.stderr
    assert not (tmp_path / "work").exists() or not list((tmp_path / "work").glob("*/*"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_the_v1_script_parser_stops_the_run_before_any_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The v1 parser would convert a command-line value before the entry check sees it."""
    monkeypatch.setenv("NXF_SYNTAX_PARSER", "v1")
    result, _ = _run(_echo_flow(), tmp_path)
    assert result.returncode != 0
    assert NF_PARSER_MESSAGE in result.stdout + result.stderr
    assert not (tmp_path / "work").exists() or not list((tmp_path / "work").glob("*/*"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_an_unscattered_steps_output_broadcasts_to_every_task_of_a_scattered_step(tmp_path: Path) -> None:
    """The note's step reads a conditional step's output, so its own output is a queue of one element."""
    gate_tool = CommandLineTool(
        "gate", Inputs(n=Input(cwl.int)),
        Outputs(said=Output(cwl.string, glob="said.txt", load_contents=True, output_eval="$(self[0].contents)")),
    ).base_command("echo").argument("yes", position=1).stdout("said.txt")
    write_tool = CommandLineTool(
        "write_note",
        Inputs(text=Input(cwl.string, position=1), after=Input(cwl.string, required=False)),
        Outputs(note=Output(cwl.file, glob="note.txt")),
    ).base_command("echo").stdout("note.txt")
    copy_tool = CommandLineTool(
        "copy_note",
        Inputs(note=Input(cwl.file, position=1), name=Input(cwl.string)),
        Outputs(copy=Output(cwl.file, glob="$(inputs.name).txt")),
    ).base_command("cp").argument("$(inputs.name).txt", position=2)
    gate = Step(gate_tool, step_name="gate")
    write, copy = Step(write_tool, step_name="write_note"), Step(copy_tool, step_name="copy_note")
    gate.inputs.n = 1
    gate.when = "$(inputs.n > 0)"
    write.inputs.text, write.inputs.after = "shared", gate.outputs.said
    copy.inputs.note = write.outputs.note
    copy.inputs.name = ["a", "b", "c"]
    copy.scatter_on(copy.inputs.name)
    flow = Workflow([gate, write, copy], "broadcast")
    flow.outputs.copies = copy.outputs.copy
    result, observed = _run(flow, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["copies"] == ["shared\n"] * 3
