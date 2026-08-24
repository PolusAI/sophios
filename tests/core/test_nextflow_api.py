"""Sprint 1 acceptance tests for the Sophios Nextflow intermediate representation."""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, cast

import pytest

from sophios import cli, inference
from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow
from sophios.input_output_nf import (
    render_nextflow,
    render_nextflow_config,
    write_nextflow_artifacts,
)
from sophios.nf_symbols import is_nextflow_identifier, normalize_nextflow_identifier
from sophios.nf_reader import (
    NextflowDocument,
    NextflowPort,
    import_nextflow,
    nextflow_to_cwl,
    parse_nf_file,
    parse_nf_text,
    promote_nextflow_document,
    render_nextflow_document,
)
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfCommand,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow, cwl_type_to_nf_qualifier
from sophios.wic_types import GraphReps, NodeData, RoseTree, Tool, Yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _command(*tokens: str) -> NfCommand:
    return NfCommand(tuple(NfTemplate((NfLiteral(token),)) for token in tokens))


def _output(name: str, glob: str | None = None) -> NfPort:
    return NfPort(
        name,
        "path",
        name,
        NfTemplate((NfLiteral(glob or name),)),
    )


def _node_data(
    name: str,
    compiled_cwl: Yaml,
    *,
    workflow_inputs: Yaml | None = None,
    source_yml: Yaml | None = None,
) -> NodeData:
    """Build the smallest real NodeData needed by a conversion fixture."""
    return NodeData(
        [],
        name,
        source_yml or {},
        compiled_cwl,
        Tool(f"{name}.cwl", compiled_cwl),
        workflow_inputs or {},
        {},
        {},
        cast(GraphReps, None),
        {},
        f'"{name}"',
    )


def _synthetic_rose(
    workflow_cwl: Yaml,
    tools: list[Yaml],
    *,
    workflow_inputs: Yaml | None = None,
    source_yml: Yaml | None = None,
) -> RoseTree:
    """Construct a typed RoseTree without invoking compiler internals."""
    children = [
        RoseTree(_node_data(str(tool.get("id", f"tool_{index}")), tool), [])
        for index, tool in enumerate(tools)
    ]
    return RoseTree(
        _node_data(
            str(workflow_cwl.get("id", "workflow")),
            workflow_cwl,
            workflow_inputs=workflow_inputs,
            source_yml=source_yml,
        ),
        children,
    )


def _tool(
    name: str,
    *,
    inputs: Yaml | None = None,
    outputs: Yaml | None = None,
    **extra: Any,
) -> Yaml:
    """Return a compact CommandLineTool dictionary."""
    return {
        "id": name,
        "class": "CommandLineTool",
        "cwlVersion": "v1.2",
        "baseCommand": name,
        "inputs": inputs or {},
        "outputs": outputs or {},
        **extra,
    }


def _workflow(steps: list[Yaml], *, inputs: Yaml | None = None, outputs: Yaml | None = None) -> Yaml:
    """Return a compact compiled CWL Workflow dictionary."""
    return {
        "id": "wf",
        "class": "Workflow",
        "cwlVersion": "v1.2",
        "inputs": inputs or {},
        "outputs": outputs or {},
        "steps": steps,
    }


@pytest.fixture(scope="module")
def unsupported_real_linear_rose() -> RoseTree:
    """Compile the reviewer's real shell/primitive-output workflow."""
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    append = Step(clt_path=REPO_ROOT / "cwl_adapters" / "append.cwl")
    append.inputs.str = "Hello"
    cat = Step(clt_path=REPO_ROOT / "cwl_adapters" / "cat.cwl")
    return Workflow([touch, append, cat], "wf")._compile().rose


@pytest.fixture(scope="module")
def real_supported_rose() -> RoseTree:
    """Compile a wholly supported two-process workflow through the real API."""
    touch = Step.from_cwl_document(
        {
            "id": "TOUCH",
            "class": "CommandLineTool",
            "cwlVersion": "v1.2",
            "baseCommand": "touch",
            "inputs": {
                "filename": {
                    "type": "string",
                    "inputBinding": {"position": 1},
                }
            },
            "outputs": {
                "result": {
                    "type": "File",
                    "outputBinding": {"glob": "$(inputs.filename)"},
                }
            },
        },
        process_name="touch",
    )
    touch.inputs.filename = "message.txt"
    copy_step = Step.from_cwl_document(
        {
            "id": "COPY",
            "class": "CommandLineTool",
            "cwlVersion": "v1.2",
            "baseCommand": "cp",
            "arguments": [{"position": 2, "valueFrom": "copy.txt"}],
            "inputs": {
                "source": {
                    "type": "File",
                    "inputBinding": {"position": 1},
                }
            },
            "outputs": {
                "result": {
                    "type": "File",
                    "outputBinding": {"glob": "copy.txt"},
                }
            },
        },
        process_name="copy",
    )
    copy_step.inputs.source = touch.outputs.result
    return Workflow([touch, copy_step], "wf")._compile().rose


# T1.1 — eight IR, validation, and hydration scenarios.


@pytest.mark.fast
def test_nf101_t11_port_dict_roundtrip() -> None:
    port = _output("reads", "*.fq")
    assert NfPort.from_dict(port.to_dict()) == port


@pytest.mark.fast
def test_nf101_t11_process_dict_roundtrip() -> None:
    process = NfProcess(
        name="ALIGN",
        inputs=[NfPort("reads", "path")],
        outputs=[_output("bam", "result.bam")],
        command=_command("align", "reads"),
        container="aligner:1",
        resources=NfResources(2, 1024),
    )
    assert NfProcess.from_dict(process.to_dict()) == process


@pytest.mark.fast
def test_nf101_t11_workflow_dict_roundtrip() -> None:
    workflow = ExecutableNextflowWorkflow(
        name="wf",
        processes=[NfProcess("ECHO", [], [_output("out")], _command("echo", "hi"))],
        connections=[NfWorkflowOutputConnection("ECHO", "out", "result")],
        params={"message": "hello"},
    )
    assert ExecutableNextflowWorkflow.from_dict(workflow.to_dict()) == workflow


@pytest.mark.fast
def test_nf101_t11_json_roundtrip_is_deterministic() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {"b": 2, "a": 1})
    serialized = workflow.to_json()
    assert serialized == workflow.to_json()
    assert ExecutableNextflowWorkflow.from_json(serialized) == workflow


@pytest.mark.fast
def test_nf101_t11_executable_schema_declares_version_and_kind() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {})
    payload = workflow.to_dict()

    assert payload["schema_version"] == 1
    assert payload["representation_kind"] == "executable"

    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="schema version"):
        ExecutableNextflowWorkflow.from_dict(payload)

    payload["schema_version"] = 1
    payload["representation_kind"] = "structural"
    with pytest.raises(ValueError, match="representation kind"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_nf101_t11_executable_values_are_deeply_immutable_and_hashable() -> None:
    process = NfProcess(
        "P",
        [NfPort("message", "val")],
        [_output("result", "result.txt")],
        _command("touch", "result.txt"),
    )
    workflow = ExecutableNextflowWorkflow(
        "wf",
        [process],
        [
            NfWorkflowInputConnection("message", "P", "message"),
            NfWorkflowOutputConnection("P", "result", "result"),
        ],
        {"message": {"nested": ["hello"]}},
    )

    assert hash(process)
    assert hash(workflow)
    with pytest.raises(AttributeError):
        cast(Any, process.inputs).append(NfPort("late", "val"))
    with pytest.raises(AttributeError):
        cast(Any, process.command.tokens).append(NfTemplate((NfLiteral("late"),)))
    with pytest.raises(TypeError):
        cast(Any, workflow.params)["message"] = "changed"

    with pytest.raises(ValueError, match="keys must be strings"):
        ExecutableNextflowWorkflow("wf", (), (), cast(Any, {1: "value"}))
    with pytest.raises(ValueError, match="JSON-compatible"):
        ExecutableNextflowWorkflow("wf", (), (), {"value": cast(Any, {1, 2})})


@pytest.mark.fast
def test_nf101_t11_executable_ir_has_no_opaque_or_magic_directive_fields() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf", [NfProcess("P", [], [], _command("true"))], [], {}
    ).to_dict()
    payload["directives"] = {"_unparsed": "workflow.onComplete { ... }"}
    with pytest.raises(ValueError, match="unknown fields"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_nf101_t11_uses_nextflow_identifier_symbols() -> None:
    for name in ("café", "Δelta", "$money", "_private", "Ⅻstep", "process"):
        assert is_nextflow_identifier(name)
        assert NfPort(name, "val").name == name

    for name in ("if", "class", "_", "9start", "dash-name", "😀"):
        assert not is_nextflow_identifier(name)
        with pytest.raises(ValueError, match="Nextflow identifier"):
            NfPort(name, "val")

    assert normalize_nextflow_identifier("9-café") == "_9_café"
    assert normalize_nextflow_identifier("if") == "_if"
    with pytest.raises(ValueError, match="reserved Nextflow backend identifier"):
        NfPort("__sophios_shell_quote_9f72e", "val")

    with pytest.raises(ValueError, match="qualifier"):
        NfPort("reads", "unsupported")


@pytest.mark.fast
def test_nf101_t11_rejects_duplicate_process_names() -> None:
    process = NfProcess("P", [], [], _command("true"))
    with pytest.raises(ValueError, match="duplicate process"):
        ExecutableNextflowWorkflow("wf", [process, process], [], {})


@pytest.mark.fast
def test_nf101_t11_rejects_connection_to_unknown_process() -> None:
    with pytest.raises(ValueError, match="unknown destination process"):
        ExecutableNextflowWorkflow(
            "wf",
            [],
            [NfWorkflowInputConnection("message", "MISSING", "message")],
            {"message": "hi"},
        )


@pytest.mark.fast
def test_nf101_t11_rejects_nullable_boundary_and_duplicate_workflow_emits() -> None:
    with pytest.raises((TypeError, ValueError)):
        NfWorkflowInputConnection("message", cast(Any, None), "message")

    first = NfProcess("A", [], [_output("out", "a.txt")], _command("touch", "a.txt"))
    second = NfProcess("B", [], [_output("out", "b.txt")], _command("touch", "b.txt"))
    with pytest.raises(ValueError, match="duplicate output emit"):
        ExecutableNextflowWorkflow(
            "wf",
            [first, second],
            [
                NfWorkflowOutputConnection("A", "out", "result"),
                NfWorkflowOutputConnection("B", "out", "result"),
            ],
            {},
        )


@pytest.mark.fast
def test_nf101_t11_rejects_mixed_channel_semantics_for_one_parameter() -> None:
    value_process = NfProcess("VALUE", [NfPort("shared", "val")], [], _command("true"))
    path_process = NfProcess("PATH", [NfPort("shared", "path")], [], _command("true"))
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [value_process, path_process],
            [
                NfWorkflowInputConnection("shared", "VALUE", "shared"),
                NfWorkflowInputConnection("shared", "PATH", "shared"),
            ],
            {"shared": "input.txt"},
        )


# T1.2 — five real-boundary and rejection scenarios.


@pytest.mark.fast
def test_nf101_t12_real_supported_rosetree_converts(real_supported_rose: RoseTree) -> None:
    converted = cwl_rosetree_to_nextflow(real_supported_rose)
    assert converted.name == "wf"
    assert [process.name for process in converted.processes] == [
        "wf__step__1__touch",
        "wf__step__2__copy",
    ]


@pytest.mark.fast
def test_nf101_t12_real_unsupported_rosetree_aggregates_capability_errors(
    unsupported_real_linear_rose: RoseTree,
) -> None:
    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(unsupported_real_linear_rose)
    message = str(error.value)
    assert "steps[1].run.requirements.ShellCommandRequirement" in message
    assert "steps[1].run.requirements.InitialWorkDirRequirement" in message
    assert "steps[1].run.inputs.file.inputBinding.shellQuote" in message
    assert "steps[2].run.outputs.output.type" in message
    assert "steps[2].run.outputs.output.outputBinding.loadContents" in message
    assert "steps[2].run.outputs.output.outputBinding.outputEval" in message


@pytest.mark.fast
def test_nf101_t12_rejects_every_unconsumed_tool_field_before_lowering() -> None:
    tool = _tool(
        "UNCONSUMED",
        inputs={
            "reference": {
                "type": "File",
                "secondaryFiles": [".fai"],
                "format": "https://edamontology.org/format_1929",
                "inputBinding": {
                    "position": 1,
                    "itemSeparator": ",",
                    "loadContents": True,
                },
            }
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
        requirements={
            "DockerRequirement": {
                "dockerPull": "ubuntu:24.04",
                "dockerOutputDirectory": "/output",
            }
        },
        successCodes=[1],
    )
    rose = _synthetic_rose(
        _workflow(
            [
                {
                    "id": "UNCONSUMED",
                    "in": {"reference": "reference"},
                    "out": ["result"],
                    "run": "UNCONSUMED.cwl",
                }
            ],
            inputs={"reference": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "UNCONSUMED/result"}},
        ),
        [tool],
        workflow_inputs={"reference": {"class": "File", "path": "reference.fa"}},
    )

    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(rose)

    message = str(error.value)
    assert "steps[0].run.successCodes" in message
    assert "steps[0].run.inputs.reference.secondaryFiles" in message
    assert "steps[0].run.inputs.reference.format" in message
    assert "steps[0].run.inputs.reference.inputBinding.itemSeparator" in message
    assert "steps[0].run.inputs.reference.inputBinding.loadContents" in message
    assert "steps[0].run.requirements.DockerRequirement.dockerOutputDirectory" in message


@pytest.mark.fast
def test_nf101_t12_ignores_inert_documentation_but_not_semantics() -> None:
    baseline_tool = _tool(
        "IDENTITY",
        inputs={
            "source": {
                "type": "File",
                "inputBinding": {"position": 1},
            }
        },
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "result.txt"},
            }
        },
        arguments=[{"position": 2, "valueFrom": "result.txt"}],
    )
    documented_tool = copy.deepcopy(baseline_tool)
    documented_tool.update({
        "$namespaces": {"edam": "https://edamontology.org/"},
        "$schemas": ["https://example.org/formats.rdf"],
        "label": "Identity",
        "doc": "Copies one file without changing executable semantics.",
    })
    documented_tool["inputs"]["source"].update({
        "label": "Source",
        "doc": "The file to copy.",
    })
    documented_tool["outputs"]["result"].update({
        "label": "Result",
        "doc": "The copied file.",
    })

    workflow = _workflow(
        [{
            "id": "IDENTITY",
            "in": {"source": "source"},
            "out": ["result"],
            "run": "IDENTITY.cwl",
        }],
        inputs={"source": {"type": "File"}},
        outputs={"result": {"type": "File", "outputSource": "IDENTITY/result"}},
    )
    workflow_inputs = {"source": {"class": "File", "path": "source.txt"}}
    baseline = cwl_rosetree_to_nextflow(
        _synthetic_rose(workflow, [baseline_tool], workflow_inputs=workflow_inputs)
    )
    documented = cwl_rosetree_to_nextflow(
        _synthetic_rose(workflow, [documented_tool], workflow_inputs=workflow_inputs)
    )

    assert documented == baseline
    assert render_nextflow(documented) == render_nextflow(baseline)

    documented_tool["permanentFailCodes"] = [1]
    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(
            _synthetic_rose(workflow, [documented_tool], workflow_inputs=workflow_inputs)
        )
    message = str(error.value)
    assert "steps[0].run.permanentFailCodes" in message
    for path in (
        "steps[0].run.$namespaces",
        "steps[0].run.$schemas",
        "steps[0].run.label",
        "steps[0].run.doc",
        "steps[0].run.inputs.source.label",
        "steps[0].run.inputs.source.doc",
        "steps[0].run.outputs.result.label",
        "steps[0].run.outputs.result.doc",
    ):
        assert path not in message


@pytest.mark.fast
def test_nf101_t12_rejects_compiledworkflow_substitution() -> None:
    compiled = CompiledWorkflow("wf", _workflow([]), {})
    with pytest.raises(TypeError, match="RoseTree"):
        cwl_rosetree_to_nextflow(cast(Any, compiled))


@pytest.mark.fast
def test_nf101_t12_requires_workflow_root() -> None:
    rose = RoseTree(_node_data("tool", _tool("tool")), [])
    with pytest.raises(ValueError, match="root.*Workflow"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t12_rejects_nested_or_unsupported_workflow_constructs() -> None:
    nested = _workflow([])
    rose = _synthetic_rose(
        _workflow([{"id": "nested", "in": {}, "out": [], "run": "nested.cwl"}]),
        [nested],
    )
    with pytest.raises(ValueError, match="nested workflows.*Phase 2"):
        cwl_rosetree_to_nextflow(rose)

    conditional = _synthetic_rose(
        _workflow([{"id": "conditional", "in": {}, "out": [], "run": "tool.cwl", "when": "$(true)"}]),
        [_tool("tool")],
    )
    with pytest.raises(ValueError, match="when.*not supported.*Phase 1"):
        cwl_rosetree_to_nextflow(conditional)


@pytest.mark.fast
def test_nf101_t12_conversion_does_not_mutate_rosetree(real_supported_rose: RoseTree) -> None:
    before = copy.deepcopy(real_supported_rose.data.compiled_cwl)
    cwl_rosetree_to_nextflow(real_supported_rose)
    assert real_supported_rose.data.compiled_cwl == before


@pytest.mark.fast
@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (lambda workflow: workflow.update({"requirements": {}}), "workflow.requirements"),
        (
            lambda workflow: workflow["inputs"]["message"].update(
                {"inputBinding": {"position": 1}}
            ),
            "workflow.inputs.message.inputBinding",
        ),
        (
            lambda workflow: workflow["outputs"]["result"].update(
                {"pickValue": "first_non_null"}
            ),
            "workflow.outputs.result.pickValue",
        ),
        (
            lambda workflow: workflow["steps"][0].update({"requirements": {}}),
            "steps[0].requirements",
        ),
        (
            lambda workflow: workflow["steps"][0]["in"]["message"].update(
                {"valueFrom": "changed"}
            ),
            "steps[0].in.message.valueFrom",
        ),
    ],
)
def test_nf101_t12_closed_world_analysis_covers_workflow_and_step_levels(
    mutation: Any,
    diagnostic: str,
) -> None:
    tool = _tool(
        "WRITE",
        inputs={"message": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
    )
    workflow = _workflow(
        [
            {
                "id": "WRITE",
                "in": {"message": {"source": "message"}},
                "out": ["result"],
                "run": "WRITE.cwl",
            }
        ],
        inputs={"message": {"type": "string"}},
        outputs={"result": {"type": "File", "outputSource": "WRITE/result"}},
    )
    mutation(workflow)
    rose = _synthetic_rose(workflow, [tool], workflow_inputs={"message": "hello"})
    with pytest.raises(ValueError, match=re.escape(diagnostic)):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize(
    ("workflow_inputs", "default", "diagnostic"),
    [
        ({}, None, "required workflow input value is missing"),
        ({"message": None}, None, "explicit null input values"),
        ({}, ["not", "scalar"], "JSON scalar defaults only"),
    ],
)
def test_nf101_t12_boundary_missingness_is_not_conflated_with_defaults(
    workflow_inputs: dict[str, Any],
    default: Any,
    diagnostic: str,
) -> None:
    definition: dict[str, Any] = {"type": "string"}
    if default is not None:
        definition["default"] = default
    rose = _synthetic_rose(
        _workflow([], inputs={"message": definition}),
        [],
        workflow_inputs=workflow_inputs,
    )
    with pytest.raises(ValueError, match=diagnostic):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t12_rejects_undeclared_workflow_input_values() -> None:
    rose = _synthetic_rose(_workflow([]), [], workflow_inputs={"extra": "value"})
    with pytest.raises(ValueError, match="has no declared workflow input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t12_rejects_workflow_input_identifier_collisions() -> None:
    rose = _synthetic_rose(
        _workflow(
            [],
            inputs={
                "out-dir": {"type": "string"},
                "out_dir": {"type": "string"},
            },
        ),
        [],
        workflow_inputs={"out-dir": "first", "out_dir": "second"},
    )
    with pytest.raises(
        ValueError,
        match=r"workflow input identifiers 'out-dir', 'out_dir'.*normalize to 'out_dir'",
    ):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t12_aggregates_workflow_input_collisions_with_other_findings() -> None:
    tool = _tool("COLLISION")
    rose = _synthetic_rose(
        _workflow(
            [
                {
                    "id": "COLLISION",
                    "in": {},
                    "out": [],
                    "run": "COLLISION.cwl",
                    "unknown_field": True,
                }
            ],
            inputs={
                "out-dir": {"type": "string"},
                "out_dir": {"type": "string"},
            },
        ),
        [tool],
        workflow_inputs={"out-dir": "first", "out_dir": "second"},
    )

    with pytest.raises(ValueError) as exc_info:
        cwl_rosetree_to_nextflow(rose)

    diagnostic = str(exc_info.value)
    assert diagnostic.startswith("Nextflow Phase 1 capability analysis failed:\n")
    assert "workflow.inputs: workflow input identifiers" in diagnostic
    assert "steps[0].unknown_field" in diagnostic


@pytest.mark.fast
def test_nf101_t12_rejects_tool_port_identifier_collisions() -> None:
    tool = _tool(
        "COLLISION",
        inputs={
            "out-dir": {"type": "string", "default": "first"},
            "out_dir": {"type": "string", "default": "second"},
        },
    )
    rose = _synthetic_rose(
        _workflow([
            {"id": "COLLISION", "in": {}, "out": [], "run": "COLLISION.cwl"},
        ]),
        [tool],
    )
    with pytest.raises(
        ValueError,
        match=r"tool input identifiers 'out-dir', 'out_dir'.*normalize to 'out_dir'",
    ):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize(
    ("cwl_type", "value"),
    [
        ("string", ["not", "a", "string"]),
        ("int", True),
        (
            "File",
            {"class": "File", "path": "reference.fa", "secondaryFiles": ["reference.fa.fai"]},
        ),
    ],
)
def test_nf101_t12_rejects_boundary_values_outside_supported_shape(
    cwl_type: Any,
    value: Any,
) -> None:
    rose = _synthetic_rose(
        _workflow([], inputs={"value": {"type": cwl_type}}),
        [],
        workflow_inputs={"value": value},
    )
    with pytest.raises(ValueError, match="does not match its supported CWL type"):
        cwl_rosetree_to_nextflow(rose)


# T1.3 — twelve type, command, container, and resource scenarios.


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
def test_nf101_t13_cwl_type_mapping(cwl_type: Any, qualifier: str) -> None:
    assert cwl_type_to_nf_qualifier(cwl_type) == qualifier


@pytest.mark.fast
@pytest.mark.parametrize("cwl_type", ["Any", "File[]", {"type": "array", "items": "File"}])
def test_nf101_t13_rejects_unbounded_and_collection_types(cwl_type: Any) -> None:
    with pytest.raises(ValueError, match="unsupported CWL type"):
        cwl_type_to_nf_qualifier(cwl_type)


@pytest.mark.fast
def test_nf101_t13_rejects_absent_optional_input_before_lowering() -> None:
    tool = _tool("OPTIONAL", inputs={"message": {"type": ["null", "string"]}})
    rose = _synthetic_rose(
        _workflow(
            [{"id": "OPTIONAL", "in": {"message": "message"}, "out": [], "run": "OPTIONAL.cwl"}],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [tool],
        workflow_inputs={"message": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.message.*absent optional"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t13_composes_command_arguments_bindings_and_redirects() -> None:
    tool = _tool(
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
    rose = _synthetic_rose(
        _workflow(
            [
                {
                    "id": "RUN",
                    "in": {"message": "message", "source": "source"},
                    "out": ["report"],
                    "run": "RUN.cwl",
                }
            ],
            inputs={"message": {"type": "string"}, "source": {"type": "File"}},
            outputs={"report": {"type": "File", "outputSource": "RUN/report"}},
        ),
        [tool],
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
def test_nf101_t13_applies_unwired_scalar_default_before_lowering() -> None:
    tool = _tool(
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
    rose = _synthetic_rose(
        _workflow(
            [{"id": "DEFAULT", "in": {}, "out": ["result"], "run": "DEFAULT.cwl"}],
            outputs={"result": {"type": "File", "outputSource": "DEFAULT/result"}},
        ),
        [tool],
    )
    workflow = cwl_rosetree_to_nextflow(rose)
    assert workflow.params == {"DEFAULT___message": "hello default"}
    assert NfWorkflowInputConnection("DEFAULT___message", "DEFAULT", "message") in workflow.connections
    assert "Channel.value(params.DEFAULT___message)" in render_nextflow(workflow)


@pytest.mark.fast
def test_nf101_t13_rejects_collisions_between_source_and_default_params() -> None:
    tool = _tool(
        "DEFAULT",
        inputs={
            "message": {
                "type": "string",
                "default": "tool default",
                "inputBinding": {"position": 1},
            }
        },
    )
    rose = _synthetic_rose(
        _workflow(
            [{"id": "DEFAULT", "in": {}, "out": [], "run": "DEFAULT.cwl"}],
            inputs={"DEFAULT___message": {"type": "string"}},
        ),
        [tool],
        workflow_inputs={"DEFAULT___message": "workflow value"},
    )
    with pytest.raises(
        ValueError,
        match="lowered workflow parameter names collide: DEFAULT___message",
    ):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t13_rejects_output_glob_outside_typed_input_subset() -> None:
    tool = _tool(
        "BAD_GLOB",
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "$(runtime.outdir)/result.txt"},
            }
        },
    )
    rose = _synthetic_rose(
        _workflow([{"id": "BAD_GLOB", "in": {}, "out": ["result"], "run": "BAD_GLOB.cwl"}]),
        [tool],
    )
    with pytest.raises(ValueError, match="unsupported CWL expression"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t13_maps_docker_requirement() -> None:
    tool = _tool(
        "CONTAINER",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    rose = _synthetic_rose(
        _workflow([{"id": "CONTAINER", "in": {}, "out": [], "run": "CONTAINER.cwl"}]),
        [tool],
    )
    assert cwl_rosetree_to_nextflow(rose).processes[0].container == "ubuntu:24.04"


@pytest.mark.fast
def test_nf101_t13_rejects_mixed_container_execution_policy() -> None:
    containerized = _tool(
        "CONTAINERIZED",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    host = _tool("HOST")
    rose = _synthetic_rose(
        _workflow([
            {"id": "CONTAINERIZED", "in": {}, "out": [], "run": "CONTAINERIZED.cwl"},
            {"id": "HOST", "in": {}, "out": [], "run": "HOST.cwl"},
        ]),
        [containerized, host],
    )
    with pytest.raises(ValueError, match="mixed container execution is not supported"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t13_maps_cpu_and_memory_requirements() -> None:
    tool = _tool(
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
    rose = _synthetic_rose(
        _workflow([{"id": "RESOURCES", "in": {}, "out": [], "run": "RESOURCES.cwl"}]),
        [tool],
    )
    assert cwl_rosetree_to_nextflow(rose).processes[0].resources == NfResources(2, 1024)


# T1.4 — eight graph, interface, params, scatter, and inference scenarios.


@pytest.mark.fast
def test_nf101_t14_preserves_linear_dag(real_supported_rose: RoseTree) -> None:
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
def test_nf101_t14_preserves_fanout() -> None:
    producer = _tool(
        "A", outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}
    )
    consumer_b = _tool("B", inputs={"value": {"type": "File"}})
    consumer_c = _tool("C", inputs={"value": {"type": "File"}})
    rose = _synthetic_rose(
        _workflow(
            [
                {"id": "A", "in": {}, "out": ["out"], "run": "A.cwl"},
                {"id": "B", "in": {"value": "A/out"}, "out": [], "run": "B.cwl"},
                {"id": "C", "in": {"value": "A/out"}, "out": [], "run": "C.cwl"},
            ]
        ),
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
def test_nf101_t14_preserves_workflow_input_connection(real_supported_rose: RoseTree) -> None:
    connections = cwl_rosetree_to_nextflow(real_supported_rose).connections
    assert NfWorkflowInputConnection(
        "wf__step__1__touch___filename",
        "wf__step__1__touch",
        "filename",
    ) in connections


@pytest.mark.fast
def test_nf101_t14_preserves_workflow_output_connection(real_supported_rose: RoseTree) -> None:
    connections = cwl_rosetree_to_nextflow(real_supported_rose).connections
    assert NfWorkflowOutputConnection(
        "wf__step__2__copy",
        "result",
        "wf__step__2__copy___result",
    ) in connections


@pytest.mark.fast
def test_nf101_t14_copies_workflow_params(real_supported_rose: RoseTree) -> None:
    params = cwl_rosetree_to_nextflow(real_supported_rose).params
    assert params == {"wf__step__1__touch___filename": "message.txt"}


@pytest.mark.fast
def test_nf101_t14_rejects_executable_scatter_before_lowering() -> None:
    tool = _tool("SCATTER", inputs={"item": {"type": "string"}})
    step = {
        "id": "SCATTER",
        "in": {"item": "items"},
        "out": [],
        "run": "SCATTER.cwl",
        "scatter": "item",
        "scatterMethod": "dotproduct",
    }
    rose = _synthetic_rose(
        _workflow([step], inputs={"items": {"type": {"type": "array", "items": "string"}}}),
        [tool],
        workflow_inputs={"items": ["a", "b"]},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].scatter.*Phase 2"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_nf101_t14_forward_conversion_does_not_call_inference(
    monkeypatch: pytest.MonkeyPatch,
    real_supported_rose: RoseTree,
) -> None:
    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("forward conversion must not invoke inference")

    monkeypatch.setattr(inference, "perform_edge_inference", fail_if_called)
    cwl_rosetree_to_nextflow(real_supported_rose)


@pytest.mark.fast
def test_nf101_t14_rejects_unknown_connection_source() -> None:
    tool = _tool("B", inputs={"value": {"type": "File"}})
    rose = _synthetic_rose(
        _workflow([{"id": "B", "in": {"value": "MISSING/out"}, "out": [], "run": "B.cwl"}]),
        [tool],
    )
    with pytest.raises(ValueError, match="unknown source process"):
        cwl_rosetree_to_nextflow(rose)


# NF-102 — deterministic artifacts and core runtime semantics.


def _runtime_workflow() -> ExecutableNextflowWorkflow:
    produce = NfProcess(
        "PRODUCE",
        [NfPort("message", "val")],
        [_output("result", "message.txt")],
        NfCommand(
            (
                _command("printf").tokens[0],
                _command("%s").tokens[0],
                NfTemplate((NfInputReference("message"),)),
            ),
            stdout=NfTemplate((NfLiteral("message.txt"),)),
        ),
    )
    copy_process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [_output("copy", "copy.txt")],
        NfCommand((
            NfTemplate((NfLiteral("cp"),)),
            NfTemplate((NfInputReference("source"),)),
            NfTemplate((NfLiteral("copy.txt"),)),
        )),
    )
    return ExecutableNextflowWorkflow(
        "PIPELINE",
        [produce, copy_process],
        [
            NfWorkflowInputConnection("message", "PRODUCE", "message"),
            NfProcessConnection("PRODUCE", "result", "COPY", "source"),
            NfWorkflowOutputConnection("COPY", "copy", "result"),
        ],
        {"message": "hello from Sophios"},
    )


@pytest.mark.serial
def test_nf102_t21_writes_four_deterministic_artifacts(tmp_path: Path) -> None:
    workflow = _runtime_workflow()
    before = workflow.to_json()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}
    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]
    write_nextflow_artifacts(workflow, tmp_path)
    assert {path.name: path.read_bytes() for path in paths} == first
    assert workflow.to_json() == before


@pytest.mark.fast
def test_nf102_t21_renderer_rejects_non_executable_representation() -> None:
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        render_nextflow(cast(Any, {"representation_kind": "structural"}))


@pytest.mark.serial
def test_nf102_t21_renders_named_workflow_and_entry_wrapper() -> None:
    rendered = render_nextflow(_runtime_workflow())
    assert "workflow PIPELINE {" in rendered
    assert "take:\n    message" in rendered
    assert "COPY(PRODUCE.out.result)" in rendered
    assert "result = COPY.out.copy" in rendered
    assert "workflow {\n    PIPELINE(Channel.value(params.message))\n}" in rendered


@pytest.mark.serial
def test_nf102_t21_renders_real_compiled_source_with_typed_glob(
    real_supported_rose: RoseTree,
) -> None:
    rendered = render_nextflow(cwl_rosetree_to_nextflow(real_supported_rose))
    assert 'path "${filename}", emit: result' in rendered
    assert "wf__step__2__copy(wf__step__1__touch.out.result)" in rendered


@pytest.mark.serial
def test_nf102_t21_renders_supported_process_metadata() -> None:
    process = NfProcess(
        "TASK",
        [NfPort("source", "path")],
        [_output("report", "report.txt")],
        _command("touch", "report.txt"),
        container="ubuntu:24.04",
        resources=NfResources(2, 1024),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "TASK", "source")],
        {"source": "input.txt"},
    ))
    assert 'container "ubuntu:24.04"' in rendered
    assert "cpus 2" in rendered
    assert 'memory "1024 MB"' in rendered
    assert 'path "report.txt", emit: report' in rendered


@pytest.mark.serial
def test_nf102_t21_path_parameter_rendering_is_runtime_shape_independent() -> None:
    process = NfProcess(
        "READ",
        [NfPort("source", "path")],
        [],
        _command("true"),
    )
    connections = [NfWorkflowInputConnection("source", "READ", "source")]
    from_string = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": "input.txt"},
    )
    from_mapping = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": {"class": "File", "path": "input.txt"}},
    )

    rendered = render_nextflow(from_string)
    assert rendered == render_nextflow(from_mapping)
    assert (
        "Channel.fromPath(params.source instanceof Map ? params.source.path : "
        "params.source, checkIfExists: true)"
    ) in rendered


@pytest.mark.fast
def test_nf102_t21_executable_ir_rejects_mixed_container_policy() -> None:
    containerized = NfProcess("CONTAINERIZED", [], [], _command("true"), container="ubuntu:24.04")
    host = NfProcess("HOST", [], [], _command("true"))
    with pytest.raises(ValueError, match="mixed container execution is not supported"):
        ExecutableNextflowWorkflow("WF", [containerized, host], [], {})


@pytest.mark.fast
def test_nf102_t21_config_uses_validated_workflow_container_policy() -> None:
    host = ExecutableNextflowWorkflow(
        "HOST_WF",
        [NfProcess("HOST", [], [], _command("true"))],
        [],
        {},
    )
    containerized = ExecutableNextflowWorkflow(
        "CONTAINER_WF",
        [NfProcess("CONTAINER", [], [], _command("true"), container="ubuntu:24.04")],
        [],
        {},
    )
    assert render_nextflow_config(host) == "docker.enabled = false\n"
    assert render_nextflow_config(containerized) == "docker.enabled = true\n"


@pytest.mark.serial
def test_nf102_t21_rejects_cyclic_or_multiply_connected_dags() -> None:
    a = NfProcess(
        "A",
        [NfPort("value", "path")],
        [_output("out", "a.txt")],
        _command("touch", "a.txt"),
    )
    b = NfProcess(
        "B",
        [NfPort("value", "path")],
        [_output("out", "b.txt")],
        _command("touch", "b.txt"),
    )
    with pytest.raises(ValueError, match="cycle"):
        ExecutableNextflowWorkflow(
            "WF",
            [a, b],
            [
                NfProcessConnection("A", "out", "B", "value"),
                NfProcessConnection("B", "out", "A", "value"),
            ],
            {},
        )

    with pytest.raises(ValueError, match="one source"):
        ExecutableNextflowWorkflow(
            "WF",
            [a, b],
            [
                NfWorkflowInputConnection("first", "A", "value"),
                NfWorkflowInputConnection("second", "A", "value"),
            ],
            {"first": 1, "second": 2},
        )


@pytest.mark.serial
def test_nf102_t21_rejects_invalid_private_ir_before_writing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path qualifier and typed glob"):
        NfProcess("BAD_OUTPUT", [], [NfPort("result", "val", "result")], _command("true"))
    assert list(tmp_path.iterdir()) == []


def _nextflow_executable() -> str:
    executable = shutil.which("nextflow")
    if executable is None:
        pytest.skip("Nextflow executable is not available")
    return executable


def _run_nextflow(
    workflow: ExecutableNextflowWorkflow,
    directory: Path,
) -> subprocess.CompletedProcess[str]:
    write_nextflow_artifacts(workflow, directory)
    return _execute_nextflow(directory)


def _execute_nextflow(directory: Path, *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(directory / ".nxf-home"),
        "NXF_OFFLINE": "true",
    })
    return subprocess.run(
        [
            _nextflow_executable(),
            "run",
            "workflow.nf",
            "-params-file",
            "nextflow_params.json",
            "-c",
            "nextflow.config",
            "-work-dir",
            str(directory / "work"),
        ],
        cwd=directory,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _single_process_workflow(
    process: NfProcess,
    *,
    params: dict[str, Any],
    output_port: str,
) -> ExecutableNextflowWorkflow:
    connections = [
        NfWorkflowInputConnection(port.name, process.name, port.name)
        for port in process.inputs
    ]
    output_connection = NfWorkflowOutputConnection(
        process.name, output_port, "result"
    )
    return ExecutableNextflowWorkflow(
        "PIPELINE", [process], [*connections, output_connection], params
    )


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf102_t21_and_nf104_t41_actual_nextflow_golden_path(
    tmp_path: Path,
    real_supported_rose: RoseTree,
) -> None:
    result = _run_nextflow(cwl_rosetree_to_nextflow(real_supported_rose), tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "work").rglob("copy.txt"))
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == ""


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf102_t21_real_compiler_preserves_adversarial_argument(tmp_path: Path) -> None:
    value = "price is $5; touch SHOULD_NOT_EXIST ' \" $(uname)\nsecond line"
    write = Step.from_cwl_document(
        {
            "id": "WRITE",
            "class": "CommandLineTool",
            "cwlVersion": "v1.2",
            "baseCommand": "printf",
            "arguments": [{"position": 1, "valueFrom": "%s"}],
            "inputs": {
                "message": {
                    "type": "string",
                    "inputBinding": {"position": 2},
                }
            },
            "stdout": "result.txt",
            "outputs": {
                "result": {
                    "type": "File",
                    "outputBinding": {"glob": "result.txt"},
                }
            },
        },
        process_name="write",
    )
    write.inputs.message = value
    workflow = Workflow([write], "wf")._compile().rose
    result = _run_nextflow(cwl_rosetree_to_nextflow(workflow), tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [value]
    assert not list(tmp_path.rglob("SHOULD_NOT_EXIST"))


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf102_t21_file_object_parameter_executes_with_runtime_shape_dispatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("runtime file object\n", encoding="utf-8")
    copy_process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [_output("result", "result.txt")],
        NfCommand((
            NfTemplate((NfLiteral("cp"),)),
            NfTemplate((NfInputReference("source"),)),
            NfTemplate((NfLiteral("result.txt"),)),
        )),
    )
    workflow = ExecutableNextflowWorkflow(
        "WF",
        [copy_process],
        [NfWorkflowInputConnection("source", "COPY", "source")],
        {"source": {"class": "File", "path": str(source)}},
    )

    result = _run_nextflow(workflow, tmp_path / "run")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    outputs = list((tmp_path / "run" / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == [
        "runtime file object\n"
    ]


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf102_t21_directory_staging_and_command_quoting(tmp_path: Path) -> None:
    source = tmp_path / "source_directory"
    source.mkdir()
    (source / "input.txt").write_text("value with spaces", encoding="utf-8")
    process = NfProcess(
        "READ_DIRECTORY",
        [NfPort("source", "path")],
        [_output("result", "result.txt")],
        NfCommand(
            (
                NfTemplate((NfLiteral("cat"),)),
                NfTemplate((NfInputReference("source"), NfLiteral("/input.txt"))),
            ),
            stdout=NfTemplate((NfLiteral("result.txt"),)),
        ),
    )
    workflow = _single_process_workflow(
        process,
        params={"source": str(source)},
        output_port="result",
    )
    result = _run_nextflow(workflow, tmp_path / "run")
    assert result.returncode == 0, result.stderr
    outputs = list((tmp_path / "run" / "work").rglob("result.txt"))
    assert [path.read_text(encoding="utf-8") for path in outputs] == ["value with spaces"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf102_t21_fanout_and_resource_rendering(tmp_path: Path) -> None:
    produce = NfProcess(
        "PRODUCE",
        [],
        [_output("result", "data.txt")],
        NfCommand(
            _command("printf", "%s", "fanout").tokens,
            stdout=NfTemplate((NfLiteral("data.txt"),)),
        ),
        resources=NfResources(cpus=2, memory_mb=64),
    )
    consume_a = NfProcess(
        "CONSUME_A",
        [NfPort("source", "path")],
        [_output("result", "a.txt")],
        NfCommand(
            (
                NfTemplate((NfLiteral("cat"),)),
                NfTemplate((NfInputReference("source"),)),
            ),
            stdout=NfTemplate((NfLiteral("a.txt"),)),
        ),
    )
    consume_b = NfProcess(
        "CONSUME_B",
        [NfPort("source", "path")],
        [_output("result", "b.txt")],
        NfCommand(
            (
                NfTemplate((NfLiteral("cat"),)),
                NfTemplate((NfInputReference("source"),)),
            ),
            stdout=NfTemplate((NfLiteral("b.txt"),)),
        ),
    )
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [produce, consume_a, consume_b],
        [
            NfProcessConnection("PRODUCE", "result", "CONSUME_A", "source"),
            NfProcessConnection("PRODUCE", "result", "CONSUME_B", "source"),
            NfWorkflowOutputConnection("CONSUME_A", "result", "a"),
            NfWorkflowOutputConnection("CONSUME_B", "result", "b"),
        ],
        {},
    )
    result = _run_nextflow(workflow, tmp_path)
    assert result.returncode == 0, result.stderr
    rendered = render_nextflow(workflow)
    assert "cpus 2" in rendered
    assert 'memory "64 MB"' in rendered
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("a.txt")] == ["fanout"]
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("b.txt")] == ["fanout"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf101_symbol_contract_matches_actual_v2_parser(tmp_path: Path) -> None:
    accepted = ("café", "Δelta", "$money", "_private", "Ⅻstep", "process")
    process_blocks = "\n".join(
        f'process {name} {{\n    script:\n    \"\"\"\n    true\n    \"\"\"\n}}'
        for name in accepted
    )
    calls = "\n".join(f"    {name}()" for name in accepted)
    source = f"nextflow.enable.dsl=2\n{process_blocks}\nworkflow {{\n{calls}\n}}\n"
    script = tmp_path / "symbols.nf"
    script.write_text(source, encoding="utf-8")
    run_env = dict(os.environ)
    run_env.update({
        "NXF_ANSI_LOG": "false",
        "NXF_HOME": str(tmp_path / ".nxf-home"),
        "NXF_OFFLINE": "true",
        "NXF_SYNTAX_PARSER": "v2",
    })
    accepted_result = subprocess.run(
        [_nextflow_executable(), "run", "-preview", script.name],
        cwd=tmp_path,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert accepted_result.returncode == 0, accepted_result.stderr

    for invalid in ("if", "class", "_", "9start", "dash-name", "😀"):
        script.write_text(
            f'nextflow.enable.dsl=2\nprocess {invalid} {{\nscript:\n\"\"\"\ntrue\n\"\"\"\n}}\n'
            f'workflow {{\n    {invalid}()\n}}\n',
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [_nextflow_executable(), "run", "-preview", script.name],
            cwd=tmp_path,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert rejected.returncode != 0, f"actual parser unexpectedly accepted {invalid!r}"


# NF-103 — Python and CLI target selection.


@pytest.mark.serial
def test_nf103_t31_default_and_explicit_cwl_targets_match() -> None:
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    workflow = Workflow([touch], "wf")
    assert workflow.compile() == workflow.compile(target="cwl")


@pytest.mark.serial
def test_nf103_t31_nextflow_target_uses_one_internal_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    from sophios.api.python import _workflow_runtime as runtime

    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    workflow = Workflow([touch], "wf")
    original = runtime.compile_workflow
    calls = 0

    def counted_compile(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "compile_workflow", counted_compile)
    compiled = workflow.compile(target="nextflow")
    assert isinstance(compiled, ExecutableNextflowWorkflow)
    assert calls == 1


@pytest.mark.serial
def test_nf103_t31_writer_generates_all_nextflow_artifacts(tmp_path: Path) -> None:
    touch = Step(clt_path=REPO_ROOT / "cwl_adapters" / "touch.cwl")
    touch.inputs.filename = "empty.txt"
    workflow = Workflow([touch], "wf")
    assert [path.name for path in workflow.to_nextflow(tmp_path)] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]


@pytest.mark.fast
def test_nf103_t32_cli_target_defaults_and_rejects_cwl_run_modes() -> None:
    assert cli.get_args("workflow.wic", ["--generate_cwl_workflow"]).target == "cwl"
    assert cli.get_args("workflow.wic", ["--target", "nextflow"]).target == "nextflow"
    with pytest.raises(SystemExit):
        cli.get_args(
            "workflow.wic",
            ["--generate_cwl_workflow", "--target", "nextflow"],
        )


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf103_t32_cli_generates_and_executes_nextflow_artifacts(tmp_path: Path) -> None:
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "WRITE.cwl").write_text(
        """cwlVersion: v1.2
class: CommandLineTool
baseCommand: touch
arguments: [result.txt]
inputs: {}
outputs:
  result:
    type: File
    outputBinding:
      glob: result.txt
""",
        encoding="utf-8",
    )
    workflow_path = tmp_path / "workflow.wic"
    workflow_path.write_text(
        """steps:
- id: WRITE
  in: {}
  out: [result]
""",
        encoding="utf-8",
    )
    config = {
        "search_paths_cwl": {"global": [str(adapters)], "gpu": []},
        "search_paths_wic": {"global": [str(tmp_path)]},
        "renaming_conventions": [],
        "inference_rules": {},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    command = subprocess.run(
        [
            sys.executable,
            "-m",
            "sophios.main",
            "--yaml",
            str(workflow_path),
            "--config_file",
            str(config_path),
            "--homedir",
            str(tmp_path),
            "--target",
            "nextflow",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert command.returncode == 0, f"stdout:\n{command.stdout}\nstderr:\n{command.stderr}"
    generated = tmp_path / "autogenerated"
    assert {path.name for path in generated.iterdir()} >= {
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    }
    execution = _execute_nextflow(generated)
    assert execution.returncode == 0, f"stdout:\n{execution.stdout}\nstderr:\n{execution.stderr}"


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf103_t31_python_generated_artifacts_execute(tmp_path: Path) -> None:
    document = {
        "id": "WRITE",
        "class": "CommandLineTool",
        "cwlVersion": "v1.2",
        "baseCommand": "touch",
        "arguments": ["result.txt"],
        "inputs": {},
        "outputs": {"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
    }
    step = Step.from_cwl_document(document, process_name="WRITE")
    workflow = Workflow([step], "pipeline")
    workflow.add_output("result", step.outputs.result)
    workflow.to_nextflow(tmp_path)
    execution = _execute_nextflow(tmp_path)
    assert execution.returncode == 0, f"stdout:\n{execution.stdout}\nstderr:\n{execution.stderr}"
    assert len(list((tmp_path / "work").rglob("result.txt"))) == 1


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
def test_nf106_t62_docker_container_behavior_or_explicit_skip(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("R11 conditional: Docker executable is not installed")
    daemon = subprocess.run(
        [docker, "info"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if daemon.returncode != 0:
        pytest.skip("R11 conditional: Docker daemon is not available")
    process = NfProcess(
        "CONTAINERIZED",
        [],
        [_output("result", "result.txt")],
        _command("touch", "result.txt"),
        container="ubuntu:24.04",
    )
    workflow = _single_process_workflow(
        process,
        params={},
        output_port="result",
    )
    result = _run_nextflow(workflow, tmp_path)
    if result.returncode != 0 and "pull" in result.stdout.lower():
        pytest.skip("R11 conditional: pinned container image is unavailable offline")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# NF-105 — supported DSL2 reader and structural import.


@pytest.mark.fast
def test_nf105_t51_generated_source_roundtrips_through_reader(tmp_path: Path) -> None:
    expected = _runtime_workflow()
    write_nextflow_artifacts(expected, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert isinstance(parsed, NextflowDocument)
    assert parsed.name == expected.name
    assert [process.name for process in parsed.processes] == [
        process.name for process in expected.processes
    ]
    assert parsed.connections == expected.connections
    assert dict(parsed.params) == dict(expected.params)
    assert parsed.opaque_regions == ()
    assert parsed.verified_executable == expected
    assert promote_nextflow_document(parsed) == expected
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        render_nextflow(cast(Any, parsed))


@pytest.mark.fast
def test_nf105_t51_source_without_verified_ir_cannot_be_promoted() -> None:
    parsed = parse_nf_text(render_nextflow(_runtime_workflow()))
    assert parsed.opaque_regions == ()
    with pytest.raises(ValueError, match="matching validated executable IR artifact"):
        promote_nextflow_document(parsed)


@pytest.mark.fast
def test_nf105_t51_promotion_rechecks_provenance_instead_of_trusting_field_name() -> None:
    parsed = parse_nf_text(render_nextflow(_runtime_workflow()))
    different = ExecutableNextflowWorkflow("DIFFERENT", (), (), {})
    forged = replace(parsed, verified_executable=different)
    with pytest.raises(ValueError, match="source does not match"):
        promote_nextflow_document(forged)


@pytest.mark.fast
def test_nf105_t51_reader_rejects_tampered_source_or_parameters(tmp_path: Path) -> None:
    workflow = _runtime_workflow()
    write_nextflow_artifacts(workflow, tmp_path)
    script = tmp_path / "workflow.nf"
    script.write_text(
        script.read_text(encoding="utf-8").replace("copy.txt", "changed.txt", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match its executable IR"):
        parse_nf_file(script)

    write_nextflow_artifacts(workflow, tmp_path)
    params = tmp_path / "nextflow_params.json"
    params.write_text('{"message": "changed"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="parameters do not match"):
        parse_nf_file(script)


@pytest.mark.fast
def test_nf105_t51_reader_rejects_invalid_or_stale_ir_artifact(tmp_path: Path) -> None:
    write_nextflow_artifacts(_runtime_workflow(), tmp_path)
    ir_path = tmp_path / "nextflow_workflow.json"
    payload = json.loads(ir_path.read_text(encoding="utf-8"))
    payload["representation_kind"] = "structural"
    ir_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="representation kind"):
        parse_nf_file(tmp_path / "workflow.nf")


@pytest.mark.fast
def test_nf105_t51_reader_retains_process_and_global_unknown_content() -> None:
    source = """nextflow.enable.dsl=2
include { HELPER } from './helper'
process TASK {
    errorStrategy 'retry'
    output:
    path "result.txt", emit: result
    script:
    \"\"\"
    touch result.txt
    \"\"\"
}
workflow PIPELINE {
    main:
    TASK()
    emit:
    result = TASK.out.result
    onComplete { println 'done' }
}
workflow {
    PIPELINE()
}
"""
    parsed = parse_nf_text(source)
    opaque = "\n".join(parsed.opaque_regions)
    assert "include { HELPER }" in opaque
    assert "onComplete" in opaque
    assert "errorStrategy" in opaque
    assert render_nextflow_document(parsed) == source
    with pytest.raises(ValueError, match="opaque regions"):
        promote_nextflow_document(parsed)


@pytest.mark.fast
def test_nf105_t51_reader_extracts_ports_container_and_resources() -> None:
    source = render_nextflow(ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess(
            "TASK",
            [NfPort("source", "path")],
            [_output("result", "result.txt")],
            NfCommand((
                NfTemplate((NfLiteral("cp"),)),
                NfTemplate((NfInputReference("source"),)),
                NfTemplate((NfLiteral("result.txt"),)),
            )),
            container="ubuntu:24.04",
            resources=NfResources(cpus=2, memory_mb=512),
        )],
        [
            NfWorkflowInputConnection("source", "TASK", "source"),
            NfWorkflowOutputConnection("TASK", "result", "result"),
        ],
        {"source": "input.txt"},
    ))
    parsed = parse_nf_text(source, params={"source": "input.txt"})
    process = parsed.processes[0]
    assert process.container == "ubuntu:24.04"
    assert process.cpus == "2"
    assert process.memory == "512 MB"
    assert process.inputs == (NextflowPort("source", "path"),)
    assert process.outputs == (
        NextflowPort("result", "path", "result", "result.txt"),
    )


@pytest.mark.fast
def test_nf105_t51_reader_rejects_cycles_and_missing_processes() -> None:
    cyclic = """nextflow.enable.dsl=2
process A {
    input:
    val value
    output:
    val value, emit: out
    script:
    \"\"\"
    true
    \"\"\"
}
process B {
    input:
    val value
    output:
    val value, emit: out
    script:
    \"\"\"
    true
    \"\"\"
}
workflow WF {
    main:
    A(B.out.out)
    B(A.out.out)
}
workflow { WF() }
"""
    with pytest.raises(ValueError, match="cycle"):
        parse_nf_text(cyclic)

    with pytest.raises(ValueError, match="unknown process"):
        parse_nf_text(cyclic.replace("B(A.out.out)", "B(MISSING.out.out)"))


@pytest.mark.fast
def test_nf105_t52_nextflow_to_cwl_preserves_structure() -> None:
    structural = parse_nf_text(render_nextflow(_runtime_workflow()))
    workflow_cwl, tools = nextflow_to_cwl(structural)
    assert [tool["id"] for tool in tools] == ["PRODUCE", "COPY"]
    assert tools[1]["inputs"]["source"]["type"] == "File"
    assert workflow_cwl["steps"][1]["in"]["source"] == "PRODUCE/result"
    assert workflow_cwl["outputs"]["result"]["outputSource"] == "COPY/copy"


@pytest.mark.fast
def test_nf105_t52_public_import_reconstructs_sophios_workflow(tmp_path: Path) -> None:
    write_nextflow_artifacts(_runtime_workflow(), tmp_path)
    with pytest.warns(UserWarning, match="opaque"):
        imported = import_nextflow(tmp_path / "workflow.nf")
    assert isinstance(imported, Workflow)
    assert imported.process_name == "PIPELINE"
    assert [step.process_name for step in imported.steps] == ["PRODUCE", "COPY"]
    imported_produce = cast(Step, imported.steps[0])
    imported_copy = cast(Step, imported.steps[1])
    assert imported_copy.inputs.source.source_parameter is imported_produce.outputs.result
    compiled = imported.compile()
    assert isinstance(compiled, CompiledWorkflow)
    assert list(compiled.cwl_workflow["steps"])


# NF-106 — structural and executable round trips.


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf105_t52_and_nf106_t61_reader_writer_runtime_roundtrip(tmp_path: Path) -> None:
    original_dir = tmp_path / "original"
    regenerated_dir = tmp_path / "regenerated"
    original = _run_nextflow(_runtime_workflow(), original_dir)
    assert original.returncode == 0, original.stderr
    parsed = parse_nf_file(original_dir / "workflow.nf")
    regenerated = _run_nextflow(promote_nextflow_document(parsed), regenerated_dir)
    assert regenerated.returncode == 0, regenerated.stderr
    original_outputs = [path.read_text(encoding="utf-8") for path in (original_dir / "work").rglob("copy.txt")]
    regenerated_outputs = [
        path.read_text(encoding="utf-8") for path in (regenerated_dir / "work").rglob("copy.txt")
    ]
    assert original_outputs == regenerated_outputs == ["hello from Sophios"]


@pytest.mark.nextflow
@pytest.mark.serial
def test_nf106_t61_json_hydration_preserves_runtime_behavior(tmp_path: Path) -> None:
    workflow = _runtime_workflow()
    hydrated = ExecutableNextflowWorkflow.from_json(workflow.to_json())
    result = _run_nextflow(hydrated, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("copy.txt")] == [
        "hello from Sophios"
    ]


@pytest.mark.fast
def test_nf106_t62_concrete_public_module_exports_importer() -> None:
    from sophios.api.python import nextflow

    assert nextflow.import_nextflow is import_nextflow
    assert nextflow.NextflowDocument is NextflowDocument
    assert nextflow.promote_nextflow_document is promote_nextflow_document
    assert nextflow.render_nextflow_document is render_nextflow_document
