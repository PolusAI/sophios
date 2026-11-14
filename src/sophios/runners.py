"""The programs that run a compiled workflow, and the one value that describes a local run.

`Cwltool`, `Toil` and `Nextflow` are what `Workflow.run(runner=...)` takes and the command line's flags build; a
`RunSettings` is a runner and what is given to the job. This module imports nothing from Sophios, so the command
line's parser reads its defaults here without importing a backend.
"""
import copy
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Final, Self, TypeAlias, get_args


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
        remove_entrypoints: Run every image without its entrypoint.
        cachedir: Where cwltool caches a job whose tool and inputs are unchanged; '' for no cache.
    """

    cachedir: str
    name: ClassVar[str] = 'cwltool'
    program: ClassVar[str] = 'cwltool_filterlog'
    install: ClassVar[str] = 'install Sophios in this environment (pip install sophios), which provides it'

    # pylint: disable-next=too-many-arguments
    def __init__(self, *flags: str, container_engine: str = 'docker', pull_dir: str = '', ignore_install: bool = False,
                 ignore_processes: bool = False, provenance: bool = True, remove_entrypoints: bool = False,
                 cachedir: str = '') -> None:
        super().__init__(*flags, container_engine=container_engine, pull_dir=pull_dir, ignore_install=ignore_install,
                         ignore_processes=ignore_processes, provenance=provenance,
                         remove_entrypoints=remove_entrypoints)
        object.__setattr__(self, 'cachedir', cachedir)


@dataclass(frozen=True, init=False)
class Toil(_CwlRunner):
    """Toil's CWL runner, `toil-cwl-runner`: the settings of `Cwltool` but `cachedir`, which Toil does not read."""

    name: ClassVar[str] = 'toil-cwl-runner'
    program: ClassVar[str] = 'toil-cwl-runner'
    install: ClassVar[str] = 'install Toil (pip install "toil[cwl]")'
    cachedir: ClassVar[str] = ''


@dataclass(frozen=True, init=False)
class Nextflow(_Runner):
    """Nextflow, which runs the pipeline the target `nextflow` lowers a workflow to. `flags` are its options, as it
    spells them: `Nextflow('-process.executor', 'slurm', '-resume')`."""

    name: ClassVar[str] = 'nextflow'
    target: ClassVar[str] = 'nextflow'
    program: ClassVar[str] = 'nextflow'
    install: ClassVar[str] = 'install Nextflow (https://www.nextflow.io/docs/latest/install.html)'
    # What the generated configuration hard-wires (`docker.enabled`); the settings of a CWL runner it has none of.
    container_engine: ClassVar[str] = 'docker'
    pull_dir: ClassVar[str] = ''
    ignore_install: ClassVar[bool] = False
    ignore_processes: ClassVar[bool] = False
    provenance: ClassVar[bool] = False
    remove_entrypoints: ClassVar[bool] = False
    cachedir: ClassVar[str] = ''

    def __init__(self, *flags: str) -> None:
        object.__setattr__(self, 'flags', flags)


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
    it has: the one place a runner is chosen (`cachedir` is `Cwltool`'s alone; `Nextflow` has none).

    Raises:
        ValueError: `target` has no runner called `name`.
    """
    runner = runners_of(target)[0] if name is None else named(target, name)
    own = {field.name for field in fields(runner)}
    return runner(*flags, **{setting: value for setting, value in settings.items() if setting in own})


@dataclass(frozen=True, slots=True)
class RunSettings:
    """One local run: its runner, and what is given to the job.

    Attributes:
        runner: The program that runs it, with its flags and settings; the target is `runner.target`.
        outdir: The runner's output directory; '' for `<basepath>/outdir_<runner>_<name>_<time>`.
        inputs_file: A job file whose values win over the workflow's, or ''.
        quiet: Pass `--quiet` to cwltool (never to Toil), which hides its own log.
        generate_run_script: Write `run.sh` with the runner's command instead of running it.
    """

    runner: Runner
    outdir: str = ''
    inputs_file: str = ''
    quiet: bool = True
    generate_run_script: bool = False
