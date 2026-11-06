"""The safe JavaScript subset: a when decides under Nextflow as JavaScript decides it.

design_docs/sophios_nextflow_backend.md §6, Expressions, requires the two
authorities to agree over generated expressions. node evaluates each one with
its step's input values; the step's when is that expression, or, for a number,
the expression `=== ` the number node prints, so the step runs under the pinned
Nextflow exactly when node gives true. node drops an expression with a
non-finite subexpression, which fails by design (A11, test_phase3_cells.py).
The generated grammar leaves out `Math.pow` and `Math.sqrt`, as the sample of
the frozen table this replaces did; the pinned edges and cells hold them.
"""

# pylint: disable=missing-function-docstring

import json
import re
import subprocess
from typing import Any

from hypothesis import assume, example, given, strategies as st
import pytest

from sophios.input_output_nf import write_nextflow_artifacts
from sophios.utils_nf import compiled_source_to_nextflow

from ..budgets import budget
from .test_phase3_cells import CALCULATIONS, PREDICATES
from .testkit import execute_nextflow, step, synthetic_source, tool, workflow_doc

#: Each input a case may read, by its CWL type.
TYPES = {"i": "int", "n": "int", "x": "float", "flag": "boolean", "s": "string", "maybe": "int?"}

_NUMBER = st.recursive(
    st.sampled_from(["0", "1", "2", "3", "7", "10", "0.5", "1.25", "2.5", "100", "0.1", "1.0", "0.0001", "12345678.9",
                     "0.49999999999999994", "1e15", "1e19", "9007199254740993", "inputs.i", "inputs.n", "inputs.x"]),
    lambda inner: st.one_of(
        st.tuples(st.sampled_from(["-", "+"]), inner).map(lambda c: f"{c[0]}({c[1]})"),
        st.tuples(st.sampled_from(["Math.abs", "Math.floor", "Math.ceil", "Math.round"]), inner).map(
            lambda c: f"{c[0]}({c[1]})"),
        st.tuples(st.sampled_from(["Math.min", "Math.max"]), st.lists(inner, min_size=2, max_size=3)).map(
            lambda call: f"{call[0]}({', '.join(call[1])})"),
        st.tuples(inner, st.sampled_from(["+", "-", "*", "/", "%"]), inner).map(lambda c: f"({c[0]} {c[1]} {c[2]})"),
    ),
    max_leaves=6,
)
_COMPARISON = st.tuples(_NUMBER, st.sampled_from(["<", "<=", ">", ">=", "==", "!=", "===", "!=="]), _NUMBER).map(
    " ".join)
_BOOLEAN = st.recursive(
    _COMPARISON | st.sampled_from(["true", "false", "inputs.flag", "inputs.s === 'x'", "inputs.s != 'y'", "'a' === 'a'",
                                   "inputs.s !== 'it\"s'", "inputs.maybe === null", "null != inputs.maybe"]),
    lambda inner: st.one_of(inner.map(lambda e: f"!({e})"), st.tuples(inner, st.sampled_from(["&&", "||"]), inner).map(
        " ".join)),
    max_leaves=3,
)
#: Each input's value: an int in the entry check's range, a float with its sign, and an absent optional.
_VALUES = st.fixed_dictionaries({
    "i": st.integers(-2**31, 2**31 - 1) | st.integers(-9, 9), "n": st.integers(-9, 9),
    "x": st.sampled_from([0.0, -0.0, -0.5, 2.25, 1e-7, 1e15 + 0.5]), "flag": st.booleans(),
    "s": st.sampled_from(["x", "y", 'it"s', ""]), "maybe": st.none() | st.integers(-3, 3)})
_CASE = st.tuples((_NUMBER | _BOOLEAN).map(lambda e: f"$({e})"), _VALUES)

#: Edge cases where digits, rounding, precedence or a sign could slip, each with no input.
EDGES = [
    "$(1 + 2 * 3 - 4 / 8)", "$((1 + 2) * (3 - 4) / 8)", "$(-7 % 3)", "$(7 % -3)", "$(-7.5 % 2)",
    "$(0.1 + 0.2)", "$(1 / 3)", "$(2 / 3 * 3)", "$(9007199254740993)", "$(1e15 + 0.3)",
    "$(Math.round(2.5))", "$(Math.round(-2.5))", "$(Math.round(-0.4))", "$(Math.round(0.49999999999999994))",
    "$(Math.round(1e19))", "$(Math.floor(-1.5))", "$(Math.ceil(-1.5))", "$(Math.abs(-0))",
    "$(Math.sqrt(2))", "$(Math.pow(2, 10))", "$(Math.pow(2, -2))",
    "$(Math.min(3, -1, 2))", "$(Math.max(3, -1, 2.5))", "$(-(-3))", "$(+-+3)", "$(- -3)", "$(12345678.9)", "$(0.0001)",
    "$(1 < 2 && 2 <= 2 && !(3 > 4) || false)", "$(1 == 1.0 && 1 === 1.0)", "$('a' === 'a' && 'a' != 'b')",
    "$(true != false)", "$(0.1 + 0.2 == 0.3)", "$(Math.round(-0.5) === 0)",
]
_CELLS = {"i": 7, "x": 2.25, "flag": True, "s": "x", "n": 3}
#: The edges; the calculator (A1-A9) and predicate (W1-W7) cells; each of whose left sides is -0 in JavaScript,
#: which == and != treat as 0; a null check on an optional input, absent and present; and int inputs whose quotient
#: and product JavaScript takes as doubles.
PINNED = [
    *((text, {}) for text in EDGES),
    *((text, _CELLS) for texts in CALCULATIONS.values() for text in texts),
    *((text, _CELLS) for text in [*PREDICATES, "$(inputs.s === 'x')"]), ("$(inputs.n > 0)", {"n": 1}),
    ("$(inputs.n % 2 == 0)", {"n": -4}), ("$(inputs.n * -1 != 0)", {"n": 0}),
    ("$(Math.ceil(inputs.x) == 0)", {"x": -0.5}),
    *((text, {"maybe": maybe}) for text in ("$(inputs.maybe === null)", "$(inputs.maybe !== null)")
      for maybe in (None, 1)),
    ("$(inputs.i / inputs.n)", {"i": 1, "n": 3}), ("$(inputs.i * inputs.i)", {"i": 2**31 - 1}),
]


def _node(cases: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """What node gives for each case's expression, or None when it or a parenthesized subexpression is not finite."""
    script = r"""
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(([text, inputs]) => {
  const body = text.slice(2, -1);
  const parts = [];
  for (let i = 0; i < body.length; i++) if (body[i] === '(') {
    let depth = 0, j = i;
    for (; j < body.length; j++) { if (body[j] === '(') depth++; if (body[j] === ')' && --depth === 0) break; }
    let k = i;
    while (k > 0 && /[A-Za-z.]/.test(body[k - 1])) k--;
    parts.push(body.slice(k, j + 1));
  }
  const run = part => Function('inputs', 'return (' + part + ')')(inputs);
  const finite = [body, ...parts].every(part => { try { const v = run(part); return typeof v !== 'number' || Number.isFinite(v); } catch (e) { return true; } });
  return finite ? JSON.stringify(run(body)) : null;
})));
"""
    printed = subprocess.run(["node", "-e", script], input=json.dumps(cases), text=True, capture_output=True,
                             check=True)
    return [None if value is None else json.loads(value) if value in ("true", "false") else value
            for value in json.loads(printed.stdout)]


@pytest.mark.nextflow
@pytest.mark.serial
@budget(3)
@given(st.lists(_CASE, min_size=1, max_size=12))
@example(PINNED)
def test_a_when_decides_as_javascript_does(tmp_path_factory: pytest.TempPathFactory,
                                           cases: list[tuple[str, dict[str, Any]]]) -> None:
    """Each case is a step that reads the inputs its expression names, with the values the case gives them."""
    tools, steps, inputs, job = list[Any](), list[Any](), dict[str, str](), dict[str, Any]()
    decided: list[tuple[str, dict[str, Any], bool]] = []
    for (text, values), value in zip(cases, _node(cases)):
        if value is None:
            continue
        index = len(decided)
        when = text if isinstance(value, bool) else f"$(({text[2:-1]}) === {value})"
        decided.append((when, values, value is not False))
        read = sorted(set(re.findall(r"inputs\.(\w+)", text))) or ["i"]
        tools.append(tool(f"P{index}", inputs={name: {"type": TYPES[name]} for name in read}, baseCommand="touch",
                          arguments=[f"ran_{index}.txt"],
                          outputs={"out": {"type": "File", "outputBinding": {"glob": f"ran_{index}.txt"}}}))
        steps.append(step(f"P{index}", **{"in": {name: f"p{index}_{name}" for name in read}, "out": ["out"],
                                          "when": when}))
        inputs |= {f"p{index}_{name}": TYPES[name] for name in read}
        job |= {f"p{index}_{name}": values.get(name, 1) for name in read if values.get(name, 1) is not None}
    assume(decided)
    root = tmp_path_factory.mktemp("when")
    outputs = {f"out_{index}": {"type": "File?", "outputSource": f"P{index}/out"} for index in range(len(decided))}
    write_nextflow_artifacts(compiled_source_to_nextflow(synthetic_source(
        workflow_doc(steps, inputs=inputs, outputs=outputs), tools, workflow_inputs=job)), root)
    result = execute_nextflow(root)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = {int(path.stem.removeprefix("ran_")) for path in (root / "work").glob("*/*/ran_*.txt")}
    assert [(when, values, index in ran) for index, (when, values, _) in enumerate(decided)] == decided
