"""Source-to-runtime conformance under installed, pinned Nextflow (R1-R11)."""

# pylint: disable=missing-function-docstring,protected-access

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import sophios.main as sophios_main
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_reader import parse_nf_file, promote_nextflow_document
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow
from sophios.wic_types import RoseTree

from .test_public_api import supported_workflow
from .testkit import (
    REPO_ROOT,
    command,
    execute_nextflow,
    output_port,
    ref,
    run_nextflow,
    runtime_workflow,
    single_process_workflow,
    template,
)

FOUR_STEP_MESSAGES = (
    "Sophios reaches Nextflow",
    "spaces remain one argument",
    "a literal costs $5",
    "literal && text is not shell syntax",
)


@pytest.mark.nextflow
@pytest.mark.serial
def test_actual_nextflow_golden_path(
    tmp_path: Path,
    real_supported_rose: RoseTree,
) -> None:
    result = run_nextflow(cwl_rosetree_to_nextflow(real_supported_rose), tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "work").rglob("copy.txt"))
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == ""


@pytest.mark.nextflow
@pytest.mark.serial
def test_real_compiler_preserves_adversarial_argument(tmp_path: Path) -> None:
    value = "price is $5; touch SHOULD_NOT_EXIST ' \" $(uname)\nsecond line"
    write_tool = (
        CommandLineTool(
            "write_message",
            Inputs(message=Input(cwl.string, position=2)),
            Outputs(result=Output(cwl.file, glob="result.txt")),
        )
        .base_command("printf")
        .argument("%s", position=1)
        .stdout("result.txt")
    )
    write = Step(write_tool, step_name="write")
    write.inputs.message = value
    workflow = Workflow([write], "wf")._compile().rose
    result = run_nextflow(cwl_rosetree_to_nextflow(workflow), tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [value]
    assert not list(tmp_path.rglob("SHOULD_NOT_EXIST"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_large_memory_directive_is_valid_nextflow_syntax(tmp_path: Path) -> None:
    workflow = ExecutableNextflowWorkflow(
        "WF",
        [NfProcess(
            "TASK",
            [],
            [],
            command("true"),
            resources=NfResources(cpus=1, memory_mb=1_234_567),
        )],
        [],
        {},
    )
    write_nextflow_artifacts(workflow, tmp_path)

    result = execute_nextflow(tmp_path, preview=True)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@pytest.mark.nextflow
@pytest.mark.serial
def test_file_object_parameter_executes_with_runtime_shape_dispatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("runtime file object\n", encoding="utf-8")
    copy_process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [output_port("result", "result.txt")],
        command("cp", template(ref("source")), "result.txt"),
    )
    workflow = ExecutableNextflowWorkflow(
        "WF",
        [copy_process],
        [NfWorkflowInputConnection("source", "COPY", "source")],
        {"source": {"class": "File", "path": str(source)}},
    )

    result = run_nextflow(workflow, tmp_path / "run")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "run" / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [
        "runtime file object\n"
    ]


@pytest.mark.nextflow
@pytest.mark.serial
def test_directory_staging_and_command_quoting(tmp_path: Path) -> None:
    source = tmp_path / "source_directory[1]"
    source.mkdir()
    (source / "input.txt").write_text("value with spaces", encoding="utf-8")
    process = NfProcess(
        "READ_DIRECTORY",
        [NfPort("source", "path", path_kind="directory")],
        [output_port("result", "result.txt")],
        command("cat", template(ref("source"), "/input.txt"), stdout="result.txt"),
    )
    workflow = single_process_workflow(
        process,
        params={"source": str(source)},
        output_port_name="result",
    )
    result = run_nextflow(workflow, tmp_path / "run")
    assert result.returncode == 0, result.stderr
    outputs = list((tmp_path / "run" / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == ["value with spaces"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_literal_dollar_and_mixed_backtick_globs_execute(tmp_path: Path) -> None:
    from sophios.input_output_nf import render_nextflow

    literal = NfProcess(
        "LITERAL",
        [],
        [output_port("result", "out$name.txt")],
        command("touch", "out$name.txt"),
    )
    mixed_glob = template("`", ref("name"), ".txt")
    mixed = NfProcess(
        "MIXED",
        [NfPort("name", "val")],
        [NfPort("result", "path", "result", mixed_glob)],
        command("touch", mixed_glob),
    )
    workflow = ExecutableNextflowWorkflow(
        "WF",
        [literal, mixed],
        [
            NfWorkflowInputConnection("name", "MIXED", "name"),
            NfWorkflowOutputConnection("LITERAL", "result", "literal"),
            NfWorkflowOutputConnection("MIXED", "result", "mixed"),
        ],
        {"name": "report"},
    )

    rendered = render_nextflow(workflow)
    assert "path 'out$name.txt', emit: result" in rendered
    assert 'path "`${name}.txt", emit: result' in rendered
    result = run_nextflow(workflow, tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert len(list((tmp_path / "work").rglob("out$name.txt"))) == 1
    assert len(list((tmp_path / "work").rglob("`report.txt"))) == 1


@pytest.mark.nextflow
@pytest.mark.serial
def test_fanout_and_resource_rendering(tmp_path: Path) -> None:
    from sophios.input_output_nf import render_nextflow

    produce = NfProcess(
        "PRODUCE",
        [],
        [output_port("result", "data.txt")],
        command("printf", "%s", "fanout", stdout="data.txt"),
        resources=NfResources(cpus=2, memory_mb=64),
    )
    consume_a = NfProcess(
        "CONSUME_A",
        [NfPort("source", "path")],
        [output_port("result", "a.txt")],
        command("cat", template(ref("source")), stdout="a.txt"),
    )
    consume_b = NfProcess(
        "CONSUME_B",
        [NfPort("source", "path")],
        [output_port("result", "b.txt")],
        command("cat", template(ref("source")), stdout="b.txt"),
    )
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [produce, consume_a, consume_b],
        [
            NfProcessConnection("PRODUCE", "result", "CONSUME_A", "source"),
            NfProcessConnection("PRODUCE", "result", "CONSUME_B", "source"),
            NfWorkflowOutputConnection("CONSUME_A", "result", "a"),
            NfWorkflowOutputConnection("CONSUME_B", "result", "b"),
        ],
        {},
    )
    result = run_nextflow(workflow, tmp_path)
    assert result.returncode == 0, result.stderr
    rendered = render_nextflow(workflow)
    assert "cpus 2" in rendered
    assert 'memory "64 MB"' in rendered
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("a.txt")] == ["fanout"]
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("b.txt")] == ["fanout"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_symbol_contract_matches_actual_v2_parser(tmp_path: Path) -> None:
    accepted = ("café", "Δelta", "$money", "_private", "Ⅻstep", "process")
    process_blocks = "\n".join(
        f'process {name} {{\n    script:\n    \"\"\"\n    true\n    \"\"\"\n}}'
        for name in accepted
    )
    calls = "\n".join(f"    {name}()" for name in accepted)
    source = f"nextflow.enable.dsl=2\n{process_blocks}\nworkflow {{\n{calls}\n}}\n"
    script = tmp_path / "symbols.nf"
    script.write_text(source, encoding="utf-8")
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(tmp_path / ".nxf-home"),
        "NXF_OFFLINE": "true",
        "NXF_SYNTAX_PARSER": "v2",
    })
    accepted_result = subprocess.run(
        ["nextflow", "run", "-preview", script.name],
        cwd=tmp_path,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert accepted_result.returncode == 0, accepted_result.stderr

    for invalid in ("if", "class", "_", "9start", "dash-name", "😀"):
        script.write_text(
            f'nextflow.enable.dsl=2\nprocess {invalid} {{\nscript:\n\"\"\"\ntrue\n\"\"\"\n}}\n'
            f'workflow {{\n    {invalid}()\n}}\n',
            encoding="utf-8",
        )
        rejected = subprocess.run(
            ["nextflow", "run", "-preview", script.name],
            cwd=tmp_path,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert rejected.returncode != 0, f"actual parser unexpectedly accepted {invalid!r}"


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_cli_generates_and_executes_nextflow_artifacts(tmp_path: Path) -> None:
    # The step name must match the adapter stem so the CLI can resolve
    # touch.cwl through search_paths_cwl.
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "result.txt"
    workflow_path = Workflow([touch], "workflow").write_wic(tmp_path)
    config = {
        "search_paths_cwl": {"global": [str(REPO_ROOT / "cwl_adapters")], "gpu": []},
        "search_paths_wic": {"global": [str(tmp_path)]},
        "renaming_conventions": [],
        "inference_rules": {},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sophios.main",
            "--yaml",
            str(workflow_path),
            "--config_file",
            str(config_path),
            "--homedir",
            str(tmp_path),
            "--target",
            "nextflow",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    generated = tmp_path / "autogenerated"
    assert {path.name for path in generated.iterdir()} >= {
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    }
    assert (generated / "nextflow.config").read_text(encoding="utf-8") == "docker.enabled = true\n"
    execution = execute_nextflow(generated)
    assert execution.returncode == 0, f"stdout:\n{execution.stdout}\nstderr:\n{execution.stderr}"
    assert len(list((generated / "work").rglob("result.txt"))) == 1


@pytest.mark.nextflow
@pytest.mark.serial
def test_python_and_cli_generated_artifacts_execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = supported_workflow()
    python_dir = tmp_path / "python"
    workflow.to_nextflow(python_dir)

    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    monkeypatch.chdir(cli_root)
    sophios_main._write_nextflow_target(workflow._compile().rose, "nextflow_public")
    cli_dir = cli_root / "autogenerated"

    for directory in (python_dir, cli_dir):
        result = execute_nextflow(directory)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        outputs = list((directory / "work").rglob("stdout.txt"))
        assert len(outputs) == 1
        assert outputs[0].read_text(encoding="utf-8").strip() == "hello from Sophios"


@pytest.mark.nextflow
@pytest.mark.serial
def test_four_step_checked_in_adapter_workflow_executes_end_to_end(
    tmp_path: Path,
) -> None:
    adapters = REPO_ROOT / "cwl_adapters"
    first = Step(clt_path=adapters / "echo.cwl", step_name="emit_single_1")
    first.inputs.message = FOUR_STEP_MESSAGES[0]

    second = Step(clt_path=adapters / "echo_3.cwl", step_name="emit_triplet_1")
    second.inputs.message1 = "spaces remain"
    second.inputs.message2 = "one"
    second.inputs.message3 = "argument"

    third = Step(clt_path=adapters / "echo.cwl", step_name="emit_single_2")
    third.inputs.message = FOUR_STEP_MESSAGES[2]

    fourth = Step(clt_path=adapters / "echo_3.cwl", step_name="emit_triplet_2")
    fourth.inputs.message1 = "literal"
    fourth.inputs.message2 = "&&"
    fourth.inputs.message3 = "text is not shell syntax"

    steps = [first, second, third, fourth]
    workflow = Workflow(steps, "nextflow_four_step_acceptance")
    for index, current in enumerate(steps, start=1):
        setattr(workflow.outputs, f"message_{index}", current.outputs.stdout)

    artifacts = workflow.to_nextflow(tmp_path)
    serialized = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert len(serialized["processes"]) == 4

    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = sorted(
        output.read_text(encoding="utf-8").strip()
        for output in (tmp_path / "work").rglob("stdout")
    )
    assert outputs == sorted(FOUR_STEP_MESSAGES)


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(
    ("reverse", "expected"),
    [(True, "beta\nalpha\n"), (False, "alpha\nbeta\n")],
    ids=["reversed", "forward"],
)
def test_boolean_flag_changes_observable_command_behavior(
    reverse: bool,
    expected: str,
    tmp_path: Path,
) -> None:
    """R2.1: one real compiled fixture, two flag values, two observable results."""
    write_tool = (
        CommandLineTool(
            "write_lines",
            Inputs(
                first=Input(cwl.string, position=2),
                second=Input(cwl.string, position=3),
            ),
            Outputs(result=Output(cwl.file, glob="lines.txt")),
        )
        .base_command("printf")
        .argument("%s\\n%s\\n", position=1)
        .stdout("lines.txt")
    )
    write = Step(write_tool, step_name="write_lines")
    write.inputs.first = "alpha"
    write.inputs.second = "beta"

    sort_tool = (
        CommandLineTool(
            "sort_lines",
            Inputs(
                reverse=Input(cwl.boolean, position=1, flag="-r"),
                source=Input(cwl.file, position=2),
            ),
            Outputs(result=Output(cwl.file, glob="sorted.txt")),
        )
        .base_command("sort")
        .stdout("sorted.txt")
    )
    sort_step = Step(sort_tool, step_name="sort_lines")
    sort_step.inputs.reverse = reverse
    sort_step.inputs.source = write.outputs.result

    workflow = Workflow([write, sort_step], "nextflow_boolean_flag")
    workflow.outputs.sorted = sort_step.outputs.result

    workflow.to_nextflow(tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "work").rglob("sorted.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [expected]


@pytest.mark.nextflow
@pytest.mark.serial
def test_basename_derived_output_name_executes(tmp_path: Path) -> None:
    """R2.2: an output file name derived from a staged input's basename."""
    write_tool = (
        CommandLineTool(
            "write_source",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="lines.txt")),
        )
        .base_command("echo")
        .stdout("lines.txt")
    )
    write = Step(write_tool, step_name="write_source")
    write.inputs.message = "basename derived"

    copy_tool = (
        CommandLineTool(
            "copy_named_after_source",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="$(inputs.source.basename).copy")),
        )
        .base_command("cp")
        .argument("$(inputs.source.basename).copy", position=2)
    )
    copy_step = Step(copy_tool, step_name="copy_named_after_source")
    copy_step.inputs.source = write.outputs.result

    workflow = Workflow([write, copy_step], "nextflow_basename")
    workflow.outputs.copied = copy_step.outputs.result

    workflow.to_nextflow(tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    copies = list((tmp_path / "work").rglob("lines.txt.copy"))
    assert [path.read_text(encoding="utf-8") for path in copies] == ["basename derived\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_mixed_adapter_and_builder_workflow_executes_end_to_end(tmp_path: Path) -> None:
    """One workflow mixing an imported adapter with tool_builder steps runs end to end.

    Deliberate exception to the one-idiom-per-test rule: composing both public
    construction surfaces in a single executed workflow is the claim under test.
    """
    message = "mixed construction, one backend"
    emit = Step(clt_path=REPO_ROOT / "cwl_adapters" / "echo.cwl", step_name="emit")
    emit.inputs.message = message

    copy_tool = (
        CommandLineTool(
            "copy_file",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="copy.txt")),
        )
        .base_command("cp")
        .argument("copy.txt", position=2)
    )
    duplicate = Step(copy_tool, step_name="duplicate")
    duplicate.inputs.source = emit.outputs.stdout

    count_tool = (
        CommandLineTool(
            "count_bytes",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="count.txt")),
        )
        .base_command("wc", "-c")
        .stdout("count.txt")
    )
    count = Step(count_tool, step_name="count")
    count.inputs.source = emit.outputs.stdout

    workflow = Workflow([emit, duplicate, count], "nextflow_mixed_e2e")
    workflow.outputs.copy = duplicate.outputs.result
    workflow.outputs.count = count.outputs.result

    workflow.to_nextflow(tmp_path)
    result = execute_nextflow(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    copies = list((tmp_path / "work").rglob("copy.txt"))
    assert [path.read_text(encoding="utf-8") for path in copies] == [f"{message}\n"]
    counts = list((tmp_path / "work").rglob("count.txt"))
    assert len(counts) == 1
    counted_bytes = int(counts[0].read_text(encoding="utf-8").split()[0])
    assert counted_bytes == len(message) + 1


@pytest.mark.nextflow
@pytest.mark.serial
def test_reader_writer_runtime_roundtrip(tmp_path: Path) -> None:
    original_dir = tmp_path / "original"
    regenerated_dir = tmp_path / "regenerated"
    original = run_nextflow(runtime_workflow(), original_dir)
    assert original.returncode == 0, original.stderr
    parsed = parse_nf_file(original_dir / "workflow.nf")
    regenerated = run_nextflow(promote_nextflow_document(parsed), regenerated_dir)
    assert regenerated.returncode == 0, regenerated.stderr
    original_outputs = [
        path.read_text(encoding="utf-8") for path in (original_dir / "work").rglob("copy.txt")
    ]
    regenerated_outputs = [
        path.read_text(encoding="utf-8") for path in (regenerated_dir / "work").rglob("copy.txt")
    ]
    assert original_outputs == regenerated_outputs == ["hello from Sophios"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_json_hydration_preserves_runtime_behavior(tmp_path: Path) -> None:
    workflow = runtime_workflow()
    hydrated = ExecutableNextflowWorkflow.from_json(workflow.to_json())
    result = run_nextflow(hydrated, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("copy.txt")] == [
        "hello from Sophios"
    ]


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_docker_container_behavior(tmp_path: Path) -> None:
    process = NfProcess(
        "CONTAINERIZED",
        [],
        [output_port("result", "result.txt")],
        command("touch", "result.txt"),
        container="debian:bookworm-slim",
    )
    workflow = single_process_workflow(process, params={}, output_port_name="result")
    result = run_nextflow(workflow, tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
