"""Pure deterministic rendering of validated executable IR into artifacts."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, cast

import pytest

from sophios.input_output_nf import (
    render_nextflow,
    render_nextflow_config,
    write_nextflow_artifacts,
)
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfResources,
    NfShellLiteral,
    NfTemplate,
    NfWorkflowInputConnection,
)
from sophios.utils_nf import cwl_rosetree_to_nextflow
from sophios.wic_types import RoseTree

from .testkit import command, flag_workflow, output_port, runtime_workflow


@pytest.mark.serial
def test_writes_four_deterministic_artifacts(tmp_path: Path) -> None:
    workflow = runtime_workflow()
    before = workflow.to_json()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}
    assert [path.name for path in paths] == [
        "nextflow_workflow.json",
        "workflow.nf",
        "nextflow.config",
        "nextflow_params.json",
    ]
    write_nextflow_artifacts(workflow, tmp_path)
    assert {path.name: path.read_bytes() for path in paths} == first
    assert workflow.to_json() == before


@pytest.mark.fast
def test_renderer_rejects_non_executable_representation() -> None:
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        render_nextflow(cast(Any, {"representation_kind": "structural"}))


@pytest.mark.fast
def test_write_artifacts_rejects_non_executable_representation(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="requires ExecutableNextflowWorkflow"):
        write_nextflow_artifacts(cast(Any, {"representation_kind": "structural"}), tmp_path)


@pytest.mark.serial
def test_renders_named_workflow_and_entry_wrapper() -> None:
    rendered = render_nextflow(runtime_workflow())
    assert "workflow PIPELINE {" in rendered
    assert "take:\n    message" in rendered
    assert "COPY(PRODUCE.out.result)" in rendered
    assert "result = COPY.out.copy" in rendered
    assert "workflow {\n    PIPELINE(Channel.value(params.message))\n}" in rendered


@pytest.mark.serial
def test_renders_real_compiled_source_with_typed_glob(real_supported_rose: RoseTree) -> None:
    rendered = render_nextflow(cwl_rosetree_to_nextflow(real_supported_rose))
    assert 'path "${filename}", emit: result' in rendered
    assert "wf__step__2__copy(wf__step__1__touch.out.result)" in rendered


@pytest.mark.serial
def test_renders_supported_process_metadata() -> None:
    process = NfProcess(
        "TASK",
        [NfPort("source", "path")],
        [output_port("report", "report.txt")],
        command("touch", "report.txt"),
        container="ubuntu:24.04",
        resources=NfResources(2, 1024),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "TASK", "source")],
        {"source": "input.txt"},
    ))
    assert "container 'ubuntu:24.04'" in rendered
    assert "cpus 2" in rendered
    assert 'memory "1024 MB"' in rendered
    assert "path 'report.txt', emit: report" in rendered


@pytest.mark.serial
def test_renders_stage_as_rename_and_bare_input_declarations() -> None:
    process = NfProcess(
        "STAGE",
        [NfPort("source", "path", stage_as="renamed.txt"), NfPort("other", "path")],
        [output_port("report", "report.txt")],
        NfCommand((NfTemplate((NfLiteral("cat"),)), NfTemplate((NfLiteral("renamed.txt"),)))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [
            NfWorkflowInputConnection("source", "STAGE", "source"),
            NfWorkflowInputConnection("other", "STAGE", "other"),
        ],
        {"source": "input.txt", "other": "input2.txt"},
    ))
    assert "path source, stageAs: 'renamed.txt'" in rendered
    assert "path other\n" in rendered


@pytest.mark.serial
def test_renders_resources_without_numeric_semantic_loss() -> None:
    process = NfProcess(
        "TASK",
        [],
        [output_port("report", "report.txt")],
        command("touch", "report.txt"),
        resources=NfResources(cpus=2, memory_mb=1234567),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("WF", [process], [], {}))

    assert "cpus 2" in rendered
    assert 'memory "1234567 MB"' in rendered
    assert "e+" not in rendered.lower()


@pytest.mark.serial
def test_path_parameter_rendering_is_runtime_shape_independent() -> None:
    process = NfProcess(
        "READ",
        [NfPort("source", "path")],
        [],
        command("true"),
    )
    connections = [NfWorkflowInputConnection("source", "READ", "source")]
    from_string = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": "input.txt"},
    )
    from_mapping = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": {"class": "File", "path": "input.txt"}},
    )

    rendered = render_nextflow(from_string)
    assert rendered == render_nextflow(from_mapping)
    assert (
        "Channel.fromPath(params.source instanceof Map ? params.source.path : "
        "params.source, checkIfExists: true, type: 'file', glob: false)"
    ) in rendered


@pytest.mark.serial
def test_renders_boolean_flag_as_a_conditional_argv_word() -> None:
    process = NfProcess(
        "SORT",
        [NfPort("reverse", "val")],
        [output_port("result", "sorted.txt")],
        NfCommand(
            (NfTemplate((NfLiteral("sort"),)), NfFlag("reverse", "-r")),
            stdout=NfTemplate((NfLiteral("sorted.txt"),)),
        ),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("reverse", "SORT", "reverse")],
        {"reverse": True},
    ))

    assert "${reverse ? __sophios_shell_quote_9f72e('-r') : ''}" in rendered


@pytest.mark.serial
def test_renders_array_binding_with_prefix_as_a_conditional_expression() -> None:
    process = NfProcess(
        "NAMES",
        [NfPort("names", "val", is_array=True)],
        [output_port("result", "out.txt")],
        NfCommand(
            (NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names", "--name")),
            stdout=NfTemplate((NfLiteral("out.txt"),)),
        ),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("names", "NAMES", "names")],
        {"names": ["alice", "bob"]},
    ))

    assert (
        "${names.isEmpty() ? '' : ([__sophios_shell_quote_9f72e('--name')] + "
        "names.collect{ __sophios_shell_quote_9f72e(it.toString()) }).join(' ')}"
    ) in rendered


@pytest.mark.serial
def test_renders_array_binding_without_a_prefix() -> None:
    process = NfProcess(
        "NAMES",
        [NfPort("names", "val", is_array=True)],
        [output_port("result", "out.txt")],
        NfCommand(
            (NfTemplate((NfLiteral("echo"),)), NfArrayBinding("names")),
            stdout=NfTemplate((NfLiteral("out.txt"),)),
        ),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("names", "NAMES", "names")],
        {"names": ["alice"]},
    ))

    assert (
        "${names.isEmpty() ? '' : names.collect{ __sophios_shell_quote_9f72e(it.toString()) }"
        ".join(' ')}"
    ) in rendered


@pytest.mark.serial
def test_renders_shell_literal_as_raw_unquoted_text() -> None:
    process = NfProcess(
        "REDIRECT",
        [],
        [output_port("result", "out.txt")],
        NfCommand((
            NfTemplate((NfLiteral("printf"),)),
            NfTemplate((NfLiteral("hi"),)),
            NfShellLiteral(">>"),
            NfTemplate((NfLiteral("out.txt"),)),
        )),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("WF", [process], [], {}))

    command_line = next(line for line in rendered.splitlines() if "printf" in line)
    assert " >> " in command_line
    assert "__sophios_shell_quote_9f72e('>>')" not in command_line
    assert command_line.count("__sophios_shell_quote_9f72e") == 3


@pytest.mark.serial
def test_renders_shell_literal_escapes_gstring_interpolation() -> None:
    """The script block is a GString, so a literal $ must not trigger interpolation."""
    process = NfProcess(
        "REDIRECT",
        [],
        [output_port("result", "out.txt")],
        NfCommand((NfTemplate((NfLiteral("true"),)), NfShellLiteral("$HOME >> out.txt"))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("WF", [process], [], {}))

    assert r"\$HOME >> out.txt" in rendered


@pytest.mark.serial
def test_renders_array_of_path_channel_construction_with_staging_per_element() -> None:
    process = NfProcess(
        "CAT",
        [NfPort("sources", "path", is_array=True)],
        [],
        NfCommand((NfTemplate((NfLiteral("cat"),)), NfArrayBinding("sources"))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("sources", "CAT", "sources")],
        {"sources": ["a.txt", "b.txt"]},
    ))

    assert (
        "Channel.value(params.sources.collect { entry -> file("
        "entry instanceof Map ? entry.path : entry, "
        "checkIfExists: true, type: 'file') })"
    ) in rendered


@pytest.mark.serial
def test_flag_workflow_artifacts_are_byte_stable(tmp_path: Path) -> None:
    workflow = flag_workflow()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}

    write_nextflow_artifacts(workflow, tmp_path)

    assert {path.name: path.read_bytes() for path in paths} == first
    assert render_nextflow(workflow) == render_nextflow(workflow)


@pytest.mark.serial
def test_renders_basename_segments_in_commands_and_globs() -> None:
    basename = NfTemplate((NfBasenameReference("source"), NfLiteral(".copy")))
    process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [NfPort("result", "path", "result", basename)],
        NfCommand((NfTemplate((NfLiteral("cp"),)), basename)),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "COPY", "source")],
        {"source": "lines.txt"},
    ))

    assert "source.name.toString() + '.copy'" in rendered
    assert 'path "${source.name}.copy", glob: false, emit: result' in rendered


@pytest.mark.fast
def test_all_literal_output_name_keeps_globbing_on() -> None:
    # An author-written name carries no runtime data, so its pattern meaning
    # is whatever the author wrote; nothing here is assembled at run time.
    glob = NfTemplate((NfLiteral("out*.txt"),))
    process = NfProcess(
        "RUN", [], [NfPort("result", "path", "result", glob)], command("true")
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("WF", [process], [], {}))

    # The exact declaration pins the absence of the option: with it, the
    # rendered line would read ", glob: false, emit: result" instead.
    assert "path 'out*.txt', emit: result" in rendered


@pytest.mark.fast
def test_assembled_name_with_an_author_written_metacharacter_keeps_globbing_on() -> None:
    # The reference makes the name runtime-assembled, but the author put a
    # metacharacter in their own literal part, so they asked for a pattern.
    glob = NfTemplate((NfBasenameReference("source"), NfLiteral("*.txt")))
    process = NfProcess(
        "COPY",
        [NfPort("source", "path")],
        [NfPort("result", "path", "result", glob)],
        command("true"),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("source", "COPY", "source")],
        {"source": "lines.txt"},
    ))

    assert 'path "${source.name}*.txt", emit: result' in rendered


@pytest.mark.fast
def test_pattern_valued_input_glob_keeps_globbing_on() -> None:
    # A plain input reference carries arbitrary data, and a glob is a
    # legitimate thing to pass in: glob: $(inputs.pattern) with "*.txt" must
    # still match. Only a basename reference is literal by construction.
    glob = NfTemplate((NfInputReference("pattern"),))
    process = NfProcess(
        "MATCH",
        [NfPort("pattern", "val")],
        [NfPort("matched", "path", "matched", glob)],
        command("true"),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("pattern", "MATCH", "pattern")],
        {"pattern": "*.txt"},
    ))

    assert 'path "${pattern}", emit: matched' in rendered


@pytest.mark.fast
def test_config_uses_validated_workflow_container_policy() -> None:
    host = ExecutableNextflowWorkflow(
        "HOST_WF",
        [NfProcess("HOST", [], [], command("true"))],
        [],
        {},
    )
    containerized = ExecutableNextflowWorkflow(
        "CONTAINER_WF",
        [NfProcess("CONTAINER", [], [], command("true"), container="ubuntu:24.04")],
        [],
        {},
    )
    assert render_nextflow_config(host) == "docker.enabled = false\n"
    assert render_nextflow_config(containerized) == "docker.enabled = true\n"


@pytest.mark.serial
@pytest.mark.parametrize(
    ("port", "construction"),
    [
        (
            NfPort("item", "val"),
            "Channel.value(params.items)",
        ),
        (
            NfPort("item", "path"),
            "Channel.value(params.items.collect { entry -> file("
            "entry instanceof Map ? entry.path : entry, "
            "checkIfExists: true, type: 'file') })",
        ),
    ],
    ids=["val", "path"],
)
def test_renders_the_scatter_adapter_at_the_consumption_site(
    port: NfPort,
    construction: str,
) -> None:
    """The adapted parameter carries the whole list; each sink derives its own queue."""
    process = NfProcess(
        "SCATTER",
        [port],
        [output_port("result", "out.txt")],
        command("echo", NfTemplate((NfLiteral("x"),))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("items", "SCATTER", "item", "scatter")],
        {"items": ["a", "b"]},
    ))

    assert "    SCATTER(items.flatten())" in rendered
    assert construction in rendered


@pytest.mark.serial
def test_renders_an_unadapted_workflow_input_without_an_operator() -> None:
    process = NfProcess(
        "TASK",
        [NfPort("item", "val")],
        [output_port("result", "out.txt")],
        command("echo", NfTemplate((NfLiteral("x"),))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("items", "TASK", "item")],
        {"items": "a"},
    ))

    assert "    TASK(items)" in rendered
    assert ".flatten()" not in rendered
