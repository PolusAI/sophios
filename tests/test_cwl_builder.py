from pathlib import Path

import pytest
import yaml

import sophios.apis.python._cwl_builder_support as cwl_builder_support
from sophios.apis.python.cwl_builder import (
    CommandLineTool,
    Dirent,
    Field,
    Input,
    Inputs,
    Output,
    Outputs,
    cwl,
    secondary_file,
)
from sophios.apis.python.api import Step


def _rich_tool() -> CommandLineTool:
    mode_type = cwl.enum("fast", "accurate", name="Mode")
    settings_type = cwl.record(
        {
            "threads": Field(cwl.int),
            "preset": Field(mode_type),
            "tags": Field.array(cwl.string),
        },
        name="Settings",
    )
    inputs = Inputs(
        reads=Input.array(cwl.file, flag="--reads")
        .format("edam:format_2572")
        .secondary_files(secondary_file(".bai", required=False)),
        mode=Input(mode_type, flag="--mode"),
        settings=Input(settings_type).load_listing("shallow_listing"),
    )
    outputs = Outputs(sam=Output.stdout())

    return (
        CommandLineTool("aligner", inputs, outputs)
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


@pytest.mark.fast
def test_cwl_builder_requires_structural_core() -> None:
    with pytest.raises(TypeError):
        CommandLineTool("missing-inputs")  # type: ignore[call-arg]


@pytest.mark.fast
def test_cwl_builder_covers_common_clt_surface() -> None:
    tool = _rich_tool().to_dict()

    assert tool["$namespaces"] == {"edam": "https://edamontology.org/"}
    assert tool["$schemas"] == ["https://example.org/formats.rdf"]
    assert tool["intent"] == ["edam:operation_3198"]
    assert tool["baseCommand"] == ["bash", "-lc"]
    assert tool["arguments"] == [{"position": 0, "valueFrom": "run-aligner"}]
    assert tool["stdout"] == "aligned.sam"
    assert tool["successCodes"] == [0, 2]
    assert tool["inputs"]["reads"]["secondaryFiles"] == [{"pattern": ".bai", "required": False}]
    assert tool["inputs"]["settings"]["loadListing"] == "shallow_listing"
    assert tool["outputs"]["sam"]["type"] == "stdout"
    assert tool["requirements"]["ShellCommandRequirement"] == {}
    assert tool["requirements"]["DockerRequirement"] == {"dockerPull": "alpine:3.20"}
    assert tool["requirements"]["ResourceRequirement"] == {
        "coresMin": 1.5,
        "ramMin": 1024,
        "outdirMin": 256,
    }
    assert tool["requirements"]["EnvVarRequirement"] == {
        "envDef": [{"envName": "LC_ALL", "envValue": "C"}]
    }
    assert tool["requirements"]["InitialWorkDirRequirement"] == {
        "listing": [{"entry": "threads=4\n", "entryname": "config.txt"}]
    }
    assert tool["requirements"]["NetworkAccess"] == {"networkAccess": False}
    assert tool["requirements"]["InlineJavascriptRequirement"] == {
        "expressionLib": ["function passthrough(x) { return x; }"]
    }
    assert len(tool["requirements"]["SchemaDefRequirement"]["types"]) == 2
    assert tool["hints"]["WorkReuse"] == {"enableReuse": False}


@pytest.mark.fast
def test_cwl_builder_accepts_raw_extensions() -> None:
    tool = CommandLineTool(
        "custom-tool",
        Inputs(message=Input(cwl.string)),
        Outputs(out=Output(cwl.file, glob="out.txt")),
    )

    with pytest.warns(UserWarning, match="raw CWL injection"):
        rendered = tool.time_limit(60).extra(sbol_intent="example:custom", customExtension={"enabled": True}).to_dict()

    assert rendered["requirements"]["ToolTimeLimit"] == {"timelimit": 60}
    assert rendered["sbol_intent"] == "example:custom"
    assert rendered["customExtension"] == {"enabled": True}


@pytest.mark.fast
def test_cwl_builder_rejects_reserved_or_salad_raw_keys() -> None:
    tool = CommandLineTool(
        "custom-tool",
        Inputs(message=Input(cwl.string)),
        Outputs(out=Output(cwl.file, glob="out.txt")),
    )

    with pytest.raises(ValueError, match="builder-managed keys"):
        tool.extra(inputs={"bad": "idea"})

    with pytest.raises(ValueError, match="SALAD document-assembly keys"):
        tool.requirement({"class": "EnvVarRequirement", "$import": "bad"})


@pytest.mark.fast
def test_cwl_builder_high_level_helpers_hide_cwl_plumbing() -> None:
    inputs = Inputs(
        input=Input(cwl.directory, position=1).label("Input Zarr dataset").doc("Path to input zarr dataset"),
        output=Input(cwl.directory, position=2).label("Output segmentation Zarr").doc(
            "Path for output segmentation zarr"
        ),
        model=Input(cwl.file, flag="--model", required=False).label("Model override file"),
        tile_size=Input(cwl.int, flag="--tile-size", required=False).label("Tile size"),
        iou_threshold=Input(cwl.float, flag="--iou-threshold", required=False).label("IoU threshold"),
    )
    outputs = Outputs(output=Output(cwl.directory, from_input=inputs.output).label("Output segmentation Zarr"))
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
        .to_dict()
    )

    assert tool["$namespaces"]["edam"] == "https://edamontology.org/"
    assert tool["$namespaces"]["cwltool"] == "http://commonwl.org/cwltool#"
    assert tool["$schemas"] == [
        "https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl"
    ]
    assert tool["hints"]["cwltool:CUDARequirement"] == {
        "cudaVersionMin": "11.7",
        "cudaComputeCapability": "3.0",
        "cudaDeviceCountMin": 2,
    }
    assert tool["requirements"]["ResourceRequirement"] == {"coresMin": 4, "ramMin": 64000}
    assert tool["requirements"]["InitialWorkDirRequirement"] == {
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
    assert tool["requirements"]["InlineJavascriptRequirement"] == {}
    assert tool["inputs"]["input"]["inputBinding"] == {"position": 1}
    assert tool["inputs"]["model"]["type"] == ["null", "File"]
    assert tool["inputs"]["model"]["inputBinding"] == {"prefix": "--model"}
    assert tool["inputs"]["tile_size"]["type"] == ["null", "int"]
    assert tool["outputs"]["output"]["outputBinding"] == {"glob": "$(inputs.output.basename)"}


@pytest.mark.fast
def test_cwl_builder_save_round_trips_yaml(tmp_path: Path) -> None:
    tool = _rich_tool()
    output_path = tmp_path / "aligner.cwl"

    saved_path = tool.save(output_path)

    assert saved_path == output_path
    assert yaml.safe_load(output_path.read_text(encoding="utf-8")) == tool.to_dict()


@pytest.mark.fast
def test_cwl_builder_validate_uses_cwltool_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRuntimeContext:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs

    class FakeLoadTool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def fetch_document(self, path: str, loading_context: str) -> tuple[str, dict[str, str], str]:
            self.calls.append(("fetch_document", (Path(path).suffix, loading_context)))
            return loading_context, {"class": "CommandLineTool"}, "file:///aligner.cwl"

        def resolve_and_validate_document(
            self,
            loading_context: str,
            workflowobj: dict[str, str],
            uri: str,
            preprocess_only: bool = False,
        ) -> tuple[str, str]:
            self.calls.append(("resolve_and_validate_document", preprocess_only))
            assert loading_context == "prepared-context"
            assert workflowobj == {"class": "CommandLineTool"}
            assert uri == "file:///aligner.cwl"
            return "validated-context", "file:///validated-aligner.cwl"

        def make_tool(self, uri: str, loading_context: str) -> dict[str, str]:
            self.calls.append(("make_tool", uri))
            assert loading_context == "validated-context"
            return {"uri": uri, "loading_context": loading_context}

    def fake_get_default_args() -> dict[str, object]:
        return {
            "validate": False,
            "skip_schemas": False,
            "workflow": None,
            "do_validate": True,
        }

    def fake_setup_loading_context(
        loading_context: None,
        runtime_context: FakeRuntimeContext,
        args: object,
    ) -> str:
        assert loading_context is None
        assert Path(str(runtime_context.kwargs["workflow"])).name == "aligner.cwl"
        assert runtime_context.kwargs["validate"] is True
        assert runtime_context.kwargs["skip_schemas"] is False
        assert Path(str(getattr(args, "workflow"))).name == "aligner.cwl"
        assert getattr(args, "validate") is True
        assert getattr(args, "skip_schemas") is False
        return "prepared-context"

    fake_load_tool = FakeLoadTool()
    monkeypatch.setattr(cwl_builder_support, "_import_cwltool_load_tool", lambda: fake_load_tool)
    monkeypatch.setattr(
        cwl_builder_support,
        "_import_cwltool_validation_support",
        lambda: (FakeRuntimeContext, fake_get_default_args, fake_setup_loading_context),
    )

    result = _rich_tool().validate()

    assert result.uri == "file:///validated-aligner.cwl"
    assert result.process == {
        "uri": "file:///validated-aligner.cwl",
        "loading_context": "validated-context",
    }


@pytest.mark.fast
def test_cwl_builder_converts_to_in_memory_step() -> None:
    tool = CommandLineTool(
        "echo_tool",
        Inputs(message=Input(cwl.string, position=1)),
        Outputs(out=Output.stdout()),
    ).stdout("stdout.txt")

    step = tool.to_step(step_name="say_hello")
    step.inputs.message = "hello"

    assert isinstance(step, Step)
    assert step.process_name == "say_hello"
    assert step.clt_path.name == "say_hello.cwl"
    assert step.yaml["inputs"]["message"]["type"] == "string"
    assert step.yaml["outputs"]["out"]["type"] == "stdout"
    assert step._yml["in"]["message"] == {"wic_inline_input": "hello"}
