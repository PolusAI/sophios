"""Semantic lowering: supported CWL source becomes typed executable IR."""

# pylint: disable=missing-function-docstring

import copy
from typing import Any

import pytest
from sophios.input_output_nf import render_nextflow
from sophios.ir.artifacts import CompilationResult
from sophios.nf_types import (
    NfArrayBinding,
    NfBasenameReference,
    NfEnvVar,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfResources,
    NfShellLiteral,
    NfTemplate,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.lang.diagnostics import Diagnostic, Locator, Severity
from sophios.lang.error_codes import SophiosErrorCode
from sophios.utils_nf import (
    compiled_source_to_nextflow, compiled_source_to_nextflow_with_notes, cwl_type_to_nf_qualifier,
)

from .testkit import one_step, step, synthetic_source, tool, workflow_doc


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


# Each real fixture's workflow name, (process, inputs) list, connections and params.
GOLDEN_GRAPHS = {
    "real_supported_result": (
        "wf",
        [("touch", (NfPort("filename", "val"),)), ("copy", (NfPort("source", "path", path_kind="file"),))],
        (
            NfWorkflowInputConnection("touch_filename", "touch", "filename"),
            NfProcessConnection("touch", "result", "copy", "source"),
            NfWorkflowOutputConnection("touch", "result", "touch_result"),
            NfWorkflowOutputConnection("copy", "result", "copy_result"),
        ),
        {"touch_filename": "message.txt"},
    ),
    # A scattered port stays a scalar element port; its parameter carries the whole source array.
    "real_scattered_result": (
        "wf",
        [("echo_item", (NfPort("item", "val"),))],
        (
            NfWorkflowInputConnection("echo_item_item", "echo_item", "item", "dotproduct"),
            NfWorkflowOutputConnection("echo_item", "result", "echo_item_result"),
        ),
        {"echo_item_item": ["alpha", "beta"]},
    ),
    # The subworkflow's step is a namespaced process that reads the outer producer.
    "real_nested_result": (
        "root",
        [("write", (NfPort("message", "val"),)), ("child_inner_copy", (NfPort("source", "path", path_kind="file"),))],
        (
            NfWorkflowInputConnection("write_message", "write", "message"),
            NfProcessConnection("write", "result", "child_inner_copy", "source"),
            NfWorkflowOutputConnection("write", "result", "write_result"),
            NfWorkflowOutputConnection("child_inner_copy", "result", "child_inner_copy_result"),
        ),
        {"write_message": "nested composition"},
    ),
}


@pytest.mark.fast
@pytest.mark.parametrize("fixture", sorted(GOLDEN_GRAPHS))
def test_a_real_compilation_lowers_to_its_exact_graph(fixture: str, request: pytest.FixtureRequest) -> None:
    name, processes, connections, params = GOLDEN_GRAPHS[fixture]
    workflow = compiled_source_to_nextflow(request.getfixturevalue(fixture))
    assert workflow.name == name
    assert [(process.name, process.inputs) for process in workflow.processes] == processes
    assert workflow.connections == connections
    assert workflow.params == params


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
    rose = one_step(run_tool, ["report"], message="hello", source={"class": "File", "path": "input.txt"})
    process = compiled_source_to_nextflow(rose).processes[0]
    rendered = render_nextflow(compiled_source_to_nextflow(rose))
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
@pytest.mark.parametrize(
    "binding", [{"prefix": "--verbose"}, {}, {"separate": True}], ids=["prefix", "no-prefix", "separate-no-prefix"]
)
@pytest.mark.parametrize("value_from", [None, "$(inputs.verbose)"], ids=["no-valuefrom", "self-valuefrom"])
@pytest.mark.parametrize(("declared", "value"), [
    ("boolean", True), ("boolean", False), (["null", "boolean"], True), (["null", "boolean"], False),
    (["null", "boolean"], None),
])
def test_a_boolean_binding_is_a_flag_token_exactly_when_it_has_a_prefix(
    binding: dict[str, Any], value_from: str | None, declared: Any, value: bool | None
) -> None:
    """A flag tests its value and never dereferences it, so false and an absent optional lower alike.

    A valueFrom restating its own input is the same flag, and cwltool accepts
    separate: true without a prefix; only a falsy separate rejects.
    """
    input_binding = {"position": 1, **binding, **({} if value_from is None else {"valueFrom": value_from})}
    rose = one_step(tool("FLAGS", inputs={"verbose": {"type": declared, "inputBinding": input_binding}}), verbose=value)
    workflow = compiled_source_to_nextflow(rose)
    assert workflow.params == {"verbose": [] if value is None else value}
    flags = (NfFlag("verbose", "--verbose"),) if binding.get("prefix") else ()
    assert workflow.processes[0].command.tokens[1:] == flags


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
    rose = one_step(sort_tool, source={"class": "File", "path": "in.txt"}, reverse=True)

    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens

    assert tokens[0] == NfTemplate((NfLiteral("SORT"),))
    assert tokens[1] == NfTemplate((NfLiteral("--stable"),))
    assert tokens[2] == NfFlag("reverse", "-r")
    assert tokens[3] == NfTemplate((NfInputReference("source"),))


@pytest.mark.fast
def test_array_of_scalars_lowers_to_a_val_array_port_with_a_binding() -> None:
    names_tool = tool(
        "NAMES",
        inputs={
            "names": {
                "type": {"type": "array", "items": "string"},
                "inputBinding": {"position": 1, "prefix": "--name"},
            }
        },
    )
    rose = one_step(names_tool, names=["alice", "bob"])

    process = compiled_source_to_nextflow(rose).processes[0]

    assert process.inputs[0] == NfPort("names", "val", is_array=True)
    assert process.command.tokens[-1] == NfArrayBinding("names", "--name")


@pytest.mark.fast
def test_array_of_files_lowers_to_a_path_array_port() -> None:
    cat_tool = tool(
        "CAT",
        inputs={
            "sources": {
                "type": {"type": "array", "items": "File"},
                "inputBinding": {"position": 1},
            }
        },
    )
    rose = one_step(cat_tool, sources=[{"class": "File", "path": "a.txt"}, {"class": "File", "path": "b.txt"}])

    process = compiled_source_to_nextflow(rose).processes[0]

    assert process.inputs[0] == NfPort("sources", "path", is_array=True)
    assert process.command.tokens[-1] == NfArrayBinding("sources")


@pytest.mark.fast
def test_array_binding_without_a_prefix_lowers_correctly() -> None:
    cat_tool = tool(
        "CAT",
        inputs={"sources": {"type": {"type": "array", "items": "string"}, "inputBinding": {}}},
    )
    rose = one_step(cat_tool, sources=["a", "b"])

    process = compiled_source_to_nextflow(rose).processes[0]

    assert process.command.tokens[-1] == NfArrayBinding("sources", None)


@pytest.mark.fast
def test_empty_array_value_is_accepted_and_distinct_from_absent() -> None:
    """A supplied [] is a present, valid, zero-length value, not absence."""
    names_tool = tool(
        "NAMES",
        inputs={
            "names": {
                "type": {"type": "array", "items": "string"},
                "inputBinding": {"position": 1, "prefix": "--name"},
            }
        },
    )
    rose = one_step(names_tool, names=[])

    assert compiled_source_to_nextflow(rose).params == {"names": []}


@pytest.mark.fast
def test_shell_quote_false_literal_lowers_to_a_raw_shell_literal_token() -> None:
    redirect_tool = tool(
        "REDIRECT",
        outputs={"result": {"type": "File", "outputBinding": {"glob": "out.txt"}}},
        requirements={"ShellCommandRequirement": {}},
        arguments=[
            {"position": 1, "valueFrom": "hello"},
            {"position": 2, "valueFrom": ">>", "shellQuote": False},
            {"position": 3, "valueFrom": "out.txt"},
        ],
    )
    rose = one_step(redirect_tool, ["result"])

    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens

    assert tokens[0] == NfTemplate((NfLiteral("REDIRECT"),))
    assert tokens[1] == NfTemplate((NfLiteral("hello"),))
    assert tokens[2] == NfShellLiteral(">>")
    assert tokens[3] == NfTemplate((NfLiteral("out.txt"),))


@pytest.mark.fast
def test_shell_quote_false_takes_precedence_over_boolean_flag_lowering() -> None:
    """A literal valueFrom fully overrides type-specific binding, even on a boolean input."""
    literal_tool = tool(
        "LITERAL",
        inputs={
            "flag": {
                "type": "boolean",
                "inputBinding": {"position": 1, "valueFrom": "--literal", "shellQuote": False},
            }
        },
        requirements={"ShellCommandRequirement": {}},
    )
    rose = one_step(literal_tool, flag=True)

    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens

    assert tokens[1] == NfShellLiteral("--literal")
    assert not any(isinstance(token, NfFlag) for token in tokens)


@pytest.mark.fast
def test_command_of_only_shell_literals_still_runs_a_program() -> None:
    """A shell literal is itself executable command text, not an empty command."""
    literal_tool = tool(
        "LITERAL",
        requirements={"ShellCommandRequirement": {}},
        baseCommand=None,
        arguments=[{"position": 1, "valueFrom": "printf ok", "shellQuote": False}],
    )
    rose = one_step(literal_tool)

    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens

    assert tokens == (NfShellLiteral("printf ok"),)


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
    rose = one_step(copy_tool, ["result"], source={"class": "File", "path": "lines.txt"})

    process = compiled_source_to_nextflow(rose).processes[0]

    expected = (NfBasenameReference("source"), NfLiteral(".copy"))
    assert process.outputs[0].glob == NfTemplate(expected)
    assert process.command.tokens[-1] == NfTemplate(expected)
    assert process.command.stdout == NfTemplate(
        (NfBasenameReference("source"), NfLiteral(".log"))
    )


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
    rose = synthetic_source(
        workflow_doc(
            [step("DEFAULT", out=["result"])],
            outputs={"result": {"type": "File", "outputSource": "DEFAULT/result"}},
        ),
        [default_tool],
    )
    workflow = compiled_source_to_nextflow(rose)
    assert workflow.params == {"_DEFAULT_message": "hello default"}
    assert NfWorkflowInputConnection("_DEFAULT_message", "_DEFAULT", "message") in workflow.connections
    assert "Channel.value(__sophios_input_9f72e('_DEFAULT_message', params._DEFAULT_message, 'string'))" in (
        render_nextflow(workflow)
    )


@pytest.mark.fast
def test_every_parameter_records_its_declared_type() -> None:
    """Workflow inputs carry their own declarations; a tool default carries its tool input's."""
    take = tool(
        "TAKE",
        inputs={
            "name": {"type": "string", "inputBinding": {"position": 1}},
            "count": {"type": ["null", "int"]},
            "files": {"type": {"type": "array", "items": "File"}, "inputBinding": {"position": 2}},
            "dirs": {"type": ["null", {"type": "array", "items": "Directory"}], "inputBinding": {"position": 3}},
            "ratio": {"type": "float", "default": 0.5},
        },
        outputs={"result": {"type": "File", "outputBinding": {"glob": "result.txt"}}},
    )
    declared = {"name": "string", "count": ["null", "int"], "files": {"type": "array", "items": "File"},
                "dirs": ["null", {"type": "array", "items": "Directory"}], "unread": "boolean"}
    rose = synthetic_source(
        workflow_doc(
            [step("TAKE", **{"in": {name: name for name in ("name", "count", "files", "dirs")}, "out": ["result"]})],
            inputs={name: {"type": cwl_type} for name, cwl_type in declared.items()},
            outputs={"result": {"type": "File", "outputSource": "TAKE/result"}},
        ),
        [take],
        workflow_inputs={"name": "n", "count": 1, "files": [], "dirs": [], "unread": True},
    )
    assert compiled_source_to_nextflow(rose).param_types == {
        "name": "string", "count": "int?", "files": "File[]", "dirs": "Directory[]?", "unread": "boolean",
        "_TAKE_ratio": "float",
    }


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
    rose = synthetic_source(
        workflow_doc(
            [step("DEFAULT")],
            inputs={"DEFAULT_message": {"type": "string"}},
        ),
        [default_tool],
        workflow_inputs={"DEFAULT_message": "workflow value"},
    )
    with pytest.raises(
        ValueError,
        match="workflow parameter identifiers 'DEFAULT_message', 'DEFAULT~message' normalize to '_DEFAULT_message'",
    ):
        compiled_source_to_nextflow(rose)


@pytest.mark.fast
def test_maps_docker_requirement() -> None:
    container = tool(
        "CONTAINER",
        requirements={"DockerRequirement": {"dockerPull": "ubuntu:24.04"}},
    )
    rose = one_step(container)
    assert compiled_source_to_nextflow(rose).processes[0].container == "ubuntu:24.04"


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
    ceiling_only = tool(
        "CEILING_ONLY",
        requirements={"ResourceRequirement": {"coresMax": 2, "ramMax": 1024}},
    )
    rose = synthetic_source(
        workflow_doc([step("RESOURCES"), step("CEILING_ONLY")]),
        [resources, ceiling_only],
    )
    # CWL reserves the minimum; a maximum alone is all there is to request.
    assert [process.resources for process in compiled_source_to_nextflow(rose).processes] == [
        NfResources(1, 512),
        NfResources(2, 1024),
    ]


@pytest.mark.fast
def test_preserves_fanout() -> None:
    producer = tool(
        "A", outputs={"out": {"type": "File", "outputBinding": {"glob": "out.txt"}}}
    )
    consumer_b = tool("B", inputs={"value": {"type": "File"}})
    consumer_c = tool("C", inputs={"value": {"type": "File"}})
    rose = synthetic_source(
        workflow_doc([
            step("A", out=["out"]),
            step("B", **{"in": {"value": "A/out"}}),
            step("C", **{"in": {"value": "A/out"}}),
        ]),
        [producer, consumer_b, consumer_c],
    )
    connections = compiled_source_to_nextflow(rose).connections
    assert len([
        edge
        for edge in connections
        if isinstance(edge, NfProcessConnection)
        and edge.from_process == "_A"
        and edge.from_port == "out"
    ]) == 2


@pytest.mark.fast
def test_forward_conversion_uses_resolved_graph_edges(
    real_supported_result: CompilationResult,
) -> None:
    compilation = copy.deepcopy(real_supported_result)
    steps = compilation.artifact.cwl["steps"]
    steps[1]["in"]["source"]["source"] = "not_the_resolved_edge/result"

    connections = compiled_source_to_nextflow(compilation).connections

    assert NfProcessConnection("touch", "result", "copy", "source") in connections


@pytest.mark.fast
def test_conversion_does_not_mutate_compilation_result(
    real_supported_result: CompilationResult,
) -> None:
    before = copy.deepcopy(real_supported_result.artifact.cwl)
    compiled_source_to_nextflow(real_supported_result)
    assert real_supported_result.artifact.cwl == before


@pytest.mark.fast
@pytest.mark.parametrize("base_command", ["pre$(inputs.s)", ["pre$(inputs.s)"]], ids=["string", "list"])
def test_base_command_and_prefix_are_literals(base_command: Any) -> None:
    literal = tool(
        "LITERAL",
        inputs={"s": {"type": "string", "inputBinding": {"prefix": "-$(inputs.s)"}}},
        baseCommand=base_command,
    )
    rose = one_step(literal, s="x")
    tokens = compiled_source_to_nextflow(rose).processes[0].command.tokens
    assert tokens == (
        NfTemplate((NfLiteral("pre$(inputs.s)"),)),
        NfTemplate((NfLiteral("-$(inputs.s)"),)),
        NfTemplate((NfInputReference("s"),)),
    )


def _boolean_valuefrom_tokens(binding: dict[str, Any], *, owner: dict[str, Any] | None = None) -> tuple[Any, ...]:
    inputs: dict[str, Any] = {"b": {"type": "boolean"}}
    arguments: list[Any] = []
    if owner is None:
        arguments.append({"valueFrom": "$(inputs.b)", **binding})
    else:
        inputs["s"] = {**owner, "inputBinding": {"valueFrom": "$(inputs.b)", **binding}}
    values: dict[str, Any] = {"b": True, "s": "x"}
    rose = one_step(
        tool("FLAG", inputs=inputs, baseCommand="echo", arguments=arguments), **{name: values[name] for name in inputs}
    )
    return tuple(compiled_source_to_nextflow(rose).processes[0].command.tokens[1:])


@pytest.mark.fast
@pytest.mark.parametrize("owner", [None, {"type": "string"}], ids=["argument", "string-input-binding"])
def test_a_whole_boolean_valuefrom_is_a_flag(owner: dict[str, Any] | None) -> None:
    assert _boolean_valuefrom_tokens({"prefix": "--flag"}, owner=owner) == (NfFlag("b", "--flag"),)
    assert _boolean_valuefrom_tokens({}, owner=owner) == ()


@pytest.mark.fast
def test_a_boolean_valuefrom_on_an_optional_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="a boolean valueFrom on optional input 's' is not supported"):
        _boolean_valuefrom_tokens({"prefix": "--flag"}, owner={"type": "string?"})


def _argument_tokens(*arguments: Any) -> tuple[Any, ...]:
    rose = synthetic_source(
        workflow_doc([step("ARGS", **{"in": {"x": "x"}})], inputs={"x": "string"}),
        [tool("ARGS", inputs={"x": {"type": "string"}}, baseCommand="echo", arguments=list(arguments))],
        workflow_inputs={"x": "v"},
    )
    return tuple(compiled_source_to_nextflow(rose).processes[0].command.tokens[1:])


# design §6, Commands: a `$` that is not followed by `(` or `{` is plain text.
PLAIN_DOLLARS = ["$", "cost $5", "$HOME", "a $ b", "$$", ' mv a "$1" ', "trailing $\n", "$)", "$}"]


@pytest.mark.fast
@pytest.mark.parametrize("text", PLAIN_DOLLARS)
def test_a_dollar_not_starting_an_expression_is_plain_text(text: str) -> None:
    assert _argument_tokens(text, {"valueFrom": text}) == (NfTemplate((NfLiteral(text),)),) * 2


@pytest.mark.fast
@pytest.mark.parametrize("text", PLAIN_DOLLARS)
def test_a_plain_dollar_stays_text_beside_a_reference(text: str) -> None:
    [token] = _argument_tokens(f"{text}$(inputs.x){text}")
    assert token == NfTemplate((NfLiteral(text.lstrip()), NfInputReference("x"), NfLiteral(text.rstrip())))


@pytest.mark.fast
@pytest.mark.parametrize("text", ["$(runtime.cores)", "${return 1}", "cost $5 $(inputs.x.size)", "$$(self)"])
def test_a_dollar_starting_any_other_expression_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="unsupported CWL expression"):
        _argument_tokens(text)


@pytest.mark.fast
@pytest.mark.parametrize("env_def", [
    [{"envName": "A", "envValue": "cost $5"}, {"envName": "B", "envValue": " $(inputs.s) "}],
    {"A": "cost $5", "B": {"envValue": " $(inputs.s) "}},
], ids=["list", "map"])
def test_env_var_requirement_lowers_each_variable_to_an_export(env_def: Any) -> None:
    rose = synthetic_source(
        workflow_doc([step("ENV", **{"in": {"s": "s"}})], inputs={"s": "string"}),
        [tool("ENV", inputs={"s": {"type": "string"}}, requirements={"EnvVarRequirement": {"envDef": env_def}})],
        workflow_inputs={"s": "v"},
    )
    workflow = compiled_source_to_nextflow(rose)
    assert workflow.processes[0].command.env == (
        NfEnvVar("A", NfTemplate((NfLiteral("cost $5"),))), NfEnvVar("B", NfTemplate((NfInputReference("s"),))),
    )
    script = render_nextflow(workflow).split('    """\n')[1]
    assert script.splitlines()[:2] == [
        "    export A=${__sophios_shell_quote_9f72e('cost $5')}",
        "    export B=${__sophios_shell_quote_9f72e(s.toString())}",
    ]


def _gpu_lowering(cuda: Any, section: str = "hints") -> tuple[Any, tuple[Diagnostic, ...]]:
    rose = synthetic_source(
        workflow_doc([step("GPU")]), [tool("GPU", **{section: {"cwltool:CUDARequirement": cuda}})]
    )
    return compiled_source_to_nextflow_with_notes(rose)


@pytest.mark.fast
@pytest.mark.parametrize("section", ["requirements", "hints"])
@pytest.mark.parametrize(("cuda", "count"), [
    ({}, 1),
    ({"cudaDeviceCountMin": 2}, 2),
    ({"cudaDeviceCountMax": 3}, 3),
    ({"cudaDeviceCountMin": 2, "cudaDeviceCountMax": 4}, 2),
])
def test_cuda_requirement_lowers_to_the_accelerator_directive(cuda: Any, count: int, section: str) -> None:
    """The count cwltool's job requests: the minimum, else the maximum, else 1 (design §6)."""
    workflow, notes = _gpu_lowering(cuda, section)
    assert workflow.processes[0].resources == NfResources(accelerator=count)
    assert f"\n    accelerator {count}\n" in render_nextflow(workflow)
    assert notes == ()


@pytest.mark.fast
def test_the_unchecked_cuda_fields_are_notes_and_leave_the_model_unchanged() -> None:
    plain, _ = _gpu_lowering({"cudaDeviceCountMin": 1})
    workflow, notes = _gpu_lowering(
        {"cudaDeviceCountMin": 1, "cudaVersionMin": "11.4", "cudaComputeCapability": ["3.0", "7.5"]}
    )
    assert workflow == plain
    assert [(note.severity, note.code, note.locator) for note in notes] == [
        (Severity.NOTE, SophiosErrorCode.UNCHECKED_CUDA_FIELD, Locator("GPU"))
    ] * 2
    assert [note.message.split(" is not checked")[0] for note in notes] == [
        "cwltool:CUDARequirement cudaVersionMin '11.4'",
        "cwltool:CUDARequirement cudaComputeCapability ['3.0', '7.5']",
    ]
