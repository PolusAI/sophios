"""Source-to-runtime conformance under installed, pinned Nextflow."""

# pylint: disable=missing-function-docstring,protected-access

from collections import Counter
from collections.abc import Sequence
from functools import partial
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, NamedTuple

from hypothesis import example, given, strategies as st
import pytest
import yaml

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow
from sophios.backends import recorded_outputs
from sophios.nf_symbols import NEXTFLOW_SCRIPT_NAMES
from sophios.run_status import RunState, RunStatus

from ..budgets import budget
from ..cases import DATA, case_dirs
from ..helpers import python_process, search_config
from .test_executable_ir import NAMES
from .test_public_api import supported_workflow
from .test_reference_differential import _cwltool, _observed, _run_cwltool, _run_nextflow, _tree
from .testkit import assert_succeeded, example_workflow, examples, execute_nextflow


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
def test_a_containerized_command_runs_after_its_images_entrypoint(tmp_path: Path) -> None:
    """CWL gives the command to the container as its arguments, so the image's ENTRYPOINT runs first; a tool with no
    DockerRequirement runs on the host in the same workflow."""
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

    #: Prints where it runs; a tool without a DockerRequirement runs on the host beside the container ones.
    on_host = (CommandLineTool("on_host", Inputs(), Outputs(place=Output(cwl.file, glob="place.txt")))
               .base_command("sh", "-c", "test -e /.dockerenv && echo container || echo host").stdout("place.txt"))
    steps = [
        Step(words("no_base_command").docker(image), step_name="no_base_command"),
        Step(words("base_command", "printf").docker(image), step_name="base_command"),
        Step(words("no_entrypoint", "echo").docker("debian:bookworm-slim"), step_name="no_entrypoint"),
        Step(on_host, step_name="on_host"),
    ]
    workflow = Workflow(steps, "nextflow_entrypoint")
    for step in steps[:3]:
        setattr(workflow.outputs, step.process_name, step.outputs.out)
    workflow.outputs.on_host = steps[3].outputs.place
    result = execute_nextflow(workflow.compile(target="nextflow").write(tmp_path / "pipeline"))
    assert_succeeded(result)
    work = tmp_path / "pipeline" / "work"
    assert sorted(path.read_text(encoding="utf-8") for path in work.rglob("out.txt")) == [
        "an argument\n", "from the entrypoint an argument\n", "from the entrypoint printf an argument\n",
    ]
    assert [path.read_text(encoding="utf-8") for path in work.rglob("place.txt")] == ["host\n"]


def _recorded(stdout: str) -> dict[str, Any]:
    """Each workflow output the pipeline recorded on ``stdout``, by its Nextflow name."""
    return dict(recorded_outputs(stdout))


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


def _example(folder: Path, directory: Path, monkeypatch: pytest.MonkeyPatch) -> Workflow:
    """The workflow of the example `folder`, read from its copy at `directory` (`example_workflow`), which is made the
    working directory. Nextflow is kept from asking the network for a newer release."""
    workflow = example_workflow(folder, directory)
    monkeypatch.chdir(directory)
    monkeypatch.setenv("NXF_OFFLINE", "true")
    return workflow


def _files(directory: Path) -> dict[str, Any]:
    """What `directory` holds, each file as its text."""
    return {path.name: _tree(path) for path in directory.iterdir()}


def _kept(directory: Path, before: dict[str, Any]) -> bool:
    """Whether a run left every file `directory` held `before` it as it was."""
    return {name: found for name, found in _files(directory).items() if name in before} == before


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
@pytest.mark.parametrize("folder", examples("runs"))
def test_an_example_workflow_runs_under_nextflow_as_under_cwltool(
        folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each directory of tests/core/data/nextflow/runs is a workflow to read and to run: `workflow.wic`, the tools
    and child workflows it names, `job.yml` for its inputs, and `expected.yaml` stating every output by class,
    basename and contents. Run as Nextflow it gives exactly those outputs, and so does cwltool, the reference runner,
    for the CWL the same workflow compiles to: the statement is CWL's answer, not Sophios's. The two runners agreeing
    is not enough, since a compiler that reversed a list of sources would reverse it for both. Both leave the files
    beside the workflow as they were. An example with no `reference` run here is run as Nextflow only. A
    `workflow.nf` and `nextflow.config` are the pipeline written, for what no run shows."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    stated = yaml.safe_load((folder / "expected.yaml").read_text(encoding="utf-8"))
    for runner in ("nextflow", "cwltool")[:1 if stated.get("reference") == "none" else 2]:
        directory = tmp_path / runner
        workflow = _example(folder, directory, monkeypatch)
        before = _files(directory)
        if runner == "nextflow":
            ran = workflow.run(target="nextflow", basepath=str(directory / "out"))
            assert ran is not None and isinstance(ran.outputs, dict)
            outputs = ran.outputs
        else:
            outputs = _run_cwltool(cwltool.main.main, workflow, directory)
        assert {name: _observed(value) for name, value in outputs.items()} == stated["outputs"], runner
        assert _kept(directory, before), f"{runner} changed a file beside the workflow"
    pipeline = tmp_path / "nextflow" / "out" / "nextflow_workflow"
    for written in ("workflow.nf", "nextflow.config"):
        if (folder / written).exists():
            assert (pipeline / written).read_text(encoding="utf-8") == (folder / written).read_text(encoding="utf-8")


def _stopped(folder: Path, expected: dict[str, Any], tmp_path: Path,
             monkeypatch: pytest.MonkeyPatch) -> tuple[RunStatus, Path]:
    """An example the Nextflow target runs and cannot finish: the run fails, an error line of its log or the error the
    run raised names the step `expected` names, the run's log or Nextflow's says why as `expected` says, and the files
    beside the workflow are left as they were. cwltool `fails` on the same workflow too, or `runs` it, as `expected`
    says: where it runs, the stop is a rule of Sophios's own. Returns the run's status and its work directory."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    directory = tmp_path / "nextflow"
    workflow = _example(folder, directory, monkeypatch)
    before = _files(directory)
    handle = workflow.start(target="nextflow", basepath=str(directory / "out"))
    status = handle.wait()
    assert status.state is RunState.FAILED, handle.logs()
    logs, pipeline = handle.logs(), directory / "out" / "nextflow_workflow"
    raised = [line for line in logs.splitlines() if "ERROR" in line or expected["says"] in line]
    assert any(expected["step"] in line for line in raised), logs
    assert expected["says"] in logs + (pipeline / ".nextflow.log").read_text(encoding="utf-8"), logs
    assert _kept(directory, before)
    reference = _example(folder, tmp_path / "cwltool", monkeypatch)
    code, _, messages = _cwltool(cwltool.main.main, reference, tmp_path / "cwltool")
    assert ("fails" if code else "runs") == expected["cwltool"], messages
    return status, pipeline / "work"


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
@pytest.mark.parametrize("folder, expected",
                         case_dirs("nextflow/stops/before_a_task", "step", "says", "unwritten", "cwltool"))
def test_a_run_stops_before_a_step_whose_inputs_it_cannot_decide(
        folder: Path, expected: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pickValue that finds no one value, and a `when` holding a subexpression that is not finite, stop the run
    naming the step and why (`_stopped`) before any task of it is started: no task failed, and none wrote the step's
    file."""
    status, work = _stopped(folder, expected, tmp_path, monkeypatch)
    assert not status.steps and not list(work.rglob(expected["unwritten"]))


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
@pytest.mark.parametrize("folder, expected", case_dirs("nextflow/stops/in_a_task", "step", "says", "cwltool"))
def test_a_run_stops_at_a_task_that_leaves_no_one_answer(
        folder: Path, expected: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two files for an output that takes one, none for an output that needs one, and two entries of one name in a
    working directory stop the run naming the step and why (`_stopped`): no output is picked, and no entry is put
    inside another."""
    _stopped(folder, expected, tmp_path, monkeypatch)


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
_INT = st.integers(-2**31, 2**31 - 1)
_METHODS = ["dotproduct", "flat_crossproduct", "nested_crossproduct"]
_END = "end of an array of words"
_TYPES = {"string": cwl.string, "File": cwl.file, "int": cwl.int}


class _Step(NamedTuple):
    """A step: its method; its scattered inputs, each `(kind, words)` for string, File (a word is its text) or int, or
    `("File", j)` for step j's gathered Files; its other inputs, each `(kind, value)`: a string, int or File of
    text, a File that is step j's unscattered output, or a File[] or string[] of words, of step j's gathered Files,
    of step j's first scattered input (`("shares", j)`) or of several steps' outputs merge_flattened (`("merge", js)`);
    an int is written by the tool as `$(inputs.shared0)0`. Then: whether its File output declares `$(self[0])`; whether
    its first input is the first step's first; and the name its output shares with other steps', if it has one."""
    method: str
    inputs: list[tuple[str, Any]]
    broadcasts: Sequence[tuple[str, Any]] = ()
    single: bool = False
    shares: bool = False
    basename: str | None = None


def _array(value: Any, steps: list[_Step], printed: list[Any]) -> list[str]:
    """The texts of the elements a File[] or string[] input holds."""
    if isinstance(value, int):
        return list(printed[value])
    if not isinstance(value, tuple):
        return list(value)
    tag, source = value
    if tag == "shares":
        return list(steps[source].inputs[0][1])
    return [text for index in source for text in (printed[index] if isinstance(printed[index], list) else [
        printed[index]])]


def _printed(spec: _Step, steps: list[_Step], printed: list[Any]) -> Any:
    """What CWL v1.2 (WorkflowStep, scatterMethod) gives a step whose task prints its arguments joined by `-`: one task
    per index (None if unequal), every combination, or nested one level per input; the rest reach each whole."""
    def words(source: Any) -> list[str]:
        return [str(word) for word in (printed[source] if isinstance(source, int) else source)]
    tail = ["".join(_array(value, steps, printed)) if kind.endswith("[]") else f"{value}0" if kind == "int" else
            printed[value] if isinstance(value, int) else value for kind, value in spec.broadcasts]
    lists = [words(source) for _, source in spec.inputs]

    def nested(combination: list[str], rest: list[list[str]]) -> Any:
        return "-".join([*combination, *tail]) if not rest else [nested([*combination, word], rest[1:])
                                                                 for word in rest[0]]
    if not lists or spec.method == "nested_crossproduct":
        return nested([], lists)
    if spec.method == "dotproduct":
        return [nested(list(row), []) for row in zip(*lists)] if len(set(map(len, lists))) == 1 else None
    return [nested(list(combination), []) for combination in itertools.product(*lists)]


@st.composite
def _scatters(draw: st.DrawFn) -> list[_Step]:
    """1..5 steps; one may read earlier scattering, non-nesting steps' gathered Files, once or twice, or an unscattered
    step's File; dotproduct inputs are equally long unless the draw says otherwise, so later steps run; S3's mismatch
    stays an @example."""
    steps: list[_Step] = []
    printed: list[Any] = []
    for _ in range(draw(st.integers(1, 5))):
        method = draw(st.sampled_from(_METHODS))
        ran = [index for index, values in enumerate(printed) if values is not None]
        gathered = [index for index in ran if steps[index].inputs and steps[index].method != "nested_crossproduct"]
        whole = [index for index in ran if not steps[index].inputs]
        literal = {kind: [index for index, step in enumerate(steps) if step.inputs and step.inputs[0][0] == kind
                          and isinstance(step.inputs[0][1], list)] for kind in ("File", "string")}
        shares = bool(steps and steps[0].inputs) and draw(st.booleans())
        first = [steps[0].inputs[0]] if shares else []
        for _ in range(draw(st.integers(0, 2)) if gathered else 0):
            first.append(("File", draw(st.sampled_from(gathered))))
        lengths = [len(source if isinstance(source, list) else printed[source]) for _, source in first]
        size = (lengths[0] if lengths else draw(st.integers(0, 3))) if method == "dotproduct" and draw(
            st.integers(0, 7)) else None

        inputs = first + [(kind, draw(st.lists(_INT if kind == "int" else _WORD, min_size=size or 0,
                                               max_size=3 if size is None else size)))
                          for kind in draw(st.lists(st.sampled_from(["string", "File", "int"]),
                                                    max_size=3 - len(first)))]
        kinds: list[Any] = [
            st.tuples(st.sampled_from(["string", "File"]), _WORD), st.tuples(st.just("int"), _INT),
            st.tuples(st.sampled_from(["File[]", "string[]"]), st.lists(_WORD, max_size=2))]
        for tag in ("File", "string"):
            if literal[tag]:
                kinds.append(st.tuples(st.just(f"{tag}[]"), st.tuples(
                    st.just("shares"), st.sampled_from(literal[tag]))))
        if whole:
            kinds.append(st.tuples(st.just("File"), st.sampled_from(whole)))
        if gathered:
            kinds.append(st.tuples(st.just("File[]"), st.sampled_from(gathered)))
        if len(gathered + whole) > 1:
            kinds.append(st.tuples(st.just("File[]"), st.tuples(st.just("merge"), st.lists(
                st.sampled_from(gathered + whole), min_size=2, max_size=3, unique=True))))
        steps.append(_Step(method, inputs, draw(st.lists(st.one_of(kinds), max_size=3)), draw(st.booleans()), shares,
                           draw(st.none() | st.sampled_from(["out.txt", "piece.txt"]))))
        printed.append(_printed(steps[-1], steps, printed))
    return steps


def _staged(root: Path, name: str, words: list[str]) -> list[str]:
    """Each word in a file of its own under `root`: two scalar inputs of one basename collide in a Nextflow task."""
    paths = [root / f"{name}{index}.txt" for index in range(len(words))]
    for path, word in zip(paths, words):
        path.write_text(word, encoding="utf-8")
    return list(map(str, paths))


def _scatter_reads(spec: _Step) -> tuple[dict[str, Input], list[str], list[tuple[str, int]], list[str]]:
    """What the task of `spec` takes and prints: its ports, the script part that prints each input and shifts it off
    (an array ends at a word no draw can make), the arguments it is given, and the inputs staged as entries of their
    own, which are Files that are another step's output, all sharing a name."""
    ports, reads, arguments, entries, position = {}, [], [], [], 1
    for name, kind, value, is_input in [(f"in{n}", kind, source, True) for n, (kind, source) in enumerate(
            spec.inputs)] + [(f"shared{n}", kind, value, False) for n, (kind, value) in enumerate(spec.broadcasts)]:
        if kind == "File" and isinstance(value, int):
            ports[name] = Input(cwl.file, position=None)
            entries.append(name)
            reads.append(f"cat -- {name}.txt")
        elif kind.endswith("[]"):
            ports[name] = Input(cwl.array(_TYPES[kind[:-2]]), position=position)
            arguments.append((_END, position + 1))
            reads.append(f'while [ "$1" != "{_END}" ]; do {"cat --" if kind == "File[]" else "printf %s"} "$1"; '
                         'shift; done; shift')
            position += 2
        else:
            ports[name] = Input(_TYPES[kind], position=None if kind == "int" and not is_input else position)
            if kind == "int" and not is_input:
                arguments.append((f"$(inputs.{name})0", position))
            reads.append('cat -- "$1"; shift' if kind == "File" else 'printf %s "$1"; shift')
            position += 1
    return ports, reads, arguments, entries


def _scatter_step(index: int, spec: _Step) -> Step:
    """Step `index` of a scatter workflow: a task that prints each of its inputs, joined by `-`."""
    ports, reads, arguments, entries = _scatter_reads(spec)
    # Each task sleeps up to 0.3 s by what it prints, so the tasks of a step finish out of order. A step's file is
    # named after it: a task writing the name of a staged input would write through it.
    script = ('sleep "0.$(( $({ printf %s "$*"; ' + "".join(f"cat -- {name}.txt; " for name in entries) +
              '} | cksum | cut -d" " -f1) % 4 ))"; ' + "; printf -; ".join(reads))
    output = spec.basename or f"s{index}.txt"
    inputs = Inputs(**ports)
    tool = CommandLineTool(f"each{index}", inputs, Outputs(
        out=Output(cwl.file, glob=output, output_eval="$(self[0])" if spec.single else None),
        text=Output(cwl.string, glob=output, load_contents=True, output_eval="$(self[0].contents)")))
    for name in entries:
        tool.stage(getattr(inputs, name), entryname=f"{name}.txt")
    for value, where in arguments:
        tool.argument(value, position=where)
    return Step(tool.base_command("sh", "-c", script, f"s{index}").stdout(output), step_name=f"s{index}")


def _scatter_workflow(steps: list[_Step], root: Path) -> tuple[Workflow, list[list[str]]]:
    """`steps` as a workflow, each File written under `root`, and each step's scattered sources by their names."""
    built = [_scatter_step(index, spec) for index, spec in enumerate(steps)]
    sources = [[f"s{source}.out" if isinstance(source, int) else "s0_0" if spec.shares and n == 0 else
                f"s{index}_{n}" for n, (_, source) in enumerate(spec.inputs)] for index, spec in enumerate(steps)]
    flow = Workflow(built, "scatter")

    def shared(index: int, n: int, kind: str, value: Any) -> Any:
        """What step `index` takes for its `n`th unscattered input."""
        if isinstance(value, tuple) and value[0] == "merge":
            return StepInput(source=[built[source].outputs.out for source in value[1]], link_merge="merge_flattened")
        if isinstance(value, tuple):
            return getattr(flow.inputs, sources[value[1]][0])
        if kind != "int" and isinstance(value, int):
            return built[value].outputs.out
        if kind == "File[]":
            return _staged(root, f"{index}_shared{n}", value)
        if kind == "File":  # A metacharacter names a file, not a pattern.
            return _staged(root, f"{index}_reference[1]*{n}", [value])[0]
        return value

    for index, (step, spec) in enumerate(zip(built, steps)):
        for n, ((kind, source), name) in enumerate(zip(spec.inputs, sources[index])):
            if isinstance(source, int):
                setattr(step.inputs, f"in{n}", built[source].outputs.out)
                continue
            setattr(step.inputs, f"in{n}", getattr(flow.inputs, name).as_type(cwl.array(_TYPES[kind])))
            setattr(flow.inputs, name, _staged(root, name + "_", source) if kind == "File" else source)
        if spec.inputs:
            step.scatter_on(*(getattr(step.inputs, f"in{n}") for n in range(len(spec.inputs))), method=spec.method)
        for n, (kind, value) in enumerate(spec.broadcasts):
            setattr(step.inputs, f"shared{n}", shared(index, n, kind, value))
        setattr(flow.outputs, f"s{index}_out", step.outputs.out)
        setattr(flow.outputs, f"s{index}_text", step.outputs.text)
    return flow, sources


def _s(*words: str) -> tuple[str, list[str]]:
    return ("string", list(words))


def _f(*words: str) -> tuple[str, list[str]]:
    return ("File", list(words))


def _i(*numbers: int) -> tuple[str, list[int]]:
    return ("int", list(numbers))


#: Steps whose Files are all called out.txt, or piece.txt.
_NAMED = ("dotproduct", [_s("a", "b", "c"), _s("x", "y", "z")], [("int", 2)], False, False, "out.txt")
_SWAPPED = ("dotproduct", [_s("x", "y", "z"), _s("a", "b", "c")], [("int", 2)], False, False, "out.txt")
_PAIRS = ("dotproduct", [_s("a", "b", "c"), _s("x", "y", "z")], [("int", 2)], False, False)


@pytest.mark.nextflow
@pytest.mark.serial
@budget(3)
@given(_scatters())
# S1, S2, S9 and S10 (design §6, Topology): two and three inputs pair by index; a scalar or a File reaches every task.
@example([("dotproduct", [_s("a", "b", "c"), _s("2", "1", "0")], [], False, False),
          ("dotproduct", [_s("a", "b", "c"), _s("x", "y", "z"), _s("0", "0", "0")], [], False, False),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], [("string", "0")], False, False),
          ("dotproduct", [_f("firstA\n", "firstB\n"), _f("secondA\n", "secondB\n")], [("File", "shared\n")], False,
           False)])
# S5, S6, S7, S8 and S11: the first input outermost, flat or one level per input; every output per invocation.
@example([("flat_crossproduct", [_s("a", "b"), _s("1", "0")], [], False, False),
          ("flat_crossproduct", [_s("a", "b"), _s("x", "y", "z"), _s("0")], [], False, False),
          ("nested_crossproduct", [_s("a", "b"), _s("1", "0", "0")], [], False, False),
          ("nested_crossproduct", [_s("a", "b"), _s("x", "y"), _s("0")], [], False, False)])
# S4, S6 and S8 empty: no task, an empty dimension keeps its outer shape; an empty unscattered File array.
@example([("dotproduct", [_s(), _s()], [], False, False),
          ("flat_crossproduct", [_s("a"), _s(), _s("0")], [], False, False),
          ("nested_crossproduct", [_s("a", "b"), _s(), _s("0")], [], False, False),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], [("File[]", [])], False, False)])
# One parameter scattered at two steps, `$(self[0])` declared; one unscattered File is a list of one, not a bare Path.
@example([("dotproduct", [_s("alpha", "beta", "gamma")], [("string", "shared")], True, False),
          ("dotproduct", [_s("alpha", "beta", "gamma")], [("string", "shared")], True, True),
          ("dotproduct", [_s("a", "b"), _s("x", "y")], [("File[]", ["shared\n"])], False, False),
          ("dotproduct", [_s("alpha", "beta")], [("File", "shared file\n")], False, False)])
# An empty scatter beside an unscattered step ends; File elements and a File broadcast (S10 of one input).
@example([("dotproduct", [_s()], [], False, False), ("dotproduct", [], [], False, False),
          ("dotproduct", [_f("alpha\n", "beta\n")], [("File", "shared file\n")], False, False),
          ("dotproduct", [_f("a\n", "b\n")], [("File", "shared\n")], False, False)])
# S3: unequal lengths fail the run naming each input and its length; no task runs truncated.
@example([("dotproduct", [_s("a", "b"), _s("x")], [], False, False),
          ("dotproduct", [_s("a", "b"), _s("0")], [], False, False)])
# S12, S13, S14: three gathered Files scattered again beside Files, gathered into one step and, apart, into another,
# and staged whole in every task of a scattered step.
@example([_PAIRS, ("dotproduct", [("File", 0), _f("1\n", "2\n", "3\n")], [], False, False),
          ("dotproduct", [], [("File[]", 1)], False, False), ("dotproduct", [], [("File[]", 0)], False, False),
          ("dotproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0)], False, False)])
# A gather of one file reaches a step, scattered or not, as a list.
@example([("dotproduct", [_s("a"), _s("x")], [("int", 2)], False, False),
          ("dotproduct", [("File", 0), _f("1\n")], [], False, False), ("dotproduct", [], [("File[]", 1)], False, False),
          ("dotproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0)], False, False),
          ("flat_crossproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0)], False, False)])
# Three gathered Files in every task of a flat_crossproduct; a nested step over a gathered array and an input array.
@example([_PAIRS, ("flat_crossproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0)], False, False),
          ("nested_crossproduct", [("File", 0), _f("x", "y", "z")], [], False, False)])
# One array scattered at one step and taken whole at another: strings, then Files.
@example([("dotproduct", [_s("x", "y z")], [], False, False),
          ("dotproduct", [], [("string[]", ("shares", 0))], False, False)])
@example([("dotproduct", [_f("a.txt\n", "b.txt\n")], [], False, False),
          ("dotproduct", [], [("File[]", ("shares", 0))], False, False)])
# Two gathered arrays of same-named Files staged for a scattered step, flat and nested, from one producer and two.
@example([_NAMED, ("dotproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0), ("File[]", 0)], False, False)])
@example([_NAMED, _SWAPPED, ("dotproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0), ("File[]", 1)], False, False)])
@example([_NAMED, _SWAPPED,
          ("nested_crossproduct", [_s("p", "q"), _s("r", "s")], [("File[]", 0), ("File[]", 1)], False, False)])
# One gathered array rescattered twice, its Files finishing out of order (the first task sleeps longest).
@example([("dotproduct", [_s("b", "a"), _s("0", "1")], [], False, False, "out.txt"),
          ("nested_crossproduct", [("File", 0), ("File", 0)], [], False, False)])
# Four scatter shapes, an empty one among them, each gathered into its own File[] of one scattered tally.
@example([("dotproduct", [_i(2001, 2002)], [], False, False, "out.txt"),
          ("dotproduct", [_i(2001, 2001, 2002, 2002), _s("north", "south", "north", "west")], [("File[]", 0)], False,
           False, "piece.txt"),
          ("flat_crossproduct", [_i(2002, 2003), _s("north", "south")],
           [("string", "beta")], False, False, "piece.txt"),
          ("dotproduct", [_i(2002, 2003)], [("string", "delta"), ("string[]", ["north", "west"])], False, False,
           "piece.txt"),
          ("dotproduct", [_i(), _s()], [("string", "gamma")], False, False, "piece.txt"),
          ("dotproduct", [_s("alpha", "beta", "delta", "gamma")],
           [("File[]", 1), ("File[]", 2), ("File[]", 3), ("File[]", 4)], False, False)])
# One step per lane, then a report of the lanes' headers and both lanes' pieces.
@example([("dotproduct", [_i(2001, 2001, 2002, 2002), _i(11, 12, 1, 2)], [("string", "lane_a")], False, False,
           "piece.txt"),
          ("dotproduct", [_i(2002, 2002), _i(6, 7)], [("string", "lane_b")], False, False, "piece.txt"),
          ("dotproduct", [], [("File[]", ["lane_a header\n", "lane_b header\n"]), ("File[]", 0), ("File[]", 1)], False,
           False)])
# A year's unscattered File reaches every task of that year's step; another step reads both years' merged; a report
# gathers the steps' outputs.
@example([("dotproduct", [], [("int", 2001)], False, False, "out.txt"),
          ("dotproduct", [], [("int", 2002)], False, False, "out.txt"),
          ("dotproduct", [_s("north", "south")], [("File", 0)], False, False),
          ("dotproduct", [_s("north", "west")], [("File", 1)], False, False),
          ("dotproduct", [_i(2001, 2001, 2002, 2002), _s("north", "south", "north", "west")],
           [("File[]", ("merge", [0, 1]))], False, False),
          ("dotproduct", [], [("File[]", 2), ("File[]", 3), ("File[]", 4)], False, False)])
# Scattered groups, one empty, merge_flattened into one input of a scattered tally.
@example([("dotproduct", [_i(2001, 2002), _s("north", "south")], [("string", "alpha")], False, False, "piece.txt"),
          ("flat_crossproduct", [_i(2002, 2003), _s("north", "west")], [("string", "beta")], False, False, "piece.txt"),
          ("dotproduct", [_i(), _s()], [("string", "gamma")], False, False, "piece.txt"),
          ("dotproduct", [_s("alpha", "beta", "gamma")], [("File[]", ("merge", [0, 1, 2]))], False, False)])
def test_a_scatter_records_what_cwl_defines_for_its_method(
        tmp_path_factory: pytest.TempPathFactory, drawn: list[_Step]) -> None:
    """design §6, Topology: each step's outputs, File and captured text, are recorded as `_printed` gives them, in
    invocation order whatever order its tasks finish in, from one task per value."""
    steps = [_Step(*spec) for spec in drawn]
    root = tmp_path_factory.mktemp("scatter")
    flow, sources = _scatter_workflow(steps, root)
    printed: list[Any] = []
    for spec in steps:
        printed.append(_printed(spec, steps, printed))
    result = execute_nextflow(flow.compile(target="nextflow").write(root / "run"))
    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # Each task's command is `sh -c <script> <step name> ...`, after the lines linking any entries.
    tasks = Counter(shlex.split(text[text.index("\n'sh' '-c'"):])[3] for text in (
        path.read_text(encoding="utf-8") for path in (root / "run" / "work").rglob(".command.sh")))
    if None in printed:
        mismatched = [index for index, values in enumerate(printed) if values is None]
        assert result.returncode != 0 and any(f"s{i}: dotproduct scatter inputs have mismatched lengths: " + ", ".join(
            f"{name}={len(printed[source] if isinstance(source, int) else source)}"
            for name, (_, source) in zip(sources[i], steps[i].inputs)) in output for i in mismatched), output
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
@example((True, True, [("plain.txt", "first\n"), ("plain.txt", "second\n")], False))
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


@pytest.mark.nextflow
@pytest.mark.serial
def test_the_command_line_runs_the_nextflow_target_with_paths_read_beside_the_workflow(tmp_path: Path) -> None:
    """tests/core/data/nextflow/command_line is a workflow naming a file and an output directory by relative paths.
    Started from elsewhere, the run reads both beside the `.wic` file, makes the output directory there, which the
    tool's writable copy leaves empty, and writes the output object under the Sophios name of the output."""
    home = tmp_path / "beside"
    shutil.copytree(DATA / "nextflow" / "command_line", home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _sophios_cli(home, home / "workflow.wic", home, "--target", "nextflow", "--run_local", "--basepath",
                          "out", cwd=elsewhere, timeout=180)
    assert_succeeded(result)
    assert not (home / "autogenerated").exists()
    assert (home / "made").is_dir() and not any((home / "made").iterdir())
    outputs = json.loads((elsewhere / "out" / "output_workflow.json").read_text(encoding="utf-8"))
    made = outputs["copy_into~outDir"]
    assert (made["class"], made["basename"]) == ("Directory", "made")
    assert (Path(made["path"]) / "copied.txt").read_text(encoding="utf-8") == "copied text\n"
    assert sorted(path.name for path in elsewhere.iterdir()) == ["out"]
