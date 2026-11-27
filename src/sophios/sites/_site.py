"""A site, `Machine`, which checks, submits and attaches a run, and the base class every site shares, which `Slurm`
(`_slurm`) fills in too."""
# pylint: disable=protected-access  # a site starts its run in the run's folder
import json
import posixpath
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

from .. import __version__, targets
from ..api.python._inference import Inference
from ..api.python._workflow_runtime import _view, compile_workflow_result
from ..cwl import post_compile
from ..ir.locations import input_paths
from ..ir.refusals import NotLowered
from ..ir.resolved import Requirement, ResolvedWorkflow, bind, tool_steps
from ..lang.diagnostics import Diagnostic, Locator, Severity, SophiosError, report
from ..lang.error_codes import SophiosErrorCode as Code
from ..lang.names import WRITTEN_NAME
from ..nextflow.lower import nextflow_names
from ..nextflow.render import NEXTFLOW_CONFIG, NF_ENGINE_CONFIG
from ..runtime import preflight
from ..runtime.backends import RunRequest, nextflow_directory
from ..runtime.inputs import output_target_inputs
from ..runtime.runners import (SHELL_PATH, URL_SCHEME, Cwltool, LogLevel, Nextflow, RunConfig, Runner, RunSettings,
                               SlurmSettings, Toil, build, read_config)
from ..runtime.setup import Lowered
from ..wic_types import Json, TargetName, Tool, Tools

from ._run import Run, _run_folder
from ._runners import (DEFAULT_MEMORY_GIB, DEFAULT_RUNNER, OLDEST, ON_A_MACHINE, SAMPLE, argv, nextflow_block,
                       nextflow_config, read_runner, set_by_the_site, _recorded)
from ._ssh import _ALIVE, _CHECKS, _PUT, _USED, URL, _Said, _call, _q, _unreachable, host
from ._states import UNCLAIMED_MINUTES, Job

if TYPE_CHECKING:
    from ..api.python.workflow import Workflow


#: A folder every runner can run in and from, as the compute service takes its own: Toil runs each step's command
#: through a shell, its job store, in `runs`, on that command, and `bin` is a folder of PATH, which `:` splits.
_FOLDER: Final = re.compile(r'/[A-Za-z0-9/._+-]*')
#: What the compute service's driver refuses in an outputs folder as written, since cwltool reads a path as a URI, each
#: with why: a control character, a blank first, `?` or `#`, and a %-escape.
_UNREAD: Final = ((re.compile(r'[\x00-\x1f\x7f-\x9f]'), 'holds a control character'),
                  (re.compile(r'^\s'), 'starts with a blank'),
                  (re.compile(r'[?#]'), 'holds "?" or "#", where CWL ends a path'),
                  (re.compile(r'%[0-9A-Fa-f]{2}'), 'holds a %-escape, which CWL decodes'))
#: A question a check asks the site: a command that prints one line, and the judge of that line.
_Ask = tuple[str, Callable[[str], Diagnostic | None]]
#: Where a site placed a run, by setting: its value, and where it came from.
_Placed = dict[str, tuple[str, str]]
#: What a site places, in the order its record and `submit` say it.
_PLACED: Final = ('partition', 'time', 'memory', 'cpus', 'gpus', 'steps_partition', 'steps_time', 'gpu_partition',
                  'account')


def _error(code: Code, message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, code, message)


def site_config(config: RunConfig | None) -> tuple[str | None, str | None, LogLevel | None, SlurmSettings]:
    """`config` as a site reads it: the outputs folder, absolute, None for the run's own; `relative_to`; the log
    level; and the Slurm settings, which only a `Slurm` site reads. The pair rules are `read_config`'s, read as where
    there is no working directory; a site also refuses what that reading leaves to the compute service, as it has no
    service behind it: an outputs folder or `relative_to` that is not a string or is written for a shell, and an
    outputs folder that is empty, a URL (`file:` too: a site takes paths), or that the service's driver refuses as
    cwltool would misread it (`_UNREAD`). The folder is read as the driver reads it: from `relative_to`, `..`, `.`
    and a doubled or trailing `/` resolved by name.

    Raises:
        ValueError: Every problem, a line each, in the user's words.
    """
    read = read_config(config, local=False)
    out, relative_to = read.out_dir, read.relative_to
    problems = [*read.problems, *(f'{name} {value!r} is not a string: give a path on the site'
                                  for name, value in (('the outputs folder', out), ('relative_to', relative_to))
                                  if value is not None and not isinstance(value, str))]
    if isinstance(out, str):
        if not out:
            problems.append("the outputs folder '' is empty: name one, or give none for the run's own folder")
        elif SHELL_PATH.match(out):
            problems.append(f'the outputs folder {out!r} is written for a shell, which no shell expands on a site: '
                            'give the path itself')
        elif URL_SCHEME.match(out):
            problems.append(f'the outputs folder {out!r} is a URL: a site takes a path')
        problems += [f'the outputs folder {out!r} {why}, as the compute service refuses it: rename the folder'
                     for unread, why in _UNREAD if unread.search(out)]
    if isinstance(relative_to, str) and SHELL_PATH.match(relative_to):
        problems.append(f'relative_to({relative_to!r}) is written for a shell, which no shell expands on a site: give '
                        'the path itself')
    if problems:
        raise ValueError('\n'.join(problems))
    folder = None if out is None else '/' + posixpath.normpath(posixpath.join(relative_to or '/', out)).lstrip('/')
    return folder, relative_to, read.log_level, read.slurm


class _Site:
    """What every site does, and its scheduler's part as a machine has it."""

    url: str
    runs: str
    bin: str | None
    container_engine: str
    #: What the leader claims a run with: its pid on a machine.
    _CLAIMANT: ClassVar[str] = '$$'
    #: What the scheduler adds to the launch of each runner (design §5), by the runner's name, given the run's folder
    #: and the settings the site read: a machine's.
    _RUNNERS: ClassVar[Mapping[str, Callable[[str, Mapping[str, str]], tuple[str, ...]]]] = ON_A_MACHINE
    #: The runners that give a step whose tool declares no memory `DEFAULT_MEMORY_GIB` GiB there (`wic066`): Toil.
    _DEFAULT_MEMORY: ClassVar[tuple[str, ...]] = (Toil.name,)

    def __post_init__(self) -> None:
        found = URL.fullmatch(self.url) if isinstance(self.url, str) else None
        if found is None or int(found['port'] or 0) > 65535:
            raise ValueError(f'url={self.url!r} is not the URL of a site: ssh://[user@]host[:port], and nothing else')
        if sys.platform == 'win32':
            raise _unreachable(self.url, 'Sophios runs on Windows itself, whose paths no site reads: run it inside '
                               'WSL')
        for name, value in (('runs', self.runs), ('bin', self.bin)):
            if (name == 'runs' or value is not None) and not (isinstance(value, str) and _FOLDER.fullmatch(value)):
                raise ValueError(f'{name}={value!r} is not an absolute folder on {host(self.url)} of letters, digits '
                                 'and / . _ - + only, which every runner can run in and from, as the compute service '
                                 "takes its own: Toil runs each step's command through a shell, its job store in runs= "
                                 'on it, and bin= is a folder of PATH, which : splits')
        if not isinstance(self.container_engine, str):
            raise ValueError(f'container_engine={self.container_engine!r} is not the name of a command, such as '
                             "'docker'")

    # pylint: disable-next=too-many-arguments  # check's arguments are Workflow.run's
    def check(self, workflow: 'Workflow', config: RunConfig | None = None, *, target: TargetName = 'cwl',
              runner: Runner | None = None, tool_registry: Tools | None = None,
              inference: Inference = Inference()) -> list[Diagnostic]:
        """Every problem `submit` would hit, at once, and every note the compile and the target made; it writes
        nothing here or there.

        The workflow is compiled and lowered here, a relative File or Directory it reads read from `relative_to`;
        one call there then checks each File and Directory it reads, the runner in `bin` and its version, the
        container engine when a step runs in a container, that `runs` can be written, and that the outputs folder
        is missing in a folder that exists, or empty, named by no run there that has not ended, and inside no
        Directory the run reads; on Slurm also its commands, your `SBATCH_` settings, and that every partition a job
        of the run goes to is there (`wic067`). Under Toil, and on Slurm under Nextflow, a note, `wic066`, at each
        step whose tool declares no memory: it gets 4 GiB there.

        Args:
            workflow: The workflow, built with the Python API.
            config: Where the outputs go, the same `RunConfig` a local run and the compute service take: its outputs
                folder, relative to its `relative_to`, which it then requires, or absolute; missing in a folder that
                exists, or empty, and neither `runs`, inside it nor around it. None for the run's own folder,
                `<runs>/<id>/out`. Its `relative_to` is also where a
                relative File or Directory the workflow reads is read from, and its `log_level` Toil's `--logLevel`,
                before the runner's own flags. Its Slurm settings say where a run's jobs go on Slurm (`Slurm`), and
                are not used on a machine.
            target: The language the runner is given it in: `cwl` or `nextflow`.
            runner: The runner as a value, as `Workflow.run` takes it; None for Toil (`cwl`) or Nextflow. Toil's
                `--maxJobs` and Nextflow's `-qs`, how many steps run at once, 64 when not given, and Toil's
                `--clean`, when its scratch is removed, `always` when not given, are read by the site; every other
                flag is passed on, after the site's own. Its other settings stay their defaults.
            tool_registry: As `Workflow.compile` takes it.
            inference: As `Workflow.compile` takes it.

        Raises:
            SophiosError: The workflow does not compile; `wic062` when the site cannot be reached.
            ValueError: No `bin`; a config that is not a RunConfig, whose outputs folder and `relative_to` do not go
                together, or one a site cannot read; on Slurm, a Slurm setting the run uses in a form the compute
                service refuses, in the compute client's words; no target of that name; a runner that does not run
                the target, with a setting of its own, or with a flag the site sets or reads a value it cannot have.
        """
        return self._prepare(workflow, config, target, runner, tool_registry, inference)[0]

    # pylint: disable-next=too-many-arguments  # submit's arguments are check's
    def submit(self, workflow: 'Workflow', config: RunConfig | None = None, *, target: TargetName = 'cwl',
               runner: Runner | None = None, tool_registry: Tools | None = None,
               inference: Inference = Inference()) -> 'Run':
        """Start `workflow` there, as `check` checks it, and return its run at once, having printed its id, its
        runner with its flags, how many steps run at once, on Slurm where its jobs go and for how long, and where its
        outputs go, each with where it comes from. The run outlives ssh and Python.

        Every argument is `check`'s. The outputs folder is made as the run starts, missing or empty then too, and
        used by no run there that has not ended: of two runs that start into one folder at once, at least one finds
        the other and does not start, claimed by no job, its `reason` saying why. On Slurm, when the call that
        submits fails or its answer is lost, a job named after the run is adopted, or the run is submitted again: its
        claim lets only one leader run it.

        Raises:
            SophiosError: Every error `check` returns, nothing written; `wic021` when another run there took the
                outputs folder since the check, naming it; `wic062` when the run could not be started: on Slurm, when
                Slurm refused its job twice, saying it did not start, its outputs folder free; else saying how to
                attach to it should it have started all the same.
            ValueError: As `check` raises it.
        """
        diagnostics, record, files = self._prepare(workflow, config, target, runner, tool_registry, inference)
        if any(diagnostic.severity is Severity.ERROR for diagnostic in diagnostics):
            raise SophiosError(diagnostics)
        report(diagnostics)
        run = Run(self, record)
        print(_announce(host(self.url), record), flush=True)
        try:
            said = _call(self.url, self._start(run._folder, record, files))
        except SophiosError as error:
            said = self._again(run, files['job.sh'], error)
        if said.startswith(b'used\n'):   # another run took the outputs folder since the check
            raise SophiosError([self._used(record['outdir'][0], *line.split(' ', 1))
                                for line in said.decode('utf-8').splitlines()[1:]])
        if said.strip():   # the scheduler's job
            print(f'{type(self).__name__} has it as job {" ".join(said.decode().split())}.')
        print(f'From anywhere, {run!r} follows it.')
        return run

    def attach(self, run_id: str) -> 'Run':
        """The run `run_id` there, from any computer: its folder is its record. It needs no `bin`.

        Raises:
            SophiosError: `wic064` when it names no run there, or one another kind of site submitted, which the message
                names; `wic062` when the site cannot be reached.
            ValueError: `run_id` is not a run id.
        """
        # A run id is its workflow's name and more of a name's characters, so the names a workflow may have are
        # the ids a run may have: the line `submit` prints is always one `attach` takes.
        if not (isinstance(run_id, str) and WRITTEN_NAME.fullmatch(run_id)):
            raise ValueError(f'{run_id!r} is not a run id: <workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>')
        record = _Said(_call(self.url, f'{_PUT}\nput {_q(_run_folder(self.runs, run_id) + "/run.json")} 0')).file()
        if record is None:
            raise SophiosError.error(Code.SITE_RUN_UNKNOWN, f'{host(self.url)} has no run {run_id} under '
                                     f'{self.runs}: check the id, the URL and runs=')
        found, kind = json.loads(record), type(self).__name__
        if found['site'] != kind:
            raise SophiosError.error(Code.SITE_RUN_UNKNOWN, f'{host(self.url)} has no run {run_id} of a {kind} under '
                                     f"{self.runs}: a {found['site']} site submitted it, so attach to it with "
                                     f"{found['site']}({self.url!r}, runs={self.runs!r})")
        return Run(self, found)

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # check's, and all it reads
    def _prepare(self, workflow: 'Workflow', config: RunConfig | None, target: str, runner: Runner | None,
                 tool_registry: Tools | None, inference: Inference) -> tuple[list[Diagnostic], Json, dict[str, str]]:
        """`check`'s diagnostics, and what `submit` writes: the run's record and its files, by their path in it, when
        there is no error."""
        if self.bin is None:
            raise ValueError(f'bin= names the folder on {host(self.url)} that holds the runners, which check() and '
                             f'submit() run: give it, as {type(self).__name__}(url, runs=..., bin=...)')
        outdir, relative_to, log_level, slurm = site_config(config)
        row = targets.target(target)
        given = runner is not None
        runner = targets.resolve(row.name, runner) if runner is not None else build(row.name, DEFAULT_RUNNER[row.name],
                                                                                    ())
        passed, read = self._read(runner)
        self._slurm(slurm, runner)
        run_id = f'{workflow.process_name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}'
        run = _run_folder(self.runs, run_id)
        result = compile_workflow_result(workflow, tool_registry=tool_registry, inference=inference)
        view = _view(workflow, result)
        found = [*result.diagnostics, *(Diagnostic(
            Severity.ERROR, Code.REALTIME_NOT_ASYNC, f'this step declares the real-time analysis '
            f'{declaration.analysis!r}, and a site never runs real-time analyses', declaration.span)
            for declaration in result.realtime)]
        folders = output_target_inputs(view)
        made, problems = self._made(view, folders, run, row.name)
        inputs, asks = self._inputs({name: value for name, value in view.values.items() if name not in folders},
                                    relative_to, outdir)
        found += [*problems, *inputs, *self._apart(outdir)]
        if problems:
            return found, {}, {}
        try:
            lowered = row.lower(targets.for_run(row, bind(view, values=made), Path(relative_to or '/')))
        except NotLowered as not_lowered:
            return [*found, *not_lowered.diagnostics], {}, {}
        if row.name == 'cwl':
            lowered = replace(lowered, model=post_compile.inline_artifact_runs(lowered.model))
        refused = self._engine_refused(runner, lowered)
        settings = RunSettings(self._engined(runner), outdir or f'{run}/out')
        # A CWL run's files as cwltool's backend writes them for either CWL runner: an output folder given a name is
        # an empty Directory as CWL writes one, which Toil reads under `--bypass-file-store`, as a site runs it, where
        # it cannot read its own form (`ToilBackend.empty_directory`), which needs its file store.
        backend = targets.backend(build(row.name, Cwltool.name if row.name == 'cwl' else runner.name, ()))
        with tempfile.TemporaryDirectory(prefix='sophios-site-') as staging:
            written = backend.write(lowered, (), Path(staging), run=settings, root_dir=Path(relative_to or '/'))
            needs = backend.needs(written.documents, ())
            files = {path.relative_to(staging).as_posix(): path.read_bytes().decode('utf-8')
                     for path in sorted(Path(staging).rglob('*')) if path.is_file()}
        notes = (_memory_notes(lowered.view, host(self.url), runner.name) if runner.name in self._DEFAULT_MEMORY
                 else [])
        problems, placed = self._ask(settings.runner, needs, asks, outdir, refused, partial(
            self._place, slurm=slurm, runner=runner.name, view=lowered.view))
        found += [*lowered.notes, *notes, *refused, *problems]
        if any(diagnostic.severity is Severity.ERROR for diagnostic in found):
            return found, {}, {}
        read |= placed
        values = {setting: value for setting, (value, _) in read.items()}
        settings = replace(settings, runner=self._there(runner, run, values, (
            *(('--logLevel', log_level) if log_level is not None and runner.name == Toil.name else ()), *passed)))
        if runner.name == Nextflow.name:
            files[(nextflow_directory(Path(), written.name) / NEXTFLOW_CONFIG).as_posix()] += nextflow_config(
                self._block(run, values, runner))
        files['job.sh'] = self._job(RunRequest(written.name, Path(run), settings), values, written.folders)
        record: Json = {
            'id': run_id, 'site': type(self).__name__, 'workflow': written.name, 'target': row.name,
            'sophios': __version__,
            'runner': [runner.name, 'runner=' if given else f"a site's default for {row.name}"],
            'flags': list(runner.flags),
            **{setting: [int(value) if setting == 'max_parallel' else value, source]
               for setting, (value, source) in read.items()},
            'outdir': [settings.outdir, 'RunConfig(...)' if outdir else "the run's own folder"]}
        files['run.json'] = json.dumps(record, indent=2) + '\n'
        return found, record, files

    def _read(self, runner: Runner) -> tuple[tuple[str, ...], dict[str, tuple[str, str]]]:
        """`read_runner` of the value `runner` here: what this site sets itself is the command line it runs with none
        of the user's flags, given every setting it may give the runner (`SAMPLE`), and the block it writes into
        Nextflow's configuration."""
        there = self._there(runner, '/run', SAMPLE)
        return read_runner(runner, set_by_the_site(there, self._block('/run', SAMPLE, runner)))

    def _engined(self, runner: Runner) -> Runner:
        """The runner a site runs for the value `runner`, with none of its flags: the site's engine; never quiet, as
        its log is where a failed step is read from. Nextflow's pipeline runs its containers with Docker or
        Singularity; under another engine no process runs a container, or check refuses it, so the pipeline is the one
        for Docker, which it never starts."""
        engine = 'docker' if runner.name == Nextflow.name and self.container_engine not in NF_ENGINE_CONFIG else \
            self.container_engine
        return build(runner.target, runner.name, (), container_engine=engine, quiet=False)

    def _there(self, runner: Runner, run: str, read: Mapping[str, str], flags: Sequence[str] = ()) -> Runner:
        """`_engined(runner)`, in the run's folder `run`, with what the scheduler adds to its launch, given the
        settings the site read and placed, then `flags`."""
        return self._engined(runner).evolve(flags=(*self._RUNNERS[runner.name](run, read), *flags))

    def _apart(self, outdir: str | None) -> list[Diagnostic]:
        """`wic021` when the outputs folder `outdir` is `runs`, inside it or around it: a run that fails empties
        it, and `runs` holds every run's folder and the image cache, as the compute service refuses a folder among its
        runs."""
        runs = posixpath.normpath(self.runs)
        if outdir is None or not (_around(outdir, runs) or _around(runs, outdir)):
            return []
        return [_error(Code.DIRECTORY_NOT_WRITABLE, f'Sophios writes the outputs to {outdir} on {host(self.url)}, '
                       f'which is runs={self.runs!r}, inside it or around it, where the site keeps every run and its '
                       'images: name a folder outside it')]

    def _made(self, view: ResolvedWorkflow, folders: set[str], run: str,
              target: str) -> tuple[dict[str, Any], list[Diagnostic]]:
        """Each of the workflow's output folders, `folders` (a Directory a tool writes into and returns), given one
        name, made empty in the run's `made` folder: for Nextflow, rebound there, where its job makes it; a CWL runner
        makes it itself. And `wic016` at each one given a path, or given in a list (a scattered step's), each element
        judged as one folder is: the folder lands in the outputs folder under one name, and neither a site nor a local
        run makes the folders of a list."""
        made: dict[str, Any] = {}
        problems = []
        for name in sorted(folders):
            value = view.values.get(name)
            listed, items = (True, value) if isinstance(value, list) else (False, [value])
            for item in items:
                written = item.get('location', item.get('path', item.get('basename'))) if isinstance(item, dict) \
                    and item.get('class') == 'Directory' else item
                if not isinstance(written, str):
                    continue
                if '://' in written or written.startswith('/'):
                    problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' is an output folder given the "
                                           f'path {written}, which a site does not write in place: give it a name, '
                                           "such as 'masks', and it lands in the outputs folder under that name"))
                elif written in ('', '.', '..') or '/' in written:
                    problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' is an output folder given "
                                           f'{written!r}, which is not one folder name: a site makes it in the run, '
                                           'and it lands in the outputs folder under that name; give it one name, '
                                           "such as 'masks'"))
                elif listed:
                    problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' gives the output folder "
                                           f'{written!r} in a list, as a scattered step takes them, and neither a '
                                           'site nor a local run makes the output folders of a list: give the step '
                                           'one folder'))
                elif target == Nextflow.target:
                    made[name] = {'class': 'Directory', 'path': f'{run}/made/{written}'}
        return made, problems

    def _inputs(self, values: Mapping[str, Any], relative_to: str | None,
                outdir: str | None) -> tuple[list[Diagnostic], list[_Ask]]:
        """`wic016` at each File and Directory path a site cannot read as written, and the question for each other:
        a relative one is read from `relative_to`, by name, as the runner reads it. And `wic021` when the output
        folder `outdir` is inside a Directory the run reads, as the compute service refuses it."""
        problems: list[Diagnostic] = []
        asks: list[_Ask] = []
        for name, value in input_paths(dict(values)):
            if (written := preflight.written_path(value)) is None:
                continue
            if SHELL_PATH.match(written):
                problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' names {written}, which is written for "
                                       'a shell, and no shell expands it on a site: give the path itself'))
            elif not written.startswith('/') and relative_to is None:
                problems.append(_error(Code.MISSING_INPUT_FILE, f"input '{name}' names the relative path {written}, "
                                       'and no relative_to is given to read it from, as there is no working '
                                       "directory on a site: give the folder it is relative to, as RunConfig('results')"
                                       ".relative_to('/path/to/project'), or an absolute path"))
            else:
                path = posixpath.normpath(posixpath.join(relative_to or '/', written))
                asks.append((f'kind {_q(path)}', partial(self._input, name, value['class'], path)))
                if outdir is not None and value['class'] == 'Directory' and _around(outdir, path):
                    problems.append(_error(Code.DIRECTORY_NOT_WRITABLE, f'Sophios writes the outputs to {outdir} on '
                                           f"{host(self.url)}, which is inside {path}, the folder input '{name}' "
                                           'reads: name a folder beside it'))
        return problems, asks

    def _engine_refused(self, runner: Runner, lowered: Lowered[Any]) -> list[Diagnostic]:
        """`wic065` at each Nextflow process with a container, when the site's engine is neither of the two Nextflow's
        pipeline runs its containers with."""
        if runner.name != Nextflow.name or self.container_engine in NF_ENGINE_CONFIG:
            return []
        steps = nextflow_names(lowered.view)['processes']
        return [_error(Code.SITE_NEXTFLOW_CONTAINER, f"step '{steps[process.name]}' runs in a container, and "
                       "Nextflow's pipeline runs its containers with Docker or Singularity only, which "
                       f"{host(self.url)}'s container_engine={self.container_engine!r} is neither of: use "
                       "container_engine='docker' or 'singularity', or target='cwl'")
                for process in lowered.model.processes if process.container is not None]

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # one call, every question
    def _ask(self, runner: Runner, needs: preflight.Needs, asks: list[_Ask], outdir: str | None,
             refused: list[Diagnostic], place: Callable[[Sequence[str]], tuple[list[Diagnostic], _Placed]]
             ) -> tuple[list[Diagnostic], _Placed]:
        """Every problem the site has with the run, in one call: each check a command printing one line, and a judge
        of that line; the scheduler's questions, which `place` answers with where the run's jobs go; and, for an
        outputs folder the config names, the record of each run there that has not ended, which may name it too."""
        asks = [('uname -s', self._platform), *asks]
        program, engine = runner.name, runner.container_engine
        flag = '-version' if program == Nextflow.name else '--version'
        asks.append((f'runs {_q(f"{self.bin}/{program}")} {flag}', partial(self._runner, program, flag)))
        if needs.containers and not refused:
            probe = 'info' if engine in preflight.DOCKER_LIKE else '--version'
            asks.append((f'probe {_q(engine)} {probe}', partial(self._engine, engine, probe, needs.images)))
        asks.append((f'writable {_q(self.runs)}', partial(self._free, self.runs, 'its runs', 'name another runs=')))
        if outdir is not None:
            asks.append((f'free {_q(outdir)}', partial(self._free, outdir, 'the outputs', 'name another folder')))
        questions = self._questions()
        used = [self._unfinished(), _USED, f'used {_q(self.runs)} {_q(outdir)} ""; echo'] if outdir is not None else []
        script = '\n'.join([*self._environment(), _CHECKS, *(command for command, _ in asks), *questions, *used])
        said = _Said(_call(self.url, script))
        try:
            lines = [said.line() for _ in range(len(asks) + len(questions))]
            users = list(iter(said.line, '')) if outdir is not None else []
        except ValueError as error:
            raise _unreachable(self.url, f'its checks printed what Sophios cannot read ({error})') from None
        placing, placed = place(lines[len(asks):])
        return [found for (_, judge), line in zip(asks, lines) if (found := judge(line)) is not None] + placing + [
            self._used(outdir, *user.split(' ', 1)) for user in users if outdir is not None], placed

    def _platform(self, said: str) -> Diagnostic | None:
        if said in ('Darwin', 'Linux'):
            return None
        return _error(Code.SITE_UNREACHABLE,
                      f'{host(self.url)} runs {said or "no uname"}, and a site is macOS or Linux')

    def _input(self, name: str, kind: object, path: str, said: str) -> Diagnostic | None:
        if said == kind:
            return None
        there = host(self.url)
        why = {'missing': f'does not exist there: copy your data to {there}, or correct the path',
               'unreadable': 'you may not read there'}.get(said, f'is not a {kind} there')
        return _error(Code.MISSING_INPUT_FILE, f"input '{name}' names {path} on {there}, which {why}")

    def _runner(self, program: str, flag: str, said: str) -> Diagnostic | None:
        path, oldest = f'{self.bin}/{program}', '.'.join(map(str, OLDEST[program]))
        status, _, output = said.partition(' ')
        versions = re.findall(r'(\d+)\.(\d+)\.(\d+)', output)
        if said == 'missing':
            why = f'there is no {path} you can run: install {program} in bin=, or name the folder that holds it'
        elif status != '0':
            why = (f'{path} {flag} exited with status {status} ({output.strip()[:300] or "it said nothing"})'
                   + (': Nextflow needs a Java it can run, in bin=' if program == Nextflow.name else ''))
        elif versions and tuple(map(int, versions[-1])) >= OLDEST[program]:
            return None
        else:
            why = (f'{path} is {".".join(versions[-1]) if versions else "of no version Sophios can read"}, and a '
                   f'site runs {program} {oldest} or later: install it in bin=')
        return _error(Code.SITE_RUNNER, f'on {host(self.url)}, {why}')

    def _engine(self, engine: str, probe: str, images: list[str], said: str) -> Diagnostic | None:
        status, _, output = said.partition(' ')
        if status == '0':
            return None
        names = list(dict.fromkeys(images))
        uses, there = names[0] if len(names) == 1 else f'{names[0]} and {len(names) - 1} more', host(self.url)
        if said == 'missing':
            return _error(Code.CONTAINER_ENGINE_UNAVAILABLE, f'{engine} is not on PATH on {there}, and this '
                          f'workflow runs tools in containers ({uses}): install it there, or name another with '
                          'container_engine=')
        return _error(Code.CONTAINER_ENGINE_UNAVAILABLE, f'`{engine} {probe}` exited with status {status} on '
                      f'{there} ({output.strip()[:300] or "it said nothing"}): start the engine there, or give '
                      'your user access to it')

    def _free(self, path: str, holds: str, remedy: str, said: str) -> Diagnostic | None:
        if said == 'yes':
            return None
        why = {'exists': 'exists and is not a folder',
               'link': 'is a link, which a site does not follow, as it reads every path by name',
               'parent': 'cannot be made: the folder that would hold it does not exist',
               'full': 'is not empty, and a run writing there would replace same-named files and merge into '
               'same-named folders'}.get(said, f'cannot be made: {said[3:]} is not a folder you can write')
        return _error(Code.DIRECTORY_NOT_WRITABLE, f'Sophios writes {holds} to {path} on {host(self.url)}, which '
                      f'{why}: {remedy}')

    def _used(self, outdir: str, run_id: str, other: str) -> Diagnostic:
        """`wic021`: the outputs folder `outdir` is `other`, inside it or around it, the outputs folder of the run
        `run_id`, which has not ended."""
        how = '' if other == outdir else f', inside {other},' if _around(outdir, other) else f', around {other},'
        return _error(Code.DIRECTORY_NOT_WRITABLE, f'Sophios writes the outputs to {outdir} on {host(self.url)}, '
                      f'which is used{how} by run {run_id}, which has not ended (one whose job was killed '
                      'outright ends at its next status()): name another folder')

    def _environment(self) -> list[str]:
        """What every job and check runs first, so a check sees what the job will: no Toil or Python setting of your
        profile's, and `bin` first on PATH. What a computer provides through a module comes from your login
        profile."""
        assert self.bin is not None, 'check and submit refuse a site without bin='
        return ['unset "${!TOIL_@}" "${!PYTHON@}"', 'export PYTHONNOUSERSITE=1 TOIL_HISTORY=False',
                f'export PATH={_q(self.bin)}:"$PATH"']

    def _job(self, request: RunRequest, read: Mapping[str, str], folders: Iterable[str]) -> str:
        """`job.sh`, the leader script (design Appendix A), for `request`, given the settings the site read and
        placed, `read`: on Slurm its header; the claim, by the leader's pid or job id, the record of how it ended in
        `exit` whatever happens after the claim, `folders` made, what the scheduler runs before the runner (`_before`),
        and the runner's launch run from `bin`. Toil's `--clean`, `read`'s `clean`, says when its scratch is removed; a
        runner that leaves none has none."""
        runner, run, outdir, clean = request.run.runner, str(request.basepath), request.run.outdir, read.get('clean')
        launch = targets.backend(runner).launch(request)
        if runner.name == Toil.name:
            folders = (*folders, f'{run}/toil/work', f'{run}/toil/tmp')
        leader, images = _q(f'{run}/leader'), f'{self.runs}/.images'
        caches = [*(f'export {name}=${{{name}:-{_q(f"{images}/cache")}}}'
                    for name in ('SINGULARITY_CACHEDIR', 'APPTAINER_CACHEDIR')),
                  f'export NXF_SINGULARITY_CACHEDIR={_q(f"{images}/nextflow")}' if runner.name == Nextflow.name else
                  f'export CWL_SINGULARITY_CACHE={_q(images)}'] if runner.container_engine == 'singularity' else []
        assert self.bin is not None, 'check and submit refuse a site without bin='
        claimant = f'"{self._CLAIMANT}"'
        return '\n'.join([
            '#!/bin/bash -l', *self._header(run, read), 'set -euo pipefail',
            f'ln -s {claimant} {leader} 2> /dev/null || [ -L {leader} ] || ln -s {claimant} {leader}',
            f'[ "$(readlink {leader})" = {claimant} ] || exit 0',
            f'trap {_q(_recorded(run, clean, outdir))} EXIT', "trap 'exit 143' TERM", *self._environment(), *caches,
            *(f'export {name}={_q(value)}' for name, value in launch.env.items()),
            *(f'mkdir -p {_q(folder)}' for folder in folders), *self._before(runner),
            f'cd {_q(str(launch.cwd or run))}',
            f'{{ {shlex.join(argv(self.bin, runner, launch))} | tee {_q(f"{run}/stdout")}; }} >> '
            f'{_q(f"{run}/run.log")} 2>&1', ''])

    def _start(self, run: str, record: Json, files: Mapping[str, str]) -> str:
        """The one call that starts a run: hang-ups ignored, so a dropped connection cannot stop it between the leader
        and the record; the outputs folder, missing or empty still, and no link; the run's folder, with `out`, a link
        to the outputs folder the config names, and the files; then, should another run that has not ended use that
        folder (`_USED`), the run claimed `none`, its `reason` written, and `used` and each such run printed, a line
        each, the leader never started: two runs that start at once each write their record first, so at least one
        finds the other; else the folder made, and the leader."""
        outdir, named = _q(record['outdir'][0]), record['outdir'][1] != "the run's own folder"
        why = f'{record["outdir"][0]} was taken by another run as this one started, so it did not start'
        return '\n'.join([
            "trap '' HUP PIPE", 'set -e',
            *([f'if [ -L {outdir} ] || [ -e {outdir} ]; then [ -d {outdir} ] && [ ! -L {outdir} ] && '
               f'[ -z "$(ls -A {outdir})" ] || {{ echo {outdir} is no longer missing or empty >&2; exit 1; }}; fi']
              if named else []),
            f'mkdir -p {_q(self.runs)}', f'mkdir {_q(run)}', *([f'ln -s {outdir} {_q(f"{run}/out")}'] if named else []),
            *(f'mkdir -p {_q(f"{run}/{parent}")}' for parent in sorted({str(PurePosixPath(path).parent)
                                                                        for path in files} - {'.'})),
            *(f'printf %s {_q(text)} > {_q(f"{run}/{path}")}' for path, text in files.items()),
            *([self._unfinished(), _USED, f'u=$(used {_q(self.runs)} {outdir} {_q(run)})',
               f'if [ -n "$u" ]; then ln -s none {_q(f"{run}/leader")}; printf %s {_q(why)} > {_q(f"{run}/reason")}; '
               'printf "used\\n%s\\n" "$u"; exit 0; fi'] if named else []),
            f'[ -d {outdir} ] || mkdir {outdir}', *self._leader(run, files['job.sh'])])

    # The scheduler's part: a machine's.

    def _unfinished(self) -> str:
        """A script's function: whether the run in folder `$1` has not ended, so its outputs folder is its own: it has
        no `exit` nor `reason`, and is claimed by its leader, or by none and submitted within `UNCLAIMED_MINUTES`. A
        leader killed outright has not ended until a status writes its `reason`, having emptied that folder, so no
        status ever empties a folder another run has taken since."""
        return ('unfinished() { if [ -e "$1/exit" ] || [ -e "$1/reason" ]; then return 1; fi; '
                'l=$(readlink "$1/leader" 2> /dev/null) || '
                f'{{ [ -n "$(find "$1/run.json" -mmin -{UNCLAIMED_MINUTES} 2> /dev/null)" ]; return; }}; '
                'case $l in cancelled|none) return 1 ;; esac; }')

    def _cancelling(self, run: str) -> list[str]:
        """The lines that cancel the run in the folder they run in: claimed `cancelled`, so a job that has not
        started never will, then its leader, whose pid the claim names, stopped with TERM to its process group."""
        return [_ALIVE, 'ln -s cancelled leader 2> /dev/null || true', 'leader=$(readlink leader)',
                f'if alive "$leader" {_q(PurePosixPath(run).name)}; then kill -TERM -- "-$leader"; fi']

    def _questions(self) -> list[str]:
        """The scheduler's questions a check asks, each printing one line: none on a machine."""
        return []

    def _slurm(self, slurm: SlurmSettings, runner: Runner) -> None:
        """Check the config's Slurm settings, `slurm`, that the run uses under `runner`, before anything is compiled
        or sent: none on a machine, which reads none."""
        del slurm, runner

    def _place(self, said: Sequence[str], *, slurm: SlurmSettings, runner: str,
               view: ResolvedWorkflow) -> tuple[list[Diagnostic], _Placed]:
        """What the answers to `_questions` say, for the run of `view` by `runner` with the config's Slurm settings,
        `slurm`: the scheduler's problems with the run, and where its jobs go, each setting with where it came from;
        none on a machine."""
        del said, slurm, runner, view
        return [], {}

    def _header(self, run: str, read: Mapping[str, str]) -> list[str]:
        """The lines of the leader's script before its first command, for the run in `run`, placed as `read` says:
        none on a machine."""
        del run, read
        return []

    def _block(self, run: str, read: Mapping[str, str], runner: Runner) -> dict[str, dict[str, str]]:
        """What the scheduler sets in the configuration of `runner`, for the run in `run`, given the settings the site
        read and placed: for Nextflow, a machine's block (`nextflow_block`); none for a CWL runner."""
        del run
        return nextflow_block(read) if runner.name == Nextflow.name else {}

    def _before(self, runner: Runner) -> list[str]:
        """The lines the job runs just before `runner`, after your login profile: none on a machine."""
        del runner
        return []

    def _leader(self, run: str, job: str) -> list[str]:
        """The lines that start the leader, `job` being its script: on a machine, in a process group of its own
        (`set -m`), which outlives the call and which `cancel` stops. `run.log` is opened to be added to, as `job.sh`
        opens it for the runner: what the leader says after the runner follows it."""
        del job
        return [f'cd {_q(run)}', 'set -m', f'nohup bash -l {_q(f"{run}/job.sh")} >> {_q(f"{run}/run.log")} 2>&1 '
                '< /dev/null &']

    def _again(self, run: Run, job: str, error: SophiosError) -> bytes:
        """When the call that starts `run` failed or its answer was lost, the run's one leader, by another call; on a
        machine, which starts it once, `error`, and how to follow the run should it have started all the same."""
        del job
        raise SophiosError([*error.diagnostics, _started(run)]) from None

    def _asked(self, run_id: str) -> list[str]:
        """The lines a status asks the scheduler with, before it reads the folder: none on a machine."""
        del run_id
        return []

    # pylint: disable-next=useless-return  # a machine is asked nothing
    def _queue(self, run_id: str, said: _Said, now: int) -> Callable[[], Sequence[Job]] | None:
        """What `_asked` said, read from `said`: the run's jobs, as `decide` asks for them; None on a machine."""
        del run_id, said, now
        return None


@dataclass(frozen=True)
class Machine(_Site):
    """A computer of your own you can `ssh` to, macOS or Linux, as a site: a workstation or a cloud instance.

    Args:
        url: `ssh://[user@]host[:port]`: ssh is given `[user@]host`, and `-p <port>` with a port, so your keys,
            agent and `~/.ssh/config` apply.
        runs: The absolute folder there that holds a folder per run, `<runs>/<id>/`, and the image cache,
            `<runs>/.images`; letters, digits and `/ . _ - +` only, as every runner runs in it.
        bin: The absolute folder there that holds the runners, and what they need (the Java Nextflow runs on); first
            on PATH; letters, digits and `/ . _ - +` only. `check` and `submit` need it; `attach` does not.
        container_engine: The engine every runner runs containers with: `docker`, `podman`, `singularity`, or another
            docker-compatible command; Nextflow's pipeline runs them with `docker` or `singularity`.

    Raises:
        SophiosError: `wic062` on Windows itself, as `sophios.compute.Service` is refused there: a site's paths are
            POSIX paths, so no call of a site is made from it.
        ValueError: A URL of another form, or a `runs` or `bin` that is not an absolute folder of letters, digits and
            `/ . _ - +` only.
    """

    url: str
    _: KW_ONLY
    runs: str
    bin: str | None = None
    container_engine: str = 'docker'


def _started(run: Run) -> Diagnostic:
    """`wic062`: the run may have started though its start failed, and how to follow it."""
    return _error(Code.SITE_UNREACHABLE, f'run {run.id} may have started all the same: {run!r} follows it')


def _around(path: str, folder: str) -> bool:
    """Whether `path` is `folder`, or inside it, by name."""
    return PurePosixPath(folder) in (PurePosixPath(path), *PurePosixPath(path).parents)


def _announce(there: str, record: Json) -> str:
    """What `submit` prints first: the id, the runner and its flags, how many steps run at once, on Slurm where the
    run's jobs go and for how long, and the outputs, each with where it comes from."""
    (runner, runner_from), flags = record['runner'], record['flags']
    parallel = (f", at most {record['max_parallel'][0]} at once ({record['max_parallel'][1]})"
                if 'max_parallel' in record else '')
    placed = ', '.join(f'{setting} {record[setting][0]} ({record[setting][1]})' for setting in _PLACED
                       if setting in record)
    return (f'Submitting {record["id"]} to {there}: {shlex.join([runner, *flags])} ({runner_from}){parallel}.\n'
            + (f'Slurm: {placed}.\n' if placed else '') + f'Outputs: {record["outdir"][0]} ({record["outdir"][1]})')


def _memory_notes(view: ResolvedWorkflow, there: str, runner: str) -> list[Diagnostic]:
    """`wic066` at each step whose tool declares no memory: Toil gives it `--defaultMemory` (decision 11), and refuses
    to start it on a computer with less; on Slurm Nextflow's task asks for the same (decision 44)."""
    given = (f'Toil gives it {DEFAULT_MEMORY_GIB} GiB on {there} (--defaultMemory {DEFAULT_MEMORY_GIB}Gi), and '
             'refuses to start it on a computer with less' if runner == Toil.name else
             f'its Slurm job on {there} asks for {DEFAULT_MEMORY_GIB} GiB (process.memory)')
    return [Diagnostic(Severity.NOTE, Code.SITE_DEFAULT_MEMORY,
                       f'its tool declares no memory, so {given}: declare what it needs with ramMin',
                       tool.step.span, Locator(step=tool.step.name))
            for tool in tool_steps(view) if isinstance(tool.step.run, Tool)
            and not _declares_memory(tool.step.run.cwl, tool.step.inherited)]


def _declares_memory(tool: Any, inherited: Iterable[Requirement]) -> bool:
    """Whether Toil gives a step its tool's memory, not `--defaultMemory`, and Nextflow on Slurm a task, not the block's
    `process.memory`: the ResourceRequirement that wins names `ramMin` or `ramMax`. Else `check` notes it, `wic066`."""
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
