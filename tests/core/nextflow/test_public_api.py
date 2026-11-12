"""Public Python and CLI contracts for the Nextflow target."""

# pylint: disable=missing-function-docstring,protected-access

import json
from pathlib import Path
from typing import Any, cast

import pytest

from sophios import cli
import sophios.main as sophios_main
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import CompiledNextflowWorkflow, CompiledWorkflow, Step, Workflow
from sophios.lang import SophiosErrorCode
from sophios.lang.diagnostics import lines
from sophios.nf_reader import read_nextflow
from sophios.nf_types import NfWorkflowOutputConnection

from ..cases import DATA
from ..helpers import adapter_step, run_cli


def supported_workflow() -> Workflow:
    """One-process echo workflow built through the public tool builder."""
    echo_tool = (
        CommandLineTool(
            "emit_text", Inputs(message=Input(cwl.string, position=1)),
            Outputs(file=Output(cwl.file, glob="stdout.txt")))
        .base_command("echo")
        .stdout("stdout.txt")
    )
    step = Step(echo_tool, step_name="emit_text")
    step.inputs.message = "hello from Sophios"
    workflow = Workflow([step], "nextflow_public")
    workflow.outputs.file = step.outputs.file
    return workflow


def gpu_workflow() -> Workflow:
    """One GPU process with two equal outputs: a device count, and the two CUDA fields the target does not check."""
    gpu_tool = (
        CommandLineTool("gpu_tool", Inputs(), Outputs(file=Output(cwl.file, glob="out.txt"),
                                                      other=Output(cwl.file, glob="out.txt")))
        .base_command("touch", "out.txt")
        .gpu(cuda_version_min="11.4", compute_capability="3.0", device_count_min=2)
    )
    step = Step(gpu_tool, step_name="gpu")
    workflow = Workflow([step], "nextflow_gpu")
    workflow.outputs.file = step.outputs.file
    return workflow


#: The notes of `gpu_workflow()`: the two CUDA fields the Nextflow accelerator directive does not check.
CUDA_NOTES = [("note", "wic046", "gpu")] * 2
FOUR_FILES = {"nextflow_workflow.json", "workflow.nf", "nextflow.config", "nextflow_params.json"}


@pytest.mark.fast
@pytest.mark.filterwarnings("error::UserWarning")
def test_a_nextflow_compile_keeps_every_note_and_only_the_outputs_it_declares(
        capsys: pytest.CaptureFixture[str]) -> None:
    """The CWL compile's version and notes, then the lowering's, kept on the result and printed on stderr, and nothing
    warns; and the workflow outputs are the ones it declares, so a declared `emit_text_file` is not the step output
    `emit_text~file`, whose Nextflow name it would share."""
    consume = CommandLineTool("consume", Inputs(data=Input(cwl.file, position=1)),
                              Outputs(result=Output(cwl.file, glob="result.txt"))).base_command("cat")
    workflow = Workflow([*gpu_workflow().steps, Step(consume)], "nextflow_noted")
    compiled = workflow.compile(target="nextflow")
    printed = capsys.readouterr().err.splitlines()
    core = workflow.compile()
    assert [note for note in core.diagnostics if note.code is SophiosErrorCode.INFERENCE_GUESS]
    assert (compiled.lang_version, *compiled.diagnostics) == (core.lang_version, *core.diagnostics,
                                                              *compiled.diagnostics[len(core.diagnostics):])
    assert [(note.severity.value, note.code.value, note.locator and note.locator.step) for note in
            compiled.diagnostics[len(core.diagnostics):]] == CUDA_NOTES
    assert printed == lines(compiled.diagnostics)
    assert compiled.nextflow_workflow.processes[0].resources.accelerator == 2
    declared = supported_workflow()
    declared.outputs.emit_text_file = declared.steps[0].outputs.file
    assert [connection.to_port for connection in declared.compile(target="nextflow").nextflow_workflow.connections
            if isinstance(connection, NfWorkflowOutputConnection)] == ["file", "emit_text_file"]


@pytest.mark.fast
def test_the_cli_reports_notes_and_refusals_as_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A note reaches the diagnostics report in either form, and the pipeline is written; a workflow the target
    cannot run is reported as any error is, on stderr, at its line, with its code and step, exit 1 and no file to go
    and read; and it leaves none of the files an earlier write left."""
    monkeypatch.chdir(tmp_path)
    wic = gpu_workflow().write_wic(tmp_path)
    capsys.readouterr()

    def notes(form: str) -> list[str]:
        assert run_cli(monkeypatch, "--yaml", wic, "--target", "nextflow", "--basepath", "out",
                       "--diagnostics", form) is None
        return [line for line in capsys.readouterr().err.splitlines() if line.startswith(("note", "{"))]

    assert [line.split("]")[0] for line in notes("text")] == ["note [wic046"] * 2
    assert [(note["severity"], note["code"], note["step"]) for note in map(json.loads, notes("json"))] == CUDA_NOTES
    assert "accelerator 2" in (tmp_path / "out" / "workflow.nf").read_text(encoding="utf-8")

    supported_workflow().compile(target="nextflow").write("autogenerated")
    refused = DATA / "nextflow" / "cli" / "refused.wic"
    assert run_cli(monkeypatch, "--yaml", refused, "--target", "nextflow") == 1
    printed = capsys.readouterr().err
    assert "refused.wic:3:3: error [nf-s010]" in printed and "(step 'echo')" in printed, printed
    assert not (tmp_path / "error_refused.txt").exists()
    assert not any((tmp_path / "autogenerated").iterdir())


@pytest.mark.fast
def test_write_returns_the_directory_it_filled_and_replaces_the_pipeline_there(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no argument `write()` fills the current directory; either way it returns the directory it wrote into, with
    exactly the four files, and the model the compile holds replaces the pipeline already there."""
    compiled = supported_workflow().compile(target="nextflow")
    monkeypatch.chdir(tmp_path)
    assert compiled.write() == Path(".")
    assert compiled.write("out") == Path("out")
    assert {path.name for path in tmp_path.iterdir()} == FOUR_FILES | {"out"}
    assert {path.name for path in (tmp_path / "out").iterdir()} == FOUR_FILES

    replacement = Workflow([adapter_step("touch", filename="empty.txt")], "wf").compile(target="nextflow")
    assert isinstance(replacement, CompiledNextflowWorkflow)
    replacement.write(tmp_path)
    assert read_nextflow(tmp_path)[0] == replacement.nextflow_workflow
    assert json.loads((tmp_path / "nextflow_workflow.json").read_text(encoding="utf-8")) == json.loads(
        replacement.nextflow_workflow.to_json())


_OPTION_GROUPS = {"every target": cli.neutral, **cli.target_options}


@pytest.mark.fast
def test_a_target_takes_its_own_options_and_no_other(capsys: pytest.CaptureFixture[str]) -> None:
    """The target defaults to cwl, and one Sophios does not know is refused; every option belongs to exactly one
    group, every target or one target, and an option set for another target is refused naming the target it is for;
    the run modes act under nextflow, and an option only a CWL run reads does not; an unknown argument points to
    --passthrough_flags under every target."""
    workflow = supported_workflow()
    default = workflow.compile()
    assert isinstance(default, CompiledWorkflow) and workflow.compile(target="cwl") == default
    with pytest.raises(ValueError, match="unknown target 'unknown'"):
        cast(Any, workflow).compile(target="unknown")
    assert cli.get_args("workflow.wic").target == "cwl"
    declared = [action for action in cli.parser._actions if action.dest != "help"]
    grouped = [action for group in _OPTION_GROUPS.values() for action in group._group_actions]
    assert sorted(map(id, declared)) == sorted(map(id, grouped))
    for target in cli.target_options:
        for owner, group in _OPTION_GROUPS.items():
            for action in group._group_actions:
                args = cli.get_args("workflow.wic", ["--target", target])
                setattr(args, action.dest, object())
                if owner in {"every target", target}:
                    cli.validate_target_args(args)
                    continue
                with pytest.raises(SystemExit, match="2"):
                    cli.validate_target_args(args)
                assert f"{action.option_strings[0]} (--target {owner})" in capsys.readouterr().err
        with pytest.raises(SystemExit, match="2"):
            sophios_main._main(cli.get_args("workflow.wic", ["--target", target]), ["--bogus"])
        assert "--passthrough_flags" in capsys.readouterr().err.splitlines()[-1]
    for flags in (["--run_local"], ["--check"], ["--generate_run_script"], ["--passthrough_flags", "yes"]):
        assert cli.get_args("workflow.wic", ["--target", "nextflow", *flags]).target == "nextflow"
    for flags in (["--generate_cwl_workflow"], ["--quiet"], ["--cwl_runner", "toil-cwl-runner"],
                  ["--container_engine", "podman"], ["--cachedir", "cache"], ["--outdir", "out"],
                  ["--inputs_file", "inputs.yml"]):
        with pytest.raises(SystemExit, match="2"):
            cli.get_args("workflow.wic", ["--target", "nextflow", *flags])
    with pytest.raises(SystemExit, match="2"):
        cli.get_args("workflow.wic", ["--run_local", "--generate_cwl_workflow"])


@pytest.mark.fast
def test_a_plain_cwl_workflow_under_allow_raw_cwl_is_lowered_under_nextflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under the CWL target a plain workflow needs a mode to run in; under the Nextflow target it is lowered and
    written."""
    raw = str(DATA / "nextflow" / "cli" / "raw.cwl")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="1"):
        sophios_main._main(cli.get_args(raw, ["--allow_raw_cwl", "--homedir", str(tmp_path)]), [])
    assert "--generate_cwl_workflow" in capsys.readouterr().out
    sophios_main._main(cli.get_args(raw, [
        "--allow_raw_cwl", "--homedir", str(tmp_path), "--target", "nextflow", "--basepath", "out"]), [])
    assert {path.name for path in (tmp_path / "out").iterdir()} == FOUR_FILES
