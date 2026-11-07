"""Semantic lowering: supported CWL source becomes typed executable IR."""

# pylint: disable=missing-function-docstring

import copy
from typing import Any

from cwl_utils.expression import interpolate
from hypothesis import example, given, strategies as st
import pytest
from sophios.input_output_nf import nextflow_params, render_nextflow
from sophios.ir.artifacts import CompilationResult
from sophios.nf_types import (
    NfFlag,
    NfLiteral,
    NfPort,
    NfTemplate,
    NfProcessConnection,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)
from sophios.compiler import resolved_view
from sophios.utils_nf import cwl_type_to_nf_qualifier, nextflow_names
from sophios.wic_types import Yaml

from ..budgets import budget
from ..cases import cases, diagnostic, match
from .testkit import lowered, lowered_case, one_step, tool


@pytest.mark.fast
@pytest.mark.parametrize(("cwl_type", "qualifier"), [
    ("File", "path"), ("Directory", "path"), ("string", "val"), ("int", "val"), ("float", "val"), ("boolean", "val"),
    ("File?", "path"), ("Any", None), ("File[]", None), ({"type": "array", "items": "File"}, None),
], ids=["file", "directory", "string", "int", "float", "boolean", "optional-file", "any", "array-shorthand", "array"])
def test_a_cwl_type_has_its_qualifier_or_is_refused(cwl_type: Any, qualifier: str | None) -> None:
    if qualifier is None:
        with pytest.raises(ValueError, match="unsupported CWL type"):
            cwl_type_to_nf_qualifier(cwl_type)
    else:
        assert cwl_type_to_nf_qualifier(cwl_type) == qualifier


# Each real fixture's workflow name, (process, inputs) list, connections and params.
GOLDEN_GRAPHS = {
    "real_supported_result": (
        "wf",
        [("touch", (NfPort("filename", "val", scalar_type="string"),)),
         ("copy", (NfPort("source", "path", path_kind="file"),))],
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
        [("echo_item", (NfPort("item", "val", scalar_type="string"),))],
        (
            NfWorkflowInputConnection("echo_item_item", "echo_item", "item", "dotproduct"),
            NfWorkflowOutputConnection("echo_item", "result", "echo_item_result"),
        ),
        {"echo_item_item": ["alpha", "beta"]},
    ),
    # The subworkflow's step is a namespaced process that reads the outer producer.
    "real_nested_result": (
        "root",
        [("write", (NfPort("message", "val", scalar_type="string"),)),
         ("child_inner_copy", (NfPort("source", "path", path_kind="file"),))],
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
    workflow = lowered(request.getfixturevalue(fixture))
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
    workflow = lowered_case(one_step(run_tool, ["report"], message="hello",
                                     source={"class": "File", "path": "input.txt"}))[0]
    process = workflow.processes[0]
    rendered = render_nextflow(workflow)
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
    flag_tool = tool("FLAGS", inputs={"verbose": {"type": declared, "inputBinding": input_binding}})
    workflow = lowered_case(one_step(flag_tool, verbose=value))[0]
    assert workflow.params == {"verbose": [] if value is None else value}
    flags = (NfFlag("verbose", "--verbose"),) if binding.get("prefix") else ()
    assert workflow.processes[0].command.tokens[1:] == flags


@pytest.mark.fast
@pytest.mark.parametrize(("shorthand", "expanded", "value"), [
    ("string[]", {"type": "array", "items": "string"}, ["a", "b"]),
    ("File[]", {"type": "array", "items": "File"}, [{"class": "File", "path": "a.txt"}]),
    ("int[]?", ["null", {"type": "array", "items": "int"}], [1]),
    (["null", "string[]"], ["null", {"type": "array", "items": "string"}], []),
])
def test_an_array_type_in_shorthand_lowers_as_written_out(shorthand: Any, expanded: Any, value: Any) -> None:
    """CWL reads `T[]` as `{type: array, items: T}` (CWL v1.2, Type DSL); a .cwl tool may declare either."""
    def lower(cwl_type: Any) -> Any:
        return lowered_case(one_step(tool("T", inputs={"x": {"type": cwl_type, "inputBinding": {}}}), x=value))[0]
    assert lower(shorthand) == lower(expanded)


@pytest.mark.fast
def test_forward_conversion_uses_resolved_graph_edges(
    real_supported_result: CompilationResult,
) -> None:
    compilation = copy.deepcopy(real_supported_result)
    steps = compilation.artifact.cwl["steps"]
    steps[1]["in"]["source"]["source"] = "not_the_resolved_edge/result"
    cwl = copy.deepcopy(compilation.artifact.cwl)

    connections = lowered(compilation).connections

    assert compilation.artifact.cwl == cwl, "lowering reads the compilation and never writes it"
    assert NfProcessConnection("touch", "result", "copy", "source") in connections


@pytest.mark.fast
@pytest.mark.parametrize("fixture", ["real_supported_result", "real_scattered_result", "real_nested_result"])
def test_the_names_a_run_maps_back_are_the_names_the_lowering_makes(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    """Each process and workflow output of the lowered model has exactly one Sophios name."""
    result: CompilationResult = request.getfixturevalue(fixture)
    workflow = lowered(result)
    names = nextflow_names(resolved_view(result))
    assert sorted(names["processes"]) == sorted(process.name for process in workflow.processes)
    assert sorted(names["outputs"]) == sorted(
        connection.to_port for connection in workflow.connections
        if isinstance(connection, NfWorkflowOutputConnection)
    )


# design §6, Commands: a `$` before no `(` or `{` is text; the last two were once refused beside a reference, and
# an empty argument is kept.
@budget(200)
@given(st.lists(st.text("$ \n\ta5HOME)}{(\\\"'é").filter(lambda t: "$(" not in t and "${" not in t),
                min_size=1, max_size=3))
@example(["$", "cost $5", "$HOME", "a $ b", "$$", ' mv a "$1" ', "trailing $\n", "$)", "$}", "{$", "($", ""])
def test_a_dollar_starting_no_expression_is_text_as_cwltool_reads_it(texts: list[str]) -> None:
    for text in texts:  # cwltool passes a text with no expression through, and reads one beside a reference
        workflow = lowered_case(one_step(tool("ARGS", inputs={"x": {"type": "string"}}, baseCommand="echo", arguments=[
            text, {"valueFrom": text}, (joined := f"{text}$(inputs.x){text}")]), x="v"))[0]
        _, *bare, beside = workflow.to_dict()["processes"][0]["command"]["tokens"]
        assert bare == [{"segments": [{"kind": "literal", "value": text}]}] * 2
        assert "".join(p.get("value", "V") for p in beside["segments"]) == interpolate(joined, {"inputs": {"x": "V"}})


@pytest.mark.fast
@pytest.mark.parametrize("case", cases(
    "nextflow/lowered", "tool", "out", "job", "workflow", "tools", "lowered", "render", "notes", "params_file"))
def test_a_supported_source_lowers_as_its_case_says(case: Yaml) -> None:
    """A case's source lowers, its documents left as they were, to a model whose payload holds `lowered`, with
    `params_file` as its params file, each `render` fragment in its script, and exactly its `notes` when it lists them
    (tests/core/cases.py reads each form). A case with none of `lowered`, `render`, `notes` or `params_file` asserts
    only that its source lowers."""
    documents = copy.deepcopy(case)
    workflow, notes = lowered_case(case)
    assert case == documents
    assert match(case.get("lowered", {}), workflow.to_dict())
    assert nextflow_params(workflow) == case.get("params_file", nextflow_params(workflow))
    rendered = render_nextflow(workflow)
    assert [text for text in case.get("render", []) if text not in rendered] == []
    assert match(case.get("notes", [diagnostic(note) for note in notes]), [diagnostic(note) for note in notes])
