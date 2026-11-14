"""Setup many tests repeat: files written under a directory, the command line run in-process, the shipped tools
as Python API steps, a real-time analysis declared, a run kept off the container engine, a test run under each CWL
runner, the example workflows of tests/core/data/examples read and run, and the segmentation workflow a compute
service is sent."""
from collections.abc import Callable
import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final, Iterable, Mapping
from urllib.parse import unquote, urlsplit

import pytest
import yaml

import sophios
import sophios.plugins
import sophios.preflight
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl as types
from sophios.api.python.workflow import Step, Workflow
from sophios.main import main
from sophios.wic_types import Tools

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ADAPTERS: Final = REPO_ROOT / 'cwl_adapters'
HELLOWORLD: Final = REPO_ROOT / 'docs' / 'tutorials' / 'helloworld.wic'
EACH_RUNNER: Final = pytest.mark.parametrize('runner', ['cwltool', 'toil-cwl-runner'])
EXAMPLES: Final = Path(__file__).resolve().parent / 'data' / 'examples'


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


def examples() -> list[Any]:
    """Each example directory of tests/core/data/examples, by its name."""
    return [pytest.param(path, id=path.name) for path in sorted(EXAMPLES.iterdir()) if path.is_dir()]


def stated(folder: Path) -> Any:
    """What the example `folder`'s expected.yaml states."""
    return yaml.safe_load((folder / 'expected.yaml').read_text(encoding='utf-8'))


def example_workflow(folder: Path, directory: Path) -> Workflow:
    """A copy of the example `folder` at `directory`, and the workflow its `workflow.wic` is: read with the tools and
    child workflows beside it and the tools Sophios ships, its inputs given the values of `job.yml`. A relative path in
    it is read against the working directory, which the caller makes `directory`."""
    shutil.copytree(folder, directory)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(directory), str(ADAPTERS)]}})
    children = {'global': {path.stem: path for path in directory.glob('*.wic') if path.name != 'workflow.wic'}}
    workflow = Workflow.from_wic(directory / 'workflow.wic', tool_registry=tools, workflow_paths=children)
    job = directory / 'job.yml'
    for name, value in (yaml.safe_load(job.read_text(encoding='utf-8')) if job.exists() else {}).items():
        setattr(workflow.inputs, name, value)
    return workflow


def tree(path: Path) -> Any:
    """What a runner-independent observer sees at `path`: a file's text, or a directory's files and texts."""
    if path.is_dir():
        return {str(item.relative_to(path)): tree(item) for item in sorted(path.rglob('*')) if item.is_file()}
    data = path.read_bytes()
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()


def observed(value: Any) -> Any:
    """An output as a runner-independent observer sees it: a File's or Directory's class, basename and contents,
    never its path; null and a string, number or boolean as they are."""
    if isinstance(value, list):
        return [observed(item) for item in value]
    if not isinstance(value, dict):
        return value
    # cwltool gives a Directory it left where its step made it a location and no path, and an entry it captured the
    # path of the file the entry links to: the name is the object's.
    path = Path(value['path'] if 'path' in value else unquote(urlsplit(value['location']).path))
    return {'class': value['class'], 'basename': value.get('basename', path.name), 'contents': tree(path)}


def files_beside(directory: Path) -> dict[str, Any]:
    """What `directory` holds, each file as its text."""
    return {path.name: tree(path) for path in directory.iterdir()}


def cwltool_run(cwltool_main: Callable[..., int], workflow: Workflow, directory: Path) -> tuple[int, str, str]:
    """cwltool's exit code, output object and messages for the CWL `workflow` compiles to, written in `directory`.
    Each output stays where its step left it: moved into one directory, two steps' out.txt could not both keep
    the name."""
    compiled = workflow.compile()
    document, job = compiled.write_cwl(directory), compiled.write_job_inputs(directory)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cwltool_main(
        ['--quiet', '--leave-outputs', '--leave-tmpdir', '--tmpdir-prefix', f'{directory}/tmp/',
         '--tmp-outdir-prefix', f'{directory}/tmp_out/', str(document), str(job)],
        stdout=stdout, stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def cwltool_outputs(cwltool_main: Callable[..., int], workflow: Workflow, directory: Path) -> dict[str, Any]:
    """The output object of a successful `cwltool_run`."""
    code, outputs, messages = cwltool_run(cwltool_main, workflow, directory)
    assert code == 0, messages
    return dict(json.loads(outputs))


def autoseg(plate: str, folder: str | list[str], models: str, on_workflow: bool, gpu: bool = True) -> Workflow:
    """The owner's segmentation shape: a containerized tool with no base command, asking for a GPU when `gpu`, which
    reads `plate`, and `models` staged as `./models`, and fills the output folder `folder`, staged writable, and
    returns it; scattered over `folder` when it is a list. Each is bound on the step, or on a workflow input of its own
    name, which the Python API never scatters over."""
    inputs = Inputs(input=Input(types.directory, flag='--inpDir', position=1),
                    output=Input(types.directory, flag='--outDir', position=2), model=Input(types.directory))
    tool = CommandLineTool('ichnaea_segmentation', inputs,
                           Outputs(output=Output(types.directory, glob='$(inputs.output.basename)')))
    if gpu:
        tool = tool.gpu(cuda_version_min='12.6', compute_capability='3.0', device_count_min=1)
    tool = (tool.docker('vjaganat90/ichnaea-segmentation-plugin:devn')
            .stage(inputs.output, writable=True).stage(inputs.model, entryname='models').resources(cores=4, ram=64000))
    step = Step(tool, step_name='autoseg')
    workflow = Workflow([step], 'autoseg')
    bound = workflow.inputs if on_workflow else step.inputs
    bound.input, bound.output, bound.model = plate, folder, models
    if on_workflow:
        step.inputs.input, step.inputs.output, step.inputs.model = (
            workflow.inputs.input, workflow.inputs.output, workflow.inputs.model)
    if isinstance(folder, list):
        step.scatter_on(step.inputs.output)
    workflow.outputs.segmentation = step.outputs.output
    return workflow
