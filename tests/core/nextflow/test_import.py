"""The import is the renderer's exact inverse over the workflows Sophios compiles (design §4.2): the pipeline of each
example workflow imports to a workflow that compiles back to the same model, layout-only rewrites included; an edit
outside the subset stops the import at the line it names."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
import contextlib
from contextlib import suppress
from dataclasses import dataclass
from functools import cache, partial
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from hypothesis import example, given, strategies as st
import pytest

from sophios.api.python._ports import ParameterNamespace
from sophios.api.python.nextflow import import_nextflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Inference, StepInput, Workflow
from sophios.input_output_nf import (
    NEXTFLOW_CONFIG,
    NEXTFLOW_PARAMS,
    NEXTFLOW_SCRIPT,
    NF_HELPER_FUNCTIONS,
    write_nextflow_artifacts,
)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.nf_reader import read_nextflow
from sophios.nf_types import ExecutableNextflowWorkflow

from ..budgets import budget
from ..cases import DATA
from ..helpers import EXAMPLES, example_workflow, examples
from ..hermetic import network_refused
from ..provocations import IMPORT_EDITS, import_edited
from .test_phase3_cells import calculator_workflow, predicates_workflow

#: The example workflows of tests/core/data/examples, by name.
RUNS = [param.values[0].name for param in examples()]
_FILES = (NEXTFLOW_SCRIPT, NEXTFLOW_PARAMS, NEXTFLOW_CONFIG)


@cache
def _model(name: str) -> ExecutableNextflowWorkflow:
    """The model the example `name` compiles to, read and compiled in a copy of its directory; `calculator` is the
    subset differential's thirty conditions."""
    if name == "calculator":
        return calculator_workflow().compile(target="nextflow").nextflow_workflow
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch) / name
        workflow = example_workflow(EXAMPLES / name, directory)
        with contextlib.chdir(directory):
            return workflow.compile(target="nextflow").nextflow_workflow


@cache
def _texts(name: str | None) -> dict[str, str]:
    """The three files written for the example `name`, or for the one-step workflow `import_edited` writes (None)."""
    with tempfile.TemporaryDirectory() as directory:
        if name is None:
            import_edited(directory, NEXTFLOW_SCRIPT, "", "")
        else:
            write_nextflow_artifacts(_model(name), directory)
        return {file: (Path(directory) / file).read_text(encoding="utf-8") for file in _FILES}


def _imported(directory: Path) -> ExecutableNextflowWorkflow:
    return import_nextflow(directory).compile(target="nextflow", inference=Inference(enabled=False)).nextflow_workflow


def _echo(name: str, **inputs: Input) -> CommandLineTool:
    """A tool that echoes its inputs in order into `<name>.txt`."""
    return CommandLineTool(name, Inputs(**inputs), Outputs(out=Output(cwl.file, glob=f"{name}.txt"))).base_command(
        "echo").stdout(f"{name}.txt")


@pytest.mark.parametrize("name", RUNS)
def test_the_pipeline_of_an_example_imports_to_its_model(
        name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The three files written for each example of tests/core/data/examples, alone in a directory and read with
    the network refused, import to a workflow that compiles to the model they were written from; no path a parameter
    names is looked at: not `nextflow_workflow.json`, no tool registry, no input file."""
    model = _model(name)
    for file, text in _texts(name).items():
        (tmp_path / file).write_text(text, encoding="utf-8")
    named = {item["path"] for value in model.to_dict()["params"].values()
             for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict)}
    looked: list[str] = []
    stat = os.stat

    def recording(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        looked.append(os.fspath(path) if isinstance(path, (str, os.PathLike)) else str(path))
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", recording)
    with network_refused() as attempts:
        imported = _imported(tmp_path)
    assert (imported, attempts, named & set(looked)) == (model, [], set())


def _flag_first() -> Workflow:
    """A command that starts with a flag, which renders nothing when it is false."""
    step = CommandLineTool("first", Inputs(env=Input(cwl.boolean, position=0, flag="env")), Outputs()).argument(
        "true", position=1).to_step()
    step.inputs.env = False
    return Workflow([step], "flag_first")


def test_a_pipeline_v0_7_0_wrote_imports() -> None:
    """v0.7.0 indented a script's lines, and wrote a command that starts with a flag without parentheses."""
    assert _imported(DATA / "nextflow" / "v0.7.0") == _flag_first().compile(target="nextflow").nextflow_workflow


# Text with what CWL, Groovy and the shell each escape. A base command is not interpolated, so any text is
# literal there; in an argument, a literal `$(` or `${` is written escaped, and a backslash before one is escaped.
_TEXT = st.lists(st.sampled_from([" ", "a", "$", "\\", "'", '"', "{", "}", "(", ")", "<", "|", ";", "#", "*", "?",
                                  "[", "é", "\t", "$(", "${"]), max_size=4).map("".join)
_LITERAL = st.lists(st.sampled_from([" ", "a", "$", "\\\\", "'", '"', "}", ")", "<", "|", "#", "*", "[", "é", "\t",
                                     "\\$(", "\\${"]), min_size=1, max_size=4).map("".join)
# An int given for a float, alone or in a list, is the float it holds.
_VALUES: dict[str, st.SearchStrategy[Any]] = {
    "string": _TEXT, "int": st.integers(-5, 10**6),
    "float": st.floats(-1e17, 1e17, allow_nan=False) | st.integers(-5, 10**6),
    "boolean": st.booleans(), "File": st.sampled_from(["/data/a.txt", "/data/b c.txt"]),
    "string[]": st.lists(_TEXT, max_size=2), "string?": st.none() | _TEXT, "int?": st.none() | st.integers(0, 9),
    "float?": st.none() | st.floats(0, 10) | st.integers(0, 9),
    "float[]": st.lists(st.floats(0, 10) | st.integers(0, 9), max_size=2),
}
_PREFIXES = st.sampled_from(["-x", "--name=", "--long"])


def _origins(kind: str, value: Any) -> st.SearchStrategy[str]:
    """Where a value can come from: a workflow input or the step, or, a JSON scalar, the tool's default or, unless it
    is a path, the step input's (design §6, Step inputs)."""
    scalar = value is not None and not isinstance(value, list)
    return st.sampled_from(["workflow", "step", *(["tool default"] if scalar else []),
                            *(["step default"] if scalar and kind != "File" else [])])


@st.composite
def _generated(draw: st.DrawFn) -> Workflow:
    """One tool with inputs of each kind, each value bound on the step, a workflow input or a default, reaching the
    command by bindings and by arguments among literal and shell text, in a container or with resources."""
    kinds = draw(st.lists(st.sampled_from(sorted(_VALUES)), min_size=1, max_size=4))
    names = [f"i{index}" for index in range(len(kinds))]
    values = [draw(_VALUES[kind]) for kind in kinds]
    origins = [draw(_origins(kind, value)) for kind, value in zip(kinds, values)]
    specs = {}
    for name, kind, value, origin in zip(names, kinds, values, origins):
        base = kind.removesuffix("?")
        binding: dict[str, Any] = {}
        if base == "boolean" or kind.endswith("?") or draw(st.booleans()):
            binding = {"position": draw(st.integers(1, 9))}
            if base in ("boolean", "string[]", "float[]") or draw(st.booleans()):
                binding["flag"] = draw(_PREFIXES)
                if base not in ("boolean", "string[]", "float[]") and draw(st.booleans()):
                    binding["separate"] = False
        specs[name] = Input(cwl.array(base[:-2]) if base.endswith("[]") else base, required=not kind.endswith("?"),
                            **binding, **({"default": value} if origin == "tool default" else {}))
    words = [st.just(f"$(inputs.{name})") for name, kind in zip(names, kinds) if kind in ("string", "int", "float")]
    words += [st.just(f"$(inputs.{name}.basename)") for name, kind in zip(names, kinds) if kind == "File"]
    tool = CommandLineTool("tool", Inputs(**specs), Outputs(out=Output(cwl.file, glob="out.txt"))).base_command(
        *draw(st.lists(_TEXT.filter(bool), min_size=1, max_size=2))).stdout("out.txt")
    for value, position, prefix in draw(st.lists(st.tuples(st.lists(st.one_of(_LITERAL, *words), min_size=1,
                                                                    max_size=3).map("".join),
                                                           st.integers(0, 9), st.none() | _PREFIXES), max_size=3)):
        tool.argument(value, position=position, **({"prefix": prefix} if prefix else {}))
    if draw(st.booleans()):
        tool.argument(draw(st.sampled_from(["| tee", "2>&1", "&& true"])), shell_quote=False,
                      position=draw(st.integers(0, 9))).shell_command()
    elif draw(st.booleans()):  # Sophios supports a shell command on the host only
        tool.docker(draw(_TEXT.filter(str.strip)))
    strings = [st.just(f"$(inputs.{name})") for name, kind in zip(names, kinds) if kind == "string"]
    for index in range(draw(st.integers(0, 2))):
        tool.env_var(f"V{index}", draw(st.one_of(_TEXT.filter(lambda text: "$(" not in text and "${" not in text),
                                                 *strings)))
    if draw(st.booleans()):
        tool.resources(cores_min=draw(st.integers(1, 8)), ram_min=draw(st.integers(1, 10**6) | st.floats(0.5, 1e17)))
    step = tool.to_step()
    workflow = Workflow([step], "generated")
    for name, kind, value, origin in zip(names, kinds, values, origins):
        if origin == "workflow":  # optional or not whatever the tool's input is, if it has a value
            optional = value is None or draw(st.booleans())
            setattr(step.inputs, name, getattr(workflow.inputs, f"w_{name}").as_type(
                kind.removesuffix("?") + "?" * optional))
            if value is not None:
                setattr(workflow.inputs, f"w_{name}", value)
        elif origin == "step default":
            setattr(step.inputs, name, StepInput(default=value))
        elif origin == "step" and value is not None:
            setattr(step.inputs, name, value)
    workflow.outputs.out = step.outputs.out
    return workflow


@budget(100)
@given(_generated())
@example(calculator_workflow())
@example(predicates_workflow())
def test_the_import_of_a_generated_tool_compiles_to_its_model(workflow: Workflow) -> None:
    """Pinned: the conditions the subset differential runs, which each operator of the subset writes."""
    compiled = workflow.compile(target="nextflow")
    with tempfile.TemporaryDirectory() as directory:
        assert _imported(compiled.write(directory)) == compiled.nextflow_workflow


_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _lines(text: str) -> list[tuple[str, str]]:
    """Each line of `workflow.nf` with its kind: `code`, whose layout is free; `opener`, the triple quote that
    opens a script or stub, whose indentation alone is; and `content`, a helper's or a body's line."""
    lines = text.split("\n")
    kinds = ["code"] * len(lines)
    for helper in NF_HELPER_FUNCTIONS:
        if (start := text.find(helper)) >= 0:
            first = text.count("\n", 0, start)
            kinds[first:first + helper.count("\n") + 1] = ["content"] * (helper.count("\n") + 1)
    body = False
    for index, line in enumerate(lines):
        if body:
            kinds[index], body = "content", line.strip() != '"""'
        elif line.strip() == '"""':  # a script's or stub's closing quotes are in its body
            kinds[index], body = "opener", True
    return list(zip(lines, kinds))


def _code_lines(edit: Callable[[str], list[str]]) -> Callable[[dict[str, str]], dict[str, str]]:
    """A rewrite of `workflow.nf` that replaces each non-empty code line by the lines `edit` makes of it."""
    def apply(files: dict[str, str]) -> dict[str, str]:
        written = [new for line, kind in _lines(files[NEXTFLOW_SCRIPT])
                   for new in (edit(line) if kind == "code" and line.strip() else [line])]
        return {**files, NEXTFLOW_SCRIPT: "\n".join(written)}
    return apply


def _spaced(line: str) -> str:
    """`line` with every space between tokens, outside its strings, a longer run of spaces and tabs."""
    pieces = _STRINGS.split(line)
    strings = _STRINGS.findall(line)
    return "".join(piece.replace(" ", " \t  ") + (strings[index] if index < len(strings) else "")
                   for index, piece in enumerate(pieces))


def _reindented(files: dict[str, str]) -> dict[str, str]:
    written = [f"\t  {line.lstrip(' ')}" if kind in ("code", "opener") and line.strip() else line
               for line, kind in _lines(files[NEXTFLOW_SCRIPT])]
    return {**files, NEXTFLOW_SCRIPT: "\n".join(written)}


_PROCESS_BLOCKS = re.compile(r"(?ms)^process \S+ \{\n.*?\n^\}\n")


def _reordered(files: dict[str, str]) -> dict[str, str]:
    text = files[NEXTFLOW_SCRIPT]
    blocks = _PROCESS_BLOCKS.findall(text)
    start, end = text.index(blocks[0]), text.index(blocks[-1]) + len(blocks[-1])
    return {**files, NEXTFLOW_SCRIPT: text[:start] + "\n".join(reversed(blocks)) + text[end:]}


def _crlf(files: dict[str, str]) -> dict[str, str]:
    return {**files, **{name: files[name].replace("\n", "\r\n") for name in (NEXTFLOW_SCRIPT, NEXTFLOW_CONFIG)}}


def _json(files: dict[str, str]) -> dict[str, str]:
    params = json.loads(files[NEXTFLOW_PARAMS])
    return {**files, NEXTFLOW_PARAMS: json.dumps(dict(reversed(params.items())), indent=1, ensure_ascii=True)}


def _config(files: dict[str, str]) -> dict[str, str]:
    return {**files, NEXTFLOW_CONFIG: "// written by Sophios\n\n" + files[NEXTFLOW_CONFIG].replace(" = ", "  =\t")}


def _around_quotes(files: dict[str, str]) -> dict[str, str]:
    written, body, closers = [], False, 0
    for line, kind in _lines(files[NEXTFLOW_SCRIPT]):
        if kind == "opener":
            line, body = line.replace('"""', '/* c */ """'), True
        elif body and line.strip() == '"""':
            closers, body = closers + 1, False
            line += "  /* c */ // end" if closers % 2 else "   "
        elif not body and any(helper.startswith(line + "\n") for helper in NF_HELPER_FUNCTIONS):
            line = "  " + line
        written.append(line)
    return {**files, NEXTFLOW_SCRIPT: "\n".join(written)}


@dataclass(frozen=True, slots=True)
class Layout:
    """A rewrite of the three files that changes only their layout, and why it does not change what they say."""

    name: str
    apply: Callable[[dict[str, str]], dict[str, str]]
    #: Why the rewrite is layout. Not a comment: it is the claim the property checks.
    rationale: str


#: Not layout, though it looks it: reordering main: (call order is step order), adding or removing a space the
#: renderer writes one or none of (it joins or splits a token), and anything in a string, script, stub body or helper.
LAYOUTS: tuple[Layout, ...] = (
    Layout("process order", _reordered,
           "Each process block is declared on its own and a call names it wherever it is declared; the order of "
           "the calls, which is the order of the steps, does not change."),
    Layout("indentation", _reindented,
           "Groovy reads a statement, a declaration or a script's opening quotes alike at any indentation."),
    Layout("space runs", _code_lines(lambda line: [_spaced(line)]),
           "Groovy separates two tokens by any run of spaces and tabs; outside a string, the run's length is not "
           "data."),
    Layout("blank lines", _code_lines(lambda line: [line, "", "  "]),
           "A line break ends a statement, and an empty line is no statement."),
    Layout("line comments", _code_lines(lambda line: ["// a comment, with 'quotes' and \"{\" in it", line + "  // c"]),
           "Groovy drops a `//` comment to the end of its line, outside a string."),
    Layout("block comments", _code_lines(lambda line: ["/* a block", "   comment */", line + " /* c */"]),
           "Groovy drops a block comment as it drops the space it stands in."),
    Layout("around a script's quotes", _around_quotes,
           "What comes before the quotes that open a script or stub, and after the quotes that close it, is outside "
           "its string, and Groovy reads a def at any indentation."),
    Layout("CR LF", _crlf,
           "Groovy reads a carriage return and a line feed as a line feed, even inside a triple-quoted script "
           "(design §4.2, probed on the pinned Nextflow under the v2 parser)."),
    Layout("params JSON", _json,
           "`nextflow_params.json` is read as JSON, in which whitespace, key order and escapes are not data."),
    Layout("config", _config,
           "`nextflow.config` is Groovy: its comments, blank lines and space runs are layout as in `workflow.nf`."),
)


@budget(10)
@given(chosen=st.sets(st.sampled_from(LAYOUTS)))
@example(chosen=set(LAYOUTS))
def test_layout_leaves_the_import_unchanged(chosen: set[Layout]) -> None:
    """Any of the rewrites above, alone or together, applied to the pipeline of each example: the import is the model
    the pipeline was written from. Every pipeline is rewritten for every set drawn, and for all the rewrites at once."""
    for name in RUNS:
        files = _texts(name)
        # In the order above: the process blocks are found by their unchanged layout, so they move first.
        for layout in LAYOUTS:
            files = layout.apply(files) if layout in chosen else files
        with tempfile.TemporaryDirectory() as scratch:
            for file, text in files.items():
                (Path(scratch) / file).write_bytes(text.encode("utf-8"))
            assert _imported(Path(scratch)) == _model(name), name


_CODE = SophiosErrorCode
_DEEP = 3000  # nested deeper than Python's default recursion limit, 1000
_NF, _PARAMS = NEXTFLOW_SCRIPT, NEXTFLOW_PARAMS
#: An edit: the code the import stops with, the file edited, the text replaced wherever it occurs (or the texts, each
#: by its own), its replacement, and text on the line the import stops at.
Edit = tuple[SophiosErrorCode, str, str | tuple[str, ...], str | tuple[str, ...], str]
#: Edits of a pipeline, by the pipeline: an example's, or the one-step pipeline `import_edited` writes (None). One
#: edit per code; then text Sophios never writes, each where a check that no other edit reaches reads it.
EDITS: dict[str, tuple[str | None, list[Edit]]] = {
    "one per code": (None, [(code, *edit) for code, edit in IMPORT_EDITS.items()]),
    "one step": (None, [
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    greet(message)\n", "    greet(message.view())\n", ".view()"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    greet(message)\n", "    greet(message)\n    other(message)\n",
         "other("),
        # A second block of a process, which nothing else would read, and a process after a workflow.
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "\nworkflow provoke {\n",
         '\nprocess greet {\n    script:\n    """\n    rm o\n    """\n}\n\nworkflow provoke {\n', "process greet"),
        (_CODE.IMPORT_MISMATCH, _NF, "\nworkflow provoke {\n",
         "\nworkflow first {\n}\n\nprocess other {\n}\n\nworkflow provoke {\n", "process other"),
        # A lone carriage return ends no line; after a script's closing quotes, a comment is layout but code is not.
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    input:\n", "    input:\r", "input:"),
        (_CODE.IMPORT_FOREIGN_DECLARATION, _NF, '    """\n\n    stub:', '    """ x\n\n    stub:', " x"),
        # The stub is Sophios's, word for word; a command on one line holds no shell literal, whose `;` would end it.
        (_CODE.IMPORT_FOREIGN_SCRIPT, _NF, "    exit 1\n", "    exit 0\n", "stub:"),
        (_CODE.IMPORT_MISMATCH, _NF, "('echo')} ", "('echo')} ; ", "('echo')} ;"),
        (_CODE.IMPORT_FOREIGN_SCRIPT, _NF, "${__sophios_shell_quote_9f72e('echo')}",
         "${" + "(" * _DEEP + "__sophios_shell_quote_9f72e('echo')" + ")" * _DEEP + "}", "${((("),
        # The compile renames a capitalized workflow and refuses an empty command word; the tool builder keeps the
        # port name `keys` for a method; a parameter's value is never null.
        (_CODE.IMPORT_UNWRITABLE, _NF, "provoke", "Provoke", "workflow Provoke"),
        (_CODE.IMPORT_UNWRITABLE, _NF, "('echo')", "('')", "workflow provoke"),
        (_CODE.IMPORT_UNWRITABLE, _NF, ("    val message\n", "(message.toString())"),
         ("    val keys\n", "(keys.toString())"), "process greet"),
        (_CODE.IMPORT_PARAMS, _PARAMS, '"hi"', "null", '"message": null'),
        # Lines of tens of KB that nearly match a form with several holes: one after a merge, which the reader tries
        # as the merge's next step, and one in `main:`, where it tries every statement as a scatter's shape.
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    greet(message)\n",
         "    ch_greet_in = a.merge(b) { __w0, __w1 -> tuple(__w0, __w1) }\n        .merge("
         + "a) { __merged, b -> tuple(" * 1000 + "}" * 1000 + "\n    greet(message)\n", ".merge(a) { __merged"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    greet(message)\n",
         "    ch_greet_shape = " + "a.map { b -> [[" * 1000 + "}" * 1000 + "\n    greet(message)\n",
         "ch_greet_shape = a.map"),
    ]),
    "commands": ("commands", [
        (_CODE.IMPORT_FOREIGN_DECLARATION, _NF, 'path "${filename}"',
         'path "${' + "(" * _DEEP + "filename" + ")" * _DEEP + '}"', 'path "${((('),
        (_CODE.IMPORT_FOREIGN_SCRIPT, _NF, "('hello  there')", "('\\u0000')", "export GREETING="),
        # The command of a parenthesized script is on the line after `(`, and its redirects on the `)` line.
        (_CODE.IMPORT_FOREIGN_SCRIPT, _NF, ") > ${", ") >> ${", ") >>"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "params.emit_message, 'string')", "params.emit_message, 'strin')",
         "'strin'"),
        # A float parameter's value is its text, "2.5": never a number, nor text that is no finite float.
        *((_CODE.IMPORT_PARAMS, _PARAMS, '"print_bindings_scale": "2.5"', f'"print_bindings_scale": {value}',
           f'"print_bindings_scale": {value}') for value in ("2", '"x"', '"1e999"')),
    ]),
    "resources": ("resources", [
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "    cpus 2\n", "    container ''\n    cpus 2\n", "container ''"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, 'memory "64 MB"', 'memory "0 MB"', 'memory "0 MB"'),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "cudaVersionMin: '11.4'", "cudaVersionMin: ['11.4']",
         "cudaVersionMin: ['11.4']"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "Directory: ['result']", "Directory: [result]", "Directory: [result]"),
    ]),
    "conditions_and_scatter": ("conditions_and_scatter", [
        # A string outside the subset; a channel no statement makes; a carrier names exactly the last val inputs.
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "(__w0 != 'b')", "(__w0 != 'é')", "'é'"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "= pair_unless_b_first.map", "= nope.map", "nope.map"),
        (_CODE.IMPORT_MISMATCH, _NF, "WorkflowStepInput: ['enabled']", "WorkflowStepInput: ['go']",
         "WorkflowStepInput"),
    ]),
    "calculator": ("calculator", [
        # A condition nested deeper than Python recurses; a number literal that is not finite; an operand, a check's
        # parts and a check's value each as no compile writes them.
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "run: (Double.valueOf('3') == Double.valueOf('3'))",
         "run: " + "(!" * _DEEP + "(Double.valueOf('3') == Double.valueOf('3'))" + ")" * _DEEP, "run: (!(!"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "run: (Double.valueOf('3') == Double.valueOf('3'))",
         "run: (Double.valueOf('3') == Double.valueOf('1e999'))", "Double.valueOf('1e999')"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "run: (Double.valueOf('3') == Double.valueOf('3'))",
         "run: (Double.valueOf('3') == three)", "three)"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "7.5)', '(inputs.i + 0.5)'", "7.5)'; '(inputs.i + 0.5)'",
         "; '(inputs.i + 0.5)'"),
        (_CODE.IMPORT_FOREIGN_STATEMENT, _NF, "__sophios_round_9f72e(Double.valueOf('1e19'))",
         "Double.valueOf('1e19')", "Double.valueOf('1e19'), 'c12"),
    ]),
    "staging": ("staging", [
        (_CODE.IMPORT_FOREIGN_SCRIPT, _NF, "('renamed.txt')}\n", "('a/b')}\n", "('a/b')"),
    ]),
}


def _stops(code: SophiosErrorCode, path: Path, at: str, importing: Callable[[], object]) -> None:
    """`importing` stops with `code` at a line of `path` that holds `at`, which its message names."""
    with pytest.raises(SophiosError) as caught:
        importing()
    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.code is code, (at, diagnostic)
    assert diagnostic.span is not None and Path(diagnostic.span.file) == path, (at, diagnostic)
    assert at in path.read_bytes().decode("utf-8", "surrogateescape").split("\n")[diagnostic.span.start_line - 1]
    assert at in diagnostic.message, diagnostic


def _edited(directory: Path, pipeline: str | None, file: str, old: str | tuple[str, ...],
            new: str | tuple[str, ...]) -> Workflow:
    """The files `_texts(pipeline)`, each `old` replaced by its `new` in `file`, written into `directory`, imported."""
    olds, news = (old,) if isinstance(old, str) else old, (new,) if isinstance(new, str) else new
    for name, text in _texts(pipeline).items():
        if name == file:
            for replaced, replacement in zip(olds, news, strict=True):
                assert replaced in text, replaced
                text = text.replace(replaced, replacement)
        (directory / name).write_text(text, encoding="utf-8")
    return import_nextflow(directory)


@pytest.mark.parametrize("pipeline, edits", EDITS.values(), ids=EDITS)
def test_an_edit_outside_the_subset_stops_the_import_at_its_line(
        pipeline: str | None, edits: list[Edit], tmp_path: Path) -> None:
    """Each edit stops the import with its code, at a line that holds the text the edit names (`_stops`), and within
    seconds: each hole of a form is read once, so a line of tens of KB that nearly matches one is read in linear
    time."""
    for index, (code, file, old, new, at) in enumerate(edits):
        directory = tmp_path / str(index)
        directory.mkdir()
        started = time.perf_counter()
        _stops(code, directory / file, at, partial(_edited, directory, pipeline, file, old, new))
        assert time.perf_counter() - started < 5.0, at


def _unread(tmp_path: Path, name: str) -> Path:
    """A one-step pipeline holding a parameter no step reads, named `name` in all three files."""
    step = _echo("greet", message=Input(cwl.string, position=1)).to_step()
    workflow = Workflow([step], "unread")
    step.inputs.message = "hi"
    workflow.inputs.placeholder.as_type("string")  # pylint: disable=no-member
    workflow.inputs.placeholder = "kept"
    workflow.outputs.out = step.outputs.out
    workflow.compile(target="nextflow").write(tmp_path)
    for file in _FILES:
        path = tmp_path / file
        path.write_text(path.read_text(encoding="utf-8").replace("placeholder", name), encoding="utf-8")
    return tmp_path


#: Words the pipeline `_unread` writes, which a parameter's name would be read as.
_WRITTEN = re.compile(r"\w+")


@budget(30)
@given(st.sampled_from(sorted(dir(ParameterNamespace))) | st.from_regex(r"_{0,2}[a-z][a-z0-9_]{0,5}", fullmatch=True))
@example("keys")
@example("_items")
@example("_store")
@example("__x__")
def test_a_parameter_imports_exactly_when_a_python_workflow_can_take_its_input(name: str) -> None:
    """A parameter is a workflow input: a pipeline whose parameter is renamed `name` imports exactly when
    `flow.inputs.<name> = value` works. An attribute of the port namespace, such as `_store`, is read as itself, so it
    stops the import at its workflow, not with an AttributeError; `keys` and `_items` import."""
    with tempfile.TemporaryDirectory() as scratch:
        directory = _unread(Path(scratch) / "written", "placeholder")
        written = set(_WRITTEN.findall("".join((directory / file).read_text(encoding="utf-8") for file in _FILES)))
        if name in written or name == "placeholder":
            return
        directory = _unread(Path(scratch) / "renamed", name)
        try:
            setattr(Workflow([], "python").inputs, name, "kept")
        except ValueError:
            _stops(_CODE.IMPORT_UNWRITABLE, directory / NEXTFLOW_SCRIPT, "workflow unread",
                   lambda: import_nextflow(directory))
        else:
            assert name in [port.name for port in import_nextflow(directory).inputs]


@st.composite
def _corrupted(draw: st.DrawFn) -> tuple[str, str, bytes]:
    """The example of step inputs or of commands, or thirty conditions, with one of its three files cut short or
    holding a non-UTF-8 byte, an integer longer than Python converts, or nesting deeper than it recurses."""
    name = draw(st.sampled_from(["step_inputs", "commands", "calculator"]))
    file = draw(st.sampled_from(_FILES))
    data = _texts(name)[file].encode("utf-8")
    kind = draw(st.sampled_from(["cut", "byte", "digits", "nesting"]))
    if kind == "cut":
        return name, file, data[:draw(st.integers(0, len(data) - 1))]
    if kind == "nesting":
        at = draw(st.sampled_from([0, *(index + 1 for index, byte in enumerate(data) if byte in b"([{'\"")]))
        opener, closer = draw(st.sampled_from([(b"(", b""), (b"(", b")"), (b"[", b"]"), (b"{", b"}"), (b"${", b"}"),
                                               (b"(!", b")")]))
        return name, file, data[:at] + opener * _DEEP + closer * _DEEP + data[at:]
    at = draw(st.integers(0, len(data)))
    inserted = b"9" * 5000 if kind == "digits" else draw(st.sampled_from([b"\xff", b"\xc3", b"\xed\xa0\x80"]))
    return name, file, data[:at] + inserted + data[at:]


@budget(300)
@given(_corrupted())
def test_whatever_bytes_the_files_hold_the_read_returns_a_model_or_stops_with_a_code(
        corrupted: tuple[str, str, bytes]) -> None:
    """The read returns a model or raises SophiosError or OSError, as its docstring says, and nothing else."""
    name, file, data = corrupted
    with tempfile.TemporaryDirectory() as directory:
        for each, text in _texts(name).items():
            (Path(directory) / each).write_bytes(data if each == file else text.encode("utf-8"))
        with suppress(SophiosError, OSError):
            read_nextflow(directory)


def test_a_minimum_of_more_arguments_than_python_recurses_imports(tmp_path: Path) -> None:
    """The renderer folds a Math.min into one call per argument after the first, which the reader counts."""
    step = _echo("least", n=Input(cwl.int, position=1)).to_step()
    workflow = Workflow([step], "least")
    step.inputs.n, step.when = 2, "$(Math.min(" + ", ".join(["inputs.n"] * _DEEP) + ") > 1)"
    workflow.outputs.out = step.outputs.out
    compiled = workflow.compile(target="nextflow")
    assert _imported(compiled.write(tmp_path)) == compiled.nextflow_workflow


def test_a_condition_too_deep_to_render_stops_the_import_at_its_workflow(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Python 3.11 recurses deeper rendering nested calls than reading them, so a condition its run: line reads
    may be too deep to render: the import stops at the workflow."""
    def too_deep(_model: ExecutableNextflowWorkflow) -> str:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("sophios.nf_reader.render_nextflow", too_deep)
    _stops(_CODE.IMPORT_FOREIGN_STATEMENT, tmp_path / NEXTFLOW_SCRIPT, "workflow provoke",
           partial(_edited, tmp_path, None, NEXTFLOW_SCRIPT, "", ""))
