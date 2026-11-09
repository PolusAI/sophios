"""Source-to-runtime conformance under installed, pinned Nextflow."""

# pylint: disable=missing-function-docstring,protected-access

from collections import Counter
from functools import partial
import itertools
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

from hypothesis import example, given, strategies as st
import pytest

import sophios.main as sophios_main
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.workflow import Step, Workflow
from sophios.input_output_nf import NF_OUTPUT_MARKER, write_nextflow_artifacts
from sophios.nf_symbols import NEXTFLOW_SCRIPT_NAMES
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfCommand,
    NfPort,
    NfProcess,
    NfResources,
)
from sophios.plugins import get_tools_cwl
from sophios.utils_nf import compiled_source_to_nextflow, nextflow_names

from ..budgets import budget
from ..cases import cases, match
from ..helpers import ADAPTERS, adapter_step, python_process, search_config, write_tree
from .test_executable_ir import NAMES
from .test_public_api import supported_workflow
from .test_reference_differential import _observed, _run_cwltool, _run_nextflow, _tree
from .testkit import (
    assert_succeeded,
    command,
    execute_nextflow,
    output_port,
    run_nextflow,
    single_process_workflow,
    template,
)


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_cuda_requirement_runs_under_the_local_executor(tmp_path: Path) -> None:
    """design §6, Resources and containers: the local executor accepts the accelerator directive and runs the task.

    The CUDA fields and the Directory output ride in the ext directive, which changes nothing (design §4.2).
    """
    gpu_tool = (
        CommandLineTool("gpu_tool", Inputs(), Outputs(result=Output(cwl.directory, glob="made")))
        .base_command("mkdir", "made")
        .gpu(cuda_version_min="11.4", device_count_min=2, extra={"cudaComputeCapability": ["7.0", "8.0"]})
    )
    run = Step(gpu_tool, step_name="gpu")
    workflow = Workflow([run], "nextflow_gpu")
    workflow.outputs.result = run.outputs.result
    workflow.compile(target="nextflow").write(tmp_path)
    assert (
        "\n    accelerator 2\n"
        "    ext cudaVersionMin: '11.4', cudaComputeCapability: ['7.0', '8.0'], Directory: ['result']\n"
    ) in (tmp_path / "workflow.nf").read_text(encoding="utf-8")
    result = execute_nextflow(tmp_path)
    assert_succeeded(result)
    assert '"class":"Directory"' in result.stdout


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

    result = execute_nextflow(tmp_path, "-preview")

    assert_succeeded(result)


@pytest.mark.nextflow
@pytest.mark.serial
def test_symbol_contract_matches_actual_v2_parser(tmp_path: Path) -> None:
    accepted = [name for name, valid in NAMES if valid]
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

    for invalid in (name for name, valid in NAMES if not valid):
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


def _sophios_cli(home: Path, workflow: Path, cwl_dir: Path, *flags: str, cwd: Path | None = None,
                 timeout: int = 90) -> "subprocess.CompletedProcess[str]":
    """`python -m sophios.main` on a workflow, finding tools in `cwl_dir` and workflows in `home`."""
    config = search_config(home / "config.json", [cwl_dir], [home], renaming_conventions=[], inference_rules={})
    return python_process("-m", "sophios.main", "--yaml", workflow, "--config_file", config, "--homedir", home, *flags,
                          cwd=cwd or home, capture_output=True, timeout=timeout)


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_cli_generates_and_executes_nextflow_artifacts(tmp_path: Path) -> None:
    # The step name must match the adapter stem so the CLI can resolve
    # touch.cwl through search_paths_cwl.
    workflow_path = Workflow([adapter_step("touch", filename="result.txt")], "workflow").write_wic(tmp_path)
    result = _sophios_cli(tmp_path, workflow_path, ADAPTERS, "--target", "nextflow", "--basepath", "out")
    assert_succeeded(result)
    assert not (tmp_path / "autogenerated" / "workflow.nf").exists()
    generated = tmp_path / "out"
    assert {path.name for path in generated.iterdir()} >= {
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    }
    assert (generated / "nextflow.config").read_text(encoding="utf-8") == "docker.enabled = true\n"
    execution = execute_nextflow(generated)
    assert_succeeded(execution)
    assert len(list((generated / "work").rglob("result.txt"))) == 1


@pytest.mark.nextflow
@pytest.mark.serial
def test_python_and_cli_generated_artifacts_execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = supported_workflow()
    python_dir = workflow.compile(target="nextflow").write(tmp_path / "python")

    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    monkeypatch.chdir(cli_root)
    sophios_main._write_nextflow_target(
        compile_workflow_result(workflow),
        "nextflow_public",
        Path("autogenerated"),
        "text",
    )
    cli_dir = cli_root / "autogenerated"

    for directory in (python_dir, cli_dir):
        result = execute_nextflow(directory)
        assert_succeeded(result)
        outputs = list((directory / "work").rglob("stdout.txt"))
        assert len(outputs) == 1
        assert outputs[0].read_text(encoding="utf-8").strip() == "hello from Sophios"


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize("count", [1, 2], ids=["one-element", "many-elements"])
def test_array_of_directories_stages_every_element_for_one_process_call(count: int, tmp_path: Path) -> None:
    sources = []
    for index in range(count):
        source = tmp_path / f"source_{index}"
        source.mkdir()
        (source / "inner.txt").write_text("inner\n", encoding="utf-8")
        sources.append(str(source))
    process = NfProcess(
        "LIST_DIRECTORIES",
        [NfPort("sources", "path", path_kind="directory", is_array=True)],
        [output_port("result", "names.txt")],
        NfCommand(
            (template("echo"), NfArrayBinding("sources")),
            stdout=template("names.txt"),
        ),
    )
    workflow = single_process_workflow(
        process, params={"sources": sources}, param_types={"sources": "Directory[]"}, output_port_name="result"
    )
    result = run_nextflow(workflow, tmp_path / "run")
    assert_succeeded(result)
    outputs = list((tmp_path / "run" / "work").rglob("names.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [
        " ".join(f"gather_sources_{index + 1}/source_{index}" for index in range(count)) + "\n"
    ]


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_pattern_glob_matches_an_entry_as_a_cwl_glob_does(tmp_path: Path) -> None:
    """F69 and F84: a pattern glob, or a glob of the input's own basename, captures the entry the task made."""
    write_tool = (
        CommandLineTool(
            "write_source",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="lines.txt")),
        )
        .base_command("echo")
        .stdout("lines.txt")
    )
    write = Step(write_tool, step_name="write")
    write.inputs.message = "entry content"
    own_tool = (
        CommandLineTool(
            "own",
            Inputs(source=Input(cwl.file)),
            Outputs(
                wild=Output(cwl.file, glob="lines*"),
                same=Output(cwl.file, glob="$(inputs.source.basename)"),
            ),
        )
        .base_command("true")
        .initial_workdir(["$(inputs.source)"])
    )
    own = Step(own_tool, step_name="own")
    own.inputs.source = write.outputs.result
    renamed_tool = CommandLineTool(
        "renamed", Inputs(source=Input(cwl.file)), Outputs(wild=Output(cwl.file, glob="renamed*"))
    )
    renamed_tool = renamed_tool.base_command("true").stage(renamed_tool.inputs.source, entryname="renamed.txt")
    renamed = Step(renamed_tool, step_name="renamed")
    renamed.inputs.source = write.outputs.result
    workflow = Workflow([write, own, renamed], "nextflow_entry_globs")
    workflow.outputs.wild = own.outputs.wild
    workflow.outputs.same = own.outputs.same
    workflow.outputs.renamed = renamed.outputs.wild
    result = execute_nextflow(workflow.compile(target="nextflow").write(tmp_path))
    assert_succeeded(result)
    # Nextflow stages each entry's input as a symlink in the hidden directory; the task links the entry beside it.
    entries = [
        path for path in (tmp_path / "work").rglob("*")
        if path.is_symlink() and not path.parent.name.startswith(".sophios_entry_")
    ]
    assert sorted(path.name for path in entries) == ["lines.txt", "renamed.txt"]
    assert {path.read_text(encoding="utf-8") for path in entries} == {"entry content\n"}


@pytest.mark.nextflow
@pytest.mark.serial
def test_mixed_adapter_and_builder_workflow_executes_end_to_end(tmp_path: Path) -> None:
    """One workflow mixing an imported adapter with tool_builder steps runs end to end.

    Deliberate exception to the one-idiom-per-test rule: composing both public
    construction surfaces in a single executed workflow is the claim under test.
    """
    message = "mixed construction, one backend"
    emit = adapter_step("echo", "emit", message=message)

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

    result = execute_nextflow(workflow.compile(target="nextflow").write(tmp_path))
    assert_succeeded(result)
    copies = list((tmp_path / "work").rglob("copy.txt"))
    assert [path.read_text(encoding="utf-8") for path in copies] == [f"{message}\n"]
    counts = list((tmp_path / "work").rglob("count.txt"))
    assert len(counts) == 1
    counted_bytes = int(counts[0].read_text(encoding="utf-8").split()[0])
    assert counted_bytes == len(message) + 1


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
    workflow = single_process_workflow(process, params={}, param_types={}, output_port_name="result")
    result = run_nextflow(workflow, tmp_path)
    assert_succeeded(result)


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_a_containerized_command_runs_after_its_images_entrypoint(tmp_path: Path) -> None:
    """CWL gives the command to the container as its arguments, so the image's ENTRYPOINT runs first."""
    image = "sophios-nextflow-entrypoint:test"
    context = tmp_path / "image"
    context.mkdir()
    (context / "Dockerfile").write_text(
        'FROM debian:bookworm-slim\nENTRYPOINT ["/bin/echo", "from the entrypoint"]\n', encoding="utf-8"
    )
    built = subprocess.run(["docker", "build", "-q", "-t", image, str(context)], capture_output=True, text=True,
                           timeout=300, check=False)
    assert built.returncode == 0, built.stderr

    def words(name: str, *base_command: str) -> CommandLineTool:
        tool_ = CommandLineTool(name, Inputs(), Outputs(out=Output(cwl.file, glob="out.txt"))).stdout("out.txt")
        return (tool_.base_command(*base_command) if base_command else tool_).argument("an argument", position=1)

    steps = [
        Step(words("no_base_command").docker(image), step_name="no_base_command"),
        Step(words("base_command", "printf").docker(image), step_name="base_command"),
        Step(words("no_entrypoint", "echo").docker("debian:bookworm-slim"), step_name="no_entrypoint"),
    ]
    workflow = Workflow(steps, "nextflow_entrypoint")
    for step in steps:
        setattr(workflow.outputs, step.process_name, step.outputs.out)
    result = execute_nextflow(workflow.compile(target="nextflow").write(tmp_path / "pipeline"))
    assert_succeeded(result)
    assert sorted(path.read_text(encoding="utf-8") for path in (tmp_path / "pipeline" / "work").rglob("out.txt")) == [
        "an argument\n", "from the entrypoint an argument\n", "from the entrypoint printf an argument\n",
    ]


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_host_and_container_tasks_run_in_one_workflow(tmp_path: Path) -> None:
    """A tool without a DockerRequirement runs on the host beside one that runs in a container."""
    def where(name: str) -> CommandLineTool:
        return (
            CommandLineTool(name, Inputs(), Outputs(place=Output(cwl.file, glob="place.txt")))
            .base_command("sh", "-c", "test -e /.dockerenv && echo container || echo host")
            .stdout("place.txt")
        )

    in_container = Step(where("in_container").docker("debian:bookworm-slim"), step_name="in_container")
    on_host = Step(where("on_host"), step_name="on_host")
    workflow = Workflow([in_container, on_host], "nextflow_host_and_container")
    workflow.outputs.container_place = in_container.outputs.place
    workflow.outputs.host_place = on_host.outputs.place

    workflow.compile(target="nextflow").write(tmp_path)
    assert (tmp_path / "nextflow.config").read_text(encoding="utf-8") == "docker.enabled = true\n"
    result = execute_nextflow(tmp_path)
    assert_succeeded(result)
    places = {
        task.parent.joinpath(".command.run").read_text(encoding="utf-8").count("docker run") > 0:
        task.read_text(encoding="utf-8")
        for task in (tmp_path / "work").rglob("place.txt")
    }
    assert places == {True: "container\n", False: "host\n"}


def _recorded(stdout: str) -> dict[str, Any]:
    """Each workflow output the pipeline recorded on ``stdout``, by its Nextflow name."""
    marker = f"{NF_OUTPUT_MARKER} "
    return dict(json.loads(line.removeprefix(marker)) for line in stdout.splitlines() if line.startswith(marker))


@pytest.mark.nextflow
@pytest.mark.serial
def test_the_pipeline_records_each_workflow_output_as_its_value(tmp_path: Path) -> None:
    """A path is its class and path, a skipped step's output null, and a nested crossproduct's output
    one array level per scattered input, each file converted at the innermost level."""
    touch = Step(CommandLineTool("touch_file", Inputs(), Outputs(out=Output(cwl.file, glob="out.txt")))
                 .base_command("touch", "out.txt"), step_name="touch")
    folder = Step(CommandLineTool("make_folder", Inputs(), Outputs(out=Output(cwl.directory, glob="d")))
                  .base_command("mkdir", "d"), step_name="folder")
    skipped = Step(CommandLineTool("skipped", Inputs(n=Input(cwl.int)), Outputs(out=Output(cwl.file, glob="n.txt")))
                   .base_command("touch", "n.txt"), step_name="skipped")
    skipped.inputs.n = 1
    skipped.when = "$(inputs.n > 1)"
    grid = Step(CommandLineTool("cell", Inputs(a=Input(cwl.string), b=Input(cwl.string)),
                                Outputs(out=Output(cwl.file, glob="cell.txt"))).base_command("touch", "cell.txt"),
                step_name="grid")
    grid.inputs.a, grid.inputs.b = ["a", "b"], ["x", "y", "z"]
    grid.scatter_on(grid.inputs.a, grid.inputs.b, method="nested_crossproduct")
    workflow = Workflow([touch, folder, skipped, grid], "recorded")
    workflow.outputs.file, workflow.outputs.folder = touch.outputs.out, folder.outputs.out
    workflow.outputs.skipped, workflow.outputs.grid = skipped.outputs.out, grid.outputs.out

    result = execute_nextflow(workflow.compile(target="nextflow").write(tmp_path))

    assert_succeeded(result)
    recorded = _recorded(result.stdout)
    assert recorded["skipped"] is None
    assert recorded["file"] == {"class": "File", "path": recorded["file"]["path"]}
    assert Path(recorded["file"]["path"]).is_file() and Path(recorded["file"]["path"]).name == "out.txt"
    assert recorded["folder"]["class"] == "Directory" and Path(recorded["folder"]["path"]).is_dir()
    assert [[cell["class"] for cell in row] for row in recorded["grid"]] == [["File"] * 3] * 2
    assert len({cell["path"] for row in recorded["grid"] for cell in row}) == 6


@pytest.mark.nextflow
@pytest.mark.serial
def test_two_workflows_started_from_one_base_path_each_run_their_own_pipeline(tmp_path: Path) -> None:
    """Each run's pipeline, work directory and Nextflow log are in `nextflow_<workflow>/` under the base path, so
    a run started before the other has read its pipeline runs its own, and its outputs are CWL objects naming the
    files it made."""
    base = tmp_path / "base"
    other = supported_workflow()
    other.process_name = "other"
    other.steps[0].inputs.message = "hello from the other"
    handles = [workflow.start(target="nextflow", basepath=str(base)) for workflow in (supported_workflow(), other)]
    for handle, (name, text) in zip(handles, [("nextflow_public", "hello from Sophios"),
                                              ("other", "hello from the other")]):
        result = handle.wait()
        assert result.exit_code == 0 and result.outputs is not None
        file = result.outputs["file"]
        assert (file["class"], file["basename"]) == ("File", "stdout.txt")
        assert Path(file["path"]).read_text(encoding="utf-8") == f"{text}\n"
        assert Path(file["path"]).is_relative_to(base.resolve() / f"nextflow_{name}" / "work")
        assert json.loads((base / f"output_{name}.json").read_text(encoding="utf-8")) == result.outputs
        assert (base / f"nextflow_{name}" / ".nextflow.log").is_file()


def _seen(value: Any) -> Any:
    """A recorded output as test_reference_differential observes one, null as it is, a path also as whether it is a
    symlink (`link`)."""
    if value is None:
        return None
    if isinstance(value, list):
        return [_seen(item) for item in value]
    return {**_observed(value), "link": Path(value["path"]).is_symlink()}


def _case_files(case: dict[str, Any]) -> dict[str, str]:
    """A case's files: each tool a CommandLineTool but for what it leaves out, a workflow of one step per tool unless
    it gives its `steps`, each child workflow, and its other files."""
    tools = case.get("tools", {})
    return {
        **{f"{stem}.cwl": json.dumps({"cwlVersion": "v1.2", "class": "CommandLineTool", "inputs": {}, "outputs": {},
                                      **tool}) for stem, tool in tools.items()},
        "workflow.wic": "steps:\n" + (case.get("steps") or "".join(f"- id: {stem}\n" for stem in tools)),
        **{f"{stem}.wic": text for stem, text in case.get("children", {}).items()},
        **case.get("files", {}),
    }


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize("case", cases("nextflow/runs.yaml", "tools", "steps", "children", "files", "outputs",
                                       "fails", "rendered"))
def test_a_workflow_runs_as_its_case_says(
        case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A case's workflow, its tools written beside it or among cwl_adapters, runs to each of its `outputs`, a workflow
    output by its Sophios name, observed as its class, basename and contents, or `fails` showing the text given, no
    output unlisted, and a `rendered` match of the script written; the files beside it are left as they were
    (tests/core/cases.py reads each form); an output's `link` is whether it is a symlink."""
    write_tree(tmp_path, _case_files(case))
    monkeypatch.chdir(tmp_path)
    inputs = {path.name: _tree(path) for path in tmp_path.iterdir() if path.suffix not in (".wic", ".cwl")}
    tools = get_tools_cwl({"search_paths_cwl": {"global": [str(tmp_path), str(ADAPTERS)]}})
    children = {"global": {path.stem: path for path in tmp_path.glob("*.wic") if path.name != "workflow.wic"}}
    compiled = compile_workflow_result(Workflow.from_wic(tmp_path / "workflow.wic", tool_registry=tools,
                                                         workflow_paths=children), tool_registry=tools)
    write_nextflow_artifacts(compiled_source_to_nextflow(compiled), tmp_path)
    if "rendered" in case:
        assert match(case["rendered"], (tmp_path / "workflow.nf").read_text(encoding="utf-8"))
    result = execute_nextflow(tmp_path)
    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    if "fails" in case:
        assert result.returncode != 0 and (case["fails"] is True or case["fails"] in output), output
    else:
        assert result.returncode == 0, output
        names = nextflow_names(compiled)["outputs"]
        observed = {names[key]: _seen(value) for key, value in _recorded(result.stdout).items()}
        assert match(case["outputs"], observed) and observed.keys() == case["outputs"].keys()
    assert {path.name: _tree(path) for path in tmp_path.iterdir() if path.name in inputs} == inputs


_TEXT = st.text(st.characters(codec="utf-8", exclude_characters="\x00"))
_LITERAL = _TEXT.filter(lambda text: "$(" not in text and "${" not in text)
#: design §6, Float text: both runners print a float alike in this region; cwltool prints 1e-07 as 0.
_VALUES = {"string": _TEXT, "int": st.integers(-2**31, 2**31 - 1), "boolean": st.booleans(),
           "float": st.just(0.0) | st.floats(1e-4, 1e16, exclude_max=True).flatmap(lambda x: st.sampled_from([x, -x]))}
#: Names a script resolves to something else unless renamed, names every script declares, and plain ones.
_NAMES = [*sorted(NEXTFLOW_SCRIPT_NAMES), "env", "file", "main", "params", "Math", "String", "Map", "plain"]


@st.composite
def _argv_tools(draw: st.DrawFn) -> dict[str, Any]:
    """A tool inside the supported subset: inputs `(name, type, value, position or None when unbound, prefix, optional,
    separate, valueFrom of itself)`, literal arguments, exported variables, a `lead` flag that may render nothing."""
    names = draw(st.lists(st.sampled_from(_NAMES), unique=True, max_size=4))
    literals = draw(st.lists(_LITERAL, max_size=2))
    positions = iter(draw(st.permutations(range(5, 5 + len(names) + len(literals)))))
    inputs = []
    for name in names:
        kind = draw(st.sampled_from([*_VALUES, "string[]", "int[]"]))
        optional = not kind.endswith("[]") and draw(st.booleans())
        value = draw(st.lists(_VALUES[kind[:-2]]) if kind.endswith("[]") else
                     st.none() | _VALUES[kind] if optional else _VALUES[kind])
        bound = kind == "boolean" or draw(st.booleans())
        prefix = draw(st.sampled_from(["-r", "--name", "--ratio=", "-x"])) if bound and (
            kind == "boolean" or draw(st.booleans())) else None
        inputs.append((name, kind, value, next(positions) if bound else None, prefix, optional,
                       not (prefix and kind in ("string", "int", "float") and draw(st.booleans())),
                       kind == "boolean" and draw(st.booleans())))
    strings = [f"$(inputs.{name})" for name, kind, *_, optional, _, _ in inputs if kind == "string" and not optional]
    env = draw(st.dictionaries(st.sampled_from(["GREETING", "LITERAL"]), _LITERAL | st.sampled_from(strings or [""])))
    return {"inputs": inputs, "arguments": [(next(positions), text) for text in literals], "env": env,
            "lead": draw(st.none() | st.booleans()), "shell": draw(st.booleans()),
            "names": (draw(st.sampled_from(["argv", "echo", "file", "Math"])),
                      draw(st.sampled_from(["argv", "echo", "wf", "main"])))}


def _argv_workflow(spec: dict[str, Any]) -> Workflow:
    """``spec``'s tool, whose program prints each exported variable and then each argument it is given."""
    inputs, values = dict[str, Any](), dict[str, Any]()
    for name, kind, value, position, prefix, optional, separate, itself in spec["inputs"]:
        binding = {} if position is None else {"position": position, "flag": prefix, "separate": None if separate else
                                               False, "value_from": f"$(inputs.{name})" if itself else None}
        inputs[name] = Input(cwl.array(kind[:-2]) if kind.endswith("[]") else kind, required=not optional, **binding)
        values |= {} if value is None else {name: value}
    if spec["lead"] is not None:
        inputs["lead"], values["lead"] = Input(cwl.boolean, position=0, flag="env"), spec["lead"]
    script = "".join(f'printf "%s\\0" "${name}"; ' for name in spec["env"]) + 'for a; do printf "%s\\0" "$a"; done'
    tool = CommandLineTool("argv", Inputs(**inputs), Outputs(out=Output(cwl.file, glob="out.txt"))).stdout("out.txt")
    for position, word in [*enumerate(("sh", "-c", script, "sh"), start=1), *spec["arguments"]]:
        tool = tool.argument(word, position=position)
    for name, value in spec["env"].items():
        tool = tool.env_var(name, value)
    step = Step(tool.shell_command() if spec["shell"] else tool, step_name=spec["names"][0])
    for name, value in values.items():
        setattr(step.inputs, name, value)
    workflow = Workflow([step], spec["names"][1])
    workflow.outputs.out = step.outputs.out
    return workflow


#: A value that would run as shell syntax unquoted; its second line starts at column 0.
_INJECTION = ("injected", "string", "price is $5; touch SHOULD_NOT_EXIST ' \" $(uname)\nsecond line", 12, None, False,
              True, False)
#: Each name a script resolves to something else unless renamed, holding a value it must print as it is. Indented
#: lines once lost their indentation, and `\r` became `\n`: Nextflow strips the indentation a script's lines share,
#: which a value's line at column 0, as in _INJECTION, hides.
_SCRIPT_NAMES = [(name, "string", value, 5 + index, None, False, True, False) for index, (name, value) in enumerate(
    zip(["task", "log", "channel", "Math", "String", "Map", "plain"],
        ["a\n  b", "x\r\tb", "it costs $HOME", "two words", "", "-n", "plain"]))]


#: A flag either way and as its own valueFrom, an absent optional flag, optional values absent and present, a filled
#: and an empty array, an unbound absent optional, a literal holding `$`, and variables of a literal and an input's.
_EVERY_BINDING = {
    "lead": False, "shell": False, "names": ("argv", "argv"),
    "arguments": [(40, "printf '%s|' \"$1\" '$5' $ > out.txt")],
    "env": {"GREETING": "$(inputs.greeting)", "LITERAL": "it's a $5 \"deal\" \\n"},
    "inputs": [
        *_SCRIPT_NAMES, _INJECTION,
        ("greeting", "string", "x'; touch SHOULD_NOT_EXIST $(uname) \" \\ `id`\nsecond line", None, None, False, True,
         False),
        *((f"r{index}", "boolean", value, 20 + index, "-r", False, True, itself)
          for index, (value, itself) in enumerate([(True, False), (False, False), (True, True), (False, True)])),
        ("maybe", "boolean", None, 24, "-r", True, True, False),
        ("note", "string", None, None, None, True, True, False),
        *((f"{name}_{index}", kind, value if index else None, 25 + 4 * index + offset, prefix, True, separate, False)
          for index, values in enumerate([(), ("two words", 3, 0.5, "")])
          for offset, (name, kind, prefix, separate, value) in enumerate(zip(
              ["name", "count", "ratio", "plain"], ["string", "int", "float", "string"],
              ["--name", "--count", "--ratio=", None], [True, True, False, True], values or [None] * 4))),
        ("names", "string[]", ["alice", "bob"], 33, "--name", False, True, False),
        ("none", "string[]", [], 34, "--name", False, True, False)]}


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
@budget(3)
@given(_argv_tools())
@example(_EVERY_BINDING)
@example({"lead": True, "shell": True, "names": ("echo", "echo"), "env": {}, "arguments": [],
          "inputs": [*_SCRIPT_NAMES, _INJECTION]})
@example({"lead": False, "shell": False, "names": ("file", "wf"), "env": {}, "arguments": [], "inputs": _SCRIPT_NAMES})
@example({"lead": None, "shell": True, "names": ("Math", "main"), "env": {}, "arguments": [], "inputs": _SCRIPT_NAMES})
def test_a_command_receives_the_arguments_cwltool_gives_it(
        tmp_path_factory: pytest.TempPathFactory, spec: dict[str, Any]) -> None:
    """A tool's values, flags, literals and variables reach its command byte for byte as under cwltool, and none runs
    as shell syntax."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    root = tmp_path_factory.mktemp("argv")
    printed = {runner: Path(run(_argv_workflow(spec), root / runner)["out"]["path"]).read_bytes()
               for runner, run in (("nextflow", _run_nextflow), ("cwltool", partial(_run_cwltool, cwltool.main.main)))}
    assert printed["nextflow"] == printed["cwltool"]
    assert not list(root.rglob("SHOULD_NOT_EXIST"))


_WORD = st.text(st.characters(codec="utf-8", exclude_characters="\x00"), max_size=12)
_METHODS = ["dotproduct", "flat_crossproduct", "nested_crossproduct"]
#: A step: its method; its scattered inputs, each `(kind, words)` or `("File", j)` for step j's gathered Files; an
#: unscattered string, File or File array of words or of step j's, or an int its tool writes as `$(inputs.shared)0`;
#: whether its File output declares `$(self[0])`; and whether its first input is the first step's first.
_Step = tuple[str, list[tuple[str, Any]], tuple[str, Any] | None, bool, bool]


def _printed(spec: _Step, printed: list[Any]) -> Any:
    """What CWL v1.2 (WorkflowStep, scatterMethod) gives a step whose task prints its arguments joined by `-`: one task
    per index (None if unequal), every combination, or nested one level per input; the rest reach each whole."""
    method, inputs, broadcast, *_ = spec

    def words(source: Any) -> Any:
        return printed[source] if isinstance(source, int) else source
    kind, value = broadcast or ("", "")
    tail = [] if not kind else [
        "".join(words(value)) if kind == "File[]" else f"{value}0" if kind == "int" else value]
    lists = [words(source) for _, source in inputs]

    def nested(combination: list[str], rest: list[list[str]]) -> Any:
        return "-".join([*combination, *tail]) if not rest else [nested([*combination, word], rest[1:])
                                                                 for word in rest[0]]
    if not lists or method == "nested_crossproduct":
        return nested([], lists)
    if method == "dotproduct":
        return [nested(list(row), []) for row in zip(*lists)] if len(set(map(len, lists))) == 1 else None
    return [nested(list(combination), []) for combination in itertools.product(*lists)]


@st.composite
def _scatters(draw: st.DrawFn) -> list[_Step]:
    """1..5 steps; one may read an earlier scattering, non-nesting step's gathered Files; dotproduct inputs are equally
    long unless the draw says otherwise, so later steps run; S3's mismatch stays an @example."""
    steps: list[_Step] = []
    printed: list[Any] = []
    for _ in range(draw(st.integers(1, 5))):
        method = draw(st.sampled_from(_METHODS))
        gathered = [index for index, (step, values) in enumerate(zip(steps, printed))
                    if step[1] and step[0] != "nested_crossproduct" and values is not None]
        shares = bool(steps and steps[0][1]) and draw(st.booleans())
        first = [steps[0][1][0]] if shares else []
        if draw(st.booleans()) and gathered:
            first.append(("File", draw(st.sampled_from(gathered))))
        lengths = [len(source if isinstance(source, list) else printed[source]) for _, source in first]
        size = (lengths[0] if lengths else draw(st.integers(0, 3))) if method == "dotproduct" and draw(
            st.integers(0, 7)) else None
        inputs = first + [(draw(st.sampled_from(["string", "File"])), draw(st.lists(
            _WORD, min_size=size or 0, max_size=3 if size is None else size))) for _ in range(draw(st.integers(
                0, 3 - len(first))))]
        broadcast = draw(st.none() | st.tuples(st.sampled_from(["string", "File"]), _WORD) | st.tuples(
            st.just("int"), st.integers(-2**31, 2**31 - 1)) | st.tuples(st.just("File[]"), st.lists(
                _WORD, max_size=2) | (st.sampled_from(gathered) if gathered else st.nothing())))
        steps.append((method, inputs, broadcast, draw(st.booleans()), shares))
        printed.append(_printed(steps[-1], printed))
    return steps


def _scatter_workflow(steps: list[_Step], root: Path) -> tuple[Workflow, list[list[str]]]:
    """`steps` as a workflow, each File written under `root`, and each step's scattered sources by their names."""
    built, sources = [], []

    def staged(name: str, words: list[str]) -> list[str]:
        """Each word in a file of its own: two scalar inputs of one basename collide in a Nextflow task."""
        paths = [root / f"{name}{index}.txt" for index in range(len(words))]
        for path, word in zip(paths, words):
            path.write_text(word, encoding="utf-8")
        return list(map(str, paths))

    for index, (_method, inputs, broadcast, single, shares) in enumerate(steps):
        reads = [f'cat -- "${n}"' if kind == "File" else f'printf %s "${n}"' for n, (kind, _) in enumerate(inputs, 1)]
        ports = {f"in{n}": Input(cwl.file if kind == "File" else cwl.string, position=n + 1)
                 for n, (kind, _) in enumerate(inputs)}
        if broadcast:
            reads.append({"File": f'cat -- "${len(inputs) + 1}"', "File[]": f'shift {len(inputs)}; for f; do cat -- '
                          '"$f"; done'}.get(broadcast[0], f'printf %s "${len(inputs) + 1}"'))
            ports["shared"] = Input({"string": cwl.string, "int": cwl.int, "File": cwl.file, "File[]": cwl.array(
                cwl.file)}[broadcast[0]], position=None if broadcast[0] == "int" else len(inputs) + 1)
        # Each task sleeps up to 0.3 s by what it prints, so the tasks of a step finish out of order. A step's file is
        # named after it: a task writing the name of a staged input would write through it.
        script = 'sleep "0.$(( $(printf %s "$*" | cksum | cut -d" " -f1) % 4 ))"; ' + "; printf -; ".join(reads)
        tool = CommandLineTool(f"each{index}", Inputs(**ports), Outputs(
            out=Output(cwl.file, glob=f"s{index}.txt", output_eval="$(self[0])" if single else None),
            text=Output(cwl.string, glob=f"s{index}.txt", load_contents=True, output_eval="$(self[0].contents)")))
        if broadcast and broadcast[0] == "int":
            tool.argument("$(inputs.shared)0", position=len(inputs) + 1)
        built.append(Step(tool.base_command("sh", "-c", script, f"s{index}").stdout(f"s{index}.txt"),
                          step_name=f"s{index}"))
        sources.append([f"s{source}.out" if isinstance(source, int) else "s0_0" if shares and n == 0 else
                        f"s{index}_{n}" for n, (_, source) in enumerate(inputs)])
    flow = Workflow(built, "scatter")
    for index, (step, (method, inputs, broadcast, *_)) in enumerate(zip(built, steps)):
        for n, ((kind, source), name) in enumerate(zip(inputs, sources[index])):
            if isinstance(source, int):
                setattr(step.inputs, f"in{n}", built[source].outputs.out)
                continue
            setattr(step.inputs, f"in{n}", getattr(flow.inputs, name).as_type(cwl.array(
                cwl.file if kind == "File" else cwl.string)))
            setattr(flow.inputs, name, staged(name + "_", source) if kind == "File" else source)
        if inputs:
            step.scatter_on(*(getattr(step.inputs, f"in{n}") for n in range(len(inputs))), method=method)
        if broadcast and broadcast[0] == "File[]":
            step.inputs.shared = built[broadcast[1]].outputs.out if isinstance(broadcast[1], int) else staged(
                f"{index}_shared", broadcast[1])
        elif broadcast:  # A metacharacter names a file, not a pattern.
            step.inputs.shared = staged(f"{index}_reference[1]*", [broadcast[1]])[0] if broadcast[0] == "File" else (
                broadcast[1])
        setattr(flow.outputs, f"s{index}_out", step.outputs.out)
        setattr(flow.outputs, f"s{index}_text", step.outputs.text)
    return flow, sources


def _s(*words: str) -> tuple[str, list[str]]:
    return ("string", list(words))


def _f(*words: str) -> tuple[str, list[str]]:
    return ("File", list(words))


_PAIRS = ("dotproduct", [_s("a", "b", "c"), _s("x", "y", "z")], ("int", 2), False, False)


@pytest.mark.nextflow
@pytest.mark.serial
@budget(3)
@given(_scatters())
# S1, S2, S9 and S10 (design §6, Topology): two and three inputs pair by index; a scalar or a File reaches every task.
@example([("dotproduct", [_s("a", "b", "c"), _s("2", "1", "0")], None, False, False),
          ("dotproduct", [_s("a", "b", "c"), _s("x", "y", "z"), _s("0", "0", "0")], None, False, False),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], ("string", "0"), False, False),
          ("dotproduct", [_f("firstA\n", "firstB\n"), _f("secondA\n", "secondB\n")], ("File", "shared\n"), False,
           False)])
# S5, S6, S7, S8 and S11: the first input outermost, flat or one level per input; every output per invocation.
@example([("flat_crossproduct", [_s("a", "b"), _s("1", "0")], None, False, False),
          ("flat_crossproduct", [_s("a", "b"), _s("x", "y", "z"), _s("0")], None, False, False),
          ("nested_crossproduct", [_s("a", "b"), _s("1", "0", "0")], None, False, False),
          ("nested_crossproduct", [_s("a", "b"), _s("x", "y"), _s("0")], None, False, False)])
# S4, S6 and S8 empty: no task, an empty dimension keeps its outer shape; an empty unscattered File array.
@example([("dotproduct", [_s(), _s()], None, False, False),
          ("flat_crossproduct", [_s("a"), _s(), _s("0")], None, False, False),
          ("nested_crossproduct", [_s("a", "b"), _s(), _s("0")], None, False, False),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], ("File[]", []), False, False)])
# One parameter scattered at two steps, `$(self[0])` declared; one unscattered File is a list of one, not a bare Path.
@example([("dotproduct", [_s("alpha", "beta", "gamma")], ("string", "shared"), True, False),
          ("dotproduct", [_s("alpha", "beta", "gamma")], ("string", "shared"), True, True),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], ("File[]", ["shared\n"]), False, False),
          ("dotproduct", [_s("alpha", "beta")], ("File", "shared file\n"), False, False)])
# An empty scatter beside an unscattered step ends; File elements and a File broadcast (S10 of one input).
@example([("dotproduct", [_s()], None, False, False), ("dotproduct", [], None, False, False),
          ("dotproduct", [_f("alpha\n", "beta\n")], ("File", "shared file\n"), False, False),
          ("dotproduct", [_f("a\n", "b\n")], ("File", "shared\n"), False, False)])
# S3: unequal lengths fail the run naming each input and its length; no task runs truncated.
@example([("dotproduct", [_s("a", "b"), _s("x")], None, False, False),
          ("dotproduct", [_s("a", "b"), _s("0")], None, False, False)])
# S12, S13, S14: three gathered Files scattered again beside Files, gathered into one step and, apart, into another,
# and staged whole in every task of a scattered step.
@example([_PAIRS, ("dotproduct", [("File", 0), _f("1\n", "2\n", "3\n")], None, False, False),
          ("dotproduct", [], ("File[]", 1), False, False), ("dotproduct", [], ("File[]", 0), False, False),
          ("dotproduct", [_s("p", "q"), _s("r", "s")], ("File[]", 0), False, False)])
# A gather of one file reaches a step, scattered or not, as a list.
@example([("dotproduct", [_s("a"), _s("x")], ("int", 2), False, False),
          ("dotproduct", [("File", 0), _f("1\n")], None, False, False), ("dotproduct", [], ("File[]", 1), False, False),
          ("dotproduct", [_s("p", "q"), _s("r", "s")], ("File[]", 0), False, False),
          ("flat_crossproduct", [_s("p", "q"), _s("r", "s")], ("File[]", 0), False, False)])
# Three gathered Files in every task of a flat_crossproduct; a nested step over a gathered array and an input array.
@example([_PAIRS, ("flat_crossproduct", [_s("p", "q"), _s("r", "s")], ("File[]", 0), False, False),
          ("nested_crossproduct", [("File", 0), _f("x", "y", "z")], None, False, False)])
def test_a_scatter_records_what_cwl_defines_for_its_method(
        tmp_path_factory: pytest.TempPathFactory, steps: list[_Step]) -> None:
    """design §6, Topology: each step's outputs, File and captured text, are recorded as `_printed` gives them, in
    invocation order whatever order its tasks finish in, from one task per value."""
    root = tmp_path_factory.mktemp("scatter")
    flow, sources = _scatter_workflow(steps, root)
    printed: list[Any] = []
    for spec in steps:
        printed.append(_printed(spec, printed))
    result = execute_nextflow(flow.compile(target="nextflow").write(root / "run"))
    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # Each task's command is `sh -c <script> <step name> ...`.
    tasks = Counter(shlex.split(path.read_text(encoding="utf-8").split("\n", 1)[1])[3]
                    for path in (root / "run" / "work").rglob(".command.sh"))
    if None in printed:
        mismatched = [index for index, values in enumerate(printed) if values is None]
        assert result.returncode != 0 and any(f"s{i}: dotproduct scatter inputs have mismatched lengths: " + ", ".join(
            f"{name}={len(printed[source] if isinstance(source, int) else source)}"
            for name, (_, source) in zip(sources[i], steps[i][1])) in output for i in mismatched), output
        assert not any(tasks[f"s{index}"] for index in mismatched), tasks
        return
    assert result.returncode == 0, output

    def contents(value: Any) -> Any:
        return [contents(item) for item in value] if isinstance(value, list) else Path(
            value["path"]).read_bytes().decode()

    def count(values: Any) -> int:
        return 1 if isinstance(values, str) else sum(map(count, values))
    assert tasks == Counter({f"s{index}": count(values) for index, values in enumerate(printed)}), tasks
    recorded = _recorded(result.stdout)
    assert [(recorded[f"s{index}_text"], contents(recorded[f"s{index}_out"])) for index in range(len(printed))] == [
        (values, values) for values in printed]


@st.composite
def _staged_paths(draw: st.DrawFn) -> tuple[bool, bool, list[tuple[str, str]], bool]:
    """Whether the input is a Directory, whether an array, each element's name and text, and whether the parameter
    is a bare path rather than the object the compiler writes."""
    array, element = draw(st.booleans()), st.tuples(st.sampled_from(
        ["source[0]*.txt", "a b?.txt", "plain.txt", "source_directory[1]"]), _TEXT)
    return (draw(st.booleans()), array, draw(st.lists(element, min_size=not array, max_size=4 if array else 1)),
            draw(st.booleans()))


@pytest.mark.nextflow
@pytest.mark.serial
@budget(2)
@given(_staged_paths())
@example((False, True, [("source[0]*.txt", "alpha\n"), ("source[0]*.txt", "beta\n")], True))
@example((False, True, [], True))
@example((False, False, [("source.txt", "runtime file object\n")], False))
@example((True, False, [("source_directory[1]", "value with spaces")], True))
@example((False, False, [("plain.txt", "\r")], False))
def test_a_staged_path_reaches_its_task_whole_and_in_order(
        tmp_path_factory: pytest.TempPathFactory, staged: tuple[bool, bool, list[tuple[str, str]], bool]) -> None:
    """design §6, Inputs and channels: each File, or Directory holding inner.txt, is staged for one task, in order,
    whether its parameter is the object the compiler writes or a bare path a hand-edited params file holds; same
    basenames in two directories stay apart, a metacharacter names a file, and an empty array adds nothing."""
    directory, array, elements, bare = staged
    root = tmp_path_factory.mktemp("staged")
    paths = [root / str(index) / name for index, (name, _) in enumerate(elements)]
    for path, (_, text) in zip(paths, elements):
        (path if directory else path.parent).mkdir(parents=True)
        (path / "inner.txt" if directory else path).write_bytes(text.encode())
    kind = cwl.directory if directory else cwl.file
    read = 'for p; do cat -- "$p/inner.txt"; done' if directory else 'for p; do cat -- "$p"; done'
    tool = CommandLineTool("read", Inputs(sources=Input(cwl.array(kind) if array else kind, position=1)),
                           Outputs(out=Output(cwl.file, glob="out.txt"))).base_command("sh", "-c", read, "sh")
    step = Step(tool.stdout("out.txt"), step_name="read")
    step.inputs.sources = [str(path) for path in paths] if array else str(paths[0])
    flow = Workflow([step], "staged")
    flow.outputs.out = step.outputs.out
    run = flow.compile(target="nextflow").write(root / "run")
    if bare:
        params = json.loads((run / "nextflow_params.json").read_text(encoding="utf-8"))
        (run / "nextflow_params.json").write_text(json.dumps({name: [item["path"] for item in value] if isinstance(
            value, list) else value["path"] for name, value in params.items()}), encoding="utf-8")
    result = execute_nextflow(run)
    assert_succeeded(result)
    assert Path(_recorded(result.stdout)["out"]["path"]).read_bytes().decode() == "".join(
        text for _, text in elements)


def _near_the_limit(size: int) -> Any:
    """Text of exactly ``size`` UTF-8 bytes, ending in 2- and 3-byte characters."""
    return st.lists(st.sampled_from(["é", "✓"]), max_size=3).map(
        lambda tail: ("a" * (size - len("".join(tail).encode())) + "".join(tail)).encode())


@pytest.mark.nextflow
@pytest.mark.serial
@budget(3)
@given(st.one_of(st.text(st.characters(codec="utf-8") | st.sampled_from(["\n", "é", "✓"])).map(str.encode),
                 st.binary(), st.integers(65530, 65540).flatmap(_near_the_limit)))
@example(b"line\n\n")
@example(b"a" * 65536)
@example(b"a" * 65537)
@example("café ✓\n".encode())
@example(b"ok\xff\xfe")
def test_a_captured_text_is_the_files_utf8_up_to_the_cwl_limit(
        tmp_path_factory: pytest.TempPathFactory, contents: bytes) -> None:
    """CWL v1.2 loadContents: the file's text, trailing newlines kept, when it is at most 65536 bytes of strict UTF-8;
    the run fails naming the limit and the size above it, and naming UTF-8 for bytes that are not."""
    root = tmp_path_factory.mktemp("capture")
    (root / "source.bin").write_bytes(contents)
    tool = CommandLineTool("capture_text", Inputs(source=Input(cwl.file, position=1)), Outputs(text=Output(
        cwl.string, glob="out.txt", load_contents=True, output_eval="$(self[0].contents)"))).base_command("cp")
    step = Step(tool.argument("out.txt", position=2), step_name="capture_text")
    step.inputs.source = str(root / "source.bin")
    flow = Workflow([step], "capture")
    flow.outputs.captured = step.outputs.text
    run = flow.compile(target="nextflow").write(root / "run")
    assert "eval" not in (run / "workflow.nf").read_text(encoding="utf-8")
    result = execute_nextflow(run)
    output = result.stdout + result.stderr
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if len(contents) > 65536:
        assert result.returncode != 0 and "65536 bytes or less" in output and f"{len(contents)} bytes" in output, output
    elif text is None:
        assert result.returncode != 0 and "is not valid UTF-8" in output, output
    else:
        assert result.returncode == 0, output
        assert _recorded(result.stdout)["captured"] == text


#: A tool that copies its source into an output-target Directory, which it captures.
_COPY_INTO = """cwlVersion: v1.2
class: CommandLineTool
baseCommand: cp
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.outDir)
      writable: true
inputs:
  outDir:
    type: Directory
  source:
    type: File
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
arguments:
- $(inputs.source)
- $(inputs.outDir.basename)/copied.txt
"""


@pytest.mark.nextflow
@pytest.mark.serial
def test_the_command_line_runs_the_nextflow_target_with_paths_read_beside_the_workflow(tmp_path: Path) -> None:
    """Started from elsewhere, the run reads a relative path beside the `.wic` file, makes the output directory
    named there, which the tool's writable copy leaves empty, and writes the output object under the Sophios
    name of the output."""
    write_tree(tmp_path, {
        "copy_into.cwl": _COPY_INTO, "source.txt": "copied text\n",
        "w.wic": "steps:\n  copy_into:\n    in:\n      outDir: !ii made\n      source: !ii source.txt\n"
                 "    out:\n    - outDir: !& made\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _sophios_cli(tmp_path, tmp_path / "w.wic", tmp_path, "--target", "nextflow", "--run_local", "--basepath",
                          "out", cwd=elsewhere, timeout=180)
    assert_succeeded(result)
    assert (tmp_path / "made").is_dir() and not any((tmp_path / "made").iterdir())
    outputs = json.loads((elsewhere / "out" / "output_w.json").read_text(encoding="utf-8"))
    made = outputs["copy_into~outDir"]
    assert (made["class"], made["basename"]) == ("Directory", "made")
    assert (Path(made["path"]) / "copied.txt").read_text(encoding="utf-8") == "copied text\n"
    assert sorted(path.name for path in elsewhere.iterdir()) == ["out"]
