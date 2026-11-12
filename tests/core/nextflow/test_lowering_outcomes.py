"""The Nextflow target's lowering ends one of three ways, whatever it is given: a model, a `NotLowered` holding a coded
refusal for each construct it cannot lower, or the compile's own `SophiosError` (design_docs/one-target-table-design.md
§5). Any other exception is a crash: an uncoded refusal a case has not reached yet.
"""
import copy
from typing import Any

import pytest
from hypothesis import example, given, strategies as st

from sophios.ir.resolved import NotLowered
from sophios.lang.diagnostics import SophiosError

from ..budgets import budget
from .testkit import lowered_case

_SCALARS = ["string", "int", "float", "boolean", "File", "Directory", "long", "double", "Any"]
_TYPES = st.one_of(
    st.sampled_from(_SCALARS),
    st.sampled_from(_SCALARS).map(lambda name: f"{name}?"),
    st.sampled_from(_SCALARS).map(lambda name: f"{name}[]"),
    st.sampled_from(_SCALARS).map(lambda name: {"type": "array", "items": name}),
    st.just({"type": "array", "items": {"type": "array", "items": "string"}}),
    st.just({"type": "enum", "symbols": ["a", "b"]}),
    st.just({"type": "record", "fields": {"a": "string"}}),
    st.lists(st.sampled_from(["null", "int", "string", "boolean"]), min_size=2, max_size=3, unique=True),
    st.just(["null", "boolean", {"type": "array", "items": "string"}]),
)
#: Values in the shapes CWL takes and in shapes it does not: NaN and infinities, a NUL byte, lists where one value
#: goes, mappings where text goes.
_ODD = st.one_of(st.integers(-2, 3), st.floats(allow_nan=True, allow_infinity=True), st.booleans(), st.none(),
                 st.sampled_from(["", " ", "a", "a/b", "a\x00b", "$(inputs.x)", "$(inputs.x).txt", "${return 1}",
                                  "$(inputs.x.path)", "$(inputs.x.basename)", "$(runtime.cores)", "*.txt",
                                  "$(self[0])", "$(self[0].contents)"]),
                 st.lists(st.sampled_from(["a", "b"]), max_size=2), st.just({"a": 1}))
_BINDING = st.fixed_dictionaries({}, optional={
    "position": _ODD, "prefix": _ODD, "separate": _ODD, "valueFrom": _ODD, "shellQuote": st.booleans(),
    "itemSeparator": _ODD, "loadContents": st.booleans()})
_INPUT = st.fixed_dictionaries({"type": _TYPES}, optional={
    "inputBinding": _BINDING | _ODD, "default": _ODD, "secondaryFiles": st.just([".bai"]),
    "loadContents": st.booleans()})
_OUTPUT_BINDING = st.fixed_dictionaries({}, optional={"glob": _ODD, "loadContents": st.booleans(), "outputEval": _ODD})
_OUTPUT = st.fixed_dictionaries({"type": _TYPES}, optional={"outputBinding": _OUTPUT_BINDING | _ODD})
_DIRENT = st.fixed_dictionaries({"entry": _ODD}, optional={"entryname": _ODD, "writable": _ODD})
_REQUIREMENTS = st.dictionaries(st.sampled_from([
    "DockerRequirement", "EnvVarRequirement", "InitialWorkDirRequirement", "ResourceRequirement",
    "ShellCommandRequirement", "NetworkAccess", "InlineJavascriptRequirement", "SoftwareRequirement",
    "cwltool:CUDARequirement"]), st.one_of(
        st.fixed_dictionaries({}, optional={"dockerPull": _ODD, "envDef": st.dictionaries(_ODD.map(str), _ODD,
                                                                                          max_size=2) | _ODD,
                                            "listing": st.lists(_DIRENT | _ODD, max_size=2) | _ODD,
                                            "coresMin": _ODD, "ramMin": _ODD, "networkAccess": _ODD,
                                            "cudaDeviceCountMin": _ODD}),
        _ODD), max_size=3)


#: What a tool mostly says: the shapes the target lowers, so that a drawn case reaches the assembly and the model.
_PLAIN_INPUT = st.fixed_dictionaries({"type": st.sampled_from(["string", "int", "float", "boolean", "File", "string?",
                                                               "string[]", "File[]"])},
                                     optional={"inputBinding": st.fixed_dictionaries(
                                         {"position": st.integers(0, 3)},
                                         optional={"prefix": st.sampled_from(["-x", "--y"]),
                                                   "separate": st.booleans()})})
_PLAIN_GLOB = st.sampled_from(["out.txt", "$(inputs.x)", "$(inputs.x).txt", "$(inputs.xs).txt", "$(inputs.x.basename)",
                               "*.txt", "$(inputs.y)"])
_PLAIN_OUTPUT = st.fixed_dictionaries({"type": st.sampled_from(["File", "File?", "Directory", "string"])},
                                      optional={"outputBinding": st.fixed_dictionaries(
                                          {"glob": _PLAIN_GLOB},
                                          optional={"loadContents": st.booleans(),
                                                    "outputEval": st.sampled_from(["$(self[0])",
                                                                                   "$(self[0].contents)"])})})
_PLAIN_VALUE = st.one_of(st.sampled_from(["a", "a.txt", "dir"]), st.integers(0, 3), st.booleans(),
                         st.floats(allow_nan=True, allow_infinity=True), st.lists(st.sampled_from(["a", "b"]),
                                                                                  max_size=2))


@st.composite
def _cases(draw: st.DrawFn) -> dict[str, Any]:
    """A one-step case (testkit.one_step), its tool drawn from the shapes CWL takes and from shapes it does not."""
    odd = draw(st.booleans())
    inputs = draw(st.dictionaries(st.sampled_from(["x", "y", "xs"]), _INPUT if odd else _PLAIN_INPUT, max_size=3))
    outputs = draw(st.dictionaries(st.sampled_from(["o", "p"]), _OUTPUT if odd else _PLAIN_OUTPUT, max_size=2))
    tool: dict[str, Any] = {"id": "T", "inputs": inputs, "outputs": outputs}
    for key, strategy in (("arguments", st.lists(_ODD | _BINDING, max_size=2) | _ODD),
                          ("baseCommand", _ODD), ("requirements", _REQUIREMENTS | _ODD), ("hints", _REQUIREMENTS),
                          ("stdout", _ODD), ("stdin", _ODD), ("successCodes", st.just([0, 1]))):
        if draw(st.integers(0, 3 if odd else 12)) == 0:
            tool[key] = draw(strategy)
    job = {name: draw(_ODD if odd else _PLAIN_VALUE) for name in inputs}
    return {"tool": tool, "out": draw(st.lists(st.sampled_from(sorted(outputs)), unique=True)) if outputs else [],
            "job": job}


_PRODUCER = {"id": "MAKE", "inputs": {"name": {"type": "string", "inputBinding": {"position": 1}}},
             "outputs": {"o": {"type": "File", "outputBinding": {"glob": "$(inputs.name)"}},
                         "maybe": {"type": "File?", "outputBinding": {"glob": "*.log"}},
                         "text": {"type": "string", "outputBinding": {"glob": "t.txt", "loadContents": True,
                                                                      "outputEval": "$(self[0].contents)"}}},
             "baseCommand": "touch"}


@st.composite
def _workflows(draw: st.DrawFn) -> dict[str, Any]:
    """A two-step case of the `workflow` form: a producer, a consumer of one of its outputs, perhaps scattered or
    conditional, and workflow outputs naming steps and ports that are there and ones that are not."""
    consumed = draw(st.sampled_from(["o", "maybe", "text"]))
    consumer_type = draw(st.sampled_from(["File", "File?", "string", "string?", "File[]", "string[]"]))
    use: dict[str, Any] = {"id": "USE", "in": {"src": {"wic_alias": "made"}}, "out": ["r"]}
    if draw(st.booleans()):
        use["when"] = draw(st.sampled_from(["$(inputs.flag)", "$(inputs.src != null)", "$(inputs.flag == 1)",
                                            "${return true;}", "$(true)"]))
        use["in"]["flag"] = "flag"
    if draw(st.booleans()):
        use["scatter"] = ["src"]
    make: dict[str, Any] = {"id": "MAKE", "in": {"name": "names" if draw(st.booleans()) else "name"},
                            "out": [{consumed: {"wic_anchor": "made"}}]}
    if draw(st.booleans()):
        make["scatter"] = ["name"]
    outputs = {f"out{index}": {"type": draw(st.sampled_from(["File", "File?", "File[]", "string"])),
                               "outputSource": source}
               for index, source in enumerate(draw(st.lists(st.sampled_from(
                   ["USE/r", "MAKE/o", "MAKE/maybe", "MAKE/text", "MAKE/nope", "nope/o", "name"]), max_size=2,
                   unique=True)))}
    workflow = {"steps": [make, use], "outputs": outputs,
                "inputs": {"name": {"type": "string"}, "names": {"type": "string[]"}, "flag": {"type": "boolean"}}}
    consumer = {"id": "USE", "inputs": {"src": {"type": consumer_type, "inputBinding": {"position": 1}}},
                "outputs": {"r": {"type": "File", "outputBinding": {"glob": "r.txt"}}}, "baseCommand": "cat",
                "stdout": "r.txt"}
    return {"workflow": workflow, "tools": [_PRODUCER, consumer],
            "job": {"name": draw(_PLAIN_VALUE), "names": ["a", "b"], "flag": draw(_PLAIN_VALUE)}}


def _written_out(cwl_type: Any) -> Any:
    """`cwl_type` with CWL's shorthand written out: `T[]` as `{type: array, items: T}`, `T?` as `[null, T]`."""
    if isinstance(cwl_type, list):
        return [_written_out(each) for each in cwl_type]
    if isinstance(cwl_type, str) and cwl_type.endswith("?"):
        return ["null", _written_out(cwl_type[:-1])]
    if isinstance(cwl_type, str) and cwl_type.endswith("[]"):
        return {"type": "array", "items": _written_out(cwl_type[:-2])}
    return cwl_type


def _outcome(case: dict[str, Any]) -> Any:
    """The model `case` lowers to, the codes of its refusals, or that it does not compile."""
    try:
        return lowered_case(case)[0]
    except NotLowered as refused:
        return [refusal.code for refusal in refused.refusals]
    except SophiosError:
        return "does not compile"


@pytest.mark.fast
@budget(60)
@given(_cases() | _workflows())
# A valueFrom YAML reads as a boolean, a requirement that is not a mapping, and a list input given one value.
@example({"tool": {"id": "T", "inputs": {}, "outputs": {}, "arguments": [{"valueFrom": True}]}, "out": [], "job": {}})
@example({"tool": {"id": "T", "inputs": {}, "outputs": {}, "requirements": {"DockerRequirement": "ubuntu"}},
          "out": [], "job": {}})
@example({"tool": {"id": "T", "inputs": {"xs": {"type": "string[]", "inputBinding": {"position": 1}}}, "outputs": {}},
          "out": [], "job": {"xs": "a"}})
def test_lowering_ends_in_a_model_a_coded_refusal_or_a_compile_error(case: dict[str, Any]) -> None:
    """Whatever a tool says, lowering it to Nextflow lowers it, refuses it with a code, or never compiles; and its
    input types written out, as CWL reads its shorthand (CWL v1.2, Type DSL), it ends the same way."""
    outcome = _outcome(case)
    if "tool" in case:
        written = copy.deepcopy(case)
        for port in written["tool"]["inputs"].values():
            port["type"] = _written_out(port["type"])
        assert _outcome(written) == outcome
