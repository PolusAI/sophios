"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
import re
from typing import Any, cast

import pytest

from sophios.api.python.workflow import CompiledWorkflow
from sophios.input_output_nf import render_nextflow
from sophios.nf_types import NfFlag, NfLiteral, NfResources, NfTemplate
from sophios.utils_nf import cwl_rosetree_to_nextflow
from sophios.wic_types import RoseTree

from .testkit import node_data, step, synthetic_rose, tool, workflow_doc


@pytest.mark.fast
def test_real_unsupported_rosetree_aggregates_capability_errors(
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
def test_rejects_every_unconsumed_tool_field_before_lowering() -> None:
    unconsumed = tool(
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
    rose = synthetic_rose(
        workflow_doc(
            [step("UNCONSUMED", **{"in": {"reference": "reference"}, "out": ["result"]})],
            inputs={"reference": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "UNCONSUMED/result"}},
        ),
        [unconsumed],
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
@pytest.mark.parametrize("value", [False, True])
def test_accepts_boolean_flag_bindings_for_both_values(value: bool) -> None:
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "prefix": "--verbose"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": "boolean"},
        ),
        [flags],
        workflow_inputs={"verbose": value},
    )

    assert cwl_rosetree_to_nextflow(rose).params == {"verbose": value}


@pytest.mark.fast
@pytest.mark.parametrize("value", [False, True])
def test_rejects_value_from_on_a_boolean_binding(value: bool) -> None:
    """CWL applies boolean semantics after valueFrom; Phase 2 has no such lowering."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {
                    "position": 1,
                    "prefix": "--verbose",
                    "valueFrom": "$(inputs.verbose)",
                },
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": value},
    )

    with pytest.raises(ValueError, match=r"valueFrom.*boolean"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_file_output_wired_into_a_boolean_flag() -> None:
    """A staged path is always truthy in Groovy, so the flag would never clear."""
    producer = tool("MAKE", outputs={"out": {"type": "File", "outputBinding": {"glob": "f.txt"}}})
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_rose(
        workflow_doc([
            step("MAKE", out=["out"]),
            step("SORT", **{"in": {"reverse": "MAKE/out"}}),
        ]),
        [producer, consumer],
    )

    with pytest.raises(ValueError, match=r"reverse.*boolean source"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize("supplied", ["false", "0", ""])
def test_rejects_string_source_wired_into_a_boolean_flag(supplied: str) -> None:
    """Groovy truthiness of a string would decide the flag, not the source value."""
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "flagval"}})],
            inputs={"flagval": {"type": "string"}},
        ),
        [consumer],
        workflow_inputs={"flagval": supplied},
    )

    with pytest.raises(ValueError, match=r"reverse.*boolean source"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_a_boolean_source_wired_into_a_boolean_flag() -> None:
    """The supported wiring stays supported: boolean source into a flag input."""
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "flagval"}})],
            inputs={"flagval": {"type": "boolean"}},
        ),
        [consumer],
        workflow_inputs={"flagval": False},
    )

    assert cwl_rosetree_to_nextflow(rose).params == {"flagval": False}


@pytest.mark.fast
def test_rejects_absent_optional_boolean_flag() -> None:
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": ["null", "boolean"],
                "inputBinding": {"position": 1, "prefix": "--verbose"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": ["null", "boolean"]}},
        ),
        [flags],
        workflow_inputs={"verbose": None},
    )

    with pytest.raises(ValueError, match="absent optional"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_ignores_inert_documentation_but_not_semantics() -> None:
    baseline_tool = tool(
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

    workflow = workflow_doc(
        [step("IDENTITY", **{"in": {"source": "source"}, "out": ["result"]})],
        inputs={"source": {"type": "File"}},
        outputs={"result": {"type": "File", "outputSource": "IDENTITY/result"}},
    )
    workflow_inputs = {"source": {"class": "File", "path": "source.txt"}}
    baseline = cwl_rosetree_to_nextflow(
        synthetic_rose(workflow, [baseline_tool], workflow_inputs=workflow_inputs)
    )
    documented = cwl_rosetree_to_nextflow(
        synthetic_rose(workflow, [documented_tool], workflow_inputs=workflow_inputs)
    )

    assert documented == baseline
    assert render_nextflow(documented) == render_nextflow(baseline)

    documented_tool["permanentFailCodes"] = [1]
    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(
            synthetic_rose(workflow, [documented_tool], workflow_inputs=workflow_inputs)
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
def test_rejects_compiledworkflow_substitution() -> None:
    compiled = CompiledWorkflow("wf", workflow_doc([]), {})
    with pytest.raises(TypeError, match="RoseTree"):
        cwl_rosetree_to_nextflow(cast(Any, compiled))


@pytest.mark.fast
def test_requires_workflow_root() -> None:
    rose = RoseTree(node_data("tool", tool("tool")), [])
    with pytest.raises(ValueError, match="root.*Workflow"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_nested_or_unsupported_workflow_constructs() -> None:
    nested = workflow_doc([])
    rose = synthetic_rose(workflow_doc([step("nested")]), [nested])
    with pytest.raises(ValueError, match="nested workflows.*Phase 2"):
        cwl_rosetree_to_nextflow(rose)

    conditional = synthetic_rose(
        workflow_doc([step("conditional", run="tool.cwl", when="$(true)")]),
        [tool("tool")],
    )
    with pytest.raises(ValueError, match="when.*not supported.*Phase 1"):
        cwl_rosetree_to_nextflow(conditional)


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
def test_closed_world_analysis_covers_workflow_and_step_levels(
    mutation: Any,
    diagnostic: str,
) -> None:
    write = tool(
        "WRITE",
        inputs={"message": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
    )
    workflow = workflow_doc(
        [step("WRITE", **{"in": {"message": {"source": "message"}}, "out": ["result"]})],
        inputs={"message": {"type": "string"}},
        outputs={"result": {"type": "File", "outputSource": "WRITE/result"}},
    )
    mutation(workflow)
    rose = synthetic_rose(workflow, [write], workflow_inputs={"message": "hello"})
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
def test_boundary_missingness_is_not_conflated_with_defaults(
    workflow_inputs: dict[str, Any],
    default: Any,
    diagnostic: str,
) -> None:
    definition: dict[str, Any] = {"type": "string"}
    if default is not None:
        definition["default"] = default
    rose = synthetic_rose(
        workflow_doc([], inputs={"message": definition}),
        [],
        workflow_inputs=workflow_inputs,
    )
    with pytest.raises(ValueError, match=diagnostic):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_undeclared_workflow_input_values() -> None:
    rose = synthetic_rose(workflow_doc([]), [], workflow_inputs={"extra": "value"})
    with pytest.raises(ValueError, match="has no declared workflow input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_workflow_input_identifier_collisions() -> None:
    rose = synthetic_rose(
        workflow_doc(
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
def test_aggregates_workflow_input_collisions_with_other_findings() -> None:
    collision = tool("COLLISION")
    rose = synthetic_rose(
        workflow_doc(
            [step("COLLISION", unknown_field=True)],
            inputs={
                "out-dir": {"type": "string"},
                "out_dir": {"type": "string"},
            },
        ),
        [collision],
        workflow_inputs={"out-dir": "first", "out_dir": "second"},
    )

    with pytest.raises(ValueError) as exc_info:
        cwl_rosetree_to_nextflow(rose)

    diagnostic = str(exc_info.value)
    assert diagnostic.startswith("Nextflow Phase 1 capability analysis failed:\n")
    assert "workflow.inputs: workflow input identifiers" in diagnostic
    assert "steps[0].unknown_field" in diagnostic


@pytest.mark.fast
def test_rejects_tool_port_identifier_collisions() -> None:
    collision = tool(
        "COLLISION",
        inputs={
            "out-dir": {"type": "string", "default": "first"},
            "out_dir": {"type": "string", "default": "second"},
        },
    )
    rose = synthetic_rose(workflow_doc([step("COLLISION")]), [collision])
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
def test_rejects_boundary_values_outside_supported_shape(cwl_type: Any, value: Any) -> None:
    rose = synthetic_rose(
        workflow_doc([], inputs={"value": {"type": cwl_type}}),
        [],
        workflow_inputs={"value": value},
    )
    with pytest.raises(ValueError, match="does not match its supported CWL type"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_input_before_lowering() -> None:
    optional = tool("OPTIONAL", inputs={"message": {"type": ["null", "string"]}})
    rose = synthetic_rose(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [optional],
        workflow_inputs={"message": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.message.*absent optional"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_workflow_input_feeding_required_input() -> None:
    required = tool("REQUIRED", inputs={"message": {"type": "string"}})
    rose = synthetic_rose(
        workflow_doc(
            [step("REQUIRED", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [required],
        workflow_inputs={},
    )
    with pytest.raises(ValueError, match="absent required"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_basename_against_a_value_input() -> None:
    basename = tool(
        "BASENAME",
        inputs={"label": {"type": "string"}},
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "$(inputs.label.basename).txt"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("BASENAME", **{"in": {"label": "label"}, "out": ["result"]})],
            inputs={"label": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "BASENAME/result"}},
        ),
        [basename],
        workflow_inputs={"label": "input"},
    )
    with pytest.raises(ValueError, match="basename.*path"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_output_glob_outside_typed_input_subset() -> None:
    bad_glob = tool(
        "BAD_GLOB",
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "$(runtime.outdir)/result.txt"},
            }
        },
    )
    rose = synthetic_rose(workflow_doc([step("BAD_GLOB", out=["result"])]), [bad_glob])
    with pytest.raises(ValueError, match="unsupported CWL expression"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_executable_scatter_before_lowering() -> None:
    scatter = tool("SCATTER", inputs={"item": {"type": "string"}})
    scattered_step = step(
        "SCATTER",
        **{"in": {"item": "items"}, "scatter": "item", "scatterMethod": "dotproduct"},
    )
    rose = synthetic_rose(
        workflow_doc(
            [scattered_step],
            inputs={"items": {"type": {"type": "array", "items": "string"}}},
        ),
        [scatter],
        workflow_inputs={"items": ["a", "b"]},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].scatter.*Phase 2"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_mixed_container_execution_policy() -> None:
    containerized = tool(
        "CONTAINERIZED",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    host = tool("HOST")
    rose = synthetic_rose(
        workflow_doc([step("CONTAINERIZED"), step("HOST")]),
        [containerized, host],
    )
    with pytest.raises(ValueError, match="mixed container execution is not supported"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_fractional_cpu_requirements_before_lowering() -> None:
    resources = tool(
        "RESOURCES",
        requirements={"ResourceRequirement": {"coresMax": 2.5}},
    )
    rose = synthetic_rose(workflow_doc([step("RESOURCES")]), [resources])

    with pytest.raises(ValueError, match=r"coresMax.*whole number"):
        cwl_rosetree_to_nextflow(rose)
    with pytest.raises(ValueError, match="cpus must be a positive integer"):
        NfResources(cpus=cast(Any, 2.5))


@pytest.mark.fast
def test_rejects_unknown_connection_source() -> None:
    consumer = tool("B", inputs={"value": {"type": "File"}})
    rose = synthetic_rose(
        workflow_doc([step("B", **{"in": {"value": "MISSING/out"}})]),
        [consumer],
    )
    with pytest.raises(ValueError, match="unknown source process"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_separate_without_a_prefix() -> None:
    """cwltool raises for separate without prefix, so the backend must not accept it."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "separate": False},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_separate_true_without_a_prefix_on_a_boolean_binding() -> None:
    """cwltool accepts separate: true without a prefix; only a falsy separate rejects."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "separate": True},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    assert cwl_rosetree_to_nextflow(rose).params == {"verbose": True}


@pytest.mark.fast
def test_rejects_separate_null_without_a_prefix_on_a_boolean_binding() -> None:
    """cwltool's own .get("separate", True) only defaults an absent key; separate: null still rejects."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "separate": None},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_separate_null_without_a_prefix_on_a_string_binding() -> None:
    """The general _binding_tokens path rejects separate: null the same way the boolean path does."""
    echo = tool(
        "ECHO",
        inputs={
            "message": {
                "type": "string",
                "inputBinding": {"position": 1, "separate": None},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("ECHO", **{"in": {"message": "message"}})],
            inputs={"message": {"type": "string"}},
        ),
        [echo],
        workflow_inputs={"message": "hi"},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_whitespace_only_prefix_on_a_boolean_binding() -> None:
    """A blank prefix must hit the named capability diagnostic, not NfFlag's own invariant."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "prefix": "   "},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="CWL command prefix for 'verbose' must be a non-empty string"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize("raw_source", [[], 42])
def test_rejects_a_flag_input_wired_to_an_unrecognized_source_shape(raw_source: Any) -> None:
    """A shape _source_values cannot read must still fail closed, not silently skip the boolean-source check."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "prefix": "--verbose"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc([step("FLAGS", **{"in": {"verbose": raw_source}})]),
        [flags],
    )

    with pytest.raises(ValueError):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_a_command_of_only_flags_still_runs_a_program() -> None:
    """A flag-only argv would render an empty script that silently exits zero."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {"position": 1, "prefix": "--verbose"},
            }
        },
        baseCommand=None,
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    tokens = cwl_rosetree_to_nextflow(rose).processes[0].command.tokens

    assert tokens[0] == NfTemplate((NfLiteral("true"),))
    assert tokens[1] == NfFlag("verbose", "--verbose")
