from pathlib import Path

import pytest
import yaml

import sophios.apis.python.cwl_builder as cwl_builder
from sophios.apis.python.cwl_builder import (
    CommandLineToolBuilder,
    Dirent,
    Field,
    Input,
    Output,
    Type,
    secondary_file,
)


def _rich_builder() -> CommandLineToolBuilder:
    mode_type = Type.enum("fast", "accurate", name="Mode")
    settings_type = Type.record(
        {
            "threads": Field.int(),
            "preset": Field.of(mode_type),
            "tags": Field.array(Type.string()),
        },
        name="Settings",
    )

    return (
        CommandLineToolBuilder("aligner")
        .label("Align reads")
        .doc(["Toy CLT", "for serialization coverage"])
        .namespace("edam", "https://edamontology.org/")
        .schema("https://example.org/formats.rdf")
        .intent("edam:operation_3198")
        .base_command("bash", "-lc")
        .shell_command()
        .inline_javascript("function passthrough(x) { return x; }")
        .schema_definitions(mode_type, settings_type)
        .docker(docker_pull="alpine:3.20")
        .resources(cores_min=1.5, ram_min=1024, outdir_min=256)
        .env_var("LC_ALL", "C")
        .initial_workdir([Dirent("threads=4\n", entryname="config.txt")])
        .work_reuse(False, as_hint=True)
        .network_access(False)
        .argument("run-aligner", position=0)
        .inputs(
            reads=Input.array(
                Type.file(),
                prefix="--reads",
                format="edam:format_2572",
                secondary_files=[secondary_file(".bai", required=False)],
            ),
            mode=Input.of(mode_type, prefix="--mode"),
            settings=Input.of(settings_type, load_listing="shallow_listing"),
        )
        .outputs(sam=Output.stdout())
        .stdout("aligned.sam")
        .success_codes(0, 2)
    )


@pytest.mark.fast
def test_cwl_builder_covers_common_clt_surface() -> None:
    tool = _rich_builder().to_dict()

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
    tool = (
        CommandLineToolBuilder("custom-tool")
        .inputs(message=Input.string())
        .outputs(out=Output.file(glob="out.txt"))
        .time_limit(60)
        .extra(sbol_intent="example:custom", customExtension={"enabled": True})
        .to_dict()
    )

    assert tool["requirements"]["ToolTimeLimit"] == {"timelimit": 60}
    assert tool["sbol_intent"] == "example:custom"
    assert tool["customExtension"] == {"enabled": True}


@pytest.mark.fast
def test_cwl_builder_save_round_trips_yaml(tmp_path: Path) -> None:
    builder = _rich_builder()
    output_path = tmp_path / "aligner.cwl"

    saved_path = builder.save(output_path)

    assert saved_path == output_path
    assert yaml.safe_load(output_path.read_text(encoding="utf-8")) == builder.to_dict()


@pytest.mark.fast
def test_cwl_builder_validate_uses_cwltool_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLoadTool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def fetch_document(self, path: str) -> tuple[str, dict[str, str], str]:
            self.calls.append(("fetch_document", Path(path).suffix))
            return "loading-context", {"class": "CommandLineTool"}, "file:///aligner.cwl"

        def resolve_and_validate_document(
            self,
            loading_context: str,
            workflowobj: dict[str, str],
            uri: str,
            preprocess_only: bool = False,
        ) -> tuple[str, str]:
            self.calls.append(("resolve_and_validate_document", preprocess_only))
            assert loading_context == "loading-context"
            assert workflowobj == {"class": "CommandLineTool"}
            assert uri == "file:///aligner.cwl"
            return "validated-context", "file:///validated-aligner.cwl"

        def make_tool(self, uri: str, loading_context: str) -> dict[str, str]:
            self.calls.append(("make_tool", uri))
            assert loading_context == "validated-context"
            return {"uri": uri, "loading_context": loading_context}

    fake_load_tool = FakeLoadTool()
    monkeypatch.setattr(cwl_builder, "_import_cwltool_load_tool", lambda: fake_load_tool)

    result = _rich_builder().validate()

    assert result.uri == "file:///validated-aligner.cwl"
    assert result.process == {
        "uri": "file:///validated-aligner.cwl",
        "loading_context": "validated-context",
    }
    assert [name for name, _ in fake_load_tool.calls] == [
        "fetch_document",
        "resolve_and_validate_document",
        "make_tool",
    ]
