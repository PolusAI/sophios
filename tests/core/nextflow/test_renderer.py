"""Pure deterministic rendering of validated executable IR into artifacts."""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any, cast

import pytest

from sophios.input_output_nf import (
    NF_LOAD_CONTENTS_FUNCTION,
    NF_PARSER_CHECK,
    NF_STUB_LINES,
    render_nextflow,
    render_nextflow_config,
    write_nextflow_artifacts,
)
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.workflow import Workflow
from sophios.ir.artifacts import CompilationResult
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NF_LOAD_CONTENTS_HELPER,
    NF_LOAD_CONTENTS_LIMIT,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfEntry,
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
from sophios.utils_nf import compiled_source_to_nextflow

from .test_phase3_cells import _scatter_step
from .testkit import command, flag_workflow, output_port, ref, runtime_workflow, template


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


#: The entry workflow's record of `runtime_workflow()`'s one output, an unscattered file.
RESULT_RECORD = ("    PIPELINE.out.result.toList().subscribe { items -> println('sophios-output ' + "
                 "groovy.json.JsonOutput.toJson(['result', __sophios_output_9f72e(items[0], 0)])) }\n")


@pytest.mark.serial
def test_renders_named_workflow_and_entry_wrapper() -> None:
    rendered = render_nextflow(runtime_workflow())
    assert "workflow PIPELINE {" in rendered
    assert "take:\n    message" in rendered
    assert "COPY(PRODUCE.out.result)" in rendered
    assert "result = COPY.out.copy" in rendered
    entry = "Channel.value(__sophios_input_9f72e('message', params.message, 'string'))"
    assert f"workflow {{\n{NF_PARSER_CHECK}    PIPELINE({entry})\n{RESULT_RECORD}}}" in rendered


@pytest.mark.fast
def test_a_parameter_no_step_reads_is_checked_before_the_call() -> None:
    workflow = runtime_workflow()
    workflow = ExecutableNextflowWorkflow(
        workflow.name, workflow.processes, workflow.connections,
        {**workflow.params, "unread": 1}, {**workflow.param_types, "unread": "int"},
    )
    assert render_nextflow(workflow).endswith(
        f"workflow {{\n{NF_PARSER_CHECK}    __sophios_input_9f72e('unread', params.unread, 'int')\n"
        f"    PIPELINE(Channel.value(__sophios_input_9f72e('message', params.message, 'string')))\n{RESULT_RECORD}}}\n"
    )


@pytest.mark.fast
def test_every_process_ends_with_the_stub_that_refuses_a_dry_run() -> None:
    rendered = render_nextflow(runtime_workflow())
    stub = "".join(f"\n    {line}" for line in NF_STUB_LINES)
    assert rendered.count(f'    """\n{stub}\n}}') == rendered.count("process ") == 2


@pytest.mark.fast
def test_an_output_record_has_one_array_level_per_scattered_dimension(
    real_scattered_result: CompilationResult,
) -> None:
    """An unscattered output records its one value, a scatter its list, a nested crossproduct one level per input."""
    nested = _scatter_step("NEST", "nested_crossproduct", {"a": ["a"], "b": ["x"]})
    shapes = {
        "unscattered": render_nextflow(runtime_workflow()),
        "scattered": render_nextflow(compiled_source_to_nextflow(real_scattered_result)),
        "nested": render_nextflow(compiled_source_to_nextflow(compile_workflow_result(Workflow([nested], "nest")))),
    }
    values = {shape: [line.split("__sophios_output_9f72e(", 1)[1].split(")", 1)[0]
                      for line in rendered.splitlines() if "subscribe { items" in line]
              for shape, rendered in shapes.items()}
    assert values == {"unscattered": ["items[0], 0"], "scattered": ["items, 1"], "nested": ["items, 2"]}


@pytest.mark.serial
def test_renders_real_compiled_source_with_typed_glob(
    real_supported_result: CompilationResult,
) -> None:
    rendered = render_nextflow(compiled_source_to_nextflow(real_supported_result))
    assert "path \"${filename}\", arity: '1', emit: result" in rendered
    assert "copy(touch.out.result)" in rendered


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
        {"source": "input.txt"}, {"source": "File"},
    ))
    assert "container 'ubuntu:24.04'" in rendered
    assert "cpus 2" in rendered
    assert 'memory "1024 MB"' in rendered
    assert "path 'report.txt', emit: report" in rendered


@pytest.mark.serial
def test_renders_each_entry_staged_hidden_and_made_by_the_task() -> None:
    """design §6, Inputs and channels: a hidden staging name, then a symlink or a copy under the CWL name."""
    process = NfProcess(
        "STAGE",
        [
            NfPort("source", "path", entry=NfEntry("renamed.txt")),
            NfPort("outDir", "path", path_kind="directory", entry=NfEntry(copy=True)),
            NfPort("other", "path"),
        ],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("outDir"),)), "directory")],
        NfCommand((template("cat"), template("renamed.txt"), template("--outDir"), template(ref("outDir")))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection(name, "STAGE", name) for name in ("source", "outDir", "other")],
        {"source": "input.txt", "outDir": "out", "other": "input2.txt"},
        {"source": "File", "outDir": "Directory", "other": "File"},
    ))
    quote = "__sophios_shell_quote_9f72e"
    entry = "(outDir.toString() - '.sophios_entry_outDir/')"
    assert (
        "    path source, stageAs: '.sophios_entry_source/*'\n"
        "    path outDir, stageAs: '.sophios_entry_outDir/*'\n"
        "    path other\n"
    ) in rendered
    # A basename reference to the entry is its staged file's name, which the entry keeps.
    assert '    path "${outDir.name}", glob: false, emit: result\n' in rendered
    exists = "InitialWorkDirRequirement entry already exists in the task directory:"
    renamed, own = f"${{{quote}('renamed.txt')}}", f"${{{quote}({entry})}}"
    assert (
        f"    if [ -e {renamed} ] || [ -L {renamed} ]; then echo '{exists}' {renamed} >&2; exit 1; fi\n"
        f"    ln -s -- ${{{quote}(source.toString())}} {renamed}\n"
        f"    if [ -e {own} ] || [ -L {own} ]; then echo '{exists}' {own} >&2; exit 1; fi\n"
        f"    cp -RL -- ${{{quote}(outDir.toString())}} {own}\n"
        f"    ${{{quote}('cat')}} ${{{quote}('renamed.txt')}} ${{{quote}('--outDir')}} ${{{quote}({entry})}}\n"
    ) in rendered


@pytest.mark.serial
def test_a_scattered_entry_is_staged_hidden_in_its_tuple() -> None:
    process = NfProcess(
        "STAGE",
        [NfPort("source", "path", entry=NfEntry(copy=True))],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("source"),)))],
        NfCommand((template("touch"), template(ref("source")))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("sources", "STAGE", "source", "dotproduct")],
        {"sources": ["a.txt", "b.txt"]}, {"sources": "File[]"},
    ))
    assert "    tuple val(__sophios_scatter_index_9f72e), path(source, stageAs: '.sophios_entry_source/*')\n" in rendered
    assert "    cp -RL -- ${__sophios_shell_quote_9f72e(source.toString())} " in rendered


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
        {"source": "input.txt"}, {"source": "File"},
    )
    from_mapping = ExecutableNextflowWorkflow(
        "WF",
        [process],
        connections,
        {"source": {"class": "File", "path": "input.txt"}}, {"source": "File"},
    )

    rendered = render_nextflow(from_string)
    assert rendered == render_nextflow(from_mapping)
    assert (
        "Channel.value(file(__sophios_input_9f72e('source', params.source, 'File'), glob: false))"
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
        {"reverse": True}, {"reverse": "boolean"},
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
        {"names": ["alice", "bob"]}, {"names": "string[]"},
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
        {"names": ["alice"]}, {"names": "string[]"},
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
        {"sources": ["a.txt", "b.txt"]}, {"sources": "File[]"},
    ))

    assert (
        "Channel.value(__sophios_input_9f72e('sources', params.sources, 'File[]')"
        ".collect { entry -> file(entry, glob: false) })"
    ) in rendered


@pytest.mark.serial
def test_renders_array_of_path_input_with_a_list_arity_and_array_of_val_without() -> None:
    process = NfProcess(
        "CAT",
        [NfPort("sources", "path", is_array=True), NfPort("names", "val", is_array=True)],
        [],
        NfCommand((
            NfTemplate((NfLiteral("cat"),)),
            NfArrayBinding("sources"),
            NfArrayBinding("names"),
        )),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [
            NfWorkflowInputConnection("sources", "CAT", "sources"),
            NfWorkflowInputConnection("names", "CAT", "names"),
        ],
        {"sources": ["a.txt"], "names": ["alice"]}, {"sources": "File[]", "names": "string[]"},
    ))

    # A path input holding one file reaches the script as a bare Path, so the
    # arity is what lets the array binding call list operations on it; the
    # numbered directories keep same-named files apart.
    assert "    path sources, arity: '0..*', stageAs: 'gather_sources_*/*'\n" in rendered
    assert "    val names\n" in rendered


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
        {"source": "lines.txt"}, {"source": "File"},
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
    assert "path 'out*.txt', arity: '1', emit: result" in rendered


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
        {"source": "lines.txt"}, {"source": "File"},
    ))

    assert "path \"${source.name}*.txt\", arity: '1', emit: result" in rendered


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
        {"pattern": "*.txt"}, {"pattern": "string"},
    ))

    assert "path \"${pattern}\", arity: '1', emit: matched" in rendered


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
            "Channel.value(__sophios_input_9f72e('items', params.items, 'string[]'))",
        ),
        (
            NfPort("item", "path"),
            "Channel.value(__sophios_input_9f72e('items', params.items, 'File[]')"
            ".collect { entry -> file(entry, glob: false) })",
        ),
    ],
    ids=["val", "path"],
)
def test_a_scattered_parameter_carries_its_whole_array(
    port: NfPort,
    construction: str,
) -> None:
    process = NfProcess(
        "SCATTER",
        [port],
        [output_port("result", "out.txt")],
        command("echo", NfTemplate((NfLiteral("x"),))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "WF",
        [process],
        [NfWorkflowInputConnection("items", "SCATTER", "item", "dotproduct")],
        {"items": ["a", "b"]},
        {"items": "string[]" if port.qualifier == "val" else "File[]"},
    ))

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
        {"items": "a"}, {"items": "string"},
    ))

    assert "    TASK(items)" in rendered


@pytest.mark.fast
def test_a_capture_marker_renders_as_a_single_valued_path_output() -> None:
    """The author's cardinality declaration is stated, not silently dropped."""
    glob = NfTemplate((NfLiteral("out.txt"),))
    declared = NfProcess(
        "DECLARED",
        [],
        [NfPort("result", "path", "result", glob, capture="single")],
        command("touch", "out.txt"),
    )
    undeclared = NfProcess(
        "UNDECLARED",
        [],
        [NfPort("result", "path", "result", glob)],
        command("touch", "out.txt"),
    )

    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "wf", [declared, undeclared], [], {}
    ))

    assert "    path 'out.txt', glob: false, arity: '1', emit: result\n" in rendered
    assert "    path 'out.txt', emit: result\n" in rendered
    assert rendered.count("arity: '1'") == 1


@pytest.mark.fast
@pytest.mark.parametrize("glob", ["out{1,2}.txt", r"out\literal.txt"])
def test_a_capture_marker_disables_nextflow_only_glob_metacharacters(glob: str) -> None:
    process = NfProcess(
        "CAPTURE",
        [],
        [NfPort("result", "path", "result", NfTemplate((NfLiteral(glob),)), capture="single")],
        command("true"),
    )

    rendered = render_nextflow(ExecutableNextflowWorkflow("wf", [process], [], {}))
    output_line = next(line for line in rendered.splitlines() if "arity: '1'" in line)

    assert "glob: false, arity: '1'" in output_line


def _text_capture_workflow() -> ExecutableNextflowWorkflow:
    glob = NfTemplate((NfLiteral("out.txt"),))
    process = NfProcess(
        "READ",
        [],
        [NfPort("text", "val", "text", glob, capture="text")],
        command("printf", "hello", stdout="out.txt"),
    )
    return ExecutableNextflowWorkflow("wf", [process], [], {})


@pytest.mark.fast
def test_a_text_capture_renders_as_a_value_output_over_the_generated_helper() -> None:
    rendered = render_nextflow(_text_capture_workflow())

    assert (
        f"    val({NF_LOAD_CONTENTS_HELPER}"
        "(task.workDir.resolve('out.txt'))), emit: text\n"
    ) in rendered
    assert NF_LOAD_CONTENTS_FUNCTION in rendered
    # The mapping's own two halves: the byte limit and strict UTF-8 decoding.
    assert f"bytes.length > {NF_LOAD_CONTENTS_LIMIT}" in rendered
    assert "UTF_8.newDecoder()" in rendered


@pytest.mark.fast
def test_the_load_contents_helper_is_emitted_only_where_it_is_used() -> None:
    """Artifacts for models that predate this capture must stay byte-identical."""
    assert NF_LOAD_CONTENTS_HELPER not in render_nextflow(runtime_workflow())
    assert NF_LOAD_CONTENTS_HELPER in render_nextflow(_text_capture_workflow())
