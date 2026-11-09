"""Setup many tests repeat: files written under a directory, the command line run in-process, the shipped tools
as Python API steps, a real-time analysis declared, a run kept off the container engine, and a test run under
each CWL runner."""
import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

import pytest

import sophios
import sophios.plugins
import sophios.preflight
from sophios.api.python.workflow import Step, Workflow
from sophios.main import main
from sophios.wic_types import Tools

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ADAPTERS: Final = REPO_ROOT / 'cwl_adapters'
HELLOWORLD: Final = REPO_ROOT / 'docs' / 'tutorials' / 'helloworld.wic'
EACH_RUNNER: Final = pytest.mark.parametrize('runner', ['cwltool', 'toil-cwl-runner'])


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Write each 'relative path: text' under root, making parent directories; return root."""
    for relative, text in files.items():
        (target := root / relative).parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return root


def search_config(path: Path, cwl: Iterable[Path | str], wic: Iterable[Path | str] = (), **extra: Any) -> Path:
    """Write a global config naming the CWL and wic search paths; return its path."""
    path.write_text(json.dumps({'search_paths_cwl': {'global': [str(p) for p in cwl]},
                                'search_paths_wic': {'global': [str(p) for p in wic]}, **extra}), encoding='utf-8')
    return path


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str | Path) -> int | str | None:
    """Run the sophios command line in-process; return the exit code, or None when main returns."""
    monkeypatch.setattr(sys, 'argv', ['sophios', *map(str, argv)])
    try:
        main()
    except SystemExit as exit_:
        return exit_.code
    return None


def adapter_step(tool: str, name: str | None = None, **values: Any) -> Step:
    """A Python API step for a shipped adapter tool, its inputs set to values."""
    step = Step(clt_path=ADAPTERS / f'{tool}.cwl', step_name=name)
    for port, value in values.items():
        setattr(step.inputs, port, value)
    return step


def echo_workflow(name: str = 'wf') -> Workflow:
    """A workflow of one shipped `echo` step."""
    return Workflow([adapter_step('echo', message='hello')], name)


def live_workflow(name: str, cwl_tool: str = 'touch', config: Any = None) -> Workflow:
    """A workflow of touch plus a cwl_subinterpreter step declaring a real-time analysis of cwl_tool."""
    declaration = Step(clt_path=ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl')
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = cwl_tool
    declaration.inputs.max_times = 2
    declaration.inputs.config = config if config is not None else {'in': {'filename': 'again.txt'}}
    return Workflow([adapter_step('touch', filename='empty.txt'), declaration], name)


def no_preflight(monkeypatch: pytest.MonkeyPatch) -> list[list[Path]]:
    """Replace the container-engine preflight with a recorder of the documents it was asked to prepare."""
    prepared: list[list[Path]] = []

    def recorder(documents: Iterable[Path], *_settings_and_jobs: Any) -> None:
        prepared.append(list(documents))

    monkeypatch.setattr(sophios.preflight, 'prepare', recorder)
    return prepared


@functools.cache
def shipped_tools() -> Tools:
    """The tools this checkout ships, read from the repository and not from the home."""
    return sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(ADAPTERS)]}})


def python_process(*argv: str | Path, check: bool = False, **options: Any) -> subprocess.CompletedProcess[str]:
    """`python <argv>` in a new interpreter that imports the sophios under test, whatever the working directory."""
    package = str(Path(sophios.__file__).resolve().parents[1])
    path = os.pathsep.join(filter(None, (package, os.environ.get('PYTHONPATH'))))
    return subprocess.run([sys.executable, *map(str, argv)], env={**os.environ, 'PYTHONPATH': path}, text=True,
                          check=check, **options)


def run_local_process(workflow: Path, *flags: str, **options: Any) -> subprocess.CompletedProcess[str]:
    """`sophios --run_local` on `workflow` in a process of its own, as on the command line: Toil and cwltool share
    logging state that one process running both leaves broken."""
    return python_process('-m', 'sophios.main', '--yaml', workflow, '--run_local', '--ignore_docker_install',
                          '--ignore_docker_processes', *flags, **options)
