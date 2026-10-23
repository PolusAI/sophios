"""Executable IR invariants: identifiers, immutability, hydration, and graph rules."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
import json
from pathlib import Path
import re
from typing import Any, cast

import pytest

from sophios.input_output_nf import render_nextflow
from sophios.nf_expr import parse
from sophios.nf_reader import parse_nf_text
from sophios.nf_symbols import (
    NEXTFLOW_SCRIPT_DECLARED_NAMES, NEXTFLOW_SCRIPT_NAMES, is_nextflow_identifier, normalize_nextflow_identifier,
)
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfConnection,
    NfEnvVar,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfShellLiteral,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .testkit import command, output_port, ref, template


@pytest.mark.fast
def test_port_dict_roundtrip() -> None:
    port = NfPort("reads", "path", path_kind="directory")
    assert NfPort.from_dict(port.to_dict()) == port


@pytest.mark.fast
def test_process_dict_roundtrip() -> None:
    process = NfProcess(
        name="ALIGN",
        inputs=[NfPort("reads", "path")],
        outputs=[output_port("bam", "result.bam")],
        command=command("align", "reads"),
        container="aligner:1",
        resources=NfResources(2, 1024),
    )
    assert NfProcess.from_dict(process.to_dict()) == process


@pytest.mark.fast
def test_workflow_dict_roundtrip() -> None:
    workflow = ExecutableNextflowWorkflow(
        name="wf",
        processes=[NfProcess("ECHO", [], [output_port("out")], command("echo", "hi"))],
        connections=[NfWorkflowOutputConnection("ECHO", "out", "result")],
        params={"message": "hello"}, param_types={"message": "string"},
    )
    assert ExecutableNextflowWorkflow.from_dict(workflow.to_dict()) == workflow


@pytest.mark.fast
def test_json_roundtrip_is_deterministic() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {"b": 2, "a": 1}, {"b": "int", "a": "int"})
    serialized = workflow.to_json()
    assert serialized == workflow.to_json()
    assert ExecutableNextflowWorkflow.from_json(serialized) == workflow


@pytest.mark.fast
def test_executable_schema_declares_version_and_kind() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {})
    payload = workflow.to_dict()

    current = ExecutableNextflowWorkflow.SCHEMA_VERSION
    assert payload["schema_version"] == current
    assert payload["representation_kind"] == "executable"

    for version in (current - 1, current + 1):
        payload["schema_version"] = version
        with pytest.raises(ValueError, match=re.escape(
            f"executable Nextflow schema version {version} is not the current version {current}"
        )):
            ExecutableNextflowWorkflow.from_dict(payload)

    payload["schema_version"] = current
    payload["representation_kind"] = "structural"
    with pytest.raises(ValueError, match="representation kind"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_executable_values_are_deeply_immutable_and_hashable() -> None:
    process = NfProcess(
        "P",
        [NfPort("message", "val")],
        [output_port("result", "result.txt")],
        command("touch", "result.txt"),
    )
    workflow = ExecutableNextflowWorkflow(
        "wf",
        [process],
        [
            NfWorkflowInputConnection("message", "P", "message"),
            NfWorkflowOutputConnection("P", "result", "result"),
        ],
        {"message": {"nested": ["hello"]}}, {"message": "string"},
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
def test_executable_ir_has_no_opaque_or_magic_directive_fields() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf", [NfProcess("P", [], [], command("true"))], [], {}
    ).to_dict()
    payload["directives"] = {"_unparsed": "workflow.onComplete { ... }"}
    with pytest.raises(ValueError, match="unknown fields"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_uses_nextflow_identifier_symbols() -> None:
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
@pytest.mark.parametrize("qualifier", ["tuple", "env", "stdin", "each"])
def test_rejects_qualifiers_without_an_approved_lowering(qualifier: str) -> None:
    with pytest.raises(ValueError, match="qualifier"):
        NfPort("reads", qualifier)


@pytest.mark.fast
@pytest.mark.parametrize("prefix", ["", "   "])
def test_flag_requires_a_non_empty_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        NfFlag("reverse", prefix)


@pytest.mark.fast
def test_public_module_exports_flag_and_command_token() -> None:
    from sophios.api.python import nextflow

    assert nextflow.NfFlag is NfFlag
    assert nextflow.NfCommandToken is NfCommandToken
    assert "NfFlag" in nextflow.__all__
    assert "NfCommandToken" in nextflow.__all__
    # Every member of the command-token union is nameable through the public
    # module, or a caller cannot construct or match what it gets back.
    assert nextflow.NfArrayBinding is NfArrayBinding
    assert "NfArrayBinding" in nextflow.__all__


@pytest.mark.fast
def test_flags_are_unrepresentable_outside_command_position() -> None:
    with pytest.raises(TypeError) as excinfo:
        NfTemplate((cast(Any, NfFlag("reverse", "-r")),))
    # The message is built from NfTemplateSegment, so every accepted kind is
    # named without a second hand-maintained list beside the isinstance check.
    message = str(excinfo.value)
    assert message == (
        "template segments must be one of: NfLiteral, NfInputReference, NfBasenameReference"
    )


@pytest.mark.fast
def test_basename_segment_accepts_both_path_kinds() -> None:
    # The design claims File and Directory inputs alike; a directory port is
    # staged under its own name too, so both path kinds carry a basename.
    command = NfCommand((NfTemplate((NfLiteral("cp"),)), NfTemplate((NfBasenameReference("source"),))))
    for path_kind in ("file", "directory"):
        process = NfProcess(
            "COPY", [NfPort("source", "path", path_kind=path_kind)], [], command
        )
        assert process.inputs[0].path_kind == path_kind


def _proc(
    port: NfPort, *tokens: NfCommandToken, stdout: NfTemplate | None = None, outputs: tuple[NfPort, ...] = ()
) -> NfProcess:
    """A process with the one input `port` whose command is `echo` and then `tokens`."""
    return NfProcess("P", [port], list(outputs), NfCommand((NfTemplate((NfLiteral("echo"),)), *tokens), stdout=stdout))


_BASENAME = NfTemplate((NfBasenameReference("v"),))
_PLAIN = NfTemplate((NfInputReference("v"),))
_ARRAY_ONLY = "array-marked inputs may only be referenced by array bindings"


@pytest.mark.fast
@pytest.mark.parametrize(("build_process", "message"), [
    (lambda: _proc(NfPort("other", "val"), NfFlag("v", "-r")), "unknown inputs"),
    (lambda: _proc(NfPort("other", "val"), NfArrayBinding("v", "-n")), "unknown inputs"),
    (lambda: _proc(NfPort("v", "path"), NfFlag("v", "-r")), "flag tokens must reference val inputs"),
    # The design claims a basename is valid in every template position, so each is checked.
    (lambda: _proc(NfPort("v", "val"), _BASENAME), "basename references must target path inputs"),
    (lambda: _proc(NfPort("v", "val"), stdout=_BASENAME), "basename references must target path inputs"),
    (
        lambda: _proc(NfPort("v", "val"), outputs=(NfPort("out", "path", "out", _BASENAME),)),
        "basename references must target path inputs",
    ),
    (
        lambda: _proc(NfPort("v", "val"), NfArrayBinding("v", "--name")),
        "array bindings must reference array-marked inputs",
    ),
    # A plain reference renders values.toString(), so a two-item array reaches the command line as "[a, b]".
    (
        lambda: _proc(NfPort("v", "val", is_array=True), NfTemplate((NfLiteral("--joined="), NfInputReference("v")))),
        _ARRAY_ONLY,
    ),
    (lambda: _proc(NfPort("v", "val", is_array=True), _PLAIN), _ARRAY_ONLY),
    (lambda: _proc(NfPort("v", "val", is_array=True), stdout=_PLAIN), _ARRAY_ONLY),
    (lambda: _proc(NfPort("v", "val", is_array=True), outputs=(NfPort("out", "path", "out", _PLAIN),)), _ARRAY_ONLY),
    # values.name.toString() is a GPath spread over the list, so a File[] read as a basename renders
    # --tag=[a.txt, b.txt]; in glob position the rendered name is a list, breaking the literal-name rule.
    (
        lambda: _proc(NfPort("v", "path", is_array=True), NfTemplate((NfLiteral("--tag="), NfBasenameReference("v")))),
        _ARRAY_ONLY,
    ),
    (
        lambda: _proc(
            NfPort("v", "path", is_array=True),
            outputs=(NfPort("out", "path", "out", NfTemplate((NfBasenameReference("v"), NfLiteral(".done")))),),
        ),
        _ARRAY_ONLY,
    ),
    # A flag lets list truthiness decide whether its prefix appears; checked on hydration too.
    (
        lambda: NfProcess.from_dict(
            {**(payload := _proc(NfPort("v", "val"), NfFlag("v", "--verbose")).to_dict()),
             "inputs": [{**payload["inputs"][0], "is_array": True}]}
        ),
        f"{_ARRAY_ONLY}: v",
    ),
])
def test_a_process_reference_to_an_input_it_does_not_admit_is_rejected(
    build_process: Callable[[], NfProcess], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_process()


@pytest.mark.fast
@pytest.mark.parametrize(("part", "payload"), [
    (NfArrayBinding("names"), {"kind": "array", "name": "names", "prefix": None}),
    (NfFlag("reverse", "-r"), {"kind": "flag", "name": "reverse", "prefix": "-r"}),
    (NfBasenameReference("source"), {"kind": "basename", "name": "source"}),
    (NfShellLiteral(">>"), {"kind": "shell_literal", "text": ">>"}),
])
def test_a_command_part_writes_its_payload_fields(part: Any, payload: dict[str, Any]) -> None:
    """A round trip cannot see a field renamed on both sides, so each part's payload is pinned."""
    assert part.to_dict() == payload


@pytest.mark.fast
@pytest.mark.parametrize("prefix", ["", "   "])
def test_array_binding_rejects_a_blank_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        NfArrayBinding("names", prefix)


@pytest.mark.fast
def test_every_parameter_has_one_declared_type_its_sinks_agree_with() -> None:
    process = NfProcess("ONE", [NfPort("value", "val")], [], command("true"))
    connections = [NfWorkflowInputConnection("value", "ONE", "value")]
    with pytest.raises(ValueError, match="give a type to exactly the parameters in params: value"):
        ExecutableNextflowWorkflow("WF", [process], connections, {"value": "a"})
    with pytest.raises(ValueError, match="give a type to exactly the parameters in params: extra"):
        ExecutableNextflowWorkflow("WF", [process], connections, {"value": "a"}, {"value": "string", "extra": "int"})
    with pytest.raises(ValueError, match="must be one of PARAM_TYPES, not 'long'"):
        ExecutableNextflowWorkflow("WF", [process], connections, {"value": 1}, {"value": "long"})
    for declared in ("string[]", "File"):
        with pytest.raises(ValueError, match=re.escape(f"'value' of type '{declared}' cannot feed ONE.value")):
            ExecutableNextflowWorkflow("WF", [process], connections, {"value": "a"}, {"value": declared})
    for declared in ("boolean", "int", "float", "string", "string?"):
        ExecutableNextflowWorkflow("WF", [process], connections, {"value": "a"}, {"value": declared})


@pytest.mark.fast
def test_process_connection_rejects_mismatched_cardinality() -> None:
    # A scalar output driving an array-marked port is a type error. The port's
    # list arity would let Nextflow deliver it as a one-element list and hide
    # the mismatch, so it is diagnosed here.
    producer = NfProcess(
        "PRODUCE", [], [output_port("out", "out.txt")], command("touch", "out.txt")
    )
    consumer = NfProcess(
        "JOIN",
        [NfPort("values", "path", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("cat"),)), NfArrayBinding("values"))),
    )
    with pytest.raises(ValueError, match="incompatible channel cardinalities"):
        ExecutableNextflowWorkflow(
            "WF",
            [producer, consumer],
            [NfProcessConnection("PRODUCE", "out", "JOIN", "values")],
            {},
        )


@pytest.mark.fast
def test_array_binding_is_valid_against_path_or_val_ports() -> None:
    """Unlike a flag, an array binding may target either qualifier."""
    for qualifier in ("val", "path"):
        process = NfProcess(
            "NAMES",
            [NfPort("names", qualifier, is_array=True)],
            [],
            NfCommand((NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names"))),
        )
        assert process.inputs[0].is_array is True


@pytest.mark.fast
def test_array_typed_outputs_are_unrepresentable() -> None:
    glob = NfTemplate((NfLiteral("result.txt"),))
    with pytest.raises(ValueError, match="array-typed outputs are deferred"):
        NfProcess(
            "MAKE",
            [],
            [NfPort("result", "path", "result", glob, is_array=True)],
            NfCommand((NfTemplate((NfLiteral("true"),)),)),
        )


@pytest.mark.fast
def test_array_marker_participates_in_channel_qualifier_consistency() -> None:
    """An array port and a scalar port must never share one workflow parameter."""
    scalar_process = NfProcess("SCALAR", [NfPort("shared", "val")], [], command("true"))
    array_process = NfProcess(
        "ARRAY",
        [NfPort("shared", "val", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("true"),)), NfArrayBinding("shared"))),
    )
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [scalar_process, array_process],
            [
                NfWorkflowInputConnection("shared", "SCALAR", "shared"),
                NfWorkflowInputConnection("shared", "ARRAY", "shared"),
            ],
            {"shared": ["a", "b"]}, {"shared": "string"},
        )


@pytest.mark.fast
def test_shell_literal_rejects_non_string_text() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        NfShellLiteral(cast(Any, 3))
    with pytest.raises(ValueError, match="NUL"):
        NfShellLiteral("bad\x00text")


@pytest.mark.fast
def test_shell_literals_are_unrepresentable_outside_command_position() -> None:
    with pytest.raises(TypeError, match="template segments must be one of"):
        NfTemplate((cast(Any, NfShellLiteral(">>")),))


@pytest.mark.fast
def test_public_module_exports_shell_literal() -> None:
    from sophios.api.python import nextflow

    assert nextflow.NfShellLiteral is NfShellLiteral
    assert "NfShellLiteral" in nextflow.__all__


def _env_process(*env: NfEnvVar, qualifier: str = "val") -> NfProcess:
    return NfProcess("ENV", [NfPort("x", qualifier)], [], NfCommand((NfTemplate((NfLiteral("env"),)),), env=env))


@pytest.mark.fast
def test_an_env_var_is_literal_text_or_one_input_reference_and_survives_hydration() -> None:
    process = _env_process(NfEnvVar("A", template("a $b")), NfEnvVar("_B2", template(ref("x"))))
    payload = ExecutableNextflowWorkflow(
        "wf", [process], [NfWorkflowInputConnection("x", "ENV", "x")], {"x": "v"}, {"x": "string"}
    )
    assert ExecutableNextflowWorkflow.from_dict(payload.to_dict()) == payload
    for value in (template("a", ref("x")), NfTemplate((NfBasenameReference("x"),))):
        with pytest.raises(ValueError, match="literal text or one input reference"):
            NfEnvVar("A", value)


@pytest.mark.fast
@pytest.mark.parametrize("name", ["", "1A", "A-B", "A B", "A=B"])
def test_an_env_var_name_is_a_shell_variable_name(name: str) -> None:
    with pytest.raises(ValueError, match="shell variable name"):
        NfEnvVar(name, template("v"))


@pytest.mark.fast
def test_a_command_sets_an_env_var_once_from_a_val_input() -> None:
    with pytest.raises(ValueError, match="more than once"):
        _env_process(NfEnvVar("A", template("1")), NfEnvVar("A", template("2")))
    with pytest.raises(ValueError, match="environment variables must reference val inputs: x"):
        _env_process(NfEnvVar("A", template(ref("x"))), qualifier="path")


@pytest.mark.fast
def test_stage_as_port_survives_hydration() -> None:
    port = NfPort("source", "path", stage_as="renamed.txt")
    assert NfPort.from_dict(port.to_dict()) == port
    assert port.to_dict()["stage_as"] == "renamed.txt"


@pytest.mark.fast
def test_stage_as_requires_a_path_qualifier() -> None:
    with pytest.raises(ValueError, match="only path ports may declare a stage_as"):
        NfPort("source", "val", stage_as="renamed.txt")


@pytest.mark.fast
def test_stage_as_rejects_array_marked_ports() -> None:
    with pytest.raises(ValueError, match="array-marked ports cannot declare a stage_as"):
        NfPort("source", "path", is_array=True, stage_as="renamed.txt")


@pytest.mark.fast
@pytest.mark.parametrize("stage_as", ["", "   ", "sub/dir.txt", "reads*.fq", "sample?.txt"])
def test_stage_as_rejects_blank_or_separator_containing_names(stage_as: str) -> None:
    with pytest.raises(ValueError, match="stage_as"):
        NfPort("source", "path", stage_as=stage_as)


@pytest.mark.fast
def test_process_rejects_duplicate_stage_as_literal() -> None:
    with pytest.raises(ValueError, match="same literal name"):
        NfProcess(
            "STAGE",
            [
                NfPort("a", "path", stage_as="same.txt"),
                NfPort("b", "path", stage_as="same.txt"),
            ],
            [],
            NfCommand((NfTemplate((NfLiteral("cat"),)), NfTemplate((NfLiteral("same.txt"),)))),
        )


@pytest.mark.fast
def test_process_rejects_referencing_a_renamed_input_elsewhere_plainly() -> None:
    with pytest.raises(ValueError, match="references a renamed IWDR input elsewhere"):
        NfProcess(
            "STAGE",
            [NfPort("source", "path", stage_as="renamed.txt")],
            [],
            NfCommand((
                NfTemplate((NfLiteral("cat"),)),
                NfTemplate((NfInputReference("source"),)),
            )),
        )


@pytest.mark.fast
def test_process_rejects_referencing_a_renamed_input_elsewhere_via_basename() -> None:
    with pytest.raises(ValueError, match="references a renamed IWDR input elsewhere"):
        NfProcess(
            "STAGE",
            [NfPort("source", "path", stage_as="renamed.txt")],
            [output_port("result", NfTemplate((NfBasenameReference("source"),)))],
            NfCommand((NfTemplate((NfLiteral("cat"),)), NfTemplate((NfLiteral("renamed.txt"),)))),
        )


@pytest.mark.fast
def test_rejects_duplicate_process_names() -> None:
    process = NfProcess("P", [], [], command("true"))
    with pytest.raises(ValueError, match="duplicate process"):
        ExecutableNextflowWorkflow("wf", [process, process], [], {})


@pytest.mark.fast
def test_rejects_connection_to_unknown_process() -> None:
    with pytest.raises(ValueError, match="unknown destination process"):
        ExecutableNextflowWorkflow(
            "wf",
            [],
            [NfWorkflowInputConnection("message", "MISSING", "message")],
            {"message": "hi"}, {"message": "string"},
        )


@pytest.mark.fast
def test_rejects_nullable_boundary_and_duplicate_workflow_emits() -> None:
    with pytest.raises((TypeError, ValueError)):
        NfWorkflowInputConnection("message", cast(Any, None), "message")

    first = NfProcess("A", [], [output_port("out", "a.txt")], command("touch", "a.txt"))
    second = NfProcess("B", [], [output_port("out", "b.txt")], command("touch", "b.txt"))
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
def test_rejects_mixed_channel_semantics_for_one_parameter() -> None:
    value_process = NfProcess("VALUE", [NfPort("shared", "val")], [], command("true"))
    path_process = NfProcess("PATH", [NfPort("shared", "path")], [], command("true"))
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [value_process, path_process],
            [
                NfWorkflowInputConnection("shared", "VALUE", "shared"),
                NfWorkflowInputConnection("shared", "PATH", "shared"),
            ],
            {"shared": "input.txt"}, {"shared": "string"},
        )


@pytest.mark.fast
def test_rejects_mixed_path_kinds_for_one_parameter() -> None:
    file_process = NfProcess("READ_FILE", [NfPort("shared", "path")], [], command("true"))
    directory_process = NfProcess(
        "READ_DIRECTORY",
        [NfPort("shared", "path", path_kind="directory")],
        [],
        command("true"),
    )
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [file_process, directory_process],
            [
                NfWorkflowInputConnection("shared", "READ_FILE", "shared"),
                NfWorkflowInputConnection("shared", "READ_DIRECTORY", "shared"),
            ],
            {"shared": "input"}, {"shared": "File"},
        )


@pytest.mark.fast
def test_rejects_mixed_container_policy() -> None:
    containerized = NfProcess("CONTAINERIZED", [], [], command("true"), container="ubuntu:24.04")
    host = NfProcess("HOST", [], [], command("true"))
    with pytest.raises(ValueError, match="mixed container execution is not supported"):
        ExecutableNextflowWorkflow("WF", [containerized, host], [], {})


@pytest.mark.serial
def test_rejects_cyclic_or_multiply_connected_dags() -> None:
    a = NfProcess(
        "A",
        [NfPort("value", "path")],
        [output_port("out", "a.txt")],
        command("touch", "a.txt"),
    )
    b = NfProcess(
        "B",
        [NfPort("value", "path")],
        [output_port("out", "b.txt")],
        command("touch", "b.txt"),
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
            {"first": 1, "second": 2}, {"first": "File", "second": "File"},
        )


@pytest.mark.serial
def test_rejects_invalid_private_ir_before_writing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path qualifier and typed glob"):
        NfProcess("BAD_OUTPUT", [], [NfPort("result", "val", "result")], command("true"))
    assert list(tmp_path.iterdir()) == []


def _adapted_workflow(
    adapter: str | None = "dotproduct",
    port: NfPort | None = None,
) -> ExecutableNextflowWorkflow:
    # The param has to match what the edge consumes: a scattered edge
    # takes the whole array, while an unadapted edge into a scalar port
    # takes one value.
    destination = port or NfPort("item", "val")
    array_valued = adapter is not None or destination.is_array
    return ExecutableNextflowWorkflow(
        "wf",
        [NfProcess("SCATTER", [destination], [], command("true"))],
        [NfWorkflowInputConnection("items", "SCATTER", "item", adapter)],
        {"items": ["a", "b"] if array_valued else "a"},
        {"items": "string[]" if array_valued else "string"},
    )


@pytest.mark.fast
@pytest.mark.parametrize("adapter", ["gather", "scatter", "", "collect"])
def test_rejects_an_adapter_outside_the_approved_set(adapter: str) -> None:
    approved = "dotproduct, flat_crossproduct, nested_crossproduct"
    with pytest.raises(ValueError, match=f"channel adapter must be one of {approved}"):
        _adapted_workflow(adapter)


@pytest.mark.fast
def test_rejects_an_adapter_targeting_an_array_marked_port() -> None:
    with pytest.raises(ValueError, match="cannot target the array-marked port SCATTER.item"):
        _adapted_workflow(port=NfPort("item", "val", is_array=True))


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_rejects_a_gathered_output_rescattered_into_an_array_marked_port(method: str) -> None:
    """One element per invocation cannot fill an array-marked port, which would be bound to a bare Path."""
    pair = NfProcess(
        "PAIR", [NfPort("a", "val"), NfPort("b", "val")], [output_port("f", "out.txt")], command("true")
    )
    following = NfProcess(
        "NEXT",
        [NfPort("x", "path", is_array=True), NfPort("y", "val")],
        [output_port("g", "g.txt")],
        command("true"),
    )
    with pytest.raises(ValueError, match=f"channel adapter '{method}' cannot target the array-marked port NEXT.x"):
        ExecutableNextflowWorkflow(
            "wf",
            [pair, following],
            [
                NfWorkflowInputConnection("avals", "PAIR", "a", "dotproduct"),
                NfWorkflowInputConnection("bvals", "PAIR", "b", "dotproduct"),
                NfProcessConnection("PAIR", "f", "NEXT", "x", method),
                NfWorkflowInputConnection("ys", "NEXT", "y", method),
                NfWorkflowOutputConnection("NEXT", "g", "result"),
            ],
            {"avals": ["x"], "bvals": ["y"], "ys": ["z"]}, {"avals": "string[]", "bvals": "string[]", "ys": "string[]"},
        )


@pytest.mark.fast
def test_rejects_one_parameter_scattered_at_one_sink_and_taken_as_a_scalar_at_another() -> None:
    """A scattered sink takes the whole array, so the parameter carries a list there and a scalar here."""
    scattered = NfProcess("SCATTER", [NfPort("item", "val")], [], command("true"))
    whole = NfProcess("WHOLE", [NfPort("item", "val")], [], command("true"))
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [scattered, whole],
            [
                NfWorkflowInputConnection("items", "SCATTER", "item", "dotproduct"),
                NfWorkflowInputConnection("items", "WHOLE", "item"),
            ],
            {"items": ["a", "b"]}, {"items": "string[]"},
        )


@pytest.mark.fast
def test_one_array_parameter_may_be_scattered_differently_and_taken_whole() -> None:
    """An adapter belongs to its scattered process; every sink here takes the whole array (design §5.2)."""
    pair = NfProcess("PAIR", [NfPort("item", "val"), NfPort("tag", "val")], [], command("true"))
    each = NfProcess("EACH", [NfPort("item", "val")], [], command("true"))
    whole = NfProcess(
        "WHOLE", [NfPort("items", "val", is_array=True)], [],
        NfCommand((NfTemplate((NfLiteral("echo"),)), NfArrayBinding("items"))),
    )
    ExecutableNextflowWorkflow(
        "wf",
        [pair, each, whole],
        [
            NfWorkflowInputConnection("items", "PAIR", "item", "flat_crossproduct"),
            NfWorkflowInputConnection("tags", "PAIR", "tag", "flat_crossproduct"),
            NfWorkflowInputConnection("items", "EACH", "item", "dotproduct"),
            NfWorkflowInputConnection("items", "WHOLE", "items"),
        ],
        {"items": ["a", "b"], "tags": ["t"]}, {"items": "string[]", "tags": "string[]"},
    )


@pytest.mark.fast
def test_accepts_one_parameter_adapted_at_every_sink() -> None:
    first = NfProcess("FIRST", [NfPort("item", "val")], [], command("true"))
    second = NfProcess("SECOND", [NfPort("item", "val")], [], command("true"))

    workflow = ExecutableNextflowWorkflow(
        "wf",
        [first, second],
        [
            NfWorkflowInputConnection("items", "FIRST", "item", "dotproduct"),
            NfWorkflowInputConnection("items", "SECOND", "item", "dotproduct"),
        ],
        {"items": ["a", "b"]}, {"items": "string[]"},
    )

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "FIRST", "item", "dotproduct"),
        NfWorkflowInputConnection("items", "SECOND", "item", "dotproduct"),
    )


@pytest.mark.fast
def test_rejects_a_process_mixing_scatter_methods() -> None:
    process = NfProcess(
        "PAIR", [NfPort("first", "val"), NfPort("second", "val")], [], command("true")
    )
    with pytest.raises(ValueError, match="mixes channel adapters dotproduct, flat_crossproduct"):
        ExecutableNextflowWorkflow(
            "wf",
            [process],
            [
                NfWorkflowInputConnection("firsts", "PAIR", "first", "flat_crossproduct"),
                NfWorkflowInputConnection("seconds", "PAIR", "second", "dotproduct"),
            ],
            {"firsts": ["a"], "seconds": ["b"]}, {"firsts": "string[]", "seconds": "string[]"},
        )


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct"])
@pytest.mark.parametrize("process_connection_first", [False, True])
def test_rejects_an_ungathered_process_connection_out_of_a_multi_input_scattered_process(
    method: str, process_connection_first: bool
) -> None:
    pair = NfProcess(
        "PAIR", [NfPort("a", "val"), NfPort("b", "val")], [output_port("f", "out.txt")], command("true")
    )
    following = NfProcess("NEXT", [NfPort("x", "path")], [output_port("g", "g.txt")], command("true"))
    inputs: list[NfConnection] = [
        NfWorkflowInputConnection("avals", "PAIR", "a", method),
        NfWorkflowInputConnection("bvals", "PAIR", "b", method),
    ]
    edge = NfProcessConnection("PAIR", "f", "NEXT", "x")
    connections = [edge, *inputs] if process_connection_first else [*inputs, edge]
    with pytest.raises(ValueError, match=r"connection PAIR\.f -> NEXT\.x must carry a gather adapter"):
        ExecutableNextflowWorkflow(
            "wf",
            [pair, following],
            [*connections, NfWorkflowOutputConnection("NEXT", "g", "result")],
            {"avals": ["x"], "bvals": ["y"]}, {"avals": "string[]", "bvals": "string[]"},
        )


@pytest.mark.fast
@pytest.mark.parametrize("method", ["dotproduct", "flat_crossproduct"])
@pytest.mark.parametrize("process_connection_first", [False, True])
def test_rejects_gathering_the_skipped_positions_of_a_conditional_multi_input_process(
    method: str, process_connection_first: bool
) -> None:
    pair = NfProcess(
        "PAIR",
        [NfPort("a", "val"), NfPort("b", "val")],
        [output_port("f", "out.txt")],
        command("true"),
        condition=parse("$(inputs.a == 'x')"),
    )
    following = NfProcess("NEXT", [NfPort("x", "path", is_array=True)], [output_port("g", "g.txt")], command("true"))
    inputs: list[NfConnection] = [
        NfWorkflowInputConnection("avals", "PAIR", "a", method),
        NfWorkflowInputConnection("bvals", "PAIR", "b", method),
    ]
    edge = NfProcessConnection("PAIR", "f", "NEXT", "x", "gather")
    connections = [edge, *inputs] if process_connection_first else [*inputs, edge]
    with pytest.raises(ValueError, match=r"connection PAIR\.f -> NEXT\.x gathers the skipped positions"):
        ExecutableNextflowWorkflow(
            "wf",
            [pair, following],
            [*connections, NfWorkflowOutputConnection("NEXT", "g", "result")],
            {"avals": ["x"], "bvals": ["y"]}, {"avals": "string[]", "bvals": "string[]"},
        )


@pytest.mark.fast
def test_the_port_capture_set_is_closed() -> None:
    """A capture marker is closed data, so it can never hold expression text."""
    assert frozenset(NfPort.CAPTURE_QUALIFIERS) == frozenset({"single", "text"})

    for rejected in ("$(self[0])", "$(self[0].contents)", "single_path", "contents", ""):
        with pytest.raises(ValueError, match="port capture must be one of"):
            NfPort("result", "path", "result", NfTemplate((NfLiteral("out.txt"),)), capture=rejected)


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["*.txt", "out?.txt", "out[0].txt", "*", "sub/*.txt"])
def test_a_wildcard_glob_cannot_carry_a_capture_marker(glob: str) -> None:
    """Projecting the first match is provable only when one match exists."""
    with pytest.raises(ValueError, match="no wildcard character"):
        NfPort("result", "path", "result", NfTemplate((NfLiteral(glob),)), capture="single")


@pytest.mark.fast
def test_a_reference_bearing_glob_cannot_carry_a_capture_marker() -> None:
    """A reference's runtime value cannot be shown wildcard-free at compile time."""
    for glob in (
        NfTemplate((NfInputReference("name"),)),
        NfTemplate((NfLiteral("out-"), NfInputReference("name"))),
        NfTemplate((NfBasenameReference("source"), NfLiteral(".copy"))),
    ):
        with pytest.raises(ValueError, match="single-literal glob with no input reference"):
            NfPort("result", "path", "result", glob, capture="single")


@pytest.mark.fast
def test_a_capture_marker_requires_a_glob_and_its_own_qualifier() -> None:
    with pytest.raises(ValueError, match="single-literal glob with no input reference"):
        NfPort("result", "path", "result", None, capture="single")
    with pytest.raises(ValueError, match="capture 'single' requires a path port"):
        NfPort("result", "val", "result", NfTemplate((NfLiteral("out.txt"),)), capture="single")


@pytest.mark.fast
def test_a_capture_marker_cannot_combine_with_an_array_or_stage_as_marker() -> None:
    glob = NfTemplate((NfLiteral("out.txt"),))
    with pytest.raises(ValueError, match="cannot combine with an array marker"):
        NfPort("result", "path", "result", glob, is_array=True, capture="single")
    with pytest.raises(ValueError, match="cannot combine with an array marker"):
        NfPort("result", "path", "result", glob, stage_as="staged.txt", capture="single")


def _text_capture_process(name: str = "READ") -> NfProcess:
    glob = NfTemplate((NfLiteral("out.txt"),))
    return NfProcess(
        name,
        [NfPort("message", "val")],
        [NfPort("text", "val", "text", glob, capture="text")],
        NfCommand((NfTemplate((NfLiteral("printf"),)),), stdout=glob),
    )


@pytest.mark.fast
def test_a_text_capture_output_is_the_one_val_qualified_output() -> None:
    process = _text_capture_process()

    assert process.outputs[0].qualifier == "val"
    assert NfProcess.from_dict(process.to_dict()) == process

    glob = NfTemplate((NfLiteral("out.txt"),))
    with pytest.raises(ValueError, match="require path qualifier and typed glob"):
        NfProcess("P", [], [NfPort("text", "val", "text", glob)], command("true"))
    with pytest.raises(ValueError, match="capture 'text' requires a val port"):
        NfPort("text", "path", "text", glob, capture="text")


@pytest.mark.fast
def test_a_captured_value_cannot_feed_a_process_input() -> None:
    """The graph has no qualifier agreement check for process edges to lean on."""
    producer = _text_capture_process("PRODUCE")
    consumer = NfProcess(
        "CONSUME",
        [NfPort("text", "val")],
        [output_port("result", "copy.txt")],
        command("printf", "%s", NfTemplate((NfInputReference("text"),)), stdout="copy.txt"),
    )

    with pytest.raises(ValueError, match=r"PRODUCE.text captures file text"):
        ExecutableNextflowWorkflow(
            "wf",
            [producer, consumer],
            [
                NfWorkflowInputConnection("message", "PRODUCE", "message"),
                NfProcessConnection("PRODUCE", "text", "CONSUME", "text"),
                NfWorkflowOutputConnection("CONSUME", "result", "result"),
            ],
            {"message": "hi"}, {"message": "string"},
        )


@pytest.mark.fast
def test_a_captured_value_reaches_a_workflow_output() -> None:
    producer = _text_capture_process("PRODUCE")

    workflow = ExecutableNextflowWorkflow(
        "wf",
        [producer],
        [
            NfWorkflowInputConnection("message", "PRODUCE", "message"),
            NfWorkflowOutputConnection("PRODUCE", "text", "captured"),
        ],
        {"message": "hi"}, {"message": "string"},
    )

    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow


def _conditional_payload() -> dict[str, Any]:
    process = NfProcess(
        "TASK",
        [NfPort("a", "val")],
        [output_port("result", "out.txt")],
        command("true"),
        condition=parse("$(inputs.a > 1)"),
    )
    workflow = ExecutableNextflowWorkflow(
        "WF", [process], [NfWorkflowInputConnection("a", "TASK", "a")], {"a": 2}, {"a": "int"}
    )
    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow
    return cast(dict[str, Any], json.loads(workflow.to_json()))


@pytest.mark.fast
@pytest.mark.parametrize(("field", "value"), [
    ("condition", {}),
    ("condition", False),
    ("condition", {"op": "string", "value": "false"}),
    ("condition", {"op": "bogus"}),
    ("condition", {"op": ">", "args": [{"op": "ref", "value": "a"}]}),
    ("is_array", "false"),
    ("is_array", 0),
], ids=["empty-condition", "false-condition", "string-condition", "unknown-op", "wrong-arity", "string-is-array", "zero-is-array"])
def test_hydration_refuses_a_malformed_condition_or_port(field: str, value: Any) -> None:
    payload = _conditional_payload()
    target = payload["processes"][0] if field == "condition" else payload["processes"][0]["inputs"][0]
    target[field] = value
    with pytest.raises((ValueError, TypeError, KeyError)):
        ExecutableNextflowWorkflow.from_json(json.dumps(payload))


@pytest.mark.fast
@pytest.mark.parametrize("name", sorted(NEXTFLOW_SCRIPT_NAMES) + ["Math", "Élan"])
def test_no_name_takes_a_name_the_script_resolves(name: str) -> None:
    assert normalize_nextflow_identifier(name) == f"_{name}"
    assert normalize_nextflow_identifier(name, variable=False) == f"_{name}"


@pytest.mark.fast
@pytest.mark.parametrize("name", sorted(NEXTFLOW_SCRIPT_DECLARED_NAMES - NEXTFLOW_SCRIPT_NAMES))
def test_only_a_process_or_workflow_avoids_a_name_every_script_declares(name: str) -> None:
    assert normalize_nextflow_identifier(name, variable=False) == f"_{name}"
    assert normalize_nextflow_identifier(name) == name
    with pytest.raises(ValueError, match="already declared by every Nextflow script"):
        NfProcess(name, [], [output_port("o", "o.txt")], command("true"))
    with pytest.raises(ValueError, match="already declared by every Nextflow script"):
        ExecutableNextflowWorkflow(name, [], [], {})


@pytest.mark.fast
def test_the_workflow_name_is_never_a_process_name() -> None:
    process = NfProcess("echo", [], [output_port("o", "o.txt")], command("true"))
    with pytest.raises(ValueError, match="workflow name 'echo' is also a process name"):
        ExecutableNextflowWorkflow("echo", [process], [], {})


def _optional_producer() -> NfProcess:
    return NfProcess("MAYBE", [NfPort("x", "val")], [NfPort("o", "path", "o", template("maybe.txt"), optional=True)],
                     command("true"))


@pytest.mark.fast
def test_an_optional_output_round_trips_and_reads_back() -> None:
    workflow = ExecutableNextflowWorkflow(
        "WF",
        [_optional_producer()],
        [NfWorkflowInputConnection("x", "MAYBE", "x"), NfWorkflowOutputConnection("MAYBE", "o", "o")],
        {"x": 1}, {"x": "int"},
    )
    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow
    assert parse_nf_text(render_nextflow(workflow), params={"x": 1}).processes[0].outputs[0].optional


@pytest.mark.fast
@pytest.mark.parametrize(("glob", "capture"), [("*.log", None), ("maybe.txt", "single")], ids=["pattern", "single"])
def test_an_optional_output_never_requires_exactly_one_file(glob: str, capture: str | None) -> None:
    # Nextflow fails arity '1' when nothing matches, optional or not.
    with pytest.raises(ValueError, match="optional output cannot require exactly one file"):
        NfPort("o", "path", "o", template(glob), capture=capture, optional=True)


@pytest.mark.fast
@pytest.mark.parametrize("adapter", [None, "dotproduct"], ids=["consumed", "scattered"])
def test_an_optional_output_reaches_only_an_unscattered_workflow_output(adapter: str | None) -> None:
    consume = NfProcess("CONSUME", [NfPort("source", "path")], [], command("true"))
    connections: list[Any] = [NfWorkflowInputConnection("xs" if adapter else "x", "MAYBE", "x", adapter)]
    connections.append(NfProcessConnection("MAYBE", "o", "CONSUME", "source") if adapter is None
                       else NfWorkflowOutputConnection("MAYBE", "o", "o"))
    with pytest.raises(ValueError, match="optional output"):
        ExecutableNextflowWorkflow("WF", [_optional_producer(), consume], connections,
                                   {"xs": [1]} if adapter else {"x": 1}, {"xs": "int[]"} if adapter else {"x": "int"})
