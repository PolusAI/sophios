"""The Phase 3 acceptance cells (design §6) as builders. A1-A9, W1-W7 are decided in test_subset_differential.py,
S1-S14 in test_runtime.py's scatter property, W8-W11 and A11 in the example workflows of
tests/core/data/nextflow/runs and stops; A10 has no runtime test (rejected)."""

# pylint: disable=missing-function-docstring

from typing import Any

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow


def _text_output() -> Output:
    """A string output holding the contents of out.txt."""
    return Output(cwl.string, glob="out.txt", load_contents=True, output_eval="$(self[0].contents)")


def _printf(name: str, *ports: str) -> CommandLineTool:
    """printf the hyphen-joined ports, captured as the step's text output."""
    declared = {port: Input(cwl.string, position=index) for index, port in enumerate(ports, start=1)}
    tool = CommandLineTool(name, Inputs(**declared), Outputs(line=_text_output()))
    return tool.base_command("printf").stdout("out.txt").argument("-".join(["%s"] * len(ports)), position=0)


def _scatter_step(name: str, method: str, values: dict[str, Any]) -> Step:
    step = Step(_printf(name, *values), step_name=name.lower())
    for port, value in values.items():
        setattr(step.inputs, port, value)
    step.scatter_on(*(getattr(step.inputs, port) for port in values if isinstance(values[port], list)), method=method)
    return step


# A1-A9 -------------------------------------------------------------------

CALCULATIONS = {
    "A1": ["$(inputs.i + 0.5)", "$(3)"],
    "A2": ["$(inputs.i - inputs.x + 1)"],
    "A3": ["$(inputs.i * inputs.x / 4)"],
    "A4": ["$(-7 % 3)", "$(inputs.i % -4)"],
    "A5": ["$(Math.pow(inputs.i, 2))", "$(Math.pow(inputs.x, -1))"],
    "A6": ["$(-(inputs.i + 2) * +3)"],
    "A7": ["$(Math.sqrt(inputs.x * 4) + Math.abs(-inputs.i))"],
    "A8": ["$(Math.floor(inputs.x) + Math.ceil(inputs.x))", "$(Math.round(-2.5))", "$(Math.round(1e19))"],
    "A9": ["$(Math.min(inputs.i, 1))", "$(Math.max(1, 2, inputs.x))"],
}
CALCULATED = "7.5 3 5.75 3.9375 -1 3 49 0.4444444444444444 -27 10 5 -2 10000000000000000000 1 2.25"


def _calc_tool(name: str) -> CommandLineTool:
    tool = CommandLineTool(name, Inputs(i=Input(cwl.int), x=Input(cwl.float)),
                           Outputs(out=Output(cwl.file, glob=f"{name}.txt")))
    return tool.base_command("touch").argument(f"{name}.txt", position=1)


def _calc_workflow(whens: list[str], name: str) -> Workflow:
    steps = []
    for index, when in enumerate(whens):
        step = Step(_calc_tool(f"c{index}"), step_name=f"c{index}")
        step.inputs.i, step.inputs.x = 7, 2.25
        step.when = when
        steps.append(step)
    return Workflow(steps, name)


def calculator_workflow() -> Workflow:
    """A1-A9: a step for each calculation compared in a when with what node computes, then with twice that."""
    expressions = [text for texts in CALCULATIONS.values() for text in texts]
    whens = [
        f"$(({text[2:-1]}) === {value}{scale})"
        for scale in ("", " * 2") for text, value in zip(expressions, CALCULATED.split(), strict=True)
    ]
    return _calc_workflow(whens, "calc_cells")


# W1-W11 ------------------------------------------------------------------

PREDICATES = {
    "$(true)": True,                                         # W1
    "$(false)": False,                                       # W1
    "$(inputs.flag)": True,                                  # W2
    "$(inputs.n > 2 && inputs.n <= 3)": True,                # W3
    "$(inputs.s === 'x' && inputs.s != 'y')": True,          # W4
    "$(inputs.s == 'y')": False,                             # W4
    "$(inputs.flag && !(inputs.n < 0) || false)": True,      # W5
    "$(Math.round(inputs.n / 2) >= 2)": True,                # W6
}


def _when_tool(name: str) -> CommandLineTool:
    ports = Inputs(flag=Input(cwl.boolean), s=Input(cwl.string), n=Input(cwl.int))
    tool = CommandLineTool(name, ports, Outputs(out=Output(cwl.file, glob=f"{name}.txt")))
    return tool.base_command("touch").argument(f"{name}.txt", position=1)


def predicates_workflow() -> Workflow:
    """W1-W6: a step for each predicate."""
    steps = []
    for index, predicate in enumerate(PREDICATES):
        step = Step(_when_tool(f"p{index}"), step_name=f"p{index}")
        step.inputs.flag, step.inputs.s, step.inputs.n = True, "x", 3
        step.when = predicate
        steps.append(step)
    return Workflow(steps, "when_cells")
