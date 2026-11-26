"""A tool written in `.wic` is lowered from what its lines say: a stream it names as an output, and a script it
writes, need no CWL refusal."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from sophios.compiler import CompileSettings, compile_source
from sophios.ir.frontdoor import bundle_from_disk
from sophios.nextflow.model import NfLiteral
from sophios.plugins import get_tools_cwl

from .testkit import lowered


def _lowered(root: Path):  # type: ignore[no-untyped-def]
    tools = get_tools_cwl({"search_paths_cwl": {"global": [str(root.parent)]}})
    others = {"global": {path.stem: path for path in root.parent.glob("*.wic") if path != root}}
    return lowered(compile_source(bundle_from_disk(root, others, tools), CompileSettings()))


@pytest.mark.fast
def test_a_stream_output_with_no_file_named_is_written_to_a_file_the_output_collects(tmp_path: Path) -> None:
    (tmp_path / "say.wic").write_text(
        "command: echo {message}\ninputs: {message: string}\noutputs: {out: stdout, err: stderr}\n", encoding="utf-8")
    (tmp_path / "root.wic").write_text("steps:\n- id: say\n  run: say.wic\n  in: {message: !ii hi}\n", encoding="utf-8")
    (process,) = _lowered(tmp_path / "root.wic").processes
    assert [(port.name, port.glob) for port in process.outputs] == [
        ("out", process.command.stdout), ("err", process.command.stderr)]
    assert [stream.segments for stream in (process.command.stdout, process.command.stderr)] == [
        (NfLiteral("stdout.txt"),), (NfLiteral("stderr.txt"),)]


@pytest.mark.fast
def test_a_script_is_a_file_the_task_writes_before_it_runs_it(tmp_path: Path) -> None:
    (tmp_path / "run.wic").write_text("script: |\n  echo \"$message\"\ninputs: {message: string}\n", encoding="utf-8")
    (tmp_path / "root.wic").write_text("steps:\n- id: run\n  run: run.wic\n  in: {message: !ii hi}\n", encoding="utf-8")
    (process,) = _lowered(tmp_path / "root.wic").processes
    assert [(file.name, file.text) for file in process.files] == [(".script.sh", 'echo "$message"\n')]
