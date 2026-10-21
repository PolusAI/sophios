"""The corpus workflows the Nextflow target runs: each is compiled, run under Nextflow with Docker, and checked.

Only the manual lane `.github/workflows/nextflow_corpus.yml` collects these tests. It checks out
mm-workflows and image-workflows beside sophios, as `run_workflows.yml` does, and
`.github/update_sophios_config.py` writes the config that reaches them.
"""
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

import sophios.input_output as io
import sophios.plugins
from sophios.api.python import workflow as python_workflow
from sophios.nf_types import ExecutableNextflowWorkflow, NfWorkflowOutputConnection
from sophios.python_cwl_adapter import import_python_file

from .testkit import REPO_ROOT, execute_nextflow

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
        result = subprocess.run(
            [sys.executable, "-m", "sophios.main", "--yaml", str(source), "--config_file", str(CONFIG),
             "--target", "nextflow", "--basepath", str(pipeline)],
            cwd=pipeline.parent, capture_output=True, text=True, timeout=900, check=False,
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        return
    # A Python workflow names its tools relative to its repository, as run_workflows.yml compiles it.
    tools = sophios.plugins.get_tools_cwl(io.read_config_from_disk(CONFIG))
    step = python_workflow.Step

    def step_with_tools(*args: object, **kwargs: object) -> object:
        kwargs.setdefault("tool_registry", tools)
        return step(*args, **kwargs)

    monkeypatch.chdir(source.parents[1])
    with patch.object(python_workflow, "Step", step_with_tools):
        built = import_python_file(source.stem, source).workflow()
    built.to_nextflow(pipeline, tool_registry=tools)


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.nextflow_corpus
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_a_corpus_workflow_runs_under_nextflow(workflow: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run succeeds and every declared workflow output holds what its type says."""
    pipeline = tmp_path / "pipeline"
    _compile(CHECKOUTS / workflow, pipeline, monkeypatch)
    executable = ExecutableNextflowWorkflow.from_json(
        (pipeline / "nextflow_workflow.json").read_text(encoding="utf-8")
    )
    # An output directory a tool writes into is a name the run makes, as Sophios's CWL runs make it.
    for name, declared in executable.param_types.items():
        value = executable.params[name]
        path = value.get("path") if hasattr(value, "get") else value
        if declared == "Directory" and isinstance(path, str) and "/" not in path and not (pipeline / path).exists():
            (pipeline / path).mkdir()

    outputs = [
        connection for connection in executable.connections if isinstance(connection, NfWorkflowOutputConnection)
    ]
    sinks = "".join(
        f"\n    {executable.name}.out.{output.to_port}.subscribe {{ new File('emits/{output.to_port}').text = "
        "(it instanceof List ? it : [it]).join('\\n') }"
        for output in outputs
    )
    script = pipeline / "workflow.nf"
    (pipeline / "emits").mkdir()
    script.write_text(script.read_text(encoding="utf-8")[:-2] + sinks + "\n}\n", encoding="utf-8")

    result = execute_nextflow(pipeline, timeout=3600)
    assert result.returncode == 0, f"stdout:\n{result.stdout[-6000:]}\nstderr:\n{result.stderr[-6000:]}"
    processes = {process.name: process for process in executable.processes}
    for output in outputs:
        [port] = [port for port in processes[output.from_process].outputs if port.name == output.from_port]
        values = (pipeline / "emits" / output.to_port).read_text(encoding="utf-8").splitlines()
        if port.capture == "text" or (port.optional and not values):
            continue  # a captured text, or an optional output's null
        assert values and all(Path(value).exists() for value in values), f"{output.to_port}: {values}"
