"""Pure deterministic rendering of validated executable IR into artifacts."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from sophios.input_output_nf import (
    NF_LOAD_CONTENTS_FUNCTION,
    NF_PARSER_CHECK,
    NF_STUB_LINES,
    render_nextflow,
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
@pytest.mark.parametrize("build", [runtime_workflow, flag_workflow])
def test_writes_four_deterministic_artifacts(build: Callable[[], ExecutableNextflowWorkflow], tmp_path: Path) -> None:
    workflow = build()
    before = workflow.to_json()
    paths = write_nextflow_artifacts(workflow, tmp_path)
    first = {path.name: path.read_bytes() for path in paths}
    assert list(first) == ["nextflow_workflow.json", "workflow.nf", "nextflow.config", "nextflow_params.json"]
    write_nextflow_artifacts(workflow, tmp_path)
    assert {path.name: path.read_bytes() for path in paths} == first
    assert workflow.to_json() == before


#: The entry workflow's record of `runtime_workflow()`'s one output, an unscattered file.
RESULT_RECORD = ("    PIPELINE.out.result.toList().subscribe { items -> println('sophios-output ' + "
                 "groovy.json.JsonOutput.toJson(['result', __sophios_output_9f72e(items[0], 0)])) }\n")


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


def _fed(*processes: NfProcess, adapter: str | None = None, **params: tuple[Any, str]) -> ExecutableNextflowWorkflow:
    """Each input of `processes` fed through `adapter` by the param `params` gives it as (value, declared type), or
    by the one param when there is one."""
    names = list(params)
    return ExecutableNextflowWorkflow("WF", processes, [
        NfWorkflowInputConnection(port.name if port.name in params else names[0], process.name, port.name, adapter)
        for process in processes for port in process.inputs],
        {name: value for name, (value, _) in params.items()},
        {name: declared for name, (_, declared) in params.items()})


def _staged() -> ExecutableNextflowWorkflow:
    return _fed(NfProcess("STAGE", [
        NfPort("source", "path", entry=NfEntry("renamed.txt")),
        NfPort("outDir", "path", path_kind="directory", entry=NfEntry(copy=True)), NfPort("other", "path")],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("outDir"),)), "directory")],
        command("cat", "renamed.txt", "--outDir", template(ref("outDir")))),
        source=("input.txt", "File"), outDir=("out", "Directory"), other=("input2.txt", "File"))


def _unread() -> ExecutableNextflowWorkflow:
    workflow = runtime_workflow()
    return ExecutableNextflowWorkflow(workflow.name, workflow.processes, workflow.connections,
                                      {**workflow.params, "unread": 1}, {**workflow.param_types, "unread": "int"})


def _text_capture_workflow() -> ExecutableNextflowWorkflow:
    return ExecutableNextflowWorkflow("wf", [NfProcess("READ", [], [NfPort("text", "val", "text", template("out.txt"),
                                                                           capture="text")],
                                                       command("printf", "hello", stdout="out.txt"))], [], {})


def _alone(*outputs: NfPort, tokens: tuple[Any, ...] = (template("true"),)) -> ExecutableNextflowWorkflow:
    return ExecutableNextflowWorkflow("WF", [NfProcess("TASK", [], outputs, NfCommand(tokens))], [], {})


_Q = "__sophios_shell_quote_9f72e"
_ENTRY = "(outDir.toString() - '.sophios_entry_outDir/')"
_EXISTS = "InitialWorkDirRequirement entry already exists in the task directory:"
_RENAMED, _OWN = f"${{{_Q}('renamed.txt')}}", f"${{{_Q}({_ENTRY})}}"
_BASENAME_COPY = NfTemplate((NfBasenameReference("source"), NfLiteral(".copy")))
_ENTRY_CALL = "Channel.value(__sophios_input_9f72e('message', params.message, 'string'))"
_NAMES = NfPort("names", "val", is_array=True)

#: Each IR idiom, built when its row runs, the fixed Nextflow text it renders as, and text it never renders.
IDIOMS: dict[str, tuple[Callable[[], ExecutableNextflowWorkflow], list[str], list[str]]] = {
    "named-workflow-and-entry-wrapper": (runtime_workflow, [
        "workflow PIPELINE {", "take:\n    message", "COPY(PRODUCE.out.result)", "result = COPY.out.copy",
        f"workflow {{\n{NF_PARSER_CHECK}    PIPELINE({_ENTRY_CALL})\n{RESULT_RECORD}}}"], [NF_LOAD_CONTENTS_HELPER]),
    "an-unread-param-checked-before-the-call": (_unread, [
        f"workflow {{\n{NF_PARSER_CHECK}    __sophios_input_9f72e('unread', params.unread, 'int')\n"
        f"    PIPELINE({_ENTRY_CALL})\n{RESULT_RECORD}}}\n"], []),
    "process-metadata": (partial(_fed, NfProcess(
        "TASK", [NfPort("source", "path")], [output_port("report", "report.txt")], command("touch", "report.txt"),
        container="ubuntu:24.04", resources=NfResources(2, 1024)), source=("input.txt", "File")),
        ["container 'ubuntu:24.04'", "cpus 2", 'memory "1024 MB"', "path 'report.txt', emit: report"], []),
    # design §6, Inputs and channels: a hidden staging name, then a symlink or a copy under the CWL name. A basename
    # reference to the entry is the last part of its staged path: the path's `.name` would be the whole of it.
    "each-entry-staged-hidden-and-made-by-the-task": (_staged, [
        "    path source, stageAs: '.sophios_entry_source/*'\n    path outDir, stageAs: '.sophios_entry_outDir/*'\n"
        "    path other\n", "    path \"${outDir.toString().tokenize('/').last()}\", glob: false, emit: result\n",
        f"if [ -e {_RENAMED} ] || [ -L {_RENAMED} ]; then echo '{_EXISTS}' {_RENAMED} >&2; exit 1; fi\n"
        f"ln -s -- ${{{_Q}(source.toString())}} {_RENAMED}\n"
        f"if [ -e {_OWN} ] || [ -L {_OWN} ]; then echo '{_EXISTS}' {_OWN} >&2; exit 1; fi\n"
        f"cp -RL -- ${{{_Q}(outDir.toString())}} {_OWN}\n"
        f"${{{_Q}('cat')}} ${{{_Q}('renamed.txt')}} ${{{_Q}('--outDir')}} ${{{_Q}({_ENTRY})}}\n"], []),
    "a-scattered-entry-staged-hidden-in-its-tuple": (partial(_fed, NfProcess(
        "STAGE", [NfPort("source", "path", entry=NfEntry(copy=True))],
        [NfPort("result", "path", "result", NfTemplate((NfBasenameReference("source"),)))],
        command("touch", template(ref("source")))), adapter="dotproduct", sources=(["a.txt", "b.txt"], "File[]")), [
        "    tuple val(__sophios_scatter_index_9f72e), path(source, stageAs: '.sophios_entry_source/*')\n",
        f"\ncp -RL -- ${{{_Q}(source.toString())}} "], []),
    "a-boolean-flag-as-a-conditional-argv-word": (partial(_fed, NfProcess(
        "SORT", [NfPort("reverse", "val")], [output_port("result", "sorted.txt")],
        NfCommand((template("sort"), NfFlag("reverse", "-r")), stdout=template("sorted.txt"))),
        reverse=(True, "boolean")),
        [f"${{reverse ? {_Q}('-r') : ''}}"], []),
    "an-array-binding-with-a-prefix": (partial(_fed, NfProcess(
        "NAMES", [_NAMES], [], NfCommand((template("echo"), NfArrayBinding("names", "--name")))),
        names=(["alice", "bob"], "string[]")),
        [f"${{names.isEmpty() ? '' : ([{_Q}('--name')] + names.collect{{ {_Q}(it.toString()) }}).join(' ')}}"], []),
    "an-array-binding-without-a-prefix": (partial(_fed, NfProcess(
        "NAMES", [_NAMES], [], NfCommand((template("echo"), NfArrayBinding("names")))), names=(["alice"], "string[]")),
        [f"${{names.isEmpty() ? '' : names.collect{{ {_Q}(it.toString()) }}.join(' ')}}"], []),
    "a-shell-literal-as-raw-unquoted-text": (partial(_alone, tokens=(
        template("printf"), template("hi"), NfShellLiteral(">>"), template("out.txt"))),
        [f"\n${{{_Q}('printf')}} ${{{_Q}('hi')}} >> ${{{_Q}('out.txt')}}\n"], [f"{_Q}('>>')"]),
    # The script block is a GString, so a literal $ must not trigger interpolation.
    "a-shell-literal-escapes-gstring-interpolation": (partial(_alone, tokens=(
        template("true"), NfShellLiteral("$HOME >> out.txt"))), [r"\$HOME >> out.txt"], []),
    "an-array-of-paths-staged-per-element": (partial(_fed, NfProcess(
        "CAT", [NfPort("sources", "path", is_array=True)], [], NfCommand((template("cat"), NfArrayBinding("sources")))),
        sources=(["a.txt", "b.txt"], "File[]")), ["Channel.value(__sophios_input_9f72e('sources', params.sources, "
                                                  "'File[]').collect { entry -> file(entry, glob: false) })"], []),
    # A path input holding one file reaches the script as a bare Path, so the arity is what lets the array binding
    # call list operations on it; the numbered directories keep same-named files apart.
    "an-array-of-paths-with-a-list-arity-and-of-vals-without": (partial(_fed, NfProcess(
        "CAT", [NfPort("sources", "path", is_array=True), _NAMES], [],
        NfCommand((template("cat"), NfArrayBinding("sources"), NfArrayBinding("names")))),
        sources=(["a.txt"], "File[]"), names=(["alice"], "string[]")),
        ["    path sources, arity: '0..*', stageAs: 'gather_sources_*/*'\n", "    val names\n"], []),
    "basename-segments-in-commands-and-globs": (partial(_fed, NfProcess(
        "COPY", [NfPort("source", "path")], [NfPort("result", "path", "result", _BASENAME_COPY)],
        command("cp", _BASENAME_COPY)), source=("lines.txt", "File")),
        ["source.toString().tokenize('/').last().toString() + '.copy'",
         "path \"${source.toString().tokenize('/').last()}.copy\", glob: false, emit: result"], []),
    # An author-written name carries no runtime data, so its pattern meaning is whatever the author wrote.
    "an-all-literal-output-name-keeps-globbing-on": (partial(_alone, NfPort("result", "path", "result",
                                                                            template("out*.txt"))),
                                                     ["path 'out*.txt', arity: '1', emit: result"], []),
    # The reference makes the name runtime-assembled, but the author asked for a pattern in their own literal part.
    "an-assembled-name-with-an-author-written-metacharacter": (partial(_fed, NfProcess(
        "COPY", [NfPort("source", "path")], [NfPort("result", "path", "result", NfTemplate(
            (NfBasenameReference("source"), NfLiteral("*.txt"))))], command("true")), source=("lines.txt", "File")),
        ["path \"${source.toString().tokenize('/').last()}*.txt\", arity: '1', emit: result"], []),
    # A plain reference carries arbitrary data, and glob: $(inputs.pattern) with "*.txt" must still match.
    "a-pattern-valued-input-glob-keeps-globbing-on": (partial(_fed, NfProcess(
        "MATCH", [NfPort("pattern", "val")], [NfPort("matched", "path", "matched", template(ref("pattern")))],
        command("true")), pattern=("*.txt", "string")), ["path \"${pattern}\", arity: '1', emit: matched"], []),
    **{f"a-scattered-{qualifier}-parameter-carries-its-whole-array": (partial(_fed, NfProcess(
        "SCATTER", [NfPort("item", qualifier)], [output_port("result", "out.txt")], command("echo", "x")),
        adapter="dotproduct", items=(["a", "b"], declared)), [construction], [])
       for qualifier, declared, construction in (
           ("val", "string[]", "Channel.value(__sophios_input_9f72e('items', params.items, 'string[]'))"),
           ("path", "File[]", "Channel.value(__sophios_input_9f72e('items', params.items, 'File[]')"
                              ".collect { entry -> file(entry, glob: false) })"))},
    "an-unadapted-workflow-input-without-an-operator": (partial(_fed, NfProcess(
        "TASK", [NfPort("item", "val")], [output_port("result", "out.txt")], command("echo", "x")),
        items=("a", "string")), ["    TASK(items)"], []),
    # The author's cardinality declaration is stated, not silently dropped.
    "a-capture-marker-as-a-single-valued-path-output": (lambda: ExecutableNextflowWorkflow("wf", [
        NfProcess(name, [], [NfPort("result", "path", "result", template("out.txt"), capture=capture)],
                  command("touch", "out.txt")) for name, capture in (("DECLARED", "single"), ("UNDECLARED", None))],
        [], {}), ["    path 'out.txt', glob: false, arity: '1', emit: result\n",
                  "    path 'out.txt', emit: result\n"], []),
    **{f"a-capture-marker-disables-{glob}": (partial(_alone, NfPort("result", "path", "result", template(glob),
                                                                    capture="single")),
                                             ["glob: false, arity: '1', emit: result"], [])
       for glob in ("out{1,2}.txt", r"out\literal.txt")},
    # The mapping's own two halves: the byte limit and strict UTF-8 decoding.
    "a-text-capture-as-a-value-output-over-the-generated-helper": (_text_capture_workflow, [
        f"    val({NF_LOAD_CONTENTS_HELPER}(task.workDir.resolve('out.txt'))), emit: text\n", NF_LOAD_CONTENTS_FUNCTION,
        f"bytes.length > {NF_LOAD_CONTENTS_LIMIT}", "UTF_8.newDecoder()"], []),
}


@pytest.mark.fast
@pytest.mark.parametrize(("build", "present", "absent"), IDIOMS.values(), ids=IDIOMS)
def test_each_idiom_renders_as_its_fixed_text(build: Callable[[], ExecutableNextflowWorkflow], present: list[str],
                                              absent: list[str]) -> None:
    """design §6, the idiom table: an IR idiom renders as one fixed Nextflow text, and the load-contents helper only
    where a text capture uses it, so models that predate it render byte-identically."""
    rendered = render_nextflow(build())
    assert [text for text in present if text not in rendered] == []
    assert [text for text in absent if text in rendered] == []
