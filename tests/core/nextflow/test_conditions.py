"""Conditional steps: `when` lowering to a branch/mix skip with CWL null (design §6, Topology)."""

# pylint: disable=missing-function-docstring

from typing import Any

import pytest

from sophios.input_output_nf import render_nextflow
from sophios.lang.subset import parse
from sophios.nf_types import ExecutableNextflowWorkflow, NfOptionalBinding, NfProcess

from .testkit import command, lowered, lowered_case, output_port, step, tool


def _when_workflow(
    when: str,
    params: dict[str, Any],
    *,
    second_step: bool = False,
    **inputs: Any,
) -> ExecutableNextflowWorkflow:
    declared = {
        name: cwl_type if isinstance(cwl_type, dict) else {"type": cwl_type}
        for name, cwl_type in inputs.items()
    }
    produce = tool(
        "PRODUCE",
        inputs=declared,
        baseCommand="echo",
        arguments=[{"position": 1, "valueFrom": "ok"}],
        stdout="out.txt",
        outputs={
            "result": {
                "type": "string",
                "outputBinding": {"glob": "out.txt", "loadContents": True, "outputEval": "$(self[0].contents)"},
            }
        },
    )
    result: str | dict[str, Any] = {"result": {"wic_anchor": "result"}} if second_step else "result"
    steps = [step("PRODUCE", **{"in": {name: name for name in inputs}, "out": [result], "when": when})]
    tools = [produce]
    outputs = {"result": {"type": "string?", "outputSource": "PRODUCE/result"}}
    if second_step:
        consume = tool(
            "CONSUME",
            inputs={"maybe_result": {"type": "string?"}},
            baseCommand=["touch", "done.txt"],
            outputs={"done": {"type": "File", "outputBinding": {"glob": "done.txt"}}},
        )
        steps.append(step("CONSUME", **{"in": {"maybe_result": {"wic_alias": "result"}}, "out": ["done"]}))
        tools.append(consume)
        outputs = {
            "result": {"type": "string?", "outputSource": "PRODUCE/result"},
            "done": {"type": "File", "outputSource": "CONSUME/done"},
        }
    workflow = {
        "steps": steps,
        "inputs": {name: definition["type"] for name, definition in declared.items()},
        "outputs": outputs,
    }
    return lowered_case({"workflow": workflow, "tools": tools, "job": params})[0]


@pytest.mark.fast
@pytest.mark.parametrize(("when", "inputs"), [
    ("$(true)", {"a": "int"}),
    ("$(false)", {"a": "int"}),
    ("$(inputs.flag)", {"flag": "boolean"}),
    ("$(inputs.a > 1)", {"a": "int"}),
    ("$(inputs.a == 2)", {"a": "int"}),
    ("$(inputs.a === 2)", {"a": "int"}),
    ("$(inputs.a > 1 && inputs.flag || !inputs.flag)", {"a": "int", "flag": "boolean"}),
    ("$(Math.round(inputs.b) > 1)", {"b": "float"}),
    ("$(inputs.maybe === null)", {"maybe": ["null", "int"]}),
])
def test_admitted_when_cells_lower_to_a_process_condition(when: str, inputs: dict[str, Any]) -> None:
    values = {name: (False if cwl_type == "boolean" else 1.5 if cwl_type == "float" else 1)
              for name, cwl_type in inputs.items()}
    workflow = _when_workflow(when, values, **inputs)
    assert workflow.processes[0].condition is not None


@pytest.mark.fast
@pytest.mark.parametrize("when", ["$(true)", "$(false)"])
def test_a_conditional_step_with_no_inputs_is_rejected_at_lowering(when: str) -> None:
    with pytest.raises(ValueError, match="step 'PRODUCE': when: a conditional step needs at least one input to "
                                         "gate on"):
        _when_workflow(when, {})


@pytest.mark.fast
def test_a_conditional_process_with_no_inputs_is_rejected_at_the_ir_boundary() -> None:
    with pytest.raises(ValueError, match="process 'PRODUCE' has a condition but no inputs"):
        NfProcess(
            "PRODUCE", [], [output_port("result", "out.txt")], command("echo", stdout="out.txt"),
            condition=parse("$(true)"),
        )


@pytest.mark.fast
@pytest.mark.parametrize(("when", "predicate"), [
    ("$(inputs.maybe === null)", "(__w0 == null || __w0 == [])"),
    ("$(inputs.maybe == null)", "(__w0 == null || __w0 == [])"),
    ("$(null === inputs.maybe)", "(__w0 == null || __w0 == [])"),
    ("$(inputs.maybe !== null)", "(!(__w0 == null || __w0 == []))"),
    ("$(inputs.maybe != null)", "(!(__w0 == null || __w0 == []))"),
])
def test_a_null_check_renders_as_a_test_for_the_absent_sentinel(when: str, predicate: str) -> None:
    # An absent optional input reaches the process as the [] sentinel, never as null.
    workflow = _when_workflow(when, {}, maybe=["null", "int"])
    assert f"        run: {predicate}\n" in render_nextflow(workflow)


@pytest.mark.fast
def test_a_when_diagnostic_names_the_authored_inputs() -> None:
    rendered = render_nextflow(_when_workflow("$(inputs.n / inputs.d > 1)", {"n": 4, "d": 2}, n="int", d="int"))
    assert "'(inputs.n / inputs.d)', [d: __w1, n: __w0]" in rendered
    assert "inputs.__w" not in rendered


@pytest.mark.fast
def test_when_on_a_single_input_scattered_step_lowers() -> None:
    produce = tool(
        "PRODUCE",
        inputs={"a": {"type": "int"}},
        baseCommand="echo",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    workflow = {
        "steps": [step("PRODUCE", **{
            "in": {"a": "a"}, "out": ["result"], "when": "$(inputs.a > 0)", "scatter": "a",
        })],
        "inputs": {"a": {"type": {"type": "array", "items": "int"}}},
        "outputs": {"result": {"type": {"type": "array", "items": "File"}, "outputSource": "PRODUCE/result"}},
    }
    workflow_model, _notes = lowered_case({"workflow": workflow, "tools": [produce], "job": {"a": [1, 2]}})
    assert workflow_model.processes[0].condition is not None


@pytest.mark.fast
@pytest.mark.parametrize(
    ("arguments", "admitted"), [([], True), ([{"position": 2, "valueFrom": "$(inputs.result)"}], False)]
)
def test_optional_binding_consumer_of_a_conditional_output(arguments: list[Any], admitted: bool) -> None:
    """A skipped step's null reaches an optional input's own binding, and no other reference to it."""
    produce = tool(
        "PRODUCE",
        inputs={"a": {"type": "int"}},
        baseCommand="echo",
        outputs={
            "result": {
                "type": "string",
                "outputBinding": {"glob": "out.txt", "loadContents": True, "outputEval": "$(self[0].contents)"},
            }
        },
    )
    consume = tool(
        "CONSUME",
        inputs={"result": {"type": "string?", "inputBinding": {"position": 1, "prefix": "--result"}}},
        arguments=arguments,
        baseCommand="echo",
        outputs={"done": {"type": "File", "outputBinding": {"glob": "done.txt"}}},
    )
    workflow = {
        "steps": [
            step("PRODUCE", **{"in": {"a": "a"}, "out": [{"result": {"wic_anchor": "result"}}],
                               "when": "$(inputs.a > 0)"}),
            step("CONSUME", **{"in": {"result": {"wic_alias": "result"}}, "out": ["done"]}),
        ],
        "inputs": {"a": "int"},
        "outputs": {"done": {"type": "File", "outputSource": "CONSUME/done"}},
    }
    case = {"workflow": workflow, "tools": [produce, consume], "job": {"a": 1}}
    if admitted:
        assert lowered_case(case)[0].processes[1].command.tokens[-1] == NfOptionalBinding("result", "--result")
    else:
        with pytest.raises(ValueError, match="not one the absent-optional lowering admits"):
            lowered_case(case)


@pytest.mark.fast
def test_a_condition_inside_an_inlined_subworkflow_survives_inlining() -> None:
    # pylint: disable=import-outside-toplevel
    from sophios.api.python._workflow_runtime import compile_workflow_result
    from sophios.api.python.workflow import Step, Workflow
    from .test_capabilities import _copy_tool, _write_step
    write = _write_step()
    inner = Step(_copy_tool(), step_name="inner_copy")
    inner.when = "$(false)"
    child = Workflow([inner], "child")
    inner.inputs.source = child.inputs.source
    child.outputs.result = inner.outputs.result
    child.inputs.source = write.outputs.result
    workflow = lowered(compile_workflow_result(Workflow([write, child], "root")))
    conditions = {process.name: process.condition for process in workflow.processes}
    assert [name for name, condition in conditions.items() if condition == parse("$(false)")] == [
        name for name in conditions if "inner_copy" in name
    ]
    assert sum(condition is not None for condition in conditions.values()) == 1
