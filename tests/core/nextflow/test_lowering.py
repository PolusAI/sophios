"""Semantic lowering: supported CWL source becomes typed executable IR, as CWL reads it (design §6). What a lowered
workflow does is the example workflows'; here, laws of how a binding's text is read."""

# pylint: disable=missing-function-docstring

from typing import Any

from cwl_utils.expression import interpolate
from hypothesis import example, given, strategies as st
import pytest

from sophios.api.python.workflow import Workflow
from sophios.nf_types import NfFlag

from ..budgets import budget
from .testkit import lowered_case, one_step, tool


@pytest.mark.fast
@budget(30)
@given(st.sampled_from([{"prefix": "--verbose"}, {}, {"separate": True}]),
       st.sampled_from([None, "$(inputs.verbose)"]),
       st.sampled_from([("boolean", True), ("boolean", False), (["null", "boolean"], True),
                        (["null", "boolean"], False), (["null", "boolean"], None), ("boolean?", None)]))
@example({"prefix": "--verbose"}, "$(inputs.verbose)", (["null", "boolean"], None))
@example({"separate": True}, None, ("boolean", False))
def test_a_boolean_binding_is_a_flag_token_exactly_when_it_has_a_prefix(
        binding: dict[str, Any], value_from: str | None, typed: tuple[Any, bool | None]) -> None:
    """A flag tests its value and never dereferences it, so false and an absent optional lower alike; a valueFrom
    restating its own input is the same flag, and cwltool accepts separate: true without a prefix."""
    declared, value = typed
    input_binding = {"position": 1, **binding, **({} if value_from is None else {"valueFrom": value_from})}
    flag_tool = tool("FLAGS", inputs={"verbose": {"type": declared, "inputBinding": input_binding}})
    workflow = lowered_case(one_step(flag_tool, verbose=value))[0]
    assert workflow.params == {"verbose": [] if value is None else value}
    flags = (NfFlag("verbose", "--verbose"),) if binding.get("prefix") else ()
    assert workflow.processes[0].command.tokens[1:] == flags


# design §6, Commands: a `$` before no `(` or `{` is text; the last two were once refused beside a reference, and
# an empty argument is kept.
@budget(200)
@given(st.lists(st.text("$ \n\ta5HOME)}{(\\\"'é").filter(lambda t: "$(" not in t and "${" not in t),
                min_size=1, max_size=3))
@example(["$", "cost $5", "$HOME", "a $ b", "$$", ' mv a "$1" ', "trailing $\n", "$)", "$}", "{$", "($", ""])
def test_a_dollar_starting_no_expression_is_text_as_cwltool_reads_it(texts: list[str]) -> None:
    for text in texts:  # cwltool passes a text with no expression through, and reads one beside a reference
        workflow = lowered_case(one_step(tool("ARGS", inputs={"x": {"type": "string"}}, baseCommand="echo", arguments=[
            text, {"valueFrom": text}, (joined := f"{text}$(inputs.x){text}")]), x="v"))[0]
        _, *bare, beside = workflow.to_dict()["processes"][0]["command"]["tokens"]
        assert bare == [{"segments": [{"kind": "literal", "value": text}]}] * 2
        assert "".join(p.get("value", "V") for p in beside["segments"]) == interpolate(joined, {"inputs": {"x": "V"}})


@pytest.mark.fast
def test_a_call_of_an_empty_workflow_lowers_to_no_process() -> None:
    """A call is inlined, so a call of a workflow with no step leaves nothing to run, and nothing to note."""
    assert Workflow([Workflow([], "child")], "root").compile(target="nextflow").nextflow_workflow.processes == ()
