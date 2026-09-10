"""Executable IR invariants: identifiers, immutability, hydration, and graph rules."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, cast

import pytest

from sophios.nf_symbols import is_nextflow_identifier, normalize_nextflow_identifier
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfFlag,
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

    assert payload["schema_version"] == 5
    assert payload["representation_kind"] == "executable"

    payload["schema_version"] = 1
    with pytest.raises(ValueError, match="schema version"):
        ExecutableNextflowWorkflow.from_dict(payload)

    payload["schema_version"] = 5
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
    # Every member of the command-token union is nameable through the public
    # module, or a caller cannot construct or match what it gets back.
    assert nextflow.NfArrayBinding is NfArrayBinding
    assert "NfArrayBinding" in nextflow.__all__


@pytest.mark.fast
def test_flag_must_reference_a_declared_value_input() -> None:
    flag_command = NfCommand((NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r")))
    with pytest.raises(ValueError, match="unknown inputs"):
        NfProcess("SORT", [], [], flag_command)
    with pytest.raises(ValueError, match="flag.*val"):
        NfProcess("SORT", [NfPort("reverse", "path")], [], flag_command)


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
def test_basename_segment_accepts_both_path_kinds() -> None:
    # The design claims File and Directory inputs alike; a directory port is
    # staged under its own name too, so both path kinds carry a basename.
    command = NfCommand((NfTemplate((NfLiteral("cp"),)), NfTemplate((NfBasenameReference("source"),))))
    for path_kind in ("file", "directory"):
        process = NfProcess(
            "COPY", [NfPort("source", "path", path_kind=path_kind)], [], command
        )
        assert process.inputs[0].path_kind == path_kind


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
def test_array_binding_survives_hydration() -> None:
    process = NfProcess(
        "NAMES",
        [NfPort("names", "val", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names", "--name"))),
    )
    assert NfProcess.from_dict(process.to_dict()) == process
    assert process.command.tokens[1].to_dict() == {
        "kind": "array",
        "name": "names",
        "prefix": "--name",
    }
    assert process.inputs[0].to_dict()["is_array"] is True


@pytest.mark.fast
def test_array_binding_prefix_may_be_none() -> None:
    binding = NfArrayBinding("names")
    assert binding.prefix is None
    assert binding.to_dict() == {"kind": "array", "name": "names", "prefix": None}


@pytest.mark.fast
@pytest.mark.parametrize("prefix", ["", "   "])
def test_array_binding_rejects_a_blank_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        NfArrayBinding("names", prefix)


@pytest.mark.fast
def test_array_binding_must_reference_an_array_marked_input() -> None:
    array_command = NfCommand(
        (NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names", "--name"))
    )
    with pytest.raises(ValueError, match="unknown inputs"):
        NfProcess("NAMES", [], [], array_command)
    with pytest.raises(ValueError, match="array bindings must reference array-marked inputs"):
        NfProcess("NAMES", [NfPort("names", "val")], [], array_command)


@pytest.mark.fast
def test_array_marked_input_rejects_a_plain_reference() -> None:
    # The converse direction: a plain reference renders values.toString(), so
    # a two-item array would reach the command line as "[a, b]" rather than
    # expanding per item.
    message = "array-marked inputs require an array binding, not a plain or basename reference"
    for template in (
        NfTemplate((NfLiteral("--joined="), NfInputReference("values"))),
        NfTemplate((NfInputReference("values"),)),
    ):
        with pytest.raises(ValueError, match=message):
            NfProcess(
                "JOIN",
                [NfPort("values", "val", is_array=True)],
                [],
                NfCommand((NfTemplate((NfLiteral("echo"),)), template)),
            )
    # Reachable through a stream target and an output glob too, since both
    # take the same reference type.
    with pytest.raises(ValueError, match=message):
        NfProcess(
            "JOIN",
            [NfPort("values", "val", is_array=True)],
            [],
            NfCommand(
                (NfTemplate((NfLiteral("echo"),)),),
                stdout=NfTemplate((NfInputReference("values"),)),
            ),
        )
    with pytest.raises(ValueError, match=message):
        NfProcess(
            "JOIN",
            [NfPort("values", "val", is_array=True)],
            [NfPort("out", "path", "out", NfTemplate((NfInputReference("values"),)))],
            NfCommand((NfTemplate((NfLiteral("echo"),)),)),
        )


@pytest.mark.fast
def test_array_marked_input_rejects_a_basename_reference() -> None:
    # Same route as a plain reference: values.name.toString() is a GPath
    # spread over the list, so a File[] read as a basename renders
    # --tag=[a.txt, b.txt]. In glob position it also breaks the premise the
    # literal-name rule rests on, since the rendered name is a list.
    message = "array-marked inputs require an array binding, not a plain or basename reference"
    with pytest.raises(ValueError, match=message):
        NfProcess(
            "TAG",
            [NfPort("values", "path", is_array=True)],
            [],
            NfCommand((
                NfTemplate((NfLiteral("echo"),)),
                NfTemplate((NfLiteral("--tag="), NfBasenameReference("values"))),
            )),
        )
    with pytest.raises(ValueError, match=message):
        NfProcess(
            "TAG",
            [NfPort("values", "path", is_array=True)],
            [NfPort(
                "out", "path", "out",
                NfTemplate((NfBasenameReference("values"), NfLiteral(".done"))),
            )],
            NfCommand((NfTemplate((NfLiteral("echo"),)),)),
        )


@pytest.mark.fast
def test_workflow_input_value_must_match_destination_cardinality() -> None:
    # The live path: a workflow input is the only edge that can reach an
    # array port today. A scalar param there renders params.values.collect{},
    # which in Groovy iterates a String's characters.
    array_process = NfProcess(
        "JOIN",
        [NfPort("values", "val", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("echo"),)), NfArrayBinding("values"))),
    )
    with pytest.raises(ValueError, match="delivers a scalar to JOIN.values, which expects an array"):
        ExecutableNextflowWorkflow(
            "WF",
            [array_process],
            [NfWorkflowInputConnection("values", "JOIN", "values")],
            {"values": "abc"},
        )
    scalar_process = NfProcess("ONE", [NfPort("value", "val")], [], command("true"))
    with pytest.raises(ValueError, match="delivers a list to ONE.value, which expects a scalar"):
        ExecutableNextflowWorkflow(
            "WF",
            [scalar_process],
            [NfWorkflowInputConnection("value", "ONE", "value")],
            {"value": ["a", "b"]},
        )
    # An empty sequence stays legal on both sides: it is the absent-optional
    # sentinel on a scalar port and a real empty array on an array port.
    cases: tuple[tuple[NfProcess, dict[str, Any]], ...] = (
        (array_process, {"values": []}),
        (scalar_process, {"value": []}),
    )
    for process, params in cases:
        port = process.inputs[0].name
        ExecutableNextflowWorkflow(
            "WF",
            [process],
            [NfWorkflowInputConnection(port, process.name, port)],
            params,
        )


@pytest.mark.fast
def test_process_connection_rejects_mismatched_cardinality() -> None:
    # A scalar output driving an array-marked port compiled, then failed
    # inside Nextflow with Path.isEmpty() rather than diagnosing here.
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
            {"shared": ["a", "b"]},
        )


@pytest.mark.fast
def test_hydration_rejects_an_array_kind_or_field_older_than_schema_version_5() -> None:
    process = NfProcess(
        "NAMES",
        [NfPort("names", "val", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names", "--name"))),
    )
    payload = ExecutableNextflowWorkflow(
        "wf",
        [process],
        [NfWorkflowInputConnection("names", "NAMES", "names")],
        {"names": ["a", "b"]},
    ).to_dict()
    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 4
    with pytest.raises(ValueError, match=r"'is_array'.*schema version 5.*schema version 4"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_hydration_rejects_an_array_kind_token_older_than_schema_version_5() -> None:
    """A hand-crafted payload can carry the array kind without is_array; the kind gate still fires."""
    process = NfProcess(
        "NAMES",
        [NfPort("names", "val")],
        [],
        command("echo"),
    )
    payload = ExecutableNextflowWorkflow(
        "wf",
        [process],
        [NfWorkflowInputConnection("names", "NAMES", "names")],
        {"names": "unused"},
    ).to_dict()
    payload["processes"][0]["command"]["tokens"].append(
        {"kind": "array", "name": "names", "prefix": None}
    )
    payload["schema_version"] = 4
    with pytest.raises(ValueError, match=r"'array'.*schema version 5.*schema version 4"):
        ExecutableNextflowWorkflow.from_dict(payload)


@pytest.mark.fast
def test_hydration_accepts_earlier_subset_schema_versions() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf", [NfProcess("P", [], [], command("true"))], [], {}
    ).to_dict()
    assert payload["schema_version"] == 5

    for earlier in (2, 3, 4):
        payload["schema_version"] = earlier
        assert ExecutableNextflowWorkflow.from_dict(payload).to_dict()["schema_version"] == 5

    for unsupported in (1, 6):
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
