"""Step-input default, linkMerge and pickValue lowering (design §6, Topology); their runs are cases in
tests/core/data/nextflow/runs.yaml."""

# pylint: disable=missing-function-docstring

from typing import Any

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow
from sophios.input_output_nf import render_nextflow
from sophios.ir.resolved import NotLowered
from sophios.nf_types import (
    ExecutableNextflowWorkflow, NfPort, NfProcess, NfProcessConnection, NfStepInputConnection,
    NfWorkflowInputConnection,
)
from sophios.wic_types import Yaml

from .test_channels import _lower
from .testkit import command, lowered_case, output_port, refusal_lines, step, tool

_STRINGS = {"type": "array", "items": "string"}


def _echo(name: str, port: Any) -> CommandLineTool:
    """``echo`` its one input, an array per item, into out.txt."""
    return CommandLineTool(
        name, Inputs(words=Input(port, position=1)), Outputs(line=Output(cwl.file, glob="out.txt"))
    ).base_command("echo").stdout("out.txt")


def _say(name: str, port: Any, value: Any) -> Step:
    step = Step(_echo(name, port), step_name=name)
    step.inputs.words = value
    return step


def _joined(step_input: Any, port_type: Any, inputs: dict[str, Any], *, scatter: bool = False) -> Any:
    """A source workflow whose JOIN step binds ``step_input`` to its ``words``, of ``port_type``."""
    join = tool("JOIN", inputs={"words": {"type": port_type, "inputBinding": {"position": 1}}},
                outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}, stdout="out.txt")
    fields = {"scatter": "words"} if scatter else {}
    workflow = {
        "steps": [step("JOIN", **{"in": {"words": {"wic_raw_cwl": step_input}}, "out": ["out"]}, **fields)],
        "inputs": {name: {"type": declared} for name, (declared, _) in inputs.items()},
        "outputs": {"out": {"type": "File", "outputSource": "JOIN/out"}},
    }
    return {"workflow": workflow, "tools": [join], "job": {name: value for name, (_, value) in inputs.items()}}


def _findings(case: Yaml) -> list[str]:
    """The refusals of the case's lowering, as they print, less their spans."""
    with pytest.raises(NotLowered) as error:
        lowered_case(case)
    return refusal_lines(error.value)


_A, _B = ("string", "a"), ("string", "b")
#: How a refusal of the joined step input begins.
_WORDS = "step 'JOIN': input 'words': "


@pytest.mark.fast
@pytest.mark.parametrize(("step_input", "port_type", "inputs", "finding"), [
    ({"source": ["a", "b"], "linkMerge": "merge_deep"}, _STRINGS, {"a": _A, "b": _B},
     _WORDS + "linkMerge: unsupported linkMerge 'merge_deep'"),
    ({"source": ["a", "b"], "pickValue": "any_non_null"}, "string", {"a": _A, "b": _B},
     _WORDS + "pickValue: unsupported pickValue 'any_non_null'"),
    ({"source": "a", "pickValue": "first_non_null"}, "string", {"a": _A},
     _WORDS + "pickValue picks from a list; one source with no linkMerge is not one"),
    ({"source": ["a", "b"], "default": "d"}, _STRINGS, {"a": _A, "b": _B},
     _WORDS + "default: a default never applies beside a linkMerge or a pickValue: the joined value "
     "is not null"),
    ({"default": ["d"]}, _STRINGS, {},
     _WORDS + "default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"default": 1}, "string", {},
     _WORDS + "default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"source": ["xs", "b"], "pickValue": "first_non_null"}, "string", {"xs": (_STRINGS, ["x"]), "b": _B},
     _WORDS + "'xs' is an array, and [] stands for both a null and an empty array, so pickValue "
     "cannot take it"),
    ({"source": "xs", "default": ["d"]}, _STRINGS, {"xs": (_STRINGS, ["x"])},
     _WORDS + "default: the Nextflow target supports a JSON scalar default of the tool input's type"),
    ({"source": ["xs", "b"]}, _STRINGS, {"xs": (_STRINGS, ["x"]), "b": _B},
     _WORDS + "merge_nested over the array 'xs' makes a nested array, which is not supported"),
    ({"source": ["maybe", "b"]}, _STRINGS, {"maybe": ("string?", "m"), "b": _B},
     _WORDS + "'maybe' may be null, and a joined list has no lowering of a null element; pick from "
     "it with pickValue"),
    ({"source": ["a", "b"]}, "string", {"a": _A, "b": _B},
     _WORDS + "the step input gives a list, but its tool input declares 'string'"),
    ({"source": ["a", "b"], "pickValue": "first_non_null"}, _STRINGS, {"a": _A, "b": _B},
     _WORDS + "the step input gives one value, but its tool input declares "
     "{'type': 'array', 'items': 'string'}"),
    ({"source": ["a", "b"], "pickValue": "first_non_null"}, "File", {"a": ("File?", None), "b": ("File", "b.txt")},
     "workflow 'wf': workflow: workflow input 'a' is an absent optional File?, which has no lowering; give it a value"),
    ({"source": "a", "default": "b.txt"}, "File", {"a": ("File?", "a.txt")},
     _WORDS + "default: a step-input default of a File or Directory input is not supported; give the "
     "path as a workflow input"),
], ids=["link-merge", "pick-value", "pick-one-source", "default-beside-merge", "array-default", "mistyped-default",
        "pick-over-array", "default-over-array", "nested-array", "null-element", "list-to-scalar", "one-to-list",
        "pick-over-absent-path", "path-default"])
def test_each_step_input_the_idioms_cannot_take_is_rejected_by_name(
    step_input: Any, port_type: Any, inputs: dict[str, Any], finding: str
) -> None:
    assert _findings(_joined(step_input, port_type, inputs)) == [finding]


@pytest.mark.fast
def test_a_scattered_input_takes_its_one_source_as_it_is() -> None:
    rose = _joined({"source": ["xs"], "linkMerge": "merge_flattened"}, "string", {"xs": (_STRINGS, ["x"])},
                   scatter=True)
    assert "step 'JOIN': input 'words': a scattered input takes its one source as it is" in _findings(rose)


@pytest.mark.fast
def test_a_label_is_documentation() -> None:
    plain = _joined({"source": ["a", "b"]}, _STRINGS, {"a": _A, "b": _B})
    labelled = _joined({"source": ["a", "b"], "label": "two words"}, _STRINGS, {"a": _A, "b": _B})
    assert render_nextflow(lowered_case(labelled)[0]) == render_nextflow(lowered_case(plain)[0])


@pytest.mark.fast
def test_the_idioms_lower_to_one_step_input_connection() -> None:
    lowered, _notes = lowered_case(_joined({"source": ["a", "b"]}, _STRINGS, {"a": _A, "b": _B}))
    assert NfStepInputConnection(
        (NfWorkflowInputConnection("a", "_JOIN", "words"), NfWorkflowInputConnection("b", "_JOIN", "words")),
        "merge_nested",
    ) in lowered.connections
    defaulted, _notes = lowered_case(
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
