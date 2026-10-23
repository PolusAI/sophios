"""Step-input default, linkMerge and pickValue under installed, pinned Nextflow.

design_docs/sophios_nextflow_backend.md §6, Topology, Step inputs: each is one
fixed Groovy idiom over the step input's values, compared here with what CWL
gives.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, get_args

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow
from sophios.input_output_nf import render_nextflow
from sophios.nf_types import (
    ExecutableNextflowWorkflow, NfPort, NfProcess, NfProcessConnection, NfStepInputConnection,
    NfWorkflowInputConnection,
)
from sophios.utils_nf import compiled_source_to_nextflow

from .test_capabilities import _findings
from .test_channels import _lower, _run
from .testkit import command, output_port, step, synthetic_source, tool, workflow_doc

_STRINGS = {"type": "array", "items": "string"}


def _echo(name: str, port: Any) -> CommandLineTool:
    """``echo`` its one input, an array per item, into out.txt."""
    return CommandLineTool(
        name, Inputs(words=Input(port, position=1)), Outputs(line=Output(cwl.file, glob="out.txt"))
    ).base_command("echo").stdout("out.txt")


def _word(name: str, text: str) -> Step:
    """A step whose text output is ``text``."""
    tool = CommandLineTool(
        name, Inputs(text=Input(cwl.string, position=1)),
        Outputs(said=Output(cwl.string, glob="said.txt", load_contents=True, output_eval="$(self[0].contents)")),
    ).base_command("printf").stdout("said.txt")
    step = Step(tool, step_name=name)
    step.inputs.text = text
    return step


def _maybe(name: str, text: str, *, runs: bool) -> Step:
    """A conditional step whose text output is ``text`` when it runs, and null when it does not."""
    tool = CommandLineTool(
        name, Inputs(text=Input(cwl.string, position=1), go=Input(cwl.boolean)),
        Outputs(said=Output(cwl.string, glob="said.txt", load_contents=True, output_eval="$(self[0].contents)")),
    ).base_command("printf").stdout("said.txt")
    step = Step(tool, step_name=name)
    step.inputs.text, step.inputs.go = text, runs
    step.when = "$(inputs.go)"
    return step


def _say(name: str, port: Any, value: Any) -> Step:
    step = Step(_echo(name, port), step_name=name)
    step.inputs.words = value
    return step


@pytest.mark.nextflow
@pytest.mark.serial
def test_link_merge_joins_the_sources_in_order(tmp_path: Path) -> None:
    # Every file is named out.txt: each joined file is staged apart, as a gathered one is.
    write_tool = CommandLineTool(
        "write", Inputs(name=Input(cwl.string, position=1)), Outputs(file=Output(cwl.file, glob="out.txt"))
    ).base_command("echo").stdout("out.txt")
    each, one = Step(write_tool, step_name="each"), Step(write_tool, step_name="one")
    each.inputs.name = ["a", "b"]
    each.scatter_on(each.inputs.name)
    one.inputs.name = "c"
    cat = Step(CommandLineTool(
        "cat_all", Inputs(files=Input(cwl.array(cwl.file), position=1)), Outputs(line=Output(cwl.file, glob="all.txt"))
    ).base_command("cat").stdout("all.txt"), step_name="cat_all")
    cat.inputs.files = StepInput(source=[each.outputs.file, one.outputs.file], link_merge="merge_flattened")
    flow = Workflow([each, one, cat], "merged")
    nested = _say("nested", cwl.array(cwl.string), StepInput(
        source=[flow.inputs.x.as_type("string"), flow.inputs.y.as_type("string")]
    ))
    single = _say("single", cwl.array(cwl.string), StepInput(
        source=[flow.inputs.x.as_type("string")], link_merge="merge_nested"
    ))
    flow.steps.extend([nested, single])
    flow.outputs.flattened, flow.outputs.nested, flow.outputs.single = (
        cat.outputs.line, nested.outputs.line, single.outputs.line
    )
    result, observed = _run(flow, tmp_path, params={"x": "x", "y": "y z"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["flattened"] == ["a\nb\nc\n"]
    assert observed["nested"] == ["x y z\n"]
    assert observed["single"] == ["x\n"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_pick_value_picks_from_the_joined_sources(tmp_path: Path) -> None:
    skipped, ran = _maybe("skipped", "never", runs=False), _maybe("ran", "only", runs=True)
    flow = Workflow([skipped, ran], "picked")
    absent, present = flow.inputs.absent.as_type("string?"), flow.inputs.present.as_type("string?")
    first = _say("first", cwl.string, StepInput(
        source=[absent, skipped.outputs.said, present], pick_value="first_non_null"
    ))
    only = _say("only", cwl.string, StepInput(
        source=[skipped.outputs.said, ran.outputs.said], pick_value="the_only_non_null"
    ))
    every = _say("every", cwl.array(cwl.string), StepInput(
        source=[present, skipped.outputs.said, ran.outputs.said, absent], pick_value="all_non_null"
    ))
    flow.steps.extend([first, only, every])
    flow.outputs.first, flow.outputs.only = first.outputs.line, only.outputs.line
    flow.outputs.every = every.outputs.line
    result, observed = _run(flow, tmp_path, params={"absent": None, "present": "x"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: observed[name] for name in ("first", "only", "every")} == {
        "first": ["x\n"], "only": ["only\n"], "every": ["x only\n"],
    }


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(("pick", "values", "found"), [
    ("first_non_null", [None, None], 0), ("the_only_non_null", [None, None], 0), ("the_only_non_null", ["x", "y"], 2),
])
def test_a_pick_value_that_finds_no_one_value_stops_the_run(
    pick: str, values: list[str | None], found: int, tmp_path: Path
) -> None:
    flow = Workflow([], "picked")
    step = _say("pick", cwl.string, StepInput(
        source=[flow.inputs.a.as_type("string?"), flow.inputs.b.as_type("string?")], pick_value=pick
    ))
    flow.steps.append(step)
    flow.outputs.line = step.outputs.line
    result, _ = _run(flow, tmp_path, params=dict(zip("ab", values)))
    assert result.returncode != 0
    assert f"Sophios step input pick.words: {pick} finds {found} values that are not null" in (
        result.stdout + result.stderr
    )
    assert not list((tmp_path / "work").rglob("out.txt"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_default_is_the_value_with_no_source_and_takes_the_place_of_a_null_one(tmp_path: Path) -> None:
    skipped = _maybe("skipped", "never", runs=False)
    flow = Workflow([skipped], "defaulted")
    unsourced = _say("unsourced", cwl.string, StepInput(default="d1"))
    absent = _say("absent", cwl.string, StepInput(source=flow.inputs.absent.as_type("string?"), default="d2"))
    present = _say("present", cwl.string, StepInput(source=flow.inputs.given.as_type("string?"), default="d3"))
    after_skip = _say("after_skip", cwl.string, StepInput(source=skipped.outputs.said, default="d4"))
    flow.steps.extend([unsourced, absent, present, after_skip])
    flow.outputs.unsourced, flow.outputs.absent, flow.outputs.present, flow.outputs.after_skip = (
        unsourced.outputs.line, absent.outputs.line, present.outputs.line, after_skip.outputs.line
    )
    result, observed = _run(flow, tmp_path, params={"absent": None, "given": "g"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: observed[name] for name in ("unsourced", "absent", "present", "after_skip")} == {
        "unsourced": ["d1\n"], "absent": ["d2\n"], "present": ["g\n"], "after_skip": ["d4\n"],
    }


def _joined(step_input: Any, port_type: Any, inputs: dict[str, Any], *, scatter: bool = False) -> Any:
    """A source workflow whose JOIN step binds ``step_input`` to its ``words``, of ``port_type``."""
    join = tool("JOIN", inputs={"words": {"type": port_type, "inputBinding": {"position": 1}}},
                outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}, stdout="out.txt")
    fields = {"scatter": "words"} if scatter else {}
    return synthetic_source(
        workflow_doc([step("JOIN", **{"in": {"words": step_input}, "out": ["out"]}, **fields)],
                     inputs={name: {"type": declared} for name, (declared, _) in inputs.items()},
                     outputs={"out": {"type": "File", "outputSource": "JOIN/out"}}),
        [join],
        workflow_inputs={name: value for name, (_, value) in inputs.items()},
    )


_A, _B = ("string", "a"), ("string", "b")


@pytest.mark.fast
@pytest.mark.parametrize(("step_input", "port_type", "inputs", "finding"), [
    ({"source": ["a", "b"], "linkMerge": "merge_deep"}, _STRINGS, {"a": _A, "b": _B},
     "steps[0].in.words.linkMerge: unsupported linkMerge 'merge_deep'"),
    ({"source": ["a", "b"], "pickValue": "any_non_null"}, "string", {"a": _A, "b": _B},
     "steps[0].in.words.pickValue: unsupported pickValue 'any_non_null'"),
    ({"source": "a", "pickValue": "first_non_null"}, "string", {"a": _A},
     "steps[0].in.words.pickValue: pickValue picks from a list; one source with no linkMerge is not one"),
    ({"source": ["a", "b"], "default": "d"}, _STRINGS, {"a": _A, "b": _B},
     "steps[0].in.words.default: a default never applies beside a linkMerge or a pickValue: the joined value "
     "is not null"),
    ({"default": ["d"]}, _STRINGS, {},
     "steps[0].in.words.default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"default": 1}, "string", {},
     "steps[0].in.words.default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"source": ["xs", "b"], "pickValue": "first_non_null"}, "string", {"xs": (_STRINGS, ["x"]), "b": _B},
     "steps[0].in.words: 'xs' is an array, and [] stands for both a null and an empty array, so pickValue "
     "cannot take it"),
    ({"source": "xs", "default": ["d"]}, _STRINGS, {"xs": (_STRINGS, ["x"])},
     "steps[0].in.words.default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"source": ["xs", "b"]}, _STRINGS, {"xs": (_STRINGS, ["x"]), "b": _B},
     "steps[0].in.words: merge_nested over the array 'xs' makes a nested array, which is not supported"),
    ({"source": ["maybe", "b"]}, _STRINGS, {"maybe": ("string?", "m"), "b": _B},
     "steps[0].in.words: 'maybe' may be null, and a joined list has no lowering of a null element; pick from "
     "it with pickValue"),
    ({"source": ["a", "b"]}, "string", {"a": _A, "b": _B},
     "steps[0].in.words: the step input gives a list, but its tool input declares 'string'"),
    ({"source": ["a", "b"], "pickValue": "first_non_null"}, _STRINGS, {"a": _A, "b": _B},
     "steps[0].in.words: the step input gives one value, but its tool input declares "
     "{'type': 'array', 'items': 'string'}"),
    ({"source": ["a", "b"], "pickValue": "first_non_null"}, "File", {"a": ("File?", None), "b": ("File", "b.txt")},
     "workflow: workflow input 'a' is an absent optional File?, which has no lowering; give it a value"),
    ({"source": "a", "default": "b.txt"}, "File", {"a": ("File?", "a.txt")},
     "steps[0].in.words.default: a step-input default of a File or Directory input is not supported; give the "
     "path as a workflow input"),
], ids=["link-merge", "pick-value", "pick-one-source", "default-beside-merge", "array-default", "mistyped-default",
        "pick-over-array", "default-over-array", "nested-array", "null-element", "list-to-scalar", "one-to-list",
        "pick-over-absent-path", "path-default"])
def test_each_step_input_the_idioms_cannot_take_is_rejected_by_name(
    step_input: Any, port_type: Any, inputs: dict[str, Any], finding: str
) -> None:
    assert _findings(_joined(step_input, port_type, inputs)) == [finding]


@pytest.mark.fast
def test_a_step_inputs_producer_is_called_before_it_whatever_the_step_order() -> None:
    produce = tool("P", inputs={"text": {"type": "string", "inputBinding": {"position": 1}}},
                   outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}, stdout="out.txt")
    join = tool("JOIN", inputs={"files": {"type": {"type": "array", "items": "File"}, "inputBinding": {"position": 1}}},
                outputs={"out": {"type": "File", "outputBinding": {"glob": "all.txt"}}}, stdout="all.txt")
    rendered = render_nextflow(compiled_source_to_nextflow(synthetic_source(
        workflow_doc([step("JOIN", **{"in": {"files": {"source": ["P/out"], "linkMerge": "merge_flattened"}},
                                      "out": ["out"]}),
                      step("P", **{"in": {"text": "a"}, "out": ["out"]})],
                     inputs={"a": {"type": "string"}}, outputs={"out": {"type": "File", "outputSource": "JOIN/out"}}),
        [join, produce], workflow_inputs={"a": "a"},
    )))
    assert rendered.index("    _P(") < rendered.index("    _JOIN(")


@pytest.mark.fast
def test_a_scattered_input_takes_its_one_source_as_it_is() -> None:
    rose = _joined({"source": ["xs"], "linkMerge": "merge_flattened"}, "string", {"xs": (_STRINGS, ["x"])},
                   scatter=True)
    assert "steps[0].in.words: a scattered input takes its one source as it is" in _findings(rose)


@pytest.mark.fast
def test_a_label_is_documentation() -> None:
    plain = _joined({"source": ["a", "b"]}, _STRINGS, {"a": _A, "b": _B})
    labelled = _joined({"source": ["a", "b"], "label": "two words"}, _STRINGS, {"a": _A, "b": _B})
    assert render_nextflow(compiled_source_to_nextflow(labelled)) == render_nextflow(compiled_source_to_nextflow(plain))


@pytest.mark.fast
def test_the_idioms_lower_to_one_step_input_connection() -> None:
    lowered = compiled_source_to_nextflow(_joined({"source": ["a", "b"]}, _STRINGS, {"a": _A, "b": _B}))
    assert NfStepInputConnection(
        (NfWorkflowInputConnection("a", "_JOIN", "words"), NfWorkflowInputConnection("b", "_JOIN", "words")),
        "merge_nested",
    ) in lowered.connections
    defaulted = compiled_source_to_nextflow(
        _joined({"source": "maybe", "default": "d"}, "string", {"maybe": ("string?", None)})
    )
    assert defaulted.params == {"maybe": [], "_JOIN_words": "d"}
    sources = (NfWorkflowInputConnection("maybe", "_JOIN", "words"),
               NfWorkflowInputConnection("_JOIN_words", "_JOIN", "words"))
    assert NfStepInputConnection(sources, "merge_nested", "first_non_null") in defaulted.connections


def _merged(*sources: Any, link_merge: str = "merge_flattened", pick_value: str | None = None,
            port: NfPort | None = None) -> ExecutableNextflowWorkflow:
    """PAIR gathers into, and A and the parameter xs join at, JOIN's input."""
    pair = NfProcess("PAIR", [NfPort("item", "val")], [output_port("f", "f.txt")], command("true"))
    produce = NfProcess("A", [], [output_port("f", "a.txt")], command("true"))
    join = NfProcess("JOIN", [port or NfPort("files", "path", is_array=True)], [], command("true"))
    return ExecutableNextflowWorkflow(
        "WF", [pair, produce, join],
        [NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
         NfStepInputConnection(sources, link_merge, pick_value)],
        {"items": ["x"], "xs": ["x.txt"], "one": "o.txt"}, {"items": "string[]", "xs": "File[]", "one": "File"},
    )


_GATHERED = NfProcessConnection("PAIR", "f", "JOIN", "files", "gather")
_ONE = NfProcessConnection("A", "f", "JOIN", "files")
_XS = NfWorkflowInputConnection("xs", "JOIN", "files")


@pytest.mark.fast
@pytest.mark.parametrize(("build", "message"), [
    (lambda: _merged(_ONE, pick_value="first_non_null"), "gives one value to a port that is array-typed"),
    (lambda: _merged(_ONE, link_merge="merge_nested", port=NfPort("files", "path")),
     "gives a list to a port that is not array-typed"),
    (lambda: _merged(_GATHERED, _ONE, link_merge="merge_nested"),
     "an array source joins only by merge_flattened with no pickValue"),
    (lambda: _merged(_XS, _ONE, pick_value="all_non_null"),
     "an array source joins only by merge_flattened with no pickValue"),
    (lambda: _merged(NfWorkflowInputConnection("items", "JOIN", "files")),
     "workflow input 'items' is val\\[\\], which its port's path cannot take"),
    (lambda: _merged(_ONE, NfProcessConnection("A", "f", "PAIR", "item")), "must all reach one process input"),
    (lambda: _merged(NfWorkflowInputConnection("xs", "JOIN", "files", "dotproduct")),
     "a step input's sources cannot be scattered"),
    (lambda: _merged(_ONE, link_merge="merge_deep"), "linkMerge must be one of"),
])
def test_a_step_input_connection_keeps_the_idioms_invariants(build: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


@pytest.mark.fast
def test_a_step_input_connection_renders_as_one_helper_call() -> None:
    assert "__sophios_step_input_9f72e('JOIN.files', __values, [true, false, true], 'merge_flattened', null)" in (
        render_nextflow(_merged(_GATHERED, _ONE, _XS))
    )


@pytest.mark.fast
def test_a_nested_steps_record_takes_its_sources_through_the_workflow_that_calls_it() -> None:
    """Each source of a record on a step two workflows down resolves to what feeds it, in the order written."""
    inner = _say("inner", cwl.array(cwl.string), None)
    middle = Workflow([inner], "middle")
    inner.inputs.words = StepInput(
        source=[middle.inputs.y.as_type("string"), middle.inputs.x.as_type("string")], link_merge="merge_nested"
    )
    outer = Workflow([middle], "outer")
    middle.inputs.x, middle.inputs.y = outer.inputs.x.as_type("string"), "y"
    outer.inputs.x = "x"
    lowered = _lower(Workflow([outer], "root"))
    joined = next(connection for connection in lowered.connections if isinstance(connection, NfStepInputConnection))
    assert [lowered.params[source.from_port] for source in joined.sources] == ["y", "x"]


@pytest.mark.fast
def test_every_connection_kind_is_nameable_through_the_public_module() -> None:
    """A caller cannot match a connection it gets back unless it can name its kind."""
    from sophios.api.python import nextflow  # pylint: disable=import-outside-toplevel

    for kind in get_args(nextflow.NfConnection):
        assert getattr(nextflow, kind.__name__) is kind and kind.__name__ in nextflow.__all__
