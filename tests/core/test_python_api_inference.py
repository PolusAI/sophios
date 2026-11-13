"""`Inference` configures the Python API's edge inference as the command-line flags configure the CLI's."""
import io
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import example, given, strategies as st

import sophios.api.python._workflow_runtime as python_runtime
from sophios import main as cli
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.input_output import SHIPPED_RENAMINGS
from sophios.api.python.workflow import (Inference, InvalidInferenceError, RunResult, RunState, SophiosError,
                                         SophiosErrorCode, Step, Workflow)
from sophios.nf_types import NfProcessConnection
from sophios.wic_types import Json

from .budgets import budget
from .helpers import search_config

RENAMINGS = (("data", "file"),)


def _workflow(consumer_input: str) -> Workflow:
    """One producer with two File outputs, `other` then `file`, and a consumer whose File input is left to inference."""
    emit = (CommandLineTool("emit", Inputs(message=Input(cwl.string, position=1)),
                            Outputs(other=Output(cwl.file, glob="out.txt"), file=Output(cwl.file, glob="out.txt")))
            .base_command("echo").stdout("out.txt"))
    consume = (CommandLineTool("consume", Inputs(**{consumer_input: Input(cwl.file, position=1)}),
                               Outputs(result=Output(cwl.file, glob="result.txt")))
               .base_command("cat").stdout("result.txt"))
    producer = Step(emit)
    producer.inputs.message = "hello"
    return Workflow([producer, Step(consume)], "wf")


def _mdrun_workflow(other_output: str) -> Workflow:
    """An mdrun-like producer with `other_output` then `output_edr_path`, both bare Files, and a consumer whose
    `input_energy_path` is left to inference."""
    mdrun = (CommandLineTool("mdrun", Inputs(message=Input(cwl.string, position=1)),
                             Outputs(**{other_output: Output(cwl.file, glob="out.txt")},
                                     output_edr_path=Output(cwl.file, glob="out.txt")))
             .base_command("echo").stdout("out.txt"))
    energy = (CommandLineTool("consume", Inputs(input_energy_path=Input(cwl.file, position=1)),
                              Outputs(result=Output(cwl.file, glob="result.txt")))
              .base_command("cat").stdout("result.txt"))
    producer = Step(mdrun)
    producer.inputs.message = "hello"
    return Workflow([producer, Step(energy)], "wf")


def _feeds(cwl_workflow: Json) -> list[Json]:
    """The consumer step's inputs, as wired in the compiled workflow."""
    steps = cwl_workflow["steps"]
    named = steps.items() if isinstance(steps, dict) else ((step["id"], step) for step in steps)
    return [step["in"] for name, step in named if name.endswith("consume")]


def _cli(workflow: Workflow, directory: Path, monkeypatch: pytest.MonkeyPatch, *flags: str,
         renamings: tuple[tuple[str, str], ...] | None = ()) -> Json:
    """The CWL the command line writes for `workflow`'s `.wic`, with `flags` and a config holding `renamings`
    (`None` leaves the key out of the config)."""
    bundle = directory / "bundle"
    bundle.mkdir()
    wic = workflow.write_wic(bundle)
    config = search_config(directory / "config.json", [bundle], [bundle], **(
        {} if renamings is None else {"renaming_conventions": [list(pair) for pair in renamings]}))
    monkeypatch.chdir(directory)
    monkeypatch.setattr("sys.argv", ["sophios", "--yaml", str(wic), "--config_file", str(config),
                                     "--generate_cwl_workflow", "--basepath", "out", *flags])
    cli.main()
    return cast(Json, yaml.safe_load((directory / "out" / "wf.cwl").read_text(encoding="utf-8")))


@pytest.mark.fast
@pytest.mark.parametrize("surface, renamings, edge", [
    ("python", None, "output_edr_path"), ("python", (), "output_log_path"),
    ("cli", None, "output_edr_path"), ("cli", (), "output_log_path"),
], ids=["python-ships", "python-empty-is-none", "cli-key-missing-ships", "cli-empty-list-is-none"])
def test_input_energy_path_reads_the_output_its_renamings_pair_it_with(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str,
        renamings: tuple[tuple[str, str], ...] | None, edge: str) -> None:
    """Both surfaces start from the shipped renamings when none are given (`None`: no argument, no config key), so
    `energy_` pairs with `edr_` and `input_energy_path` reads `output_edr_path`, not the output declared first, a log.
    A list given is the whole list, so an empty one leaves the output declared first."""
    workflow = _mdrun_workflow("output_log_path")
    compiled = workflow.compile() if renamings is None else workflow.compile(inference=Inference(renamings=renamings))
    cwl = _cli(workflow, tmp_path, monkeypatch, renamings=renamings) if surface == "cli" else compiled.cwl_workflow
    [feed] = _feeds(cwl)
    assert feed["input_energy_path"] == f"mdrun/{edge}"


class _Completed:  # pylint: disable=too-few-public-methods
    """A started run that has completed: for a test of what `run()` writes, not of the run."""

    def collect(self) -> RunResult:
        return RunResult(RunState.COMPLETED, {}, Path("outdir"), Path("run.log"), 0)


def _documented(consumer: str, inference: Inference) -> tuple[str | None, bool]:
    """What docs/edge-inference.md says feeds `consumer`, and whether a tie: the File declared first, unless the naming
    conventions pair one output with the input's name, `input_` stripped and the renamings applied; none when off."""
    if not inference.enabled:
        return None, False
    wanted = consumer.removeprefix("input_")
    for before, after in inference.renamings or ():
        wanted = after if wanted == before else wanted
    paired = [output for output in ("file", "other") if inference.naming_conventions and output == wanted]
    return (paired[0], False) if len(paired) == 1 else ("other", True)


#: Each `before` is a whole name after `input_` or in none, so the docs' wording decides.
_CONSUMERS = ("file", "other", "data", "input_file", "input_data", "summary")
_RENAMINGS = (("data", "file"), ("data", "other"), ("summary", "file"))


@pytest.mark.fast
@given(st.sampled_from(_CONSUMERS), st.booleans(), st.booleans(),
       st.lists(st.sampled_from(_RENAMINGS), unique=True, max_size=2), st.booleans())
@example("file", True, False, [], True)  # an input called `file` pairs with the output `file`
@example("data", True, False, list(RENAMINGS), True)  # `data` pairs with it only through a renaming
@example("file", True, True, [], False)  # strict turns the tie note into the same error
@example("file", False, False, [], False)  # disabled leaves the input unconnected
@example("data", True, False, [], False)  # with the conventions off the first output, `other`, feeds the input
@example("input_data", True, False, [("data", "file")], True)  # renamed once `input_` is stripped
@budget(8)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_each_door_connects_the_input_the_docs_say(consumer: str, enabled: bool, strict: bool,
                                                   renamings: list[tuple[str, str]], naming: bool) -> None:
    """`compile()`, its Nextflow target, `run()` and the command line with the matching flags connect what
    `_documented` says, a tie noted as `wic043`, or refused as it in every door when strict. The naming conventions
    are on unless switched off."""
    inference = (Inference(enabled=enabled, strict=strict, renamings=renamings) if naming
                 else Inference(enabled=enabled, strict=strict, naming_conventions=False))
    flags = [flag for flag, on in (("--inference_disable", not enabled), ("--inference_strict", strict),
                                   ("--no_inference_naming_conventions", not naming)) if on]
    configured = tuple(inference.renamings or ())
    feed, tie = _documented(consumer, inference)
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(python_runtime.preflight, "prepare", lambda *args, **kwargs: None)
        monkeypatch.setattr(python_runtime.run_local, "start", lambda request: _Completed())
        if tie and strict:
            for door in (lambda: _workflow(consumer).compile(inference=inference),
                         lambda: _workflow(consumer).run(basepath=str(Path(directory, "run")), inference=inference)):
                with pytest.raises(SophiosError) as raised:
                    door()
                assert [d.code for d in raised.value.diagnostics] == [SophiosErrorCode.INFERENCE_GUESS]
            with redirect_stderr(io.StringIO()) as err, pytest.raises(SystemExit):
                _cli(_workflow(consumer), Path(directory), monkeypatch, *flags, renamings=configured)
            assert "error [wic043]" in err.getvalue()
            return
        compiled = _workflow(consumer).compile(inference=inference)
        _workflow(consumer).run(basepath=str(Path(directory, "run")), inference=inference)
        ran = yaml.safe_load(Path(directory, "run", "wf.cwl").read_text(encoding="utf-8"))
        from_cli = _cli(_workflow(consumer), Path(directory), monkeypatch, *flags, renamings=configured)
    wired = [{consumer: f"emit/{feed}"} if feed else {}]
    assert _feeds(compiled.cwl_workflow) == _feeds(ran) == _feeds(from_cli) == wired
    assert ("wic043" in "".join(map(str, compiled.diagnostics))) == tie
    if feed is None:  # an input left unconnected the Nextflow target refuses
        with pytest.raises(ValueError, match=f"consume.{consumer} is not connected"):
            _workflow(consumer).compile(target="nextflow", inference=inference)
        return
    lowered = _workflow(consumer).compile(target="nextflow", inference=inference).nextflow_workflow
    assert [f"{edge.from_process}/{edge.from_port}" for edge in lowered.connections
            if isinstance(edge, NfProcessConnection)] == [f"emit/{feed}"]


_PAIR = st.tuples(st.text(), st.text()) | st.lists(st.text(), min_size=2, max_size=2)
_NOT_A_PAIR = (st.text() | st.tuples(st.text()) | st.tuples(st.text(), st.text(), st.text())
               | st.tuples(st.text(), st.integers()))
_RULE = st.sampled_from(["continue", "break"])
_NOT_A_RULE = st.tuples(st.integers(), _RULE) | st.tuples(st.text(), st.text().filter(
    lambda text: text not in ("continue", "break")) | st.none())
_FIXES = ("(before, after)", "naming_conventions=True", "insert_steps=True", "'continue' or 'break'")


@st.composite
def _settings(draw: st.DrawFn) -> tuple[dict[str, Any], set[str]]:
    """Settings for `Inference` breaking the docstring's rules drawn, each named by the fix its refusal names."""
    broken = draw(st.sets(st.sampled_from(_FIXES), max_size=2))
    renamings = draw(st.permutations(draw(st.lists(_PAIR, max_size=2)) + draw(
        st.lists(_NOT_A_PAIR, min_size=1, max_size=1) if "(before, after)" in broken else st.just([]))))
    renamings = renamings or ([draw(_PAIR)] if "naming_conventions=True" in broken else [])
    rules = dict(draw(st.lists(st.tuples(st.text(), _RULE), max_size=2)) + draw(
        st.lists(_NOT_A_RULE, min_size=1, max_size=1) if "'continue' or 'break'" in broken else st.just([])))
    rules = rules or ({draw(st.text()): draw(_RULE)} if "insert_steps=True" in broken else {})
    settings: dict[str, Any] = draw(st.fixed_dictionaries({}, optional=dict.fromkeys(("enabled", "strict"),
                                                                                     st.booleans())))
    for key, given_values, fix in (("renamings", renamings, "naming_conventions=True"),
                                   ("format_rules", rules, "insert_steps=True")):
        switch = fix.removesuffix("=True")
        settings[switch] = fix not in broken and (bool(given_values) or draw(st.booleans()))
        if given_values:
            settings[key] = given_values
    return settings, broken


@pytest.mark.fast
@given(_settings())
@example(({"insert_steps": True, "format_rules": {"tiff": "stop"}}, {"'continue' or 'break'"}))
@example(({"naming_conventions": True, "renamings": [("a", "b", "c")]}, {"(before, after)"}))
@example(({"naming_conventions": True, "renamings": ["ab"]}, {"(before, after)"}))
@example(({"naming_conventions": True, "renamings": [("a", 1)]}, {"(before, after)"}))
@example(({"naming_conventions": False, "renamings": RENAMINGS}, {"naming_conventions=True"}))
@example(({"format_rules": {"edam:format_2": "break"}}, {"insert_steps=True"}))
@example(({"insert_steps": True, "format_rules": {2: "break"}}, {"'continue' or 'break'"}))  # a format is a string
# A list of renamings becomes a tuple, and the rules are a copy the caller cannot change under it.
@example(({"renamings": [("data", "file")], "insert_steps": True, "format_rules": {"tiff": "break"}}, set()))
@example(({}, set()))  # renamings start from the shipped ones
@example(({"naming_conventions": False}, set()))  # which conventions off leave unused, not an error
@budget(40)
def test_an_inference_is_refused_at_construction_or_keeps_what_it_was_given(
        drawn: tuple[dict[str, Any], set[str]]) -> None:
    """Settings breaking a rule are `api007`, saying what to write instead; others are kept, renamings as tuples, the
    shipped ones unless given or the conventions are off, and the format rules as a copy the caller cannot change. A
    copy that switches the conventions keeps the renamings nobody gave as the shipped ones."""
    settings, broken = drawn
    if broken:
        with pytest.raises(InvalidInferenceError) as raised:
            Inference(**settings)
        assert {d.code for d in raised.value.diagnostics} == {SophiosErrorCode.INVALID_INFERENCE} == {"api007"}
        assert any(fix in str(raised.value) for fix in broken), str(raised.value)
        return
    inference = Inference(**settings)
    rules = settings.get("format_rules", {})
    given_rules = dict(rules)
    rules.clear()
    given_renamings = settings.get("renamings")
    assert inference.renamings == (None if given_renamings is None else tuple(tuple(pair) for pair in given_renamings))
    shipped = SHIPPED_RENAMINGS if settings.get("naming_conventions", True) else ()
    expected = given_renamings if given_renamings is not None else shipped
    assert python_runtime.compile_settings(inference).inference.renaming_conventions == tuple(map(tuple, expected))
    assert dict(inference.format_rules) == given_rules
    with pytest.raises(TypeError):
        inference.format_rules["tiff"] = "continue"  # type: ignore[index]
    if given_renamings is None:  # a copy switching the conventions neither raises nor drops the shipped renamings
        for switched in (True, False):
            copy = replace(inference, naming_conventions=switched)
            assert python_runtime.compile_settings(copy).inference.renaming_conventions == (
                SHIPPED_RENAMINGS if switched else ())
