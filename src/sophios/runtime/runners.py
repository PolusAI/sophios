"""The programs that run a compiled workflow, the one value that describes a local run, and the configuration a run
is given wherever it runs.

`Cwltool`, `Toil` and `Nextflow` are what `Workflow.run(runner=...)` takes and the command line's flags build; a
`RunSettings` is a runner and what is given to the job. A `RunConfig`, with its `SlurmSettings`, is where a run's
outputs go and how its jobs are run, the same object for a local run, a site and the compute service, which each read
it with `read_config`. This module imports nothing from Sophios, so the command line's parser reads its defaults here
without importing a backend.
"""
import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, NamedTuple, Self, TypeAlias, get_args


@dataclass(frozen=True, init=False)
class _Runner:
    """What every runner holds: the arguments it is given, before the documents, unchecked."""

    flags: tuple[str, ...]

    #: As `--cwl_runner` and the diagnostics name it.
    name: ClassVar[str]
    #: The language it is given a workflow in: a name of `sophios.targets.TARGETS`.
    target: ClassVar[str]
    #: The program a run calls.
    program: ClassVar[str]
    #: What to do when the program is missing.
    install: ClassVar[str]

    def evolve(self, **changes: Any) -> Self:
        """This runner with `changes` made to its settings."""
        changed = copy.copy(self)
        for setting, value in changes.items():
            object.__setattr__(changed, setting, value)
        return changed


@dataclass(frozen=True, init=False)
class _CwlRunner(_Runner):
    """What a CWL runner is told about the machine and the images: see `Cwltool` for each setting."""

    container_engine: str
    pull_dir: str
    ignore_install: bool
    ignore_processes: bool
    provenance: bool
    remove_entrypoints: bool
    target: ClassVar[str] = 'cwl'

    # pylint: disable-next=too-many-arguments
    def __init__(self, *flags: str, container_engine: str = 'docker', pull_dir: str = '', ignore_install: bool = False,
                 ignore_processes: bool = False, provenance: bool = True, remove_entrypoints: bool = False) -> None:
        for name, value in (('flags', flags), ('container_engine', container_engine), ('pull_dir', pull_dir),
                            ('ignore_install', ignore_install), ('ignore_processes', ignore_processes),
                            ('provenance', provenance), ('remove_entrypoints', remove_entrypoints)):
            object.__setattr__(self, name, value)


@dataclass(frozen=True, init=False)
class Cwltool(_CwlRunner):
    """cwltool, the reference runner.

    Args:
        flags: Arguments for cwltool, before the documents.
        container_engine: `docker`, `podman`, `singularity`, or another docker-compatible command.
        pull_dir: Where `singularity` images are pulled; '' for the working directory.
        ignore_install: Do not check that the container engine is installed.
        ignore_processes: Do not check that the engine runs few enough processes.
        provenance: Have cwltool write its record of the run under the base path.
        remove_entrypoints: Run the workflow's images without their entrypoints.
        cachedir: Where cwltool caches a job whose tool and inputs are unchanged; '' for no cache.
        quiet: Pass cwltool `--quiet`, which hides its own log; each tool's own messages still print.
    """

    cachedir: str
    quiet: bool
    name: ClassVar[str] = 'cwltool'
    program: ClassVar[str] = 'cwltool_filterlog'
    install: ClassVar[str] = 'install Sophios in this environment (pip install sophios), which provides it'

    # pylint: disable-next=too-many-arguments
    def __init__(self, *flags: str, container_engine: str = 'docker', pull_dir: str = '', ignore_install: bool = False,
                 ignore_processes: bool = False, provenance: bool = True, remove_entrypoints: bool = False,
                 cachedir: str = '', quiet: bool = True) -> None:
        super().__init__(*flags, container_engine=container_engine, pull_dir=pull_dir, ignore_install=ignore_install,
                         ignore_processes=ignore_processes, provenance=provenance,
                         remove_entrypoints=remove_entrypoints)
        object.__setattr__(self, 'cachedir', cachedir)
        object.__setattr__(self, 'quiet', quiet)


@dataclass(frozen=True, init=False)
class Toil(_CwlRunner):
    """Toil's CWL runner, `toil-cwl-runner`: the settings of `Cwltool` but `cachedir` and `quiet`, which Toil does
    not read."""

    name: ClassVar[str] = 'toil-cwl-runner'
    program: ClassVar[str] = 'toil-cwl-runner'
    install: ClassVar[str] = 'install Toil (pip install "toil[cwl]")'
    cachedir: ClassVar[str] = ''
    quiet: ClassVar[bool] = False


@dataclass(frozen=True, init=False)
class Nextflow(_Runner):
    """Nextflow, which runs the pipeline the target `nextflow` lowers a workflow to.

    Args:
        flags: Its options, as it spells them: `Nextflow('-process.executor', 'slurm', '-resume')`.
        container_engine: What runs the pipeline's containers, `docker` or `singularity`: the pipeline is written
            for it, and another is refused where it is written.
    """

    container_engine: str
    name: ClassVar[str] = 'nextflow'
    target: ClassVar[str] = 'nextflow'
    program: ClassVar[str] = 'nextflow'
    install: ClassVar[str] = 'install Nextflow (https://www.nextflow.io/docs/latest/install.html)'
    # The settings of a CWL runner it has none of.
    pull_dir: ClassVar[str] = ''
    ignore_install: ClassVar[bool] = False
    ignore_processes: ClassVar[bool] = False
    provenance: ClassVar[bool] = False
    remove_entrypoints: ClassVar[bool] = False
    cachedir: ClassVar[str] = ''
    quiet: ClassVar[bool] = False

    def __init__(self, *flags: str, container_engine: str = 'docker') -> None:
        object.__setattr__(self, 'flags', flags)
        object.__setattr__(self, 'container_engine', container_engine)


Runner: TypeAlias = Cwltool | Toil | Nextflow

#: Every runner, the default of its target first.
RUNNERS: Final[tuple[type[Runner], ...]] = get_args(Runner)


def runners_of(target: str) -> tuple[type[Runner], ...]:
    """The runners that run `target`, its default first."""
    return tuple(runner for runner in RUNNERS if runner.target == target)


def named(target: str, name: str) -> type[Runner]:
    """The runner of `target` called `name`.

    Raises:
        ValueError: None of its runners has that name.
    """
    for runner in runners_of(target):
        if runner.name == name:
            return runner
    raise ValueError(f"No backend runs {target} with {name!r}; the runners are "
                     f"{', '.join(runner.name for runner in runners_of(target))}.")


def build(target: str, name: str | None, flags: Iterable[str], **settings: Any) -> Runner:
    """The runner of `target` called `name`, or its default when `name` is None, with `flags` and each of `settings`
    it has: the one place a runner is chosen (`cachedir` and `quiet` are `Cwltool`'s alone; `Nextflow` has only
    `container_engine`).

    Raises:
        ValueError: `target` has no runner called `name`.
    """
    runner = runners_of(target)[0] if name is None else named(target, name)
    own = {field.name for field in fields(runner)}
    return runner(*flags, **{setting: value for setting, value in settings.items() if setting in own})


@dataclass(frozen=True, slots=True)
class RunSettings:
    """One run, here or on a site: its runner, and what is given to the job.

    Attributes:
        runner: The program that runs it, with its flags and settings; the target is `runner.target`.
        outdir: The outputs folder, a CWL runner's `--outdir` or Nextflow's `-output-dir`; '' for
            `<basepath>/outdir_<runner>_<name>_<time>`.
        inputs_file: A job file whose values win over the workflow's, or ''.
        generate_run_script: Write `run.sh` with the runner's command instead of running it.
    """

    runner: Runner
    outdir: str = ''
    inputs_file: str = ''
    generate_run_script: bool = False


@dataclass(frozen=True, kw_only=True)
class SlurmSettings:
    """Where a run's Slurm jobs go and how long they may last, each a string in Slurm's own form, checked where it is
    used. A field about a job the run does not have is not used, never refused: a local run and a `Machine` have no
    Slurm job, and cwltool no step jobs.

    Args:
        partition: The partition of the leader, the job that lasts the whole run.
        time: The leader's time limit, such as `2:00:00` or `1-00:00:00`.
        memory: The leader's memory, such as `8G`.
        steps_partition: The partition of each step's own job, Toil's or Nextflow's.
        steps_time: The time limit of each step's own job.
        gpu_partition: Where a step that asks for a GPU goes.
    """

    partition: str | None = None
    time: str | None = None
    memory: str | None = None
    steps_partition: str | None = None
    steps_time: str | None = None
    gpu_partition: str | None = None


#: Toil's log levels, which `RunConfig.log_level` takes.
LogLevel: TypeAlias = Literal['DEBUG', 'INFO', 'WARNING', 'ERROR']


class RunConfig:
    """Where a run's outputs go and how its jobs are run: the same object for a local run (`Workflow.run(config=)`),
    a site (`sophios.sites`) and the compute service. Made with the outputs folder, and given the rest by setters
    that set and return it, so the calls chain::

        RunConfig('results').relative_to('/shared/lab').slurm_settings(SlurmSettings(time='2:00:00'))

    A second call of a setter replaces the first. What no setter sets is the place's own: the working directory, no
    Slurm settings, the default log level.

    Args:
        workflow_out_dir: The outputs folder, which every output lands in. Relative, it is read from `relative_to`,
            which a site and the compute service then require, or, in a local run without one, from the working
            directory; absolute, it is used as it is, and takes no `relative_to`. None for the place's own: a local
            run's `<basepath>/outdir_<runner>_<name>_<time>`, the run's own folder elsewhere.
    """

    def __init__(self, workflow_out_dir: str | None = None) -> None:
        self._out_dir = workflow_out_dir
        self._relative_to: str | None = None
        self._slurm = SlurmSettings()
        self._log_level: LogLevel | None = None

    def relative_to(self, path: str) -> Self:
        """The absolute folder the run's relative paths are read from: the outputs folder's, which it requires, and
        those of the File and Directory inputs bound in Python, which on a site it must be given for; the compute
        service's `basepath`."""
        self._relative_to = path
        return self

    def slurm_settings(self, settings: SlurmSettings) -> Self:
        """Where the run's Slurm jobs go and how long they may last; not used where the run has no Slurm job."""
        self._slurm = settings
        return self

    def log_level(self, level: LogLevel) -> Self:
        """Toil's `--logLevel`, wherever Toil runs; cwltool and Nextflow do not use it."""
        self._log_level = level
        return self


class ReadConfig(NamedTuple):
    """What a `RunConfig` says, as `read_config` reads it, and its problems, a line each in the user's words."""

    out_dir: str | None
    relative_to: str | None
    slurm: SlurmSettings
    log_level: LogLevel | None
    problems: tuple[str, ...]


#: How the compute service reads an outputs folder: absolute when it is a path from `/` or a `file:` URL, in any case
#: of letters; relative when it is neither, nor any other URL (`URL_SCHEME`), nor a path written for a shell
#: (`SHELL_PATH`), which the service refuses on its own, in its own words, and a site by the same two.
_SERVICE_ABSOLUTE: Final = re.compile('/|file:', re.IGNORECASE)
#: A URL's scheme first, as the compute service's driver reads one: `C:` too.
URL_SCHEME: Final = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')
#: A path written for a shell: `~` first, or a `$VARIABLE` anywhere, which the compute service's driver refuses and no
#: shell on a site expands.
SHELL_PATH: Final = re.compile(r'~|.*\$[{A-Za-z_]', re.DOTALL)


def read_config(config: RunConfig | None, *, local: bool) -> ReadConfig:
    """`config` (None says nothing) as a run reads it, `local`ly or where there is no working directory, on a site
    or the compute service: the one place the rules of its outputs folder and `relative_to` live. Every door reads it
    here and reports the problems its own way, a `config` that is not a RunConfig, such as the folder itself, among
    them; a site also refuses what this leaves to the service (`sophios.sites._site.site_config`).

    The rules: `relative_to` needs an outputs folder, and is an absolute path; an absolute outputs folder takes no
    `relative_to`; and, but in a local run, which reads it from the working directory, a relative outputs folder needs
    `relative_to`. Locally a path is absolute as this machine reads it; on the compute service as the service does,
    which judges, and refuses, what is neither absolute nor relative to it, and every value that is not a string.
    """
    if config is None:
        return ReadConfig(None, None, SlurmSettings(), None, ())
    if not isinstance(config, RunConfig):
        return ReadConfig(None, None, SlurmSettings(), None, (
            f"config={config!r} is not a RunConfig: make one, as RunConfig('results')",))
    # pylint: disable=protected-access  # the class's one reader, beside it
    out, relative_to = config._out_dir, config._relative_to
    read = partial(ReadConfig, out, relative_to, config._slurm, config._log_level)
    if not local and not all(isinstance(path, str) for path in (out, relative_to) if path is not None):
        return read(())  # a path the request cannot hold: its schema refuses it

    def absolute(path: str) -> bool:
        return Path(path).is_absolute() if local else _SERVICE_ABSOLUTE.match(path) is not None
    problems = []
    if relative_to is not None and out is None:
        problems.append(f'relative_to({relative_to!r}) is given with no outputs folder to read from it: name one, '
                        f"as RunConfig('results').relative_to({relative_to!r})")
    if relative_to is not None and not (Path(relative_to).is_absolute() if local else relative_to.startswith('/')):
        problems.append(f'relative_to({relative_to!r}) is not an absolute path: give the folder the relative '
                        'paths are read from as one')
    if out is not None and relative_to is not None and absolute(out):
        problems.append(f'the outputs folder {out!r} is an absolute path, and relative_to({relative_to!r}) is '
                        'given: give the folder relative to it, or give no relative_to')
    if out is not None and relative_to is None and not local and not (
            out.startswith('/') or URL_SCHEME.match(out) or SHELL_PATH.match(out)):
        problems.append(f'the outputs folder {out!r} is relative, and no relative_to is given to read it from, '
                        'as there is no working directory where the run runs: give relative_to(path), or an '
                        'absolute folder')
    return read(tuple(problems))
