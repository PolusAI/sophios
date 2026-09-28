"""Conditional steps: `when` lowering to a branch/mix skip with CWL null (design §6, Topology)."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any

import pytest

from sophios.nf_types import ExecutableNextflowWorkflow
from sophios.utils_nf import compiled_source_to_nextflow

from .testkit import execute_nextflow, step, synthetic_source, tool, workflow_doc


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
    ("$(true)", {}),
    ("$(false)", {}),
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
    process = workflow.processes[0]
    assert process.condition is not None
    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow


@pytest.mark.fast
def test_when_requires_a_boolean_result() -> None:
    with pytest.raises(ValueError, match="must compute a boolean, not a number"):
        _when_workflow("$(inputs.a + 1)", {"a": 3}, a="int")


@pytest.mark.fast
def test_when_outside_the_safe_subset_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match=r"unsupported construct '\*\*'"):
        _when_workflow("$(inputs.a ** 2)", {"a": 3}, a="int")


@pytest.mark.fast
def test_when_on_a_scattered_step_is_rejected() -> None:
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
    with pytest.raises(ValueError, match="per-combination when is not supported yet"):
        compiled_source_to_nextflow(synthetic_source(workflow, [produce], workflow_inputs={"a": [1, 2]}))


@pytest.mark.fast
def test_non_optional_consumer_of_a_conditional_output_is_rejected() -> None:
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
        inputs={"result": {"type": "string"}},
        baseCommand="true",
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
    with pytest.raises(ValueError, match="non-optional port cannot consume a possibly-null"):
        compiled_source_to_nextflow(synthetic_source(workflow, [produce, consume], workflow_inputs={"a": 1}))


@pytest.mark.fast
def test_path_consumer_of_a_conditional_output_is_rejected() -> None:
    produce = tool(
        "PRODUCE",
        inputs={"a": {"type": "int"}},
        baseCommand="echo",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    consume = tool(
        "CONSUME",
        inputs={"result": {"type": "File?"}},
        baseCommand="true",
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
    with pytest.raises(ValueError, match="a path consumer of a possibly-null"):
        compiled_source_to_nextflow(synthetic_source(workflow, [produce, consume], workflow_inputs={"a": 1}))


@pytest.mark.fast
def test_array_typed_output_of_a_conditional_step_is_rejected() -> None:
    produce = tool(
        "PRODUCE",
        inputs={"a": {"type": "int"}},
        baseCommand="echo",
        outputs={"result": {"type": {"type": "array", "items": "File"}, "outputBinding": {"glob": "*.txt"}}},
    )
    workflow = workflow_doc(
        [step("PRODUCE", **{"in": {"a": "a"}, "out": ["result"], "when": "$(inputs.a > 0)"})],
        inputs={"a": "int"},
        outputs={"result": {"type": {"type": "array", "items": "File"}, "outputSource": "PRODUCE/result"}},
    )
    with pytest.raises(ValueError, match="array-typed output of a conditional step"):
        compiled_source_to_nextflow(synthetic_source(workflow, [produce], workflow_inputs={"a": 1}))


@pytest.mark.fast
def test_when_requires_the_condition_schema_version() -> None:
    payload = _when_workflow("$(inputs.a > 0)", {"a": 3}, a="int").to_dict()
    payload["schema_version"] = 10
    with pytest.raises(ValueError, match="'condition' requires executable Nextflow schema version 11"):
        ExecutableNextflowWorkflow.from_dict(payload)


def _run(workflow: ExecutableNextflowWorkflow, directory: Path) -> tuple[int, str]:
    from sophios.input_output_nf import write_nextflow_artifacts  # pylint: disable=import-outside-toplevel
    write_nextflow_artifacts(workflow, directory)
    result = execute_nextflow(directory)
    return result.returncode, result.stdout + result.stderr


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_true_condition_runs_the_task(tmp_path: Path) -> None:
    workflow = _when_workflow("$(inputs.a > 0)", {"a": 1}, a="int")
    code, log = _run(workflow, tmp_path)
    assert code == 0, log
    assert (tmp_path / "work").exists()
    processes = [child for child in (tmp_path / "work").glob("*/*") if child.is_dir()]
    assert len(processes) == 1


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_false_condition_runs_no_task_and_downstream_completes(tmp_path: Path) -> None:
    workflow = _when_workflow("$(inputs.a > 0)", {"a": 0}, second_step=True, a="int")
    code, log = _run(workflow, tmp_path)
    assert code == 0, log
    processes = [child for child in (tmp_path / "work").glob("*/*") if child.is_dir()]
    # A zero exit with exactly one task means CONSUME ran and PRODUCE was skipped.
    assert len(processes) == 1
