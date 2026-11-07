"""The backend's support table is complete, and capability analysis reads it (design §3)."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
import copy
from importlib.resources import files
from typing import Any

import pytest
import yaml
from cwl_utils.parser import cwl_v1_2

from sophios.lang import SUPPORT_MATRIX
from sophios.ir.resolved import Construct, NotLowered, Refusal
from sophios.nf_support import CONSTRUCTS, FIELDS, REQUIREMENT_CLASSES, State
from sophios.wic_types import Yaml

from .testkit import lowered_case, step, tool


@pytest.mark.fast
@pytest.mark.parametrize("cwl_class", sorted(SUPPORT_MATRIX))
def test_the_sophios_classes_have_the_support_matrix_fields(cwl_class: str) -> None:
    assert set(FIELDS[cwl_class]) == set(SUPPORT_MATRIX[cwl_class])


def _declared_fields(cwl_class: str) -> set[str]:
    """The fields cwl_utils declares for a CWL class, or cwltool's extension schema for a `cwltool:` one."""
    if cwl_class.startswith("cwltool:"):
        schema = yaml.safe_load(files("cwltool").joinpath("extensions-v1.2.yml").read_text("utf-8"))
        [record] = [item for item in schema["$graph"] if item.get("name") == cwl_class.removeprefix("cwltool:")]
        return set(record["fields"])
    return set(getattr(cwl_v1_2, cwl_class).attrs)


@pytest.mark.fast
@pytest.mark.parametrize("cwl_class", sorted(FIELDS))
def test_every_field_cwl_utils_or_cwltool_declares_is_classified(cwl_class: str) -> None:
    assert set(FIELDS[cwl_class]) == _declared_fields(cwl_class)


@pytest.mark.fast
def test_every_requirement_class_is_a_cwl_class_and_an_admitted_one_has_its_fields() -> None:
    for classes in REQUIREMENT_CLASSES.values():
        for name, row in classes.items():
            assert _declared_fields(name), name
            assert row.state is State.REJECTED or name in FIELDS, name


@pytest.mark.fast
def test_every_field_stands_under_a_construct_a_refusal_names() -> None:
    """A refusal names a construct of the view; the join says which fields each stands for (resolved-view §7)."""
    assert set(CONSTRUCTS) == {construct.value for construct in Construct}
    rows = {row for joined in CONSTRUCTS.values() for row in joined}
    assert {cwl_class for cwl_class, _name in rows} <= set(FIELDS)
    assert [(cwl_class, name) for cwl_class, fields in FIELDS.items() for name in fields
            if (cwl_class, None) not in rows and (cwl_class, name) not in rows] == []


# Design §3 names the compile-time fields: these wherever CWL declares them, and NetworkAccess.
COMPILE_TIME = {"doc", "format", "intent", "label", "streamable"}


@pytest.mark.fast
def test_the_compile_time_fields_are_the_ones_the_design_names() -> None:
    compile_time = {
        (cwl_class, name) for cwl_class, rows in FIELDS.items() for name, row in rows.items()
        if row.state is State.COMPILE_TIME
    }
    assert compile_time == {
        (cwl_class, name) for cwl_class, rows in FIELDS.items() for name in rows
        if name in COMPILE_TIME or cwl_class == "NetworkAccess"
    }


def _strings() -> Yaml:
    return {"type": "array", "items": "string"}


def _documents() -> tuple[Yaml, Yaml]:
    """A tool step scattered over `x`, so the workflow's `x`, the tool's `xs` and the gathered `o` are array schemas."""
    run = tool(
        "T",
        inputs={
            "x": {"type": "string", "inputBinding": {"position": 1}},
            "xs": {"type": _strings(), "inputBinding": {"position": 2}},
        },
        outputs={"o": {"type": "File", "outputBinding": {"glob": "o.txt"}}},
        requirements={},
    )
    workflow = {
        "steps": [step("T", **{"in": {"x": {"wic_raw_cwl": {"source": "x"}}, "xs": {"wic_raw_cwl": {"source": "xs"}}},
                               "out": ["o"], "scatter": ["x"]})],
        "inputs": {"x": {"type": _strings()}, "xs": {"type": _strings()}},
        "outputs": {"o": {"type": {"type": "array", "items": "File"}, "outputSource": "T/o"}},
        "requirements": {"ScatterFeatureRequirement": {}},
    }
    return workflow, run


# Where each class's value sits in `_documents()`.
POSITIONS: dict[str, Callable[[Yaml, Yaml], Yaml]] = {
    "Workflow": lambda workflow, _run: workflow,
    "WorkflowStep": lambda workflow, _run: workflow["steps"][0],
    "WorkflowStepInput": lambda workflow, _run: workflow["steps"][0]["in"]["x"]["wic_raw_cwl"],
    "WorkflowInputParameter": lambda workflow, _run: workflow["inputs"]["x"],
    "WorkflowOutputParameter": lambda workflow, _run: workflow["outputs"]["o"],
    "CommandLineTool": lambda _workflow, run: run,
    "CommandInputParameter": lambda _workflow, run: run["inputs"]["x"],
    "CommandLineBinding": lambda _workflow, run: run["inputs"]["x"]["inputBinding"],
    "CommandOutputParameter": lambda _workflow, run: run["outputs"]["o"],
    "CommandOutputBinding": lambda _workflow, run: run["outputs"]["o"]["outputBinding"],
    "InputArraySchema": lambda workflow, _run: workflow["inputs"]["x"]["type"],
    "CommandInputArraySchema": lambda _workflow, run: run["inputs"]["xs"]["type"],
    "OutputArraySchema": lambda workflow, _run: workflow["outputs"]["o"]["type"],
}


def _position(cwl_class: str) -> Callable[[Yaml, Yaml], Yaml]:
    """A row's position; a requirement class's fields sit in the tool's requirements."""
    return POSITIONS.get(cwl_class, lambda _workflow, run: run["requirements"].setdefault(cwl_class, {}))


# The subject of a refusal of a field, by its class: the port it sits on, or in the tool the path to it.
PORT_SUBJECTS = {
    "WorkflowStepInput": "x", "WorkflowInputParameter": "x", "InputArraySchema": "x",
    "WorkflowOutputParameter": "o", "OutputArraySchema": "o",
}
TOOL_PATHS = {
    "CommandLineTool": "", "CommandInputParameter": "inputs.x.", "CommandInputArraySchema": "inputs.xs.type.",
    "CommandLineBinding": "inputs.x.inputBinding.", "CommandOutputParameter": "outputs.o.",
    "CommandOutputBinding": "outputs.o.outputBinding.",
}


def _subject(cwl_class: str, name: str) -> str | None:
    """What a refusal of the field `name` of `cwl_class` names, in `_documents()`; `None` for a construct of the
    workflow's or a step's own."""
    if cwl_class in PORT_SUBJECTS:
        return PORT_SUBJECTS[cwl_class]
    if cwl_class in TOOL_PATHS:
        return f"T.cwl: {TOOL_PATHS[cwl_class]}{name}"
    if cwl_class in REQUIREMENT_CLASSES["CommandLineTool"]:
        return f"T.cwl: requirements.{cwl_class}.{name}"
    return None


def _refused(workflow: Yaml, run: Yaml, note: str) -> list[Refusal]:
    """The refusals of the lowering of `workflow` whose reason ends with `note`."""
    with pytest.raises(NotLowered) as raised:
        _lower(workflow, run)
    return [refusal for refusal in raised.value.refusals if refusal.reason.endswith(note)]


def _constructs(cwl_class: str, name: str) -> set[str]:
    """The constructs the join says a refusal of the field `name` of `cwl_class` names: its most specific rows'."""
    specific = {construct for construct, rows in CONSTRUCTS.items() if (cwl_class, name) in rows}
    return specific or {construct for construct, rows in CONSTRUCTS.items() if (cwl_class, None) in rows}


def _lower(workflow: Yaml, run: Yaml, **values: Any) -> Any:
    return lowered_case({"workflow": workflow, "tools": [run], "job": {"x": ["hi"], "xs": ["a", "b"], **values}})[0]


# The compiler refuses these itself (wic038), so no compiled workflow reaches the lowering with them.
COMPILER_REFUSED = {("WorkflowOutputParameter", "linkMerge"), ("WorkflowOutputParameter", "pickValue")}
REJECTED = [
    (cwl_class, name) for cwl_class, rows in FIELDS.items() for name, row in rows.items()
    if row.state is State.REJECTED and (cwl_class, name) not in COMPILER_REFUSED
]


@pytest.mark.fast
@pytest.mark.parametrize(("cwl_class", "name"), REJECTED, ids=[f"{c}.{n}" for c, n in REJECTED])
def test_a_rejected_field_is_a_finding_whose_message_is_its_note(cwl_class: str, name: str) -> None:
    """One refusal carries the row's note, is of a construct the join gives the row, and names the field's place."""
    workflow, run = _documents()
    _position(cwl_class)(workflow, run)[name] = "x"
    refused = _refused(workflow, run, FIELDS[cwl_class][name].note)
    assert [(refusal.construct in _constructs(cwl_class, name), refusal.subject)
            for refusal in refused] == [(True, _subject(cwl_class, name))], refused


SAMPLES = {
    "doc": "documentation", "label": "a label", "intent": ["http://edamontology.org/operation_0004"],
    "format": "http://edamontology.org/format_1929", "streamable": True,
}


@pytest.mark.fast
def test_compile_time_fields_leave_the_executable_model_unchanged() -> None:
    workflow, run = _documents()
    # A File input, so its job value carries `format` too: a list, as a `.wic` file's `!ii` writes it.
    run["inputs"]["f"] = {"type": "File", "inputBinding": {"position": 3}}
    workflow["inputs"]["f"] = {"type": "File"}
    workflow["steps"][0]["in"]["f"] = {"wic_raw_cwl": {"source": "f"}}
    plain = _lower(copy.deepcopy(workflow), copy.deepcopy(run), f={"class": "File", "path": "f.txt"})
    for cwl_class, place in POSITIONS.items():
        place(workflow, run).update({name: SAMPLES[name] for name in COMPILE_TIME & set(FIELDS[cwl_class])})
    run["requirements"]["NetworkAccess"] = {"networkAccess": True}
    run.update({"$namespaces": {"edam": "https://edamontology.org/"}, "$schemas": ["https://example.org/formats.rdf"]})
    run["hints"] = [{"class": "NetworkAccess", "networkAccess": True}]
    value = {"class": "File", "location": "f.txt", "format": [SAMPLES["format"]]}
    assert _lower(workflow, run, f=value) == plain


@pytest.mark.fast
@pytest.mark.parametrize("section", ["requirements", "hints"])
@pytest.mark.parametrize("value", [False, "$(inputs.x)", None])
def test_network_access_other_than_the_literal_true_is_rejected_by_name(section: str, value: Any) -> None:
    workflow, run = _documents()
    run[section] = {"NetworkAccess": {} if value is None else {"networkAccess": value}}
    assert [(refusal.construct, refusal.subject) for refusal in _refused(workflow, run, "only the literal true is "
            "supported: a Nextflow task has network access, and the generated pipeline cannot withhold it")] == [
        (Construct.TOOL, f"T.cwl: {section}.NetworkAccess.networkAccess")]


REJECTED_CLASSES = sorted(
    name for name, row in REQUIREMENT_CLASSES["CommandLineTool"].items() if row.state is State.REJECTED
)


@pytest.mark.fast
@pytest.mark.parametrize("section", ["requirements", "hints"])
@pytest.mark.parametrize("name", REJECTED_CLASSES)
def test_a_rejected_requirement_class_is_a_finding_whose_message_is_its_note(section: str, name: str) -> None:
    workflow, run = _documents()
    run[section] = {name: {}}
    refused = _refused(workflow, run, REQUIREMENT_CLASSES["CommandLineTool"][name].note)
    expected = [(Construct.TOOL, f"T.cwl: {section}.{name}")]
    assert [(refusal.construct, refusal.subject) for refusal in refused] == expected
