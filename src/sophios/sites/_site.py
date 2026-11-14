"""The sites: `Machine` and `Slurm`, which check, submit and attach a run."""
# pylint: disable=protected-access  # a site starts its run in the run's folder
import json
import re
import secrets
import shlex
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, Final

from .. import __version__, preflight, targets
from ..api.python._inference import Inference
from ..api.python._workflow_runtime import lowered_for_site
from ..backends import Launch, RunRequest, nextflow_directory
from ..input_output_nf import NEXTFLOW_CONFIG
from ..ir.artifacts import CompilationArtifact
from ..ir.locations import absolute_paths, input_paths
from ..ir.resolved import Requirement, ResolvedTool, ResolvedWorkflow, tool_steps
from ..lang.diagnostics import Diagnostic, Locator, Severity, SophiosError, report
from ..lang.error_codes import SophiosErrorCode as Code
from ..run_setup import Lowered
from ..runtime_inputs import output_target_inputs, runner_job_inputs
from ..utils_nf import nextflow_names
from ..wic_types import Json, Tools

from ._run import Run, _run_folder
from ._runners import (MAX_PARALLEL, OLDEST, ON_A_MACHINE, ON_SLURM, _CLEAN, _Queue, _SBATCH_VARIABLES,
                       _TASK_VARIABLES, _TIME, _header, _nextflow, _recorded, _seconds)
from ._ssh import _ANSWER, _CHECKS, _PUT, _Said, _alive, _call, _q, _unreachable
from ._states import Job, _jobs

if TYPE_CHECKING:
    from ..api.python.workflow import Workflow


#: What a setting may hold: none takes shell code. A folder is absolute, and its characters fit an `#SBATCH` line.
_HOST: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*')
_FOLDER: Final = re.compile(r'/[A-Za-z0-9/._+-]*')
_NAME: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./+-]*')
_RUN_ID: Final = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')
#: The Slurm commands a run on Slurm uses, each asked so it answers; `sacct` must have accounting behind it.
_SLURM: Final = ('sbatch --version', 'squeue --version', 'scancel --version', 'sacct -n -X -P -o JobID -S now',
                 "sinfo -a -h -o '%P|%l|%G'")
#: The submit's time, as `sacct --starttime` reads it, in a script that runs in the run's folder: run.json's.
_SUBMITTED: Final = '"$(date -r run.json +%Y-%m-%dT%H:%M:%S)"'
#: A question a check asks the site: a command that prints one line, and the judge of that line.
_Ask = tuple[str, Callable[[str], Diagnostic | None]]


def _error(code: Code, message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, code, message)


class _Site:
    """What every site does, and its scheduler's part as a machine has it, which `Slurm` replaces."""

    host: str
    root: str
    bin: str
    container_engine: str
    modules: tuple[str, ...]
    #: The design's runner-by-scheduler table: `ON_A_MACHINE` or `ON_SLURM`.
    _RUNNERS: ClassVar[Mapping[str, Callable[[str, int, str, _Queue | None], tuple[str, ...]]]] = ON_A_MACHINE
    #: The pattern of each setting of the site's own that may be None, which no other site has.
    _OPTIONAL: ClassVar[Mapping[str, re.Pattern[str]]] = {}
    #: What the leader claims a run with: its pid on a machine, its job id on Slurm.
    _CLAIMANT: ClassVar[str] = '$$'

    def __post_init__(self) -> None:
        if sys.platform == 'win32':
            raise _unreachable(self.host, 'Sophios runs on Windows itself, whose paths no site reads: run it inside '
                               'WSL')
        if isinstance(self.modules, str):
            raise ValueError(f'modules={self.modules!r}: modules= takes the names, as a tuple')
        object.__setattr__(self, 'modules', tuple(self.modules))
        for name, value, pattern in (('host', self.host, _HOST), ('root', self.root, _FOLDER),
                                     ('bin', self.bin, _FOLDER), ('container_engine', self.container_engine, _NAME),
                                     *(('modules', module, _NAME) for module in self.modules),
                                     *((name, getattr(self, name), pattern) for name, pattern in self._OPTIONAL.items()
                                       if getattr(self, name) is not None)):
            if not (isinstance(value, str) and pattern.fullmatch(value)):
                raise ValueError(f'{name}={value!r} is not a {name} a site takes: it matches {pattern.pattern}')

    # pylint: disable-next=too-many-arguments
    def check(self, workflow: 'Workflow', *, target: str = 'cwl', runner: str | None = None,
              outdir: str | None = None, max_parallel: int = MAX_PARALLEL, clean: str = 'always',
              tool_registry: Tools | None = None, inference: Inference = Inference()) -> list[Diagnostic]:
        """Every problem `submit` would hit, at once, and every note the compile and the target made; it writes
        nothing here or there.

        The workflow is compiled and lowered here, its relative paths read against `root`; one call there then checks
        each File and Directory it reads, the runner in `bin` and its version, the container engine when a step runs
        in a container, that `<root>/sophios` can be written, and that `outdir` does not exist yet; on Slurm also its
        commands, the partitions the run needs and your `SBATCH_` settings. Under Toil, a note, `wic061`, at each step
        whose tool declares no memory: Toil gives it 4 GiB there. cwltool, which no site runs, is refused, `wic058`,
        before anything is compiled or sent.

        Args:
            workflow: The workflow, built with the Python API.
            target: The language the runner is given it in, as `Workflow.run` takes it.
            runner: The target's runner; None for a site's default, its first runner a site runs:
                toil-cwl-runner for `cwl`.
            outdir: A CWL runner's `--outdir` there, new, relative to `root` or absolute; None for `<run>/out`.
            max_parallel: How many steps run at once: Toil's `--maxJobs`, Nextflow's `executor.queueSize`.
            clean: Toil's `--clean`: `'always'`, `'onSuccess'` or `'never'`; the other runners take `'always'`.
            tool_registry: As `Workflow.compile` takes it.
            inference: As `Workflow.compile` takes it.

        Raises:
            SophiosError: The workflow does not compile; `wic057` when the site cannot be reached.
            ValueError: An argument that names no target or runner, or that the runner does not take.
        """
        return self._prepare(workflow, target, runner, outdir, max_parallel, clean, tool_registry, inference)[0]

    # pylint: disable-next=too-many-arguments,too-many-locals
    def submit(self, workflow: 'Workflow', *, target: str = 'cwl', runner: str | None = None,
               outdir: str | None = None, max_parallel: int = MAX_PARALLEL, clean: str = 'always',
               tool_registry: Tools | None = None, inference: Inference = Inference()) -> 'Run':
        """Start `workflow` there, as `check` checks it, and return its run at once, having printed its id, its
        settings with where each comes from, and where its outputs go. The run outlives ssh and Python.

        Every argument is `check`'s. On Slurm, when the call that submits fails or its answer is lost, a job named
        after the run is adopted, or the run is submitted again: its claim lets only one leader run it.

        Raises:
            SophiosError: Every error `check` returns, nothing written; `wic057` when the run could not be started,
                saying how to attach to it should it have started all the same.
            ValueError: As `check` raises it.
        """
        diagnostics, record, files = self._prepare(workflow, target, runner, outdir, max_parallel, clean,
                                                   tool_registry, inference)
        if any(diagnostic.severity is Severity.ERROR for diagnostic in diagnostics):
            raise SophiosError(diagnostics)
        report(diagnostics)
        run = Run(self, record)
        print(_announce(self.host, record), flush=True)
        try:
            said = _call(self.host, self._start(run._folder, record, files))
        except SophiosError as error:
            try:
                said = self._again(run._folder, files['job.sh'], error)
            except SophiosError as failed:
                raise SophiosError([*failed.diagnostics, _error(Code.SITE_UNREACHABLE, f'run {run.id} may have started '
                                                                f'all the same: {run!r} follows it')]) from None
        if said.strip():
            print(f'Slurm has it as job {" ".join(said.decode().split())}.')
        print(f'From anywhere, {run!r} follows it.')
        return run

    def attach(self, run_id: str) -> 'Run':
        """The run `run_id` there, from any computer: its folder is its record.

        Raises:
            SophiosError: `wic059` when it names no run there; `wic057` when the site cannot be reached.
            ValueError: `run_id` is not a run id.
        """
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError(f'{run_id!r} is not a run id: <workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>')
        record = _Said(_call(self.host, f'{_PUT}\nput {_q(_run_folder(self.root, run_id) + "/run.json")} 0')).file()
        if record is None:
            raise SophiosError.error(Code.SITE_RUN_UNKNOWN, f'{self.host} has no run {run_id} under '
                                     f'{self.root}/sophios/runs: check the id, the host and root=')
        return Run(self, json.loads(record))

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
    def _prepare(self, workflow: 'Workflow', target: str, runner: str | None, outdir: str | None,
                 max_parallel: int, clean: str, tool_registry: Tools | None,
                 inference: Inference) -> tuple[list[Diagnostic], Json, dict[str, str]]:
        """`check`'s diagnostics, and what `submit` writes: the run's record and its files, by their path in it, when
        there is no error."""
        default = next(name for name, each in targets.target(target).runners.items() if each.program in self._RUNNERS)
        chosen = runner if runner is not None else default
        backend = targets.backend(target, chosen)
        program = backend.program
        if program not in self._RUNNERS:
            return [_error(Code.SITE_RUNNER, f"no site runs {program}: leave runner= out for {default}, a site's "
                           f'default for target={target!r}')], {}, {}
        if outdir is not None and program == 'nextflow':
            raise ValueError("outdir= is a CWL runner's --outdir: a Nextflow run's outputs stay in its work folder, "
                             'where the Nextflow target leaves them')
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise ValueError(f'max_parallel={max_parallel!r}: a whole number of steps at once')
        if clean not in _CLEAN or (clean != 'always' and program != 'toil-cwl-runner'):
            raise ValueError(f"clean={clean!r}: Toil takes {', '.join(_CLEAN)}; {program} leaves no scratch of its "
                             "own outside its outputs, and takes 'always'")
        root = PurePosixPath(self.root)
        name = workflow.process_name
        run_id = f'{name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}'
        run = _run_folder(self.root, run_id)
        given = outdir is not None
        outdir = str(root / outdir) if outdir is not None else f'{run}/out'
        if not _FOLDER.fullmatch(outdir):
            raise ValueError(f'outdir={outdir!r} is not a folder a site takes: it matches {_FOLDER.pattern}')
        lowered, diagnostics = lowered_for_site(workflow, target=target, root=root, tool_registry=tool_registry,
                                                inference=inference)
        if lowered is None:
            return list(diagnostics), {}, {}
        with tempfile.TemporaryDirectory(prefix='sophios-site-') as staging:
            written = backend.write(lowered, (), Path(staging), inputs_file='', root_dir=Path(root))
            needs = backend.needs(written.documents, (_job_values(lowered, root),))
            files = {path.relative_to(staging).as_posix(): path.read_text(encoding='utf-8')
                     for path in sorted(Path(staging).rglob('*')) if path.is_file()}
        refused = self._docker_only(program, lowered)
        notes = _memory_notes(lowered.view, self.host) if program == 'toil-cwl-runner' else []
        problems, queue = self._ask(program, needs, outdir if given else None, refused, lowered.view)
        found = [*diagnostics, *notes, *refused, *problems]
        if any(diagnostic.severity is Severity.ERROR for diagnostic in found):
            return found, {}, {}
        if program == 'nextflow':
            config = (nextflow_directory(Path(), written.name) / NEXTFLOW_CONFIG).as_posix()
            files[config] += _nextflow(run, max_parallel, queue)
        launch, files['job.sh'] = self._job(RunRequest(
            written.name, Path(run), runner=chosen, target=target, container_engine=self.container_engine,
            outdir='' if program == 'nextflow' else outdir), max_parallel, clean, written.folders,
            queue)
        record: Json = {
            'id': run_id, 'workflow': written.name, 'target': target, 'site': repr(self), 'sophios': __version__,
            'runner': [chosen, f"a site's default for {target}" if runner is None else 'runner='],
            **({'partition': list(queue.partition), 'time': list(queue.time)} if queue is not None else {}),
            'max_parallel': [max_parallel, "Sophios's default" if max_parallel == MAX_PARALLEL else 'max_parallel='],
            'clean': [clean, "Sophios's default" if clean == 'always' else 'clean='],
            'outdir': ([launch.outdir, "Nextflow's work folder, where the Nextflow target leaves its outputs"]
                       if program == 'nextflow' else [outdir, 'outdir=' if given else "Sophios's default"])}
        files['run.json'] = json.dumps(record, indent=2) + '\n'
        return found, record, files

    def _docker_only(self, program: str, lowered: Lowered[Any]) -> list[Diagnostic]:
        """`wic060` at each Nextflow process with a container, unless the engine is Docker, which the pipeline a site
        runs needs."""
        if program != 'nextflow' or self.container_engine == 'docker':
            return []
        steps = nextflow_names(lowered.view)['processes']
        return [_error(Code.SITE_NEXTFLOW_CONTAINER, f"step '{steps[process.name]}' runs in a container, and a site "
                       f'runs Nextflow\'s containers with Docker only, which {self.host}\'s container_engine='
                       f"{self.container_engine!r} is not: use container_engine='docker', or target='cwl'")
                for process in lowered.model.processes if process.container is not None]

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
    def _ask(self, program: str, needs: preflight.Needs, outdir: str | None, refused: list[Diagnostic],
             view: ResolvedWorkflow) -> tuple[list[Diagnostic], _Queue | None]:
        """Every problem the site has with the run, in one call: each check a command printing one line, and a
        judge of that line; and the scheduler's questions, which its `_place` answers with where the run's jobs go."""
        root = PurePosixPath(self.root)
        problems, asks = self._inputs(needs.jobs)
        asks.insert(0, ('uname -s', self._platform))
        flag = '-version' if program == 'nextflow' else '--version'
        asks.append((f'runs {_q(f"{self.bin}/{program}")} {flag}', partial(self._runner, program, flag)))
        engine = 'docker' if program == 'nextflow' else self.container_engine
        if needs.containers and not refused:
            probe = 'info' if engine in preflight.DOCKER_LIKE else '--version'
            asks.append((f'probe {_q(engine)} {probe}', partial(self._engine, engine, probe, needs.images)))
        asks.append((f'writable {_q(f"{root}/sophios")}', partial(
            self._writable, f'{root}/sophios', 'its runs', 'name another root=')))
        if outdir is not None:
            asks.append((f'new {_q(outdir)}', partial(self._writable, outdir, 'the outputs', 'name a new outdir=')))
        questions = self._questions()
        script = '\n'.join([*self._environment(), _CHECKS, *(command for command, _ in asks), *questions])
        lines = _call(self.host, script).decode('utf-8', 'replace').removesuffix('\n').split('\n')
        if len(lines) != len(asks) + len(questions):
            raise _unreachable(self.host, f'its checks printed {len(lines)} lines for '
                               f'{len(asks) + len(questions)} questions')
        more, queue = self._place(lines[len(asks):], view)
        return problems + [found for (_, judge), line in zip(asks, lines) if (found := judge(line)) is not None] + \
            more, queue

    def _inputs(self, jobs: Iterable[preflight.Job]) -> tuple[list[Diagnostic], list[_Ask]]:
        """`wic016` at each File and Directory path a site cannot read as written, and the question for each other."""
        problems: list[Diagnostic] = []
        asks: list[_Ask] = []
        found = [(name, value) for job in jobs for name, value in input_paths(absolute_paths(job.values, job.base))]
        for name, value in found:
            if (path := preflight.written_path(value)) is None:
                continue
            parts = PurePosixPath(path).parts
            if '..' in parts or '$' in path or any(part.startswith('~') for part in parts):
                problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' names {path}, which a site cannot "
                                       'read as written: no shell there expands `~` or `$`, and `..` leaves your '
                                       'project folder: write it relative to your project folder, as data/plate1'))
            else:
                asks.append((f'kind {_q(path)}', partial(self._input, name, value['class'], path)))
        return problems, asks

    def _platform(self, said: str) -> Diagnostic | None:
        if said in ('Darwin', 'Linux'):
            return None
        return _error(Code.SITE_UNREACHABLE, f'{self.host} runs {said or "no uname"}, and a site is macOS or Linux')

    def _input(self, name: str, kind: str, path: str, said: str) -> Diagnostic | None:
        if said == kind:
            return None
        why = {'missing': f'does not exist there: copy your data to {self.host}, or correct the path',
               'unreadable': 'you may not read there'}.get(said, f'is not a {kind} there')
        return _error(Code.MISSING_INPUT_FILE, f"input '{name}' names {path} on {self.host}, which {why}")

    def _runner(self, program: str, flag: str, said: str) -> Diagnostic | None:
        path, oldest = f'{self.bin}/{program}', '.'.join(map(str, OLDEST[program]))
        status, _, output = said.partition(' ')
        versions = re.findall(r'(\d+)\.(\d+)\.(\d+)', output)
        if said == 'missing':
            why = f'there is no {path} you can run: install {program} in bin=, or name the folder that holds it'
        elif status != '0':
            why = (f'{path} {flag} exited with status {status} ({output.strip()[:300] or "it said nothing"})'
                   + (': Nextflow needs a Java it can run, in bin=' if program == 'nextflow' else ''))
        elif versions and tuple(map(int, versions[-1])) >= OLDEST[program]:
            return None
        else:
            why = (f'{path} is {".".join(versions[-1]) if versions else "of no version Sophios can read"}, and a '
                   f'site runs {program} {oldest} or later: install it in bin=')
        return _error(Code.SITE_RUNNER, f'on {self.host}, {why}')

    def _engine(self, engine: str, probe: str, images: list[str], said: str) -> Diagnostic | None:
        status, _, output = said.partition(' ')
        if status == '0':
            return None
        names = list(dict.fromkeys(images))
        uses = names[0] if len(names) == 1 else f'{names[0]} and {len(names) - 1} more'
        if said == 'missing':
            return _error(Code.CONTAINER_ENGINE_UNAVAILABLE, f'{engine} is not on PATH on {self.host}, and this '
                          f'workflow runs tools in containers ({uses}): install it there, or name another with '
                          'container_engine=')
        return _error(Code.CONTAINER_ENGINE_UNAVAILABLE, f'`{engine} {probe}` exited with status {status} on '
                      f'{self.host} ({output.strip()[:300] or "it said nothing"}): start the engine there, or give '
                      'your user access to it')

    def _writable(self, path: str, holds: str, remedy: str, said: str) -> Diagnostic | None:
        if said == 'yes':
            return None
        why = 'exists already' if said == 'exists' else f'cannot be made: {said[3:]} is not a folder you can write'
        return _error(Code.DIRECTORY_NOT_WRITABLE, f'Sophios writes {holds} to {path} on {self.host}, which {why}: '
                      f'{remedy}')

    def _environment(self) -> list[str]:
        """What every job and check runs first, so a check sees what the job will: no Toil or Python setting of your
        profile's, your modules, and `bin` first on PATH."""
        return ['unset "${!TOIL_@}" "${!PYTHON@}"', 'export PYTHONNOUSERSITE=1 TOIL_HISTORY=False',
                *([f'module load {" ".join(map(_q, self.modules))}'] if self.modules else []),
                f'export PATH={_q(self.bin)}:"$PATH"']

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def _job(self, request: RunRequest, max_parallel: int, clean: str, folders: Iterable[str],
             queue: _Queue | None) -> tuple[Launch, str]:
        """The runner's launch for `request`, with what the site adds to it (`_RUNNERS`), and `job.sh`, the leader
        script (design Appendix A): on Slurm its header, then the claim, by the leader's pid or job id, the record of
        how it ended in `exit` whatever happens after the claim, `folders` made, and the launch run from `bin`."""
        backend, run = targets.backend(request.target, request.runner), str(request.basepath)
        adds = self._RUNNERS[backend.program](run, max_parallel, clean, queue)
        launch = backend.launch(replace(request, passthrough_args=adds))
        if backend.program == 'toil-cwl-runner':
            folders = (*folders, f'{run}/toil/work', f'{run}/toil/tmp', *([f'{run}/batch'] if queue else []))
        leader, images = _q(f'{run}/leader'), f'{self.root}/sophios/images'
        caches = ([f'export CWL_SINGULARITY_CACHE={_q(images)}',
                   *(f'export {name}=${{{name}:-{_q(f"{images}/cache")}}}'
                     for name in ('SINGULARITY_CACHEDIR', 'APPTAINER_CACHEDIR'))]
                  if self.container_engine == 'singularity' and backend.program != 'nextflow' else [])
        argv = (f'{self.bin}/{backend.program}', *launch.argv[1:])
        claimant = f'"{self._CLAIMANT}"'
        return launch, '\n'.join([
            '#!/bin/bash -l', *_header(run, queue), 'set -euo pipefail',
            f'ln -s {claimant} {leader} 2> /dev/null || [ -L {leader} ] || ln -s {claimant} {leader}',
            f'[ "$(readlink {leader})" = {claimant} ] || exit 0',
            f'trap {_q(_recorded(run, backend.program, clean))} EXIT',
            "trap 'exit 143' TERM", *self._environment(), *caches,
            *([f'unset {" ".join(_TASK_VARIABLES)}'] if queue is not None and backend.program == 'nextflow' else []),
            *(f'export {name}={_q(value)}' for name, value in launch.env.items()),
            *(f'mkdir -p {_q(folder)}' for folder in folders), f'cd {_q(str(launch.cwd or run))}',
            f'{{ {shlex.join(argv)} | tee {_q(f"{run}/stdout")}; }} >> {_q(f"{run}/run.log")} 2>&1', ''])

    def _start(self, run: str, record: Json, files: Mapping[str, str]) -> str:
        """The one call that starts a run: hang-ups ignored, so a dropped connection cannot stop it between the leader
        and the record; the run's folder and its outdir made, each only if new; the files; and the leader."""
        outdir = record['outdir'][0]
        made = [] if record['runner'][0] == 'nextflow' else [f'mkdir -p {_q(str(PurePosixPath(outdir).parent))}',
                                                             f'mkdir {_q(outdir)}']
        return '\n'.join([
            "trap '' HUP PIPE", 'set -e', f'mkdir -p {_q(f"{self.root}/sophios/runs")}', f'mkdir {_q(run)}', *made,
            *(f'mkdir -p {_q(f"{run}/{parent}")}' for parent in sorted({str(PurePosixPath(path).parent)
                                                                        for path in files} - {'.'})),
            *(f'printf %s {_q(text)} > {_q(f"{run}/{path}")}' for path, text in files.items()),
            *self._leader(run, files['job.sh'])])

    # The scheduler's part: a machine's, which `Slurm` replaces.

    def _cancelling(self, run: str) -> list[str]:
        """The lines that cancel the run in the folder they run in: claimed `cancelled`, so a job that has not
        started never will, then its leader, whose pid the claim names, stopped with TERM to its process group."""
        return [_alive(run), 'ln -s cancelled leader 2> /dev/null || true', 'leader=$(readlink leader)',
                'if alive "$leader"; then kill -TERM -- "-$leader"; fi']

    def _questions(self) -> list[str]:
        """The scheduler's questions a check asks, each printing one line."""
        return []

    def _place(self, said: Sequence[str], view: ResolvedWorkflow) -> tuple[list[Diagnostic], _Queue | None]:
        """What the answers to `_questions` say: the scheduler's problems with the run, and where its jobs go."""
        del said, view
        return [], None

    def _leader(self, run: str, job: str) -> list[str]:
        """The lines that start the leader, `job` being its script: on a machine, in a process group of its own
        (`set -m`), which outlives the call and which `cancel` stops."""
        del job
        return [f'cd {_q(run)}', 'set -m', f'nohup bash -l {_q(f"{run}/job.sh")} > {_q(f"{run}/run.log")} 2>&1 '
                '< /dev/null &']

    def _again(self, run: str, job: str, error: SophiosError) -> bytes:
        """When the call that starts a run failed or its answer was lost, the run's one leader, by another call; on a
        machine, which starts it once, `error`."""
        del run, job
        raise error

    def _asked(self, run_id: str) -> list[str]:
        """The lines a status asks the scheduler with, before it reads the folder."""
        del run_id
        return []

    # pylint: disable-next=useless-return  # a machine is asked nothing
    def _queue(self, run_id: str, said: _Said, now: int) -> Callable[[], Sequence[Job]] | None:
        """What `_asked` said: the run's jobs, as `decide` asks for them; None on a machine."""
        del run_id, said, now
        return None


@dataclass(frozen=True)
class Machine(_Site):
    """A computer of your own you can `ssh` to, macOS or Linux, as a site: a workstation or a cloud instance.

    Args:
        host: How `ssh` names it: a `Host` of `~/.ssh/config`, `host`, or `user@host`.
        root: The folder there that relative paths are read against, and where `sophios/runs/<id>` is made.
        bin: The folder there that holds the runners, and what they need (the Java Nextflow runs on); first on PATH.
        container_engine: The engine a CWL runner runs containers with; the Nextflow pipeline a site runs needs Docker.
        modules: Environment modules each job loads first.

    Raises:
        SophiosError: `wic057` on Windows itself, as `sophios.compute.Service` is refused there: a site's paths are
            POSIX paths, so no call of a site is made from it.
        ValueError: A setting that does not fit its pattern: none takes shell code.
    """

    host: str
    _: KW_ONLY
    root: str
    bin: str
    container_engine: str = 'docker'
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class Slurm(_Site):  # pylint: disable=too-many-instance-attributes  # the design's settings
    """A Slurm cluster's login node you can `ssh` to, as a site: each run is one leader job, and each of Toil's steps
    and Nextflow's tasks a job of its own (design Appendix A). Its compute nodes share `root`'s filesystem.

    Args:
        host, root, bin, modules: As `Machine` takes them.
        container_engine: The engine a CWL runner runs containers with: `singularity`, which Apptainer provides.
        account: The account of the leader and of every step; None for your `SBATCH_ACCOUNT`, else Slurm's default.
        partition: The leader's partition and every step's; None for your `SBATCH_PARTITION`, else the default one.
        gpu_partition: The partition of each step that asks for a GPU; None for the one partition with GPUs.
        time: The leader's time limit and every step's; None for your `SBATCH_TIMELIMIT`, else the partition's
            longest, and for the steps the shortest limit of their partitions.

    Raises:
        SophiosError: `wic057` on Windows itself, as a `Machine`.
        ValueError: A setting that does not fit its pattern: none takes shell code.
    """

    host: str
    _: KW_ONLY
    root: str
    bin: str
    container_engine: str = 'singularity'
    account: str | None = None
    partition: str | None = None
    gpu_partition: str | None = None
    time: str | None = None
    modules: tuple[str, ...] = ()
    _RUNNERS: ClassVar = ON_SLURM
    _OPTIONAL: ClassVar = {'account': _NAME, 'partition': _NAME, 'gpu_partition': _NAME, 'time': _TIME}
    _CLAIMANT: ClassVar = '$SLURM_JOB_ID'

    def _questions(self) -> list[str]:
        """Each Slurm command, then your `SBATCH_` settings a run reads and those that send jobs to another cluster."""
        return [*(f'runs {command}' for command in _SLURM),
                *(f'variable {name}' for name in ('SBATCH_ACCOUNT', 'SBATCH_PARTITION', 'SBATCH_TIMELIMIT',
                                                  'SBATCH_CLUSTERS', 'SLURM_CLUSTERS'))]

    # pylint: disable-next=too-many-locals  # the conventions' table, row by row
    def _place(self, said: Sequence[str], view: ResolvedWorkflow) -> tuple[list[Diagnostic], _Queue | None]:
        """`wic062` at each Slurm command that does not answer, at each of your settings that sends jobs to another
        cluster or that a job script cannot carry, and at a partition the run needs that `sinfo -a` does not list;
        else where the run's jobs go, each setting from the one that names it, else by the convention: a GPU step's,
        under Toil and Nextflow alike, the one partition with GPUs."""
        account, partition, limit, *clusters = said[len(_SLURM):]
        problems = [_error(Code.SITE_SLURM, f'on {self.host}, `{command}` ' + (
            'is not on PATH: modules= loads the one a cluster provides' if line == 'missing' else
            f'exited with status {line.partition(" ")[0]} ({line.partition(" ")[2].strip()[:300]})'
            + (': a run is read from sacct, so accounting must answer' if command.startswith('sacct') else '')))
            for command, line in zip(_SLURM, said) if not line.startswith('0 ')]
        problems += [_error(Code.SITE_SLURM, f'your login profile on {self.host} sets {name}={value.strip()}, which '
                            'sends jobs to another cluster, where no runner follows its step jobs: remove it there')
                     for name, value in zip(('SBATCH_CLUSTERS', 'SLURM_CLUSTERS'), clusters) if value.strip()]
        problems += [_error(Code.SITE_SLURM, f'your {name} on {self.host}, {value!r}, is not one a job script can '
                            f'carry: it matches {pattern.pattern}')
                     for name, value, pattern in (('SBATCH_ACCOUNT', account, _NAME),
                                                  ('SBATCH_PARTITION', partition, _NAME),
                                                  ('SBATCH_TIMELIMIT', limit, _TIME))
                     if value and not pattern.fullmatch(value)]
        if problems:
            return problems, None
        rows = [row.split('|') for row in said[len(_SLURM) - 1].split(' ')[1:] if row.count('|') == 2]
        limits = {name.rstrip('*'): row_limit for name, row_limit, _ in rows}
        default = next((name[:-1] for name, _, _ in rows if name.endswith('*')), None)
        gpus = sorted({name.rstrip('*') for name, _, gres in rows if gres != '(null)'})
        chosen, source = ((self.partition, 'partition=') if self.partition else
                          (partition, 'your SBATCH_PARTITION') if partition else (default, "the cluster's default"))
        gpu = None
        if _gpu_steps(view):
            gpu = self.gpu_partition or (gpus[0] if len(gpus) == 1 else '')
        needed = [(chosen, source, 'partition='),
                  *([(gpu, 'gpu_partition=' if self.gpu_partition else f"of {len(gpus)} with GPUs, the one a GPU "
                      'step goes to', 'gpu_partition=')] if gpu is not None else [])]
        problems = [_error(Code.SITE_SLURM, f'{self.host} has no partition {f"{name} " if name else ""}({named}): '
                           f"sinfo -a lists {', '.join(limits) or 'none'}; name one with {option}")
                    for name, named, option in needed if name not in limits]
        if problems or chosen is None:
            return problems, None
        longest, shortest = limits[chosen], min((limits[name] for name in (chosen, gpu) if name), key=_seconds)
        leader_time = ((self.time, 'time=') if self.time else (limit, 'your SBATCH_TIMELIMIT') if limit else
                       ('UNLIMITED' if longest == 'infinite' else longest, "the partition's longest"))
        return [], _Queue((chosen, source), leader_time, self.account or account or None,
                          self.time or ('0' if shortest == 'infinite' else shortest), gpu)

    def _leader(self, run: str, job: str) -> list[str]:
        """`sbatch --parsable`, the `SBATCH_` variables of each option `job` sets unset: they beat its lines."""
        unset = [name for option in re.findall(r'^#SBATCH --([a-z-]+)', job, re.M)
                 for name in _SBATCH_VARIABLES[option]]
        return [f'unset {" ".join(unset)}', f'sbatch --parsable {_q(f"{run}/job.sh")}']

    def _again(self, run: str, job: str, error: SophiosError) -> bytes:
        """The job named after the run Slurm lists, adopted; else `sbatch` again on the same folder, which the claim
        makes safe."""
        run_id = _q(PurePosixPath(run).name)
        try:
            return _call(self.host, '\n'.join([
                "trap '' HUP PIPE", 'set -e', f'cd {_q(run)}',
                f'jobs=$(squeue -a -h -u "$(id -un)" -n {run_id} -o %i)',
                f'[ -n "$jobs" ] || jobs=$(sacct -n -X -P --name={run_id} --starttime={_SUBMITTED} -o JobID)',
                'if [ -n "$jobs" ]; then echo $jobs; else', *self._leader(run, job), 'fi']))
        except SophiosError as again:
            raise SophiosError([*error.diagnostics, *again.diagnostics]) from None

    def _asked(self, run_id: str) -> list[str]:
        """The jobs of the user's that squeue lists, with their comment, and the leaders sacct lists by the run's name
        since the submit, with when they ended."""
        return [_ANSWER, 'export SLURM_TIME_FORMAT=%s', 'answer squeue -a -h -u "$(id -un)" -o \'%i|%T|%r|%k|%j\'',
                f'answer sacct -n -X -P --name={_q(run_id)} --starttime={_SUBMITTED} -o JobID,State,End']

    def _queue(self, run_id: str, said: _Said, now: int) -> Callable[[], Sequence[Job]] | None:
        answers = [('squeue', *said.answer()), ('sacct', *said.answer())]

        def jobs() -> tuple[Job, ...]:
            for command, status, text in answers:
                if status:
                    raise _unreachable(self.host, f'Slurm did not answer, and nothing was decided: {command} exited '
                                       f'with status {status} ({text.strip()[:300] or "it said nothing"})')
            return _jobs(run_id, answers[0][2], answers[1][2], now)
        return jobs

    def _cancelling(self, run: str) -> list[str]:
        """`scancel` of every job named after the run, which stops one whether or not it has claimed the run, and
        only then the claim: a `scancel` Slurm refuses ends the call, which raises, having written nothing."""
        return [f'scancel -u "$(id -un)" -n {_q(PurePosixPath(run).name)} || exit 1',
                'ln -s cancelled leader 2> /dev/null || true']


def _announce(host: str, record: Json) -> str:
    """What `submit` prints first: the id, the runner, on Slurm the partition and time, `max_parallel`, and the
    outputs, each with its source."""
    (runner, runner_from), (parallel, parallel_from) = record['runner'], record['max_parallel']
    placed = ''.join(f'{name} {record[name][0]} ({record[name][1]}), ' for name in ('partition', 'time')
                     if name in record)
    return (f'Submitting {record["id"]} to {host}: {runner} ({runner_from}), {placed}at most {parallel} at once '
            f'({parallel_from}).\n'
            f'Outputs: {record["outdir"][0]} ({record["outdir"][1]})')


def _job_values(lowered: Lowered[Any], root: PurePosixPath) -> preflight.Job:
    """The values the run reads, as its runner gets them: for CWL, its job; for Nextflow, the view's bound values but
    the output-target Directories, which the run makes."""
    made = output_target_inputs(lowered.view)
    if isinstance(lowered.model, CompilationArtifact):
        values = runner_job_inputs(lowered.model.job_inputs, made)
    else:
        values = {port.name: port.value for port in lowered.view.inputs if port.has_value and port.name not in made}
    return preflight.Job(values, Path(root), 'the workflow')


def _memory_notes(view: ResolvedWorkflow, host: str) -> list[Diagnostic]:
    """`wic061` at each step Toil gives `--defaultMemory 4Gi` (decision 11), which it refuses to start on a computer
    with less: its tool declares no memory."""
    return [Diagnostic(Severity.NOTE, Code.SITE_DEFAULT_MEMORY,
                       f'its tool declares no memory, so Toil gives it 4 GiB on {host} (--defaultMemory 4Gi), and '
                       'refuses to start it on a computer with less: declare what it needs with ramMin',
                       tool.step.span, Locator(step=tool.step.name))
            for tool in tool_steps(view) if isinstance(tool.step.run, ResolvedTool)
            and not _declares_memory(tool.step.run.document, tool.step.inherited)]


def _gpu_steps(view: ResolvedWorkflow) -> bool:
    """Whether a step asks for a GPU: a `cwltool:CUDARequirement` wins at it, as Toil reads it."""
    return any(_winning(tool.step.run.document, tool.step.inherited, 'cwltool:CUDARequirement') is not None
               for tool in tool_steps(view) if isinstance(tool.step.run, ResolvedTool))


def _declares_memory(tool: Any, inherited: Iterable[Requirement]) -> bool:
    """Whether Toil gives a step its tool's memory, not `--defaultMemory`: the ResourceRequirement that wins names
    `ramMin` or `ramMax`."""
    found = _winning(tool, inherited, 'ResourceRequirement')
    return isinstance(found, Mapping) and ('ramMin' in found or 'ramMax' in found)


def _winning(tool: Any, inherited: Iterable[Requirement], cls: str) -> Any:
    """The `cls` requirement that wins at a tool step, as cwltool's lookup, which Toil asks, picks it: the tool's own
    requirement, else the one it inherits, else its own hint, else the hint it inherits; in a section, the last one
    written. None when there is none."""
    outside = {requirement.hint: requirement.body for requirement in inherited if requirement.cls == cls}
    return next((found for found in (_section(tool.get('requirements'), cls), outside.get(False),
                                     _section(tool.get('hints'), cls), outside.get(True)) if found is not None), None)


def _section(section: Any, cls: str) -> Any:
    """The last `cls` requirement a tool's requirements or hints write, in either of CWL's spellings, or None."""
    if isinstance(section, Mapping):
        return section.get(cls)
    return next((entry for entry in reversed(section or ())
                 if isinstance(entry, Mapping) and entry.get('class') == cls), None)
