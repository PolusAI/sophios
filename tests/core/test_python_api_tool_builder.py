import keyword
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import example, given, strategies as st
from schema_salad.utils import yaml_no_ts  # pylint: disable=no-name-in-module

from sophios.api.python._ports import ParameterNamespace
from sophios.api.python.tool_builder import (
    CommandLineTool,
    Dirent,
    Field,
    Fields,
    Input,
    Inputs,
    Output,
    Outputs,
    cwl,
    secondary_file,
)
from sophios.api.python.workflow import Step, Workflow

from .budgets import budget
from .cases import DATA
from .strategies import author_names, pinned


def _rich_tool(default: str) -> CommandLineTool:
    """A tool exercising every builder method that reaches the document; `default` is its `message`'s."""
    mode_type = cwl.enum("fast", "accurate", name="Mode")
    settings_type = cwl.record(
        Fields(
            threads=Field(cwl.int),
            preset=Field(mode_type),
            tags=Field.array(cwl.string),
        ),
        name="Settings",
    )
    inputs = Inputs(
        reads=Input.array(cwl.file, flag="--reads")
        .format("edam:format_2572")
        .secondary_files(secondary_file(".bai", required=False)),
        mode=Input(mode_type, flag="--mode"),
        settings=Input(settings_type).load_listing("shallow_listing"),
        message=Input(cwl.string, default=default),
        model=Input(cwl.file, flag="--model", required=False),
    )
    return (
        CommandLineTool("tools/aligner", inputs, Outputs(sam=Output.stdout()))
        .label("Align reads")
        .doc(["Toy CLT", "for serialization coverage"])
        .namespace("edam")
        .schema("https://example.org/formats.rdf")
        .intent("edam:operation_3198")
        .base_command("bash", "-lc")
        .shell_command()
        .inline_javascript("function passthrough(x) { return x; }")
        .schema_definitions(mode_type, settings_type)
        .docker("alpine:3.20")
        .resources(cores_min=1.5, ram_min=1024, outdir_min=256)
        .env_var("LC_ALL", "C")
        .initial_workdir([Dirent("threads=4\n", entryname="config.txt")])
        .work_reuse(False, as_hint=True)
        .network_access(False)
        .argument("run-aligner", position=0)
        .stdout("aligned.sam")
        .success_codes(0, 2)
        .time_limit(60)
        .extra(sbol_intent="example:custom", customExtension={"enabled": True})
    )


_PORTS = {Inputs: (Input(cwl.string), "inputs", "string"),
          Outputs: (Output(cwl.file, glob="o"), "outputs", {"type": "File", "outputBinding": {"glob": "o"}}),
          Fields: (Field(cwl.string), "", "")}
_ATTRIBUTES = sorted({*dir(ParameterNamespace), *dir(Inputs), *dir(Outputs), *dir(Fields)})


@pytest.mark.fast
@given(st.tuples(st.sampled_from(list(_PORTS)), author_names | st.sampled_from(_ATTRIBUTES)))
@pinned(
    (Inputs, "input-file"), (Fields, "class"), (Inputs, "items"), (Outputs, "to_dict"), (Fields, "to_dict"),
    (Inputs, "_items"), (Fields, "_store"),
    *((collection, n) for collection in (Inputs, Outputs)
      for n in sorted({*dir(ParameterNamespace), *dir(Inputs), "_", "_x", "_Out", "__x", "__x__"})))
@budget(200)
def test_a_port_name_is_accepted_when_python_can_write_and_reach_it(case: tuple[Any, str]) -> None:
    """Ports are reached as `inputs.reads`, so a name like `input-file` would build a tool nobody can reference:
    refused where it is written. `Inputs` and friends are mappings with methods, so a port called `items`,
    `to_dict` or `_items` would make `inputs.items` ambiguous. A tool's port is also an attribute of each step's
    port namespace, so the builder refuses, where the name is written, each name a step refuses, such as `_store`.
    A record field is no port, so it may be called `_store`. For `Inputs` and `Outputs` the builder refuses a
    writable name exactly when a step made from a tool document declaring that port refuses it, or when the name is
    one of the collection's own attributes, such as `keys`, which a step accepts."""
    collection, name = case
    port, key, declared = _PORTS[collection]
    owners = (collection,) if collection is Fields else (collection, ParameterNamespace)
    writable = name.isidentifier() and not keyword.iskeyword(name)
    reachable = not (name.startswith("__") and name.endswith("__") and len(name) > 4) and not any(
        name in dir(owner) for owner in owners)
    try:
        built = collection(**{name: port})
    except ValueError as error:
        assert not (writable and reachable) and ("reserved" if writable else "valid Python") in str(error), name
        refused = True
    else:
        assert writable and reachable and getattr(built, name) is built[name], name
        refused = False
    if key and writable:
        try:
            Step.from_cwl_document({"cwlVersion": "v1.2", "class": "CommandLineTool", "baseCommand": "true",
                                    "inputs": {}, "outputs": {}, key: {name: declared}})
        except ValueError:
            assert refused, name
        else:
            assert refused == (name in dir(collection)), name


@pytest.mark.fast
@pytest.mark.parametrize("text", ["1e3", "._", "._5", "0o17"])
def test_a_built_tool_is_the_document_stated_and_writes_what_reads_back_as_built(tmp_path: Path, text: str) -> None:
    """Every builder method reaches the document tests/core/data/python_api/aligner.cwl states, for its default `1e3`;
    and what `write_cwl` writes loads back as built, by PyYAML and by cwltool's YAML 1.2, which would retype each of
    these texts. `extra` warns, since what it adds bypasses the builder's checks."""
    with pytest.warns(UserWarning, match="raw CWL injection"):
        tool = _rich_tool(text)
    stated = yaml.safe_load((DATA / "python_api" / "aligner.cwl").read_text(encoding="utf-8"))
    stated["inputs"][3]["default"] = text
    assert tool.to_cwl_document() == stated
    assert tool.write_cwl(tmp_path / "aligner.cwl") == tmp_path / "aligner.cwl"
    written = (tmp_path / "aligner.cwl").read_text(encoding="utf-8")
    assert yaml.safe_load(written) == yaml_no_ts().load(written) == stated


#: A tool's own shorthands for a File, which cwltool refuses at a workflow's boundary.
_FILE_SHORTHANDS = ('stdout', 'stderr', 'stdin')


@pytest.mark.fast
@given(st.lists(st.sampled_from(['stdout', 'stderr', 'File', 'string']), min_size=1, max_size=3), st.booleans(),
       st.sampled_from(['none', 'unbound', 'bound']))
@example(['stdout', 'stderr'], True, 'none')
@example(['stdout', 'stderr'], False, 'none')  # docs/tool-builder.md hands such a tool to the workflow API
@example(['stdout'], True, 'unbound')
@example(['stdout'], True, 'bound')  # bound to a workflow input, that input must not say `stdin`
@budget(30)
def test_a_shorthand_port_reaches_the_workflow_as_a_file(types: list[str], named: bool, stdin: str) -> None:
    """Each workflow port a tool port reaches, named, bound or promoted, has its type, `File` for a shorthand; the
    tool keeps its own."""
    outputs = Outputs(**{f"o{i}": Output.stdout() if declared == "stdout" else Output.stderr()
                         if declared == "stderr" else Output(declared, glob="o") for i, declared in enumerate(types)})
    tool = CommandLineTool("tool", Inputs(**({"src": Input("stdin")} if stdin != "none" else {})), outputs).stdout(
        "out.txt").stderr("err.txt")
    step = tool.to_step(step_name="s")
    workflow = Workflow([step], "wf")
    if named:
        for index in range(len(types)):
            setattr(workflow.outputs, f"w{index}", getattr(step.outputs, f"o{index}"))
    if stdin == "bound":
        step.inputs.src = workflow.inputs.data
    compiled = workflow.compile().cwl_workflow

    def wanted(declared: str) -> str:
        return "File" if declared in _FILE_SHORTHANDS else declared
    assert {name: spec["type"] for name, spec in compiled["outputs"].items()} == {
        f"w{index}" if named else f"s~o{index}": wanted(declared) for index, declared in enumerate(types)}
    assert [spec["type"] for spec in compiled["inputs"].values()] == (["File"] if stdin != "none" else [])
    assert "data" in compiled["inputs"] or stdin != "bound"
    assert [output["type"] for output in compiled["steps"][0]["run"]["outputs"]] == types
