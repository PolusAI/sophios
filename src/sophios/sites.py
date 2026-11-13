"""Run a workflow on another computer, as yourself, over your own ssh: a site (design_docs/sites-design.md).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it.

    from sophios.sites import Machine

    ec2 = Machine('my-ec2', root='/data/cp', bin='/opt/conda/envs/run/bin')   # a Host in ~/.ssh/config
    run = ec2.submit(workflow)            # prints the run's id, its runner and where its outputs go
    status = run.wait()

Sophios compiles and lowers the workflow here, through `sophios.targets`, and writes the run's files here. It copies
those files, never the data, into the run's folder there, `<root>/sophios/runs/<id>`, and starts the runner from
`bin` as a process of its own, which outlives ssh and Python. Nothing of Sophios is installed there, and nothing of a
site is stored here: the run folder is the record, and `repr(run)` is the line that follows the run from any
computer. A relative path the workflow reads is read against `root` there, as it is read against the working
directory here.

A `Machine` is a computer of your own, macOS or Linux: a workstation or a cloud instance, never a login node.
"""
import hashlib
import json
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import KW_ONLY, dataclass, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from . import __version__, preflight, targets
from .api.python._inference import Inference
from .api.python._workflow_runtime import lowered_for_site
from .backends import Launch, RunRequest, nextflow_directory
from .input_output_nf import NEXTFLOW_CONFIG
from .ir.artifacts import CompilationArtifact
from .ir.locations import absolute_paths, input_paths
from .ir.resolved import Requirement, ResolvedTool, ResolvedWorkflow, tool_steps
from .lang.diagnostics import Diagnostic, Locator, Severity, SophiosError, report
from .lang.error_codes import SophiosErrorCode as Code
from .run_setup import Lowered
from .run_status import RunState, RunStatus, StepStatus
from .runtime_inputs import output_target_inputs, runner_job_inputs
from .utils_nf import nextflow_names
from .wic_types import Json, Tools

if TYPE_CHECKING:
    from .api.python.workflow import Workflow

#: Every call: your own ssh, so your keys, agent, `~/.ssh/config` and jump hosts apply. It asks for nothing, so a host
#: that wants a password or a second factor is reached through a shared connection (`ControlMaster`) opened by hand.
SSH: Final = ('ssh', '-T', '-o', 'BatchMode=yes', '-o', 'RemoteCommand=none', '-o', 'ConnectTimeout=20',
              '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
#: Sophios's default for `max_parallel`: Toil's `--maxJobs`, Nextflow's `executor.queueSize`.
MAX_PARALLEL: Final = 64
#: How often `wait` reads a run's status in its first minute, in seconds; every minute after.
POLL: Final = 5.0
#: How long `cancel` waits for the run to end, in seconds.
CANCEL_WAIT: Final = 120.0
#: How long a run may go unclaimed after its submit before `status` decides its job never started, in minutes.
UNCLAIMED_MINUTES: Final = 10

#: What a setting may hold: none takes shell code. A folder is absolute, and its characters fit an `#SBATCH` line.
_HOST: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*')
_FOLDER: Final = re.compile(r'/[A-Za-z0-9/._+-]*')
_NAME: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./+-]*')
_RUN_ID: Final = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')
#: The `clean=` names, Toil's own; only Toil leaves scratch outside its outputs, so the other runners take 'always'.
_CLEAN: Final = ('always', 'onSuccess', 'never')
#: The oldest version of each runner a site runs, as the sites design was read against them
#: (design_docs/sites-design.md §7).
OLDEST: Final[Mapping[str, tuple[int, ...]]] = {'toil-cwl-runner': (9, 5, 0), 'nextflow': (26, 4, 2)}
_ENDED: Final = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)


def _q(value: str) -> str:
    """`value` as one word of a script: every value enters a script this way, and only this way."""
    return shlex.quote(value)


def _toil_on_a_machine(run: str, max_parallel: int, clean: str) -> tuple[str, ...]:
    """Toil's flags on a machine (design Appendix A): its own batch system, the scratch in the run, `clean=`."""
    return ('--batchSystem', 'single_machine', '--bypass-file-store', '--leave-tmpdir', '--no-cwl-default-ram',
            '--defaultMemory', '4Gi', '--maxJobs', str(max_parallel), '--workDir', f'{run}/toil/work',
            '--tmpdir-prefix', f'{run}/toil/tmp/t', '--tmp-outdir-prefix', f'{run}/toil/tmp/o', '--clean', clean)


def _nextflow(max_parallel: int) -> str:
    """What a site sets for Nextflow, the one function it sets it with (decision 25): a block it appends to the run's
    copy of the generated `nextflow.config`, the one configuration Nextflow reads (`-C`). At most `max_parallel` tasks
    at once."""
    return f'executor {{\n    queueSize = {max_parallel}\n}}\n'


#: What a machine adds to each runner's launch, by its program, given the run's folder, `max_parallel` and `clean`:
#: the Machine column of the design's runner-by-scheduler table, which holds every runner a site runs: no cwltool
#: (decision 22). Nextflow takes none: what a site sets for it is a block of its configuration (`_nextflow`).
ON_A_MACHINE: Final[Mapping[str, Callable[[str, int, str], tuple[str, ...]]]] = {
    'toil-cwl-runner': _toil_on_a_machine,
    'nextflow': lambda run, max_parallel, clean: (),
}


def _transport(host: str) -> list[str]:
    """The command that runs a shell command on `host`: `ssh <host>`, which the tests replace with a local `bash -c`,
    and nothing else."""
    return [*SSH, host]


def _call(host: str, script: str) -> bytes:
    """Run `script` with bash, as a login shell, on `host`, and return what it printed on its output.

    The script goes on ssh's input, and the command runs it only when its sha256 is the one sent: a login profile
    that reads its input would eat part of the script and run the rest. What the profile prints comes before the hash,
    which the command prints once the script has arrived whole, so it is never read as the script's.

    Raises:
        SophiosError: `wic057`: ssh could not be run or failed, the script did not arrive whole, or it failed there.
    """
    data = script.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    guard = ('s=$(cat; echo .); s=${s%.}; h=$(printf %s "$s" | { sha256sum 2> /dev/null || shasum -a 256; }); '
             f'[ "${{h%% *}}" = {digest} ] || {{ echo "the script Sophios sent did not arrive whole: a login '
             f'profile there reads its input" >&2; exit 1; }}; echo {digest}; eval "$s"')
    try:
        done = subprocess.run([*_transport(host), f"bash -l -c '{guard}'"], input=data, capture_output=True,
                              check=False)
    except OSError as error:
        raise _unreachable(host, f'ssh could not be run ({error})') from error
    _, marker, said = done.stdout.partition(f'{digest}\n'.encode())
    if done.returncode != 0 or not marker:
        lines = done.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise _unreachable(host, f'the call there exited with status {done.returncode} '
                           f"({' / '.join(lines[-3:]) or 'it said nothing'})")
    return said


def _unreachable(host: str, why: str) -> SophiosError:
    return SophiosError.error(Code.SITE_UNREACHABLE, f'{host} cannot be used as a site: {why}.')


#: A script's function that prints a file from byte `$2` on: its length in bytes on a line, then the bytes; -1 for no
#: file. The length is read first, so a file that grows meanwhile is cut where it was.
_PUT: Final = ('put() { if [ -f "$1" ]; then n=$(($(wc -c < "$1") - $2)); [ "$n" -gt 0 ] || n=0; echo "$n"; '
               'if [ "$n" -gt 0 ]; then tail -c +$(($2 + 1)) "$1" | head -c "$n"; fi; else echo -1; fi; }')


class _Said:
    """What a script printed: lines, and files as `_PUT` prints them."""

    def __init__(self, data: bytes) -> None:
        self._data, self._at = data, 0

    def line(self) -> str:
        """The next line, without its line break."""
        end = self._data.index(b'\n', self._at)
        found, self._at = self._data[self._at:end], end + 1
        return found.decode('utf-8', 'replace')

    def file(self) -> bytes | None:
        """The next file's bytes, None for no file."""
        size = int(self.line())
        if size < 0:
            return None
        found, self._at = self._data[self._at:self._at + size], self._at + size
        return found


def _alive(run: str) -> str:
    """A script's function: the leader whose pid `$1` is, the claim, runs `<run>/job.sh` (a reused pid runs another)."""
    return (f'alive() {{ case $1 in ""|*[!0-9]*) return 1 ;; esac; '
            f'case $(ps -ww -o args= -p "$1" 2> /dev/null) in *{_q(run + "/job.sh")}*) return 0 ;; esac; return 1; }}')


#: The checks' functions, each printing one line: what a path is, what a program printed and its exit status, and
#: whether a folder can be made.
_CHECKS: Final = '\n'.join((
    'kind() { if [ -d "$1" ]; then if [ -r "$1" ] && [ -x "$1" ]; then echo Directory; else echo unreadable; fi; '
    'elif [ -e "$1" ]; then if [ -r "$1" ]; then echo File; else echo unreadable; fi; else echo missing; fi; }',
    'said() { echo "$1 $(printf %s "$2" | tr "\\n\\r" "  ")"; }',
    'runs() { if [ -x "$1" ]; then out=$("$@" 2>&1); said $? "$out"; else echo missing; fi; }',
    'probe() { if command -v "$1" > /dev/null 2>&1; then out=$("$@" 2>&1 > /dev/null); said $? "$out"; '
    'else echo missing; fi; }',
    'writable() { p=$1; while [ ! -e "$p" ]; do p=$(dirname "$p"); done; '
    'if [ -d "$p" ] && [ -w "$p" ] && [ -x "$p" ]; then echo yes; else echo "no $p"; fi; }',
    'new() { if [ -e "$1" ] || [ -L "$1" ]; then echo exists; else writable "$1"; fi; }'))


#: A question a check asks the site: a command that prints one line, and the judge of that line.
_Ask = tuple[str, Callable[[str], Diagnostic | None]]


def _error(code: Code, message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, code, message)


@dataclass(frozen=True, slots=True)
class Folder:
    """What a run's folder says, read before anything else is asked: the claim, whether its leader runs, `exit`."""

    #: `leader`'s target: the leader's pid, `cancelled` or `none`; '' while nothing has claimed the run.
    leader: str
    #: The claimed leader runs `<run>/job.sh`, read before `exit`, so a leader found gone has left its `exit`.
    alive: bool
    #: What `exit` holds: the job script's exit status, written last; None while there is none.
    exit: int | None


# pylint: disable-next=too-many-return-statements  # the design's table: the first row that applies
def decide(folder: Folder, scratch: str | None) -> tuple[RunState, str | None]:
    """A run's state and why, from its folder alone (design Appendix B, on a machine): the first row that applies.

    `scratch` is the folder a hard kill leaves (Toil's `<run>/toil`), None for a runner that leaves none.
    """
    if folder.exit == 0:
        return RunState.COMPLETED, None
    if folder.leader == 'cancelled' or folder.exit == 143:
        return RunState.CANCELLED, 'cancelled'
    if folder.leader == 'none':
        return RunState.FAILED, f'its job never claimed the run within {UNCLAIMED_MINUTES} minutes of the submit'
    if folder.exit is not None and folder.exit <= 128:
        return RunState.FAILED, f'the runner exited with status {folder.exit}'
    if folder.exit is not None:
        return RunState.FAILED, (f'the runner ended with status {folder.exit}, a kill by signal {folder.exit - 128}: '
                                 'out of memory, or by hand')
    if not folder.leader:
        return RunState.QUEUED, None
    if folder.alive:
        return RunState.RUNNING, None
    left = f'; {scratch} is left: rm -rf {scratch} removes it' if scratch else ''
    return RunState.FAILED, ('its job was killed before it recorded how the run ended (SIGKILL, out of memory, or '
                             f'the computer stopped){left}')


@dataclass(frozen=True)
class Machine:
    """A computer of your own you can `ssh` to, macOS or Linux, as a site: a workstation or a cloud instance.

    Args:
        host: How `ssh` names it: a `Host` of `~/.ssh/config`, `host`, or `user@host`.
        root: The folder there that relative paths are read against, and where `sophios/runs/<id>` is made.
        bin: The folder there that holds the runners, and what they need (the Java Nextflow runs on); first on PATH.
        container_engine: The engine a CWL runner runs containers with; the Nextflow target's need Docker.
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

    def __post_init__(self) -> None:
        if sys.platform == 'win32':
            raise _unreachable(self.host, 'Sophios runs on Windows itself, whose paths no site reads: run it inside '
                               'WSL')
        if isinstance(self.modules, str):
            raise ValueError(f'modules={self.modules!r}: modules= takes the names, as a tuple')
        object.__setattr__(self, 'modules', tuple(self.modules))
        for name, value, pattern in (('host', self.host, _HOST), ('root', self.root, _FOLDER),
                                     ('bin', self.bin, _FOLDER), ('container_engine', self.container_engine, _NAME),
                                     *(('modules', module, _NAME) for module in self.modules)):
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
        in a container, that `<root>/sophios` can be written, and that `outdir` does not exist yet. Under Toil, a
        note, `wic061`, at each step whose tool declares no memory: Toil gives it 4 GiB there. cwltool, which no site
        runs, is refused, `wic058`, before anything is compiled or sent.

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

    # pylint: disable-next=too-many-arguments
    def submit(self, workflow: 'Workflow', *, target: str = 'cwl', runner: str | None = None,
               outdir: str | None = None, max_parallel: int = MAX_PARALLEL, clean: str = 'always',
               tool_registry: Tools | None = None, inference: Inference = Inference()) -> 'Run':
        """Start `workflow` there, as `check` checks it, and return its run at once, having printed its id, its
        settings with where each comes from, and where its outputs go. The run outlives ssh and Python.

        Every argument is `check`'s.

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
            _call(self.host, self._start(_run_folder(self.root, run.id), record, files))
        except SophiosError as error:
            raise SophiosError([*error.diagnostics, _error(
                Code.SITE_UNREACHABLE, f'run {run.id} may have started all the same: {run!r} follows it')]) from None
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
        """`check`'s diagnostics, and what `submit` writes: the run's record and its files, by their path in it."""
        default = next(name for name, each in targets.target(target).runners.items() if each.program in ON_A_MACHINE)
        chosen = runner if runner is not None else default
        backend = targets.backend(target, chosen)
        program = backend.program
        if program not in ON_A_MACHINE:
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
        problems = self._ask(program, needs, outdir if given else None, refused)
        if program == 'nextflow':
            files[(nextflow_directory(Path(), written.name) / NEXTFLOW_CONFIG).as_posix()] += _nextflow(max_parallel)
        launch, files['job.sh'] = self._job(RunRequest(
            written.name, Path(run), runner=chosen, target=target, container_engine=self.container_engine,
            outdir='' if program == 'nextflow' else outdir), max_parallel, clean, written.folders)
        record: Json = {
            'id': run_id, 'workflow': written.name, 'target': target, 'site': repr(self), 'sophios': __version__,
            'runner': [chosen, f"a site's default for {target}" if runner is None else 'runner='],
            'max_parallel': [max_parallel, "Sophios's default" if max_parallel == MAX_PARALLEL else 'max_parallel='],
            'clean': [clean, "Sophios's default" if clean == 'always' else 'clean='],
            'outdir': ([launch.outdir, "Nextflow's work folder, where the Nextflow target leaves its outputs"]
                       if program == 'nextflow' else [outdir, 'outdir=' if given else "Sophios's default"])}
        files['run.json'] = json.dumps(record, indent=2) + '\n'
        return [*diagnostics, *notes, *refused, *problems], record, files

    def _docker_only(self, program: str, lowered: Lowered[Any]) -> list[Diagnostic]:
        """`wic060` at each Nextflow process with a container, unless the engine is Docker, the Nextflow target's."""
        if program != 'nextflow' or self.container_engine == 'docker':
            return []
        steps = nextflow_names(lowered.view)['processes']
        return [_error(Code.SITE_NEXTFLOW_CONTAINER, f"step '{steps[process.name]}' runs in a container, and the "
                       f'Nextflow target runs containers with Docker only, which {self.host}\'s container_engine='
                       f"{self.container_engine!r} is not: use container_engine='docker', or target='cwl'")
                for process in lowered.model.processes if process.container is not None]

    def _ask(self, program: str, needs: preflight.Needs, outdir: str | None,
             refused: list[Diagnostic]) -> list[Diagnostic]:
        """Every problem the site has with the run, in one call: each check a command printing one line, and a
        judge of that line."""
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
        script = '\n'.join([*self._environment(), _CHECKS, *(command for command, _ in asks)])
        lines = _call(self.host, script).decode('utf-8', 'replace').removesuffix('\n').split('\n')
        if len(lines) != len(asks):
            raise _unreachable(self.host, f'its checks printed {len(lines)} lines for {len(asks)} questions')
        return problems + [found for (_, judge), line in zip(asks, lines) if (found := judge(line)) is not None]

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

    def _job(self, request: RunRequest, max_parallel: int, clean: str,
             folders: Iterable[str]) -> tuple[Launch, str]:
        """The runner's launch for `request`, with what a machine adds to it (`ON_A_MACHINE`), and `job.sh`, the
        leader script (design Appendix A, on a machine): it claims the run with its pid, records how it ended in
        `exit` whatever happens after the claim, makes `folders`, and runs the launch from `bin`."""
        backend, run = targets.backend(request.target, request.runner), str(request.basepath)
        launch = backend.launch(replace(request, passthrough_args=ON_A_MACHINE[backend.program](
            run, max_parallel, clean)))
        if backend.program == 'toil-cwl-runner':
            folders = (*folders, f'{run}/toil/work', f'{run}/toil/tmp')
        leader, images = _q(f'{run}/leader'), f'{self.root}/sophios/images'
        caches = ([f'export CWL_SINGULARITY_CACHE={_q(images)}',
                   *(f'export {name}=${{{name}:-{_q(f"{images}/cache")}}}'
                     for name in ('SINGULARITY_CACHEDIR', 'APPTAINER_CACHEDIR'))]
                  if self.container_engine == 'singularity' and backend.program != 'nextflow' else [])
        argv = (f'{self.bin}/{backend.program}', *launch.argv[1:])
        return launch, '\n'.join([
            '#!/bin/bash -l', 'set -euo pipefail',
            f'ln -s "$$" {leader} 2> /dev/null || [ -L {leader} ] || ln -s "$$" {leader}',
            f'[ "$(readlink {leader})" = "$$" ] || exit 0',
            f'trap {_q(_recorded(run, backend.program, clean))} EXIT',
            "trap 'exit 143' TERM", *self._environment(), *caches,
            *(f'export {name}={_q(value)}' for name, value in launch.env.items()),
            *(f'mkdir -p {_q(folder)}' for folder in folders), f'cd {_q(str(launch.cwd or run))}',
            f'{{ {shlex.join(argv)} | tee {_q(f"{run}/stdout")}; }} >> {_q(f"{run}/run.log")} 2>&1', ''])

    def _start(self, run: str, record: Json, files: Mapping[str, str]) -> str:
        """The one call that starts a run: hang-ups ignored, so a dropped connection cannot stop it between the leader
        and the record; the run's folder and its outdir made, each only if new; the files; and the leader, in a
        process group of its own (`set -m`), which outlives the call and which `cancel` stops."""
        outdir = record['outdir'][0]
        made = [] if record['runner'][0] == 'nextflow' else [f'mkdir -p {_q(str(PurePosixPath(outdir).parent))}',
                                                             f'mkdir {_q(outdir)}']
        return '\n'.join([
            "trap '' HUP PIPE", 'set -e', f'mkdir -p {_q(f"{self.root}/sophios/runs")}', f'mkdir {_q(run)}', *made,
            *(f'mkdir -p {_q(f"{run}/{parent}")}' for parent in sorted({str(PurePosixPath(path).parent)
                                                                        for path in files} - {'.'})),
            *(f'printf %s {_q(text)} > {_q(f"{run}/{path}")}' for path, text in files.items()),
            f'cd {_q(run)}', 'set -m',
            f'nohup bash -l {_q(f"{run}/job.sh")} > {_q(f"{run}/run.log")} 2>&1 < /dev/null &'])


def _recorded(run: str, program: str, clean: str) -> str:
    """The leader's EXIT trap: Toil's scratch removed as `clean` says, once Toil has stopped, then the exit status,
    written last."""
    scratch = f'{_q(f"{run}/toil/tmp")} {_q(f"{run}/toil/work")}'
    cleanup = (f'chmod -R u+rwX {scratch}; rm -rf {scratch}; '
               if program == 'toil-cwl-runner' and clean != 'never' else '')
    if cleanup and clean == 'onSuccess':
        cleanup = f'[ "$code" != 0 ] || {{ {cleanup}}}; '
    partial_, done = _q(f'{run}/exit.partial'), _q(f'{run}/exit')
    return f'code=$?; set +e; {cleanup}echo "$code" > {partial_}; mv {partial_} {done}'


def _announce(host: str, record: Json) -> str:
    """What `submit` prints first: the id, the runner and `max_parallel`, and the outputs, each with its source."""
    (runner, runner_from), (parallel, parallel_from) = record['runner'], record['max_parallel']
    return (f'Submitting {record["id"]} to {host}: {runner} ({runner_from}), at most {parallel} at once '
            f'({parallel_from}).\n'
            f'Outputs: {record["outdir"][0]} ({record["outdir"][1]})')


def _run_folder(root: str, run_id: str) -> str:
    return f'{root}/sophios/runs/{run_id}'


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


def _declares_memory(tool: Any, inherited: Iterable[Requirement]) -> bool:
    """Whether Toil gives a step its tool's memory, not `--defaultMemory`: the ResourceRequirement that wins names
    `ramMin` or `ramMax`. It wins as cwltool's lookup, which Toil asks, picks it: the tool's own requirement, else
    the one it inherits, else its own hint, else the hint it inherits; in a section, the last one written."""
    outside = {requirement.hint: requirement.body for requirement in inherited
               if requirement.cls == 'ResourceRequirement'}
    for found in (_resources(tool.get('requirements')), outside.get(False), _resources(tool.get('hints')),
                  outside.get(True)):
        if found is not None:
            return isinstance(found, Mapping) and ('ramMin' in found or 'ramMax' in found)
    return False


def _resources(section: Any) -> Any:
    """The last ResourceRequirement a tool's requirements or hints write, in either of CWL's spellings, or None."""
    if isinstance(section, Mapping):
        return section.get('ResourceRequirement')
    return next((entry for entry in reversed(section or ())
                 if isinstance(entry, Mapping) and entry.get('class') == 'ResourceRequirement'), None)


class Run:
    """A run on a site, made by `submit` and `attach`, never by calling the class.

    Followed as the local `RunHandle` is, `collect` aside: `status`, `wait`, `outputs`, `logs` and `cancel`; and `id`.
    Its run outlives ssh and Python, and `repr(run)` is the line that follows it from any computer. When the site
    cannot be asked, a method raises `wic057` and decides nothing.
    """

    def __init__(self, machine: Machine, record: Json) -> None:
        self._machine, self._record = machine, record
        #: The run's id: `<workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>`, the time UTC.
        self.id: str = record['id']
        self._folder = _run_folder(machine.root, self.id)
        self._backend = targets.backend(record['target'], record['runner'][0])
        self._log = b''
        self._ended: RunStatus | None = None

    def __repr__(self) -> str:
        return f'{self._machine!r}.attach({self.id!r})'

    def status(self) -> RunStatus:
        """Where the run is now, read from its folder (design Appendix B); once it has ended, as it ended, read once.

        A run whose job has not claimed it within `UNCLAIMED_MINUTES` of the submit is claimed `none`, and FAILED.

        Raises:
            SophiosError: `wic057`: the site could not be asked.
        """
        if self._ended is not None:
            return self._ended
        run = _q(self._folder)
        said = _Said(_call(self._machine.host, '\n'.join([
            f'cd {run} || exit 1', _PUT, _alive(self._folder),
            f'[ -L leader ] || [ -z "$(find run.json -mmin +{UNCLAIMED_MINUTES})" ] || ln -s none leader 2> /dev/null'
            ' || true',
            'leader=$(readlink leader 2> /dev/null || true)', 'alive "$leader" && echo "$leader yes" || '
            'echo "$leader no"',
            'mtime() { stat -c %Y "$1" 2> /dev/null || stat -f %m "$1" 2> /dev/null || echo; }',
            'mtime leader', 'mtime exit', 'put exit 0', f'put run.log {len(self._log)}',
            'if [ -f exit ]; then put stdout 0; fi'])))
        leader, alive = said.line().rsplit(' ', 1)
        claimed, ended, code = said.line(), said.line(), said.file()
        self._log += said.file() or b''
        exit_code = None if code is None else int(code)
        scratch = f'{self._folder}/toil' if self._backend.program == 'toil-cwl-runner' else None
        state, reason = decide(Folder(leader, alive == 'yes', exit_code), scratch)
        steps, outputs = (self._outcome(exit_code, said.file() or b'', state is RunState.COMPLETED)
                          if exit_code is not None and state is not RunState.CANCELLED else ((), None))
        status = RunStatus(state, reason, _when(claimed) if leader.isdigit() else None, _when(ended), steps, outputs,
                           exit_code)
        if state in _ENDED:
            self._ended = status
        return status

    def _outcome(self, exit_code: int, stdout: bytes, completed: bool) -> tuple[tuple[StepStatus, ...], Json | None]:
        """The failed steps and the outputs the backend's own outcome reads from what the run left, as here."""
        request = RunRequest(self._record['workflow'], Path(self._folder), runner=self._record['runner'][0],
                             target=self._record['target'])
        outcome = self._backend.outcome(request, exit_code, stdout.decode('utf-8', 'replace'),
                                        self._log.decode('utf-8', 'replace'), self._read)
        steps = tuple(StepStatus(name, RunState.FAILED, ended if isinstance(ended, int) else None,
                                 ended if isinstance(ended, str) else None) for name, ended in outcome.failed.items())
        return steps, outcome.outputs if completed else None

    def _read(self, name: str) -> str | None:
        """The text of the file `name` in the run's folder there, or None: how the backend's outcome reads it."""
        found = _Said(_call(self._machine.host, f'cd {_q(self._folder)} || exit 1\n{_PUT}\nput {_q(name)} 0')).file()
        return None if found is None else found.decode('utf-8', 'replace')

    def wait(self, timeout: float | None = None) -> RunStatus:
        """Read the status every `POLL` seconds for a minute, then every minute, until the run ends, printing what
        `run.log` gained and each change; a run that FAILED or was CANCELLED is returned, not raised.

        Raises:
            TimeoutError: It had not ended within `timeout` seconds; it goes on.
            KeyboardInterrupt: On Ctrl-C, having printed how to follow it again; it goes on.
            SophiosError: As `status` raises it.
        """
        began, shown = time.monotonic(), None
        try:
            while True:
                before = len(self._log)
                status = self.status()
                sys.stdout.write(self._log[before:].decode('utf-8', 'replace'))
                if (status.state, status.reason) != shown:
                    shown = (status.state, status.reason)
                    print(f'{self.id}: {status.state.value}' + (f' ({status.reason})' if status.reason else ''),
                          flush=True)
                if status.state in _ENDED:
                    return status
                waited = time.monotonic() - began
                if timeout is not None and waited >= timeout:
                    raise TimeoutError(f'run {self.id} has not ended within {timeout:g} s; it goes on, and '
                                       f'{self!r}.wait() waits again')
                time.sleep(min(POLL if waited < 60 else 60.0, float('inf') if timeout is None else timeout - waited))
        except KeyboardInterrupt:
            print(f'\nRun {self.id} goes on: {self!r} follows it again, and its cancel() stops it.', file=sys.stderr)
            raise

    def outputs(self) -> Json | None:
        """The workflow's output object, once the run COMPLETED; else None."""
        return self.status().outputs

    def logs(self) -> str:
        """`run.log` so far: the job script's and the runner's two streams, as they came."""
        self.status()
        return self._log.decode('utf-8', 'replace')

    def cancel(self) -> bool:
        """Claim the run `cancelled`, so a job that has not started never will, or stop its leader's process group
        with TERM, then wait up to `CANCEL_WAIT` seconds for it to end.

        Returns:
            bool: True when the run ended CANCELLED, by this call or before it; False when it ended COMPLETED or
            FAILED, which it stays, or had not ended within `CANCEL_WAIT`.

        Raises:
            SophiosError: As `status` raises it.
        """
        _call(self._machine.host, '\n'.join([
            f'cd {_q(self._folder)} || exit 1', _alive(self._folder), 'ln -s cancelled leader 2> /dev/null || true',
            'leader=$(readlink leader)', 'if alive "$leader"; then kill -TERM -- "-$leader"; fi']))
        deadline = time.monotonic() + CANCEL_WAIT
        while (status := self.status()).state not in _ENDED and time.monotonic() < deadline:
            time.sleep(POLL)
        return status.state is RunState.CANCELLED


def _when(seconds: str) -> datetime | None:
    return datetime.fromtimestamp(int(seconds)) if seconds.isdigit() else None


__all__ = ['Machine', 'Run']
