"""Conditional steps: `when` lowering to a branch/mix skip with CWL null (design §6, Topology)."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any

import pytest

from sophios.input_output_nf import render_nextflow
from sophios.nf_expr import parse
from sophios.nf_types import ExecutableNextflowWorkflow, NfOptionalBinding, NfProcess
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import command, execute_nextflow, output_port, step, synthetic_source, tool, workflow_doc


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
    steps = [step("PRODUCE", **{"in": {name: name for name in inputs}, "out": ["result"], "when": when})]
    tools = [produce]
    outputs = {"result": {"type": "string?", "outputSource": "PRODUCE/result"}}
    if second_step:
        consume = tool(
            "CONSUME",
            inputs={"maybe_result": {"type": "string?"}},
            baseCommand=["touch", "done.txt"],
            outputs={"done": {"type": "File", "outputBinding": {"glob": "done.txt"}}},
        )
        steps.append(step("CONSUME", **{"in": {"maybe_result": "PRODUCE/result"}, "out": ["done"]}))
        tools.append(consume)
        outputs = {
            "result": {"type": "string?", "outputSource": "PRODUCE/result"},
            "done": {"type": "File", "outputSource": "CONSUME/done"},
        }
    workflow = workflow_doc(
        steps,
        inputs={name: definition["type"] for name, definition in declared.items()},
        outputs=outputs,
    )
    return compiled_source_to_nextflow(synthetic_source(workflow, tools, workflow_inputs=params))


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
    with pytest.raises(ValueError, match=r"steps\[0\]\.when: a conditional step needs at least one input to gate on"):
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
    workflow = workflow_doc(
        [step("PRODUCE", **{
            "in": {"a": "a"}, "out": ["result"], "when": "$(inputs.a > 0)", "scatter": "a",
        })],
        inputs={"a": {"type": {"type": "array", "items": "int"}}},
        outputs={"result": {"type": {"type": "array", "items": "File"}, "outputSource": "PRODUCE/result"}},
    )
    lowered = compiled_source_to_nextflow(synthetic_source(workflow, [produce], workflow_inputs={"a": [1, 2]}))
    assert lowered.processes[0].condition is not None


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
    workflow = workflow_doc(
        [
            step("PRODUCE", **{"in": {"a": "a"}, "out": ["result"], "when": "$(inputs.a > 0)"}),
            step("CONSUME", **{"in": {"result": "PRODUCE/result"}, "out": ["done"]}),
        ],
        inputs={"a": "int"},
        outputs={"done": {"type": "File", "outputSource": "CONSUME/done"}},
    )
    source = synthetic_source(workflow, [produce, consume], workflow_inputs={"a": 1})
    if admitted:
        assert compiled_source_to_nextflow(source).processes[1].command.tokens[-1] == NfOptionalBinding(
            "result", "--result"
        )
    else:
        with pytest.raises(ValueError, match="not one the absent-optional lowering admits"):
            compiled_source_to_nextflow(source)


def _run(workflow: ExecutableNextflowWorkflow, directory: Path) -> tuple[int, str]:
    from sophios.input_output_nf import write_nextflow_artifacts  # pylint: disable=import-outside-toplevel
    write_nextflow_artifacts(workflow, directory)
    result = execute_nextflow(directory)
    return result.returncode, result.stdout + result.stderr


def _tasks(directory: Path) -> list[Path]:
    return [child for child in (directory / "work").glob("*/*") if child.is_dir()]


def _gated_chain_workflow(
    ports: tuple[str, ...], *, go: bool, flag: bool,
) -> ExecutableNextflowWorkflow:
    """UP runs when ``go``; GATED reads UP's result, the flag, an array and an absent optional."""
    text_result = {
        "type": "string",
        "outputBinding": {"glob": "out.txt", "loadContents": True, "outputEval": "$(self[0].contents)"},
    }
    up = tool(
        "UP", inputs={"go": {"type": "boolean"}}, baseCommand="echo",
        arguments=[{"position": 1, "valueFrom": "ok"}], stdout="out.txt", outputs={"result": text_result},
    )
    declared = {
        "prev": {"type": "string?"},
        "flag": {"type": "boolean"},
        "xs": {"type": {"type": "array", "items": "int"}, "inputBinding": {"position": 1}},
        "maybe": {"type": "int?"},
    }
    gated = tool(
        "GATED", inputs={name: declared[name] for name in ports}, baseCommand="echo", stdout="out.txt",
        outputs={"result": text_result},
    )
    steps = [
        step("UP", **{"in": {"go": "go"}, "out": ["result"], "when": "$(inputs.go)"}),
        step("GATED", **{
            "in": {"prev": "UP/result", "flag": "flag", "xs": "xs", "maybe": "maybe"},
            "out": ["result"],
            "when": "$(inputs.prev !== null && inputs.flag && inputs.maybe === null)",
        }),
    ]
    workflow = workflow_doc(
        steps,
        inputs={
            "go": "boolean", "flag": "boolean", "maybe": "int?",
            "xs": {"type": {"type": "array", "items": "int"}},
        },
        outputs={"result": {"type": "string?", "outputSource": "GATED/result"}},
    )
    return compiled_source_to_nextflow(
        synthetic_source(workflow, [up, gated], workflow_inputs={"go": go, "flag": flag, "xs": [1, 2, 3]})
    )


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.parametrize(("ports", "go", "flag", "outputs"), [
    pytest.param(("prev", "flag", "xs", "maybe"), True, True, ["1 2 3\n", "ok\n"], id="runs-with-the-queue-first"),
    pytest.param(("flag", "xs", "maybe", "prev"), True, True, ["1 2 3\n", "ok\n"], id="runs-with-the-queue-last"),
    pytest.param(("flag", "xs", "maybe", "prev"), False, True, [], id="skips-on-a-skipped-upstream"),
    pytest.param(("flag", "xs", "maybe", "prev"), True, False, ["ok\n"], id="skips-on-a-false-parameter"),
])
def test_a_multi_input_condition_combines_parameters_and_process_outputs(
    tmp_path: Path, ports: tuple[str, ...], go: bool, flag: bool, outputs: list[str],
) -> None:
    """Both polarities of a three-operand when over a process output, a parameter and the [] sentinel.

    The array input and the absent optional's sentinel are list-valued items
    in the combined tuple; the run case sees the whole array on its command line.
    """
    code, log = _run(_gated_chain_workflow(ports, go=go, flag=flag), tmp_path)
    assert code == 0, log
    assert len(_tasks(tmp_path)) == len(outputs)
    assert sorted(path.read_text() for path in (tmp_path / "work").glob("*/*/out.txt")) == outputs


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
    workflow = compiled_source_to_nextflow(compile_workflow_result(Workflow([write, child], "root")))
    conditions = {process.name: process.condition for process in workflow.processes}
    assert [name for name, condition in conditions.items() if condition == parse("$(false)")] == [
        name for name in conditions if "inner_copy" in name
    ]
    assert sum(condition is not None for condition in conditions.values()) == 1
