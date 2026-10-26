"""A float reaches the command as Python's str() of it (design §6, Inputs and channels)."""
import json
import math
from pathlib import Path
import random
import struct
import sys

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow
from sophios.input_output_nf import (
    NF_FLOAT_TEXT_FUNCTION, nextflow_params, render_nextflow, write_nextflow_artifacts,
)
from sophios.nf_types import NfPort, NfProcess, NfTemplate

from .test_channels import _lower
from .testkit import command, execute_nextflow, output_port, ref, single_process_workflow, template


def _doubles() -> list[float]:
    """Random bit patterns over the whole range, and the values where digits or notation could slip."""
    rng = random.Random(77)
    drawn = [struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0] for _ in range(6000)]
    edges = [0.0, 5e-324, 2.225073858507201e-308, 2.2250738585072014e-308, sys.float_info.max, 5.0, 1e15,
             0.1 + 0.2, 1 / 3, 2.0**49 + 0.25, 2.0**49 + 0.75, 123456789.12345679, 9007199254740993.0]
    for boundary in (1e-4, 1e16):  # each is a power of ten below; these are its neighbours
        for direction in (0.0, math.inf):
            value = boundary
            for _ in range(3):
                value = math.nextafter(value, direction)
                edges.append(value)
    edges += [float(f"1e{exponent}") for exponent in range(-323, 309)]
    edges += [math.ldexp(1.0, exponent) for exponent in range(-1074, 1024)]
    return [value for value in drawn if math.isfinite(value)] + edges + [-value for value in edges]


# Numbers as a hand-edited params file holds them, which Nextflow reads exactly, printed as Python's str() of the
# JSON value; cwltool keeps a job file's spelling and rounds one with an exponent to an integer.
_HAND_EDITED = ["1e-7", "1E16", "0.1", "1.50", "2.5e-3", "-1e-400", "0.0", "5", "-0", "3000000000",
                "17976931348623157e292"]
# Text given on the command line, printed as Python's str(float(text)); each reaches the command another way.
_COMMAND_LINE = {"a": "1.50", "b": "1e-4", "c": "-0.0", "d": "1E16"}


@pytest.mark.nextflow
@pytest.mark.serial
def test_every_float_reaches_the_command_as_python_prints_it(tmp_path: Path) -> None:
    """One run renders every double through an array binding, command-line text through each other position, and
    workflow inputs through a step input's linkMerge and a glob."""
    doubles = _doubles()
    hand = [*_HAND_EDITED, *(repr(value) for value in random.Random(7).sample(doubles, 300) if value)]
    show = (
        CommandLineTool(
            "show",
            Inputs(
                xs=Input(cwl.array(cwl.float), position=1), hand=Input(cwl.array(cwl.float), position=2),
                a=Input(cwl.float, position=3), b=Input(cwl.optional(cwl.float), position=4, flag="--b=",
                                                        separate=False),
                c=Input(cwl.float), d=Input(cwl.float, position=6), merged=Input(cwl.array(cwl.float), position=7),
                g=Input(cwl.float),
            ),
            # The command writes the name the glob gives 1e-7 only when the glob prints it as Python does.
            Outputs(result=Output(cwl.file, glob="$(inputs.g).txt")),
        )
        .base_command("printf").argument("%s\\n", position=0).argument("$(inputs.c)", position=5)
        .stdout("1e-07.txt")
    )
    step = Step(show, step_name="show")
    step.inputs.xs, step.inputs.hand = doubles, [0.5]
    step.inputs.a, step.inputs.b, step.inputs.c, step.inputs.d = 0.5, 0.5, 0.5, 0.5
    workflow = Workflow([step], "floats")
    step.inputs.merged = StepInput(source=[workflow.inputs.x.as_type("float"), workflow.inputs.y.as_type("float")],
                                   link_merge="merge_flattened")
    step.inputs.g = workflow.inputs.g.as_type("float")
    workflow.outputs.result = step.outputs.result
    write_nextflow_artifacts(_lower(workflow, {"x": 1e-07, "y": -0.0, "g": 1e-07}), tmp_path)
    params_path = tmp_path / "nextflow_params.json"
    params = json.loads(params_path.read_text(encoding="utf-8"))
    params["show_hand"] = "hand"
    params_path.write_text(json.dumps(params).replace('"hand"', f"[{', '.join(hand)}]"), encoding="utf-8")

    result = execute_nextflow(tmp_path, *(f"--show_{name}={text}" for name, text in _COMMAND_LINE.items()))

    assert result.returncode == 0, result.stdout + result.stderr
    [printed] = [path.read_text(encoding="utf-8") for path in (tmp_path / "work").rglob("1e-07.txt")]
    given = {name: repr(float(text)) for name, text in _COMMAND_LINE.items()}
    expected = [*map(repr, doubles), *(str(json.loads(text)) for text in hand),
                given["a"], f"--b={given['b']}", given["c"], given["d"], "1e-07", "-0.0"]
    assert printed.splitlines() == expected


def _float_workflow(tokens: tuple[str | NfTemplate, ...], param_type: str) -> str:
    process = NfProcess("SHOW", [NfPort("x", "val")], [output_port("result", "out.txt")],
                        command(*tokens, stdout="out.txt"))
    return render_nextflow(single_process_workflow(
        process, params={"x": 0.5}, param_types={"x": param_type}, output_port_name="result"))


@pytest.mark.fast
@pytest.mark.parametrize(("tokens", "param_type", "emitted"), [
    (("echo", template(ref("x"))), "float", True),
    (("echo",), "float", False),
    (("echo", template(ref("x"))), "string", False),
], ids=["rendered", "not-rendered", "no-float"])
def test_the_float_text_helper_is_emitted_only_where_a_float_is_rendered(
    tokens: tuple[str | NfTemplate, ...], param_type: str, emitted: bool,
) -> None:
    assert (NF_FLOAT_TEXT_FUNCTION in _float_workflow(tokens, param_type)) is emitted


@pytest.mark.fast
def test_the_params_file_holds_each_float_as_its_python_text() -> None:
    """A JSON number zero reaches Nextflow without its sign, so every float is written as the text the check reads."""
    process = NfProcess("SHOW", [NfPort("x", "val"), NfPort("xs", "val", is_array=True), NfPort("n", "val")],
                        [output_port("result", "out.txt")], command("echo", stdout="out.txt"))
    workflow = single_process_workflow(
        process, params={"x": -0.0, "xs": [1e-07, 5], "n": 2},
        param_types={"x": "float", "xs": "float[]", "n": "int"}, output_port_name="result")
    assert nextflow_params(workflow) == {"x": "-0.0", "xs": ["1e-07", 5], "n": 2}
