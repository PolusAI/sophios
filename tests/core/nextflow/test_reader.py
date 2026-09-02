"""Loss-aware structural reader, provenance-bound promotion, and CWL import."""

# pylint: disable=missing-function-docstring

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, cast

import pytest

from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow
from sophios.input_output_nf import (
    render_nextflow,
    render_nextflow_params,
    write_nextflow_artifacts,
)
from sophios.nf_reader import (
    NextflowDocument,
    NextflowPort,
    import_nextflow,
    nextflow_to_cwl,
    parse_nf_file,
    parse_nf_text,
    promote_nextflow_document,
    render_nextflow_document,
)
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfBasenameReference,
    NfCommand,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .testkit import command, flag_workflow, output_port, ref, runtime_workflow, template


@pytest.mark.fast
def test_generated_source_roundtrips_through_reader(tmp_path: Path) -> None:
    expected = runtime_workflow()
    write_nextflow_artifacts(expected, tmp_path)
    parsed = parse_nf_file(tmp_path / "workflow.nf")
    assert isinstance(parsed, NextflowDocument)
    assert parsed.name == expected.name
    assert [process.name for process in parsed.processes] == [
        process.name for process in expected.processes
    ]
    assert parsed.connections == expected.connections
    assert dict(parsed.params) == dict(expected.params)
    assert parsed.opaque_regions == ()
    assert parsed.verified_executable == expected
    assert promote_nextflow_document(parsed) == expected
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        render_nextflow(cast(Any, parsed))


@pytest.mark.fast
def test_flag_artifacts_parse_and_promote(tmp_path: Path) -> None:
    """Promotion is byte-identity bound, so a new token shape must round-trip."""
    expected = flag_workflow()
    write_nextflow_artifacts(expected, tmp_path)

    parsed = parse_nf_file(tmp_path / "workflow.nf")

    assert parsed.opaque_regions == ()
    assert promote_nextflow_document(parsed) == expected


@pytest.mark.fast
def test_basename_artifacts_parse_and_promote(tmp_path: Path) -> None:
    """A basename glob renders as a GString, so the reader must accept it back."""
    derived = NfTemplate((NfBasenameReference("source"), NfLiteral(".copy")))
    expected = ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess(
            "COPY",
            [NfPort("source", "path")],
            [NfPort("result", "path", "result", derived)],
            NfCommand(
                (NfTemplate((NfLiteral("cp"),)), NfTemplate((NfInputReference("source"),)), derived),
                stdout=NfTemplate((NfBasenameReference("source"), NfLiteral(".log"))),
            ),
        )],
        [
            NfWorkflowInputConnection("source", "COPY", "source"),
            NfWorkflowOutputConnection("COPY", "result", "copied"),
        ],
        {"source": "lines.txt"},
    )
    write_nextflow_artifacts(expected, tmp_path)

    parsed = parse_nf_file(tmp_path / "workflow.nf")

    assert parsed.opaque_regions == ()
    assert promote_nextflow_document(parsed) == expected


@pytest.mark.fast
def test_prior_schema_version_artifacts_still_promote(tmp_path: Path) -> None:
    """A v2-era artifact pair keeps promoting; renderer output is the contract."""
    workflow = runtime_workflow()
    payload = workflow.to_dict()
    payload["schema_version"] = 2
    (tmp_path / "nextflow_workflow.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (tmp_path / "workflow.nf").write_text(render_nextflow(workflow), encoding="utf-8")
    (tmp_path / "nextflow_params.json").write_text(
        render_nextflow_params(workflow), encoding="utf-8"
    )

    parsed = parse_nf_file(tmp_path / "workflow.nf")

    assert promote_nextflow_document(parsed) == workflow


@pytest.mark.fast
def test_source_without_verified_ir_cannot_be_promoted() -> None:
    parsed = parse_nf_text(render_nextflow(runtime_workflow()))
    assert parsed.opaque_regions == ()
    with pytest.raises(ValueError, match="matching validated executable IR artifact"):
        promote_nextflow_document(parsed)


@pytest.mark.fast
def test_promotion_rechecks_provenance_instead_of_trusting_field_name() -> None:
    parsed = parse_nf_text(render_nextflow(runtime_workflow()))
    different = ExecutableNextflowWorkflow("DIFFERENT", (), (), {})
    forged = replace(parsed, verified_executable=different)
    with pytest.raises(ValueError, match="source does not match"):
        promote_nextflow_document(forged)


@pytest.mark.fast
def test_reader_rejects_tampered_source_or_parameters(tmp_path: Path) -> None:
    workflow = runtime_workflow()
    write_nextflow_artifacts(workflow, tmp_path)
    script = tmp_path / "workflow.nf"
    script.write_text(
        script.read_text(encoding="utf-8").replace("copy.txt", "changed.txt", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match its executable IR"):
        parse_nf_file(script)

    write_nextflow_artifacts(workflow, tmp_path)
    params = tmp_path / "nextflow_params.json"
    params.write_text('{"message": "changed"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="parameters do not match"):
        parse_nf_file(script)


@pytest.mark.fast
def test_array_params_preserve_generated_provenance(tmp_path: Path) -> None:
    workflow = ExecutableNextflowWorkflow("PIPELINE", (), (), {"values": [1, 2, 3]})
    write_nextflow_artifacts(workflow, tmp_path)

    parsed = parse_nf_file(tmp_path / "workflow.nf")

    assert parsed.verified_executable == workflow
    assert promote_nextflow_document(parsed) == workflow


@pytest.mark.fast
def test_reader_rejects_invalid_or_stale_ir_artifact(tmp_path: Path) -> None:
    write_nextflow_artifacts(runtime_workflow(), tmp_path)
    ir_path = tmp_path / "nextflow_workflow.json"
    payload = json.loads(ir_path.read_text(encoding="utf-8"))
    payload["representation_kind"] = "structural"
    ir_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="representation kind"):
        parse_nf_file(tmp_path / "workflow.nf")


@pytest.mark.fast
def test_reader_retains_process_and_global_unknown_content() -> None:
    source = """nextflow.enable.dsl=2
include { HELPER } from './helper'
process TASK {
    errorStrategy 'retry'
    output:
    path "result.txt", emit: result
    script:
    \"\"\"
    touch result.txt
    \"\"\"
}
workflow PIPELINE {
    main:
    TASK()
    emit:
    result = TASK.out.result
    onComplete { println 'done' }
}
workflow {
    PIPELINE()
}
"""
    parsed = parse_nf_text(source)
    opaque = "\n".join(parsed.opaque_regions)
    assert "include { HELPER }" in opaque
    assert "onComplete" in opaque
    assert "errorStrategy" in opaque
    assert render_nextflow_document(parsed) == source
    with pytest.raises(ValueError, match="opaque regions"):
        promote_nextflow_document(parsed)


@pytest.mark.fast
def test_partially_opaque_call_yields_no_connections() -> None:
    source = """nextflow.enable.dsl=2
process TASK {
    input:
    val x
    val y
    script:
    \"\"\"
    true
    \"\"\"
}
workflow PIPELINE {
    main:
    TASK(foo { bar }, plain)
}
workflow { PIPELINE() }
"""
    parsed = parse_nf_text(source)
    assert parsed.connections == ()
    assert "TASK(foo { bar }, plain)" in "\n".join(parsed.opaque_regions)
    with pytest.raises(ValueError, match="opaque regions"):
        promote_nextflow_document(parsed)


@pytest.mark.fast
def test_reader_extracts_ports_container_and_resources() -> None:
    source = render_nextflow(ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess(
            "TASK",
            [NfPort("source", "path")],
            [output_port("result", "result.txt")],
            command("cp", template(ref("source")), "result.txt"),
            container="ubuntu:24.04",
            resources=NfResources(cpus=2, memory_mb=512),
        )],
        [
            NfWorkflowInputConnection("source", "TASK", "source"),
            NfWorkflowOutputConnection("TASK", "result", "result"),
        ],
        {"source": "input.txt"},
    ))
    parsed = parse_nf_text(source, params={"source": "input.txt"})
    process = parsed.processes[0]
    assert process.container == "ubuntu:24.04"
    assert process.cpus == "2"
    assert process.memory == "512 MB"
    assert process.inputs == (NextflowPort("source", "path"),)
    assert process.outputs == (
        NextflowPort("result", "path", "result", "result.txt"),
    )


@pytest.mark.fast
def test_reader_decodes_generated_literal_glob_escapes() -> None:
    target = "out$name`\\'s.txt"
    workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess("TASK", [], [output_port("result", target)], command("touch", target))],
        [NfWorkflowOutputConnection("TASK", "result", "result")],
        {},
    )

    parsed = parse_nf_text(render_nextflow(workflow))

    assert parsed.processes[0].outputs[0].target == target

    mixed = template("$`", ref("name"), ".txt")
    mixed_workflow = ExecutableNextflowWorkflow(
        "PIPELINE",
        [NfProcess(
            "TASK",
            [NfPort("name", "val")],
            [NfPort("result", "path", "result", mixed)],
            command("true"),
        )],
        [
            NfWorkflowInputConnection("name", "TASK", "name"),
            NfWorkflowOutputConnection("TASK", "result", "result"),
        ],
        {"name": "report"},
    )

    mixed_parsed = parse_nf_text(render_nextflow(mixed_workflow))

    assert mixed_parsed.processes[0].outputs[0].target == "$`${name}.txt"


@pytest.mark.fast
def test_reader_retains_dynamic_or_unmapped_resources_as_opaque() -> None:
    source = """nextflow.enable.dsl=2
process TASK {
    cpus params.threads
    memory "4 GB"
    output:
    path 'result.txt', emit: result
    script:
    \"\"\"
    touch result.txt
    \"\"\"
}
workflow PIPELINE {
    main:
    TASK()
    emit:
    result = TASK.out.result
}
workflow { PIPELINE() }
"""

    parsed = parse_nf_text(source)
    opaque = "\n".join(parsed.opaque_regions)
    assert "cpus params.threads" in opaque
    assert 'memory "4 GB"' in opaque
    _workflow_cwl, tools = nextflow_to_cwl(parsed)
    assert "ResourceRequirement" not in tools[0]["requirements"]
    assert render_nextflow_document(parsed) == source


@pytest.mark.fast
def test_reader_rejects_cycles_and_missing_processes() -> None:
    cyclic = """nextflow.enable.dsl=2
process A {
    input:
    val value
    output:
    val value, emit: out
    script:
    \"\"\"
    true
    \"\"\"
}
process B {
    input:
    val value
    output:
    val value, emit: out
    script:
    \"\"\"
    true
    \"\"\"
}
workflow WF {
    main:
    A(B.out.out)
    B(A.out.out)
}
workflow { WF() }
"""
    with pytest.raises(ValueError, match="cycle"):
        parse_nf_text(cyclic)

    with pytest.raises(ValueError, match="unknown process"):
        parse_nf_text(cyclic.replace("B(A.out.out)", "B(MISSING.out.out)"))


@pytest.mark.fast
def test_nextflow_to_cwl_preserves_structure() -> None:
    structural = parse_nf_text(render_nextflow(runtime_workflow()))
    workflow_cwl, tools = nextflow_to_cwl(structural)
    assert [tool["id"] for tool in tools] == ["PRODUCE", "COPY"]
    assert tools[1]["inputs"]["source"]["type"] == "File"
    assert workflow_cwl["steps"][1]["in"]["source"] == "PRODUCE/result"
    assert workflow_cwl["outputs"]["result"]["outputSource"] == "COPY/copy"


@pytest.mark.fast
def test_public_import_reconstructs_sophios_workflow(tmp_path: Path) -> None:
    write_nextflow_artifacts(runtime_workflow(), tmp_path)
    with pytest.warns(UserWarning, match="opaque"):
        imported = import_nextflow(tmp_path / "workflow.nf")
    assert isinstance(imported, Workflow)
    assert imported.process_name == "PIPELINE"
    assert [step.process_name for step in imported.steps] == ["PRODUCE", "COPY"]
    imported_produce = cast(Step, imported.steps[0])
    imported_copy = cast(Step, imported.steps[1])
    assert imported_copy.inputs.source.source_parameter is imported_produce.outputs.result
    compiled = imported.compile()
    assert isinstance(compiled, CompiledWorkflow)
    assert list(compiled.cwl_workflow["steps"])


@pytest.mark.fast
def test_concrete_public_module_exports_importer() -> None:
    from sophios.api.python import nextflow

    assert nextflow.import_nextflow is import_nextflow
    assert nextflow.NextflowDocument is NextflowDocument
    assert nextflow.promote_nextflow_document is promote_nextflow_document
    assert nextflow.render_nextflow_document is render_nextflow_document
