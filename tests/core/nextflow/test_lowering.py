"""Semantic lowering: supported CWL source becomes typed executable IR."""

# pylint: disable=missing-function-docstring

import copy
from typing import Any

import pytest

from sophios import inference
from sophios.input_output_nf import render_nextflow
from sophios.nf_types import (
    NfBasenameReference,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfResources,
    NfTemplate,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow, cwl_type_to_nf_qualifier
from sophios.wic_types import RoseTree

from .testkit import step, synthetic_rose, tool, workflow_doc


@pytest.mark.fast
@pytest.mark.parametrize(
    ("cwl_type", "qualifier"),
    [
        ("File", "path"),
        ("Directory", "path"),
        ("string", "val"),
        ("int", "val"),
        ("float", "val"),
        ("boolean", "val"),
        ("File?", "path"),
    ],
    ids=["file", "directory", "string", "int", "float", "boolean", "optional-file"],
)
def test_cwl_type_mapping(cwl_type: Any, qualifier: str) -> None:
    assert cwl_type_to_nf_qualifier(cwl_type) == qualifier


@pytest.mark.fast
@pytest.mark.parametrize("cwl_type", ["Any", "File[]", {"type": "array", "items": "File"}])
def test_rejects_unbounded_and_collection_types(cwl_type: Any) -> None:
    with pytest.raises(ValueError, match="unsupported CWL type"):
        cwl_type_to_nf_qualifier(cwl_type)


@pytest.mark.fast
def test_real_supported_rosetree_converts(real_supported_rose: RoseTree) -> None:
    converted = cwl_rosetree_to_nextflow(real_supported_rose)
    assert converted.name == "wf"
    assert [process.name for process in converted.processes] == [
        "wf__step__1__touch",
        "wf__step__2__copy",
    ]


@pytest.mark.fast
def test_composes_command_arguments_bindings_and_redirects() -> None:
    run_tool = tool(
        "RUN",
        inputs={
            "message": {"type": "string", "inputBinding": {"position": 2, "prefix": "--message"}},
            "source": {"type": "File"},
        },
        outputs={"report": {"type": "File", "outputBinding": {"glob": "stdout.txt"}}},
        baseCommand=["python", "script.py"],
        arguments=[{"position": 1, "prefix": "--mode", "valueFrom": "fast"}],
        stdin="$(inputs.source.path)",
        stdout="stdout.txt",
        stderr="stderr.txt",
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("RUN", **{"in": {"message": "message", "source": "source"}, "out": ["report"]})],
            inputs={"message": {"type": "string"}, "source": {"type": "File"}},
            outputs={"report": {"type": "File", "outputSource": "RUN/report"}},
        ),
        [run_tool],
        workflow_inputs={"message": "hello", "source": {"class": "File", "path": "input.txt"}},
    )
    process = cwl_rosetree_to_nextflow(rose).processes[0]
    rendered = render_nextflow(cwl_rosetree_to_nextflow(rose))
    command_line = next(line for line in rendered.splitlines() if " < " in line)
    assert command_line.count("__sophios_shell_quote_9f72e") == 9
    for fragment in ("'python'", "'script.py'", "'--mode'", "'fast'", "'--message'"):
        assert fragment in command_line
    assert "message.toString()" in command_line
    assert "< ${__sophios_shell_quote_9f72e(source.toString())}" in command_line
    assert "> ${__sophios_shell_quote_9f72e('stdout.txt')}" in command_line
    assert "2> ${__sophios_shell_quote_9f72e('stderr.txt')}" in command_line
    assert process.outputs[0].glob == NfTemplate((NfLiteral("stdout.txt"),))


@pytest.mark.fast
def test_boolean_flag_lowers_to_a_conditional_flag_token() -> None:
    sort_tool = tool(
        "SORT",
        inputs={
            "reverse": {
                "type": "boolean",
                "inputBinding": {"position": 1, "prefix": "-r"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "reverse"}})],
            inputs={"reverse": {"type": "boolean"}},
        ),
        [sort_tool],
        workflow_inputs={"reverse": True},
    )

    command = cwl_rosetree_to_nextflow(rose).processes[0].command

    assert command.tokens[1] == NfFlag("reverse", "-r")


@pytest.mark.fast
def test_flag_tokens_take_their_cwl_position_among_other_bindings() -> None:
    """A flag is ordered by position like any other binding, not appended."""
    sort_tool = tool(
        "SORT",
        inputs={
            "source": {"type": "File", "inputBinding": {"position": 3}},
            "reverse": {"type": "boolean", "inputBinding": {"position": 2, "prefix": "-r"}},
        },
        arguments=[{"position": 1, "valueFrom": "--stable"}],
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SORT", **{"in": {"source": "source", "reverse": "reverse"}})],
            inputs={"source": {"type": "File"}, "reverse": {"type": "boolean"}},
        ),
        [sort_tool],
        workflow_inputs={
            "source": {"class": "File", "path": "in.txt"},
            "reverse": True,
        },
    )

    tokens = cwl_rosetree_to_nextflow(rose).processes[0].command.tokens

    assert tokens[0] == NfTemplate((NfLiteral("SORT"),))
    assert tokens[1] == NfTemplate((NfLiteral("--stable"),))
    assert tokens[2] == NfFlag("reverse", "-r")
    assert tokens[3] == NfTemplate((NfInputReference("source"),))


@pytest.mark.fast
def test_basename_lowers_to_a_typed_segment_in_every_template_position() -> None:
    copy_tool = tool(
        "COPY",
        inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "$(inputs.source.basename).copy"},
            }
        },
        arguments=[{"position": 2, "valueFrom": "$(inputs.source.basename).copy"}],
        stdout="$(inputs.source.basename).log",
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("COPY", **{"in": {"source": "source"}, "out": ["result"]})],
            inputs={"source": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "COPY/result"}},
        ),
        [copy_tool],
        workflow_inputs={"source": {"class": "File", "path": "lines.txt"}},
    )

    process = cwl_rosetree_to_nextflow(rose).processes[0]

    expected = (NfBasenameReference("source"), NfLiteral(".copy"))
    assert process.outputs[0].glob == NfTemplate(expected)
    assert process.command.tokens[-1] == NfTemplate(expected)
    assert process.command.stdout == NfTemplate(
        (NfBasenameReference("source"), NfLiteral(".log"))
    )


@pytest.mark.fast
def test_boolean_binding_without_a_prefix_contributes_no_token() -> None:
    sort_tool = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "reverse"}})],
            inputs={"reverse": {"type": "boolean"}},
        ),
        [sort_tool],
        workflow_inputs={"reverse": True},
    )

    command = cwl_rosetree_to_nextflow(rose).processes[0].command

    assert [token for token in command.tokens if isinstance(token, NfFlag)] == []
    assert len(command.tokens) == 1


@pytest.mark.fast
def test_applies_unwired_scalar_default_before_lowering() -> None:
    default_tool = tool(
        "DEFAULT",
        inputs={
            "message": {
                "type": "string",
                "default": "hello default",
                "inputBinding": {"position": 1},
            }
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
        arguments=[{"position": 2, "valueFrom": "result.txt"}],
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("DEFAULT", out=["result"])],
            outputs={"result": {"type": "File", "outputSource": "DEFAULT/result"}},
        ),
        [default_tool],
    )
    workflow = cwl_rosetree_to_nextflow(rose)
    assert workflow.params == {"DEFAULT___message": "hello default"}
    assert NfWorkflowInputConnection("DEFAULT___message", "DEFAULT", "message") in workflow.connections
    assert "Channel.value(params.DEFAULT___message)" in render_nextflow(workflow)


@pytest.mark.fast
def test_rejects_collisions_between_source_and_default_params() -> None:
    default_tool = tool(
        "DEFAULT",
        inputs={
            "message": {
                "type": "string",
                "default": "tool default",
                "inputBinding": {"position": 1},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("DEFAULT")],
            inputs={"DEFAULT___message": {"type": "string"}},
        ),
        [default_tool],
        workflow_inputs={"DEFAULT___message": "workflow value"},
    )
    with pytest.raises(
        ValueError,
        match="lowered workflow parameter names collide: DEFAULT___message",
    ):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_maps_docker_requirement() -> None:
    container = tool(
        "CONTAINER",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    rose = synthetic_rose(workflow_doc([step("CONTAINER")]), [container])
    assert cwl_rosetree_to_nextflow(rose).processes[0].container == "ubuntu:24.04"


@pytest.mark.fast
def test_maps_cpu_and_memory_requirements() -> None:
    resources = tool(
        "RESOURCES",
        requirements={
            "ResourceRequirement": {
                "coresMin": 1,
                "coresMax": 2,
                "ramMin": 512,
                "ramMax": 1024,
            }
        },
    )
    rose = synthetic_rose(workflow_doc([step("RESOURCES")]), [resources])
    assert cwl_rosetree_to_nextflow(rose).processes[0].resources == NfResources(2, 1024)


@pytest.mark.fast
def test_preserves_linear_dag(real_supported_rose: RoseTree) -> None:
    workflow = cwl_rosetree_to_nextflow(real_supported_rose)
    internal = [
        connection
        for connection in workflow.connections
        if isinstance(connection, NfProcessConnection)
    ]
    assert [(edge.from_process, edge.from_port, edge.to_process, edge.to_port) for edge in internal] == [
        ("wf__step__1__touch", "result", "wf__step__2__copy", "source"),
    ]


@pytest.mark.fast
def test_preserves_fanout() -> None:
    producer = tool(
        "A", outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}
    )
    consumer_b = tool("B", inputs={"value": {"type": "File"}})
    consumer_c = tool("C", inputs={"value": {"type": "File"}})
    rose = synthetic_rose(
        workflow_doc([
            step("A", out=["out"]),
            step("B", **{"in": {"value": "A/out"}}),
            step("C", **{"in": {"value": "A/out"}}),
        ]),
        [producer, consumer_b, consumer_c],
    )
    connections = cwl_rosetree_to_nextflow(rose).connections
    assert len([
        edge
        for edge in connections
        if isinstance(edge, NfProcessConnection)
        and edge.from_process == "A"
        and edge.from_port == "out"
    ]) == 2


@pytest.mark.fast
def test_preserves_workflow_input_connection(real_supported_rose: RoseTree) -> None:
    connections = cwl_rosetree_to_nextflow(real_supported_rose).connections
    assert NfWorkflowInputConnection(
        "wf__step__1__touch___filename",
        "wf__step__1__touch",
        "filename",
    ) in connections


@pytest.mark.fast
def test_preserves_workflow_output_connection(real_supported_rose: RoseTree) -> None:
    connections = cwl_rosetree_to_nextflow(real_supported_rose).connections
    assert NfWorkflowOutputConnection(
        "wf__step__2__copy",
        "result",
        "wf__step__2__copy___result",
    ) in connections


@pytest.mark.fast
def test_copies_workflow_params(real_supported_rose: RoseTree) -> None:
    params = cwl_rosetree_to_nextflow(real_supported_rose).params
    assert params == {"wf__step__1__touch___filename": "message.txt"}


@pytest.mark.fast
def test_forward_conversion_does_not_call_inference(
    monkeypatch: pytest.MonkeyPatch,
    real_supported_rose: RoseTree,
) -> None:
    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("forward conversion must not invoke inference")

    monkeypatch.setattr(inference, "perform_edge_inference", fail_if_called)
    cwl_rosetree_to_nextflow(real_supported_rose)


@pytest.mark.fast
def test_conversion_does_not_mutate_rosetree(real_supported_rose: RoseTree) -> None:
    before = copy.deepcopy(real_supported_rose.data.compiled_cwl)
    cwl_rosetree_to_nextflow(real_supported_rose)
    assert real_supported_rose.data.compiled_cwl == before
