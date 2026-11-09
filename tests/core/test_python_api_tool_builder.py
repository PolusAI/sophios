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
from sophios.lang.cwl import CWL_VERSION

from .budgets import budget
from .strategies import author_names, pinned


def _rich_tool(default: str = "hello") -> CommandLineTool:
    """A tool exercising every builder method that reaches the document, so
    one assertion block covers the whole serialization surface; `default` is its `message`'s."""
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
    )
    return (
        CommandLineTool("aligner", inputs, Outputs(sam=Output.stdout()))
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
def test_tool_name_with_slash_keeps_document_ids() -> None:
    """A `/` in the tool name is an id, not a URI base for other fields."""
    tool = CommandLineTool(
        "tools/align",
        Inputs(message=Input(cwl.string)),
        Outputs(out=Output.stdout()),
    ).to_cwl_document()
    assert tool["class"] == "CommandLineTool"
    assert tool["cwlVersion"] == CWL_VERSION
    assert tool["id"] == "tools/align"


@pytest.mark.fast
def test_structured_port_references_do_not_accept_raw_strings() -> None:
    """A port reference is the port object, never its name as text.

    A string cannot be checked against the tool it came from, so a typo would
    survive to the emitted document and fail at runtime. The type is the check.
    """
    tool = CommandLineTool(
        "demo",
        Inputs(input=Input(cwl.file)),
        Outputs(output=Output(cwl.file, glob="out.txt")),
    )
    with pytest.raises(TypeError, match="named Input/Output object"):
        tool.stage("input")


def _by_class(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a cwl_utils requirements/hints list by its `class` key."""
    return {entry["class"]: {k: v for k, v in entry.items() if k != "class"} for entry in entries}


def _by_id(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a cwl_utils inputs/outputs list by its `id` key."""
    return {entry["id"]: {k: v for k, v in entry.items() if k != "id"} for entry in entries}


@pytest.mark.fast
def test_tool_builder_covers_common_clt_surface() -> None:
    """Every builder method reaches the emitted document.

    One tool, one assertion block: a method that silently dropped its argument
    would otherwise pass every test that never inspects the document.
    """
    tool = _rich_tool().to_cwl_document()
    inputs = _by_id(tool["inputs"])
    outputs = _by_id(tool["outputs"])
    requirements = _by_class(tool["requirements"])
    hints = _by_class(tool["hints"])

    assert tool["$namespaces"] == {"edam": "https://edamontology.org/"}
    assert tool["$schemas"] == ["https://example.org/formats.rdf"]
    assert tool["intent"] == ["edam:operation_3198"]
    assert tool["baseCommand"] == ["bash", "-lc"]
    assert tool["arguments"] == [{"position": 0, "valueFrom": "run-aligner"}]
    assert tool["stdout"] == "aligned.sam"
    assert tool["successCodes"] == [0, 2]
    assert inputs["reads"]["secondaryFiles"] == [{"pattern": ".bai", "required": False}]
    assert inputs["settings"]["loadListing"] == "shallow_listing"
    assert outputs["sam"]["type"] == "stdout"
    assert requirements["ShellCommandRequirement"] == {}
    assert requirements["DockerRequirement"] == {"dockerPull": "alpine:3.20"}
    assert requirements["ResourceRequirement"] == {
        "coresMin": 1.5,
        "ramMin": 1024,
        "outdirMin": 256,
    }
    assert requirements["EnvVarRequirement"] == {
        "envDef": [{"envName": "LC_ALL", "envValue": "C"}]
    }
    assert requirements["InitialWorkDirRequirement"] == {
        "listing": [{"entry": "threads=4\n", "entryname": "config.txt"}]
    }
    assert requirements["NetworkAccess"] == {"networkAccess": False}
    assert requirements["InlineJavascriptRequirement"] == {
        "expressionLib": ["function passthrough(x) { return x; }"]
    }
    assert len(requirements["SchemaDefRequirement"]["types"]) == 2
    assert hints["WorkReuse"] == {"enableReuse": False}


def _custom_tool() -> CommandLineTool:
    """A small tool for the raw-extension tests."""
    return CommandLineTool("custom-tool", Inputs(message=Input(cwl.string)),
                           Outputs(out=Output(cwl.file, glob="out.txt")))


@pytest.mark.fast
def test_tool_builder_accepts_raw_extensions() -> None:
    """Unmodelled CWL can be injected, and says so when it is.

    `extra` is the escape hatch for vendor keys the builder does not model. It
    warns because content that bypasses the builder also bypasses its checks.
    """
    with pytest.warns(UserWarning, match="raw CWL injection"):
        rendered = _custom_tool().time_limit(60).extra(
            sbol_intent="example:custom", customExtension={"enabled": True}).to_cwl_document()

    assert _by_class(rendered["requirements"])["ToolTimeLimit"] == {"timelimit": 60}
    assert rendered["sbol_intent"] == "example:custom"
    assert rendered["customExtension"] == {"enabled": True}


@pytest.mark.fast
def test_tool_builder_rejects_reserved_or_salad_raw_keys() -> None:
    """The escape hatch may not overwrite the document or the loader.

    Raw injection of `inputs` would silently discard what the builder computed,
    and a SALAD assembly key like `$import` changes how the document is loaded
    rather than what it says.
    """
    tool = _custom_tool()
    with pytest.raises(ValueError, match="builder-managed keys"):
        tool.extra(inputs={"bad": "idea"})

    with pytest.raises(ValueError, match="SALAD document-assembly keys"):
        tool.requirement({"class": "EnvVarRequirement", "$import": "bad"})


@pytest.mark.fast
def test_tool_builder_high_level_helpers_hide_cwl_plumbing() -> None:
    """The helpers produce the CWL a hand-written tool would.

    `edam`, `gpu`, `stage` and `resources` each stand for a block of requirement
    plumbing; this pins what each expands to, including that `stage` preserves
    call order and its `writable` flag per entry.
    """
    inputs = Inputs(
        input=Input(cwl.directory, position=1).label("Input Zarr dataset").doc("Path to input zarr dataset"),
        output=Input(cwl.directory, position=2).label("Output segmentation Zarr").doc(
            "Path for output segmentation zarr"
        ),
        model=Input(cwl.file, flag="--model", required=False).label("Model override file"),
        tile_size=Input(cwl.int, flag="--tile-size", required=False).label("Tile size"),
        iou_threshold=Input(cwl.float, flag="--iou-threshold", required=False).label("IoU threshold"),
    )
    outputs = Outputs(output=Output(cwl.directory, glob="$(inputs.output.basename)").label("Output segmentation Zarr"))
    tool = (
        CommandLineTool("sam3", inputs, outputs)
        .describe("SAM3 OME Zarr autosegmentation", "Run SAM3 autosegmentation on a zarr volume.")
        .edam()
        .gpu(cuda_version_min="11.7", compute_capability="3.0", device_count_min=2)
        .docker("polusai/ichnaea-api:latest")
        .stage(inputs.output, writable=True)
        .stage(inputs.input)
        .resources(cores=4, ram=64000)
        .base_command("/backend/.venv/bin/python", "/backend/dagster_pipelines/jobs/autosegmentation/logic.py")
        .to_cwl_document()
    )
    emitted_inputs = _by_id(tool["inputs"])
    emitted_outputs = _by_id(tool["outputs"])
    requirements = _by_class(tool["requirements"])
    hints = _by_class(tool["hints"])

    assert tool["$namespaces"]["edam"] == "https://edamontology.org/"
    assert tool["$namespaces"]["cwltool"] == "http://commonwl.org/cwltool#"
    assert tool["$schemas"] == [
        "https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl"
    ]
    assert hints["cwltool:CUDARequirement"] == {
        "cudaVersionMin": "11.7",
        "cudaComputeCapability": "3.0",
        "cudaDeviceCountMin": 2,
    }
    assert requirements["ResourceRequirement"] == {"coresMin": 4, "ramMin": 64000}
    assert requirements["InitialWorkDirRequirement"] == {
        "listing": [
            {
                "entry": "$(inputs.output)",
                "entryname": "$(inputs.output.basename)",
                "writable": True,
            },
            {
                "entry": "$(inputs.input)",
                "entryname": "$(inputs.input.basename)",
                "writable": False,
            },
        ]
    }
    assert requirements["InlineJavascriptRequirement"] == {}
    assert emitted_inputs["input"]["inputBinding"] == {"position": 1}
    assert emitted_inputs["model"]["type"] == ["null", "File"]
    assert emitted_inputs["model"]["inputBinding"] == {"prefix": "--model"}
    assert emitted_inputs["tile_size"]["type"] == ["null", "int"]
    assert emitted_outputs["output"]["outputBinding"] == {"glob": "$(inputs.output.basename)"}


@pytest.mark.fast
@pytest.mark.parametrize("text", ["._", "._5", "1e3", "0o17"])
def test_what_write_cwl_writes_reads_back_as_built(tmp_path: Path, text: str) -> None:
    """What is written loads back as built, by PyYAML and by cwltool's YAML 1.2, which would retype these strings."""
    tool = _rich_tool(text)
    assert tool.write_cwl(tmp_path / "aligner.cwl") == tmp_path / "aligner.cwl"
    written = (tmp_path / "aligner.cwl").read_text(encoding="utf-8")
    assert yaml.safe_load(written) == yaml_no_ts().load(written) == tool.to_cwl_document()


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
