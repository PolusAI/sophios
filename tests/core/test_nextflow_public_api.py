"""Public Python and CLI boundary tests for the Nextflow target."""

# pylint: disable=missing-function-docstring,protected-access

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from sophios import cli
import sophios.main as sophios_main
from sophios.api.python import _workflow_runtime as runtime
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import CompiledWorkflow, NextflowWorkflow, Step, Workflow


def _supported_workflow() -> Workflow:
    tool = (
        CommandLineTool(
            "emit_text",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(file=Output(cwl.file, glob="stdout.txt")),
        )
        .base_command("echo")
        .stdout("stdout.txt")
    )
    step = Step(tool, step_name="emit_text")
    step.inputs.message = "hello from Sophios"
    workflow = Workflow([step], "nextflow_public")
    workflow.outputs.file = step.outputs.file
    return workflow


@pytest.mark.serial
def test_default_and_explicit_cwl_targets_match() -> None:
    workflow = _supported_workflow()

    default = workflow.compile()
    explicit = workflow.compile(target="cwl")

    assert isinstance(default, CompiledWorkflow)
    assert explicit == default


@pytest.mark.serial
def test_nextflow_target_compiles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _supported_workflow()
    original = runtime.compile_workflow
    calls = 0

    def counted_compile(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "compile_workflow", counted_compile)

    compiled = workflow.compile(target="nextflow")

    assert isinstance(compiled, NextflowWorkflow)
    assert calls == 1


@pytest.mark.fast
def test_unknown_python_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported compilation target"):
        _supported_workflow().compile(target="unknown")  # type: ignore[call-overload]


@pytest.mark.serial
def test_to_nextflow_is_the_single_four_artifact_writer(tmp_path: Path) -> None:
    workflow = _supported_workflow()

    paths = workflow.to_nextflow(tmp_path)

    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]
    assert not hasattr(Workflow, "get_nextflow_workflow")
    assert not hasattr(Workflow, "to_nf")


@pytest.mark.fast
def test_nextflow_cli_target_is_standalone_and_rejects_cwl_modes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.get_args("workflow.wic", ["--target", "nextflow"]).target == "nextflow"
    assert cli.get_args("workflow.wic").target == "cwl"

    for flag in ("--generate_cwl_workflow", "--run_local", "--generate_run_script"):
        with pytest.raises(SystemExit, match="2"):
            cli.get_args("workflow.wic", ["--target", "nextflow", flag])
        assert "--target nextflow cannot be combined" in capsys.readouterr().err


@pytest.mark.fast
def test_nextflow_cli_conversion_error_is_concise_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rose_tree = _supported_workflow()._compile().rose
    capsys.readouterr()

    def reject_nested(_rose_tree: Any) -> NextflowWorkflow:
        raise ValueError("nested workflows are deferred to Phase 2")

    monkeypatch.setattr(sophios_main, "cwl_rosetree_to_nextflow", reject_nested)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match="1"):
        sophios_main._write_nextflow_target(rose_tree, "nested")

    error_path = tmp_path / "error_nested.txt"
    assert error_path.read_text(encoding="utf-8") == (
        "nested workflows are deferred to Phase 2; "
        "retry with --cwl_inline_subworkflows when flattening is valid\n"
    )
    assert capsys.readouterr().out == (
        "Failed to generate Nextflow artifacts. See error_nested.txt.\n"
    )


def _nextflow_executable() -> str:
    executable = shutil.which("nextflow")
    if executable is None:
        pytest.skip("Nextflow executable is not available locally; required CI provisions it")
    return executable


def _run_nextflow(directory: Path) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(directory / ".nxf-home"),
        "NXF_OFFLINE": "true",
    })
    return subprocess.run(
        [
            _nextflow_executable(),
            "run",
            "workflow.nf",
            "-params-file",
            "nextflow_params.json",
            "-c",
            "nextflow.config",
            "-work-dir",
            str(directory / "work"),
        ],
        cwd=directory,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


@pytest.mark.nextflow
@pytest.mark.serial
def test_python_and_cli_generated_artifacts_execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = _supported_workflow()
    python_dir = tmp_path / "python"
    workflow.to_nextflow(python_dir)

    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    monkeypatch.chdir(cli_root)
    sophios_main._write_nextflow_target(workflow._compile().rose, "nextflow_public")
    cli_dir = cli_root / "autogenerated"

    for directory in (python_dir, cli_dir):
        result = _run_nextflow(directory)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        outputs = list((directory / "work").rglob("stdout.txt"))
        assert len(outputs) == 1
        assert outputs[0].read_text(encoding="utf-8").strip() == "hello from Sophios"
