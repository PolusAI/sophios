"""Executable IR invariants: identifiers, immutability, hydration, and graph rules."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, cast

import pytest

from sophios.nf_symbols import is_nextflow_identifier, normalize_nextflow_identifier
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfFlag,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .testkit import command, output_port


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
        params={"message": "hello"},
    )
    assert ExecutableNextflowWorkflow.from_dict(workflow.to_dict()) == workflow


@pytest.mark.fast
def test_json_roundtrip_is_deterministic() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {"b": 2, "a": 1})
    serialized = workflow.to_json()
    assert serialized == workflow.to_json()
    assert ExecutableNextflowWorkflow.from_json(serialized) == workflow


@pytest.mark.fast
def test_executable_schema_declares_version_and_kind() -> None:
    workflow = ExecutableNextflowWorkflow("wf", [], [], {})
    payload = workflow.to_dict()

    assert payload["schema_version"] == 4
    assert payload["representation_kind"] == "executable"

    payload["schema_version"] = 1
    with pytest.raises(ValueError, match="schema version"):
        ExecutableNextflowWorkflow.from_dict(payload)

    payload["schema_version"] = 4
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
def test_flag_token_survives_hydration() -> None:
    process = NfProcess(
        "SORT",
        [NfPort("reverse", "val")],
        [output_port("result", "sorted.txt")],
        NfCommand((NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r"))),
    )
    assert NfProcess.from_dict(process.to_dict()) == process
    assert process.command.tokens[1].to_dict() == {
        "kind": "flag",
        "name": "reverse",
        "prefix": "-r",
    }


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


@pytest.mark.fast
def test_flag_must_reference_a_declared_value_input() -> None:
    flag_command = NfCommand((NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r")))
    with pytest.raises(ValueError, match="unknown inputs"):
        NfProcess("SORT", [], [], flag_command)
    with pytest.raises(ValueError, match="flag.*val"):
        NfProcess("SORT", [NfPort("reverse", "path")], [], flag_command)


@pytest.mark.fast
def test_flags_are_unrepresentable_outside_command_position() -> None:
    with pytest.raises(TypeError, match="typed literal or input references"):
        NfTemplate((cast(Any, NfFlag("reverse", "-r")),))


@pytest.mark.fast
def test_basename_segment_survives_hydration() -> None:
    process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("source"),)))],
        NfCommand((NfTemplate((NfLiteral("cp"),)), NfTemplate((NfBasenameReference("source"),)))),
    )
    assert NfProcess.from_dict(process.to_dict()) == process
    assert NfBasenameReference("source").to_dict() == {
        "kind": "basename",
        "name": "source",
    }


@pytest.mark.fast
def test_basename_segment_must_reference_a_path_input() -> None:
    command = NfCommand((NfTemplate((NfLiteral("cp"),)), NfTemplate((NfBasenameReference("source"),))))
    with pytest.raises(ValueError, match="unknown inputs"):
        NfProcess("COPY", [], [], command)
    with pytest.raises(ValueError, match="basename.*path"):
        NfProcess("COPY", [NfPort("source", "val")], [], command)


@pytest.mark.fast
def test_basename_port_rule_covers_stream_and_glob_positions() -> None:
    """The design claims validity in every template position, so check them all."""
    derived = NfTemplate((NfBasenameReference("source"),))
    with pytest.raises(ValueError, match="basename.*path"):
        NfProcess(
            "COPY",
            [NfPort("source", "val")],
            [],
            NfCommand((NfTemplate((NfLiteral("cp"),)),), stdout=derived),
        )
    with pytest.raises(ValueError, match="basename.*path"):
        NfProcess(
            "COPY",
            [NfPort("source", "val")],
            [NfPort("result", "path", "result", derived)],
            NfCommand((NfTemplate((NfLiteral("cp"),)),)),
        )


@pytest.mark.fast
def test_hydration_accepts_earlier_subset_schema_versions() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf", [NfProcess("P", [], [], command("true"))], [], {}
    ).to_dict()
    assert payload["schema_version"] == 4

    for earlier in (2, 3):
        payload["schema_version"] = earlier
        assert ExecutableNextflowWorkflow.from_dict(payload).to_dict()["schema_version"] == 4

    for unsupported in (1, 5):
        payload["schema_version"] = unsupported
        with pytest.raises(ValueError, match="schema version"):
            ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_hydration_rejects_a_kind_newer_than_its_declared_version() -> None:
    """A version is a claim about the value space, so it has to be enforced."""
    payload = ExecutableNextflowWorkflow(
        "wf",
        [NfProcess(
            "SORT",
            [NfPort("reverse", "val")],
            [],
            NfCommand((NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r"))),
        )],
        [NfWorkflowInputConnection("reverse", "SORT", "reverse")],
        {"reverse": True},
    ).to_dict()

    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="flag.*schema version 2"):
        ExecutableNextflowWorkflow.from_dict(payload)


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
            {"message": "hi"},
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
            {"shared": "input.txt"},
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
            {"shared": "input"},
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
            {"first": 1, "second": 2},
        )


@pytest.mark.serial
def test_rejects_invalid_private_ir_before_writing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path qualifier and typed glob"):
        NfProcess("BAD_OUTPUT", [], [NfPort("result", "val", "result")], command("true"))
    assert list(tmp_path.iterdir()) == []
