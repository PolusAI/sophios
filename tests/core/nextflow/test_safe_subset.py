"""The safe JavaScript subset in valueFrom (design §6, Expressions)."""

# pylint: disable=missing-function-docstring

from pathlib import Path
import re
from typing import Any

import pytest

from sophios.nf_expr import Expr, check, parse, render_groovy
from sophios.nf_types import ExecutableNextflowWorkflow, NfComputed, NfTemplate
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import execute_nextflow, step, synthetic_source, tool, workflow_doc

TYPES = {"a": "int", "b": "float", "s": "string", "flag": "boolean", "maybe": ["null", "int"]}


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


@pytest.mark.fast
def test_computed_valuefrom_lowers_and_round_trips() -> None:
    workflow = _calc_workflow(
        [{"position": 1, "prefix": "-n", "valueFrom": "$(inputs.a * 2 + 1)"}],
        {"a": 3},
        a="int",
    )
    tokens = workflow.processes[0].command.tokens
    computed = [token for token in tokens if isinstance(token, NfComputed)]
    assert len(computed) == 1
    assert computed[0].where == "CALC arguments[0].valueFrom $(inputs.a * 2 + 1)"
    assert computed[0].integral is False
    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow


@pytest.mark.fast
def test_computed_token_requires_schema_version_ten() -> None:
    payload = _calc_workflow([{"valueFrom": "$(inputs.a + 1)"}], {"a": 3}, a="int").to_dict()
    payload["schema_version"] = 9
    with pytest.raises(ValueError, match="'computed' requires executable Nextflow schema version 10"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
@pytest.mark.parametrize(("argument", "message"), [
    ({"valueFrom": "$(inputs.a > 1)"}, "must compute a number, not a boolean"),
    ({"valueFrom": "$(inputs.a ** 2)"}, "is outside the safe JavaScript subset: unsupported construct '\\*\\*'"),
    (
        {"prefix": "-n", "separate": False, "valueFrom": "$(inputs.a + 1)"},
        "separate: false beside a computed valueFrom",
    ),
])
def test_computed_argument_rejections(argument: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _calc_workflow([argument], {"a": 3}, a="int")


@pytest.mark.fast
@pytest.mark.fast
@pytest.mark.parametrize("value_from", ["$(inputs.a)-$(inputs.b)", "$(inputs.a).$(inputs.b)"])
def test_two_projections_in_one_field_stay_a_template(value_from: str) -> None:
    workflow = _calc_workflow([{"valueFrom": value_from}], {"a": 1, "b": 2}, a="int", b="int")
    value = workflow.processes[0].command.tokens[-1]
    assert isinstance(value, NfTemplate)
    assert not any(isinstance(token, NfComputed) for token in workflow.processes[0].command.tokens)


@pytest.mark.fast
def test_a_hydrated_call_outside_the_closed_set_is_rejected() -> None:
    payload = _calc_workflow([{"valueFrom": "$(Math.pow(inputs.a, 2))"}], {"a": 3}, a="int").to_dict()
    tokens = payload["processes"][0]["command"]["tokens"]
    token = next(item for item in tokens if item.get("kind") == "computed")
    token["expression"]["op"] = "Runtime.getRuntime().exec"
    with pytest.raises(ValueError, match="unsupported construct 'Runtime.getRuntime\\(\\)\\.exec'"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_a_hydrated_number_with_a_leading_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a strict-mode decimal"):
        NfComputed(Expr("number", value="010"), "tool field $(010)", False)


@pytest.mark.fast
def test_computed_valuefrom_on_an_optional_binding_is_rejected() -> None:
    with pytest.raises(ValueError, match="computed valueFrom on optional input 'n'"):
        _calc_workflow([], {"a": 3, "n": 1}, a="int", n={"type": ["null", "int"],
                       "inputBinding": {"valueFrom": "$(inputs.a + 1)"}})


def _run(workflow: ExecutableNextflowWorkflow, directory: Path) -> tuple[int, str, str]:
    from sophios.input_output_nf import write_nextflow_artifacts  # pylint: disable=import-outside-toplevel
    write_nextflow_artifacts(workflow, directory)
    result = execute_nextflow(directory)
    outputs = list(directory.glob("work/*/*/out.txt"))
    return result.returncode, outputs[0].read_text(encoding="utf-8") if outputs else "", result.stdout + result.stderr


ARGUMENTS = [
    {"position": 1, "valueFrom": "$((inputs.a + 1) / 4)"},
    {"position": 2, "prefix": "--b", "valueFrom": "$(Math.round(inputs.b * 2) + inputs.a % 3)"},
    {"position": 3, "valueFrom": "$(Math.max(-0.5, inputs.b, 0.0001) - -1e3)"},
    {"position": 4, "valueFrom": "$(inputs.a / inputs.d)"},
    {"position": 5, "valueFrom": "$(-7 % 3 + Math.round(-2.5) + Math.round(1e19))"},
]


@pytest.mark.nextflow
@pytest.mark.serial
def test_computed_arguments_match_javascript_under_nextflow(tmp_path: Path) -> None:
    # Expected text is node's value printed the way cwltool prints it.
    workflow = _calc_workflow(ARGUMENTS, {"a": 7, "b": 1.25, "d": 2}, a="int", b="float", d="int")
    code, text, log = _run(workflow, tmp_path)
    assert code == 0, log
    assert text == "2 --b 4 1001.25 3.5 10000000000000000000\n"


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_non_finite_subexpression_fails_with_a_named_diagnostic(tmp_path: Path) -> None:
    workflow = _calc_workflow(ARGUMENTS, {"a": 7, "b": 1.25, "d": 0}, a="int", b="float", d="int")
    code, _text, log = _run(workflow, tmp_path)
    assert code != 0
    where = "CALC arguments[3].valueFrom $(inputs.a / inputs.d)"
    assert f"Sophios expression {where}: (inputs.a / inputs.d) is Infinity" in log
    assert "[a:7, d:0]" in log


@pytest.mark.fast
def test_a_computed_workflow_reads_back_and_promotes(tmp_path: Path) -> None:
    from sophios.input_output_nf import write_nextflow_artifacts  # pylint: disable=import-outside-toplevel
    from sophios.nf_reader import parse_nf_file, promote_nextflow_document  # pylint: disable=import-outside-toplevel
    workflow = _calc_workflow(ARGUMENTS, {"a": 7, "b": 1.25, "d": 2}, a="int", b="float", d="int")
    write_nextflow_artifacts(workflow, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert parsed.opaque_regions == ()
    assert promote_nextflow_document(parsed) == workflow
