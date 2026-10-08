"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
from collections.abc import Mapping
from dataclasses import replace
import re
from typing import Any, cast

import pytest

from sophios import compiler
from sophios.api.python import _workflow_runtime as workflow_runtime
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.tool_builder import (
    CommandLineTool,
    Input,
    Inputs,
    Output,
    Outputs,
    cwl,
)
from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.input_output_nf import render_nextflow
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import Diagnostics, EdgeDef, EdgeRef, OutputBinding, ParseResult
from sophios.nf_types import (
    NfFlag,
    NfPort,
    NfProcessConnection,
    NfLiteral,
    NfResources,
    NfShellLiteral,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.utils_nf import (
    _ADMITTED_OUTPUT_EVAL,
    _NAMED_OUTPUT_EVAL_REJECTIONS,
    _output_capture,
    CompiledNextflowSource,
    compilation_result_source,
    compiled_source_to_nextflow,
)
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import Yaml

from .testkit import (
    REPO_ROOT,
    step,
    synthetic_source,
    tool,
    workflow_doc,
)


@pytest.mark.fast
def test_real_unsupported_compilation_aggregates_capability_errors(
    unsupported_real_linear_result: CompilationResult,
) -> None:
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(unsupported_real_linear_result)
    message = str(error.value)
    # append.cwl's InitialWorkDirRequirement listing ($(inputs.file), staging
    # under its own basename) is the approved self-staging no-op shape, so it
    # no longer produces a finding; its two dynamic-value shellQuote:false
    # bindings still do.
    assert "steps[1].run.inputs.str.inputBinding.shellQuote" in message
    assert "steps[1].run.inputs.file.inputBinding.shellQuote" in message
    # cat.cwl's string output is the approved file-text capture shape
    # (loadContents: true, outputEval: $(self[0].contents), literal glob), so
    # it no longer produces a finding at all; the mixed container policy does.
    assert "steps[2].run.outputs.output" not in message
    assert "workflow.steps: mixed container execution is not supported" in message


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
    rose = synthetic_source(
        workflow_doc(
            [step("UNCONSUMED", **{"in": {"reference": "reference"}, "out": ["result"]})],
            inputs={"reference": {"type": "File"}},
            outputs={"result": {"type": "File", "outputSource": "UNCONSUMED/result"}},
        ),
        [unconsumed],
        workflow_inputs={"reference": {"class": "File", "path": "reference.fa"}},
    )

    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(rose)

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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": "boolean"},
        ),
        [flags],
        workflow_inputs={"verbose": value},
    )

    assert compiled_source_to_nextflow(rose).params == {"verbose": value}


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": value},
    )

    converted = compiled_source_to_nextflow(rose)
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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose", "other": "other"}})],
            inputs={"verbose": {"type": "boolean"}, "other": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True, "other": False},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match=r"valueFrom on a boolean inputBinding is supported only as"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_file_output_wired_into_a_boolean_flag() -> None:
    """A staged path is always truthy in Groovy, so the flag would never clear."""
    producer = tool("MAKE", outputs={"out": {"type": "File", "outputBinding": {"glob": "f.txt"}}})
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_source(
        workflow_doc([
            step("MAKE", out=["out"]),
            step("SORT", **{"in": {"reverse": "MAKE/out"}}),
        ]),
        [producer, consumer],
    )

    with pytest.raises(ValueError, match=r"reverse.*boolean source"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize("supplied", ["false", "0", ""])
def test_rejects_string_source_wired_into_a_boolean_flag(supplied: str) -> None:
    """Groovy truthiness of a string would decide the flag, not the source value."""
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_source(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "flagval"}})],
            inputs={"flagval": {"type": "string"}},
        ),
        [consumer],
        workflow_inputs={"flagval": supplied},
    )

    with pytest.raises(ValueError, match=r"reverse.*boolean source"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_a_boolean_source_wired_into_a_boolean_flag() -> None:
    """The supported wiring stays supported: boolean source into a flag input."""
    consumer = tool(
        "SORT",
        inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}},
    )
    rose = synthetic_source(
        workflow_doc(
            [step("SORT", **{"in": {"reverse": "flagval"}})],
            inputs={"flagval": {"type": "boolean"}},
        ),
        [consumer],
        workflow_inputs={"flagval": False},
    )

    assert compiled_source_to_nextflow(rose).params == {"flagval": False}


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": ["null", "boolean"]}},
        ),
        [flags],
        workflow_inputs={"verbose": None},
    )

    converted = compiled_source_to_nextflow(rose)
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
    rose = synthetic_source(
        workflow_doc(
            [step("BOTH", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": ["null", "boolean"]}},
        ),
        [both],
        workflow_inputs={"verbose": None},
    )

    assert _findings(rose) == [
        "steps[0].run.inputs.verbose: absent optional values are supported only for a "
        "val input whose absence leaves command rendering unchanged"
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
    baseline = compiled_source_to_nextflow(
        synthetic_source(workflow, [baseline_tool], workflow_inputs=workflow_inputs)
    )
    documented = compiled_source_to_nextflow(
        synthetic_source(workflow, [documented_tool], workflow_inputs=workflow_inputs)
    )

    assert documented == baseline
    assert render_nextflow(documented) == render_nextflow(baseline)

    documented_tool["permanentFailCodes"] = [1]
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(
            synthetic_source(workflow, [documented_tool], workflow_inputs=workflow_inputs)
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
    with pytest.raises(TypeError, match="CompilationResult"):
        compiled_source_to_nextflow(cast(Any, compiled))


@pytest.mark.fast
def test_requires_workflow_root() -> None:
    with pytest.raises(ValueError, match="root.*Workflow"):
        CompiledNextflowSource("tool", tool("tool"), (), {})


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
    rose = synthetic_source(workflow, [write], workflow_inputs={"message": "hello"})
    with pytest.raises(ValueError, match=re.escape(diagnostic)):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc([], inputs={"message": definition}),
        [],
        workflow_inputs=workflow_inputs,
    )
    with pytest.raises(ValueError, match=diagnostic):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_undeclared_workflow_input_values() -> None:
    rose = synthetic_source(workflow_doc([]), [], workflow_inputs={"extra": "value"})
    with pytest.raises(ValueError, match="has no declared workflow input"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_workflow_input_identifier_collisions() -> None:
    rose = synthetic_source(
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
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_aggregates_workflow_input_collisions_with_other_findings() -> None:
    collision = tool("COLLISION")
    rose = synthetic_source(
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
        compiled_source_to_nextflow(rose)

    diagnostic = str(exc_info.value)
    assert diagnostic.startswith("Nextflow capability analysis failed:\n")
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
    rose = synthetic_source(workflow_doc([step("COLLISION")]), [collision])
    with pytest.raises(
        ValueError,
        match=r"tool input identifiers 'out-dir', 'out_dir'.*normalize to 'out_dir'",
    ):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc([], inputs={"value": {"type": cwl_type}}),
        [],
        workflow_inputs={"value": value},
    )
    with pytest.raises(ValueError, match="does not match its supported CWL type"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_unreferenced_absent_optional_input() -> None:
    """An optional val input with no inputBinding is never dereferenced, so absence is safe."""
    optional = tool("OPTIONAL", inputs={"message": {"type": ["null", "string"]}})
    rose = synthetic_source(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [optional],
        workflow_inputs={"message": None},
    )
    assert compiled_source_to_nextflow(rose).params == {"message": []}


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
    rose = synthetic_source(
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
        "a val input whose absence leaves command rendering unchanged",
    ):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_input_whose_binding_emits_a_shell_literal() -> None:
    """CWL omits the entire binding when its optional source is absent."""
    optional = tool(
        "OPTIONAL",
        inputs={
            "message": {
                "type": ["null", "string"],
                "inputBinding": {
                    "position": 1,
                    "valueFrom": "printf optional",
                    "shellQuote": False,
                },
            }
        },
        requirements={"ShellCommandRequirement": {}},
    )
    rose = synthetic_source(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [optional],
        workflow_inputs={"message": None},
    )

    with pytest.raises(
        ValueError,
        match=r"steps\[0\].run.inputs.message: absent optional values are supported only for ",
    ):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_input_referenced_via_another_bindings_value_from() -> None:
    """An optional input with no binding of its own but referenced elsewhere still rejects."""
    aliased = tool(
        "ALIASED",
        inputs={"message": {"type": ["null", "string"]}},
        arguments=[{"position": 1, "valueFrom": "$(inputs.message)"}],
    )
    rose = synthetic_source(
        workflow_doc(
            [step("ALIASED", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [aliased],
        workflow_inputs={"message": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.message: absent optional"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_path_input() -> None:
    """path-qualifier channel construction always stages unconditionally; absence stays rejected."""
    optional = tool("OPTIONAL", inputs={"reference": {"type": ["null", "File"]}})
    rose = synthetic_source(
        workflow_doc(
            [step("OPTIONAL", **{"in": {"reference": "reference"}})],
            inputs={"reference": {"type": ["null", "File"]}},
        ),
        [optional],
        workflow_inputs={"reference": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.reference: absent optional"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_absent_optional_workflow_input_feeding_required_input() -> None:
    required = tool("REQUIRED", inputs={"message": {"type": "string"}})
    rose = synthetic_source(
        workflow_doc(
            [step("REQUIRED", **{"in": {"message": "message"}})],
            inputs={"message": {"type": ["null", "string"]}},
        ),
        [required],
        workflow_inputs={},
    )
    with pytest.raises(ValueError, match="absent required"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
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
        "requires a File or Directory input, and label is declared 'string'"
    ]


@pytest.mark.fast
@pytest.mark.parametrize("suffix", ["path", "basename"])
@pytest.mark.parametrize(("cwl_type", "value"), [("string", "x"), ("int", 1), ("float", 1.5), ("boolean", True)])
def test_path_and_basename_require_a_file_or_directory_input(suffix: str, cwl_type: str, value: Any) -> None:
    reference = f"$(inputs.value.{suffix})"
    suffixed = tool(
        "SUFFIXED",
        inputs={"value": {"type": cwl_type, "inputBinding": {"prefix": "-v", "valueFrom": reference}}},
        stdout=f"{reference}.log",
    )
    rose = synthetic_source(
        workflow_doc([step("SUFFIXED", **{"in": {"value": "value"}})], inputs={"value": cwl_type}),
        [suffixed],
        workflow_inputs={"value": value},
    )
    reason = f"{reference} requires a File or Directory input, and value is declared {cwl_type!r}"
    assert {
        f"steps[0].run.inputs.value.inputBinding.valueFrom: {reason}",
        f"steps[0].run.stdout: {reason}",
    } <= set(_findings(rose))


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
    rose = synthetic_source(workflow_doc([step("BAD_GLOB", out=["result"])]), [bad_glob])
    with pytest.raises(ValueError, match="unsupported CWL expression"):
        compiled_source_to_nextflow(rose)


def _findings(rose: CompiledNextflowSource) -> list[str]:
    """Return the exact aggregated finding lines of one rejected conversion."""
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(rose)
    header, *lines = str(error.value).splitlines()
    assert header == "Nextflow capability analysis failed:"
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
) -> CompiledNextflowSource:
    fields: dict[str, Any] = {
        "in": {"item": source},
        "out": ["result"],
        "scatter": list(scatter) if isinstance(scatter, tuple) else scatter,
        **step_fields,
    }
    return synthetic_source(
        workflow_doc(
            [step("SCATTER", **fields)],
            inputs={"items": {"type": {"type": "array", "items": items}}},
        ),
        [_scattered_tool(port_type)],
        workflow_inputs={"items": list(value)},
    )


def _two_input_scatter_rose(*, scatterMethod: Any = "dotproduct") -> CompiledNextflowSource:
    scatter_tool = tool(
        "SCATTER",
        inputs={
            "first": {"type": "string", "inputBinding": {"position": 1}},
            "second": {"type": "string", "inputBinding": {"position": 2}},
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    fields: dict[str, Any] = {
        "in": {"first": "firsts", "second": "seconds"},
        "out": ["result"],
        "scatter": ["first", "second"],
    }
    if scatterMethod is not None:
        fields["scatterMethod"] = scatterMethod
    return synthetic_source(
        workflow_doc(
            [step("SCATTER", **fields)],
            inputs={
                "firsts": {"type": _STRING_ARRAY},
                "seconds": {"type": _STRING_ARRAY},
            },
        ),
        [scatter_tool],
        workflow_inputs={"firsts": ["a"], "seconds": ["b"]},
    )


@pytest.mark.fast
def test_accepts_multi_input_scatter_under_every_method() -> None:
    for method in ("dotproduct", "flat_crossproduct", "nested_crossproduct"):
        compiled_source_to_nextflow(_two_input_scatter_rose(scatterMethod=method))


@pytest.mark.fast
def test_rejects_multi_input_scatter_without_a_scatter_method() -> None:
    assert _findings(_two_input_scatter_rose(scatterMethod=None)) == [
        "steps[0].scatterMethod: multi-input scatter over 2 inputs requires an explicit "
        "scatterMethod"
    ]


@pytest.mark.fast
def test_accepts_two_input_dotproduct_scatter() -> None:
    workflow = compiled_source_to_nextflow(_two_input_scatter_rose(scatterMethod="dotproduct"))

    assert set(workflow.connections) >= {
        NfWorkflowInputConnection("firsts", "SCATTER", "first", "dotproduct"),
        NfWorkflowInputConnection("seconds", "SCATTER", "second", "dotproduct"),
    }


@pytest.mark.fast
@pytest.mark.parametrize("scatter", [[], {"item": True}, "", ["item", ""]], ids=[
    "empty-list", "mapping", "empty-string", "empty-name",
])
def test_rejects_a_scatter_field_that_does_not_name_one_input(scatter: Any) -> None:
    assert _findings(_scatter_rose(scatter=scatter)) == [
        "steps[0].scatter: scatter must name one input, as a string or a non-empty list"
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
    workflow = compiled_source_to_nextflow(_scatter_rose(**step_fields))

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "dotproduct"),
    )


@pytest.mark.fast
@pytest.mark.parametrize("scatter", ["item", ("item",)], ids=["string-form", "list-form"])
def test_accepts_both_single_input_scatter_spellings(scatter: Any) -> None:
    workflow = compiled_source_to_nextflow(_scatter_rose(scatter=scatter))

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "dotproduct"),
    )


@pytest.mark.fast
def test_rejects_scatter_over_a_non_array_workflow_input() -> None:
    rose = synthetic_source(
        workflow_doc(
            [step("SCATTER", **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": "string"}},
        ),
        [_scattered_tool()],
        workflow_inputs={"items": "a"},
    )

    assert _findings(rose) == [
        "steps[0].in.item: a scattered input needs an array-typed source; 'items' declares 'string'"
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
    rose = synthetic_source(
        workflow_doc(
            [step("SCATTER", **{"out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": _STRING_ARRAY}},
        ),
        [_scattered_tool()],
        workflow_inputs={"items": ["a"]},
    )

    assert _findings(rose) == [
        "steps[0].scatter: scattered input 'item' has no source; wire it to an "
        "array-typed workflow input or a scattered step's output"
    ]


def _producer() -> Yaml:
    return tool(
        "PRODUCER",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )


@pytest.mark.fast
def test_rejects_scatter_over_a_process_output() -> None:
    """No process output can carry an array, so a queue source truncates the scatter."""
    rose = synthetic_source(
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
        "steps[1].in.item: a scattered step takes inputs from workflow inputs or scattered steps; "
        "'PRODUCER/out', from a step that is not scattered, would truncate the scatter to one task"
    ]


@pytest.mark.fast
def test_rejects_a_process_output_source_on_a_scattered_steps_other_input() -> None:
    scatter_tool = _scattered_tool(
        extra={"type": "File", "inputBinding": {"position": 2}},
    )
    rose = synthetic_source(
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
        "steps[1].in.extra: a scattered step takes inputs from workflow inputs or scattered steps; "
        "'PRODUCER/out', from a step that is not scattered, would truncate the scatter to one task"
    ]


@pytest.mark.fast
def test_rejects_a_scalar_consumer_of_a_scattered_step() -> None:
    """A scattered step's output is an array; a scalar port cannot take it."""
    consumer = tool(
        "CONSUMER",
        inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "copy.txt"}}},
    )
    rose = synthetic_source(
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

    with pytest.raises(ValueError, match="gathers into a port that is not array-typed"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_a_workflow_output_of_a_scattered_step() -> None:
    rose = synthetic_source(
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

    workflow = compiled_source_to_nextflow(rose)

    assert NfWorkflowOutputConnection("SCATTER", "result", "each") in workflow.connections


@pytest.mark.fast
def test_accepts_a_scattered_boolean_flag_source() -> None:
    """A scattered flag port receives one element, so the element type is what must be boolean."""
    flags = tool(
        "SCATTER",
        inputs={"item": {"type": "boolean", "inputBinding": {"prefix": "--flag"}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    rose = synthetic_source(
        workflow_doc(
            [step("SCATTER", **{"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]})],
            inputs={"items": {"type": {"type": "array", "items": "boolean"}}},
        ),
        [flags],
        workflow_inputs={"items": [True, False]},
    )

    workflow = compiled_source_to_nextflow(rose)

    assert workflow.processes[0].command.tokens[-1] == NfFlag("item", "--flag")


@pytest.mark.fast
@pytest.mark.parametrize(
    "requirements",
    [{"ScatterFeatureRequirement": {}}, [{"class": "ScatterFeatureRequirement"}]],
    ids=["mapping", "list"],
)
def test_accepts_inert_workflow_level_scatter_requirement(requirements: Any) -> None:
    rose = _scatter_rose()
    cast(dict[str, Any], rose.workflow)["requirements"] = requirements

    assert compiled_source_to_nextflow(rose).connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item", "dotproduct"),
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    ("requirements", "diagnostic"),
    [
        (
            {"ScatterFeatureRequirement": {"method": "dotproduct"}},
            "workflow.requirements.ScatterFeatureRequirement.method: method is not "
            "consumed by the Nextflow lowering",
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
        (
            [{"directive": "${System.exit(3)}"}],
            "workflow.requirements.0: a requirement or hint needs a string `class`",
        ),
    ],
    ids=["unconsumed-field", "unsupported-class", "wrong-shape", "no-class"],
)
def test_rejects_unsupported_workflow_level_requirements(
    requirements: Any,
    diagnostic: str,
) -> None:
    rose = _scatter_rose()
    cast(dict[str, Any], rose.workflow)["requirements"] = requirements

    assert _findings(rose) == [diagnostic]


@pytest.mark.fast
@pytest.mark.parametrize("section", ["requirements", "hints"])
def test_rejects_tool_requirement_without_class(section: str) -> None:
    classless = tool("CLASSLESS")
    classless[section] = [{"directive": "${System.exit(3)}"}]
    rose = synthetic_source(workflow_doc([step("CLASSLESS")]), [classless])

    assert _findings(rose) == [
        f"steps[0].run.{section}.0: a requirement or hint needs a string `class`"
    ]


@pytest.mark.fast
def test_rejects_mixed_container_execution_policy() -> None:
    containerized = tool(
        "CONTAINERIZED",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    host = tool("HOST")
    rose = synthetic_source(
        workflow_doc([step("CONTAINERIZED"), step("HOST")]),
        [containerized, host],
    )
    with pytest.raises(ValueError, match="mixed container execution is not supported"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_fractional_cpu_requirements_before_lowering() -> None:
    resources = tool(
        "RESOURCES",
        requirements={"ResourceRequirement": {"coresMax": 2.5}},
    )
    rose = synthetic_source(workflow_doc([step("RESOURCES")]), [resources])

    with pytest.raises(ValueError, match=r"coresMax.*whole number"):
        compiled_source_to_nextflow(rose)
    with pytest.raises(ValueError, match="cpus must be a positive integer"):
        NfResources(cpus=cast(Any, 2.5))


@pytest.mark.fast
def test_rejects_unknown_connection_source() -> None:
    consumer = tool("B", inputs={"value": {"type": "File"}})
    rose = synthetic_source(
        workflow_doc([step("B", **{"in": {"value": "MISSING/out"}})]),
        [consumer],
    )
    with pytest.raises(ValueError, match="unknown source process"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    assert compiled_source_to_nextflow(rose).params == {"verbose": True}


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("ECHO", **{"in": {"message": "message"}})],
            inputs={"message": {"type": "string"}},
        ),
        [echo],
        workflow_inputs={"message": "hi"},
    )

    with pytest.raises(ValueError, match="separate cannot be specified without a prefix"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    with pytest.raises(ValueError, match="CWL command prefix for 'verbose' must be a non-empty string"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc([step("FLAGS", **{"in": {"verbose": raw_source}})]),
        [flags],
    )

    with pytest.raises(ValueError):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("FLAGS", **{"in": {"verbose": "verbose"}})],
            inputs={"verbose": {"type": "boolean"}},
        ),
        [flags],
        workflow_inputs={"verbose": True},
    )

    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens

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


def _array_rose(items: Any, value: Any, **binding: Any) -> CompiledNextflowSource:
    return synthetic_source(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": {"type": "array", "items": items}}},
        ),
        [_array_tool(items, **binding)],
        workflow_inputs={"values": value},
    )


def _array_rose_from_producer(items: Any, **binding: Any) -> CompiledNextflowSource:
    """Wire the array-typed input from a producing step, bypassing boundary-value matching.

    Isolates a type-shape rejection (an unsupported items schema) from the
    separate, and less specific, boundary-value-shape rejection.
    """
    producer = tool("PRODUCER", outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}})
    return synthetic_source(
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
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_per_item_input_binding_on_array_items() -> None:
    rose = _array_rose_from_producer({"type": "File", "inputBinding": {"prefix": "-I"}})
    with pytest.raises(ValueError, match="per-item array element bindings are deferred"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": {"type": "array", "items": "string"}}},
        ),
        [array_tool],
        workflow_inputs={"values": ["a", "b"]},
    )
    with pytest.raises(ValueError, match="per-item array element bindings are deferred"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_value_from_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a"], valueFrom="$(inputs.values)")
    with pytest.raises(ValueError, match=r"valueFrom on an array-typed inputBinding.*deferred"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_item_separator_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a", "b"], itemSeparator=",")
    with pytest.raises(ValueError, match=r"itemSeparator.*not consumed by the Nextflow lowering"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_separate_false_on_an_array_binding() -> None:
    rose = _array_rose("string", ["a", "b"], separate=False)
    with pytest.raises(ValueError, match=r"separate: false on an array-typed inputBinding.*deferred"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(workflow_doc([step("MAKE", out=["results"])]), [array_output])
    with pytest.raises(ValueError, match=r"results\.type: an output is collected by glob as a File or Directory"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("ARRAY", **{"in": {"values": "values"}})],
            inputs={"values": {"type": ["null", {"type": "array", "items": "string"}]}},
        ),
        [optional_array],
        workflow_inputs={"values": None},
    )
    with pytest.raises(ValueError, match=r"steps\[0\].run.inputs.values: absent optional"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_array_value_with_a_mismatched_item_type() -> None:
    rose = _array_rose("string", ["a", 1])
    with pytest.raises(ValueError, match="does not match its supported CWL type"):
        compiled_source_to_nextflow(rose)


def _shell_tool(arguments: list[Any], *, shell_command: bool = True) -> Yaml:
    extra: dict[str, Any] = {"arguments": arguments}
    if shell_command:
        extra["requirements"] = {"ShellCommandRequirement": {}}
    return tool(
        "SHELL",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        **extra,
    )


def _shell_rose(arguments: list[Any], *, shell_command: bool = True) -> CompiledNextflowSource:
    return synthetic_source(
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
    converted = compiled_source_to_nextflow(rose)
    assert converted.processes[0].command.tokens[0] == NfTemplate((NfLiteral("SHELL"),))


@pytest.mark.fast
def test_rejects_shell_quote_false_without_shell_command_requirement() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False}], shell_command=False)
    with pytest.raises(ValueError, match="requires ShellCommandRequirement"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_shell_quote_false_with_no_value_from() -> None:
    rose = _shell_rose([{"shellQuote": False}])
    with pytest.raises(ValueError, match="has no valueFrom"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_shell_quote_false_with_a_prefix() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False, "prefix": "-x"}])
    with pytest.raises(ValueError, match="does not support a prefix"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("SHELL", **{"in": {"message": "message"}, "out": ["result"]})],
            inputs={"message": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "SHELL/result"}},
        ),
        [_shell_tool_with_input(value_from)],
        workflow_inputs={"message": "hi"},
    )
    with pytest.raises(ValueError, match="references an input"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
        workflow_doc(
            [step("APPEND", **{"in": {"text": "text"}, "out": ["result"]})],
            inputs={"text": {"type": "string"}},
            outputs={"result": {"type": "File", "outputSource": "APPEND/result"}},
        ),
        [append_tool],
        workflow_inputs={"text": "Hello"},
    )
    with pytest.raises(ValueError, match="does not support a prefix"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_accepts_literal_shell_quote_false_binding() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False}, "out.txt"])
    converted = compiled_source_to_nextflow(rose)
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


def _iwdr_rose(listing: list[Any], **kwargs: Any) -> CompiledNextflowSource:
    return synthetic_source(
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
    converted = compiled_source_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as is None


@pytest.mark.fast
def test_accepts_iwdr_dirent_self_basename_entryname() -> None:
    """The tool_builder .stage() default: entryname restates the input's own basename."""
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "$(inputs.source.basename)"}])
    converted = compiled_source_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as is None


@pytest.mark.fast
def test_accepts_iwdr_dirent_literal_rename() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "renamed.txt"}])
    converted = compiled_source_to_nextflow(rose)
    assert converted.processes[0].inputs[0].stage_as == "renamed.txt"


@pytest.mark.fast
def test_rejects_iwdr_writable_true() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "renamed.txt", "writable": True}])
    with pytest.raises(ValueError, match="writable: true"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_inline_content_entry() -> None:
    rose = _iwdr_rose([{"entry": "literal file content", "entryname": "x.txt"}])
    with pytest.raises(ValueError, match="inline content construction is deferred"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_computed_entryname() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "$(inputs.source.path)"}])
    with pytest.raises(ValueError, match="computed or differently-referencing entryname"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_entryname_with_path_separator() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "sub/dir.txt"}])
    with pytest.raises(ValueError, match="path separator"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize("entryname", ["reads*.fq", "sample?.txt"])
def test_rejects_iwdr_entryname_with_nextflow_stage_as_wildcard(entryname: str) -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": entryname}])
    with pytest.raises(ValueError, match="stageAs wildcard"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_undeclared_input() -> None:
    rose = _iwdr_rose(["$(inputs.nope)"])
    with pytest.raises(ValueError, match="undeclared input"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_val_typed_target() -> None:
    rose = _iwdr_rose(["$(inputs.name)"], extra_inputs={"name": {"type": "string"}})
    with pytest.raises(ValueError, match="non-path input"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_array_typed_target() -> None:
    rose = _iwdr_rose(
        ["$(inputs.files)"],
        extra_inputs={"files": {"type": {"type": "array", "items": "File"}}},
    )
    with pytest.raises(ValueError, match="array-typed input"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_unsupported_dirent_field() -> None:
    rose = _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "x.txt", "foo": 1}])
    with pytest.raises(ValueError, match="unsupported Dirent fields"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_listing_entry_of_unsupported_shape() -> None:
    rose = _iwdr_rose([123])
    with pytest.raises(ValueError, match="bare .* reference or a Dirent mapping"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_listing_that_is_not_a_list() -> None:
    broken = tool(
        "STAGE",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"InitialWorkDirRequirement": {"listing": "not-a-list"}},
    )
    rose = synthetic_source(
        workflow_doc([step("STAGE", out=["result"])]),
        [broken],
    )
    with pytest.raises(ValueError, match="listing must be a list"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_rejects_iwdr_renamed_input_referenced_elsewhere() -> None:
    rose = _iwdr_rose(
        [{"entry": "$(inputs.source)", "entryname": "renamed.txt"}],
        arguments=["cat", "$(inputs.source)"],
    )
    with pytest.raises(ValueError, match="staged under an explicit rename"):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
@pytest.mark.parametrize(
    "listing",
    [
        [
            {"entry": "$(inputs.source)", "entryname": "one.txt"},
            {"entry": "$(inputs.source)", "entryname": "two.txt"},
        ],
        [
            "$(inputs.source)",
            {"entry": "$(inputs.source)", "entryname": "renamed.txt"},
        ],
    ],
)
def test_rejects_iwdr_listing_that_repeats_an_input(listing: list[object]) -> None:
    rose = _iwdr_rose(listing)
    with pytest.raises(ValueError, match="input 'source' appears more than once"):
        compiled_source_to_nextflow(rose)


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
    rose = synthetic_source(
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
        compiled_source_to_nextflow(rose)


def _copy_tool() -> CommandLineTool:
    return (
        CommandLineTool(
            "copy_file",
            Inputs(source=Input(cwl.file, position=1)),
            Outputs(result=Output(cwl.file, glob="copy.txt")),
        )
        .base_command("cp")
        .argument("copy.txt", position=2)
    )


def _write_step() -> Step:
    write_tool = (
        CommandLineTool(
            "write_message",
            Inputs(message=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="message.txt")),
        )
        .base_command("echo")
        .stdout("message.txt")
    )
    write = Step(write_tool, step_name="write")
    write.inputs.message = "nested composition"
    return write


def _copy_child(name: str, source: Any) -> Workflow:
    inner = Step(_copy_tool(), step_name="inner_copy")
    child = Workflow([inner], name)
    inner.inputs.source = child.inputs.source
    child.outputs.result = inner.outputs.result
    child.inputs.source = source
    return child


def _nested_result(*, siblings: int = 1) -> CompilationResult:
    write = _write_step()
    children = [_copy_child(f"child_{index}", write.outputs.result) for index in range(siblings)]
    steps: list[Step | Workflow] = [write, *children]
    return compile_workflow_result(Workflow(steps, "root"))


def _deep_nested_result() -> CompilationResult:
    write = _write_step()
    inner = Step(_copy_tool(), step_name="inner_copy")
    leaf = Workflow([inner], "leaf")
    inner.inputs.source = leaf.inputs.source
    middle = Workflow([leaf], "middle")
    leaf.inputs.source = middle.inputs.source
    middle.inputs.source = write.outputs.result
    return compile_workflow_result(Workflow([write, middle], "root"))


def _nested_downstream_result() -> CompilationResult:
    """Compile a child-output edge the Python surface deliberately cannot author yet."""
    write = _write_step()
    child = _copy_child("child", write.outputs.result)
    consume = Step(_copy_tool(), step_name="consume")
    consume.inputs.source = "placeholder.txt"
    root = Workflow([write, child, consume], "root")
    document = workflow_runtime.workflow_document(root, inline_subtrees=False)
    steps = list(document.steps)
    steps[1] = replace(steps[1], outputs=(OutputBinding("result", EdgeDef("childout")),))
    steps[2] = replace(steps[2], inputs=(("source", EdgeRef("childout")),))
    compiler_options, graph_settings, tag_paths = default_compilation_settings()
    tools = workflow_runtime._merged_known_tools(root._flatten_steps())
    bundle = SourceBundle(
        ParseResult(replace(document, steps=tuple(steps)), Diagnostics()),
        "root",
        RegistrySnapshot.from_tools(tools, workflows={
            ("global", child.process_name): ParseResult(
                workflow_runtime.workflow_document(child, inline_subtrees=False), Diagnostics()
            ),
        }),
    )
    return compiler.compile_source(
        bundle,
        compiler_options,
        graph_settings,
        tag_paths,
        relative_run_path=True,
        testing=True,
        graph_target=get_graph_reps("root"),
    )


def _nested_artifacts(result: CompilationResult) -> list[Any]:
    return [artifact for artifact in result.artifact.children if artifact.graph is not None]


def _workflow_call_index(result: CompilationResult) -> int:
    return next(
        index
        for index, step_node in enumerate(result.graph.steps)
        if step_node.run is not None and step_node.run.child is not None
    )


def _with_wrapper_semantic(
    result: CompilationResult,
    **changes: Any,
) -> CompilationResult:
    """A copy of `result` whose emitted workflow-call step carries `changes`."""
    changed = copy.deepcopy(result)
    changed.artifact.cwl["steps"][_workflow_call_index(changed)].update(changes)
    return changed


@pytest.mark.fast
def test_resolved_subworkflow_projects_to_a_namespaced_process(
    real_nested_result: CompilationResult,
) -> None:
    names = [process.name for process in compiled_source_to_nextflow(real_nested_result).processes]
    assert any("child_wic___child__step__1__inner_copy" in name for name in names)


@pytest.mark.fast
def test_an_empty_resolved_subworkflow_projects_to_no_processes() -> None:
    result = compile_workflow_result(Workflow([Workflow([], "child")], "root"))
    assert compiled_source_to_nextflow(result).processes == ()


@pytest.mark.fast
def test_two_resolved_subworkflow_instances_do_not_collide() -> None:
    workflow = compiled_source_to_nextflow(_nested_result(siblings=2))
    child_names = [process.name for process in workflow.processes if "inner_copy" in process.name]
    assert len(child_names) == 2
    assert len(set(child_names)) == 2


@pytest.mark.fast
def test_a_resolved_subworkflow_output_reaches_a_downstream_outer_step() -> None:
    workflow = compiled_source_to_nextflow(_nested_downstream_result())
    connection = next(
        edge
        for edge in workflow.connections
        if isinstance(edge, NfProcessConnection) and edge.to_process.endswith("consume")
    )
    assert connection.from_process.endswith("inner_copy")


@pytest.mark.fast
def test_resolved_nesting_deeper_than_one_level_projects_recursively() -> None:
    workflow = compiled_source_to_nextflow(_deep_nested_result())
    assert len([process for process in workflow.processes if "inner_copy" in process.name]) == 1
    assert any("middle_wic" in process.name and "leaf_wic" in process.name
               for process in workflow.processes)


@pytest.mark.fast
def test_rejects_scatter_on_a_resolved_subworkflow_call() -> None:
    result = _with_wrapper_semantic(_nested_result(), scatter=["source"])
    with pytest.raises(ValueError, match="scatter on a nested workflow step"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_rejects_when_on_a_resolved_subworkflow_call() -> None:
    result = _with_wrapper_semantic(_nested_result(), when="$(true)")
    with pytest.raises(ValueError, match="a when on a nested-workflow step is not supported"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_resolved_subworkflow_input_reaches_the_leaf_process(
    real_nested_result: CompilationResult,
) -> None:
    workflow = compiled_source_to_nextflow(real_nested_result)
    assert any(
        isinstance(edge, NfProcessConnection)
        and edge.from_process.endswith("write")
        and edge.to_process.endswith("inner_copy")
        for edge in workflow.connections
    )


@pytest.mark.fast
def test_projection_does_not_invent_subworkflow_inputs(
    real_nested_result: CompilationResult,
) -> None:
    source = compilation_result_source(real_nested_result)
    declared = set(cast(dict[str, Any], source.workflow["inputs"]))
    leaf_sources: set[str] = set()
    for step_document in cast(list[dict[str, Any]], source.workflow["steps"]):
        for definition in cast(dict[str, Any], step_document["in"]).values():
            value = definition.get("source") if isinstance(definition, Mapping) else definition
            if isinstance(value, str) and "/" not in value:
                leaf_sources.add(value)
    assert leaf_sources <= declared


@pytest.mark.fast
def test_projected_outputs_name_declared_leaf_outputs(
    real_nested_result: CompilationResult,
) -> None:
    source = compilation_result_source(real_nested_result)
    steps = cast(list[dict[str, Any]], source.workflow["steps"])
    endpoints = {
        f"{step_document['id']}/{output}"
        for step_document in steps
        for output in step_document["out"]
    }
    assert all(
        definition["outputSource"] in endpoints
        for definition in cast(dict[str, Any], source.workflow["outputs"]).values()
    )


@pytest.mark.fast
def test_nested_output_boundary_resolves_to_a_leaf_output(
    real_nested_result: CompilationResult,
) -> None:
    outputs = cast(dict[str, Any], compilation_result_source(real_nested_result).workflow["outputs"])
    assert any("inner_copy/result" in definition["outputSource"] for definition in outputs.values())


@pytest.mark.fast
def test_nested_output_source_names_an_existing_process(
    real_nested_result: CompilationResult,
) -> None:
    source = compilation_result_source(real_nested_result)
    process_names = {step_document["id"] for step_document in source.workflow["steps"]}
    for definition in cast(dict[str, Any], source.workflow["outputs"]).values():
        assert definition["outputSource"].rsplit("/", maxsplit=1)[0] in process_names


@pytest.mark.fast
def test_inner_input_source_is_taken_from_the_resolved_graph(
    real_nested_result: CompilationResult,
) -> None:
    result = copy.deepcopy(real_nested_result)
    nested = _nested_artifacts(result)[0]
    nested.cwl["steps"][0]["in"]["source"] = "ABSENT/result"
    workflow = compiled_source_to_nextflow(result)
    assert any(
        isinstance(edge, NfProcessConnection) and edge.from_process.endswith("write")
        for edge in workflow.connections
    )


@pytest.mark.fast
def test_every_projected_process_connection_names_existing_processes(
    real_nested_result: CompilationResult,
) -> None:
    workflow = compiled_source_to_nextflow(real_nested_result)
    names = {process.name for process in workflow.processes}
    for edge in workflow.connections:
        if isinstance(edge, NfProcessConnection):
            assert edge.from_process in names and edge.to_process in names


@pytest.mark.fast
def test_accepts_an_inert_nested_subworkflow_feature_requirement() -> None:
    result = copy.deepcopy(_nested_result())
    _nested_artifacts(result)[0].cwl["requirements"] = {"SubworkflowFeatureRequirement": {}}
    assert compiled_source_to_nextflow(result).processes


@pytest.mark.fast
def test_rejects_unsupported_nested_workflow_requirements() -> None:
    result = copy.deepcopy(_nested_result())
    _nested_artifacts(result)[0].cwl["requirements"] = {"MultipleInputFeatureRequirement": {}}
    with pytest.raises(ValueError, match="MultipleInputFeatureRequirement is not supported"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_rejects_every_unconsumed_nested_workflow_field() -> None:
    result = copy.deepcopy(_nested_result())
    _nested_artifacts(result)[0].cwl["hints"] = {"ResourceRequirement": {}}
    with pytest.raises(ValueError, match=r"\.run\.hints: hints is not consumed"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_rejects_unconsumed_nested_workflow_port_fields() -> None:
    result = copy.deepcopy(_nested_result())
    child = _nested_artifacts(result)[0].cwl
    next(iter(child["inputs"].values()))["secondaryFiles"] = [".fai"]
    next(iter(child["outputs"].values()))["pickValue"] = "first_non_null"
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(result)
    assert "secondaryFiles is not consumed" in str(error.value)
    assert "pickValue is not consumed" in str(error.value)


@pytest.mark.fast
def test_rejects_a_nested_workflow_input_default_before_projection() -> None:
    result = copy.deepcopy(_nested_result())
    child = _nested_artifacts(result)[0].cwl
    next(iter(child["inputs"].values()))["default"] = "fallback.txt"
    with pytest.raises(ValueError, match="default is not consumed"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_rejects_unconsumed_workflow_call_input_semantics() -> None:
    result = _nested_result()
    wrapper = result.artifact.cwl["steps"][_workflow_call_index(result)]
    inputs = {
        name: (
            {**definition, "valueFrom": "$(self)"}
            if isinstance(definition, Mapping)
            else {"source": definition, "valueFrom": "$(self)"}
        )
        for name, definition in wrapper["in"].items()
    }
    result = _with_wrapper_semantic(result, **{"in": inputs})
    with pytest.raises(ValueError, match="valueFrom is not consumed"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_nested_capability_findings_aggregate_across_workflow_calls() -> None:
    result = copy.deepcopy(_nested_result(siblings=2))
    for child in _nested_artifacts(result):
        child.cwl["hints"] = {"ResourceRequirement": {}}
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(result)
    assert str(error.value).count("hints is not consumed") == 2


@pytest.mark.fast
def test_a_scattered_step_inside_a_subworkflow_uses_the_outer_scatter_contract() -> None:
    echo_tool = (
        CommandLineTool(
            "echo_item",
            Inputs(item=Input(cwl.string, position=1)),
            Outputs(result=Output(cwl.file, glob="out.txt")),
        )
        .base_command("echo")
        .stdout("out.txt")
    )
    echo = Step(echo_tool, step_name="echo_item")
    echo.inputs.item = ["a", "b"]
    echo.scatter_on(echo.inputs.item)
    child = Workflow([echo], "child")
    workflow = compiled_source_to_nextflow(
        compile_workflow_result(Workflow([child], "root"))
    )
    assert any(
        isinstance(edge, NfWorkflowInputConnection) and edge.adapter == "dotproduct"
        for edge in workflow.connections
    )


def _read_cwl_adapter(name: str) -> Yaml:
    """Load one real repository CWL adapter document."""
    import yaml

    return cast(Yaml, yaml.safe_load(
        (REPO_ROOT / "cwl_adapters" / name).read_text(encoding="utf-8")
    ))


def _capture_rose(
    output: Yaml,
    *,
    output_type: str = "File",
    tool_extra: Yaml | None = None,
) -> CompiledNextflowSource:
    """One real-shaped step whose single output carries the given definition."""
    step_tool = tool(
        "make",
        inputs={"message": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": output},
        **(tool_extra or {}),
    )
    document = workflow_doc(
        [step("make", **{"in": {"message": "message"}, "out": ["result"]})],
        inputs={"message": "string"},
        outputs={"result": {"type": output_type, "outputSource": "make/result"}},
    )
    return synthetic_source(document, [step_tool], workflow_inputs={"message": "hi"})


def _capture_findings(rose: CompiledNextflowSource) -> list[str]:
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(rose)
    return [
        line.removeprefix("- ")
        for line in str(error.value).splitlines()
        if line.startswith("- ")
    ]


def _unadmitted_finding(text: str) -> str:
    """The one closed-set diagnostic every unadmitted outputEval text gets."""
    return (
        "steps[0].run.outputs.result.outputBinding: unsupported outputEval "
        f"{text!r}; the approved set is exactly '$(self[0])', '$(self[0].contents)', "
        "recognized as capture declarations, and no other expression is evaluated"
    )


@pytest.mark.fast
def test_the_admitted_output_eval_table_is_closed_in_both_directions() -> None:
    """Widening the admitted set without a design revision must fail here."""
    assert dict(_ADMITTED_OUTPUT_EVAL) == {
        "$(self[0])": "single",
        "$(self[0].contents)": "text",
    }
    assert set(_ADMITTED_OUTPUT_EVAL.values()) <= NfPort.ALLOWED_CAPTURES
    assert not set(_ADMITTED_OUTPUT_EVAL) & set(_NAMED_OUTPUT_EVAL_REJECTIONS)
    assert set(_NAMED_OUTPUT_EVAL_REJECTIONS) == {
        "$(self[0].checksum)",
        "$(self[0].dirname)",
        "$(self[0].format)",
        "$(self[0].location)",
        "$(self[0].path)",
        "$(self[0].secondaryFiles)",
    }


@pytest.mark.fast
def test_the_admitted_cardinality_declaration_lowers_to_a_capture_marker() -> None:
    workflow = compiled_source_to_nextflow(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": "$(self[0])"}}
    ))

    port = workflow.processes[0].outputs[0]
    assert (port.name, port.qualifier, port.capture) == ("result", "path", "single")


@pytest.mark.fast
@pytest.mark.parametrize("text", ["$(self[0])", " $(self[0])\n", "\t$(self[0]) "])
def test_surrounding_whitespace_is_trimmed_but_internal_variation_is_not(text: str) -> None:
    workflow = compiled_source_to_nextflow(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}
    ))

    assert workflow.processes[0].outputs[0].capture == "single"


@pytest.mark.fast
@pytest.mark.parametrize("text", ["$( self[0] )", "$(self[ 0 ])", "$(self[0] )", "$(self [0])"])
def test_internal_variation_of_the_admitted_literal_is_rejected(text: str) -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}
    ))

    assert findings == [_unadmitted_finding(text)]


_REJECTED_OUTPUT_EVAL_CORPUS = (
    "$(self[0].size + 1)",
    "$(self[0].basename.toUpperCase())",
    "$(self.length)",
    "$(self[1])",
    "$(self[0] ? self[0] : null)",
    "$(inputs.message)",
    "${return self[0];}",
    "${ return self[0].contents.trim(); }",
    "$(self[0].contents.split('\\n'))",
    "$(self[0] .contents)",
    "$(self[0].contents())",
    "prefix-$(self[0])",
    "$(self[0])$(self[0])",
    "$(JSON.parse(self[0].contents))",
    "$(parseInt(self[0].contents))",
    "$(self[0].nameroot)",
    "$(self[0].nameext)",
)


@pytest.mark.fast
@pytest.mark.parametrize("text", _REJECTED_OUTPUT_EVAL_CORPUS)
def test_every_unadmitted_output_eval_shape_names_the_approved_set(text: str) -> None:
    """No expression is evaluated, composed, or partially recognized."""
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}
    ))

    assert findings == [_unadmitted_finding(text)]


@pytest.mark.fast
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("$(self[0].dirname)", "is the Nextflow task work directory"),
        ("$(self[0].path)", "is the Nextflow task work directory"),
        ("$(self[0].location)", "a CWL location is a URI"),
        ("$(self[0].checksum)", "computing a checksum is computation"),
        ("$(self[0].secondaryFiles)", "secondary files have no approved lowering"),
        ("$(self[0].format)", "output format annotations have no approved lowering"),
    ],
)
def test_each_named_projection_rejection_explains_itself(text: str, reason: str) -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}
    ))

    assert len(findings) == 1
    assert findings[0].startswith(
        f"steps[0].run.outputs.result.outputBinding: outputEval {text!r} is not representable: "
    )
    assert reason in findings[0]


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["*.txt", "out?.txt", "out[0-9].txt"])
def test_a_wildcard_glob_beside_a_cardinality_declaration_is_rejected(glob: str) -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": glob, "outputEval": "$(self[0])"}}
    ))

    assert len(findings) == 1
    assert findings[0].startswith("steps[0].run.outputs.result.outputBinding: outputEval")
    assert "requires a glob with no wildcard character" in findings[0]
    assert "glob match ordering load-bearing, which is unproven" in findings[0]


@pytest.mark.fast
def test_an_input_reference_glob_beside_a_cardinality_declaration_is_rejected() -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "$(inputs.message)", "outputEval": "$(self[0])"}}
    ))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0])' requires a glob "
        "with no input reference; a reference's runtime value cannot be shown wildcard-free "
        "at compile time"
    ]


@pytest.mark.fast
def test_load_contents_without_its_paired_output_eval_is_rejected() -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "loadContents": True}}
    ))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: loadContents on output 'result' has no "
        "approved lowering without its paired outputEval capture declaration"
    ]


@pytest.mark.fast
def test_the_cardinality_declaration_is_not_paired_with_load_contents() -> None:
    findings = _capture_findings(_capture_rose({
        "type": "File",
        "outputBinding": {"glob": "out.txt", "loadContents": True, "outputEval": "$(self[0])"},
    }))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0])' declares output "
        "cardinality and is not paired with loadContents"
    ]


@pytest.mark.fast
def test_load_contents_on_an_input_is_rejected_by_name_at_both_locations() -> None:
    """The input-side lowering is separate and unimplemented, not smuggled in here."""
    step_tool = tool(
        "make",
        inputs={
            "parameter": {"type": "File", "loadContents": True},
            "binding": {"type": "File", "inputBinding": {"position": 1, "loadContents": True}},
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )
    document = workflow_doc(
        [step("make", **{"in": {"parameter": "one", "binding": "two"}, "out": ["result"]})],
        inputs={"one": "File", "two": "File"},
        outputs={"result": {"type": "File", "outputSource": "make/result"}},
    )
    rose = synthetic_source(document, [step_tool], workflow_inputs={"one": "a.txt", "two": "b.txt"})

    findings = _capture_findings(rose)

    assert sorted(finding.split(":")[0] for finding in findings) == [
        "steps[0].run.inputs.binding.inputBinding.loadContents",
        "steps[0].run.inputs.parameter.loadContents",
    ]
    assert all(
        "an input's contents would have to become a value channel read at staging time"
        in finding
        for finding in findings
    )


@pytest.mark.fast
def test_a_declared_but_unused_inline_javascript_requirement_stays_legal() -> None:
    """The requirement is a declaration; the violation is a non-admitted form."""
    workflow = compiled_source_to_nextflow(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": "$(self[0])"}},
        tool_extra={"requirements": {"InlineJavascriptRequirement": {}}},
    ))

    assert workflow.processes[0].outputs[0].capture == "single"


_ADMITTED_TEXT_BINDING = {
    "glob": "out.txt",
    "loadContents": True,
    "outputEval": "$(self[0].contents)",
}


@pytest.mark.fast
def test_the_admitted_file_text_capture_lowers_to_a_value_port() -> None:
    workflow = compiled_source_to_nextflow(_capture_rose(
        {"type": "string", "outputBinding": dict(_ADMITTED_TEXT_BINDING)},
        output_type="string",
    ))

    port = workflow.processes[0].outputs[0]
    assert (port.name, port.qualifier, port.capture) == ("result", "val", "text")


@pytest.mark.fast
def test_the_real_cat_adapter_output_is_the_admitted_file_text_capture() -> None:
    """cwl_adapters/cat.cwl is exactly this shape; it must lower, not reject."""
    document = _read_cwl_adapter("cat.cwl")

    assert document["outputs"]["output"]["outputBinding"] == {
        "glob": "output",
        "loadContents": True,
        "outputEval": "$(self[0].contents)",
    }
    assert _output_capture("output", document["outputs"]["output"]) == "text"


@pytest.mark.fast
def test_file_text_capture_requires_load_contents_true() -> None:
    for load_contents in (None, False, "true"):
        binding = dict(_ADMITTED_TEXT_BINDING)
        if load_contents is None:
            del binding["loadContents"]
        else:
            binding["loadContents"] = load_contents
        findings = _capture_findings(_capture_rose(
            {"type": "string", "outputBinding": binding}, output_type="string"
        ))
        assert findings == [
            "steps[0].run.outputs.result.outputBinding: outputEval "
            "'$(self[0].contents)' captures file text and requires loadContents: true "
            "beside it"
        ]


@pytest.mark.fast
@pytest.mark.parametrize("declared", ["int", "float", "long", "double"])
def test_a_numeric_file_text_capture_is_rejected_because_parsing_is_computation(
    declared: str,
) -> None:
    findings = _capture_findings(_capture_rose(
        {"type": declared, "outputBinding": dict(_ADMITTED_TEXT_BINDING)},
        output_type=declared,
    ))

    assert len(findings) == 1
    assert findings[0].startswith("steps[0].run.outputs.result.outputBinding: outputEval")
    assert "parsing that text into a number is computation rather than a projection" in findings[0]
    assert "Declare type: string and parse downstream" in findings[0]


@pytest.mark.fast
def test_file_text_capture_on_a_non_string_non_numeric_output_is_rejected() -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "File", "outputBinding": dict(_ADMITTED_TEXT_BINDING)}
    ))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0].contents)' "
        "captures file text and is admitted only on a type: string output; output "
        "'result' declares 'File'"
    ]


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["*.txt", "out?.txt", "out[0-9].txt"])
def test_a_wildcard_glob_beside_a_file_text_capture_is_rejected(glob: str) -> None:
    findings = _capture_findings(_capture_rose(
        {"type": "string", "outputBinding": {**_ADMITTED_TEXT_BINDING, "glob": glob}},
        output_type="string",
    ))

    assert len(findings) == 1
    assert "requires a glob with no wildcard character" in findings[0]


@pytest.mark.fast
def test_a_captured_value_cannot_feed_another_process() -> None:
    """No qualifier agreement check exists for process edges, so this rejects."""
    producer = tool(
        "produce",
        inputs={"message": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"text": {"type": "string", "outputBinding": dict(_ADMITTED_TEXT_BINDING)}},
    )
    consumer = tool(
        "consume",
        inputs={"text": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "copy.txt"}}},
    )
    document = workflow_doc(
        [
            step("produce", **{"in": {"message": "message"}, "out": ["text"]}),
            step("consume", **{"in": {"text": "produce/text"}, "out": ["result"]}),
        ],
        inputs={"message": "string"},
        outputs={"result": {"type": "File", "outputSource": "consume/result"}},
    )
    rose = synthetic_source(document, [producer, consumer], workflow_inputs={"message": "hi"})

    findings = _capture_findings(rose)

    assert findings == [
        "steps[1].in.text: 'produce/text' captures file text; its only approved sink is "
        "a workflow output, because the executable graph has no qualifier agreement "
        "check for process edges"
    ]


@pytest.mark.fast
def test_lowering_rejections_aggregate_with_every_other_finding_by_step() -> None:
    gated = tool("GATED", inputs={"a": {"type": "int"}}, baseCommand="true")
    unsupported = tool("UNSUPPORTED", requirements={"NetworkAccess": {"networkAccess": True}})
    rose = synthetic_source(
        workflow_doc(
            [
                step("GATED", **{"in": {"a": "a"}, "when": "$(inputs.a ** 2 > 1)"}),
                step("GATED", **{"in": {"a": "a"}, "when": "$(inputs.a + 1)"}),
                step("UNSUPPORTED"),
            ],
            inputs={"a": "int"},
        ),
        [gated, gated, unsupported],
        workflow_inputs={"a": 1},
    )
    findings = _findings(rose)
    assert [finding.split(":", 1)[0] for finding in findings] == [
        "steps[0]", "steps[1]", "steps[2].run.requirements.NetworkAccess",
    ]
    assert "must compute a boolean" in findings[1]


@pytest.mark.fast
@pytest.mark.parametrize(("inputs", "message"), [
    ({"b": {"type": "boolean", "inputBinding": {"prefix": ""}}}, "prefix for 'b' must be a non-empty string"),
    ({"s": {"type": "string", "inputBinding": {"separate": False}}}, "separate cannot be specified without a prefix"),
    ({"xs": {"type": {"type": "array", "items": "string"}, "inputBinding": {"valueFrom": "$(self)"}}}, "valueFrom"),
], ids=["empty-boolean-prefix", "separate-without-prefix", "array-valuefrom"])
def test_a_lowering_only_rejection_is_a_finding_at_its_step(inputs: Any, message: str) -> None:
    values = {"b": True, "s": "x", "xs": ["x"]}
    rose = synthetic_source(
        workflow_doc(
            [step("REJECTED", **{"in": {name: name for name in inputs}})],
            inputs={name: {"type": definition["type"]} for name, definition in inputs.items()},
        ),
        [tool("REJECTED", inputs=inputs)],
        workflow_inputs={name: values[name] for name in inputs},
    )
    [finding] = _findings(rose)
    assert finding.startswith("steps[0]: ") and message in finding


@pytest.mark.fast
def test_a_workflow_output_forwarding_an_input_is_a_workflow_finding() -> None:
    rose = synthetic_source(
        workflow_doc(
            [step("ECHO")],
            inputs={"a": "string"},
            outputs={"a_out": {"type": "string", "outputSource": "a"}},
        ),
        [tool("ECHO")],
        workflow_inputs={"a": "x"},
    )
    assert _findings(rose) == [
        "workflow: workflow output 'a_out' directly forwards a workflow input; "
        "boundary passthrough is not executable on the Nextflow target"
    ]


@pytest.mark.fast
def test_a_command_error_is_reported_instead_of_a_wrong_cause_absence_finding() -> None:
    produce = tool(
        "PRODUCE",
        inputs={"x": {"type": "int"}},
        baseCommand="echo",
        stdout="out.txt",
        outputs={"r": {"type": "string", "outputBinding": {
            "glob": "out.txt", "loadContents": True, "outputEval": "$(self[0].contents)"}}},
    )
    consume = tool("CONSUME", inputs={"v": {"type": "string?"}}, arguments=["$(runtime.cores)"])
    rose = synthetic_source(
        workflow_doc(
            [
                step("PRODUCE", **{"in": {"x": "x"}, "out": ["r"], "when": "$(inputs.x > 1)"}),
                step("CONSUME", **{"in": {"v": "PRODUCE/r"}}),
            ],
            inputs={"x": "int"},
        ),
        [produce, consume],
        workflow_inputs={"x": 2},
    )
    [finding] = _findings(rose)
    assert finding.startswith("steps[1]: CWL argument contains an unsupported CWL expression")


@pytest.mark.fast
def test_a_command_error_is_reported_instead_of_an_absent_optional_finding() -> None:
    rose = synthetic_source(
        workflow_doc([step("ABSENT", **{"in": {"v": "v"}})], inputs={"v": "string?"}),
        [tool("ABSENT", inputs={"v": {"type": "string?", "inputBinding": {}}}, arguments=["$(runtime.cores)"])],
    )
    [finding] = _findings(rose)
    assert finding.startswith("steps[0]: CWL argument contains an unsupported CWL expression")
