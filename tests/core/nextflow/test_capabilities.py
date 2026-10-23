"""Closed-world capability analysis: unsupported source semantics reject before artifacts."""

# pylint: disable=missing-function-docstring

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
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
from sophios.api.python.workflow import CompiledWorkflow, Step, StepInput, Workflow
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
    compiled_source_to_nextflow,
)
from sophios.wic_types import Yaml

from .testkit import (
    one_step,
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
def test_a_literal_file_into_a_format_declaring_input_lowers_to_its_path(tmp_path: Path) -> None:
    """Sophios writes the input's `format` on the File value; like the input's, it is compile-time (design §3)."""
    source = tmp_path / "in.mdin"
    source.write_text("x", encoding="utf-8")
    fmt = CommandLineTool(
        "fmt",
        Inputs(src=Input(cwl.file, position=1, format="edam:format_2330")),
        Outputs(out=Output(cwl.file, glob="out.txt")),
    ).base_command("cat")
    step_ = Step(fmt, step_name="fmt")
    step_.inputs.src = str(source)
    result = compile_workflow_result(Workflow([step_], "wf"))
    assert list(result.artifact.job_inputs.values()) == [
        {"class": "File", "location": str(source), "format": "edam:format_2330"}
    ]
    assert list(compiled_source_to_nextflow(result).params.values()) == [{"class": "File", "path": str(source)}]


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
    rose = one_step(both, verbose=None)

    assert _findings(rose) == [
        "steps[0].run.inputs.verbose: absent optional values are supported only for a "
        "val input whose absence leaves command rendering unchanged"
    ]


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
def test_accepts_unreferenced_absent_optional_input() -> None:
    """An optional val input with no inputBinding is never dereferenced, so absence is safe."""
    optional = tool("OPTIONAL", inputs={"message": {"type": ["null", "string"]}})
    rose = one_step(optional, message=None)
    workflow = compiled_source_to_nextflow(rose)
    assert workflow.params == {"message": []}
    assert NfWorkflowInputConnection("message", "_OPTIONAL", "message") in workflow.connections


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
    rose = one_step(optional, message=None)
    with pytest.raises(
        ValueError,
        match=r"steps\[0\].run.inputs.message: absent optional values are supported only for "
        "a val input whose absence leaves command rendering unchanged",
    ):
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
    rose = one_step(basename, ["result"], label="input")
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
    rose = one_step(suffixed, value=value)
    reason = f"{reference} requires a File or Directory input, and value is declared {cwl_type!r}"
    assert {
        f"steps[0].run.inputs.value.inputBinding.valueFrom: {reason}",
        f"steps[0].run.stdout: {reason}",
    } <= set(_findings(rose))


def _findings(source: CompiledNextflowSource | CompilationResult) -> list[str]:
    """Return the exact aggregated finding lines of one rejected conversion."""
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(source)
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
def test_accepts_two_input_dotproduct_scatter() -> None:
    workflow = compiled_source_to_nextflow(_two_input_scatter_rose(scatterMethod="dotproduct"))

    assert set(workflow.connections) >= {
        NfWorkflowInputConnection("firsts", "_SCATTER", "first", "dotproduct"),
        NfWorkflowInputConnection("seconds", "_SCATTER", "second", "dotproduct"),
    }


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
        NfWorkflowInputConnection("items", "_SCATTER", "item", "dotproduct"),
    )


@pytest.mark.fast
@pytest.mark.parametrize("scatter", ["item", ("item",)], ids=["string-form", "list-form"])
def test_accepts_both_single_input_scatter_spellings(scatter: Any) -> None:
    workflow = compiled_source_to_nextflow(_scatter_rose(scatter=scatter))

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "_SCATTER", "item", "dotproduct"),
    )


def _producer() -> Yaml:
    return tool(
        "PRODUCER",
        outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
    )


@pytest.mark.fast
def test_an_unscattered_steps_output_reaches_a_scattered_steps_other_input_as_one_value() -> None:
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

    lowered = compiled_source_to_nextflow(rose)
    assert NfProcessConnection("_PRODUCER", "out", "_SCATTER", "extra") in lowered.connections
    assert "_SCATTER(ch__SCATTER_scatter, _PRODUCER.out.out.first())" in render_nextflow(lowered)


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

    assert NfWorkflowOutputConnection("_SCATTER", "result", "each") in workflow.connections


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
        NfWorkflowInputConnection("items", "_SCATTER", "item", "dotproduct"),
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
def test_rejects_an_optional_output_with_a_pattern_glob() -> None:
    logs = tool("LOGS", baseCommand="true", outputs={"o": {"type": "File?", "outputBinding": {"glob": "*.log"}}})
    workflow = workflow_doc([step("LOGS", out=["o"])], outputs={"o": {"type": "File?", "outputSource": "LOGS/o"}})

    assert _findings(synthetic_source(workflow, [logs])) == [
        "steps[0]: an optional output cannot require exactly one file (a pattern glob or a $(self[0]) "
        "capture): Nextflow fails that output when nothing matches"
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
    rose = one_step(resources)

    with pytest.raises(ValueError, match=r"coresMax.*whole number"):
        compiled_source_to_nextflow(rose)
    with pytest.raises(ValueError, match="cpus must be a positive integer"):
        NfResources(cpus=cast(Any, 2.5))


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
    rose = one_step(flags, verbose=True)

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
    return one_step(_array_tool(items, **binding), values=value)


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
    return one_step(_shell_tool(arguments, shell_command=shell_command), ["result"])


@pytest.mark.fast
def test_accepts_shell_command_requirement_with_no_shell_quote_false() -> None:
    """ShellCommandRequirement alone changes nothing: no shellQuote:false, nothing to reject."""
    rose = _shell_rose(["hello"])
    converted = compiled_source_to_nextflow(rose)
    assert converted.processes[0].command.tokens[0] == NfTemplate((NfLiteral("SHELL"),))


def _shell_tool_with_input(value_from: str) -> Yaml:
    return tool(
        "SHELL",
        inputs={"message": {"type": "string"}},
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"ShellCommandRequirement": {}},
        arguments=[{"valueFrom": value_from, "shellQuote": False}],
    )


@pytest.mark.fast
def test_accepts_literal_shell_quote_false_binding() -> None:
    rose = _shell_rose([{"valueFrom": ">>", "shellQuote": False}, "out.txt"])
    converted = compiled_source_to_nextflow(rose)
    assert NfShellLiteral(">>") in converted.processes[0].command.tokens


_ENV_INPUTS = {"s": {"type": "string"}, "n": {"type": "int"}, "f": {"type": "File"}, "o": {"type": "string?"}}


def _env_rose(env_def: Any, *, bind_optional: bool = True) -> CompiledNextflowSource:
    values = {"s": "v", "n": 1, "f": {"class": "File", "path": "x.txt"}, "o": "w"}
    names = [name for name in _ENV_INPUTS if bind_optional or name != "o"]
    return synthetic_source(
        workflow_doc(
            [step("ENV", **{"in": {name: name for name in names}})],
            inputs={name: _ENV_INPUTS[name]["type"] for name in names},
        ),
        [tool("ENV", inputs=_ENV_INPUTS, requirements={"EnvVarRequirement": {"envDef": env_def}})],
        workflow_inputs={name: values[name] for name in names},
    )


@pytest.mark.fast
@pytest.mark.parametrize(("entry", "reason"), [
    ({"envName": "A", "envValue": "$(inputs.n)"}, "needs a string input, read whole as $(inputs.<name>); n is declared 'int'"),
    ({"envName": "A", "envValue": "$(inputs.f.path)"}, "needs a string input"),
    ({"envName": "A", "envValue": "$(inputs.s.basename)"}, "needs a string input"),
    ({"envName": "A", "envValue": "pre-$(inputs.s)"}, "is not supported: write literal text or exactly $(inputs.<name>)"),
    ({"envName": "A", "envValue": "${return 1}"}, "is not supported"),
    ({"envName": "A", "envValue": "$(runtime.cores)"}, "is not supported"),
    ({"envName": "1A", "envValue": "x"}, "environment variable name must be a shell variable name, got '1A'"),
    ({"envName": "A", "envValue": 3}, "an envDef entry needs a string envName and a string envValue"),
    ("A=x", "an envDef entry needs a string envName and a string envValue"),
])
def test_env_var_requirement_rejects_every_other_value_by_name(entry: Any, reason: str) -> None:
    [finding] = _findings(_env_rose([entry]))
    assert finding.startswith("steps[0].run.requirements.EnvVarRequirement.envDef[0]: ")
    assert reason in finding


@pytest.mark.fast
def test_env_var_requirement_rejects_an_unknown_field_and_an_absent_input() -> None:
    assert _findings(_env_rose({"A": {"envValue": "x", "foo": 1}})) == [
        "steps[0].run.requirements.EnvVarRequirement.envDef.A.foo: foo is not consumed by the Nextflow lowering"
    ]
    assert _findings(_env_rose([{"envName": "A", "envValue": "$(inputs.o)"}], bind_optional=False)) == [
        "steps[0].run.inputs.o: absent optional values are supported only for a val input whose absence "
        "leaves command rendering unchanged"
    ]


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
    document = workflow_runtime.workflow_document(root)
    steps = list(document.steps)
    steps[1] = replace(steps[1], outputs=(OutputBinding("result", EdgeDef("childout")),))
    steps[2] = replace(steps[2], inputs=(("source", EdgeRef("childout")),))
    compiler_options = default_compilation_settings()
    tools = workflow_runtime._merged_known_tools(root)
    bundle = SourceBundle(
        ParseResult(replace(document, steps=tuple(steps)), Diagnostics()),
        "root",
        RegistrySnapshot.from_tools(tools, workflows={
            ("global", child.process_name): ParseResult(
                workflow_runtime.workflow_document(child), Diagnostics()
            ),
        }),
    )
    return compiler.compile_source(bundle, compiler_options, testing=True)


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
    assert [process.name for process in workflow.processes] == ["write", "middle_leaf_inner_copy"]


@pytest.mark.fast
def test_a_nested_workflow_steps_input_takes_one_source_as_it_is() -> None:
    """The core compiler joins one source to each input a nested workflow step binds."""
    result = _nested_result()
    wrapper = result.artifact.cwl["steps"][_workflow_call_index(result)]
    name, definition = next(iter(wrapper["in"].items()))
    source = definition["source"] if isinstance(definition, Mapping) else definition
    record = {"source": [source, source], "linkMerge": "merge_nested", "pickValue": "first_non_null", "default": 1}
    findings = _findings(_with_wrapper_semantic(result, **{"in": {**wrapper["in"], name: record}}))
    assert [finding for finding in findings if "takes one source" in finding] == [
        f"workflow.steps[1].in.{name}.{field}: a nested workflow step's input takes one source as it is; "
        "merge on a step inside the nested workflow"
        for field in ("default", "linkMerge", "pickValue", "source")
    ]


@pytest.mark.fast
@pytest.mark.parametrize("when", [None, "$(inputs.enabled)"], ids=["unread", "read-by-when"])
def test_a_step_input_its_tool_does_not_declare_is_rejected_by_name(when: str | None) -> None:
    """The cookbook's when.wic: a step-only input that a when reads."""
    echo = Step(clt_path=REPO_ROOT / "cwl_adapters" / "echo.cwl")
    echo.inputs.message = "Hello"
    flow = Workflow([echo], "when")
    echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type("boolean?"))
    if when is not None:
        echo.when = when
    assert _findings(compile_workflow_result(flow)) == [
        "steps[0].in.enabled: an input the step's tool does not declare is not supported"
    ]


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
@pytest.mark.parametrize("requirement", ["SubworkflowFeatureRequirement", "MultipleInputFeatureRequirement"])
def test_accepts_an_inert_nested_workflow_feature_requirement(requirement: str) -> None:
    result = copy.deepcopy(_nested_result())
    _nested_artifacts(result)[0].cwl["requirements"] = {requirement: {}}
    assert compiled_source_to_nextflow(result).processes


@pytest.mark.fast
def test_rejects_unconsumed_nested_workflow_port_fields() -> None:
    result = copy.deepcopy(_nested_result())
    child = _nested_artifacts(result)[0].cwl
    next(iter(child["inputs"].values()))["secondaryFiles"] = [".fai"]
    next(iter(child["outputs"].values()))["pickValue"] = "first_non_null"
    for section in ("inputs", "outputs"):
        next(iter(child[section].values()))["type"] = {"type": "array", "items": "File", "name": section}
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(result)
    assert "secondaryFiles are not supported" in str(error.value)
    assert "a workflow output's pickValue is not supported" in str(error.value)
    for section in ("inputs", "outputs"):
        assert re.search(rf"\.run\.{section}\.\w+\.type\.name: an array schema's name", str(error.value))


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
    with pytest.raises(ValueError, match="a step input's valueFrom is not supported"):
        compiled_source_to_nextflow(result)


@pytest.mark.fast
def test_nested_capability_findings_aggregate_across_workflow_calls() -> None:
    result = copy.deepcopy(_nested_result(siblings=2))
    for child in _nested_artifacts(result):
        child.cwl["hints"] = {"ResourceRequirement": {}}
    with pytest.raises(ValueError) as error:
        compiled_source_to_nextflow(result)
    assert str(error.value).count("workflow hints are not supported") == 2


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


def _capture_rose(output: Yaml, *, tool_extra: Yaml | None = None) -> CompiledNextflowSource:
    """One real-shaped step whose single output carries the given definition."""
    step_tool = tool(
        "make",
        inputs={"message": {"type": "string", "inputBinding": {"position": 1}}},
        outputs={"result": output},
        **(tool_extra or {}),
    )
    return one_step(step_tool, ["result"], message="hi")


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
    assert set(_ADMITTED_OUTPUT_EVAL.values()) <= set(NfPort.CAPTURE_QUALIFIERS)
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
    findings = _findings(_capture_rose({"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}))

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
    findings = _findings(_capture_rose({"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}))

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
    findings = _findings(_capture_rose({"type": "File", "outputBinding": {"glob": "out.txt", "outputEval": text}}))

    assert len(findings) == 1
    assert findings[0].startswith(
        f"steps[0].run.outputs.result.outputBinding: outputEval {text!r} is not representable: "
    )
    assert reason in findings[0]


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["*.txt", "out?.txt", "out[0-9].txt"])
def test_a_wildcard_glob_beside_a_cardinality_declaration_is_rejected(glob: str) -> None:
    findings = _findings(_capture_rose({"type": "File", "outputBinding": {"glob": glob, "outputEval": "$(self[0])"}}))

    assert len(findings) == 1
    assert findings[0].startswith("steps[0].run.outputs.result.outputBinding: outputEval")
    assert "requires a glob with no wildcard character" in findings[0]
    assert "glob match ordering load-bearing, which is unproven" in findings[0]


@pytest.mark.fast
def test_an_input_reference_glob_beside_a_cardinality_declaration_is_rejected() -> None:
    findings = _findings(_capture_rose(
        {"type": "File", "outputBinding": {"glob": "$(inputs.message)", "outputEval": "$(self[0])"}}
    ))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0])' requires a glob "
        "with no input reference; a reference's runtime value cannot be shown wildcard-free "
        "at compile time"
    ]


@pytest.mark.fast
def test_load_contents_without_its_paired_output_eval_is_rejected() -> None:
    findings = _findings(_capture_rose({"type": "File", "outputBinding": {"glob": "out.txt", "loadContents": True}}))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: loadContents on output 'result' has no "
        "approved lowering without its paired outputEval capture declaration"
    ]


@pytest.mark.fast
def test_the_cardinality_declaration_is_not_paired_with_load_contents() -> None:
    findings = _findings(_capture_rose({
        "type": "File",
        "outputBinding": {"glob": "out.txt", "loadContents": True, "outputEval": "$(self[0])"},
    }))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0])' declares output "
        "cardinality and is not paired with loadContents"
    ]


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
        findings = _findings(_capture_rose({"type": "string", "outputBinding": binding}))
        assert findings == [
            "steps[0].run.outputs.result.outputBinding: outputEval "
            "'$(self[0].contents)' captures file text and requires loadContents: true "
            "beside it"
        ]


@pytest.mark.fast
@pytest.mark.parametrize("declared", ["int", "float"])
def test_a_numeric_file_text_capture_is_rejected_because_parsing_is_computation(
    declared: str,
) -> None:
    findings = _findings(_capture_rose({"type": declared, "outputBinding": dict(_ADMITTED_TEXT_BINDING)}))

    assert len(findings) == 1
    assert findings[0].startswith("steps[0].run.outputs.result.outputBinding: outputEval")
    assert "parsing that text into a number is computation rather than a projection" in findings[0]
    assert "Declare type: string and parse downstream" in findings[0]


@pytest.mark.fast
def test_file_text_capture_on_a_non_string_non_numeric_output_is_rejected() -> None:
    findings = _findings(_capture_rose({"type": "File", "outputBinding": dict(_ADMITTED_TEXT_BINDING)}))

    assert findings == [
        "steps[0].run.outputs.result.outputBinding: outputEval '$(self[0].contents)' "
        "captures file text and is admitted only on a type: string output; output "
        "'result' declares 'File'"
    ]


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["*.txt", "out?.txt", "out[0-9].txt"])
def test_a_wildcard_glob_beside_a_file_text_capture_is_rejected(glob: str) -> None:
    findings = _findings(_capture_rose({"type": "string", "outputBinding": {**_ADMITTED_TEXT_BINDING, "glob": glob}}))

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

    findings = _findings(rose)

    assert findings == [
        "steps[1].in.text: 'produce/text' captures file text; its only approved sink is "
        "a workflow output, because the executable graph has no qualifier agreement "
        "check for process edges"
    ]


@pytest.mark.fast
def test_lowering_rejections_aggregate_with_every_other_finding_by_step() -> None:
    gated = tool("GATED", inputs={"a": {"type": "int"}}, baseCommand="true")
    unsupported = tool("UNSUPPORTED", requirements={"ToolTimeLimit": {"timelimit": 60}})
    rose = synthetic_source(
        workflow_doc(
            [
                step("GATED@1", **{"in": {"a": "a"}, "when": "$(inputs.a ** 2 > 1)"}),
                step("GATED@2", **{"in": {"a": "a"}, "when": "$(inputs.a + 1)"}),
                step("UNSUPPORTED"),
            ],
            inputs={"a": "int"},
        ),
        [gated, gated, unsupported],
        workflow_inputs={"a": 1},
    )
    findings = _findings(rose)
    assert [finding.split(":", 1)[0] for finding in findings] == [
        "steps[0]", "steps[1]", "steps[2].run.requirements.ToolTimeLimit",
    ]
    assert "must compute a boolean" in findings[1]


@pytest.mark.fast
@pytest.mark.parametrize(("inputs", "message"), [
    ({"b": {"type": "boolean", "inputBinding": {"prefix": ""}}}, "prefix for 'b' must be a non-empty string"),
    # A blank prefix hits the named diagnostic, not NfFlag's own invariant.
    ({"b": {"type": "boolean", "inputBinding": {"prefix": "   "}}}, "prefix for 'b' must be a non-empty string"),
    # cwltool rejects separate without a prefix.
    ({"s": {"type": "string", "inputBinding": {"separate": False}}}, "separate cannot be specified without a prefix"),
    ({"b": {"type": "boolean", "inputBinding": {"separate": False}}}, "separate cannot be specified without a prefix"),
    # cwltool's .get("separate", True) defaults only an absent key, so separate: null rejects too.
    ({"b": {"type": "boolean", "inputBinding": {"separate": None}}}, "separate cannot be specified without a prefix"),
    ({"s": {"type": "string", "inputBinding": {"separate": None}}}, "separate cannot be specified without a prefix"),
    (
        {"xs": {"type": {"type": "array", "items": "string"}, "inputBinding": {"valueFrom": "$(inputs.xs)"}}},
        "valueFrom on an array-typed inputBinding",
    ),
], ids=[
    "empty-boolean-prefix", "blank-boolean-prefix", "separate-without-prefix", "boolean-separate-without-prefix",
    "boolean-separate-null", "string-separate-null", "array-valuefrom",
])
def test_a_lowering_only_rejection_is_a_finding_at_its_step(inputs: Any, message: str) -> None:
    values: dict[str, Any] = {"b": True, "s": "x", "xs": ["x"]}
    rose = one_step(tool("REJECTED", inputs=inputs), **{name: values[name] for name in inputs})
    [finding] = _findings(rose)
    assert finding.startswith("steps[0]: ") and message in finding


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
    rose = one_step(
        tool("ABSENT", inputs={"v": {"type": "string?", "inputBinding": {}}}, arguments=["$(runtime.cores)"])
    )
    [finding] = _findings(rose)
    assert finding.startswith("steps[0]: CWL argument contains an unsupported CWL expression")


@pytest.mark.fast
@pytest.mark.parametrize(("argument", "admitted"), [
    ("--input=$(inputs.f)", False),
    ("$(inputs.f)", True),
    ("--input=$(inputs.f.path)", True),
    ("--input=$(inputs.f.basename)", True),
    ("--name=$(inputs.s)", True),
], ids=["embedded-file", "whole-file", "embedded-path", "embedded-basename", "embedded-string"])
def test_a_file_reference_embedded_in_text_is_rejected(argument: str, admitted: bool) -> None:
    embedded = tool("EMBEDDED", inputs={"f": {"type": "File"}, "s": {"type": "string"}}, arguments=[argument])
    rose = one_step(embedded, f={"class": "File", "path": "in.txt"}, s="x")
    if admitted:
        compiled_source_to_nextflow(rose)
        return
    assert _findings(rose) == [
        "steps[0].run.arguments[0]: $(inputs.f) embedded in other text renders a File object as JSON in CWL; "
        "use $(inputs.f.path) or $(inputs.f.basename), or the reference alone"
    ]


@pytest.mark.fast
@pytest.mark.parametrize("number", ["long", "double"])
@pytest.mark.parametrize(("where", "path"), [
    ("tool input", "steps[0].run.inputs.n"),
    ("array item", "steps[0].run.inputs.n"),
    ("tool output", "steps[0].run.outputs.out"),
    ("workflow input", "workflow.inputs.n"),
    ("workflow output", "workflow.outputs.out"),
])
def test_long_and_double_are_rejected_naming_the_type(number: str, where: str, path: str) -> None:
    declared = {"tool input": number, "array item": {"type": "array", "items": number}}.get(where, "int")
    out = number if where == "tool output" else "File"
    produce = tool(
        "PRODUCE",
        inputs={"n": {"type": declared, "inputBinding": {"position": 1}}},
        outputs={"out": {"type": out, "outputBinding": {"glob": "out.txt"}}},
    )
    workflow = workflow_doc(
        [step("PRODUCE", **{"in": {"n": "n"}, "out": ["out"]})],
        inputs={"n": {"type": number if where == "workflow input" else declared}},
        outputs={"out": {"type": number if where == "workflow output" else out, "outputSource": "PRODUCE/out"}},
    )
    with pytest.raises(ValueError, match=re.escape(f"{path}.type: {number} is not supported")):
        compiled_source_to_nextflow(synthetic_source(workflow, [produce], workflow_inputs={}))


def _with_child(mutate: Callable[[Yaml], Any]) -> CompilationResult:
    """A copy of the nested result whose child workflow document `mutate` changes."""
    result = copy.deepcopy(_nested_result())
    mutate(_nested_artifacts(result)[0].cwl)
    return result


_SORT = tool("SORT", inputs={"reverse": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "-r"}}})


def _flags(value_from: str, **inputs: Any) -> CompiledNextflowSource:
    """A boolean --verbose binding whose valueFrom is `value_from`, beside `inputs`."""
    binding = {"position": 1, "prefix": "--verbose", "valueFrom": value_from}
    run = tool("FLAGS", inputs={"verbose": {"type": "boolean", "inputBinding": binding}, **inputs})
    values: dict[str, Any] = {"verbose": True, **dict.fromkeys(inputs, False)}
    return one_step(run, **values)


def _classless(section: str) -> CompiledNextflowSource:
    run = tool("CLASSLESS")
    run[section] = [{"directive": "${System.exit(3)}"}]
    return one_step(run)


def _string_into_flag(supplied: str) -> CompiledNextflowSource:
    document = workflow_doc([step("SORT", **{"in": {"reverse": "flagval"}})], inputs={"flagval": {"type": "string"}})
    return synthetic_source(document, [_SORT], workflow_inputs={"flagval": supplied})


def _flag_wired_to(raw_source: Any) -> CompiledNextflowSource:
    run = tool("FLAGS", inputs={"verbose": {"type": "boolean", "inputBinding": {"position": 1, "prefix": "--verbose"}}})
    return synthetic_source(workflow_doc([step("FLAGS", **{"in": {"verbose": raw_source}})]), [run])


def _boundary_value(cwl_type: Any, value: Any) -> CompiledNextflowSource:
    document = workflow_doc([], inputs={"value": {"type": cwl_type}})
    return synthetic_source(document, [], workflow_inputs={"value": value})


_EMPTY_SCATTER = "steps[0].scatter: scatter must name one input, as a string or a non-empty list"
_SCATTER_ITEM = {"in": {"item": "items"}, "out": ["result"], "scatter": ["item"]}
_FILE_RESULT = {"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}}

# Each source, built when its test runs, and the exact findings that reject it.
FINDINGS = [
    pytest.param(lambda: _two_input_scatter_rose(scatterMethod=None), [
        "steps[0].scatterMethod: multi-input scatter over 2 inputs requires an explicit scatterMethod"
    ], id="multi_input_scatter_without_a_scatter_method"),
    *(
        pytest.param(
            lambda scatter=scatter: _scatter_rose(scatter=scatter), [_EMPTY_SCATTER],
            id=f"a_scatter_field_that_does_not_name_one_input-{name}",
        )
        for scatter, name in (
            ([], "empty-list"), ({"item": True}, "mapping"), ("", "empty-string"), (["item", ""], "empty-name")
        )
    ),
    pytest.param(lambda: _scatter_rose(scatterMethod="product"), [
        "steps[0].scatterMethod: unsupported CWL scatter method 'product'"
    ], id="an_unsupported_scatter_method"),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("SCATTER", **_SCATTER_ITEM)], inputs={"items": {"type": "string"}}),
        [_scattered_tool()], workflow_inputs={"items": "a"},
    ), [
        "steps[0].in.item: a scattered input needs an array-typed source; 'items' declares 'string'"
    ], id="scatter_over_a_non_array_workflow_input"),
    # An unscattered step's output is one value, and no tool output is an array to scatter over.
    pytest.param(lambda: synthetic_source(
        workflow_doc([
            step("PRODUCER", out=["out"]), step("SCATTER", **{**_SCATTER_ITEM, "in": {"item": "PRODUCER/out"}}),
        ]),
        [_producer(), _scattered_tool("File")],
    ), [
        "steps[1].in.item: a scattered input needs an array-typed source; "
        "'PRODUCER/out', from a step that is not scattered, is one value"
    ], id="scatter_over_an_unscattered_steps_output"),
    pytest.param(lambda: _scatter_rose(port_type=_STRING_ARRAY), [
        "steps[0].scatter: scattered input 'item' is array-typed; scattering over an "
        "array of arrays is deferred beyond this lowering"
    ], id="scatter_over_an_array_typed_port"),
    pytest.param(lambda: _scatter_rose(port_type="File"), [
        "steps[0].in.item: scattered source 'items' carries 'val' elements but the port takes 'path[file]'"
    ], id="scatter_whose_element_type_mismatches_its_port"),
    pytest.param(lambda: _scatter_rose(scatter=("missing",)), [
        "steps[0].scatter: scattered input 'missing' is not declared by the step's tool"
    ], id="scatter_naming_an_undeclared_input"),
    pytest.param(lambda: synthetic_source(
        workflow_doc(
            [step("SCATTER", **{"out": ["result"], "scatter": ["item"]})], inputs={"items": {"type": _STRING_ARRAY}}
        ),
        [_scattered_tool()], workflow_inputs={"items": ["a"]},
    ), [
        "steps[0].scatter: scattered input 'item' has no source; wire it to an "
        "array-typed workflow input or a scattered step's output"
    ], id="an_unwired_scattered_input"),
    # A step id that is not a string is a finding beside the rest, not a crash of a pass keyed by step id.
    *(
        pytest.param(lambda fields=fields: synthetic_source(
            workflow_doc([{"run": "S.cwl", **_SCATTER_ITEM, **fields}], inputs={"items": {"type": _STRING_ARRAY}}),
            [tool("S", inputs={"item": {"type": "string"}},
                  outputs={"result": {"type": "string", "outputBinding": _ADMITTED_TEXT_BINDING}})],
            workflow_inputs={"items": ["a"]},
        ), [
            "steps[0]: workflow step id must be a non-empty string",
            "workflow.steps: workflow step identifier must be a non-empty string",
        ], id=f"a_step_id_that_is_not_a_string-{name}")
        for fields, name in (({}, "missing"), ({"id": ["a"], "when": "$(inputs.item == 'a')"}, "list-in-a-when-step"))
    ),
    *(
        pytest.param(
            lambda section=section: _classless(section),
            [f"steps[0].run.{section}.0: a requirement or hint needs a string `class`"],
            id=f"tool_requirement_without_class-{section}",
        )
        for section in ("requirements", "hints")
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc(
            [step("ECHO")], inputs={"a": "string"}, outputs={"a_out": {"type": "string", "outputSource": "a"}}
        ),
        [tool("ECHO")], workflow_inputs={"a": "x"},
    ), [
        "workflow: workflow output 'a_out' directly forwards a workflow input; "
        "boundary passthrough is not executable on the Nextflow target"
    ], id="a_workflow_output_forwarding_an_input_is_a_workflow_finding"),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("MAYBE", out=["o"]), step("CONSUME", **{"in": {"source": "MAYBE/o"}})]),
        [
            tool("MAYBE", outputs={"o": {"type": "File?", "outputBinding": {"glob": "maybe.txt"}}}),
            tool("CONSUME", inputs={"source": {"type": "File?", "inputBinding": {"position": 1}}}),
        ],
    ), [
        "workflow: connection _MAYBE.o -> _CONSUME.source consumes an optional output, which may be null; "
        "only a workflow output can take it"
    ], id="a_step_consuming_an_optional_output_is_a_finding"),
    # The type's finding names a long; its default is not judged against it.
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("N")]), [tool("N", inputs={"n": {"type": "long", "default": 1}})]
    ), [
        "steps[0].run.inputs.n.type: long is not supported: the Nextflow target lowers int and float; "
        "declare one of those"
    ], id="a_long_input_with_a_default_has_only_the_type_finding"),
    pytest.param(lambda: one_step(tool("ARGS", arguments=[{"valueFrom": "x", "loadContents": True}])), [
        "steps[0].run.arguments[0].loadContents: a binding's loadContents is not supported"
    ], id="loadContents_on_an_arguments_entry"),
]


@pytest.mark.fast
@pytest.mark.parametrize(("build", "expected"), FINDINGS)
def test_a_rejected_source_has_exactly_its_findings(
    build: Callable[[], CompiledNextflowSource], expected: list[str]
) -> None:
    assert _findings(build()) == expected


_BOOLEAN_VALUE_FROM = r"valueFrom on a boolean inputBinding is supported only as"
_ABSENT_OPTIONAL = r"steps\[0\].run.inputs.{}: absent optional"

# Each source, built when its test runs, and a pattern its rejection matches.
MATCHES = [
    # A scattered step's output is an array; a scalar port cannot take it.
    pytest.param(lambda: synthetic_source(
        workflow_doc(
            [
                step("SCATTER", **_SCATTER_ITEM),
                step("CONSUMER", **{"in": {"source": "SCATTER/result"}, "out": ["result"]}),
            ],
            inputs={"items": {"type": _STRING_ARRAY}},
        ),
        [
            _scattered_tool(),
            tool(
                "CONSUMER", inputs={"source": {"type": "File", "inputBinding": {"position": 1}}},
                outputs={"result": {"type": "File", "outputBinding": {"glob": "copy.txt"}}},
            ),
        ],
        workflow_inputs={"items": ["a"]},
    ), "gathers into a port that is not array-typed", id="a_scalar_consumer_of_a_scattered_step"),
    pytest.param(
        lambda: _array_rose_from_producer({"type": "array", "items": "string"}), "nested arrays are deferred",
        id="nested_arrays",
    ),
    pytest.param(
        lambda: _array_rose_from_producer({"type": "File", "inputBinding": {"prefix": "-I"}}),
        "per-item array element bindings are deferred", id="per_item_input_binding_on_array_items",
    ),
    pytest.param(
        lambda: _array_rose("string", ["a", "b"], separate=False),
        r"separate: false on an array-typed inputBinding.*deferred", id="separate_false_on_an_array_binding",
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("MAKE", out=["results"])]),
        [tool("MAKE", outputs={
            "results": {"type": {"type": "array", "items": "File"}, "outputBinding": {"glob": "*.txt"}}
        })],
    ), r"results\.type: an output is collected by glob as a File or Directory", id="array_typed_outputs"),
    # Absence (no array at all) is out of scope for the array lowering; empty is not absence.
    pytest.param(lambda: one_step(tool("ARRAY", inputs={"values": {
        "type": ["null", {"type": "array", "items": "string"}],
        "inputBinding": {"position": 1, "prefix": "--value"},
    }}), values=None), _ABSENT_OPTIONAL.format("values"), id="absent_optional_array_input"),
    pytest.param(
        lambda: _array_rose("string", ["a", 1]), "does not match its supported CWL type",
        id="array_value_with_a_mismatched_item_type",
    ),
    pytest.param(
        lambda: _shell_rose([{"valueFrom": ">>", "shellQuote": False}], shell_command=False),
        "requires ShellCommandRequirement", id="shell_quote_false_without_shell_command_requirement",
    ),
    pytest.param(
        lambda: _shell_rose([{"shellQuote": False}]), "has no valueFrom", id="shell_quote_false_with_no_value_from"
    ),
    pytest.param(
        lambda: _shell_rose([{"valueFrom": ">>", "shellQuote": False, "prefix": "-x"}]), "does not support a prefix",
        id="shell_quote_false_with_a_prefix",
    ),
    # Unquoting a value derived from any input, directly or via a suffix, is the boundary.
    *(
        pytest.param(
            lambda value_from=value_from: one_step(_shell_tool_with_input(value_from), ["result"], message="hi"),
            "references an input", id=f"shell_quote_false_referencing_an_input-{name}",
        )
        for value_from, name in (
            ("$(inputs.message)", "bare"),
            ("prefix-$(inputs.message)-suffix", "embedded"),
            ("$(inputs.message.basename)", "basename-suffix"),
        )
    ),
    # The real append.cwl adapter's shape: shellQuote: false on a plain, no-valueFrom inputBinding.
    pytest.param(lambda: one_step(tool(
        "APPEND",
        inputs={"text": {"type": "string", "inputBinding": {"shellQuote": False, "position": 1, "prefix": "echo"}}},
        outputs=_FILE_RESULT,
        requirements={"ShellCommandRequirement": {}},
    ), ["result"], text="Hello"), "does not support a prefix", id="shell_quote_false_input_binding_with_no_value_from"),
    pytest.param(
        lambda: _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "renamed.txt", "writable": True}]),
        "writable: true", id="iwdr_writable_true",
    ),
    pytest.param(
        lambda: _iwdr_rose([{"entry": "literal file content", "entryname": "x.txt"}]),
        "inline content construction is deferred", id="iwdr_inline_content_entry",
    ),
    pytest.param(
        lambda: _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "$(inputs.source.path)"}]),
        "computed or differently-referencing entryname", id="iwdr_computed_entryname",
    ),
    pytest.param(
        lambda: _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "sub/dir.txt"}]), "path separator",
        id="iwdr_entryname_with_path_separator",
    ),
    *(
        pytest.param(
            lambda entryname=entryname: _iwdr_rose([{"entry": "$(inputs.source)", "entryname": entryname}]),
            "stageAs wildcard", id=f"iwdr_entryname_with_nextflow_stage_as_wildcard-{entryname}",
        )
        for entryname in ("reads*.fq", "sample?.txt")
    ),
    pytest.param(lambda: _iwdr_rose(["$(inputs.nope)"]), "undeclared input", id="iwdr_undeclared_input"),
    pytest.param(
        lambda: _iwdr_rose(["$(inputs.name)"], extra_inputs={"name": {"type": "string"}}), "non-path input",
        id="iwdr_val_typed_target",
    ),
    pytest.param(
        lambda: _iwdr_rose(["$(inputs.files)"], extra_inputs={"files": {"type": {"type": "array", "items": "File"}}}),
        "array-typed input", id="iwdr_array_typed_target",
    ),
    pytest.param(
        lambda: _iwdr_rose([{"entry": "$(inputs.source)", "entryname": "x.txt", "foo": 1}]),
        r"listing\[0\]\.foo: foo is not consumed", id="iwdr_unsupported_dirent_field",
    ),
    pytest.param(
        lambda: _iwdr_rose([123]), "bare .* reference or a Dirent mapping", id="iwdr_listing_entry_of_unsupported_shape"
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("STAGE", out=["result"])]),
        [tool("STAGE", outputs=_FILE_RESULT, requirements={"InitialWorkDirRequirement": {"listing": "not-a-list"}})],
    ), "listing must be a list", id="iwdr_listing_that_is_not_a_list"),
    pytest.param(
        lambda: _iwdr_rose(
            [{"entry": "$(inputs.source)", "entryname": "renamed.txt"}], arguments=["cat", "$(inputs.source)"]
        ),
        "staged under an explicit rename", id="iwdr_renamed_input_referenced_elsewhere",
    ),
    pytest.param(lambda: _iwdr_rose([
        {"entry": "$(inputs.source)", "entryname": "one.txt"},
        {"entry": "$(inputs.source)", "entryname": "two.txt"},
    ]), "input 'source' appears more than once", id="iwdr_listing_that_repeats_an_input-two-dirents"),
    pytest.param(lambda: _iwdr_rose([
        "$(inputs.source)",
        {"entry": "$(inputs.source)", "entryname": "renamed.txt"},
    ]), "input 'source' appears more than once", id="iwdr_listing_that_repeats_an_input-bare-and-dirent"),
    pytest.param(lambda: one_step(tool(
        "STAGE",
        inputs={"a": {"type": "File"}, "b": {"type": "File"}},
        outputs=_FILE_RESULT,
        requirements={"InitialWorkDirRequirement": {"listing": [
            {"entry": "$(inputs.a)", "entryname": "same.txt"},
            {"entry": "$(inputs.b)", "entryname": "same.txt"},
        ]}},
        arguments=["cat", "same.txt"],
        stdout="out.txt",
    ), ["result"], a={"class": "File", "path": "a.txt"}, b={"class": "File", "path": "b.txt"}),
        "staged under the same literal name", id="iwdr_collision_same_literal_name"),
    # Aliasing a different input's presence onto this flag has no approved lowering.
    pytest.param(
        lambda: _flags("$(inputs.other)", other={"type": "boolean"}), _BOOLEAN_VALUE_FROM,
        id="value_from_aliasing_a_different_input_on_a_boolean_binding",
    ),
    # A literal string is not a provably-boolean valueFrom result.
    pytest.param(lambda: _flags("true"), _BOOLEAN_VALUE_FROM, id="literal_value_from_on_a_boolean_binding"),
    # A .basename suffix is never boolean-shaped, even on a self-reference.
    pytest.param(
        lambda: _flags("$(inputs.verbose.basename)"), _BOOLEAN_VALUE_FROM,
        id="basename_suffixed_value_from_on_a_boolean_binding",
    ),
    # A staged path is always truthy in Groovy, so the flag would never clear.
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("MAKE", out=["out"]), step("SORT", **{"in": {"reverse": "MAKE/out"}})]),
        [tool("MAKE", outputs={"out": {"type": "File", "outputBinding": {"glob": "f.txt"}}}), _SORT],
    ), r"reverse.*boolean source", id="file_output_wired_into_a_boolean_flag"),
    # Groovy truthiness of a string would decide the flag, not the source value.
    *(
        pytest.param(
            lambda supplied=supplied: _string_into_flag(supplied), r"reverse.*boolean source",
            id=f"string_source_wired_into_a_boolean_flag-{supplied!r}",
        )
        for supplied in ("false", "0", "")
    ),
    # A shape _source_values cannot read must still fail closed, not skip the boolean-source check.
    pytest.param(lambda: _flag_wired_to([]), "", id="a_flag_input_wired_to_an_unrecognized_source_shape-list"),
    pytest.param(lambda: _flag_wired_to(42), "", id="a_flag_input_wired_to_an_unrecognized_source_shape-int"),
    # CWL omits the entire binding when its optional source is absent.
    pytest.param(lambda: one_step(tool(
        "OPTIONAL",
        inputs={"message": {
            "type": ["null", "string"],
            "inputBinding": {"position": 1, "valueFrom": "printf optional", "shellQuote": False},
        }},
        requirements={"ShellCommandRequirement": {}},
    ), message=None), r"steps\[0\].run.inputs.message: absent optional values are supported only for ",
        id="absent_optional_input_whose_binding_emits_a_shell_literal"),
    # An optional input with no binding of its own but referenced elsewhere still rejects.
    pytest.param(lambda: one_step(tool(
        "ALIASED", inputs={"message": {"type": ["null", "string"]}},
        arguments=[{"position": 1, "valueFrom": "$(inputs.message)"}],
    ), message=None), _ABSENT_OPTIONAL.format("message"),
        id="absent_optional_input_referenced_via_another_bindings_value_from"),
    # path-qualifier channel construction always stages unconditionally; absence stays rejected.
    pytest.param(
        lambda: one_step(tool("OPTIONAL", inputs={"reference": {"type": ["null", "File"]}}), reference=None),
        _ABSENT_OPTIONAL.format("reference"), id="absent_optional_path_input",
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc(
            [step("REQUIRED", **{"in": {"message": "message"}})], inputs={"message": {"type": ["null", "string"]}}
        ),
        [tool("REQUIRED", inputs={"message": {"type": "string"}})],
    ), "absent required", id="absent_optional_workflow_input_feeding_required_input"),
    pytest.param(
        lambda: synthetic_source(workflow_doc([]), [], workflow_inputs={"extra": "value"}),
        "has no declared workflow input", id="undeclared_workflow_input_values",
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc([], inputs={"out-dir": {"type": "string"}, "out_dir": {"type": "string"}}),
        [], workflow_inputs={"out-dir": "first", "out_dir": "second"},
    ), r"workflow input identifiers 'out-dir', 'out_dir'.*normalize to 'out_dir'",
        id="workflow_input_identifier_collisions"),
    # A repeated id carries `@<position>`; it and an authored id may normalize alike.
    pytest.param(
        lambda: synthetic_source(workflow_doc([step("touch@2"), step("touch_2")]), [tool("touch@2"), tool("touch_2")]),
        r"workflow\.steps: workflow step identifiers 'touch@2', 'touch_2' normalize to 'touch_2'",
        id="step_identifier_collisions_naming_both_steps",
    ),
    pytest.param(lambda: synthetic_source(workflow_doc([step("COLLISION")]), [tool("COLLISION", inputs={
        "out-dir": {"type": "string", "default": "first"},
        "out_dir": {"type": "string", "default": "second"},
    })]), r"tool input identifiers 'out-dir', 'out_dir'.*normalize to 'out_dir'",
        id="tool_port_identifier_collisions"),
    *(
        pytest.param(
            lambda cwl_type=cwl_type, value=value: _boundary_value(cwl_type, value),
            "does not match its supported CWL type", id=f"boundary_values_outside_supported_shape-{cwl_type}",
        )
        for cwl_type, value in (
            ("string", ["not", "a", "string"]),
            ("int", True),
            ("File", {"class": "File", "path": "reference.fa", "secondaryFiles": ["reference.fa.fai"]}),
        )
    ),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("BAD_GLOB", out=["result"])]),
        [tool("BAD_GLOB", outputs={
            "result": {"type": "File", "outputBinding": {"glob": "$(runtime.outdir)/result.txt"}}
        })],
    ), "unsupported CWL expression", id="output_glob_outside_typed_input_subset"),
    pytest.param(lambda: synthetic_source(
        workflow_doc([step("B", **{"in": {"value": "MISSING/out"}})]), [tool("B", inputs={"value": {"type": "File"}})]
    ), "unknown source process", id="unknown_connection_source"),
    pytest.param(
        lambda: _with_wrapper_semantic(_nested_result(), scatter=["source"]), "scatter on a nested workflow step",
        id="scatter_on_a_resolved_subworkflow_call",
    ),
    pytest.param(
        lambda: _with_wrapper_semantic(_nested_result(), when="$(true)"),
        "a when on a nested-workflow step is not supported", id="when_on_a_resolved_subworkflow_call",
    ),
    pytest.param(
        lambda: _with_child(lambda cwl: cwl.update(requirements={"StepInputExpressionRequirement": {}})),
        "StepInputExpressionRequirement is not supported", id="unsupported_nested_workflow_requirements",
    ),
    pytest.param(
        lambda: _with_child(lambda cwl: cwl.update(hints={"ResourceRequirement": {}})),
        r"\.run\.hints: workflow hints are not supported", id="every_unconsumed_nested_workflow_field",
    ),
    pytest.param(
        lambda: _with_child(lambda cwl: cwl["inputs"]["source"].update(default="fallback.txt")),
        r"\.run\.inputs\.\w+\.default: a nested workflow input's default is not",
        id="a_nested_workflow_input_default_before_projection",
    ),
    *(
        pytest.param(
            lambda section=section, name=name: _with_child(lambda cwl: cwl[section][name].update(type="long")),
            re.escape(f".run.{section}.{name}.type: long is not supported"),
            id=f"long_on_a_nested_workflow_port_is_rejected_naming_the_type-{section}",
        )
        for section, name in (("inputs", "source"), ("outputs", "result"))
    ),
]


@pytest.mark.fast
@pytest.mark.parametrize(("build", "pattern"), MATCHES)
def test_a_rejected_source_raises_a_matching_error(
    build: Callable[[], CompiledNextflowSource | CompilationResult], pattern: str
) -> None:
    with pytest.raises(ValueError, match=pattern):
        compiled_source_to_nextflow(build())
