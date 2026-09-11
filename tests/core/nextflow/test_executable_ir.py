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
    NfShellLiteral,
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

    assert payload["schema_version"] == 9
    assert payload["representation_kind"] == "executable"

    payload["schema_version"] = 1
    with pytest.raises(ValueError, match="schema version"):
        ExecutableNextflowWorkflow.from_dict(payload)

    payload["schema_version"] = 9
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
    message = "array-marked inputs may only be referenced by array bindings"
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
    message = "array-marked inputs may only be referenced by array bindings"
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
def test_array_marked_input_rejects_a_flag_reference_during_hydration() -> None:
    payload = NfProcess(
        "FLAG",
        [NfPort("verbose", "val")],
        [],
        NfCommand((NfTemplate((NfLiteral("echo"),)), NfFlag("verbose", "--verbose"))),
    ).to_dict()
    payload["inputs"][0]["is_array"] = True

    with pytest.raises(
        ValueError,
        match="array-marked inputs may only be referenced by array bindings: verbose",
    ):
        NfProcess.from_dict(payload)


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
def test_shell_literal_token_survives_hydration() -> None:
    process = NfProcess(
        "REDIRECT",
        [],
        [],
        NfCommand((NfTemplate((NfLiteral("true"),)), NfShellLiteral(">>"))),
    )
    assert NfProcess.from_dict(process.to_dict()) == process
    assert process.command.tokens[1].to_dict() == {"kind": "shell_literal", "text": ">>"}


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


@pytest.mark.fast
def test_hydration_rejects_a_shell_literal_kind_older_than_schema_version_6() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf",
        [NfProcess(
            "REDIRECT",
            [],
            [],
            NfCommand((NfTemplate((NfLiteral("true"),)), NfShellLiteral(">>"))),
        )],
        [],
        {},
    ).to_dict()

    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 5
    with pytest.raises(ValueError, match=r"'shell_literal'.*schema version 6.*schema version 5"):
        ExecutableNextflowWorkflow.from_dict(payload)


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
@pytest.mark.parametrize("stage_as", ["", "   ", "sub/dir.txt"])
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
def test_hydration_rejects_a_stage_as_field_older_than_schema_version_7() -> None:
    payload = ExecutableNextflowWorkflow(
        "wf",
        [NfProcess(
            "STAGE",
            [NfPort("source", "path", stage_as="renamed.txt")],
            [],
            NfCommand((NfTemplate((NfLiteral("cat"),)), NfTemplate((NfLiteral("renamed.txt"),)))),
        )],
        [NfWorkflowInputConnection("source", "STAGE", "source")],
        {"source": "in.txt"},
    ).to_dict()

    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 6
    with pytest.raises(ValueError, match=r"'stage_as'.*schema version 7.*schema version 6"):
        ExecutableNextflowWorkflow.from_dict(payload)


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
    assert payload["schema_version"] == 9

    for earlier in (2, 3, 4, 5, 6, 7, 8):
        payload["schema_version"] = earlier
        assert ExecutableNextflowWorkflow.from_dict(payload).to_dict()["schema_version"] == 9

    for unsupported in (1, 10):
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


def _adapted_workflow(
    adapter: str | None = "scatter",
    port: NfPort | None = None,
) -> ExecutableNextflowWorkflow:
    # The param has to match what the edge consumes: the scatter adapter
    # takes the whole array, while an unadapted edge into a scalar port
    # takes one value.
    destination = port or NfPort("item", "val")
    array_valued = adapter == "scatter" or destination.is_array
    return ExecutableNextflowWorkflow(
        "wf",
        [NfProcess("SCATTER", [destination], [], command("true"))],
        [NfWorkflowInputConnection("items", "SCATTER", "item", adapter)],
        {"items": ["a", "b"] if array_valued else "a"},
    )


@pytest.mark.fast
def test_channel_adapter_survives_hydration() -> None:
    payload = _adapted_workflow().to_dict()
    assert ExecutableNextflowWorkflow.from_dict(payload) == _adapted_workflow()
    assert payload["connections"][0]["adapter"] == "scatter"


@pytest.mark.fast
def test_hydration_defaults_a_missing_adapter_to_none() -> None:
    """Every version before 8 never wrote the key, so its absence is unadapted."""
    payload = _adapted_workflow(adapter=None).to_dict()
    del payload["connections"][0]["adapter"]
    payload["schema_version"] = 7

    hydrated = ExecutableNextflowWorkflow.from_dict(payload)

    assert hydrated.connections == (
        NfWorkflowInputConnection("items", "SCATTER", "item"),
    )


@pytest.mark.fast
@pytest.mark.parametrize("adapter", ["gather", "flatten", "", "collect"])
def test_rejects_an_adapter_outside_the_approved_set(adapter: str) -> None:
    with pytest.raises(ValueError, match="channel adapter must be one of scatter"):
        _adapted_workflow(adapter)


@pytest.mark.fast
def test_rejects_an_adapter_targeting_an_array_marked_port() -> None:
    with pytest.raises(ValueError, match="cannot target the array-marked port SCATTER.item"):
        _adapted_workflow(port=NfPort("item", "val", is_array=True))


@pytest.mark.fast
def test_rejects_one_parameter_adapted_at_only_some_sinks() -> None:
    """The adapter is part of the channel contract, like path_kind and is_array."""
    scattered = NfProcess("SCATTER", [NfPort("item", "val")], [], command("true"))
    whole = NfProcess("WHOLE", [NfPort("item", "val")], [], command("true"))
    with pytest.raises(ValueError, match="incompatible channel qualifiers"):
        ExecutableNextflowWorkflow(
            "wf",
            [scattered, whole],
            [
                NfWorkflowInputConnection("items", "SCATTER", "item", "scatter"),
                NfWorkflowInputConnection("items", "WHOLE", "item"),
            ],
            {"items": ["a", "b"]},
        )


@pytest.mark.fast
def test_accepts_one_parameter_adapted_at_every_sink() -> None:
    first = NfProcess("FIRST", [NfPort("item", "val")], [], command("true"))
    second = NfProcess("SECOND", [NfPort("item", "val")], [], command("true"))

    workflow = ExecutableNextflowWorkflow(
        "wf",
        [first, second],
        [
            NfWorkflowInputConnection("items", "FIRST", "item", "scatter"),
            NfWorkflowInputConnection("items", "SECOND", "item", "scatter"),
        ],
        {"items": ["a", "b"]},
    )

    assert workflow.connections == (
        NfWorkflowInputConnection("items", "FIRST", "item", "scatter"),
        NfWorkflowInputConnection("items", "SECOND", "item", "scatter"),
    )


@pytest.mark.fast
def test_hydration_rejects_an_adapter_field_older_than_schema_version_8() -> None:
    payload = _adapted_workflow().to_dict()

    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 7
    with pytest.raises(ValueError, match=r"'adapter'.*schema version 8.*schema version 7"):
        ExecutableNextflowWorkflow.from_dict(payload)


def _captured_workflow(capture: str = "single") -> ExecutableNextflowWorkflow:
    glob = NfTemplate((NfLiteral("out.txt"),))
    process = NfProcess(
        "MAKE",
        [NfPort("message", "val")],
        [NfPort("result", "path", "result", glob, capture=capture)],
        NfCommand((NfTemplate((NfLiteral("printf"),)),), stdout=glob),
    )
    return ExecutableNextflowWorkflow(
        "wf",
        [process],
        [
            NfWorkflowInputConnection("message", "MAKE", "message"),
            NfWorkflowOutputConnection("MAKE", "result", "result"),
        ],
        {"message": "hi"},
    )


@pytest.mark.fast
def test_a_capture_marker_survives_serialization_unchanged() -> None:
    workflow = _captured_workflow()

    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow
    assert workflow.processes[0].outputs[0].to_dict()["capture"] == "single"


@pytest.mark.fast
def test_the_port_capture_set_is_closed() -> None:
    """A capture marker is closed data, so it can never hold expression text."""
    assert NfPort.ALLOWED_CAPTURES == frozenset({"single", "text"})
    assert set(NfPort.CAPTURE_QUALIFIERS) == set(NfPort.ALLOWED_CAPTURES)

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


@pytest.mark.fast
def test_hydration_rejects_a_capture_field_older_than_schema_version_9() -> None:
    payload = _captured_workflow().to_dict()

    assert ExecutableNextflowWorkflow.from_dict(payload).to_dict() == payload

    payload["schema_version"] = 8
    with pytest.raises(ValueError, match=r"'capture'.*schema version 9.*schema version 8"):
        ExecutableNextflowWorkflow.from_dict(payload)


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
            {"message": "hi"},
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
        {"message": "hi"},
    )

    assert ExecutableNextflowWorkflow.from_json(workflow.to_json()) == workflow
