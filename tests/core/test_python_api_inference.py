"""`Inference` configures the Python API's edge inference as the command-line flags configure the CLI's."""
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import sophios.api.python._workflow_runtime as python_runtime
from sophios import main as cli
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import (Inference, InvalidInferenceError, RunResult, RunState, SophiosError,
                                         SophiosErrorCode, Step, Workflow)
from sophios.nf_reader import read_nextflow
from sophios.nf_types import NfProcessConnection
from sophios.wic_types import Json, StepId, Tool

from .synthetic_tools import clt

RENAMINGS = (("data", "file"),)


def _workflow(consumer_input: str) -> Workflow:
    """One producer with two File outputs, `file` then `other`, and a consumer whose File input is left to inference.

    Without naming conventions the later output, `other`, feeds the consumer.
    An input called `file` pairs with the output `file`; an input called `data`
    pairs with it only through a `data` -> `file` renaming.
    """
    emit = (CommandLineTool("emit", Inputs(message=Input(cwl.string, position=1)),
                            Outputs(file=Output(cwl.file, glob="out.txt"), other=Output(cwl.file, glob="out.txt")))
            .base_command("echo").stdout("out.txt"))
    consume = (CommandLineTool("consume", Inputs(**{consumer_input: Input(cwl.file, position=1)}),
                               Outputs(result=Output(cwl.file, glob="result.txt")))
               .base_command("cat").stdout("result.txt"))
    producer = Step(emit)
    producer.inputs.message = "hello"
    return Workflow([producer, Step(consume)], "wf")


def _feeds(cwl_workflow: Json) -> list[Json]:
    """The consumer step's inputs, as wired in the compiled workflow."""
    steps = cwl_workflow["steps"]
    named = steps.items() if isinstance(steps, dict) else ((step["id"], step) for step in steps)
    return [step["in"] for name, step in named if name.endswith("consume")]


def _cli(workflow: Workflow, directory: Path, monkeypatch: pytest.MonkeyPatch, *flags: str,
         renamings: tuple[tuple[str, str], ...] = ()) -> Json:
    """The CWL the command line writes for `workflow`'s `.wic`, with `flags` and a config holding `renamings`."""
    bundle = directory / "bundle"
    bundle.mkdir()
    wic = workflow.write_wic(bundle)
    config = directory / "config.json"
    config.write_text(json.dumps({"search_paths_cwl": {"global": [str(bundle)]},
                                  "search_paths_wic": {"global": [str(bundle)]},
                                  "renaming_conventions": [list(pair) for pair in renamings]}), encoding="utf-8")
    monkeypatch.chdir(directory)
    monkeypatch.setattr("sys.argv", ["sophios", "--yaml", str(wic), "--config_file", str(config),
                                     "--generate_cwl_workflow", "--basepath", "out", *flags])
    cli.main()
    written: Json = yaml.safe_load((directory / "out" / "wf.cwl").read_text(encoding="utf-8"))
    return written


@pytest.mark.fast
@pytest.mark.parametrize("consumer_input, renamings", [("file", ()), ("data", RENAMINGS)],
                         ids=["name-pairs", "renaming-pairs"])
def test_naming_conventions_pick_the_edge_the_cli_picks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        consumer_input: str, renamings: tuple[tuple[str, str], ...]) -> None:
    """Without conventions the later output feeds the input; with them the output its name pairs with does."""
    default = _workflow(consumer_input).compile()
    chosen = _workflow(consumer_input).compile(inference=Inference(naming_conventions=True, renamings=renamings))
    from_cli = _cli(_workflow(consumer_input), tmp_path, monkeypatch, "--inference_use_naming_conventions",
                    renamings=renamings)

    assert _feeds(chosen.cwl_workflow) == _feeds(from_cli) != _feeds(default.cwl_workflow)


@pytest.mark.fast
def test_strict_refuses_a_choice_between_equals_as_the_cli_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`--inference_strict` and `Inference(strict=True)` turn the note into the same error."""
    assert [str(d) for d in _workflow("file").compile().diagnostics if "wic042" in str(d)]

    with pytest.raises(SophiosError) as raised:
        _workflow("file").compile(inference=Inference(strict=True))
    with pytest.raises(SystemExit):
        _cli(_workflow("file"), tmp_path, monkeypatch, "--inference_strict")

    assert [d.code for d in raised.value.diagnostics] == [SophiosErrorCode.INFERENCE_TIE]
    assert "error [wic042]" in capsys.readouterr().err


@pytest.mark.fast
def test_disabled_inference_leaves_the_input_unconnected_as_the_cli_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--inference_disable` and `Inference(enabled=False)` compile to the same CWL, with no edge."""
    off = _workflow("file").compile(inference=Inference(enabled=False))
    from_cli = _cli(_workflow("file"), tmp_path, monkeypatch, "--inference_disable")

    assert _feeds(off.cwl_workflow) == _feeds(from_cli) == [{}]
    assert _feeds(off.cwl_workflow) != _feeds(_workflow("file").compile().cwl_workflow)


@pytest.mark.fast
@pytest.mark.parametrize("inference", [Inference(), Inference(naming_conventions=True, renamings=RENAMINGS)])
def test_the_nextflow_target_connects_an_input_as_the_cwl_target_does(inference: Inference, tmp_path: Path) -> None:
    """`compile(target="nextflow", inference=)` and `to_nextflow(inference=)` lower the edge `compile(inference=)`
    infers."""
    workflow = _workflow("data")
    [feed] = _feeds(workflow.compile(inference=inference).cwl_workflow)
    workflow.to_nextflow(tmp_path, inference=inference)

    for lowered in (workflow.compile(target="nextflow", inference=inference), read_nextflow(tmp_path)):
        assert [f"{edge.from_process}/{edge.from_port}" for edge in lowered.connections
                if isinstance(edge, NfProcessConnection)] == [feed["data"]]


@pytest.mark.fast
@pytest.mark.parametrize("settings, fix", [
    ({"insert_steps": True, "format_rules": {"tiff": "stop"}}, "'continue' or 'break'"),
    ({"naming_conventions": True, "renamings": [("a", "b", "c")]}, "(before, after)"),
    ({"naming_conventions": True, "renamings": ["ab"]}, "(before, after)"),
    ({"naming_conventions": True, "renamings": [("a", 1)]}, "(before, after)"),
    ({"renamings": RENAMINGS}, "naming_conventions=True"),
    ({"format_rules": {"edam:format_2": "break"}}, "insert_steps=True"),
], ids=["format-rule", "triple", "string", "non-string", "renamings-without-conventions",
        "format-rules-without-insertion"])
def test_an_invalid_inference_is_refused_at_construction(settings: dict[str, Any], fix: str) -> None:
    """Each mistake is `api007`, and its message says what to write instead."""
    with pytest.raises(InvalidInferenceError) as raised:
        Inference(**settings)

    (code,) = {d.code for d in raised.value.diagnostics}
    assert code == SophiosErrorCode.INVALID_INFERENCE == "api007"
    assert fix in str(raised.value)


@pytest.mark.fast
def test_an_inference_keeps_what_it_was_given_immutably() -> None:
    """A list of renamings becomes a tuple, and the rules are a copy the caller cannot change under it."""
    rules = {"tiff": "break"}
    inference = Inference(naming_conventions=True, renamings=[("data", "file")], insert_steps=True, format_rules=rules)
    rules["tiff"] = "continue"

    assert inference.renamings == (("data", "file"),)
    assert dict(inference.format_rules) == {"tiff": "break"}
    with pytest.raises(TypeError):
        inference.format_rules["tiff"] = "continue"  # type: ignore[index]


class _Completed:  # pylint: disable=too-few-public-methods
    """A started run that has completed: for a test of what `run()` writes, not of the run."""

    def wait(self) -> RunResult:
        return RunResult(RunState.COMPLETED, {}, Path("outdir"), Path("run.log"), 0)


@pytest.mark.fast
def test_run_infers_the_edge_its_compile_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow `run()` writes for an `Inference` is the one `compile()` returns for it."""
    monkeypatch.setattr(python_runtime.preflight, "prepare", lambda *args, **kwargs: None)
    monkeypatch.setattr(python_runtime.run_local, "start", lambda request: _Completed())
    inference = Inference(naming_conventions=True)

    _workflow("file").run(basepath=str(tmp_path / "named"), inference=inference)
    _workflow("file").run(basepath=str(tmp_path / "default"))

    named = yaml.safe_load((tmp_path / "named" / "wf.cwl").read_text(encoding="utf-8"))
    default = yaml.safe_load((tmp_path / "default" / "wf.cwl").read_text(encoding="utf-8"))
    assert _feeds(named) == _feeds(_workflow("file").compile(inference=inference).cwl_workflow)
    assert _feeds(default) == _feeds(_workflow("file").compile().cwl_workflow) != _feeds(named)


def _converting_workflow(directory: Path) -> tuple[Workflow, dict[StepId, Tool]]:
    """`make` writes format A, `need_b` reads format B and `need_a` reads A again; `insert_steps_automatically_a_to_b`
    converts A to B and is passed in a registry, not a step. Inference inserts it before `need_b`."""
    a_file, b_file = {"type": "File", "format": "edam:format_1"}, {"type": "File", "format": "edam:format_2"}
    tools = {"make": ({}, {"file": a_file}), "need_b": ({"file": b_file}, {}), "need_a": ({"file": a_file}, {}),
             "insert_steps_automatically_a_to_b": ({"file": a_file}, {"file": b_file})}
    paths = {}
    for name, (inputs, outputs) in tools.items():
        paths[name] = directory / f"{name}.cwl"
        paths[name].write_text(yaml.safe_dump(clt(inputs, outputs)), encoding="utf-8")
    registry = {StepId("insert_steps_automatically_a_to_b", "global"): Tool(
        str(paths["insert_steps_automatically_a_to_b"]),
        clt(*tools["insert_steps_automatically_a_to_b"], canonical=True))}
    steps = [Step(clt_path=paths[name]) for name in ("make", "need_b", "need_a")]
    return Workflow(steps, "convert"), registry


def _step_ids(compiled: Any) -> list[str]:
    steps = compiled.cwl_workflow["steps"]
    return [step["id"] if isinstance(steps, list) else step for step in steps]


@pytest.mark.fast
def test_insert_steps_inserts_a_converter_from_the_tool_registry(tmp_path: Path) -> None:
    """With `insert_steps=True` the converter in `tool_registry=` joins the workflow; without it, or without the
    registry, `need_b`'s input is left unbound, a workflow input."""
    workflow, registry = _converting_workflow(tmp_path)

    inserted = workflow.compile(tool_registry=registry, inference=Inference(insert_steps=True))
    plain = workflow.compile(tool_registry=registry)
    unregistered = workflow.compile(inference=Inference(insert_steps=True))

    assert any("insert_steps_automatically_a_to_b" in name for name in _step_ids(inserted))
    for compiled in (plain, unregistered):
        assert not any("insert_steps_automatically" in name for name in _step_ids(compiled))
        assert any("need_b" in name for name in compiled.cwl_workflow["inputs"])
    assert not any("need_b" in name for name in inserted.cwl_workflow["inputs"])


@pytest.mark.fast
def test_format_rules_reach_the_inserted_step(tmp_path: Path) -> None:
    """`break` on format B stops `need_a`'s search at the inserted converter, so it no longer finds `make`'s
    output; the default continues past the converter and connects."""
    workflow, registry = _converting_workflow(tmp_path)

    continued = workflow.compile(tool_registry=registry, inference=Inference(insert_steps=True))
    stopped = workflow.compile(tool_registry=registry, inference=Inference(
        insert_steps=True, format_rules={"edam:format_2": "break"}))

    assert not any("need_a" in name for name in continued.cwl_workflow["inputs"])
    assert any("need_a" in name for name in stopped.cwl_workflow["inputs"])
