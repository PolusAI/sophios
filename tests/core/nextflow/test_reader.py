"""The reader is the renderer's inverse over workflows Sophios compiles (design §4.2).

For each workflow here, compiled to a model M, the three files written from
M read back to M itself. Model equality sees the fields the text shows only
through others, and the orders the model holds canonical.
"""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from pathlib import Path

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Inference, Step, StepInput, Workflow
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.nf_reader import read_nextflow


def _echo(name: str, **inputs: Input) -> CommandLineTool:
    """A tool that echoes its inputs in order into `<name>.txt`."""
    return CommandLineTool(name, Inputs(**inputs), Outputs(out=Output(cwl.file, glob=f"{name}.txt"))).base_command(
        "echo").stdout(f"{name}.txt")


def _bindings() -> Workflow:
    """A flag, a prefixed array, a joined optional, both kinds of entry, a piped shell literal, and variables."""
    inputs = Inputs(v=Input(cwl.boolean, position=1, flag="-v"),
                    xs=Input(cwl.array(cwl.string), position=2, flag="--x"),
                    name=Input(cwl.string, required=False, position=3, flag="--name=", separate=False),
                    src=Input(cwl.file), out=Input(cwl.directory))
    tool = (CommandLineTool("bind", inputs, Outputs(
        res=Output(cwl.directory, glob="$(inputs.out.basename)"),
        one=Output(cwl.file, glob="one.txt", output_eval="$(self[0])"),
        maybe=Output(cwl.file, glob="*.log", required=False)))
        .base_command("prog").argument("2>&1 | tee", shell_quote=False, position=4)
        .argument("$(inputs.out.basename)", position=5).shell_command()
        .stage(inputs.src, entryname="renamed.txt").stage(inputs.out, writable=True)
        .stdout("o.txt").env_var("A", "lit $x").env_var("B", "$(inputs.name)"))
    step = tool.to_step()
    step.inputs.v, step.inputs.xs, step.inputs.src, step.inputs.out = True, ["p", "q"], "/tmp/s.txt", "/tmp/o"
    step.inputs.name = "n"
    workflow = Workflow([step], "bindings")
    workflow.outputs.res, workflow.outputs.one, workflow.outputs.maybe = (
        step.outputs.res, step.outputs.one, step.outputs.maybe)
    return workflow


def _crossproduct() -> Workflow:
    """A conditional flat crossproduct gathered into an array, a step input picking a value, a conditional step."""
    pair = _echo("pair", item=Input(cwl.string, position=1), tag=Input(cwl.string, position=2)).to_step()
    use = CommandLineTool("use", Inputs(files=Input(cwl.array(cwl.file), position=1), n=Input(cwl.int, position=2)),
                          Outputs(o=Output(cwl.file, glob="o.txt"))).base_command("cat").stdout("o.txt").to_step()
    join = _echo("join", words=Input(cwl.string, position=1)).to_step()
    workflow = Workflow([pair, use, join], "crossproduct")
    pair.inputs.item, pair.inputs.tag = ["i", "j"], ["1", "2"]
    pair.scatter_on(pair.inputs.item, pair.inputs.tag, method="flat_crossproduct")
    use.inputs.files = pair.outputs.out
    use.inputs.n = 3
    use.when = "$(inputs.n > 1)"
    join.inputs.words = StepInput(source=[workflow.inputs.a.as_type("string?"), workflow.inputs.b.as_type("string?")],
                                  link_merge="merge_nested", pick_value="first_non_null")
    workflow.outputs.o, workflow.outputs.j = use.outputs.o, join.outputs.out
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
    """Conditions of nested unary operators, whose canonical text is not a decrement."""
    whens = ("$(-(-inputs.x) > 0)", "$(- -inputs.x > 0)", "$(+(+inputs.x) > 0)", "$(-(-3) > inputs.x)",
             "$(inputs.s === 'it\"s' && !(inputs.o == null))")
    steps = [_echo(f"w{index}", x=Input(cwl.int, position=1), s=Input(cwl.string, position=2),
                   o=Input(cwl.string, required=False, position=3)).to_step() for index in range(len(whens))]
    workflow = Workflow(steps, "whens")
    for step, when in zip(steps, whens):
        step.inputs.x, step.inputs.s, step.inputs.o = 2, "it\"s", workflow.inputs.o.as_type("string?")
        step.when = when
        setattr(workflow.outputs, step.process_name, step.outputs.out)
    return workflow


def _streams() -> Workflow:
    """Each stream redirected, and a shell literal `>` that is not one."""
    redirected = CommandLineTool("streams", Inputs(src=Input(cwl.file)), Outputs(
        out=Output(cwl.file, glob="out.txt"), err=Output(cwl.file, glob="err.txt"))).base_command("cat").stdin(
        "$(inputs.src.path)").stdout("out.txt").stderr("err.txt")
    literal = CommandLineTool("literal", Inputs(), Outputs(o=Output(cwl.file, glob="o.txt"))).base_command(
        "echo").argument(">", shell_quote=False, position=1).argument("o.txt", position=2).shell_command()
    first, second = redirected.to_step(), literal.to_step()
    first.inputs.src = "/tmp/in.txt"
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


BUILDERS = {
    "bindings": _bindings, "crossproduct": _crossproduct, "dotproduct": _scatters("dotproduct"),
    "flat_crossproduct": _scatters("flat_crossproduct"), "nested_crossproduct": _scatters("nested_crossproduct"),
    "resources": _resources, "directories": _directories, "unary_whens": _unary_whens, "streams": _streams,
    "merged": _merged,
}


@pytest.mark.parametrize("build", BUILDERS.values(), ids=BUILDERS.keys())
def test_the_files_written_from_a_compiled_model_read_back_to_it(build: Callable[[], Workflow], tmp_path: Path) -> None:
    model = build().compile(target="nextflow", inference=Inference(enabled=False))
    write_nextflow_artifacts(model, tmp_path)
    assert read_nextflow(tmp_path) == model
