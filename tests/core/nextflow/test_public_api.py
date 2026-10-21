"""Public Python and CLI contracts for the Nextflow target."""

# pylint: disable=missing-function-docstring,protected-access

import json
from pathlib import Path
from typing import Any, cast

import pytest

from sophios import cli
import sophios.main as sophios_main
from sophios.api.python import _workflow_runtime as runtime
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import (
    CompiledWorkflow,
    ExecutableNextflowWorkflow,
    Step,
    Workflow,
)

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
    """One GPU process: a device count, and the two CUDA fields the target does not check."""
    gpu_tool = (
        CommandLineTool("gpu_tool", Inputs(), Outputs(file=Output(cwl.file, glob="out.txt")))
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


@pytest.mark.fast
def test_the_python_api_reports_each_cuda_note_as_a_user_warning() -> None:
    with pytest.warns(UserWarning) as caught:
        executable = gpu_workflow().compile(target="nextflow")
    assert [str(warning.message) for warning in caught if "[wic046]" in str(warning.message)] == CUDA_NOTES
    assert executable.processes[0].resources.accelerator == 2


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
def test_nextflow_target_compiles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = adapter_workflow()
    original = runtime.compile_workflow_result
    calls = 0

    def counted_compile(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "compile_workflow_result", counted_compile)

    compiled = workflow.compile(target="nextflow")

    assert isinstance(compiled, ExecutableNextflowWorkflow)
    assert calls == 1


@pytest.mark.fast
def test_unknown_python_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported compilation target"):
        cast(Any, supported_workflow()).compile(target="unknown")


@pytest.mark.serial
def test_to_nextflow_is_the_single_four_artifact_writer(tmp_path: Path) -> None:
    paths = supported_workflow().to_nextflow(tmp_path)

    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]
    assert not hasattr(Workflow, "get_nextflow_workflow")
    assert not hasattr(Workflow, "to_nf")


@pytest.mark.serial
def test_to_nextflow_writes_artifacts_for_adapter_workflows(tmp_path: Path) -> None:
    paths = adapter_workflow().to_nextflow(tmp_path)

    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]


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
def test_an_unknown_argument_names_passthrough_flags_only_under_cwl(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="2"):
        sophios_main._main(cli.get_args("workflow.wic", ["--target", target]), ["--bogus"])
    error = capsys.readouterr().err.splitlines()[-1]
    assert error.startswith("main: error: unrecognized arguments: --bogus")
    assert ("--passthrough_flags" in error) == (target == "cwl")


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
    assert {path.name for path in (tmp_path / "out").iterdir()} == {
        "nextflow_workflow.json", "workflow.nf", "nextflow.config", "nextflow_params.json",
    }


@pytest.mark.fast
def test_nextflow_documentation_uses_supported_public_entry_points() -> None:
    documentation = (REPO_ROOT / "docs" / "nextflow.md").read_text(encoding="utf-8")

    assert (
        'json_path, script, config, params = workflow.to_nextflow("autogenerated")'
        in documentation
    )
    assert "sophios --yaml workflow.wic --target nextflow" in documentation
    assert "workflow.to_nf(" not in documentation
    assert "--generate_cwl_workflow --target nextflow" not in documentation
    for incompatible_flag in (
        "--generate_cwl_workflow",
        "--run_local",
        "--generate_run_script",
        "--inputs_file",
    ):
        assert f"`{incompatible_flag}`" in documentation


@pytest.mark.fast
def test_a_rejected_workflow_leaves_no_artifacts_from_an_earlier_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous = runtime.write_nextflow_workflow(supported_workflow(), tmp_path)

    def reject_unsupported(_compilation: Any) -> ExecutableNextflowWorkflow:
        raise ValueError("unsupported")

    monkeypatch.setattr(runtime, "compiled_source_to_nextflow_with_notes", reject_unsupported)
    with pytest.raises(ValueError, match="unsupported"):
        runtime.write_nextflow_workflow(supported_workflow(), tmp_path)

    assert not any(path.exists() for path in previous)


@pytest.mark.fast
def test_nextflow_cli_conversion_error_is_concise_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    compilation = runtime.compile_workflow_result(supported_workflow())
    capsys.readouterr()

    def reject_unsupported(_compilation: Any) -> ExecutableNextflowWorkflow:
        raise ValueError("workflow.steps[0].when is deferred to Phase 3")

    monkeypatch.setattr(sophios_main, "compiled_source_to_nextflow_with_notes", reject_unsupported)
    monkeypatch.chdir(tmp_path)
    previous = runtime.write_nextflow_workflow(supported_workflow(), tmp_path / "autogenerated")
    capsys.readouterr()

    with pytest.raises(SystemExit, match="1"):
        sophios_main._write_nextflow_target(compilation, "unsupported", Path("autogenerated"), "text")

    assert not any(path.exists() for path in previous)

    error_path = tmp_path / "error_unsupported.txt"
    assert error_path.read_text(encoding="utf-8") == (
        "workflow.steps[0].when is deferred to Phase 3\n"
    )
    assert capsys.readouterr().out == (
        "Failed to generate Nextflow artifacts. See error_unsupported.txt.\n"
    )
