"""Public Python and CLI contracts for the Nextflow target."""

# pylint: disable=missing-function-docstring,protected-access

import json
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from sophios import cli
import sophios.main as sophios_main
from sophios.api.python import _workflow_runtime as runtime
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import (
    CompiledNextflowWorkflow,
    CompiledWorkflow,
    Step,
    Workflow,
)
from sophios.nf_reader import read_nextflow
from sophios.nf_types import NfWorkflowOutputConnection

from .testkit import REPO_ROOT


def supported_workflow() -> Workflow:
    """One-process echo workflow built through the public tool builder."""
    echo_tool = (
        CommandLineTool(
            "emit_text",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(file=Output(cwl.file, glob="stdout.txt")),
        )
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


CUDA_NOTES = [
    f"note [wic046] cwltool:CUDARequirement {field} {value!r} is not checked: the Nextflow accelerator directive "
    "asks only for a number of GPUs (step 'gpu')"
    for field, value in (("cudaVersionMin", "11.4"), ("cudaComputeCapability", "3.0"))
]
FOUR_FILES = {"nextflow_workflow.json", "workflow.nf", "nextflow.config", "nextflow_params.json"}


@pytest.mark.fast
@pytest.mark.filterwarnings("error::UserWarning")
def test_a_nextflow_compile_keeps_every_note_and_warns_with_none() -> None:
    """The CWL compile's version and notes, then the lowering's notes; only a run warns (test_run_local)."""
    consume = CommandLineTool("consume", Inputs(file=Input(cwl.file, position=1)),
                              Outputs(result=Output(cwl.file, glob="result.txt"))).base_command("cat")
    workflow = Workflow([*gpu_workflow().steps, Step(consume)], "nextflow_noted")
    compiled = workflow.compile(target="nextflow")
    core = workflow.compile()
    assert [note for note in core.diagnostics if "note [wic042]" in note]
    assert (compiled.lang_version, *compiled.diagnostics) == (core.lang_version, *core.diagnostics, *CUDA_NOTES)
    assert compiled.nextflow_workflow.processes[0].resources.accelerator == 2


@pytest.mark.fast
def test_a_python_workflow_is_lowered_with_only_the_outputs_it_declares() -> None:
    """As `compile()` returns it, so a declared `emit_text_file` is not the step output `emit_text~file`, whose
    Nextflow name it would share."""
    workflow = supported_workflow()
    workflow.outputs.emit_text_file = workflow.steps[0].outputs.file
    compiled = workflow.compile(target="nextflow")
    assert [connection.to_port for connection in compiled.nextflow_workflow.connections
            if isinstance(connection, NfWorkflowOutputConnection)] == ["file", "emit_text_file"]


@pytest.mark.fast
def test_the_cli_reports_each_cuda_note_through_the_diagnostics_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    compilation = runtime.compile_workflow_result(gpu_workflow())
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    sophios_main._write_nextflow_target(compilation, "gpu", Path("out"), "text")
    assert capsys.readouterr().err.splitlines() == CUDA_NOTES
    sophios_main._write_nextflow_target(compilation, "gpu", Path("out"), "json")
    notes = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [(note["severity"], note["code"], note["step"]) for note in notes] == [("note", "wic046", "gpu")] * 2
    assert "accelerator 2" in (tmp_path / "out" / "workflow.nf").read_text(encoding="utf-8")


def adapter_workflow() -> Workflow:
    """One-process workflow built from a checked-in CWL adapter file."""
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    return Workflow([touch], "wf")


@pytest.mark.serial
def test_default_and_explicit_cwl_targets_match() -> None:
    workflow = supported_workflow()

    default = workflow.compile()
    explicit = workflow.compile(target="cwl")

    assert isinstance(default, CompiledWorkflow)
    assert explicit == default


@pytest.mark.serial
def test_one_nextflow_compile_writes_all_four_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`write()` writes the model the compile holds over the pipeline already there, and compiles nothing again."""
    supported_workflow().compile(target="nextflow").write(tmp_path)
    original = runtime.compile_workflow_result
    calls = 0

    def counted_compile(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "compile_workflow_result", counted_compile)

    compiled = adapter_workflow().compile(target="nextflow")
    compiled.write(tmp_path)

    assert isinstance(compiled, CompiledNextflowWorkflow)
    assert calls == 1
    assert read_nextflow(tmp_path)[0] == compiled.nextflow_workflow
    assert json.loads((tmp_path / "nextflow_workflow.json").read_text(encoding="utf-8")) == json.loads(
        compiled.nextflow_workflow.to_json())


@pytest.mark.fast
def test_write_writes_into_the_current_directory_or_the_one_given(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no argument, into the current directory; either way it returns the directory it wrote into."""
    compiled = supported_workflow().compile(target="nextflow")
    monkeypatch.chdir(tmp_path)
    assert compiled.write() == Path(".")
    assert compiled.write("out") == Path("out")
    assert {path.name for path in tmp_path.iterdir()} == FOUR_FILES | {"out"}
    assert {path.name for path in (tmp_path / "out").iterdir()} == FOUR_FILES


@pytest.mark.fast
def test_the_compiled_workflow_is_the_one_nextflow_writer() -> None:
    assert not any(hasattr(Workflow, name) for name in ("to_nextflow", "get_nextflow_workflow", "to_nf"))


@pytest.mark.fast
def test_unknown_python_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported compilation target"):
        cast(Any, supported_workflow()).compile(target="unknown")


_OPTION_GROUPS = {"every target": cli.neutral, **cli.target_options}


@pytest.mark.fast
def test_every_cli_option_belongs_to_exactly_one_group() -> None:
    declared = [action for action in cli.parser._actions if action.dest != "help"]
    grouped = [action for group in _OPTION_GROUPS.values() for action in group._group_actions]
    assert sorted(map(id, declared)) == sorted(map(id, grouped))


@pytest.mark.fast
def test_parsing_applies_the_target_check() -> None:
    assert cli.get_args("workflow.wic").target == "cwl"
    with pytest.raises(SystemExit, match="2"):
        cli.get_args("workflow.wic", ["--target", "nextflow", "--inputs_file", "inputs.yml"])


@pytest.mark.fast
@pytest.mark.parametrize("target", cli.target_options)
def test_an_option_set_for_another_target_is_rejected(target: str, capsys: pytest.CaptureFixture[str]) -> None:
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


@pytest.mark.fast
@pytest.mark.parametrize("target", cli.target_options)
def test_an_unknown_argument_names_passthrough_flags_under_every_target(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each target's runner takes passthrough arguments: CWL's runners, and Nextflow."""
    with pytest.raises(SystemExit, match="2"):
        sophios_main._main(cli.get_args("workflow.wic", ["--target", target]), ["--bogus"])
    error = capsys.readouterr().err.splitlines()[-1]
    assert error == ("main: error: unrecognized arguments: --bogus. Flags for the runner need "
                     "--passthrough_flags yes")


@pytest.mark.fast
def test_a_plain_cwl_workflow_under_allow_raw_cwl_is_lowered_under_nextflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "raw.cwl").write_text(
        "cwlVersion: v1.2\nclass: Workflow\ninputs:\n  msg: {type: string, default: hi}\noutputs: {}\n"
        f"steps:\n  say:\n    run: {REPO_ROOT / 'cwl_adapters' / 'echo.cwl'}\n"
        "    in: {message: msg}\n    out: [stdout]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="1"):
        sophios_main._main(cli.get_args("raw.cwl", ["--allow_raw_cwl", "--homedir", str(tmp_path)]), [])
    assert "Please specify either --generate_cwl_workflow" in capsys.readouterr().out
    sophios_main._main(cli.get_args("raw.cwl", [
        "--allow_raw_cwl", "--homedir", str(tmp_path), "--target", "nextflow", "--basepath", "out",
    ]), [])
    assert {path.name for path in (tmp_path / "out").iterdir()} == FOUR_FILES


@pytest.mark.fast
def test_nextflow_documentation_uses_supported_public_entry_points() -> None:
    documentation = (REPO_ROOT / "docs" / "nextflow.md").read_text(encoding="utf-8")

    assert 'compiled = workflow.compile(target="nextflow")\ncompiled.write("autogenerated")' in documentation
    assert "sophios --yaml workflow.wic --target nextflow" in documentation
    assert "workflow.to_nf(" not in documentation
    assert "to_nextflow(" not in documentation
    assert "--generate_cwl_workflow --target nextflow" not in documentation
    assert "sophios --yaml workflow.wic --target nextflow --run_local" in documentation
    assert 'workflow.run(target="nextflow")' in documentation
    for incompatible_flag in ("--generate_cwl_workflow", "--inputs_file", "--quiet"):
        assert f"`{incompatible_flag}`" in documentation


@pytest.mark.fast
def test_the_run_modes_act_under_nextflow_and_an_option_only_a_cwl_run_reads_does_not() -> None:
    for flags in (["--run_local"], ["--check"], ["--generate_run_script"], ["--passthrough_flags", "yes"]):
        assert cli.get_args("workflow.wic", ["--target", "nextflow", *flags]).target == "nextflow"
    for flags in (["--generate_cwl_workflow"], ["--quiet"], ["--cwl_runner", "toil-cwl-runner"],
                  ["--container_engine", "podman"], ["--cachedir", "cache"], ["--outdir", "out"]):
        with pytest.raises(SystemExit, match="2"):
            cli.get_args("workflow.wic", ["--target", "nextflow", *flags])
    with pytest.raises(SystemExit, match="2"):
        cli.get_args("workflow.wic", ["--run_local", "--generate_cwl_workflow"])


@pytest.mark.fast
def test_nextflow_cli_conversion_error_is_concise_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A workflow the lowering rejects leaves none of the four files an earlier write left in the base path."""
    compilation = runtime.compile_workflow_result(supported_workflow())
    capsys.readouterr()

    def reject_unsupported(_compilation: Any) -> NoReturn:
        raise ValueError("workflow.steps[0].when is deferred to Phase 3")

    monkeypatch.setattr(sophios_main, "compiled_source_to_nextflow_with_notes", reject_unsupported)
    monkeypatch.chdir(tmp_path)
    supported_workflow().compile(target="nextflow").write("autogenerated")
    capsys.readouterr()

    with pytest.raises(SystemExit, match="1"):
        sophios_main._write_nextflow_target(compilation, "unsupported", Path("autogenerated"), "text")

    assert not any((tmp_path / "autogenerated").iterdir())

    error_path = tmp_path / "error_unsupported.txt"
    assert error_path.read_text(encoding="utf-8") == (
        "workflow.steps[0].when is deferred to Phase 3\n"
    )
    assert capsys.readouterr().out == (
        "Failed to generate Nextflow artifacts. See error_unsupported.txt.\n"
    )
