"""What a run asks of the program that runs a workflow, one backend per (target, runner).

A run is described once, as a `RunRequest`, by every front door: the command line,
`Workflow.run()` and the CI harness. The backend for its target and runner describes
the run, and acts only on this machine, only in `prepare`:

- write: what the runner reads, written under a folder, and what its machine must still do;
- needs: what the run asks of the machine it runs on, which `check` judges on this one;
- prepare: `write`, then this machine's part: the check, the images, the folders;
- launch: the runner's command line, environment and working directory, from the request alone;
- outcome: after the runner exits, its outputs and the steps that failed, read from what it left.

`run_local` starts the launch as a child process and reports the outcome. A target
is the language a workflow is handed to its runner in: `cwl`, run by cwltool or Toil,
or `nextflow`, run by Nextflow.
"""
import json
import re
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from cwl_utils.parser import cwl_v1_2

from . import input_output_nf as io_nf
from . import preflight, realtime, run_setup
from .ir.artifacts import CompilationArtifact
from .lang.diagnostics import Diagnostic, SophiosError
from .nf_reader import read_nextflow
from .nf_types import ExecutableNextflowWorkflow
from .runtime_inputs import output_target_inputs
from .utils_nf import nextflow_names
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

    A backend launches it as it is given; `run_local.launch` first makes its paths absolute, as this machine reads
    them, and names its output directory.

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
        outdir: The runner's output directory; '' for `run_local`'s `<basepath>/outdir_<runner>_<name>_<time>`.
        quiet: Pass `--quiet` to cwltool (never to Toil), which hides its own log.
        provenance: Have the runner write its record of the run under `<basepath>/provenance/<name>`.
        passthrough_args: Arguments for the runner, unchecked, before the documents.
        env: Environment variables the runner gets on top of this process's.
        realtime_plans: The real-time analyses to run beside the workflow.
        generate_run_script: Write `run.sh` with the runner's command instead of running it.
        diagnostics: The notes compiling and preparing the run made, which the run's result carries.

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
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        _check_env_var_names(self.env)


@dataclass(frozen=True, slots=True)
class Launch:
    """How the runner is started: its command line, the variables it gets on top of its machine's environment, and
    its working directory, None for wherever it is started.

    `outdir` is where the runner puts the workflow's outputs.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path | None
    outdir: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a run left once its runner exited.

    Attributes:
        outputs: The workflow's output object, when the runner gave one.
        failed: Each step that failed, in the order it failed: its exit status, the name of the
            signal that ended it, or None when the runner said neither.
    """

    outputs: Json | None
    failed: Mapping[str, int | str | None]


#: How an outcome reads a file the run left, named relative to the run's folder: its text, None when there is none.
Read = Callable[[str], str | None]


class Backend(Protocol):
    """The questions a run asks of the program that runs it. Nothing here starts a process."""

    @property
    def program(self) -> str:
        """The runner's executable, as a machine that has it runs it: `launch`'s `argv[0]` but for cwltool, which
        this machine runs as `cwltool_filterlog`."""

    @property
    def writes_summary(self) -> bool:
        """Whether the runner writes the run's summary, `output_<name>.json`, itself; else the caller writes there
        the output object `outcome` returns."""

    def write(self, lowered: run_setup.Lowered[Any], analyses: tuple[realtime.Analysis, ...], basepath: Path, *,
              inputs_file: str, root_dir: Path) -> run_setup.Prepared:
        """Write what the runner reads of the model its target `lowered` under `basepath`, and nothing elsewhere:
        `inputs_file` holds values that win over the workflow's, and a relative path is read against `root_dir`.
        What the runner's machine must still do before the run starts is in what it returns."""

    def needs(self, documents: Sequence[Path], jobs: Sequence[preflight.Job]) -> preflight.Needs:
        """What running `documents` with `jobs` asks of its machine: the paths the jobs name and the images."""

    def check(self, needs: preflight.Needs, settings: preflight.RunSettings) -> None:
        """Raise every problem a run that `needs` this, and the backend's program, would hit on this machine."""

    def prepare(self, lowered: run_setup.Lowered[Any], analyses: tuple[realtime.Analysis, ...], basepath: Path,
                settings: run_setup.Settings) -> run_setup.Prepared:
        """`write` under `basepath`, then make this machine ready to run it."""

    def launch(self, request: RunRequest) -> Launch:
        """The runner's command line, environment and working directory for `request`, from it alone."""

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: str, read: Read) -> Outcome:
        """What the run left, without writing anything: `stdout` is everything the runner printed there, `log`
        both its streams, and `read` reads the other files it left."""

    def realtime_command(self, request: RunRequest) -> realtime.Command | str:
        """How a real-time analysis runs beside this run, or a line saying why none can."""


def _container_flags(container_engine: str) -> list[str]:
    """The runner flags that select the container engine `container_engine`."""
    if container_engine == 'docker':
        return []
    if container_engine == 'singularity':
        return ['--singularity']
    return ['--user-space-docker-cmd', container_engine]


def _documents(basepath: str, request: RunRequest) -> list[str]:
    if request.documents is not None:
        return list(request.documents)
    return [f'{basepath}/{request.workflow_name}.cwl', f'{basepath}/{request.workflow_name}_inputs.yml']


def _provenance_flags(basepath: str, request: RunRequest) -> list[str]:
    return ['--provenance', f'{basepath}/provenance/{request.workflow_name}'] if request.provenance else []


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


class _Cwl:
    """What every CWL runner shares: `run_setup` writes the files, the pre-flight checks the machine."""

    def write(self, lowered: run_setup.Lowered[CompilationArtifact], analyses: tuple[realtime.Analysis, ...],
              basepath: Path, *, inputs_file: str, root_dir: Path) -> run_setup.Prepared:
        """The workflow and its job under `basepath`, and its analyses beside it; the runner makes every folder."""
        return run_setup.write(lowered.model, analyses, basepath, inputs_file=inputs_file, root_dir=root_dir,
                               output_targets=output_target_inputs(lowered.view))

    def needs(self, documents: Sequence[Path], jobs: Sequence[preflight.Job]) -> preflight.Needs:
        """Every DockerRequirement of the documents, as cwl-docker-extract reads them."""
        return preflight.needs(documents, jobs)

    def check(self, needs: preflight.Needs, settings: preflight.RunSettings) -> None:
        """The pre-flight's checks: paths, programs and the container engine."""
        preflight.check(needs, settings)

    def prepare(self, lowered: run_setup.Lowered[CompilationArtifact], analyses: tuple[realtime.Analysis, ...],
                basepath: Path, settings: run_setup.Settings) -> run_setup.Prepared:
        """The workflow and its job under `basepath`, its analyses beside it, and its images pulled.

        The artifact written is the model, as the front door rewrote it.
        """
        return run_setup.prepare(lowered.model, analyses, basepath, settings, output_target_inputs(lowered.view))


class CwltoolBackend(_Cwl):
    """cwltool, through Sophios's `cwltool_filterlog` on this machine, which silences known noise in its log."""

    program: Final = 'cwltool'
    writes_summary: Final = True

    def launch(self, request: RunRequest) -> Launch:
        """`cwltool_filterlog` with the flags every Sophios run gives cwltool, then the documents."""
        basepath = str(request.basepath)
        # --skip-schemas: cwltool would download each $schemas file on every run, and a firewall or VPN
        # turns each download into a ~30 s timeout.
        # --disable-pull: the pre-flight pulled every image (cwl-docker-extract).
        argv = ['cwltool_filterlog', '--disable-pull', *(['--quiet'] if request.quiet else []),
                *_provenance_flags(basepath, request), *_container_flags(request.container_engine),
                '--write-summary', f'{basepath}/output_{request.workflow_name}.json',
                '--skip-schemas', '--relax-path-checks', '--move-outputs', '--enable-ext', '--outdir', request.outdir]
        if request.cachedir:
            argv += ['--cachedir', request.cachedir]
        argv += [*request.passthrough_args, *_documents(basepath, request)]
        return Launch(tuple(argv), dict(request.env), None, request.outdir)

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments,unused-argument
    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: str, read: Read) -> Outcome:
        """cwltool writes the output object to the summary it is given, and prints none."""
        summary = read(f'output_{request.workflow_name}.json') if exit_code == 0 else None
        return Outcome(None if summary is None else json.loads(summary), failed_steps(log.splitlines()))

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

    program: Final = 'toil-cwl-runner'
    writes_summary: Final = False

    def launch(self, request: RunRequest) -> Launch:
        """`toil-cwl-runner` with a job store per workflow under the base path, then the documents."""
        basepath = str(request.basepath)
        argv = ['toil-cwl-runner', *_container_flags(request.container_engine), '--relax-path-checks']
        # Toil writes no provenance under Slurm: an argument that is exactly `slurm` (as in
        # `--batchSystem slurm`) drops the provenance flags.
        if 'slurm' not in request.passthrough_args:
            argv += _provenance_flags(basepath, request)
        argv += ['--outdir', request.outdir,
                 # Toil's equivalent of cwltool's --cachedir.
                 '--jobStore', f'file:{basepath}/jobStore_{request.workflow_name}',
                 '--clean', 'always', '--noLinkImports',
                 '--disableProgress',  # no progress bar in the terminal
                 *request.passthrough_args, *_documents(basepath, request)]
        return Launch(tuple(argv), dict(request.env), None, request.outdir)

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: str, read: Read) -> Outcome:
        """Toil prints the output object on stdout and writes no summary.

        No failed steps: the job lines Toil relays from its workers name its own jobs, not the
        workflow's steps, so a failure is reported by the runner's own messages.
        """
        del request, log, read
        return Outcome(_last_json_object(stdout) if exit_code == 0 else None, {})

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


def recorded_outputs(stdout: str) -> Iterator[tuple[str, Json]]:
    """Each workflow output the pipeline printed, by its Nextflow name, as `input_output_nf` renders the record.

    A record may follow other text on its line: the launcher's download notice has no newline.
    """
    for line in stdout.splitlines():
        _, found, record = line.partition(f'{io_nf.NF_OUTPUT_MARKER} ')
        if found:
            name, value = json.loads(record)
            yield name, value


def _cwl_objects(value: Any) -> Any:
    """`value` with each path the pipeline recorded made a CWL File or Directory object; one that has its location,
    as a CWL runner's has, is that object already, and stays as it is."""
    match value:
        case {'class': 'File' | 'Directory' as kind, 'path': str() as path} if 'location' not in value:
            return {'class': kind, 'location': Path(path).as_uri(), 'path': path, 'basename': Path(path).name}
        case list():
            return [_cwl_objects(item) for item in value]
    return value


class NextflowBackend:
    """Nextflow, which runs the pipeline the Nextflow target lowers the workflow to."""

    program: Final = 'nextflow'
    writes_summary: Final = False

    def needs(self, documents: Sequence[Path], jobs: Sequence[preflight.Job]) -> preflight.Needs:
        """The image of each process that has one, read from the pipeline `write` wrote into the directory of
        `documents`, as the import reads it."""
        workflow, _ = read_nextflow(documents[0].parent)
        return preflight.Needs(tuple(documents), tuple(cwl_v1_2.DockerRequirement(dockerPull=process.container)
                                                       for process in workflow.processes
                                                       if process.container is not None),
                               tuple(jobs))

    def check(self, needs: preflight.Needs, settings: preflight.RunSettings) -> None:
        """The paths the jobs name, `nextflow`, starting with a Java it can run, and the container engine when a
        process has a container."""
        problems = preflight.path_problems(needs.jobs) + preflight.nextflow_problems(settings)
        if needs.containers:
            problems += preflight.engine_problems(needs.images, settings)
        if problems:
            raise SophiosError(problems)

    def write(self, lowered: run_setup.Lowered[ExecutableNextflowWorkflow], analyses: tuple[realtime.Analysis, ...],
              basepath: Path, *, inputs_file: str, root_dir: Path) -> run_setup.Prepared:
        """The four artifacts and `nextflow_names.json` in the workflow's `nextflow_directory`, and the analyses
        under `basepath`; the folders are each output-target Directory input, which the pipeline stages, so it must
        exist, where a CWL runner makes it.

        The workflow was lowered from a view whose relative File or Directory paths are read against `root_dir`
        and written absolute (`run_setup.absolute_view`): Nextflow runs from its `nextflow_directory`. An inputs
        file is a CWL runner's.
        """
        del inputs_file
        view, workflow = lowered.view, lowered.model
        directory = nextflow_directory(basepath, view.name)
        io_nf.remove_nextflow_artifacts(directory)
        documents = io_nf.write_nextflow_artifacts(workflow, directory)
        (directory / _NEXTFLOW_NAMES).write_text(json.dumps(nextflow_names(view), indent=2) + '\n',
                                                 encoding='utf-8')
        bound = {port.name: port.value for port in view.inputs if port.has_value}
        folders = []
        for name in sorted(output_target_inputs(view)):
            match bound.get(name):
                case {'class': 'Directory', 'path': str() as path}:
                    folders.append(path)
        return run_setup.Prepared(view.name, realtime.write(analyses, basepath, view.name, root_dir), documents,
                                  tuple(folders))

    def prepare(self, lowered: run_setup.Lowered[ExecutableNextflowWorkflow], analyses: tuple[realtime.Analysis, ...],
                basepath: Path, settings: run_setup.Settings) -> run_setup.Prepared:
        """`write`, the check, and each of its folders that names nothing yet made.

        A check (`settings.check`) writes into a temporary directory instead, removed when the check ends,
        so it leaves `basepath` as it found it, and makes no folder.

        Raises:
            SophiosError: What the check found in the way.
        """
        if not settings.check:
            return self._prepare(lowered, analyses, basepath, settings)
        with tempfile.TemporaryDirectory(prefix='sophios-check-') as scratch:
            return self._prepare(lowered, analyses, Path(scratch), settings)

    def _prepare(self, lowered: run_setup.Lowered[ExecutableNextflowWorkflow], analyses: tuple[realtime.Analysis, ...],
                 basepath: Path, settings: run_setup.Settings) -> run_setup.Prepared:
        """`prepare`, writing under `basepath`."""
        written = self.write(lowered, analyses, basepath, inputs_file=settings.inputs_file, root_dir=settings.root_dir)
        view = lowered.view
        targets = output_target_inputs(view)
        # A folder need not exist yet: it is made below.
        job = preflight.Job({port.name: port.value for port in view.inputs
                             if port.has_value and port.name not in targets}, settings.root_dir, 'the workflow')
        self.check(self.needs(written.documents, (job,)), settings.run)
        if not settings.check:
            for folder in written.folders:
                if not Path(folder).exists():
                    Path(folder).mkdir(parents=True)
        return written

    def launch(self, request: RunRequest) -> Launch:
        """`nextflow` reading only the generated configuration, with the pipeline, its parameters, its work directory
        and no ANSI log, then the passthrough arguments, from the workflow's `nextflow_directory`.

        From there, Nextflow's own log and cache are with the pipeline they belong to.
        """
        directory = nextflow_directory(request.basepath, request.workflow_name)
        workdir = str(directory / 'work')
        argv = ['nextflow', '-C', str(directory / io_nf.NEXTFLOW_CONFIG), 'run',
                str(directory / io_nf.NEXTFLOW_SCRIPT), '-params-file', str(directory / io_nf.NEXTFLOW_PARAMS),
                '-work-dir', workdir, '-ansi-log', 'false', *request.passthrough_args]
        return Launch(tuple(argv), {**request.env, 'NXF_SYNTAX_PARSER': 'v2'}, directory, workdir)

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def outcome(self, request: RunRequest, exit_code: int, stdout: str, log: str, read: Read) -> Outcome:
        """The outputs the pipeline recorded, by their Sophios names; the failed processes from Nextflow's log, by
        their Sophios step names."""
        names_file = read(str(nextflow_directory(Path(), request.workflow_name) / _NEXTFLOW_NAMES))
        assert names_file is not None, 'write writes it'
        names = json.loads(names_file)
        failed = {names['processes'][process]: status
                  for process, status in nextflow_failed_steps(log.splitlines()).items()}
        if exit_code != 0:
            return Outcome(None, failed)
        return Outcome({names['outputs'][name]: _cwl_objects(value) for name, value in recorded_outputs(stdout)},
                       failed)

    def realtime_command(self, request: RunRequest) -> str:
        """Why none can: an analysis watches cwltool's job directories, and a Nextflow run has none."""
        return _without_cwltool(request)
