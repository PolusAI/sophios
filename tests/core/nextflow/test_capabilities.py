"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
import re
from typing import Any, cast

import pytest

from sophios.api.python.workflow import CompiledWorkflow
from sophios.input_output_nf import render_nextflow
from sophios.nf_types import (
    NfFlag,
    NfProcessConnection,
    NfLiteral,
    NfResources,
    NfShellLiteral,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow
from sophios.wic_types import RoseTree, Yaml

from .testkit import (
    node_data,
    step,
    subworkflow_child,
    synthetic_rose,
    tool,
    workflow_doc,
)


@pytest.mark.fast
def test_real_unsupported_rosetree_aggregates_capability_errors(
    unsupported_real_linear_rose: RoseTree,
) -> None:
    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(unsupported_real_linear_rose)
    message = str(error.value)
    # append.cwl's InitialWorkDirRequirement listing ($(inputs.file), staging
    # under its own basename) is the approved self-staging no-op shape, so it
    # no longer produces a finding; its two dynamic-value shellQuote:false
    # bindings still do.
    assert "steps[1].run.inputs.str.inputBinding.shellQuote" in message
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
def test_accepts_self_referencing_value_from_on_a_boolean_binding(value: bool) -> None:
    """A bare $(inputs.<name>) valueFrom restating its own input lowers to the same flag."""
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

    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.params == {"verbose": value}
    assert converted.processes[0].command.tokens[-1] == NfFlag("verbose", "--verbose")


@pytest.mark.fast
def test_rejects_value_from_aliasing_a_different_input_on_a_boolean_binding() -> None:
    """Aliasing a different input's presence onto this flag has no approved lowering."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {
                    "position": 1,
                    "prefix": "--verbose",
                    "valueFrom": "$(inputs.other)",
                },
            },
            "other": {"type": "boolean"},
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose", "other": "other"}})],
            inputs={"verbose": {"type": "boolean"}, "other": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True, "other": False},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_literal_value_from_on_a_boolean_binding() -> None:
    """A literal string is not a provably-boolean valueFrom result."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {
                    "position": 1,
                    "prefix": "--verbose",
                    "valueFrom": "true",
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
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_basename_suffixed_value_from_on_a_boolean_binding() -> None:
    """A .basename suffix is never boolean-shaped, even on a self-reference."""
    flags = tool(
        "FLAGS",
        inputs={
            "verbose": {
                "type": "boolean",
                "inputBinding": {
                    "position": 1,
                    "prefix": "--verbose",
                    "valueFrom": "$(inputs.verbose.basename)",
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
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
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
def test_accepts_absent_optional_boolean_flag() -> None:
    """A flag never dereferences its value, so absence renders identically to false."""
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

    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.params == {"verbose": []}
    assert converted.processes[0].command.tokens[-1] == NfFlag("verbose", "--verbose")


@pytest.mark.fast
def test_rejects_absent_optional_flag_that_is_also_dereferenced() -> None:
    """A flag use does not excuse a template use of the same input.

    The flag only tests its value, but the template dereferences it, so
    absence would render the sentinel into the command line as `--label=[]`.
    """
    both = tool(
        "BOTH",
        inputs={
            "verbose": {
                "type": ["null", "boolean"],
                "inputBinding": {"position": 1, "prefix": "--verbose"},
            }
        },
        arguments=[{"position": 2, "valueFrom": "--label=$(inputs.verbose)"}],
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("BOTH", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": ["null", "boolean"]}},
        ),
        [both],
        workflow_inputs={"verbose": None},
    )

    assert _findings(rose) == [
        "steps[0].run.inputs.verbose: absent optional values are supported only for a "
        "val input that is unreferenced in its command, or drives a boolean flag and "
        "is referenced nowhere else"
    ]


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
def test_rejects_unsupported_workflow_constructs() -> None:
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
        (
            lambda workflow: workflow.update(
                {"requirements": {"MultipleInputFeatureRequirement": {}}}
            ),
            "workflow.requirements.MultipleInputFeatureRequirement",
        ),
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
def test_accepts_unreferenced_absent_optional_input() -> None:
    """An optional val input with no inputBinding is never dereferenced, so absence is safe."""
    optional = tool("OPTIONAL", inputs={"message": {"type": ["null", "string"]}})
    rose = synthetic_rose(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [optional],
        workflow_inputs={"message": None},
    )
    assert cwl_rosetree_to_nextflow(rose).params == {"message": []}


@pytest.mark.fast
def test_rejects_absent_optional_input_bound_directly_into_the_command() -> None:
    """A general presence-gated value binding (the value analogue of a flag) is deferred."""
    optional = tool(
        "OPTIONAL",
        inputs={
            "message": {
                "type": ["null", "string"],
                "inputBinding": {"position": 1, "prefix": "--message"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [optional],
        workflow_inputs={"message": None},
    )
    with pytest.raises(
        ValueError,
        match=r"steps\[0\].run.inputs.message: absent optional values are supported only for "
        "a val input that is unreferenced in its command, or drives a boolean flag and is referenced nowhere else",
    ):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_input_referenced_via_another_bindings_value_from() -> None:
    """An optional input with no binding of its own but referenced elsewhere still rejects."""
    aliased = tool(
        "ALIASED",
        inputs={"message": {"type": ["null", "string"]}},
        arguments=[{"position": 1, "valueFrom": "$(inputs.message)"}],
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("ALIASED", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [aliased],
        workflow_inputs={"message": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.message: absent optional"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_path_input() -> None:
    """path-qualifier channel construction always stages unconditionally; absence stays rejected."""
    optional = tool("OPTIONAL", inputs={"reference": {"type": ["null", "File"]}})
    rose = synthetic_rose(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"reference": "reference"}})],
            inputs={"reference": {"type": ["null", "File"]}},
        ),
        [optional],
        workflow_inputs={"reference": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.reference: absent optional"):
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
    assert _findings(rose) == [
        "steps[0].run.outputs.result.outputBinding.glob: $(inputs.label.basename) "
        "requires a File or Directory input; label lowers to a val channel"
    ]


@pytest.mark.fast
def test_reports_every_basename_against_a_value_input_by_path() -> None:
    basename = tool(
        "BASENAME",
        inputs={"label": {"type": "string"}, "tag": {"type": "string"}},
        outputs={
            "result": {
                "type": "File",
                "outputBinding": {"glob": "$(inputs.label.basename).txt"},
            }
        },
        stdout="$(inputs.tag.basename).log",
    )
    rose = synthetic_rose(
        workflow_doc(
            [
                step(
                    "BASENAME",
                    **{"in": {"label": "label", "tag": "tag"}, "out": ["result"]},
                )
            ],
            inputs={"label": {"type": "string"}, "tag": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "BASENAME/result"}},
        ),
        [basename],
        workflow_inputs={"label": "input", "tag": "name"},
    )
    assert _findings(rose) == [
        "steps[0].run.stdout: $(inputs.tag.basename) requires a File or Directory "
        "input; tag lowers to a val channel",
        "steps[0].run.outputs.result.outputBinding.glob: $(inputs.label.basename) "
        "requires a File or Directory input; label lowers to a val channel",
    ]


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


def _findings(rose: RoseTree) -> list[str]:
    """Return the exact aggregated finding lines of one rejected conversion."""
    with pytest.raises(ValueError) as error:
        cwl_rosetree_to_nextflow(rose)
    header, *lines = str(error.value).splitlines()
    assert header == "Nextflow Phase 1 capability analysis failed:"
    return [line.removeprefix("- ") for line in lines]


_STRING_ARRAY = {"type": "array", "items": "string"}


def _scattered_tool(port_type: Any = "string", **inputs: Any) -> Yaml:
    return tool(
        "SCATTER",
        inputs={
            "item": {"type": port_type, "inputBinding": {"position": 1}},
            **inputs,
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )


def _scatter_rose(
    *,
    port_type: Any = "string",
    items: Any = "string",
    source: Any = "items",
    scatter: Any = ("item",),
    value: Any = ("a", "b"),
    **step_fields: Any,
) -> RoseTree:
    fields: dict[str, Any] = {
        "in": {"item": source},
        "out": ["result"],
        "scatter": list(scatter) if isinstance(scatter, tuple) else scatter,
        **step_fields,
    }
    return synthetic_rose(
        workflow_doc(
            [step("SCATTER", **fields)],
            inputs={"items": {"type": {"type": "array", "items": items}}},
        ),
        [_scattered_tool(port_type)],
        workflow_inputs={"items": list(value)},
    )


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_rejects_multi_input_scatter_under_every_scatter_method(method: str) -> None:
    """Multi-input scatter is the only shape where scatterMethod is load-bearing."""
    scatter_tool = tool(
        "SCATTER",
        inputs={
            "first": {"type": "string", "inputBinding": {"position": 1}},
            "second": {"type": "string", "inputBinding": {"position": 2}},
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step(
                "SCATTER",
                **{
                    "in": {"first": "firsts", "second": "seconds"},
                    "out": ["result"],
                    "scatter": ["first", "second"],
                    "scatterMethod": method,
                },
            )],
            inputs={
                "firsts": {"type": _STRING_ARRAY},
                "seconds": {"type": _STRING_ARRAY},
            },
        ),
        [scatter_tool],
        workflow_inputs={"firsts": ["a"], "seconds": ["b"]},
    )

    assert _findings(rose) == [
        "steps[0].scatter: multi-input scatter over 2 inputs is deferred beyond this "
        "lowering; exactly one scattered input is supported"
    ]


@pytest.mark.fast
@pytest.mark.parametrize("scatter", [[], {"item": True}, "", ["item", ""]], ids=[
    "empty-list", "mapping", "empty-string", "empty-name",
])
def test_rejects_a_scatter_field_that_does_not_name_one_input(scatter: Any) -> None:
    assert _findings(_scatter_rose(scatter=scatter)) == [
        "steps[0].scatter: scatter must name one input, as a string or a one-element list"
    ]


@pytest.mark.fast
def test_rejects_an_unsupported_scatter_method() -> None:
    assert _findings(_scatter_rose(scatterMethod="product")) == [
        "steps[0].scatterMethod: unsupported CWL scatter method 'product'"
    ]


@pytest.mark.fast
@pytest.mark.parametrize(
    "step_fields",
    [
        {},
        {"scatterMethod": "dotproduct"},
        {"scatterMethod": "flat_crossproduct"},
        {"scatterMethod": "nested_crossproduct"},
    ],
    ids=["absent", "dotproduct", "flat_crossproduct", "nested_crossproduct"],
)
def test_accepts_an_inert_scatter_method_at_one_scattered_input(
    step_fields: dict[str, Any],
) -> None:
    """All three methods coincide at one scattered input, so each is inert."""
    workflow = cwl_rosetree_to_nextflow(_scatter_rose(**step_fields))

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "scatter"),
    )


@pytest.mark.fast
@pytest.mark.parametrize("scatter", ["item", ("item",)], ids=["string-form", "list-form"])
def test_accepts_both_single_input_scatter_spellings(scatter: Any) -> None:
    workflow = cwl_rosetree_to_nextflow(_scatter_rose(scatter=scatter))

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "scatter"),
    )


@pytest.mark.fast
def test_rejects_scatter_over_a_non_array_workflow_input() -> None:
    rose = synthetic_rose(
        workflow_doc(
            [step("SCATTER", **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": "string"}},
        ),
        [_scattered_tool()],
        workflow_inputs={"items": "a"},
    )

    assert _findings(rose) == [
        "steps[0].in.item: a scattered input must be sourced from an array-typed "
        "workflow input; 'items' declares 'string'"
    ]


@pytest.mark.fast
def test_rejects_scatter_over_an_array_typed_port() -> None:
    assert _findings(_scatter_rose(port_type=_STRING_ARRAY)) == [
        "steps[0].scatter: scattered input 'item' is array-typed; scattering over an "
        "array of arrays is deferred beyond this lowering"
    ]


@pytest.mark.fast
def test_rejects_scatter_whose_element_type_mismatches_its_port() -> None:
    assert _findings(_scatter_rose(port_type="File")) == [
        "steps[0].in.item: scattered source 'items' carries 'val' elements but the port "
        "takes 'path[file]'"
    ]


@pytest.mark.fast
def test_rejects_scatter_naming_an_undeclared_input() -> None:
    assert _findings(_scatter_rose(scatter=("missing",))) == [
        "steps[0].scatter: scattered input 'missing' is not declared by the step's tool"
    ]


@pytest.mark.fast
def test_rejects_an_unwired_scattered_input() -> None:
    rose = synthetic_rose(
        workflow_doc(
            [step("SCATTER", **{"out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": _STRING_ARRAY}},
        ),
        [_scattered_tool()],
        workflow_inputs={"items": ["a"]},
    )

    assert _findings(rose) == [
        "steps[0].scatter: scattered input 'item' has no source; a scattered input must "
        "be wired to an array-typed workflow input"
    ]


def _producer() -> Yaml:
    return tool(
        "PRODUCER",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )


@pytest.mark.fast
def test_rejects_scatter_over_a_process_output() -> None:
    """No process output can carry an array, so a queue source truncates the scatter."""
    rose = synthetic_rose(
        workflow_doc([
            step("PRODUCER", out=["out"]),
            step(
                "SCATTER",
                **{"in": {"item": "PRODUCER/out"}, "out": ["result"], "scatter": ["item"]},
            ),
        ]),
        [_producer(), _scattered_tool("File")],
    )

    assert _findings(rose) == [
        "steps[1].in.item: a scattered step's inputs must come from workflow inputs; the "
        "process output 'PRODUCER/out' would truncate the scatter to one task"
    ]


@pytest.mark.fast
def test_rejects_a_process_output_source_on_a_scattered_steps_other_input() -> None:
    scatter_tool = _scattered_tool(
        extra={"type": "File", "inputBinding": {"position": 2}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [
                step("PRODUCER", out=["out"]),
                step(
                    "SCATTER",
                    **{
                        "in": {"item": "items", "extra": "PRODUCER/out"},
                        "out": ["result"],
                        "scatter": ["item"],
                    },
                ),
            ],
            inputs={"items": {"type": _STRING_ARRAY}},
        ),
        [_producer(), scatter_tool],
        workflow_inputs={"items": ["a"]},
    )

    assert _findings(rose) == [
        "steps[1].in.extra: a scattered step's inputs must come from workflow inputs; the "
        "process output 'PRODUCER/out' would truncate the scatter to one task"
    ]


@pytest.mark.fast
def test_rejects_a_downstream_process_consumer_of_a_scattered_step() -> None:
    """A queue channel of N drives N downstream tasks where CWL gives one an array."""
    consumer = tool(
        "CONSUMER",
        inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "copy.txt"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [
                step(
                    "SCATTER",
                    **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]},
                ),
                step("CONSUMER", **{"in": {"source": "SCATTER/result"}, "out": ["result"]}),
            ],
            inputs={"items": {"type": _STRING_ARRAY}},
        ),
        [_scattered_tool(), consumer],
        workflow_inputs={"items": ["a"]},
    )

    assert _findings(rose) == [
        "steps[1].in.source: 'SCATTER/result' is an output of scattered step steps[0]; a "
        "scattered step's outputs can only reach a workflow output, because gathering "
        "them back into one value is deferred beyond this lowering"
    ]


@pytest.mark.fast
def test_accepts_a_workflow_output_of_a_scattered_step() -> None:
    rose = synthetic_rose(
        workflow_doc(
            [step("SCATTER", **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": _STRING_ARRAY}},
            outputs={
                "each": {
                    "type": {"type": "array", "items": "File"},
                    "outputSource": "SCATTER/result",
                }
            },
        ),
        [_scattered_tool()],
        workflow_inputs={"items": ["a", "b"]},
    )

    workflow = cwl_rosetree_to_nextflow(rose)

    assert NfWorkflowOutputConnection("SCATTER", "result", "each") in workflow.connections


@pytest.mark.fast
def test_accepts_a_scattered_boolean_flag_source() -> None:
    """A scattered flag port receives one element, so the element type is what must be boolean."""
    flags = tool(
        "SCATTER",
        inputs={"item": {"type": "boolean", "inputBinding": {"prefix": "--flag"}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("SCATTER", **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": {"type": "array", "items": "boolean"}}},
        ),
        [flags],
        workflow_inputs={"items": [True, False]},
    )

    workflow = cwl_rosetree_to_nextflow(rose)

    assert workflow.processes[0].command.tokens[-1] == NfFlag("item", "--flag")


@pytest.mark.fast
@pytest.mark.parametrize(
    "requirements",
    [{"ScatterFeatureRequirement": {}}, [{"class": "ScatterFeatureRequirement"}]],
    ids=["mapping", "list"],
)
def test_accepts_inert_workflow_level_scatter_requirement(requirements: Any) -> None:
    rose = _scatter_rose()
    rose.data.compiled_cwl["requirements"] = requirements

    assert cwl_rosetree_to_nextflow(rose).connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "scatter"),
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    ("requirements", "diagnostic"),
    [
        (
            {"ScatterFeatureRequirement": {"method": "dotproduct"}},
            "workflow.requirements.ScatterFeatureRequirement.method: method is not "
            "consumed by Nextflow Phase 1 lowering",
        ),
        (
            {"StepInputExpressionRequirement": {}},
            "workflow.requirements.StepInputExpressionRequirement: "
            "StepInputExpressionRequirement is not supported at the Nextflow workflow level",
        ),
        (
            "ScatterFeatureRequirement",
            "workflow.requirements: CWL Workflow requirements must be a mapping or list",
        ),
    ],
    ids=["unconsumed-field", "unsupported-class", "wrong-shape"],
)
def test_rejects_unsupported_workflow_level_requirements(
    requirements: Any,
    diagnostic: str,
) -> None:
    rose = _scatter_rose()
    rose.data.compiled_cwl["requirements"] = requirements

    assert _findings(rose) == [diagnostic]


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


def _array_tool(items: Any, **binding: Any) -> Yaml:
    return tool(
        "ARRAY",
        inputs={
            "values": {
                "type": {"type": "array", "items": items},
                "inputBinding": {"position": 1, **binding},
            }
        },
    )


def _array_rose(items: Any, value: Any, **binding: Any) -> RoseTree:
    return synthetic_rose(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": {"type": "array", "items": items}}},
        ),
        [_array_tool(items, **binding)],
        workflow_inputs={"values": value},
    )


def _array_rose_from_producer(items: Any, **binding: Any) -> RoseTree:
    """Wire the array-typed input from a producing step, bypassing boundary-value matching.

    Isolates a type-shape rejection (an unsupported items schema) from the
    separate, and less specific, boundary-value-shape rejection.
    """
    producer = tool("PRODUCER", outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}})
    return synthetic_rose(
        workflow_doc([
            step("PRODUCER", out=["out"]),
            step("ARRAY", **{"in": {"values": "PRODUCER/out"}}),
        ]),
        [producer, _array_tool(items, **binding)],
    )


@pytest.mark.fast
def test_rejects_nested_arrays() -> None:
    rose = _array_rose_from_producer({"type": "array", "items": "string"})
    with pytest.raises(ValueError, match="nested arrays are deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_per_item_input_binding_on_array_items() -> None:
    rose = _array_rose_from_producer({"type": "File", "inputBinding": {"prefix": "-I"}})
    with pytest.raises(ValueError, match="per-item array element bindings are deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_per_item_input_binding_on_the_array_schema() -> None:
    """CWL's common spelling puts the per-item binding beside items, not inside it.

    Accepting this form rendered `-B a b`, silently dropping the per-item
    prefix cwltool renders as `-B -A a -A b`.
    """
    array_tool = tool(
        "ARRAY",
        inputs={
            "values": {
                "type": {
                    "type": "array",
                    "items": "string",
                    "inputBinding": {"prefix": "-A"},
                },
                "inputBinding": {"position": 1, "prefix": "-B"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": {"type": "array", "items": "string"}}},
        ),
        [array_tool],
        workflow_inputs={"values": ["a", "b"]},
    )
    with pytest.raises(ValueError, match="per-item array element bindings are deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_value_from_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a"], valueFrom="$(inputs.values)")
    with pytest.raises(ValueError, match=r"valueFrom on an array-typed inputBinding.*deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_item_separator_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a", "b"], itemSeparator=",")
    with pytest.raises(ValueError, match=r"itemSeparator.*not consumed by Nextflow Phase 1"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_separate_false_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a", "b"], separate=False)
    with pytest.raises(ValueError, match=r"separate: false on an array-typed inputBinding.*deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_array_typed_outputs() -> None:
    array_output = tool(
        "MAKE",
        outputs={
            "results": {
                "type": {"type": "array", "items": "File"},
                "outputBinding": {"glob": "*.txt"},
            }
        },
    )
    rose = synthetic_rose(workflow_doc([step("MAKE", out=["results"])]), [array_output])
    with pytest.raises(ValueError, match=r"results\.type: primitive and non-path output capture"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_array_input() -> None:
    """Absence (no array at all) is out of scope for the array lowering; empty is not absence."""
    optional_array = tool(
        "ARRAY",
        inputs={
            "values": {
                "type": ["null", {"type": "array", "items": "string"}],
                "inputBinding": {"position": 1, "prefix": "--value"},
            }
        },
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": ["null", {"type": "array", "items": "string"}]}},
        ),
        [optional_array],
        workflow_inputs={"values": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.values: absent optional"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_array_value_with_a_mismatched_item_type() -> None:
    rose = _array_rose("string", ["a", 1])
    with pytest.raises(ValueError, match="does not match its supported CWL type"):
        cwl_rosetree_to_nextflow(rose)


def _shell_tool(arguments: list[Any], *, shell_command: bool = True) -> Yaml:
    extra: dict[str, Any] = {"arguments": arguments}
    if shell_command:
        extra["requirements"] = {"ShellCommandRequirement": {}}
    return tool(
        "SHELL",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        **extra,
    )


def _shell_rose(arguments: list[Any], *, shell_command: bool = True) -> RoseTree:
    return synthetic_rose(
        workflow_doc(
            [step("SHELL", out=["result"])],
            outputs={"result": {"type": "File", "outputSource": "SHELL/result"}},
        ),
        [_shell_tool(arguments, shell_command=shell_command)],
    )


@pytest.mark.fast
def test_accepts_shell_command_requirement_with_no_shell_quote_false() -> None:
    """ShellCommandRequirement alone changes nothing: no shellQuote:false, nothing to reject."""
    rose = _shell_rose(["hello"])
    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.processes[0].command.tokens[0] == NfTemplate((NfLiteral("SHELL"),))


@pytest.mark.fast
def test_rejects_shell_quote_false_without_shell_command_requirement() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False}], shell_command=False)
    with pytest.raises(ValueError, match="requires ShellCommandRequirement"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_shell_quote_false_with_no_value_from() -> None:
    rose = _shell_rose([{"shellQuote": False}])
    with pytest.raises(ValueError, match="has no valueFrom"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_shell_quote_false_with_a_prefix() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False, "prefix": "-x"}])
    with pytest.raises(ValueError, match="does not support a prefix"):
        cwl_rosetree_to_nextflow(rose)


def _shell_tool_with_input(value_from: str) -> Yaml:
    return tool(
        "SHELL",
        inputs={"message": {"type": "string"}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"ShellCommandRequirement": {}},
        arguments=[{"valueFrom": value_from, "shellQuote": False}],
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    "value_from",
    ["$(inputs.message)", "prefix-$(inputs.message)-suffix", "$(inputs.message.basename)"],
    ids=["bare", "embedded", "basename-suffix"],
)
def test_rejects_shell_quote_false_referencing_an_input(value_from: str) -> None:
    """Unquoting a value derived from any input -- directly or via a suffix -- is the boundary."""
    rose = synthetic_rose(
        workflow_doc(
            [step("SHELL", **{"in": {"message": "message"}, "out": ["result"]})],
            inputs={"message": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "SHELL/result"}},
        ),
        [_shell_tool_with_input(value_from)],
        workflow_inputs={"message": "hi"},
    )
    with pytest.raises(ValueError, match="references an input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_shell_quote_false_input_binding_with_no_value_from() -> None:
    """The real append.cwl adapter's shape: shellQuote:false on a plain, no-valueFrom inputBinding."""
    append_tool = tool(
        "APPEND",
        inputs={
            "text": {
                "type": "string",
                "inputBinding": {"shellQuote": False, "position": 1, "prefix": "echo"},
            }
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"ShellCommandRequirement": {}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("APPEND", **{"in": {"text": "text"}, "out": ["result"]})],
            inputs={"text": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "APPEND/result"}},
        ),
        [append_tool],
        workflow_inputs={"text": "Hello"},
    )
    with pytest.raises(ValueError, match="does not support a prefix"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_literal_shell_quote_false_binding() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False}, "out.txt"])
    converted = cwl_rosetree_to_nextflow(rose)
    assert NfShellLiteral(">>") in converted.processes[0].command.tokens


def _iwdr_tool(
    listing: list[Any],
    *,
    extra_inputs: Yaml | None = None,
    arguments: list[Any] | None = None,
) -> Yaml:
    inputs: dict[str, Any] = {"source": {"type": "File"}}
    if extra_inputs:
        inputs.update(extra_inputs)
    return tool(
        "STAGE",
        inputs=inputs,
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"InitialWorkDirRequirement": {"listing": listing}},
        arguments=arguments or ["cat", "renamed.txt"],
        stdout="out.txt",
    )


def _iwdr_rose(listing: list[Any], **kwargs: Any) -> RoseTree:
    return synthetic_rose(
        workflow_doc(
            [step("STAGE", **{"in": {"source": "source"}, "out": ["result"]})],
            inputs={"source": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "STAGE/result"}},
        ),
        [_iwdr_tool(listing, **kwargs)],
        workflow_inputs={"source": {"class": "File", "path": "in.txt"}},
    )


@pytest.mark.fast
def test_accepts_iwdr_bare_shorthand_own_basename() -> None:
    """The real append.cwl shape: a bare $(inputs.<name>) listing entry is a no-op."""
    rose = _iwdr_rose(["$(inputs.source)"])
    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as is None


@pytest.mark.fast
def test_accepts_iwdr_dirent_self_basename_entryname() -> None:
    """The tool_builder .stage() default: entryname restates the input's own basename."""
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "$(inputs.source.basename)"}])
    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as is None


@pytest.mark.fast
def test_accepts_iwdr_dirent_literal_rename() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "renamed.txt"}])
    converted = cwl_rosetree_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as == "renamed.txt"


@pytest.mark.fast
def test_rejects_iwdr_writable_true() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "renamed.txt", "writable": True}])
    with pytest.raises(ValueError, match="writable: true"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_inline_content_entry() -> None:
    rose = _iwdr_rose([{"entry": "literal file content", "entryname": "x.txt"}])
    with pytest.raises(ValueError, match="inline content construction is deferred"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_computed_entryname() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "$(inputs.source.path)"}])
    with pytest.raises(ValueError, match="computed or differently-referencing entryname"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_entryname_with_path_separator() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "sub/dir.txt"}])
    with pytest.raises(ValueError, match="path separator"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_undeclared_input() -> None:
    rose = _iwdr_rose(["$(inputs.nope)"])
    with pytest.raises(ValueError, match="undeclared input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_val_typed_target() -> None:
    rose = _iwdr_rose(["$(inputs.name)"], extra_inputs={"name": {"type": "string"}})
    with pytest.raises(ValueError, match="non-path input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_array_typed_target() -> None:
    rose = _iwdr_rose(
        ["$(inputs.files)"],
        extra_inputs={"files": {"type": {"type": "array", "items": "File"}}},
    )
    with pytest.raises(ValueError, match="array-typed input"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_unsupported_dirent_field() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "x.txt", "foo": 1}])
    with pytest.raises(ValueError, match="unsupported Dirent fields"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_listing_entry_of_unsupported_shape() -> None:
    rose = _iwdr_rose([123])
    with pytest.raises(ValueError, match="bare .* reference or a Dirent mapping"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_listing_that_is_not_a_list() -> None:
    broken = tool(
        "STAGE",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"InitialWorkDirRequirement": {"listing": "not-a-list"}},
    )
    rose = synthetic_rose(
        workflow_doc([step("STAGE", out=["result"])]),
        [broken],
    )
    with pytest.raises(ValueError, match="listing must be a list"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_renamed_input_referenced_elsewhere() -> None:
    rose = _iwdr_rose(
        [{"entry": "$(inputs.source)", "entryname": "renamed.txt"}],
        arguments=["cat", "$(inputs.source)"],
    )
    with pytest.raises(ValueError, match="staged under an explicit rename"):
        cwl_rosetree_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_collision_same_literal_name() -> None:
    collision_tool = tool(
        "STAGE",
        inputs={"a": {"type": "File"}, "b": {"type": "File"}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"InitialWorkDirRequirement": {"listing": [
            {"entry": "$(inputs.a)", "entryname": "same.txt"},
            {"entry": "$(inputs.b)", "entryname": "same.txt"},
        ]}},
        arguments=["cat", "same.txt"],
        stdout="out.txt",
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("STAGE", **{"in": {"a": "a", "b": "b"}, "out": ["result"]})],
            inputs={"a": {"type": "File"}, "b": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "STAGE/result"}},
        ),
        [collision_tool],
        workflow_inputs={
            "a": {"class": "File", "path": "a.txt"},
            "b": {"class": "File", "path": "b.txt"},
        },
    )
    with pytest.raises(ValueError, match="staged under the same literal name"):
        cwl_rosetree_to_nextflow(rose)


def _inner_tool(name: str = "INNER", **fields: Any) -> Yaml:
    return tool(
        name,
        inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "copy.txt"}}},
        **fields,
    )


def _child_document(
    *,
    steps: list[Yaml] | None = None,
    inputs: Yaml | None = None,
    outputs: Yaml | None = None,
    **fields: Any,
) -> Yaml:
    document = workflow_doc(
        steps if steps is not None
        else [step("INNER", **{"in": {"source": "source"}, "out": ["result"]})],
        inputs=inputs if inputs is not None else {"source": {"type": "File"}},
        outputs=outputs if outputs is not None
        else {"inner_result": {"type": "File", "outputSource": "INNER/result"}},
    )
    document["id"] = "child"
    document.update(fields)
    return document


def _nested_rose(
    *,
    child: Yaml | None = None,
    inner_tools: list[Yaml] | None = None,
    step_fields: dict[str, Any] | None = None,
    outputs: Yaml | None = None,
) -> RoseTree:
    fields: dict[str, Any] = {
        "in": {"source": "reference"},
        "out": ["inner_result"],
        **(step_fields or {}),
    }
    return synthetic_rose(
        workflow_doc(
            [step("CHILD", **fields)],
            inputs={"reference": {"type": "File"}},
            outputs=outputs,
        ),
        [subworkflow_child(child or _child_document(), inner_tools or [_inner_tool()])],
        workflow_inputs={"reference": "reference.txt"},
    )


@pytest.mark.fast
def test_inlines_a_subworkflow_step_into_a_namespaced_process() -> None:
    """One level of nesting lowers by inlining, with the outer step as the namespace."""
    workflow = cwl_rosetree_to_nextflow(_nested_rose(
        outputs={
            "copied": {"type": "File", "outputSource": "CHILD/inner_result"},
        },
    ))

    assert [process.name for process in workflow.processes] == ["CHILD___INNER"]
    assert workflow.connections == (
        NfWorkflowInputConnection("reference", "CHILD___INNER", "source"),
        NfWorkflowOutputConnection("CHILD___INNER", "result", "copied"),
    )


@pytest.mark.fast
def test_an_empty_subworkflow_inlines_to_no_processes() -> None:
    rose = synthetic_rose(
        workflow_doc([step("CHILD", **{"in": {}, "out": []})]),
        [subworkflow_child(workflow_doc([]), [])],
    )

    assert cwl_rosetree_to_nextflow(rose).processes == ()


@pytest.mark.fast
def test_two_instantiations_of_one_subworkflow_do_not_collide() -> None:
    """Namespacing by the outer step id is what keeps inner names unique."""
    rose = synthetic_rose(
        workflow_doc(
            [
                step("FIRST", **{"in": {"source": "reference"}, "out": ["inner_result"]}),
                step("SECOND", **{"in": {"source": "reference"}, "out": ["inner_result"]}),
            ],
            inputs={"reference": {"type": "File"}},
        ),
        [
            subworkflow_child(_child_document(), [_inner_tool()]),
            subworkflow_child(_child_document(), [_inner_tool()]),
        ],
        workflow_inputs={"reference": "reference.txt"},
    )

    workflow = cwl_rosetree_to_nextflow(rose)

    assert [process.name for process in workflow.processes] == [
        "FIRST___INNER",
        "SECOND___INNER",
    ]


@pytest.mark.fast
def test_a_subworkflow_output_reaches_a_downstream_outer_step() -> None:
    consumer = tool(
        "CONSUMER",
        inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [
                step("CHILD", **{"in": {"source": "reference"}, "out": ["inner_result"]}),
                step("CONSUMER", **{"in": {"source": "CHILD/inner_result"}, "out": ["result"]}),
            ],
            inputs={"reference": {"type": "File"}},
        ),
        [subworkflow_child(_child_document(), [_inner_tool()]), consumer],
        workflow_inputs={"reference": "reference.txt"},
    )

    workflow = cwl_rosetree_to_nextflow(rose)

    assert NfProcessConnection(
        "CHILD___INNER", "result", "CONSUMER", "source"
    ) in workflow.connections


@pytest.mark.fast
def test_rejects_nesting_deeper_than_one_level() -> None:
    grandchild = subworkflow_child(_child_document(), [_inner_tool()])
    child = RoseTree(
        node_data("child", _child_document(
            steps=[step("GRANDCHILD", **{"in": {"source": "source"}, "out": ["inner_result"]})],
            outputs={"inner_result": {"type": "File", "outputSource": "GRANDCHILD/inner_result"}},
        )),
        [grandchild],
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("CHILD", **{"in": {"source": "reference"}, "out": ["inner_result"]})],
            inputs={"reference": {"type": "File"}},
        ),
        [child],
        workflow_inputs={"reference": "reference.txt"},
    )

    assert _findings(rose) == [
        "steps[0].run.steps[0].run: nested workflows deeper than one level are deferred "
        "beyond this lowering"
    ]


@pytest.mark.fast
def test_rejects_scatter_on_a_subworkflow_step() -> None:
    assert _findings(_nested_rose(step_fields={"scatter": ["source"]})) == [
        "steps[0].scatter: scatter on a nested workflow step is deferred beyond this "
        "lowering; scattering an inlined sub-DAG is not the single-process shape scatter "
        "supports"
    ]


@pytest.mark.fast
def test_rejects_when_on_a_subworkflow_step() -> None:
    assert _findings(_nested_rose(step_fields={"when": "$(true)"})) == [
        "steps[0].when: CWL step when conditions are not supported in Nextflow Phase 1"
    ]


@pytest.mark.fast
def test_rejects_an_unbound_subworkflow_input() -> None:
    assert _findings(_nested_rose(step_fields={"in": {}})) == [
        "steps[0].run.inputs.source: the step does not bind subworkflow input 'source'; "
        "a subworkflow input is never defaulted from outside"
    ]


@pytest.mark.fast
def test_rejects_a_step_input_naming_no_subworkflow_input() -> None:
    assert _findings(_nested_rose(
        step_fields={"in": {"source": "reference", "extra": "reference"}},
    )) == [
        "steps[0].in.extra: the subworkflow declares no input named 'extra'"
    ]


@pytest.mark.fast
def test_rejects_an_out_name_the_subworkflow_does_not_declare() -> None:
    assert _findings(_nested_rose(step_fields={"out": ["inner_result", "missing"]})) == [
        "steps[0].out: the subworkflow declares no output named 'missing'"
    ]


@pytest.mark.fast
def test_rejects_a_subworkflow_output_forwarding_its_own_input() -> None:
    child = _child_document(
        outputs={"inner_result": {"type": "File", "outputSource": "source"}},
    )

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.outputs.inner_result: subworkflow output 'inner_result' forwards "
        "subworkflow input 'source'; boundary passthrough is not executable",
        "steps[0].out: the subworkflow declares no output named 'inner_result'",
    ]


@pytest.mark.fast
def test_rejects_a_subworkflow_output_naming_no_inner_step() -> None:
    child = _child_document(
        outputs={"inner_result": {"type": "File", "outputSource": "ABSENT/result"}},
    )

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.outputs.inner_result: outputSource 'ABSENT/result' names no step of "
        "the subworkflow",
        "steps[0].out: the subworkflow declares no output named 'inner_result'",
    ]


@pytest.mark.fast
def test_rejects_an_inner_step_source_that_is_neither_bound_nor_inner() -> None:
    child = _child_document(
        steps=[step("INNER", **{"in": {"source": "unbound"}, "out": ["result"]})],
    )

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.steps[0].in.source: 'unbound' is not a subworkflow input"
    ]


@pytest.mark.fast
def test_rejects_an_inner_step_source_naming_no_inner_step() -> None:
    child = _child_document(
        steps=[step("INNER", **{"in": {"source": "ABSENT/result"}, "out": ["result"]})],
    )

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.steps[0].in.source: 'ABSENT/result' names no step of the subworkflow"
    ]


@pytest.mark.fast
def test_accepts_an_inert_subworkflow_feature_requirement() -> None:
    rose = _nested_rose(child=_child_document(requirements={"SubworkflowFeatureRequirement": {}}))
    rose.data.compiled_cwl["requirements"] = {"SubworkflowFeatureRequirement": {}}

    assert [process.name for process in cwl_rosetree_to_nextflow(rose).processes] == [
        "CHILD___INNER"
    ]


@pytest.mark.fast
def test_rejects_unsupported_subworkflow_level_requirements() -> None:
    child = _child_document(requirements={"MultipleInputFeatureRequirement": {}})

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.requirements.MultipleInputFeatureRequirement: "
        "MultipleInputFeatureRequirement is not supported at the Nextflow workflow level"
    ]


@pytest.mark.fast
def test_rejects_every_unconsumed_subworkflow_field() -> None:
    child = _child_document(hints={"ResourceRequirement": {}})

    assert _findings(_nested_rose(child=child)) == [
        "steps[0].run.hints: hints is not consumed by Nextflow Phase 1 lowering"
    ]


@pytest.mark.fast
def test_composition_findings_aggregate_across_nested_steps() -> None:
    """Composition analysis reports every nested step before the flat graph is built."""
    rose = synthetic_rose(
        workflow_doc(
            [
                step("FIRST", **{"in": {}, "out": ["inner_result"]}),
                step("SECOND", **{"in": {}, "out": ["inner_result"]}),
            ],
            inputs={"reference": {"type": "File"}},
        ),
        [
            subworkflow_child(_child_document(), [_inner_tool()]),
            subworkflow_child(_child_document(), [_inner_tool()]),
        ],
        workflow_inputs={"reference": "reference.txt"},
    )

    assert _findings(rose) == [
        "steps[0].run.inputs.source: the step does not bind subworkflow input 'source'; "
        "a subworkflow input is never defaulted from outside",
        "steps[1].run.inputs.source: the step does not bind subworkflow input 'source'; "
        "a subworkflow input is never defaulted from outside",
    ]


@pytest.mark.fast
def test_a_scattered_step_inside_a_subworkflow_uses_the_outer_scatter_contract() -> None:
    """After inlining, an inner scattered step is an ordinary scattered step."""
    inner = tool(
        "INNER",
        inputs={"item": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    child = _child_document(
        steps=[step(
            "INNER",
            **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]},
        )],
        inputs={"items": {"type": _STRING_ARRAY}},
        outputs={"inner_result": {"type": "File", "outputSource": "INNER/result"}},
    )
    rose = synthetic_rose(
        workflow_doc(
            [step("CHILD", **{"in": {"items": "values"}, "out": ["inner_result"]})],
            inputs={"values": {"type": _STRING_ARRAY}},
        ),
        [subworkflow_child(child, [inner])],
        workflow_inputs={"values": ["a", "b"]},
    )

    workflow = cwl_rosetree_to_nextflow(rose)

    assert workflow.connections == (
        NfWorkflowInputConnection("values", "CHILD___INNER", "item", "scatter"),
    )
