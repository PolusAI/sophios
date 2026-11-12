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


def _declared_fields(cwl_class: str) -> set[str]:
    """The fields cwl_utils declares for a CWL class, or cwltool's extension schema for a `cwltool:` one."""
    if cwl_class.startswith("cwltool:"):
        schema = yaml.safe_load(files("cwltool").joinpath("extensions-v1.2.yml").read_text("utf-8"))
        [record] = [item for item in schema["$graph"] if item.get("name") == cwl_class.removeprefix("cwltool:")]
        return set(record["fields"])
    return set(getattr(cwl_v1_2, cwl_class).attrs)


# Design §3 names the compile-time fields: these wherever CWL declares them, and NetworkAccess.
COMPILE_TIME = {"doc", "format", "intent", "label", "streamable"}


@pytest.mark.fast
def test_the_table_classifies_every_field_cwl_declares_under_a_construct() -> None:
    """Each class's rows are the fields cwl_utils or cwltool declares, Sophios's own classes' as the support matrix
    has them; each requirement class is a CWL class, with its fields unless it is refused; every field stands under a
    construct a refusal names (resolved-view §7); and the compile-time fields are the ones design §3 names."""
    assert {cwl_class: set(FIELDS[cwl_class]) for cwl_class in SUPPORT_MATRIX} == {
        cwl_class: set(fields) for cwl_class, fields in SUPPORT_MATRIX.items()}
    assert {cwl_class: set(rows) for cwl_class, rows in FIELDS.items()} == {
        cwl_class: _declared_fields(cwl_class) for cwl_class in FIELDS}
    for classes in REQUIREMENT_CLASSES.values():
        for name, row in classes.items():
            assert _declared_fields(name) and (row.state is State.REJECTED or name in FIELDS), name
    assert set(CONSTRUCTS) == {construct.value for construct in Construct}
    rows = {row for joined in CONSTRUCTS.values() for row in joined}
    assert {cwl_class for cwl_class, _name in rows} <= set(FIELDS)
    assert [(cwl_class, name) for cwl_class, fields in FIELDS.items() for name in fields
            if (cwl_class, None) not in rows and (cwl_class, name) not in rows] == []
    assert {(cwl_class, name) for cwl_class, fields in FIELDS.items() for name, row in fields.items()
            if row.state is State.COMPILE_TIME} == {
        (cwl_class, name) for cwl_class, fields in FIELDS.items() for name in fields
        if name in COMPILE_TIME or cwl_class == "NetworkAccess"}


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


REJECTED_CLASSES = sorted(
    name for name, row in REQUIREMENT_CLASSES["CommandLineTool"].items() if row.state is State.REJECTED
)
_WITHHELD = "only the literal true is supported: a Nextflow task has network access, and the generated pipeline " \
    "cannot withhold it"


def _refused_as_its_row_says(place: Callable[[Yaml, Yaml], None], note: str,
                             constructs: set[str]) -> list[tuple[bool, str | None]]:
    """The construct and subject of each refusal carrying `note`, when `place` puts a value in `_documents()`."""
    workflow, run = _documents()
    place(workflow, run)
    return [(refusal.construct in constructs, refusal.subject) for refusal in _refused(workflow, run, note)]


def _field(cwl_class: str, name: str) -> Callable[[Yaml, Yaml], None]:
    """Set the field `name` of `cwl_class` where `_documents()` holds it."""
    def place(workflow: Yaml, run: Yaml) -> None:
        _position(cwl_class)(workflow, run)[name] = "x"
    return place


def _section(section: str, requirements: Yaml) -> Callable[[Yaml, Yaml], None]:
    """Give the tool `requirements` as its `section`."""
    def place(_workflow: Yaml, run: Yaml) -> None:
        run[section] = requirements
    return place


@pytest.mark.fast
def test_a_refused_row_is_refused_with_its_note_naming_its_place() -> None:
    """Each refused field, refused requirement class and NetworkAccess other than the literal true, set in a workflow
    that lowers without it, is one refusal carrying its row's note, of a construct the join gives the row, naming the
    place it was set: the port it sits on, or in the tool the path to it."""
    expected: dict[str, list[tuple[bool, str | None]]] = {}
    found: dict[str, list[tuple[bool, str | None]]] = {}
    for cwl_class, name in REJECTED:
        expected[f"{cwl_class}.{name}"] = [(True, _subject(cwl_class, name))]
        found[f"{cwl_class}.{name}"] = _refused_as_its_row_says(
            _field(cwl_class, name), FIELDS[cwl_class][name].note, _constructs(cwl_class, name))
    for section in ("requirements", "hints"):
        for name in REJECTED_CLASSES:
            expected[f"{section}.{name}"] = [(True, f"T.cwl: {section}.{name}")]
            found[f"{section}.{name}"] = _refused_as_its_row_says(
                _section(section, {name: {}}), REQUIREMENT_CLASSES["CommandLineTool"][name].note, {Construct.TOOL})
        for value in (False, "$(inputs.x)", None):
            expected[f"{section}.NetworkAccess={value!r}"] = [(True, f"T.cwl: {section}.NetworkAccess.networkAccess")]
            found[f"{section}.NetworkAccess={value!r}"] = _refused_as_its_row_says(
                _section(section, {"NetworkAccess": {} if value is None else {"networkAccess": value}}), _WITHHELD,
                {Construct.TOOL})
    assert found == expected
