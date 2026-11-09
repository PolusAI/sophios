from contextlib import contextmanager, nullcontext
import copy
import asyncio
import decimal
import inspect
import json
import os
import pickle
import re
from pathlib import Path
import sys
import tempfile
import traceback
import warnings
from types import SimpleNamespace
from typing import Any, Callable, Final, Iterator, Literal, NoReturn, cast
from unittest.mock import Mock, patch

from hypothesis import example, given, strategies as st
import pytest
import yaml

import sophios
import sophios.api.python._workflow_runtime as python_runtime
import sophios.plugins
import sophios.post_compile
from sophios import input_output as io
from sophios import preflight
from sophios import run_local
from sophios.backends import RunRequest
from sophios.api.python import nextflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import (_python_api_types_match, InvalidInputValueError, InvalidLinkError,
                                         InvalidStepError, RunPolicy, RunResult, RunState, RunStatus, SophiosError,
                                         SophiosErrorCode, Step, StepInput, Workflow, WorkflowRunError)
from sophios.lang.diagnostics import lines as printed_lines
from sophios.python_cwl_adapter import import_python_file
from sophios.wic_types import Json, Tools

from .budgets import budget
from .helpers import (ADAPTERS, REPO_ROOT, adapter_step, echo_workflow, no_preflight, python_process,
                      write_tree)
from .reference_model import ReferenceExpectation, reference_expectation
from .strategies import cwl_types
from .test_cookbook import _bodies, _through_the_file
from .wic_reading import read_wic


def _written(workflow: Workflow, directory: Path, name: str | None = None) -> dict[str, Any]:
    """The document `write_wic` writes for `workflow` (or its nested workflow `name`), as `parse` reads it."""
    root = workflow.write_wic(directory)
    target = root if name is None else directory / f"{name}.wic"
    return read_wic(target.read_text(encoding="utf-8"))


def _emit_text_tool() -> CommandLineTool:
    """A minimal built tool that writes its message to a file."""
    return (
        CommandLineTool(
            "emit_text",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(file=Output(cwl.file, glob="stdout.txt")),
        )
        .base_command("echo")
        .stdout("stdout.txt")
    )


def _output_directory_tool(path: Path) -> Path:
    """A built tool whose output is a directory it names itself."""
    write_tree(path.parent, {path.name: """class: CommandLineTool
cwlVersion: v1.2
requirements:
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.outDir)
        writable: true
  InlineJavascriptRequirement: {}
baseCommand: [bash, -lc]
arguments:
  - valueFrom: "touch $(inputs.outDir.basename)/done.txt"
inputs:
  outDir:
    type: Directory
    inputBinding:
      prefix: --outDir
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
"""})
    return path


def _load_global_config() -> Json:
    """The config CI provisions, read from the user's home."""
    config_file = Path().home() / "wic" / "global_config.json"
    return io.read_config_from_disk(config_file)


def _iter_python_workflow_paths(global_config: Json) -> list[tuple[str, Path]]:
    """Every discovered Python workflow, minus the corpora with their own lanes: the cookbook's scripts are
    test_cookbook.py's, each named for the `.wic` beside it, which writing it out here would overwrite."""
    paths = sophios.plugins.get_workflow_paths(global_config, 'py')
    return [
        (path_str, path)
        for _, paths_dict in paths.items()
        for path_str, path in paths_dict.items()
        if "mm-workflows" not in str(path) and "docs/tutorials/" not in str(path)
        and "docs/cookbook/" not in path.as_posix()
    ]


@contextmanager
def _step_registry_injected(tool_registry: Tools) -> Iterator[None]:
    """Inject a default tool registry into imported Python workflow scripts,
    so they see the patched ``Step``."""
    from sophios.api.python import workflow  # pylint: disable=C0415:import-outside-toplevel

    step_class = workflow.Step

    def step_factory(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("tool_registry", tool_registry)
        return step_class(*args, **kwargs)

    with patch.object(workflow, "Step", step_factory):
        yield


PAIRS = Path(__file__).resolve().parent / "data" / "python_api_pairs"
_PAIRED = import_python_file("python_api_pairs", PAIRS / "pairs.py")
#: Each pair's notes, written by hand from the inference rule: the nearest step's output, the others that fit counted.
_NOTES = {"linear_inference": ["cat.file <- append.file (1 other fits)"],
          "equal_producers": ["cat.file <- second.file (1 other fits)"]}


@pytest.mark.fast
@pytest.mark.parametrize("stem", [name for name, value in vars(_PAIRED).items()
                                  if inspect.isfunction(value) and not name.startswith("_")])
def test_a_python_workflow_compiles_as_the_wic_beside_it_and_as_the_wic_it_writes(stem: str, tmp_path: Path) -> None:
    """The objects compile to the notes written for them and to the CWL, job and notes of the hand-written `.wic` of the
    same name, a note but for where it points; and the bundle `write_wic` writes compiles, through the file door, to
    the objects' CWL, job and notes."""
    workflow = getattr(_PAIRED, stem)()
    compiled = workflow.compile()
    assert [note.message for note in compiled.diagnostics] == _NOTES.get(stem, [])
    assert _through_the_file(PAIRS / f"{stem}.wic") == (
        compiled.cwl_workflow, compiled.cwl_job_inputs, _bodies(compiled.diagnostics))
    assert _through_the_file(workflow.write_wic(tmp_path)) == (
        compiled.cwl_workflow, compiled.cwl_job_inputs, _bodies(compiled.diagnostics))


def _warned_on_cwl() -> Workflow:
    """A guessed edge, noted on either target (`wic043`); a `when` that gives a string, noted on CWL (`wic056`); and
    `cat` running on a `when` that reads an input it does not have, noted on either target (`wic056`)."""
    echo = adapter_step("echo")
    echo.inputs.message = "hi"
    echo.when = "$(inputs.message)"
    workflow = Workflow([*_PAIRED.equal_producers().steps, echo], "noted")
    workflow.steps[2].when = "$(inputs.nope)"
    return workflow


def _noted_on_nextflow() -> Workflow:
    """A CUDA field the Nextflow target does not check, which its lowering notes (`wic046`)."""
    gpu = (CommandLineTool("gpu_tool", Inputs(), Outputs(file=Output(cwl.file, glob="out.txt")))
           .base_command("touch", "out.txt").gpu(cuda_version_min="11.4"))
    return Workflow([Step(gpu, step_name="gpu")], "noted")


def _ambiguous() -> Workflow:
    """Two inputs of one step that inference would both give `touch`'s file: `wic055`, an error."""
    touch = adapter_step("touch", filename="a.txt")
    both = Step.from_cwl_document({"cwlVersion": "v1.2", "class": "CommandLineTool", "baseCommand": "true",
                                   "inputs": {"left": {"type": "File"}, "right": {"type": "File"}}, "outputs": {}},
                                  process_name="both")
    return Workflow([touch, both], "ambiguous")


@pytest.fixture(name="machine_ready")
def _machine_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with every program a run calls and a container engine, whatever this one has: the pre-flight's
    program and engine checks pass."""
    monkeypatch.setattr(preflight, "PLATFORM_LACKS_RUNNERS", None)
    monkeypatch.setattr(preflight, "runner_program", lambda name: name)
    monkeypatch.setattr(preflight, "nextflow_problems", lambda _settings: [])
    monkeypatch.setattr(preflight, "engine_problems", lambda _images, _settings: [])


@pytest.mark.fast
@pytest.mark.usefixtures("machine_ready")
@pytest.mark.parametrize("target, build, codes", [("cwl", _warned_on_cwl, ["wic043", "wic056", "wic056"]),
                                                  ("nextflow", _noted_on_nextflow, ["wic046"])])
def test_check_returns_the_notes_compile_keeps_and_prints_and_writes_nothing(
        target: Literal["cwl", "nextflow"], build: Callable[[], Workflow], codes: list[str],
        capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`check()` makes every note `compile()` keeps, the target's own among them, and prints nothing: none of them,
    nothing at all, so a `check()` and then a `compile()` show each once; it writes
    nothing into the working directory, where the base path is. `compile()` prints the notes."""
    workflow = build()
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    notes = workflow.check(target=target)
    assert not list(tmp_path.iterdir())
    assert tuple(capsys.readouterr()) == ("", "")
    compiled = workflow.compile(target=target)
    assert notes == compiled.diagnostics
    assert [str(note.code) for note in notes] == codes
    printed = capsys.readouterr().err.splitlines()
    assert printed == printed_lines(notes)
    assert any("reads inputs.nope, which its process does not declare" in note.message for note in notes) is (
        target == "cwl")


def _reading_a_file(path: Path) -> Workflow:
    """`cat` of a file bound on the workflow's input."""
    tool = (CommandLineTool("cat_tool", Inputs(file=Input(cwl.file, position=1)),
                            Outputs(out=Output(cwl.file, glob="stdout.txt")))
            .base_command("cat").stdout("stdout.txt"))
    step = tool.to_step(step_name="cat")
    workflow = Workflow([step], "wf")
    step.inputs.file = workflow.inputs.file.as_type("File")
    workflow.inputs.file = str(path)
    return workflow


@pytest.mark.fast
@pytest.mark.usefixtures("machine_ready")
@pytest.mark.parametrize("target", ["cwl", "nextflow"])
def test_check_reports_an_input_path_that_does_not_exist_as_run_does(
        target: Literal["cwl", "nextflow"], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--check` reports a bound File that is not there (`wic016`), every problem at once; so does `check()`, as the
    `SophiosError` `run()` raises, and it leaves the working directory as it found it."""
    monkeypatch.chdir(tmp_path)
    workflow = _reading_a_file(tmp_path / "absent.txt")
    with pytest.raises(SophiosError) as error:
        workflow.check(target=target)
    problem, = error.value.diagnostics
    assert str(
        problem.code) == "wic016" and f"{str(tmp_path / 'absent.txt')!r}, which does not exist" in problem.message
    assert not list(tmp_path.iterdir())
    (tmp_path / "absent.txt").write_text("x", encoding="utf-8")
    assert workflow.check(target=target) == workflow.compile(target=target).diagnostics


@pytest.mark.fast
def test_check_under_nextflow_reports_a_nextflow_that_is_not_installed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--check --target nextflow` reports the missing `nextflow` (`wic029`) with the install instructions; so does
    `check(target="nextflow")`, while a CWL check of the same workflow, which does not call Nextflow, passes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preflight, "PLATFORM_LACKS_RUNNERS", None)
    monkeypatch.setattr(preflight, "runner_program", lambda name: None if name == "nextflow" else name)
    workflow = _noted_on_nextflow()
    with pytest.raises(SophiosError) as error:
        workflow.check(target="nextflow")
    problem, = error.value.diagnostics
    assert str(problem.code) == "wic029" and "the run calls nextflow, which is neither" in problem.message
    assert "install Nextflow" in problem.message
    workflow.check()


@pytest.mark.fast
@pytest.mark.parametrize("target", ["cwl", "nextflow"])
def test_check_raises_what_compile_raises(target: Literal["cwl", "nextflow"]) -> None:
    """A workflow that does not compile is the same `SophiosError` from `check()` as from `compile()`, whichever the
    target."""
    with pytest.raises(SophiosError) as from_check:
        _ambiguous().check(target=target)
    with pytest.raises(SophiosError) as from_compile:
        _ambiguous().compile(target=target)
    assert "[wic055]" in str(from_check.value) and str(from_check.value) == str(from_compile.value)


def _refused_when() -> Workflow:
    """A `when` comparing strings, which the Nextflow target's subset does not do (`nf-s010`), at the step's line."""
    echo = Step(clt_path=ADAPTERS / "echo.cwl")
    flow = Workflow([echo], "refused")
    echo.inputs.message = "hi"
    echo.when = '$(inputs.message < "27")'
    return flow


@pytest.mark.fast
def test_a_nextflow_refusal_prints_as_a_diagnostic_at_its_line() -> None:
    """A refusal reads as every other error does: where, `error`, its code, the message, and the step."""
    line = inspect.getsourcelines(_refused_when)[1] + 2
    with pytest.raises(nextflow.NotLowered) as refused:
        _refused_when().compile(target="nextflow")
    assert str(refused.value) == (
        f"{__file__}:{line}:1: error [nf-s010] when: '$(inputs.message < \"27\")' is outside the safe JavaScript "
        "subset: < requires number operands, not string (step 'echo')")


@pytest.mark.fast
@pytest.mark.parametrize("build, policy, codes", [
    (_refused_when, RunPolicy(), ["nf-s010"]),
    (_noted_on_nextflow, RunPolicy(partial_failure=True), ["nf-s001"])])
def test_check_returns_what_the_nextflow_target_refuses(build: Callable[[], Workflow], policy: RunPolicy,
                                                        codes: list[str]) -> None:
    """`check()` answers whether the target can run the workflow, and returns the answer: each refusal is an error
    diagnostic among the notes, and partial failure, a policy of the CWL target, is the one refusal."""
    found = build().check(target="nextflow", policy=policy)
    errors = [diagnostic for diagnostic in found if str(diagnostic.severity) == "error"]
    assert [str(error.code) for error in errors] == codes
    with pytest.raises(nextflow.NotLowered) as refused:
        build().compile(target="nextflow", policy=policy)
    assert errors == list(refused.value.diagnostics)
    assert all(isinstance(refusal, nextflow.Refusal) for refusal in refused.value.refusals)


@pytest.mark.fast
@pytest.mark.parametrize("call", ["compile", "check", "run", "start"])
def test_an_unknown_target_is_one_message_from_every_entry_point(call: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The table of targets says which targets there are, once, before anything is compiled."""
    def compiled_first(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise AssertionError("compiled before the target was looked up")

    monkeypatch.setattr(python_runtime, "compile_workflow_result", compiled_first)
    flow = Workflow([adapter_step("touch")], "elsewhere")
    with pytest.raises(ValueError, match=r"^unknown target 'wdl'; the targets are cwl, nextflow$"):
        getattr(flow, call)(target="wdl")


@pytest.mark.fast
@pytest.mark.parametrize("kind, make, name", [
    *(("step", lambda name: adapter_step("touch", name), name)
      for name in ["a~b", "a@2", "a#b", ".hidden", ".."]),
    ("workflow", lambda name: Workflow([adapter_step("touch")], name), "..")])
def test_a_name_sophios_cannot_write_is_refused(kind: str, make: Callable[[str], object], name: str) -> None:
    """A step's name is an id of the compiled CWL and part of the names made from it, as in a `.wic`; a workflow's
    is the stem of the files it is written to, and `..` would name the directory above."""
    with pytest.raises(ValueError, match="^" + re.escape(f"{kind} name {name!r} is not a name Sophios can write: use "
                                                         "letters")):
        make(name)


_BINDABLE: Final = {'string': st.text(max_size=5), 'int': st.integers(-2**31, 2**31 - 1),
                    'float': st.floats(allow_nan=False, allow_infinity=False), 'boolean': st.booleans()}


@st.composite
def _built_tools(draw: st.DrawFn) -> tuple[CommandLineTool, dict[str, Any]]:
    """A tool echoing up to three inputs, and the values bound to those not left to the workflow."""
    names = draw(st.lists(st.sampled_from(['message', 'n', 'size', 'flag']), unique=True, min_size=1, max_size=3))
    types = {name: draw(st.sampled_from(sorted(_BINDABLE))) for name in names}
    bindings: list[dict[str, Any]] = [draw(st.sampled_from([{'position': 1}, {'flag': f'--{name}'}])) for name in types]
    inputs = Inputs(**{name: Input(declared, **binding) for (name, declared), binding in zip(types.items(), bindings)})
    tool = (CommandLineTool(draw(st.sampled_from(['echo_tool', 'emit'])), inputs, Outputs(out=Output.stdout()))
            .base_command('echo').stdout('stdout.txt'))
    return tool, {name: draw(_BINDABLE[declared]) for name, declared in types.items() if draw(st.booleans())}


@pytest.mark.fast
@given(_built_tools(), st.sampled_from([None, 'say']))
@example((_emit_text_tool(), {'message': 'hello'}), None)  # a built tool becomes a step named for it
@example((CommandLineTool('echo_tool', Inputs(message=Input(cwl.string, position=1)), Outputs(out=Output.stdout()))
          .base_command('echo').stdout('stdout.txt'), {'message': 'hi'}), 'say')  # `step_name` renames it and its path
@budget(15)
def test_a_built_tool_is_one_step_through_every_door(drawn: tuple[CommandLineTool, dict[str, Any]],
                                                     step_name: str | None) -> None:
    """Each in-memory door, binding by attribute or `step_inputs=`, writes nothing and makes the step `Step(clt_path=)`
    makes of the tool's file: named `step_name` or for the tool, with a path of that name, compiling alike."""
    tool, values = drawn
    name = step_name or tool.name
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.chdir(directory)
        steps = [_bind(Step(tool, step_name=step_name), **values), _bind(tool.to_step(step_name=step_name), **values),
                 _bind(Step.from_cwl_document(tool.to_cwl_document(), process_name=step_name), **values),
                 tool.to_step(step_name=step_name, step_inputs=values),
                 Step.from_cwl_document(tool.to_cwl_document(), process_name=step_name, step_inputs=values)]
        assert not list(Path(directory).iterdir())
        steps.append(_bind(Step(clt_path=tool.write_cwl(Path(directory) / f'{name}.cwl')), **values))
        compiled = [Workflow([step], 'wf').compile() for step in steps]
    assert {(step.process_name, step.clt_path.name) for step in steps} == {(name, f'{name}.cwl')}
    assert all((each.cwl_workflow, each.cwl_job_inputs) == (compiled[-1].cwl_workflow, compiled[-1].cwl_job_inputs)
               for each in compiled)


def _scale_step(declared: Any) -> Step:
    """A step of a built tool whose one input `factor` is declared `declared`."""
    tool = (CommandLineTool("scale_tool", Inputs(factor=Input(declared, position=1)), Outputs(out=Output.stdout()))
            .base_command("echo").stdout("stdout.txt"))
    return Step.from_cwl_document(tool.to_cwl_document(), process_name="scale")


@pytest.mark.fast
def test_a_numpy_float_reaches_the_job_as_a_plain_float() -> None:
    """numpy's float64 is a float, and a job file holds plain numbers."""
    numpy = pytest.importorskip("numpy")
    step = _scale_step(cwl.float)
    step.inputs.factor = numpy.float64(0.5)

    (value,) = Workflow([step], "wf").compile().cwl_job_inputs.values()

    assert (value, type(value)) == (0.5, float)


@pytest.mark.fast
def test_a_value_bound_on_a_workflow_input_is_its_job_value() -> None:
    """`workflow.inputs.<name> = value` is that input's value in the job of either target, in the form a value
    written on a step takes, as an inputs file gives one."""
    tool = (CommandLineTool("cat_tool", Inputs(name=Input(cwl.string, position=1), file=Input(cwl.file, position=2)),
                            Outputs(out=Output(cwl.file, glob="stdout.txt"))).base_command("cat").stdout("stdout.txt"))
    step = tool.to_step(step_name="cat")
    workflow = Workflow([step], "wf")
    step.inputs.name, step.inputs.file = workflow.inputs.name.as_type("string"), workflow.inputs.file.as_type("File")
    workflow.inputs.name, workflow.inputs.file = "n", "data.txt"

    assert workflow.compile().cwl_job_inputs == {"name": "n", "file": {"class": "File", "location": "data.txt"}}
    assert workflow.compile(target="nextflow").nextflow_workflow.params == {
        "name": "n", "file": {"class": "File", "path": "data.txt"}}


@pytest.mark.fast
def test_a_workflow_output_with_a_leading_underscore_is_declared_by_binding_alone() -> None:
    """A port name may have a leading `_`, as Nextflow normalization writes one; reading an undeclared one, as
    IPython's display probes private names, declares nothing."""
    tool = CommandLineTool("make", Inputs(), Outputs(out=Output(cwl.file, glob="o"))).base_command("touch", "o")
    step = tool.to_step()
    workflow = Workflow([step], "wf")
    assert getattr(workflow.outputs, "_repr_html_", None) is None
    setattr(workflow.outputs, "_Out", step.outputs.out)

    assert [output.name for output in workflow.outputs] == ["_Out"]
    assert workflow.compile().cwl_workflow["outputs"] == {"_Out": {"type": "File", "outputSource": "make/out"}}


@pytest.mark.fast
@pytest.mark.parametrize("declared, python_type", [
    (cwl.int, "int"), (cwl.float, "float"), (cwl.boolean, "bool"), (cwl.string, "str"),
])
def test_a_value_of_another_python_type_is_wic020_naming_the_type_the_port_holds(
        declared: Any, python_type: str) -> None:
    """A numpy scalar or a Decimal is not a Python number: nothing converts it, and the message says so."""
    step = _scale_step(declared)
    step.inputs.factor = decimal.Decimal(3)

    with pytest.raises(SophiosError) as caught:
        Workflow([step], "wf").compile()

    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    assert diagnostic.message.endswith(f"is of type decimal.Decimal, not a Python {python_type}.")


@pytest.mark.fast
def test_step_constructor_rejects_a_step_inputs_file_for_in_memory_tool() -> None:
    """`step_inputs_file` belongs to a tool on disk; an in-memory tool has no file to configure."""
    tool = cast(Any, _emit_text_tool())
    with pytest.raises(TypeError, match="step_inputs_file is only supported"):
        Step(tool, step_inputs_file="inputs.yml")


@pytest.mark.fast
def test_tool_builder_step_bridge_supports_multistep_workflow(capsys: pytest.CaptureFixture[str]) -> None:
    """Built tools chain: two steps, ordered, with the workflow output wired through. The Python API spells
    `outputSource` with generated names itself, so no line of authored spelling is printed for it."""
    emit_step = Step(_emit_text_tool(), step_name="emit_text")
    read_step = adapter_step("cat")
    workflow = Workflow([emit_step, read_step], "builder_and_pyapi_demo")
    emit_step.inputs.message = workflow.inputs.message.as_type(cwl.string)
    read_step.inputs.file = emit_step.outputs.file
    workflow.outputs.result = read_step.outputs.output

    compiled = workflow.compile()

    assert compiled.cwl_workflow["class"] == "Workflow"
    step_ids = [step["id"] for step in compiled.cwl_workflow["steps"]]
    assert step_ids[0].endswith("emit_text")
    assert step_ids[1].endswith("cat")
    assert list(compiled.cwl_workflow["outputs"]) == ["result"]
    assert compiled.cwl_workflow["outputs"]["result"]["outputSource"] == f"{step_ids[1]}/output"
    assert "Warning!" not in capsys.readouterr().err


@pytest.mark.fast
def test_attribute_sugar_input_warning_points_at_the_reference_and_the_typed_declaration() -> None:
    """The sugar binding warns and names the way to write it; binding an input reference to an output does not."""
    step = Step(_emit_text_tool(), step_name="emit_text")
    workflow = Workflow([step], "sugar_demo")

    with pytest.warns(UserWarning, match=r"Prefer workflow\.inputs\.message, or workflow\.inputs\.message\.as_type"):
        step.inputs.message = workflow.message
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        workflow.outputs.o = workflow.inputs.x


@pytest.mark.fast
def test_output_target_directories_compile_as_runner_local_inputs(tmp_path: Path) -> None:
    """A declared output directory becomes a string input the runner fills.

    Typed `string` rather than `Directory` because the directory does not exist
    until the step runs; the `glob` refers back to the input that names it."""
    step = Step(clt_path=_output_directory_tool(tmp_path / "write_dir.cwl"))
    step.inputs.outDir = Path("result.outDir")
    workflow = Workflow([step], "virtual_dir_demo")

    compiled = workflow.compile()

    assert compiled.cwl_job_inputs["write_dir~outDir"] == "result.outDir"
    assert compiled.cwl_workflow["inputs"]["write_dir~outDir"]["type"] == "string"
    assert compiled.cwl_workflow["steps"][0]["run"]["inputs"]["outDir"]["type"] == "string"
    assert compiled.cwl_workflow["steps"][0]["run"]["outputs"]["outDir"]["outputBinding"]["glob"] == "$(inputs.outDir)"


@pytest.mark.fast
def test_output_target_directories_reject_absolute_locations(tmp_path: Path) -> None:
    """An output directory is runner-local, so an absolute path is refused."""
    step = Step(clt_path=_output_directory_tool(tmp_path / "write_dir.cwl"))
    step.inputs.outDir = tmp_path / "absolute.outDir"
    workflow = Workflow([step], "absolute_dir_demo")

    with pytest.raises(ValueError, match="cannot use an absolute location"):
        workflow.compile()


@pytest.mark.fast
def test_subworkflow_inputs_use_child_workflow_name_and_formal_parameters(tmp_path: Path) -> None:
    """A subworkflow step names the child and passes its formal parameters: the bundle written for the parent
    compiles to what the objects do, and the child declares the interface the parent binds by name."""
    touch = adapter_step("touch", filename="empty.txt")
    sub_step = adapter_step("append")
    subworkflow = Workflow([sub_step], "child")
    sub_step.inputs.file = subworkflow.inputs.file
    sub_step.inputs.str = subworkflow.inputs.str
    subworkflow.inputs.file = touch.outputs.file
    subworkflow.inputs.str = "Hello"
    root = Workflow([touch, subworkflow], "root")

    compiled = root.compile()

    assert _through_the_file(root.write_wic(tmp_path))[:2] == (compiled.cwl_workflow, compiled.cwl_job_inputs)
    assert "subtree" not in _written(root, tmp_path)["steps"][1]
    assert _written(root, tmp_path, "child")["inputs"] == {"file": {"type": "File"}, "str": {"type": "string"}}


_CWL_TYPES = cwl_types(
    'null', 'boolean', 'int', 'long', 'float', 'double', 'string', 'File', 'Directory', 'Any', 'stdout',
    'example.org#Thing', {'type': 'record', 'fields': []}, {'type': 'enum', 'symbols': ['a']}, {'type': 'array'})


@pytest.mark.fast
@given(_CWL_TYPES, _CWL_TYPES)
@example('string', 'File')
@example('string', 'string')
@example('string[]', 'Any')
@example({'type': 'array', 'items': 'string'}, {'type': 'array', 'items': 'Any'})
@example('string', {'type': 'record', 'fields': []})
@budget(200)
def test_python_api_uses_the_conservative_language_judgment(parameter_type: Any, candidate_type: Any) -> None:
    """The eager API rejects proven disjointness and defers every unknown: it refuses only what the independent model
    judges disjoint from the port as declared and lifted one array level, as a later `scatter_on()` would."""
    def disjoint(sink: Any) -> bool:
        return reference_expectation(candidate_type, sink) is ReferenceExpectation.DISJOINT
    assert _python_api_types_match(parameter_type, candidate_type) is not (
        disjoint(parameter_type) and disjoint({'type': 'array', 'items': parameter_type}))


def _bind(step: Step, **values: Any) -> Step:
    """`step`, with `values` bound to its inputs."""
    for name, value in values.items():
        setattr(step.inputs, name, value)
    return step


def _sink(declared: str) -> Step:
    """A step that takes `x`, declared `declared`, and does nothing."""
    return Step.from_cwl_document({"cwlVersion": "v1.2", "class": "CommandLineTool", "baseCommand": "true",
                                   "inputs": {"x": {"type": declared}}, "outputs": {}}, process_name="sink")


def _touched() -> tuple[Step, Step]:
    """Steps `a` and `b`, each touching a file of its name."""
    return adapter_step("touch", "a", filename="a.txt"), adapter_step("touch", "b", filename="b.txt")


def _bindable() -> SimpleNamespace:
    """Fresh steps in one workflow whose input `text` is a `string`, and an `echo` whose undeclared `go` a
    `StepInput` bound."""
    a, b = _touched()
    steps = SimpleNamespace(a=a, b=b, append=adapter_step("append"), cat=adapter_step("cat"),
                            echo=adapter_step("echo"), sink=_sink("int"))
    steps.workflow = Workflow([a, b, steps.append, steps.cat, steps.echo, steps.sink], "w")
    steps.workflow.inputs.text.as_type("string")
    steps.defaulted = adapter_step("echo", go=StepInput(default=True))
    return steps


@pytest.mark.fast
@pytest.mark.parametrize("binding, error, fragment", [
    (lambda s: (s.append, "misspelled", "Hello"), AttributeError, "has no input named"),
    (lambda s: (s.append.inputs, "str", s.a.outputs.file), InvalidLinkError, "incompatible types"),
    (lambda s: (s.append.inputs, "file", s.workflow.inputs.text), InvalidLinkError,
     "append.file has incompatible types"),
    (lambda s: (s.echo.inputs, "go", True), AttributeError, "no input named 'go'"),
    (lambda s: (s.defaulted.inputs, "go", True), AttributeError, "no input named 'go'"),
    (lambda s: (s.cat.inputs, "file", [s.a.outputs.file]), InvalidInputValueError, "StepInput"),
    (lambda s: (s.cat.inputs, "file", (s.workflow.inputs.f,)), InvalidInputValueError, "StepInput"),
    (lambda s: (s.cat.inputs, "file", [[s.a.outputs.file]]), InvalidInputValueError, "StepInput"),
    (lambda s: (s.cat.inputs, "file", {"x": s.a.outputs.file}), InvalidInputValueError, "StepInput"),
    (lambda s: (s.cat.inputs, "file", [{"x": [s.a.outputs.file]}]), InvalidInputValueError, "StepInput"),
    (lambda s: (s.cat.inputs, "file", [StepInput(source=s.a.outputs.file)]), InvalidInputValueError,
     "a StepInput is never a literal"),
    *((lambda s, record=record: (s.sink.inputs, "x", StepInput(source=[s.a.outputs.file, s.b.outputs.file], **record)),
       InvalidLinkError, "incompatible types")
      for record in ({"link_merge": "merge_flattened"}, {"pick_value": "first_non_null"})),
])
def test_a_binding_the_api_can_prove_wrong_is_refused_where_it_is_written(
        binding: Callable[[SimpleNamespace], tuple[object, str, Any]], error: type[Exception], fragment: str) -> None:
    """An input the tool does not declare, which only a `StepInput` may bind, a source of a type the input cannot
    take, scattered or not, or a port where a literal goes, anywhere inside a list or mapping, which names
    `StepInput` instead of passing for a literal."""
    target, name, value = binding(_bindable())
    with pytest.raises(error, match=fragment) as caught:
        setattr(target, name, value)


def _reading_its_own_output(a: Step) -> Workflow:
    """A workflow of `a` and a `cat` that reads the workflow's own output, which is `a`'s."""
    cat = adapter_step("cat")
    workflow = Workflow([a, cat], "loop")
    workflow.outputs.made = a.outputs.file
    cat.inputs.file = workflow.outputs.made
    return workflow


@pytest.mark.fast
@pytest.mark.parametrize("build, error, code, fragment", [
    (lambda a, b: Workflow([adapter_step("append", file=a.outputs.file, str="Hello"), a], "w"),
     InvalidStepError, SophiosErrorCode.INVALID_STEP, "must appear earlier"),
    (lambda a, b: Workflow([adapter_step("append", file=a.outputs.file, str="Hello")], "w"),
     InvalidStepError, SophiosErrorCode.INVALID_STEP, "not a child"),
    (lambda a, b: Workflow([adapter_step("cat", file=StepInput(
        source=[a.outputs.file], pick_value="first_non_null")), a], "w"),
     InvalidStepError, SophiosErrorCode.INVALID_STEP, "earlier in the workflow step list"),
    (lambda a, b: _reading_its_own_output(a), InvalidLinkError, SophiosErrorCode.INVALID_LINK, "own output"),
    *((lambda a, b, declared=declared: Workflow([a, b, _bind(_sink(declared), x=StepInput(
        source=[a.outputs.file, b.outputs.file], link_merge="merge_flattened"))], "w"),
       SophiosError, SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE, "wic023") for declared in ("File", "int[]")),
])
def test_compile_refuses_a_workflow_wired_wrong(build: Callable[[Step, Step], Workflow], error: type[SophiosError],
                                                code: SophiosErrorCode, fragment: str) -> None:
    """Order is meaning, as inference scans backwards: a step may read only an earlier step of its own workflow, by a
    link or a record, and never its workflow's own output. A merge the bind cannot judge is the compiler's to refuse.

    A caller writes one `except SophiosError` and reads `.diagnostics`, whatever failed: the API used to raise
    bare `Exception` subclasses of its own beside the compiler's `SophiosError`, neither importable from where a
    user of `compile()` would look (issue 111).
    """
    workflow = build(*_touched())
    with pytest.raises(SophiosError) as caught:
        workflow.compile()
    assert isinstance(caught.value, error) and fragment in str(caught.value)
    assert [diagnostic.code for diagnostic in caught.value.diagnostics] == [code]


@pytest.mark.fast
def test_compiled_workflow_writes_files_that_read_back_as_its_documents(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    """Each writer writes the in-memory document and no intermediate `.wic`; `write()` both to one directory, the
    current one by default."""
    compiled = Workflow([adapter_step("echo", message="hello")], "wf").compile()
    monkeypatch.chdir(tmp_path)
    assert (compiled.write_cwl(tmp_path), compiled.write_job_inputs(tmp_path)) == (tmp_path / "wf.cwl",
                                                                                   tmp_path / "wf_inputs.yml")
    assert (yaml.safe_load(compiled.to_cwl_yaml()), yaml.safe_load(compiled.to_job_inputs_yaml())) == (
        compiled.cwl_workflow, compiled.cwl_job_inputs)
    for path, directory in ((None, Path(".")), (tmp_path / "out", tmp_path / "out")):
        assert compiled.write(path) == directory
        assert yaml.safe_load((directory / "wf.cwl").read_text(encoding="utf-8")) == compiled.cwl_workflow
        assert yaml.safe_load((directory / "wf_inputs.yml").read_text(encoding="utf-8")) == compiled.cwl_job_inputs
        assert not list(directory.rglob("*.wic"))


@pytest.mark.fast
def test_port_names_reject_namespace_collisions() -> None:
    """A port may not shadow the port namespace's own API."""
    with pytest.raises(ValueError, match="reserved by port namespaces"):
        Step.from_cwl_document({'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'true',
                                'inputs': {'_store': 'string'}, 'outputs': {}}, process_name='bad')


@pytest.mark.fast
@pytest.mark.parametrize("removed, error, fix", [
    (lambda: Workflow([], "wf").yaml, AttributeError, "write_wic"),
    (lambda: Workflow([], "wf").to_wic_yaml, AttributeError, "write_wic"),
    (lambda: _emit_text_tool().to_step(config={}),  # type: ignore[call-arg]  # pylint: disable=unexpected-keyword-arg
     TypeError, "config"),
], ids=["yaml", "to_wic_yaml", "config"])
def test_a_removed_api_fails_loudly(removed: Callable[[], object], error: type[Exception], fix: str) -> None:
    """The removed `.wic` accessors, and `to_step`'s `config=`, fail loudly instead of meaning something else."""
    with pytest.raises(error, match=fix):
        removed()


@pytest.mark.fast
def test_workflow_write_wic_rejects_non_wic_file_extension(tmp_path: Path) -> None:
    """A destination must be a `.wic` file or a directory to put one in."""
    workflow = Workflow([], "wf")

    with pytest.raises(ValueError, match=r"\.wic file or a directory"):
        workflow.write_wic(tmp_path / "workflow.yaml")


@pytest.mark.fast
def test_a_step_inputs_file_binds_each_value_as_written(tmp_path: Path) -> None:
    """Nothing is checked when the step is built: a path may name a file on the machine that runs it."""
    config = tmp_path / "append.yml"
    config.write_text(
        yaml.safe_dump({"file": {"class": "File", "location": "/cluster/project/in.txt"}, "str": "Hello"}),
        encoding="utf-8")
    append = Step(clt_path=ADAPTERS / "append.cwl", step_inputs_file=config)
    assert Workflow([append], "wf").compile().cwl_job_inputs["append~file"] == {
        "class": "File", "location": "/cluster/project/in.txt"}


class _Ended:  # pylint: disable=too-few-public-methods
    """A started run that has completed: for a test of what `run()` does around the run, not of the run."""

    def collect(self) -> RunResult:
        """The completed run's result."""
        return RunResult(RunState.COMPLETED, None, Path("outdir"), Path("run.log"), 0)


def _api_run_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_args: dict[str, Any]) -> list[str]:
    """The runner command `Workflow.run()` writes to run.sh for `run_args`."""
    workflow = Workflow([adapter_step("touch", filename="empty.txt")], "quiet_demo")
    no_preflight(monkeypatch)
    monkeypatch.chdir(tmp_path)
    workflow.run(basepath=str(tmp_path), run_args_dict={"generate_run_script": "yes", **run_args})
    return (tmp_path / "run.sh").read_text(encoding="utf-8").splitlines()[-1].split()


@pytest.mark.fast
@pytest.mark.parametrize("run_args, expected", [({}, True), *(({"quiet": quiet}, quiet in ("yes", "true", True))
                                                for quiet in ("yes", "true", True, "no", "false", False))])
def test_workflow_run_is_quiet_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                     run_args: dict[str, Any], expected: bool) -> None:
    """The Python API has always run `cwltool` with `--quiet`; `run_args_dict={'quiet': ...}` switches it, and its
    value never reaches `cwltool` as a positional."""
    cmd = _api_run_command(monkeypatch, tmp_path, run_args)
    assert ("--quiet" in cmd) is expected
    assert not {"yes", "no", "true", "false", "True", "False"} & set(cmd)


@pytest.mark.fast
@pytest.mark.parametrize("value", ["yes", "true", "1", "on", True])
def test_workflow_run_generate_run_script_run_arg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | bool
) -> None:
    """Any spelling of yes for `generate_run_script` writes `run.sh`; it does not run the workflow."""
    assert _api_run_command(monkeypatch, tmp_path, {"generate_run_script": value})


@pytest.mark.fast
def test_workflow_run_prepares_the_cwl_in_its_basepath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The pre-flight is given the CWL in the basepath, not one in the current directory."""
    example_path = REPO_ROOT / "examples" / "scripts" / "tool_builder_workflow.py"
    module = import_python_file(example_path.stem, example_path.resolve())
    workflow = module.build_workflow("hello from test")
    prepared: list[tuple[list[Path], preflight.RunSettings]] = []

    monkeypatch.setattr(python_runtime.preflight, "prepare",
                        lambda documents, settings, jobs=(): prepared.append((documents, settings)))
    monkeypatch.setattr(python_runtime.run_local, "start", lambda request: _Ended())

    workflow.run(basepath=str(tmp_path))

    (documents, settings), = prepared
    assert documents == [tmp_path / "tool_builder_workflow_demo.cwl"]
    assert settings.container_engine == "docker"
    assert settings.pull_dir == str(Path.cwd())
    assert not settings.ignore_install


@pytest.mark.fast
def test_run_runs_exactly_what_compile_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`run()` writes the one document `compile()` returns and its job, a relative path made absolute, and nothing
    else: a workflow that declares its outputs runs with those outputs only."""
    monkeypatch.chdir(tmp_path)
    Path("data.txt").write_text("hello\n", encoding="utf-8")
    cat = adapter_step("cat", file="data.txt")
    workflow = Workflow([cat, adapter_step("touch", filename="empty.txt")], "exact")
    workflow.outputs.printed = cat.outputs.output
    no_preflight(monkeypatch)

    workflow.run(basepath="base", run_args_dict={"generate_run_script": "yes", "quiet": "no"})

    compiled = workflow.compile()
    base = tmp_path / "base"
    assert sorted(path.name for path in base.iterdir()) == ["exact.cwl", "exact_inputs.yml"]
    assert yaml.safe_load((base / "exact.cwl").read_text(encoding="utf-8")) == compiled.cwl_workflow
    assert list(compiled.cwl_workflow["outputs"]) == ["printed"]
    assert yaml.safe_load((base / "exact_inputs.yml").read_text(encoding="utf-8")) == {
        **compiled.cwl_job_inputs, "cat~file": {"class": "File", "location": (tmp_path / "data.txt").as_uri()}}


@pytest.mark.fast
def test_run_with_docker_remove_entrypoints_writes_the_noentrypoint_tags(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`run()` builds the images without entrypoints, then hands the runner a document naming them:
    every embedded tool's, a `DockerRequirement` hint's as well as a requirement's."""
    monkeypatch.chdir(tmp_path)
    hinted = tmp_path / "hinted.cwl"
    write_tree(tmp_path, {"hinted.cwl": "cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: 'true'\n"
                                        "inputs: {}\noutputs: {}\n"
                                        "hints:\n  DockerRequirement:\n    dockerPull: docker.io/alpine:3.20\n"})
    no_preflight(monkeypatch)
    monkeypatch.setattr(sophios.post_compile.docker, "from_env",
                        lambda: SimpleNamespace(images=SimpleNamespace(list=lambda: [])))

    Workflow([adapter_step("touch", filename="empty.txt"), Step(clt_path=hinted)], "no_entrypoints").run(
        run_args_dict={"generate_run_script": "yes", "docker_remove_entrypoints": "yes"})

    steps = yaml.safe_load((tmp_path / "autogenerated" / "no_entrypoints.cwl").read_text(encoding="utf-8"))["steps"]
    assert [step["run"]["requirements" if "requirements" in step["run"] else "hints"]["DockerRequirement"]
            for step in steps] == [{"dockerPull": "docker.io/library/debian:bookworm-slim-noentrypoint"},
                                   {"dockerPull": "docker.io/alpine:3.20-noentrypoint"}]


@pytest.mark.fast
def test_workflow_run_does_not_forward_python_run_flags_to_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Flags meant for the Python API stay there; only runner flags pass through."""
    workflow = Workflow([adapter_step("touch", filename="empty.txt")], "runtime_flag_demo")

    requests: list[RunRequest] = []

    no_preflight(monkeypatch)

    def run(request: RunRequest) -> int:
        requests.append(request)
        return 0
    monkeypatch.setattr(python_runtime.run_local, "run", run)

    workflow.run(
        basepath=str(tmp_path),
        run_args_dict={
            "generate_run_script": "yes",
            "logLevel": "INFO",
            "outdir": str(tmp_path / "workflow_output"),
        },
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.passthrough_args == ("--logLevel", "INFO")
    assert request.generate_run_script
    assert request.outdir == str(tmp_path / "workflow_output")
    assert request.workflow_name == "runtime_flag_demo"
    assert request.basepath == tmp_path


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="directory modes do not stop Windows or root")
def test_workflow_run_reports_an_unwritable_basepath_as_wic021(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    """A `basepath` that cannot be created is one `wic021` before the run, not a PermissionError."""
    step = Step(clt_path=_output_directory_tool(tmp_path / "write_dir.cwl"))
    step.inputs.outDir = Path("result.outDir")
    workflow = Workflow([step], "locked_run_demo")
    monkeypatch.setattr(python_runtime.run_local, "start", lambda *_args, **_kwargs: pytest.fail("ran"))
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o555)

    with pytest.raises(SophiosError) as caught:
        workflow.run(basepath=str(locked / "autogenerated"))

    (problem,) = caught.value.diagnostics
    assert problem.code is SophiosErrorCode.DIRECTORY_NOT_WRITABLE
    assert problem.message == (f"Sophios writes the workflow and its run to {locked / 'autogenerated'}, "
                               f"but {locked} is not writable by you: pass run() a basepath you can write to.")


@pytest.mark.cwl_corpus
@pytest.mark.fast
def test_compile_python_workflows() -> None:
    """Import and compile all auto-discovered Python workflow scripts."""
    from sophios.api.python import workflow  # pylint: disable=C0415:import-outside-toplevel

    global_config = _load_global_config()
    tools_cwl = sophios.plugins.get_tools_cwl(global_config)
    paths_tuples = _iter_python_workflow_paths(global_config)
    import_errors: list[str] = []
    for path_stem, path in paths_tuples:
        try:
            with _step_registry_injected(tools_cwl):
                module = import_python_file(path_stem, path)
                retval: workflow.Workflow = module.workflow()

            retval.compile()
            retval.write_wic(path.parent)

            config_ci = path.parent / "config_ci.json"
            json_contents = {}
            if config_ci.exists():
                with open(config_ci, mode="r", encoding="utf-8") as r:
                    json_contents = json.load(r)
            fragments: list[str] = json_contents.get("fragments", [])
            subworkflows: list[workflow.Workflow] = retval._flatten_subworkflows()[
                1:]
            fragments += [wf.process_name for wf in subworkflows]
            json_contents["fragments"] = fragments
            with open(config_ci, mode="w", encoding="utf-8") as f:
                json.dump(json_contents, f)

        except Exception as exc:  # pylint: disable=W0718:broad-exception-caught
            import_errors.append(f"{path_stem}: {type(exc).__name__}: {exc}")
            traceback.print_exception(type(exc), value=exc, tb=None)
    if import_errors:
        pytest.fail("Python workflow imports failed:\n" +
                    "\n".join(import_errors))


def _detach_run_from_the_container_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a `Workflow.run()` call off the container engine and the disk."""
    no_preflight(monkeypatch)
    monkeypatch.setattr(python_runtime.input_output, 'write_artifacts_to_disk', lambda *_a, **_k: None)


def _failed_run(monkeypatch: pytest.MonkeyPatch, exit_code: int) -> SophiosError:
    """The error `Workflow.run()` raises when the runner ends with `exit_code`: the handle is the real one, its runner
    never started and its `wait` saying FAILED, so the raise is its `collect()`'s."""
    _detach_run_from_the_container_engine(monkeypatch)
    monkeypatch.setattr(run_local.RunHandle, '_start', lambda _handle: None)
    failed = RunStatus(RunState.FAILED, f'the runner exited with status {exit_code}', exit_code=exit_code)
    monkeypatch.setattr(run_local.RunHandle, 'wait', lambda _handle, _timeout=None: failed)
    with pytest.raises(SophiosError) as caught:
        echo_workflow('failing').run()
    return caught.value


@pytest.mark.fast
@pytest.mark.parametrize('duplicate', [
    lambda error: error,
    lambda error: pickle.loads(pickle.dumps(error)),
    copy.copy,
    copy.deepcopy,
], ids=['as-raised', 'pickle', 'copy', 'deepcopy'])
def test_run_raises_workflow_run_error_on_a_failed_run(monkeypatch: pytest.MonkeyPatch,
                                                       duplicate: Callable[[SophiosError], SophiosError]) -> None:
    """`Workflow.run()` reports a failed run, with the runner's exit code, instead of returning; the error survives
    being duplicated, so it can come back from a worker process or be re-raised elsewhere with its exit code."""
    raised = _failed_run(monkeypatch, 7)
    clone = duplicate(raised)
    assert (str(clone), list(clone.diagnostics)) == (str(raised), list(raised.diagnostics))
    assert clone.diagnostics[0].code is SophiosErrorCode.WORKFLOW_RUN_FAILED and "'failing'" in str(clone)
    assert isinstance(clone, WorkflowRunError)
    assert (clone.exit_code, clone.reason) == (7, 'the runner exited with status 7')


@pytest.mark.fast
def test_importing_sophios_leaves_the_root_logger_alone(tmp_path: Path) -> None:
    """A library that adds a handler to the root logger on import changes the logging of every program
    that imports it."""
    # A new interpreter: pytest puts handlers on the root logger before any test imports Sophios.
    run = python_process('-c', 'import logging\nimport sophios.main\nimport sophios.api.python.workflow\n'
                         'print(logging.getLogger().handlers)\n', cwd=tmp_path, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == '[]'


@pytest.mark.needs_cwltool
def test_workflow_run_runs_a_workflow_from_a_users_script(tmp_path: Path) -> None:
    """A real run, as a user's script makes it, in a new interpreter: cwltool runs and writes the summary."""
    script = '''
import json
from pathlib import Path
from sophios.api.python.workflow import Step, Workflow
echo = Step.from_cwl_document({"cwlVersion": "v1.2", "class": "CommandLineTool", "baseCommand": "echo",
                               "inputs": {"message": {"type": "string", "inputBinding": {"position": 1}}},
                               "outputs": {"said": "stdout"}, "stdout": "said.txt"}, process_name="echo")
echo.inputs.message = "hello"
Workflow([echo], "say").run()
summary = json.loads(Path("autogenerated/output_say.json").read_text(encoding="utf-8"))
print(Path(next(iter(summary.values()))["path"]).read_text(encoding="utf-8"), end="")
'''
    run = python_process('-c', script, cwd=tmp_path, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.endswith('hello\n')


#: A tool that reads one File, and runs in no container.
_CAT_TOOL = {'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'cat',
             'inputs': {'f': {'type': 'File', 'inputBinding': {'position': 1}}}, 'outputs': {'out': 'stdout'}}


def _cat() -> Step:
    return Step.from_cwl_document(_CAT_TOOL, process_name='cat')


def _cat_tool(directory: Path) -> Path:
    """`_CAT_TOOL`, written to `directory`, for a step built from a file."""
    path = directory / 'cat.cwl'
    path.write_text(yaml.safe_dump(_CAT_TOOL), encoding='utf-8')
    return path


def _job_written_by_run(monkeypatch: pytest.MonkeyPatch, workflow: Workflow) -> dict[str, Any]:
    """The job `workflow.run()` writes for the runner, with the machine and the runner left out."""
    no_preflight(monkeypatch)
    monkeypatch.setattr(python_runtime.run_local, 'start', lambda _request: _Ended())
    workflow.run()
    job = Path('autogenerated') / f'{workflow.process_name}_inputs.yml'
    return cast(dict[str, Any], yaml.safe_load(job.read_text(encoding='utf-8')))


@pytest.mark.fast
def test_a_step_input_default_path_is_resolved_against_the_working_directory(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `StepInput(default=...)` names the file a bound value of the same text would: the one here, written
    absolute when the document is built, wherever the document is then written."""
    monkeypatch.chdir(tmp_path)
    cat = _cat()
    cat.inputs.f = StepInput(default={'class': 'File', 'location': 'data.txt'})
    (step,) = Workflow([cat], 'wf').compile().cwl_workflow['steps']
    assert step['in']['f']['default'] == {'class': 'File', 'location': (tmp_path / 'data.txt').as_uri()}


def _missing_paths(monkeypatch: pytest.MonkeyPatch, workflow: Workflow) -> list[str]:
    """The `wic016` lines `workflow.run()` raises, which it must raise before the runner starts."""
    ran: list[bool] = []

    def runner(_request: RunRequest) -> _Ended:
        ran.append(True)
        return _Ended()
    monkeypatch.setattr(python_runtime.run_local, 'start', runner)
    with pytest.raises(SophiosError) as caught:
        workflow.run()
    assert not ran
    return [str(diagnostic) for diagnostic in caught.value.diagnostics
            if diagnostic.code is SophiosErrorCode.MISSING_INPUT_FILE]


@pytest.mark.fast
@pytest.mark.parametrize("tool, bound, files, step_inputs_file, inputs_file, fragments", [
    ("cat", {"f": "{cwd}/gone.txt"}, {}, None, None, ["names '{cwd}/gone.txt', which does not exist"]),
    ("cat", {"f": "gone.txt"}, {}, None, None,
     ["whose relative paths are read from {cwd}) names 'gone.txt'", "does not exist at {cwd}/gone.txt"]),
    ("cat", {}, {"cat.yml": "f: {class: File, location: gone.txt}"}, "cat.yml", None, ["names 'gone.txt'"]),
    ("cat", {"f": "gone.txt"}, {"data.txt": "hello", "in.yml": "cat~f: {class: File, location: data.txt}"}, None,
     "in.yml", []),
    ("write_dir", {"outDir": Path("result.outDir")}, {}, None, None, []),
    ("write_dir", {}, {"in.yml": "write_dir~outDir: {class: Directory, location: result.outDir}"}, None, "in.yml", []),
], ids=["absolute", "relative", "in-a-step-inputs-file", "replaced-by-an-inputs-file", "output-directory",
        "output-directory-in-an-inputs-file"])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_the_pre_flight_checks_exactly_the_paths_the_runner_reads(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: str, bound: dict[str, Any], files: dict[str, str],
        step_inputs_file: str | None, inputs_file: str | None, fragments: list[str]) -> None:
    """A path bound in Python or in a step inputs file is checked before the run starts, as an inputs file's is, from
    the working directory the run reads it from. A value an inputs file replaces is not the one the runner gets, and
    an output directory names one the step creates: neither is looked for."""
    monkeypatch.chdir(tmp_path)
    for name, text in files.items():
        Path(name).write_text(text, encoding="utf-8")
    _cat_tool(tmp_path)
    _output_directory_tool(tmp_path / "write_dir.cwl")
    step = _bind(Step(clt_path=tmp_path / f"{tool}.cwl", step_inputs_file=step_inputs_file),
                 **{port: value.format(cwd=Path.cwd()) if isinstance(value, str) else value
                    for port, value in bound.items()})
    if fragments:
        line, = _missing_paths(monkeypatch, Workflow([step], "wf"))
        assert line.startswith("error [wic016] input 'cat~f' (from the workflow, ")
        assert all(fragment.format(cwd=Path.cwd()) in line for fragment in fragments), line
        return
    start = Mock(return_value=_Ended())
    monkeypatch.setattr(python_runtime.run_local, "start", start)
    Workflow([step], "wf").run(run_args_dict={"inputs_file": inputs_file} if inputs_file else {})
    start.assert_called_once()
    assert not Path("result.outDir").exists(), "an output directory is the step's to make, not the run's"
    if "outDir" in bound:
        job = yaml.safe_load(Path("autogenerated/wf_inputs.yml").read_text(encoding="utf-8"))
        assert job["write_dir~outDir"] == "result.outDir"


@pytest.mark.fast
@pytest.mark.parametrize("bound, step_inputs_file", [({"f": "data.txt"}, None), ({}, "inputs/cat.yml")])
def test_a_relative_path_is_read_from_the_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            bound: dict[str, str],
                                                            step_inputs_file: str | None) -> None:
    """The job names it absolute, as the runner reads the job from the basepath, not from here; one in a step inputs
    file too, not from the file's directory: the file binds values as written."""
    monkeypatch.chdir(tmp_path)
    write_tree(tmp_path, {"data.txt": "hello\n", "inputs/cat.yml": "f: {class: File, location: data.txt}\n"})
    cat = _bind(Step(clt_path=_cat_tool(tmp_path), step_inputs_file=step_inputs_file), **bound)
    job = _job_written_by_run(monkeypatch, Workflow([cat], "wf"))
    assert job["cat~f"] == {"class": "File", "location": (Path.cwd() / "data.txt").as_uri()}


@pytest.mark.fast
def test_ctrl_c_during_run_is_not_reported_as_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop that handles failed runs must still stop when the user presses Ctrl-C."""
    _detach_run_from_the_container_engine(monkeypatch)

    def interrupted(_request: RunRequest) -> _Ended:
        raise KeyboardInterrupt
    monkeypatch.setattr(python_runtime.run_local, 'start', interrupted)
    with pytest.raises(KeyboardInterrupt):
        echo_workflow('interrupted').run()


@pytest.mark.needs_cwltool
def test_a_real_failed_step_is_named_with_its_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                     capfd: pytest.CaptureFixture[str]) -> None:
    """Pins cwltool's own wording: if a cwltool upgrade changes it, this fails, not a user's summary.

    capfd, not capsys: cwltool hands the real stderr's file descriptor to the step.
    """
    tool = tmp_path / "exits_3.cwl"
    tool.write_text("cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: [sh, -c, 'exit 3']\n"
                    "inputs:\n  message: string\noutputs: {}\n", encoding="utf-8")
    step = Step(clt_path=tool)
    step.inputs.message = "x"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WorkflowRunError):
        Workflow([step], "exits").run(basepath=str(tmp_path / "autogenerated"))
    err = capfd.readouterr().err
    assert "Failure! step 'exits_3" in err
    assert "exited with status 3." in err


_OTHER_ECHO: Final = "cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: false\ninputs: {}\noutputs: {}\n"


@pytest.mark.fast
@pytest.mark.parametrize('step_name', [None, 'say_hi'], ids=['named-for-its-tool', 'renamed'])
def test_a_written_step_runs_its_tool_stem_and_the_document_names_no_python_path(step_name: str | None,
                                                                                 tmp_path: Path) -> None:
    """Every step, a renamed one too, names its tool file, so the bundle compiles without the tool on a search path.
    Spans are internal: the written document never names the script that built it."""
    root = Workflow([adapter_step('echo', step_name, message='hi')], 'wf').write_wic(tmp_path)
    text = root.read_text(encoding='utf-8')
    assert read_wic(text)['steps'][0]['run'] == 'echo.cwl'
    assert (tmp_path / 'echo.cwl').is_file()
    assert '__step__' not in text and str(Path(__file__).resolve()) not in text


@pytest.mark.fast
@pytest.mark.parametrize('existing, refused', [((ADAPTERS / 'echo.cwl').read_text(encoding='utf-8'), False),
                                               (_OTHER_ECHO, True)], ids=['the-same-tool', 'a-different-tool'])
def test_write_wic_never_rewrites_the_tool_file_of_a_stem(existing: str, refused: bool, tmp_path: Path) -> None:
    """A file already named `<stem>.cwl` is never rewritten: the same tool is left as it is, a different one is
    neither replaced nor trusted."""
    source = write_tree(tmp_path, {'echo.cwl': existing}) / 'echo.cwl'
    workflow = Workflow([adapter_step('echo', message='hi')], 'wf')
    with pytest.raises(InvalidStepError, match='already exists') if refused else nullcontext():
        workflow.write_wic(tmp_path)
    assert (tmp_path / 'wf.wic').exists() is not refused
    assert source.read_text(encoding='utf-8') == existing


@pytest.mark.fast
def test_two_tools_sharing_a_file_stem_are_rejected(tmp_path: Path) -> None:
    """A document names a tool by its file stem, so two different tools under one stem are ambiguous."""
    def tool(command: str) -> dict[str, Any]:
        return {'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': command,
                'inputs': {}, 'outputs': {}}

    first = Step.from_cwl_document(tool('true'), process_name='first', run_path='tool.cwl')
    second = Step.from_cwl_document(tool('false'), process_name='second', run_path='tool.cwl')
    workflow = Workflow([first, second], 'shared_stem')

    with pytest.raises(InvalidStepError, match="share the file stem 'tool'"):
        workflow.write_wic(tmp_path)
    with pytest.raises(InvalidStepError, match="share the file stem 'tool'"):
        workflow.compile()


def _inner_and_sibling() -> tuple[Workflow, Step]:
    touch = adapter_step('touch', filename='empty.txt')
    inner = Workflow([touch], 'inner')
    inner.outputs.made = touch.outputs.file
    return inner, adapter_step('cat', file=inner.outputs.made)


def _own(step: Step, port: str) -> tuple[Step, list[Any]]:
    """`step` and its input `port`, to scatter over."""
    return step, [getattr(step.inputs, port)]


@pytest.mark.fast
@pytest.mark.parametrize("build, setter, fragment", [
    (lambda: _own(adapter_step("echo"), "message"), True, "bound before scattering"),
    (lambda: _own(adapter_step("echo", message="hello"), "message"), True, "array-valued data"),
    (lambda: (adapter_step("echo"), [adapter_step("echo", message=["a", "b"]).inputs.message]),
     True, "belong to the same step"),
    *((lambda pick=pick: _own(_bind(_sink("File"), x=StepInput(source=[step.outputs.file for step in _touched()],
                                                               pick_value=pick)), "x"), False, "array-valued data")
      for pick in ("first_non_null", "the_only_non_null")),
    (lambda: _own(_inner_and_sibling()[1], "file"), False, "array-valued data"),
])
def test_a_port_that_cannot_be_scattered_is_refused(build: Callable[[], tuple[Step, list[Any]]], setter: bool,
                                                    fragment: str) -> None:
    """Scatter needs a bound, array-valued port of the step itself; any other compiles and then fails at runtime.
    A `pickValue` that picks one source delivers one value, and a nested workflow adds no array layer of its own:
    over an unscattered step its output stays a `File`."""
    step, ports = build()
    with pytest.raises(ValueError, match=fragment):
        if setter:
            step.scatter = ports
        else:
            step.scatter_on(*ports)


@pytest.mark.fast
def test_nested_crossproduct_lifts_one_level_per_scattered_port() -> None:
    """`nested_crossproduct` nests one array per scattered port; any other method adds one."""
    echo = adapter_step('echo', message='a')
    first = adapter_step('append', file=echo.outputs.stdout, str=['x', 'y'])
    sink = adapter_step('cat', 'sink')
    lifted = sink.inputs.file
    sink.inputs.file = first.outputs.file
    first.scatter_on(first.inputs.str, method='nested_crossproduct')
    assert lifted.effective_source_type() == {'type': 'array', 'items': 'File'}
    first.inputs.file = [{'class': 'File', 'location': 'p.txt'}, {'class': 'File', 'location': 'q.txt'}]
    first.scatter_on(first.inputs.file, first.inputs.str, method='nested_crossproduct')
    assert lifted.effective_source_type() == {
        'type': 'array', 'items': {'type': 'array', 'items': 'File'}}
    first.scatter_on(first.inputs.file, first.inputs.str, method='flat_crossproduct')
    assert lifted.effective_source_type() == {'type': 'array', 'items': 'File'}


@pytest.mark.fast
def test_binding_a_path_reads_nothing_from_the_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A path is bound as written; it may name a place on another machine, such as a cluster."""
    monkeypatch.chdir(tmp_path)
    Path('images.ome.zarr').mkdir()
    here, elsewhere = _cat(), _cat()
    local, remote = here.inputs.f, elsewhere.inputs.f
    here.inputs.f = Path('images.ome.zarr')
    elsewhere.inputs.f = Path('/cluster/images.ome.zarr')
    assert local.effective_source_type() == remote.effective_source_type()  # pylint: disable=no-member


@pytest.mark.fast
@pytest.mark.parametrize('source', ['go', ['go']])
def test_a_string_is_never_a_step_input_source(source: Any) -> None:
    """A bare string is never a reference: sources are port objects."""
    with pytest.raises(InvalidInputValueError, match='str'):
        StepInput(source=source)


def _guard_and_message(record_first: bool) -> dict[str, Any]:
    """`wf.inputs.go` read by a record on an undeclared input and by a plain `string` input."""
    e1, e2 = adapter_step('echo', 'e1', message='hi'), adapter_step('echo', 'e2')
    wf = Workflow([e1, e2], 'order')
    if record_first:
        e1.inputs.go = StepInput(source=wf.inputs.go)
        e2.inputs.message = wf.inputs.go
    else:
        e2.inputs.message = wf.inputs.go
        e1.inputs.go = StepInput(source=wf.inputs.go)
    e1.when = '$(inputs.go)'
    return cast(dict[str, Any], wf.compile().cwl_workflow['inputs'])


@pytest.mark.fast
def test_a_record_on_an_undeclared_input_does_not_type_the_workflow_input() -> None:
    """The workflow input's type does not depend on which binding came first."""
    assert _guard_and_message(True)['go']['type'] == _guard_and_message(False)['go']['type'] == 'string'


#: Statements of a user's script, `# here` on the line its diagnostic, or the step it makes, must name.
_SPANNED: Final = {
    'literal': ('scale = Step.from_cwl_document(SCALE, process_name="scale")',
                'scale.inputs.n = "not an int"  # here', 'workflow = Workflow([scale], "wf")'),
    'bind': ('touch = Step(clt_path=TOUCH)', 'cat = Step(clt_path=CAT)',
             'cat.inputs.file = [touch.outputs.file]  # here'),
    'order': ('touch = Step(clt_path=TOUCH)', 'cat = Step(clt_path=CAT)  # here',
              'cat.inputs.file = touch.outputs.file', 'workflow = Workflow([cat, touch], "backwards")'),
    'made': ('step = Step(clt_path=TOUCH)  # here',),
}


def _script(kind: str, depth: int, padding: list[str]) -> str:
    lines = [*_SPANNED[kind], 'made.update(locals())']
    for _ in range(depth):
        lines = ['def build():', *(f'    {line}' for line in lines), 'build()']
    return '\n'.join([*padding, *lines]) + '\n'


@pytest.mark.fast
@given(st.sampled_from(sorted(_SPANNED)), st.integers(0, 2),
       st.lists(st.sampled_from(['', '# note', 'pass']), max_size=3),
       st.sampled_from(['<string>', '<ipython-input-3-abc>', 'build.py', str(Path(__file__).with_name('built.py'))]))
@example('literal', 1, [], __file__)  # a literal of the wrong type bound in a function is reported where it is bound
@example('literal', 0, ['import sophios'], 'build.py')  # one bound at a script's top level, at the script's line
@example('bind', 1, [], __file__)  # an error raised while binding names the bind, not a line inside Sophios
@example('order', 1, [], __file__)  # a step listed before the step it reads from, where that step was made
@example('made', 0, [''], '<string>')  # code without a file keeps its pseudo-name, not a path under the cwd
@budget(20)
def test_a_diagnostic_names_the_python_line_that_made_its_object(kind: str, depth: int, padding: list[str],
                                                                 file: str) -> None:
    """A diagnostic names the line of the user's innermost frame that bound the port or made the step, under the
    name its code was compiled with: a path resolved, a pseudo-name such as `<string>` kept."""
    source = _script(kind, depth, padding)
    line = next(number for number, text in enumerate(source.splitlines(), start=1) if text.endswith('# here'))
    made: dict[str, Any] = {}
    names = {'Step': Step, 'Workflow': Workflow, 'TOUCH': ADAPTERS / 'touch.cwl', 'CAT': ADAPTERS / 'cat.cwl',
             'made': made, 'SCALE': {'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'true',
                                     'inputs': {'n': 'int'}, 'outputs': {}}}
    named = file if file.startswith('<') else str(Path(file).resolve())
    if kind == 'made':
        exec(compile(source, file, 'exec'), names)  # pylint: disable=exec-used
        span = made['step']._span  # pylint: disable=protected-access
    else:
        with pytest.raises({'literal': SophiosError, 'bind': InvalidInputValueError,
                            'order': InvalidStepError}[kind]) as caught:
            exec(compile(source, file, 'exec'), names)  # pylint: disable=exec-used
            made['workflow'].compile()
        diagnostic = cast(SophiosError, caught.value).diagnostics[0]
        span = diagnostic.span
        assert kind != 'literal' or str(diagnostic).startswith(f'{named}:{line}:1: error [wic020]')
    assert span is not None and (span.file, span.start_line) == (named, line)


@pytest.mark.fast
def test_a_cwl_workflow_as_a_step_clt_path_is_refused_with_python_advice(tmp_path: Path) -> None:
    """A CWL Workflow given as a Step's `clt_path` is refused, naming that file and the Python fix."""
    say = tmp_path / 'say.cwl'
    write_tree(tmp_path, {
        'say.cwl': '{class: Workflow, cwlVersion: v1.2, inputs: {text: string}, '
                   'outputs: {said: {type: File, outputSource: echo/out}}, '
                   'steps: {echo: {run: echo.cwl, in: {text: text}, out: [out]}}}',
        'echo.cwl': '{class: CommandLineTool, cwlVersion: v1.2, baseCommand: echo, '
                    'inputs: {text: string}, outputs: {out: stdout}}'})
    step = Step(clt_path=say)
    step.inputs.text = 'hi'
    with pytest.raises(SophiosError) as caught:
        Workflow([step], 'wsay').compile()
    diagnostic, = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    assert diagnostic.message == (
        f"step 'say' runs {say}, a CWL Workflow given in Python as its clt_path. Sophios cannot embed "
        'a CWL Workflow as a step: build it in Python as a nested Workflow of Steps, or run the CWL '
        f'Workflow on its own with sophios --yaml {say} --allow_raw_cwl')


def _this_line() -> int:
    """The line of the caller that calls this."""
    frame = inspect.currentframe()
    assert frame is not None and frame.f_back is not None
    return frame.f_back.f_lineno


@pytest.mark.fast
def test_run_async_refuses_a_workflow_with_real_time_analysis_before_anything_runs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`api008`, at the declaration step's line; nothing is written, pulled or started."""
    touch = adapter_step('touch', filename='empty.txt')
    subinterpreter = ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl'
    declaration, declaration_line = Step(clt_path=subinterpreter), _this_line()
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    prepared = no_preflight(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SophiosError) as raised:
        asyncio.run(Workflow([touch, declaration], 'py_async').run_async())
    (diagnostic,) = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.REALTIME_NOT_ASYNC
    assert "'touch'" in diagnostic.message and 'run_async() never runs real-time analyses' in diagnostic.message
    assert diagnostic.span is not None and diagnostic.span.start_line == declaration_line
    assert Path(diagnostic.span.file) == Path(__file__).resolve()
    assert not prepared and not list(tmp_path.iterdir())
