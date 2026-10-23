"""What a local run asks of the program that runs a workflow, one backend per (target, runner).

A run is described once, as a `RunRequest`, by every front door: the command line,
`Workflow.run()` and the CI harness. The backend for its target and runner answers
four questions about it and runs nothing itself:

- check: what this machine needs before the run starts;
- prepare: what the runner reads, written under the base path;
- launch: the runner's command line, environment and working directory;
- outcome: after the runner exits, its outputs and the steps that failed.

`run_local` starts the launch as a child process and reports the outcome. A target
is the language a workflow is handed to its runner in: `cwl`, run by cwltool or Toil,
or `nextflow`, run by Nextflow.
"""
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from . import input_output_nf as io_nf
from . import preflight, realtime, run_setup
from .ir.artifacts import CompilationResult
from .ir.locations import absolute_paths
from .lang.diagnostics import SophiosError
from .nf_reader import read_nextflow
from .runtime_inputs import output_target_inputs
from .utils_nf import compiled_source_to_nextflow_with_notes, local_locations_as_paths, nextflow_names
from .wic_types import Json

_ENV_VAR_NAME: Final = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _check_env_var_names(env_vars: Mapping[str, str]) -> None:
    """Raise ValueError naming each key that is not an environment variable name.

    Values are not checked: they reach the runner through an environment mapping and never
    through a shell, so every character in them is passed as given.
    """
    invalid = [key for key in env_vars if not _ENV_VAR_NAME.fullmatch(key)]
    if invalid:
        names = ', '.join(repr(key) for key in invalid)
        raise ValueError(f'Not an environment variable name: {names}. A name is letters, digits '
                         'and underscores, and does not start with a digit.')


@dataclass(frozen=True, slots=True)
class RunRequest:  # pylint: disable=too-many-instance-attributes
    """One local run of a workflow, as every front door describes it.

    Attributes:
        workflow_name: The workflow's name: its files are `<name>.cwl`, `output_<name>.json`, ...
        basepath: The directory the workflow was written to, and the run's files go.
        runner: `cwltool`, `toil-cwl-runner`, or `nextflow` for the target `nextflow`.
        target: The language the runner is given the workflow in: `cwl` or `nextflow`.
        documents: The workflow, then its job file if it has one, as a CWL runner is given them.
            None: the files `prepare` wrote, `<basepath>/<name>.cwl` and `<basepath>/<name>_inputs.yml`.
            Nextflow is always given the files `prepare` wrote.
        container_engine: `docker`, `podman`, `singularity`, or another docker-compatible command.
        cachedir: Where cwltool caches a job whose tool and inputs are unchanged; '' for no cache.
        outdir: The runner's output directory; '' for `<basepath>/outdir_<runner>_<name>_<time>`.
        quiet: Pass `--quiet` to cwltool (never to Toil), which hides its own log.
        provenance: Have the runner write its record of the run under `<basepath>/provenance/<name>`.
        passthrough_args: Arguments for the runner, unchecked, before the documents.
        env: Environment variables the runner gets on top of this process's.
        realtime_plans: The real-time analyses to run beside the workflow.
        generate_run_script: Write `run.sh` with the runner's command instead of running it.

    Raises:
        ValueError: A key of `env` is not an environment variable name.
    """

    workflow_name: str
    basepath: Path
    runner: str = 'cwltool'
    target: str = 'cwl'
    documents: tuple[str, ...] | None = None
    container_engine: str = 'docker'
    cachedir: str = ''
    outdir: str = ''
    quiet: bool = True
    provenance: bool = True
    passthrough_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    realtime_plans: tuple[realtime.Plan, ...] = ()
    generate_run_script: bool = False

    def __post_init__(self) -> None:
        _check_env_var_names(self.env)


@dataclass(frozen=True, slots=True)
class Launch:
    """How the runner is started: its command line, its whole environment and its working directory.

    `outdir` is where the runner puts the workflow's outputs.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    outdir: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a run left once its runner exited.

    Attributes:
        outputs: The workflow's output object, when the runner gave one.
        summary: The file the output object is in, to say where it is; None to say nothing.
        failed: Each step that failed, in the order it failed: its exit status, the name of the
            signal that ended it, or None when the runner said neither.
    """

    outputs: Json | None
    summary: Path | None
    failed: Mapping[str, int | str | None]


class Backend(Protocol):
    """The questions a run asks of the program that runs it. Nothing here starts a process."""

    def check(self, documents: Sequence[Path], jobs: Sequence[preflight.Job],
              settings: preflight.RunSettings) -> None:
        """Raise every problem running `documents` with `jobs` would hit on this machine."""

    def prepare(self, compilation: CompilationResult, analyses: tuple[realtime.Analysis, ...], basepath: Path,
                settings: run_setup.Settings) -> run_setup.Prepared:
        """Write what the runner reads of `compilation` under `basepath`, and make the machine ready to run it."""

    def launch(self, request: RunRequest) -> Launch:
        """The runner's command line, environment and working directory for `request`."""

    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: Path) -> Outcome:
        """What the run left: `stdout` is everything the runner printed there, `log` both its streams."""

    def realtime_command(self, request: RunRequest) -> realtime.Command | str:
        """How a real-time analysis runs beside this run, or a line saying why none can."""


def _container_flags(container_engine: str) -> list[str]:
    """The runner flags that select the container engine `container_engine`."""
    if container_engine == 'docker':
        return []
    if container_engine == 'singularity':
        return ['--singularity']
    return ['--user-space-docker-cmd', container_engine]


def _runner_outdir(basepath: str, request: RunRequest, runner_name: str) -> str:
    """The explicit output directory, else the default one.

    The default is named after the workflow, like every other path a run writes (provenance, the
    summary, the job store), so two workflows started in the same second from one basepath never
    share it.
    """
    if request.outdir:
        return str(Path(request.outdir).absolute().resolve())
    date_time = datetime.now().strftime('%Y_%m_%d_%H.%M.%S')
    return f'{basepath}/outdir_{runner_name}_{request.workflow_name}_{date_time}'


def _documents(basepath: str, request: RunRequest) -> list[str]:
    if request.documents is not None:
        return list(request.documents)
    return [f'{basepath}/{request.workflow_name}.cwl', f'{basepath}/{request.workflow_name}_inputs.yml']


def _provenance_flags(basepath: str, request: RunRequest) -> list[str]:
    return ['--provenance', f'{basepath}/provenance/{request.workflow_name}'] if request.provenance else []


def _environment(request: RunRequest) -> dict[str, str]:
    return {**os.environ, **request.env}


#: cwltool's messages for a job that did not succeed (cwltool/job.py), as its log prints them.
_JOB_ENDED: Final = re.compile(r'\[job (?P<job>.+?)\] (?:exited with status: (?P<status>-?\d+)'
                               r'|was terminated by signal: (?P<signal>\w+)|completed (?P<completed>\w+))\s*$')
#: A terminal colour code: cwltool colours its log.
_COLOUR: Final = re.compile(r'\x1b\[[0-9;]*m')


def failed_steps(lines: Iterable[str]) -> dict[str, int | str | None]:
    """Each job the runner's log says did not succeed, in the order it failed.

    Read from cwltool's own lines, `[job <name>] exited with status: <n>` or `was terminated by
    signal: <name>`, then `[job <name>] completed <status>`. A job is failed by its `completed`
    line, which carries its exit status or signal when one came first, else None.
    """
    ended: dict[str, int | str] = {}
    failed: dict[str, int | str | None] = {}
    for line in lines:
        found = _JOB_ENDED.search(_COLOUR.sub('', line))
        if found is None:
            continue
        job = found['job']
        if found['status'] is not None:
            ended[job] = int(found['status'])
        elif found['signal'] is not None:
            ended[job] = found['signal']
        elif found['completed'] != 'success':
            failed[job] = ended.get(job)
    return failed


def _log_lines(log: Path) -> list[str]:
    try:
        return log.read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return []


class _Cwl:
    """What every CWL runner shares: the pre-flight checks the machine, `run_setup` writes the files."""

    def check(self, documents: Sequence[Path], jobs: Sequence[preflight.Job],
              settings: preflight.RunSettings) -> None:
        """The pre-flight's checks: paths, programs and the container engine."""
        preflight.check(preflight.needs(documents, jobs), settings)

    def prepare(self, compilation: CompilationResult, analyses: tuple[realtime.Analysis, ...], basepath: Path,
                settings: run_setup.Settings) -> run_setup.Prepared:
        """The workflow and its job under `basepath`, its analyses beside it, and its images pulled.

        The artifact written is `compilation.artifact`, as the front door rewrote it.
        """
        return run_setup.prepare(compilation.artifact, analyses, basepath, settings)


class CwltoolBackend(_Cwl):
    """cwltool, through Sophios's `cwltool_filterlog`, which silences known noise in its log."""

    def launch(self, request: RunRequest) -> Launch:
        """`cwltool_filterlog` with the flags every Sophios run gives cwltool, then the documents."""
        basepath = str(request.basepath.absolute().resolve())
        outdir = _runner_outdir(basepath, request, 'cwltool')
        # --skip-schemas: cwltool would download each $schemas file on every run, and a firewall or VPN
        # turns each download into a ~30 s timeout.
        # --disable-pull: the pre-flight pulled every image (cwl-docker-extract).
        argv = ['cwltool_filterlog', '--disable-pull', *(['--quiet'] if request.quiet else []),
                *_provenance_flags(basepath, request), *_container_flags(request.container_engine),
                '--write-summary', f'{basepath}/output_{request.workflow_name}.json',
                '--skip-schemas', '--relax-path-checks', '--move-outputs', '--enable-ext', '--outdir', outdir]
        if request.cachedir:
            argv += ['--cachedir', str(Path(request.cachedir).absolute())]
        argv += [*request.passthrough_args, *_documents(basepath, request)]
        return Launch(tuple(argv), _environment(request), Path.cwd(), outdir)

    # pylint: disable-next=unused-argument
    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: Path) -> Outcome:
        """cwltool writes the output object to the summary it is given, and prints none."""
        summary = Path(f'{request.basepath.absolute().resolve()}/output_{request.workflow_name}.json')
        outputs = None
        if exit_code == 0 and summary.is_file():
            outputs = json.loads(summary.read_text(encoding='utf-8'))
        return Outcome(outputs, summary, failed_steps(_log_lines(log)))

    def realtime_command(self, request: RunRequest) -> realtime.Command:
        """cwltool in a subprocess, with this run's container engine.

        Its images were pulled with the workflow's, so it does not pull; it keeps no provenance.
        This interpreter's cwltool, and not `python -m cwltool`, which exits 0 whatever happened.
        """
        engine = request.container_engine

        def command(cwl: Path, job: Path, outdir: Path, cachedir: Path) -> list[str]:
            return [sys.executable, '-c', 'import sys, cwltool.main; sys.exit(cwltool.main.run())',
                    '--disable-pull', '--skip-schemas', '--relax-path-checks', '--enable-ext',
                    *_container_flags(engine), '--cachedir', str(cachedir), '--outdir', str(outdir),
                    str(cwl), str(job)]
        return command


class ToilBackend(_Cwl):
    """Toil's CWL runner, `toil-cwl-runner`."""

    def launch(self, request: RunRequest) -> Launch:
        """`toil-cwl-runner` with a job store per workflow under the base path, then the documents."""
        basepath = str(request.basepath.absolute().resolve())
        outdir = _runner_outdir(basepath, request, 'toil')
        argv = ['toil-cwl-runner', *_container_flags(request.container_engine), '--relax-path-checks']
        # Toil writes no provenance under Slurm: an argument that is exactly `slurm` (as in
        # `--batchSystem slurm`) drops the provenance flags.
        if 'slurm' not in request.passthrough_args:
            argv += _provenance_flags(basepath, request)
        argv += ['--outdir', outdir,
                 # Toil's equivalent of cwltool's --cachedir.
                 '--jobStore', f'file:{basepath}/jobStore_{request.workflow_name}',
                 '--clean', 'always', '--noLinkImports',
                 '--disableProgress',  # no progress bar in the terminal
                 *request.passthrough_args, *_documents(basepath, request)]
        return Launch(tuple(argv), _environment(request), Path.cwd(), outdir)

    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: Path) -> Outcome:
        """Toil prints the output object on stdout and writes no summary: it is written here, as cwltool's is.

        No failed steps: the job lines Toil relays from its workers name its own jobs, not the
        workflow's steps, so a failure is reported by the runner's own messages.
        """
        del log
        outputs = _last_json_object(stdout) if exit_code == 0 else None
        summary = None
        if outputs is not None:
            summary = Path(f'{request.basepath.absolute().resolve()}/output_{request.workflow_name}.json')
            summary.write_text(json.dumps(outputs, indent=4) + '\n', encoding='utf-8')
        return Outcome(outputs, summary, {})

    def realtime_command(self, request: RunRequest) -> str:
        """Why none can: an analysis watches cwltool's job directories, and Toil has none."""
        return _without_cwltool(request)


def _without_cwltool(request: RunRequest) -> str:
    """The line that says a run's real-time analyses do not run, since its runner is not cwltool."""
    return f'Real-time analysis needs cwltool; {request.runner} runs the workflow without it.'


def _last_json_object(text: str) -> Json | None:
    """The JSON object `text` ends with, printed from a line of its own; None when it ends with none."""
    start = text.rfind('\n{') + 1
    if not text.startswith('{', start):
        return None
    try:
        found = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return found if isinstance(found, dict) else None


def nextflow_directory(basepath: Path, workflow_name: str) -> Path:
    """Where `NextflowBackend` writes and runs `workflow_name`'s pipeline: the four artifacts, `nextflow_names.json`
    with the Sophios name of each process and workflow output by its Nextflow name, the work directory `work`, and
    Nextflow's own log and cache.

    Named after the workflow, as a CWL run's `<name>.cwl` and `<name>_inputs.yml` are, so two workflows run
    together from one base path never read or overwrite each other's pipeline.
    """
    return basepath / f'nextflow_{workflow_name}'


#: The file in a workflow's `nextflow_directory` that maps each Nextflow name back to its Sophios one.
_NEXTFLOW_NAMES: Final = 'nextflow_names.json'


#: Nextflow's lines for a failed task, as its log prints them: `Error executing process > '<task>'`, then, for a
#: task that exited non-zero, `Process `<task>` terminated with an error exit status (<n>)`.
_NEXTFLOW_FAILED: Final = re.compile(r"Error executing process > '(?P<failed>[^']+)'"
                                     r"|Process `(?P<exited>[^`]+)` terminated with an error exit status "
                                     r"\((?P<status>-?\d+)\)")


def nextflow_failed_steps(lines: Iterable[str]) -> dict[str, int | None]:
    """Each process Nextflow's log says failed, in the order it failed, with its exit status when Nextflow gives one.

    A task is named `<workflow>:<process>`, followed by ` (<index>)` for a task of a scattered process.
    """
    failed: dict[str, int | None] = {}
    for line in lines:
        found = _NEXTFLOW_FAILED.search(line)
        if found is None:
            continue
        process = (found['failed'] or found['exited']).split(' (', 1)[0].rsplit(':', 1)[-1]
        if found['status'] is None:
            failed.setdefault(process, None)
        else:
            failed[process] = int(found['status'])
    return failed


def _recorded_outputs(stdout: str) -> Iterator[tuple[str, Json]]:
    """Each workflow output the pipeline printed, by its Nextflow name, as `input_output_nf` renders the record."""
    marker = f'{io_nf.NF_OUTPUT_MARKER} '
    for line in stdout.splitlines():
        if line.startswith(marker):
            name, value = json.loads(line[len(marker):])
            yield name, value


def _cwl_objects(value: Any) -> Any:
    """`value` with each path the pipeline recorded made a CWL File or Directory object."""
    match value:
        case {'class': 'File' | 'Directory' as kind, 'path': str() as path}:
            return {'class': kind, 'location': Path(path).as_uri(), 'path': path, 'basename': Path(path).name}
        case list():
            return [_cwl_objects(item) for item in value]
    return value


class NextflowBackend:
    """Nextflow, which runs the pipeline the Nextflow target lowers the workflow to."""

    def check(self, documents: Sequence[Path], jobs: Sequence[preflight.Job],
              settings: preflight.RunSettings) -> None:
        """The paths `jobs` name, `nextflow`, starting with a Java it can run, and the container engine when a
        process has a container.

        `documents` are the artifacts `prepare` wrote, in one directory; the processes are read from the pipeline
        there, as the import reads them.
        """
        workflow, _ = read_nextflow(documents[0].parent)
        problems = preflight.path_problems(jobs) + preflight.nextflow_problems(settings)
        images = [process.container for process in workflow.processes if process.container is not None]
        if images:
            problems += preflight.engine_problems(images, settings)
        if problems:
            raise SophiosError(problems)

    def prepare(self, compilation: CompilationResult, analyses: tuple[realtime.Analysis, ...], basepath: Path,
                settings: run_setup.Settings) -> run_setup.Prepared:
        """The four artifacts and `nextflow_names.json` in the workflow's `nextflow_directory`, the analyses under
        `basepath`, the check, and each output-target Directory input that names nothing yet made.

        The workflow is lowered from its bound values, each relative File or Directory path read against
        `settings.root_dir` and written absolute: Nextflow runs from its `nextflow_directory`.

        Raises:
            ValueError: The workflow has no Nextflow lowering; no artifact is left.
            SophiosError: What the check found in the way.
        """
        artifact = compilation.artifact
        bound = absolute_paths(local_locations_as_paths(artifact.job_inputs), settings.root_dir)
        compilation = replace(compilation, artifact=replace(artifact, job_inputs=bound))
        directory = nextflow_directory(basepath, artifact.name)
        io_nf.remove_nextflow_artifacts(directory)
        workflow, notes = compiled_source_to_nextflow_with_notes(compilation)
        documents = io_nf.write_nextflow_artifacts(workflow, directory)
        (directory / _NEXTFLOW_NAMES).write_text(json.dumps(nextflow_names(compilation), indent=2) + '\n',
                                                 encoding='utf-8')
        plans = realtime.write(analyses, basepath, artifact.name, settings.root_dir)
        targets = output_target_inputs(artifact)
        # An output-target Directory need not exist yet: it is made below.
        job = preflight.Job({name: value for name, value in bound.items() if name not in targets}, settings.root_dir,
                            'the workflow')
        self.check(documents, (job,), settings.run)
        if not settings.check:
            # The pipeline stages an output-target Directory as an input, so it must exist; a CWL runner makes it.
            for name in targets:
                match bound.get(name):
                    case {'class': 'Directory', 'path': str() as path} if not Path(path).exists():
                        Path(path).mkdir(parents=True)
        return run_setup.Prepared(artifact, plans, documents, notes)

    def launch(self, request: RunRequest) -> Launch:
        """`nextflow` reading only the generated configuration, with the pipeline, its parameters, its work directory
        and no ANSI log, then the passthrough arguments, from the workflow's `nextflow_directory`.

        From there, Nextflow's own log and cache are with the pipeline they belong to.
        """
        directory = nextflow_directory(request.basepath.absolute().resolve(), request.workflow_name)
        workdir = str(directory / 'work')
        argv = ['nextflow', '-C', str(directory / io_nf.NEXTFLOW_CONFIG), 'run',
                str(directory / io_nf.NEXTFLOW_SCRIPT), '-params-file', str(directory / io_nf.NEXTFLOW_PARAMS),
                '-work-dir', workdir, '-ansi-log', 'false', *request.passthrough_args]
        return Launch(tuple(argv), {**_environment(request), 'NXF_SYNTAX_PARSER': 'v2'}, directory, workdir)

    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: Path) -> Outcome:
        """The outputs the pipeline recorded, by their Sophios names and written to the summary as cwltool's are;
        the failed processes from Nextflow's log, by their Sophios step names."""
        basepath = request.basepath.absolute().resolve()
        names = json.loads((nextflow_directory(basepath, request.workflow_name) / _NEXTFLOW_NAMES)
                           .read_text(encoding='utf-8'))
        failed = {names['processes'][process]: status
                  for process, status in nextflow_failed_steps(_log_lines(log)).items()}
        if exit_code != 0:
            return Outcome(None, None, failed)
        outputs = {names['outputs'][name]: _cwl_objects(value) for name, value in _recorded_outputs(stdout)}
        summary = basepath / f'output_{request.workflow_name}.json'
        summary.write_text(json.dumps(outputs, indent=4) + '\n', encoding='utf-8')
        return Outcome(outputs, summary, failed)

    def realtime_command(self, request: RunRequest) -> str:
        """Why none can: an analysis watches cwltool's job directories, and a Nextflow run has none."""
        return _without_cwltool(request)


#: The backend of each (target, runner).
BACKENDS: Final[Mapping[tuple[str, str], Backend]] = {
    ('cwl', 'cwltool'): CwltoolBackend(),
    ('cwl', 'toil-cwl-runner'): ToilBackend(),
    ('nextflow', 'nextflow'): NextflowBackend(),
}


def backend(target: str, runner: str) -> Backend:
    """The backend that runs `target` documents with `runner`.

    Raises:
        ValueError: No backend runs them.
    """
    try:
        return BACKENDS[(target, runner)]
    except KeyError:
        known = ', '.join(f'{name} ({kind})' for kind, name in BACKENDS)
        raise ValueError(f'No backend runs {target} with {runner!r}; the runners are {known}.') from None
