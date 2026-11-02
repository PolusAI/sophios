"""The safe JavaScript subset, admitted only in a step's when (design §6, Expressions)."""

# pylint: disable=missing-function-docstring

from dataclasses import replace
import re
from typing import Any

import pytest

from sophios.nf_expr import Expr, check, parse, render_groovy
from sophios.nf_types import ExecutableNextflowWorkflow, NfTemplate
from sophios.utils_nf import compiled_source_to_nextflow

from .test_conditions import _when_workflow
from .testkit import step, synthetic_source, tool, workflow_doc

TYPES = {
    "a": "int", "b": "float", "s": "string", "flag": "boolean", "maybe": ["null", "int"], "n": "long", "d": "double",
}


def _calc_workflow(arguments: list[Any], params: dict[str, Any], **inputs: Any) -> ExecutableNextflowWorkflow:
    declared = {
        name: cwl_type if isinstance(cwl_type, dict) else {"type": cwl_type}
        for name, cwl_type in inputs.items()
    }
    calc = tool(
        "CALC",
        inputs=declared,
        arguments=arguments,
        baseCommand="echo",
        stdout="out.txt",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    workflow = workflow_doc(
        [step("CALC", **{"in": {name: name for name in inputs}, "out": ["out"]})],
        inputs={name: definition["type"] for name, definition in declared.items()},
        outputs={"out": {"type": "File", "outputSource": "CALC/out"}},
    )
    return compiled_source_to_nextflow(synthetic_source(workflow, [calc], workflow_inputs=params))


@pytest.mark.fast
@pytest.mark.parametrize(("text", "expected"), [
    ("$(1 + 2 * 3)", "number"),
    ("$((inputs.a + 1) / 4 % 2)", "number"),
    ("$(-inputs.b + +1e3)", "number"),
    ("$(Math.min(inputs.a, inputs.b, 3))", "number"),
    ("$(Math.round(Math.pow(inputs.b, 2)))", "number"),
    ("$(inputs.a > 1 && !inputs.flag || inputs.s === 'x')", "boolean"),
    ("$(inputs.maybe === null)", "boolean"),
    ("$(inputs.a == 2.0)", "boolean"),
])
def test_admitted_expressions_type_check(text: str, expected: str) -> None:
    assert check(parse(text), TYPES) == expected


@pytest.mark.fast
@pytest.mark.parametrize(("text", "message"), [
    ("$(inputs.a ** 2)", "unsupported construct '**'"),
    ("$(--inputs.a)", "unsupported construct '--'"),
    ("$(1 ++ 2)", "unsupported construct '++'"),
    ("$(inputs.a ? 1 : 2)", "unsupported construct '?'"),
    ("$(inputs.a & 1)", "unsupported construct '&'"),
    ("$(Math.PI)", "unsupported construct 'Math.PI'"),
    ("$(inputs.s.length)", "unsupported construct 'inputs.s.length'"),
    ("$(self)", "unsupported construct 'self'"),
    ("$(runtime.cores)", "unsupported construct 'runtime.cores'"),
    ("$(inputs.s + 1)", "+ requires number operands, not string"),
    ("$(inputs.a == 'x')", "== requires two operands of the same type, not number and string"),
    ("$(inputs.maybe + 1)", "optional inputs.maybe may only be compared with null"),
    ("$(inputs.a === null)", "=== with null requires an optional input on the other side"),
    ("$(!inputs.a)", "! requires boolean operands, not number"),
    ("$(Math.pow(1))", "Math.pow takes 2 arguments"),
    ("$(Math.min(1))", "Math.min takes 2 or more arguments"),
    ("$(1e999)", "number literal 1e999 is not finite"),
    ("$(010 + 1)", "number literal '010' is not a strict-mode decimal"),
    ("$(08)", "number literal '08' is not a strict-mode decimal"),
    ("$(\u0663)", "unsupported character"),
    ("$(inputs.nope)", "inputs.nope is not an input of this tool"),
    ("$(inputs.n > 1)", "inputs.n has type 'long'; only scalar inputs are admitted"),
    ("$(inputs.d > 1)", "inputs.d has type 'double'; only scalar inputs are admitted"),
    ("$(\"a\\\"b\")", "unsupported character"),
])
def test_constructs_outside_the_subset_are_rejected_by_name(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}"):
        check(parse(text), TYPES)


@pytest.mark.fast
def test_precedence_follows_javascript() -> None:
    node = parse("$(1 + 2 * 3 - -4 < 5 || true && false)")
    assert node.op == "||"
    assert node.args[0].op == "<"
    assert node.args[0].args[0].op == "-"
    assert node.args[1].op == "&&"


@pytest.mark.fast
def test_a_control_character_in_where_never_reaches_the_groovy_source() -> None:
    rendered = render_groovy(parse("$(inputs.a * 2)"), where="CALC\narguments[0]\t", inputs="[a: a]")
    assert "\n" not in rendered and "\t" not in rendered
    assert "'CALC\\u000aarguments[0]\\u0009'" in rendered


COMPUTED = ["$(inputs.a * 2 + 1)", "$(inputs.a > 1)", "$(Math.round(inputs.b))", "$(runtime.cores)", "${return 1}"]


@pytest.mark.fast
@pytest.mark.parametrize("value_from", COMPUTED)
def test_a_computed_argument_valuefrom_is_rejected_by_name(value_from: str) -> None:
    with pytest.raises(ValueError, match=re.escape("run.arguments[0].valueFrom: a computed valueFrom is not supported")):
        _calc_workflow([{"prefix": "-n", "valueFrom": value_from}], {"a": 3, "b": 1.5}, a="int", b="float")


@pytest.mark.fast
@pytest.mark.parametrize("value_from", COMPUTED)
def test_a_computed_input_binding_valuefrom_is_rejected_by_name(value_from: str) -> None:
    binding = {"type": "int", "inputBinding": {"position": 1, "valueFrom": value_from}}
    with pytest.raises(ValueError, match=re.escape("run.inputs.a.inputBinding.valueFrom: a computed valueFrom")):
        _calc_workflow([], {"a": 3, "b": 1.5}, a=binding, b="float")


@pytest.mark.fast
@pytest.mark.parametrize("value_from", ["7", "--n=$(inputs.a)", "$(inputs.a)"])
def test_a_literal_or_projection_valuefrom_stays_a_template(value_from: str) -> None:
    workflow = _calc_workflow([{"valueFrom": value_from}], {"a": 1}, a="int")
    assert isinstance(workflow.processes[0].command.tokens[-1], NfTemplate)


@pytest.mark.fast
@pytest.mark.parametrize("value_from", ["$(inputs.a)-$(inputs.b)", "$(inputs.a).$(inputs.b)"])
def test_two_projections_in_one_field_stay_a_template(value_from: str) -> None:
    workflow = _calc_workflow([{"valueFrom": value_from}], {"a": 1, "b": 2}, a="int", b="int")
    assert isinstance(workflow.processes[0].command.tokens[-1], NfTemplate)


@pytest.mark.fast
@pytest.mark.parametrize(("node", "message"), [
    (Expr("Runtime.getRuntime().exec", (Expr("ref", value="a"),)),
     "unsupported construct 'Runtime.getRuntime\\(\\)\\.exec'"),
    (Expr("number", value="010"), "not a strict-mode decimal"),
])
def test_a_condition_built_outside_the_closed_grammar_is_rejected(node: Expr, message: str) -> None:
    """A tree built without parsing, as the reader builds one, is checked when its process is built."""
    process = _when_workflow("$(Math.pow(inputs.a, 2) > 1)", {"a": 3}, a="int").processes[0]
    assert process.condition is not None
    with pytest.raises(ValueError, match=message):
        replace(process, condition=replace(process.condition, args=(node, *process.condition.args[1:])))
