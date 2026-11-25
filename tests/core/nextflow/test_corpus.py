"""The corpus workflows the Nextflow target runs, compiled and run under Nextflow with Docker. Only
`.github/workflows/run_workflows.yml` collects these, on a push, with mm-workflows and image-workflows alongside."""
from pathlib import Path
from typing import Any

import pytest

from sophios import config as sophios_config
import sophios.plugins
from sophios.nextflow.reader import read_nextflow
from sophios.nextflow.model import NfWorkflowOutputConnection
from sophios.runtime.backends import recorded_outputs

from ..helpers import REPO_ROOT, python_process, python_workflow
from .testkit import execute_nextflow

CHECKOUTS = REPO_ROOT.parent
CONFIG = Path.home() / "wic" / "global_config.json"

# Every standalone workflow of image-workflows, and the standalone mm-workflows ones the target compiles.
WORKFLOWS = [
    "image-workflows/workflows/bbbc.wic",
    "image-workflows/workflows/bbbc.py",
    "image-workflows/workflows/bbbc_sub.py",
    "mm-workflows/examples/docking/molgan.wic",
    "mm-workflows/examples/docking/load_trained_molgan.wic",
]


def _compile(source: Path, pipeline: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write the Nextflow artifacts of one corpus workflow, from the command line or the Python API."""
    if source.suffix == ".wic":
        result = python_process("-m", "sophios.main", "--yaml", source, "--config_file", CONFIG, "--target", "nextflow",
                                "--basepath", pipeline, cwd=pipeline.parent, capture_output=True, timeout=900)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        return
    # A Python workflow names its tools relative to its repository, as run_workflows.yml compiles it.
    tools = sophios.plugins.get_tools_cwl(sophios_config.read_config_from_disk(CONFIG))
    monkeypatch.chdir(source.parents[1])
    python_workflow(source, tools).compile(target="nextflow", tool_registry=tools).write(pipeline)


def _recorded(value: Any) -> list[dict[str, str]]:
    """Each File or Directory a recorded output names, at any depth; a null names none."""
    if isinstance(value, list):
        return [item for element in value for item in _recorded(element)]
    return [] if value is None else [value]


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.nextflow_corpus
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_a_corpus_workflow_runs_under_nextflow(workflow: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run succeeds, every file a workflow output publishes exists, and every directory it publishes holds
    something, as the pipeline records them."""
    pipeline = tmp_path / "pipeline"
    _compile(CHECKOUTS / workflow, pipeline, monkeypatch)
    executable, _ = read_nextflow(pipeline)
    # The output directories a tool writes into must exist when the pipeline starts (docs/nextflow.md).
    for name, declared in executable.param_types.items():
        value = executable.params[name]
        path = value.get("path") if hasattr(value, "get") else value
        if declared == "Directory" and isinstance(path, str) and "/" not in path and not (pipeline / path).exists():
            (pipeline / path).mkdir()

    outputs = [
        connection for connection in executable.connections if isinstance(connection, NfWorkflowOutputConnection)
    ]
    result = execute_nextflow(pipeline, timeout=3600)
    assert result.returncode == 0, f"stdout:\n{result.stdout[-6000:]}\nstderr:\n{result.stderr[-6000:]}"
    recorded = dict(recorded_outputs(result.stdout))
    processes = {process.name: process for process in executable.processes}
    for output in outputs:
        [port] = [port for port in processes[output.from_process].outputs if port.name == output.from_port]
        if port.capture == "text":
            continue
        values = _recorded(recorded[output.to_port])
        if port.optional and not values:
            continue  # an optional output's null
        assert values and all(Path(value["path"]).exists() for value in values), f"{output.to_port}: {values}"
        assert all(any(Path(value["path"]).iterdir()) for value in values if value["class"] == "Directory"), \
            f"{output.to_port} holds an empty folder: {values}"
