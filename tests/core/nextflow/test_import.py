"""The import is the renderer's exact inverse over the workflows Sophios compiles (design §4.2).

The fixed point. For each workflow W in CASES, M is W compiled to
Nextflow; the three files written from M import to a workflow that compiles,
with inference disabled, to M. Model equality implies equal text and also sees
the fields that never reach it. The domain is the image of compilation: the
workflows built here through the Python API, the real-world examples, the
Phase 3 calculator and predicate cells, which hold every arithmetic, ``Math``
and comparison form a condition reads, and the one workflow in examples/ the
target compiles. Every form with holes in the idiom table is written or read
for at least one of them, and each of its holes is read once.

Layout. Each rewrite in LAYOUTS changes only what design §4.2 calls
layout, with its reason, and the import of the rewritten files compiles to M.
Deliberately NOT on the list, though they look like layout: reordering the
statements of ``main:``, since the call order is the order of the steps, as
core keeps "reorder independent steps" off its list (tests/core/transformations.py);
adding a space where the renderer writes none, or removing the one it writes,
since that joins or splits a token; and anything inside a string literal, a
script or stub body, or a helper, which are content.

Rejection. One edit of generated text for each import code
(provocations.IMPORT_EDITS, which the provocation of each code applies too),
a few more, among them lines of tens of KB that nearly match a form, and a
value Sophios never writes on each kind of line that holds one: each stops
the import with its code at the line it names, not at the process or workflow
around it, and nothing is returned.

Hermetic. The import reads the three files alone: copied into an empty
directory, with no network, they import, and no path a parameter names is
looked at.
"""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

from hypothesis import given, settings, strategies as st
import pytest

from sophios.api.python._ports import ParameterNamespace
from sophios.api.python.nextflow import import_nextflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Inference, Step, StepInput, Workflow
from sophios.input_output_nf import (
    NEXTFLOW_CONFIG,
    NEXTFLOW_PARAMS,
    NEXTFLOW_SCRIPT,
    NF_HELPER_FUNCTIONS,
    Idiom,
    Idioms,
    write_nextflow_artifacts,
)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.nf_reader import read_nextflow
from sophios.nf_types import ExecutableNextflowWorkflow
from sophios.python_cwl_adapter import import_python_file

from ..hermetic import network_refused
from ..provocations import IMPORT_EDITS, import_edited
from .test_phase3_cells import calculator_workflow, predicates_workflow
from .test_real_world_examples import (flags_workflow, months_workflow, plan_workflow, summaries_workflow,
                                       tallies_workflow)
from .testkit import REPO_ROOT


def _echo(name: str, **inputs: Input) -> CommandLineTool:
    """A tool that echoes its inputs in order into `<name>.txt`."""
    return CommandLineTool(name, Inputs(**inputs), Outputs(out=Output(cwl.file, glob=f"{name}.txt"))).base_command(
        "echo").stdout(f"{name}.txt")


def _bindings() -> Workflow:
    """A flag, a prefixed array, a joined optional, both kinds of entry, a piped shell literal, variables, and
    braces in globs: escaped in a pattern, kept as written in a literal name."""
    inputs = Inputs(v=Input(cwl.boolean, position=1, flag="-v"),
                    xs=Input(cwl.array(cwl.string), position=2, flag="--x"),
                    name=Input(cwl.string, required=False, position=3, flag="--name=", separate=False),
                    src=Input(cwl.file), out=Input(cwl.directory))
    tool = (CommandLineTool("bind", inputs, Outputs(
        res=Output(cwl.directory, glob="$(inputs.out.basename)"),
        one=Output(cwl.file, glob="one.txt", output_eval="$(self[0])"),
        maybe=Output(cwl.file, glob="*.log", required=False), braced=Output(cwl.file, glob="a{b}.txt"),
        named=Output(cwl.file, glob="$(inputs.out.basename)\\\\{x}")))
        .base_command("prog").argument("2>&1 | tee", shell_quote=False, position=4)
        .argument("$(inputs.out.basename)", position=5).shell_command()
        .stage(inputs.src, entryname="renamed.txt").stage(inputs.out, writable=True)
        .stdout("o.txt").env_var("A", "lit $x").env_var("B", "$(inputs.name)"))
    step = tool.to_step()
    step.inputs.v, step.inputs.xs, step.inputs.src, step.inputs.out = True, ["p", "q"], "/data/s.txt", "/data/o"
    step.inputs.name = "n"
    workflow = Workflow([step], "bindings")
    workflow.outputs.res, workflow.outputs.one, workflow.outputs.maybe = (
        step.outputs.res, step.outputs.one, step.outputs.maybe)
    return workflow


def _crossproduct() -> Workflow:
    """A conditional flat crossproduct gathered into an array, a step input picking a value, a conditional step
    whose output's name holds a `_`, so the name of its channel, `ch_use_o_txt`, splits more than one way."""
    pair = _echo("pair", item=Input(cwl.string, position=1), tag=Input(cwl.string, position=2)).to_step()
    use = CommandLineTool("use", Inputs(files=Input(cwl.array(cwl.file), position=1), n=Input(cwl.int, position=2)),
                          Outputs(o_txt=Output(cwl.file, glob="o.txt"))).base_command("cat").stdout(
        "o.txt").to_step()
    join = _echo("join", words=Input(cwl.string, position=1)).to_step()
    workflow = Workflow([pair, use, join], "crossproduct")
    pair.inputs.item, pair.inputs.tag = ["i", "j"], ["1", "2"]
    pair.scatter_on(pair.inputs.item, pair.inputs.tag, method="flat_crossproduct")
    use.inputs.files = pair.outputs.out
    use.inputs.n = 3
    use.when = "$(inputs.n > 1)"
    join.inputs.words = StepInput(source=[workflow.inputs.a.as_type("string?"), workflow.inputs.b.as_type("string?")],
                                  link_merge="merge_nested", pick_value="first_non_null")
    workflow.outputs.o, workflow.outputs.j = use.outputs.o_txt, join.outputs.out
    return workflow


def _scatters(method: str) -> Callable[[], Workflow]:
    def build() -> Workflow:
        """A scatter over two inputs, an unscattered output into every task, and its outputs gathered."""
        first = _echo("first", word=Input(cwl.string, position=1)).to_step()
        pair = _echo("pair", a=Input(cwl.string, position=1), b=Input(cwl.int, position=2),
                     context=Input(cwl.file, position=3)).to_step()
        first.inputs.word = "context"
        pair.inputs.a, pair.inputs.b, pair.inputs.context = ["x", "y"], [1, 2], first.outputs.out
        pair.scatter_on(pair.inputs.a, pair.inputs.b, method=method)
        steps: list[Step] = [first, pair]
        if method != "nested_crossproduct":
            report = CommandLineTool("report", Inputs(files=Input(cwl.array(cwl.file), position=1)),
                                     Outputs(r=Output(cwl.file, glob="r.txt"))).base_command("cat").stdout("r.txt")
            steps.append(report.to_step())
            steps[-1].inputs.files = pair.outputs.out
        workflow = Workflow(steps, f"scatter_{method}")
        workflow.outputs.pairs = pair.outputs.out
        return workflow
    return build


def _resources() -> Workflow:
    """An image, cores, fractional memory, GPUs with a capability list, file text, and a float into a command."""
    tool = (CommandLineTool("res", Inputs(ratio=Input(cwl.float, position=1, flag="--ratio")),
                            Outputs(text=Output(cwl.string, glob="t.txt", load_contents=True,
                                                output_eval="$(self[0].contents)"),
                                    made=Output(cwl.directory, glob="made")))
            .base_command("run").docker("registry.io/a b:{1}'\"").resources(cores_min=2, ram_min=1.5)
            .gpu(cuda_version_min="11.7", device_count_min=2, extra={"cudaComputeCapability": ["7.0", "8.0"]}))
    step = tool.to_step()
    step.inputs.ratio = 0.1
    workflow = Workflow([step], "resources")
    workflow.outputs.text, workflow.outputs.made = step.outputs.text, step.outputs.made
    return workflow


def _directories() -> Workflow:
    """A Directory output into a Directory input, directly and gathered from a scatter."""
    make = CommandLineTool("make", Inputs(name=Input(cwl.string, position=1)),
                           Outputs(d=Output(cwl.directory, glob="$(inputs.name)"))).base_command("mkdir").to_step()
    each = CommandLineTool("each", Inputs(dir=Input(cwl.directory, position=1), n=Input(cwl.string, position=2)),
                           Outputs(d=Output(cwl.directory, glob="$(inputs.n)"))).base_command("cp", "-r").to_step()
    every = CommandLineTool("every", Inputs(dirs=Input(cwl.array(cwl.directory), position=1)),
                            Outputs(o=Output(cwl.file, glob="o.txt"))).base_command("ls").stdout("o.txt").to_step()
    make.inputs.name = "made"
    each.inputs.dir, each.inputs.n = make.outputs.d, ["a", "b"]
    each.scatter_on(each.inputs.n)
    every.inputs.dirs = each.outputs.d
    workflow = Workflow([make, each, every], "directories")
    workflow.outputs.o = every.outputs.o
    return workflow


def _unary_whens() -> Workflow:
    """Conditions of nested unary operators, whose canonical text is not a decrement, and of each spelling the
    Groovy text writes as another's (`===`, `!==`, a unary `+`, null on the left, `!(x == null)`), with the other."""
    whens = ("$(-(-inputs.x) > 0)", "$(- -inputs.x > 0)", "$(+(+inputs.x) > 0)", "$(-(-3) > inputs.x)",
             "$(inputs.s === 'it\"s' && !(inputs.o == null))", "$(inputs.s !== 'x' && null != inputs.o)")
    steps = [_echo(f"w{index}", x=Input(cwl.int, position=1), s=Input(cwl.string, position=2),
                   o=Input(cwl.string, required=False)).to_step() for index in range(len(whens))]
    workflow = Workflow(steps, "whens")
    for step, when in zip(steps, whens):
        step.inputs.x, step.inputs.s, step.inputs.o = 2, "it\"s", workflow.inputs.o.as_type("string?")
        step.when = when
        setattr(workflow.outputs, step.process_name, step.outputs.out)
    return workflow


def _streams() -> Workflow:
    """Each stream redirected, a shell literal `>` that is not one, and two adjacent shell literals, which the model
    merges."""
    redirected = CommandLineTool("streams", Inputs(src=Input(cwl.file)), Outputs(
        out=Output(cwl.file, glob="out.txt"), err=Output(cwl.file, glob="err.txt"))).base_command("cat").stdin(
        "$(inputs.src.path)").stdout("out.txt").stderr("err.txt")
    literal = CommandLineTool("literal", Inputs(), Outputs(o=Output(cwl.file, glob="o.txt"))).base_command(
        "echo").argument(">", shell_quote=False, position=1).argument("o.txt", position=2).argument(
        "&&", shell_quote=False, position=3).argument("true", shell_quote=False, position=4).shell_command()
    first, second = redirected.to_step(), literal.to_step()
    first.inputs.src = "/data/in.txt"
    workflow = Workflow([first, second], "streams")
    workflow.outputs.out, workflow.outputs.err, workflow.outputs.o = (
        first.outputs.out, first.outputs.err, second.outputs.o)
    return workflow


def _merged() -> Workflow:
    """Two outputs merged flat into one array input."""
    one, two = (_echo(name, w=Input(cwl.string, position=1)).to_step() for name in ("one", "two"))
    cat = CommandLineTool("cat", Inputs(files=Input(cwl.array(cwl.file), position=1)),
                          Outputs(o=Output(cwl.file, glob="o.txt"))).base_command("cat").stdout("o.txt").to_step()
    one.inputs.w, two.inputs.w = "1", "2"
    workflow = Workflow([one, two, cat], "merged")
    cat.inputs.files = StepInput(source=[one.outputs.out, two.outputs.out], link_merge="merge_flattened")
    workflow.outputs.o = cat.outputs.o
    return workflow


def _valued() -> Workflow:
    """Values bound on workflow inputs: text with what CWL and Groovy escape, ints, files, an empty optional array;
    one input feeds two steps and one none. A required int is given to an optional binding, and an optional int
    that has a value to a condition, which holds each tool input to its own optionality. A literal `$(` in an
    argument after a reference is written escaped, and a pattern glob's brace is escaped for Nextflow."""
    count = CommandLineTool("count", Inputs(
        word=Input(cwl.string, position=1), n=Input(cwl.int, required=False, position=2, flag="--n"),
        files=Input(cwl.array(cwl.file), position=3), main=Input(cwl.file, position=4, flag="--in=", separate=False),
        tag=Input(cwl.string, position=5, flag=" --tag=", separate=False),
        extras=Input(cwl.array(cwl.string), position=6, flag="--extra")),
        Outputs(o=Output(cwl.file, glob="$(inputs.word).txt"), braced=Output(cwl.file, glob="$(inputs.word){x}.txt"))
    ).base_command("wc", "$(not) \\${x}").argument("a \\$(b) \\\\ c", position=7).stdout(
        "$(inputs.word).txt").to_step()
    again = _echo("again", word=Input(cwl.string, position=1), k=Input(cwl.int)).to_step()
    again.when = "$(inputs.k > 1)"
    workflow = Workflow([count, again], "valued")
    word = workflow.inputs.word.as_type("string")
    count.inputs.word, count.inputs.n, again.inputs.word = word, workflow.inputs.n.as_type("int"), word
    count.inputs.files, count.inputs.main = workflow.inputs.files.as_type("File[]"), "/data/main.txt"
    count.inputs.tag, again.inputs.k = "t", workflow.inputs.k.as_type("int?")
    count.inputs.extras = workflow.inputs.extras.as_type("string[]?")
    workflow.inputs.unread.as_type("string")
    workflow.inputs.word, workflow.inputs.n, workflow.inputs.k, workflow.inputs.unread = "w'\"$x", 3, 2, "kept"
    workflow.inputs.files, workflow.inputs.extras = ["/data/a.txt", {"class": "File", "path": "/data/b.txt"}], []
    workflow.outputs.o, workflow.outputs.again = count.outputs.o, again.outputs.out
    return workflow


def _renamed() -> Workflow:
    """Names normalization gives a leading `_`, which the import keeps: a capitalized step with a value written on
    it, a capitalized tool input, workflow input and workflow output, and a reserved step name."""
    fetch = _echo("Fetch", word=Input(cwl.string, position=1), N=Input(cwl.int, position=2)).to_step()
    task = _echo("task", word=Input(cwl.string, position=1)).to_step()
    workflow = Workflow([fetch, task], "renamed")
    fetch.inputs.word, fetch.inputs.N, task.inputs.word = "x", workflow.inputs.X.as_type("int"), "y"
    workflow.inputs.X = 1
    workflow.outputs.Out, workflow.outputs.t = fetch.outputs.out, task.outputs.out
    return workflow


def _nested() -> Workflow:
    """A subworkflow, inlined, whose input comes from an outer step."""
    write = _echo("write", message=Input(cwl.string, position=1)).to_step()
    write.inputs.message = "nested composition"
    copy = CommandLineTool("copy_file", Inputs(source=Input(cwl.file, position=1)),
                           Outputs(result=Output(cwl.file, glob="copy.txt"))).base_command("cp").argument(
        "copy.txt", position=2).to_step(step_name="inner_copy")
    child = Workflow([copy], "child")
    copy.inputs.source = child.inputs.source
    child.inputs.source = write.outputs.out
    child.outputs.result = copy.outputs.result
    workflow = Workflow([write, child], "root")
    workflow.outputs.copied = child.outputs.result
    return workflow


def _conditions() -> Workflow:
    """A condition on a step of one input, which branches that input's channel, and on a scattered step, which
    branches the scatter combined with its other inputs, one of them an optional File with a value."""
    one = _echo("one", n=Input(cwl.int, position=1)).to_step()
    each = _echo("each", word=Input(cwl.string, position=1), n=Input(cwl.int, position=2),
                 f=Input(cwl.file, required=False)).to_step()
    workflow = Workflow([one, each], "conditions")
    one.inputs.n, one.when = 2, "$(inputs.n > 1)"
    each.inputs.word, each.inputs.n, each.inputs.f = ["a", "b"], 3, workflow.inputs.f.as_type("File?")
    workflow.inputs.f = "/data/f.txt"
    each.scatter_on(each.inputs.word)
    each.when = "$(inputs.n > 2)"
    workflow.outputs.one, workflow.outputs.each = one.outputs.out, each.outputs.out
    return workflow


def _example(name: str) -> Callable[[], Workflow]:
    def build() -> Workflow:
        path = REPO_ROOT / "examples" / "scripts" / f"{name}.py"
        workflow: Workflow = import_python_file(name, path).workflow()
        return workflow
    return build


CASES: dict[str, Callable[[], Workflow]] = {
    "bindings": _bindings, "crossproduct": _crossproduct, "dotproduct": _scatters("dotproduct"),
    "flat_crossproduct": _scatters("flat_crossproduct"), "nested_crossproduct": _scatters("nested_crossproduct"),
    "resources": _resources, "directories": _directories, "unary_whens": _unary_whens, "streams": _streams,
    "merged": _merged, "valued": _valued, "renamed": _renamed, "nested": _nested, "conditions": _conditions,
    # The one workflow in examples/ the Nextflow target compiles.
    "helloworld_pyapi": _example("helloworld_pyapi"),
    "plan": plan_workflow, "months": lambda: months_workflow(Path("/data")), "summaries": summaries_workflow,
    "tallies": tallies_workflow, "flags": flags_workflow,
    "calculator": calculator_workflow, "predicates": predicates_workflow,
}
_FILES = (NEXTFLOW_SCRIPT, NEXTFLOW_PARAMS, NEXTFLOW_CONFIG)


def _compiled(name: str) -> ExecutableNextflowWorkflow:
    return CASES[name]().compile(target="nextflow")


def _imported(directory: Path) -> ExecutableNextflowWorkflow:
    return import_nextflow(directory).compile(target="nextflow", inference=Inference(enabled=False))


@pytest.mark.parametrize("name", CASES)
def test_the_import_of_a_compiled_pipeline_compiles_to_its_model(name: str, tmp_path: Path) -> None:
    model = _compiled(name)
    write_nextflow_artifacts(model, tmp_path)
    assert _imported(tmp_path) == model


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
    """One tool with inputs of each kind, each value bound on the step or on a workflow input or the default of the
    tool or of the step input, given to the command by their own bindings and by arguments among literal words and
    shell text, in a container or with resources."""
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


@settings(max_examples=100, deadline=None)
@given(_generated())
def test_the_import_of_a_generated_tool_compiles_to_its_model(workflow: Workflow) -> None:
    model = workflow.compile(target="nextflow")
    with tempfile.TemporaryDirectory() as directory:
        write_nextflow_artifacts(model, directory)
        assert _imported(Path(directory)) == model


#: The forms the reader reads by filling them and looking up what it read, as `ch_{process}_{emit}` splits more
#: than one way: the reader matches every other form.
_LOOKED_UP = (Idioms.PROCESS_OUTPUT, Idioms.CONDITIONAL_OUTPUT)


def test_every_idiom_is_written_and_read_for_some_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """For some case, the renderer fills each form with holes in the idiom table, and the reader matches it."""
    used: set[Idiom] = set()

    def recording(method: Callable[..., Any]) -> Callable[..., Any]:
        def record(idiom: Idiom, *args: Any, **holes: Any) -> Any:
            if found := method(idiom, *args, **holes):
                used.add(idiom)
            return found
        return record

    with monkeypatch.context() as patch:
        patch.setattr(Idiom, "fill", recording(Idiom.fill))
        for name in CASES:
            write_nextflow_artifacts(_compiled(name), tmp_path / name)
    filled = set(used)
    used.clear()
    with monkeypatch.context() as patch:
        patch.setattr(Idiom, "match", recording(Idiom.match))
        patch.setattr(Idiom, "find", recording(Idiom.find))
        for name in CASES:
            read_nextflow(tmp_path / name)
    idioms = {name: idiom for name, idiom in vars(Idioms).items() if isinstance(idiom, Idiom)}
    assert [name for name, idiom in idioms.items() if idiom not in filled] == []
    assert [name for name, idiom in idioms.items() if idiom not in used and idiom not in _LOOKED_UP] == []


def test_every_hole_is_read_once() -> None:
    """Each hole of each form, named the first time or again, opens an atomic group that ends where the next hole's
    opens: matching never goes back into a hole it has read, so it is linear in the length of the line."""
    patterns = {name: idiom.pattern.pattern for name, idiom in vars(Idioms).items() if isinstance(idiom, Idiom)}
    assert [name for name, pattern in patterns.items() if pattern.count("(?P") != pattern.count("(?>(?P")] == []


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


@dataclass(frozen=True, slots=True)
class Layout:
    """A rewrite of the three files that changes only their layout, and why it does not change what they say."""

    name: str
    apply: Callable[[dict[str, str]], dict[str, str]]
    #: Why the rewrite is layout. Not a comment: it is the claim the property checks.
    rationale: str


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
    Layout("CR LF", _crlf,
           "Groovy reads a carriage return and a line feed as a line feed, even inside a triple-quoted script "
           "(design §4.2, probed on the pinned Nextflow under the v2 parser)."),
    Layout("params JSON", _json,
           "`nextflow_params.json` is read as JSON, in which whitespace, key order and escapes are not data."),
    Layout("config", _config,
           "`nextflow.config` is Groovy: its comments, blank lines and space runs are layout as in `workflow.nf`."),
)


def _everything(files: dict[str, str], layouts: tuple[Layout, ...] = LAYOUTS) -> dict[str, str]:
    # The process blocks are found by their unchanged layout, so they move first.
    for layout in layouts:
        files = layout.apply(files)
    return files


LAYOUTS += (Layout("all at once", _everything, "Each rewrite above is layout, so their composition is."),)


@pytest.mark.parametrize("layout", LAYOUTS, ids=[layout.name for layout in LAYOUTS])
@pytest.mark.parametrize("name", CASES)
def test_layout_leaves_the_import_unchanged(layout: Layout, name: str, tmp_path: Path) -> None:
    model = _compiled(name)
    write_nextflow_artifacts(model, tmp_path)
    files = {file: (tmp_path / file).read_text(encoding="utf-8") for file in _FILES}
    for file, text in layout.apply(files).items():
        (tmp_path / file).write_bytes(text.encode("utf-8"))
    assert _imported(tmp_path) == model


_CODE = SophiosErrorCode
# More edits than one per code: the constructs the import is most often handed.
_EDITS = [
    *((code, *edit) for code, edit in IMPORT_EDITS.items()),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "    greet(message)\n", "    greet(message.view())\n", ".view()"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "nextflow.enable.dsl=2\n",
     "nextflow.enable.dsl=2\ninclude { X } from './x'\n", "include"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "\nworkflow {\n",
     "\nworkflow.onComplete { println('done') }\nworkflow {\n", "onComplete"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "    input:\n", "    cpus params.threads\n    input:\n", "cpus"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "    greet(message)\n", "    greet(message)\n    other(message)\n",
     "other("),
    # A second block of a process, which nothing else would read.
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "\nworkflow provoke {\n",
     '\nprocess greet {\n    script:\n    """\n    rm o\n    """\n}\n\nworkflow provoke {\n', "process greet"),
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, "    exit 1\n", "    exit 0\n", "stub:"),
    (_CODE.IMPORT_MISMATCH, NEXTFLOW_SCRIPT, "    val message\n", "    val message, arity: '1'\n", "arity"),
    # Text after a script's opening quotes is in the script, quotes open one only after `script:` or `stub:`, and a
    # lone carriage return ends no line.
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, '    script:\n    """\n', '    script:\n    """ \n', '"""'),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "    input:\n", '    input:\n    """\n', '"""'),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "    input:\n", "    input:\r", "input:"),
    # A value the compile writes otherwise, as a text parameter's 3 is "3", or not at all.
    (_CODE.IMPORT_PARAMS, NEXTFLOW_PARAMS, '"hi"', "3", '"message": 3'),
    (_CODE.IMPORT_PARAMS, NEXTFLOW_PARAMS, '"hi"', "null", '"message": null'),
    # The compile renames a capitalized workflow, and refuses an empty command word.
    (_CODE.IMPORT_UNWRITABLE, NEXTFLOW_SCRIPT, "provoke", "Provoke", "workflow Provoke"),
    (_CODE.IMPORT_UNWRITABLE, NEXTFLOW_SCRIPT, "('echo')", "('')", "workflow provoke"),
]
# A value Sophios never writes, on each kind of line that holds one, in a case whose pipeline has that line: the
# import stops at that line, not at the process or workflow the line is in.
_VALUE_EDITS = [
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "container 'registry.io/a b:{1}\\'\"'", "container ''",
     "container ''", "resources"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "cpus 2", "cpus 0", "cpus 0", "resources"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, 'memory "1.5 MB"', 'memory "0 MB"', 'memory "0 MB"',
     "resources"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "cudaVersionMin: '11.7'", "cudaVersionMin: ['11.7']",
     "cudaVersionMin: ['11.7']", "resources"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "Directory: ['made']", "Directory: [made]", "Directory: [made]",
     "resources"),
    (_CODE.IMPORT_FOREIGN_DECLARATION, NEXTFLOW_SCRIPT, 'path "${word}.txt"', 'path "${word.size()}.txt"',
     "word.size()", "valued"),
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, "('wc')", "('\\u0000')", "('\\u0000')", "valued"),
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, "('renamed.txt')}\n", "('a/b')}\n", "('a/b')", "bindings"),
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, "('lit $x')", "('\\u0000')", "export A=", "bindings"),
    # The command of a parenthesized script is on the line after `(`, and its redirects on the `)` line.
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, "('prog')", "('\\u0000')", "('\\u0000')", "bindings"),
    (_CODE.IMPORT_FOREIGN_SCRIPT, NEXTFLOW_SCRIPT, ") > ${", ") >> ${", ") >>", "bindings"),
    # A number that is not a finite decimal, a string outside the subset, and a condition that is not a boolean.
    *((_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "Double.valueOf('1')", f"Double.valueOf({number})",
       f"Double.valueOf({number})", "valued") for number in ("'0x1'", "'abc'", "'1e999'")),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "(__w1 == 'it\"s')", "(__w1 == 'é')", "'é'", "unary_whens"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "run: (((__w1) as double) > Double.valueOf('1'))",
     "run: Double.valueOf('1')", "run: Double.valueOf('1')", "valued"),
    # A channel no statement makes, where a merge, a scatter or a branch reads it.
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "word.merge(k)", "word.merge(nope)", "merge(nope)", "valued"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, ".merge(o) {", ".merge(nope) {", ".merge(nope)", "unary_whens"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "= pair_a.map", "= nope.map", "nope.map", "dotproduct"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "= one_1_n.branch", "= nope.branch", "nope.branch", "conditions"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, ".combine(each_2_n.map", ".combine(nope.map", ".combine(nope",
     "conditions"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "'merge_nested', 'first_non_null')", "'bogus', 'first_non_null')",
     "'bogus'", "crossproduct"),
    (_CODE.IMPORT_FOREIGN_STATEMENT, NEXTFLOW_SCRIPT, "params.word, 'string')", "params.word, 'strin')", "'strin'",
     "valued"),
    # A key another value names, and an absent optional File, which has no lowering.
    (_CODE.IMPORT_PARAMS, NEXTFLOW_PARAMS, '"a": [],\n  "b": []', '"a": "b",\n  "b": 3', '"b": 3', "crossproduct"),
    (_CODE.IMPORT_PARAMS, NEXTFLOW_PARAMS, '"f": {\n    "class": "File",\n    "path": "/data/f.txt"\n  }', '"f": []',
     '"f": []', "conditions"),
]
_ALL_EDITS = [*((*edit, None) for edit in _EDITS), *_VALUE_EDITS]


def test_the_unedited_pipeline_imports(tmp_path: Path) -> None:
    """The pipeline every edit below starts from is one the import reads."""
    assert isinstance(import_edited(str(tmp_path), NEXTFLOW_SCRIPT, "", ""), Workflow)


def _stops(code: SophiosErrorCode, path: Path, at: str, importing: Callable[[], object]) -> None:
    """`importing` stops with `code` at a line of `path` that holds `at`, which its message names."""
    with pytest.raises(SophiosError) as caught:
        importing()
    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.code is code, diagnostic
    assert diagnostic.span is not None and Path(diagnostic.span.file) == path
    assert at in path.read_bytes().decode("utf-8").split("\n")[diagnostic.span.start_line - 1]
    assert at in diagnostic.message


@pytest.mark.parametrize("code, file, old, new, at, case", _ALL_EDITS, ids=[
    f"{edit[0].name}-{edit[4]!r}" + (f"-{edit[5]}" if edit[5] else "") for edit in _ALL_EDITS])
def test_an_edit_outside_the_subset_stops_the_import_at_its_line(
        code: SophiosErrorCode, file: str, old: str, new: str, at: str, case: str | None, tmp_path: Path) -> None:
    workflow = CASES[case]() if case else None
    _stops(code, tmp_path / file, at, lambda: import_edited(str(tmp_path), file, old, new, workflow))


# Lines of tens of KB that nearly match a form with several holes: one after a merge, which the reader tries as the
# merge's next step, and one in `main:`, where it tries every statement as a scatter's shape.
_NEAR_MISSES = {
    "merge": ("    ch_greet_in = a.merge(b) { __w0, __w1 -> tuple(__w0, __w1) }\n        .merge("
              + "a) { __merged, b -> tuple(" * 1000 + "}" * 1000 + "\n", ".merge(a) { __merged"),
    "shape": ("    ch_greet_shape = " + "a.map { b -> [[" * 1000 + "}" * 1000 + "\n", "ch_greet_shape = a.map"),
}


@pytest.mark.parametrize("lines, at", _NEAR_MISSES.values(), ids=list(_NEAR_MISSES))
def test_a_long_near_miss_stops_the_import_at_its_line(lines: str, at: str, tmp_path: Path) -> None:
    """A line of tens of KB that nearly matches a form stops the import at its line, as other text Sophios does not
    write does, and within seconds: each hole is read once, so the time is linear in the line's length."""
    call = "    greet(message)\n"
    started = time.perf_counter()
    _stops(_CODE.IMPORT_FOREIGN_STATEMENT, tmp_path / NEXTFLOW_SCRIPT, at,
           lambda: import_edited(str(tmp_path), NEXTFLOW_SCRIPT, call, lines + call))
    assert time.perf_counter() - started < 5.0


def test_an_int_for_a_float_parameter_stops_the_import(tmp_path: Path) -> None:
    """Sophios writes a float parameter's value as its text, `"1.0"` and never the int `1`, which an import would
    write back as `"1.0"`."""
    write_nextflow_artifacts(_compiled("resources"), tmp_path)
    path = tmp_path / NEXTFLOW_PARAMS
    path.write_text(path.read_text(encoding="utf-8").replace('"0.1"', "1"), encoding="utf-8")
    _stops(_CODE.IMPORT_PARAMS, path, '"res_ratio": 1', lambda: import_nextflow(tmp_path))


def test_a_process_block_after_the_workflows_stops_the_import(tmp_path: Path) -> None:
    """The process blocks are free in their order among themselves, not past the workflows (design §4.2)."""
    import_edited(str(tmp_path), NEXTFLOW_SCRIPT, "", "")
    path = tmp_path / NEXTFLOW_SCRIPT
    text = path.read_text(encoding="utf-8")
    (block,) = _PROCESS_BLOCKS.findall(text)
    path.write_text(text.replace(block, "") + "\n" + block, encoding="utf-8")
    _stops(_CODE.IMPORT_MISMATCH, path, "process greet", lambda: import_nextflow(tmp_path))


def test_a_port_the_tool_builder_cannot_name_stops_the_import_at_its_process(tmp_path: Path) -> None:
    """A CWL tool may call a port `keys`, which `Inputs` keeps for its method, so no workflow built through the
    tool builder compiles to the pipeline."""
    step = Step.from_cwl_document({
        "cwlVersion": "v1.2", "class": "CommandLineTool", "baseCommand": "echo", "stdout": "o.txt",
        "inputs": {"keys": {"type": "string", "inputBinding": {"position": 1}}},
        "outputs": {"out": {"type": "File", "outputBinding": {"glob": "o.txt"}}}}, process_name="tool")
    workflow = Workflow([step], "reserved")
    step.inputs.keys, workflow.outputs.out = "x", step.outputs.out
    write_nextflow_artifacts(workflow.compile(target="nextflow"), tmp_path)
    _stops(_CODE.IMPORT_UNWRITABLE, tmp_path / NEXTFLOW_SCRIPT, "process tool", lambda: import_nextflow(tmp_path))


@pytest.mark.parametrize("name", sorted({*dir(ParameterNamespace), "keys", "_items", "_x", "__x__"}))
def test_a_parameter_imports_exactly_when_a_python_workflow_can_take_its_input(name: str, tmp_path: Path) -> None:
    """A parameter is a workflow input: a pipeline whose parameter `unread` is renamed in all three files imports
    exactly when `flow.inputs.<name> = value` works. An attribute of the port namespace, such as `_store`, is read
    as itself, so it stops the import at its workflow, not with an AttributeError; `keys` and `_items` import."""
    write_nextflow_artifacts(_compiled("valued"), tmp_path)
    for file in _FILES:
        path = tmp_path / file
        path.write_text(path.read_text(encoding="utf-8").replace("unread", name), encoding="utf-8")
    try:
        setattr(Workflow([], "python").inputs, name, "kept")
    except ValueError:
        _stops(_CODE.IMPORT_UNWRITABLE, tmp_path / NEXTFLOW_SCRIPT, "workflow valued",
               lambda: import_nextflow(tmp_path))
    else:
        assert name in [port.name for port in import_nextflow(tmp_path).inputs]


def test_the_import_reads_the_three_files_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Copied alone into an empty directory, with the network refused, the files import, and no path a parameter
    names is looked at: not `nextflow_workflow.json`, no tool registry, no network, no input file."""
    model = _compiled("valued")
    write_nextflow_artifacts(model, tmp_path / "written")
    (tmp_path / "alone").mkdir()
    for file in _FILES:
        shutil.copy(tmp_path / "written" / file, tmp_path / "alone" / file)
    named = {item["path"] for value in model.to_dict()["params"].values()
             for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict)}
    looked: list[str] = []
    stat = os.stat

    def recording(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        looked.append(os.fspath(path) if isinstance(path, (str, os.PathLike)) else str(path))
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", recording)
    with network_refused() as attempts:
        imported = _imported(tmp_path / "alone")
    assert (imported, attempts, named & set(looked)) == (model, [], set())
    assert named, "the case names no file"
