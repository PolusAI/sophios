"""What a local run does between compiling and starting the runner.

The one path from a compiled artifact to the files a runner is given, shared by
the command line and `Workflow.run()`: bind the job values, write the workflow
and its real-time analyses, check the machine and pull the images, and name the
images without entrypoints when asked.
"""
from dataclasses import dataclass, replace
from collections.abc import Collection
from functools import partial
from typing import Generic, TypeVar
from pathlib import Path
import tempfile

from sophios import input_output, plugins, preflight, realtime
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.resolved import ResolvedWorkflow
from sophios.lang.diagnostics import Diagnostic
from sophios.runners import RunSettings
from sophios.runtime_inputs import EmptyDirectory, directory_literal, runner_job_inputs
from sophios.wic_types import Yaml

ModelT = TypeVar('ModelT')


@dataclass(frozen=True, slots=True)
class Settings:
    """How one run is prepared.

    Attributes:
        run: The run: its runner, whose settings the pre-flight checks the machine for, and what is given to the job.
        root_dir: Where a relative path bound in the workflow is read: the `.wic`'s
            directory from the command line, the working directory from Python.
        bound: The values bound in the workflow as the runner reads them, a relative path as the author wrote it
            (`Target.run_values`): what the pre-flight checks, and its messages name.
        inputs_file_named: How the user named `run.inputs_file`, for the pre-flight's messages.
        check: Check the machine and stop: nothing is pulled.
        pull: Pull the images the run uses; off when an earlier step pulled them.
    """

    run: RunSettings
    root_dir: Path
    bound: Yaml
    inputs_file_named: str = '--inputs_file'
    check: bool = False
    pull: bool = True


@dataclass(frozen=True, slots=True)
class Lowered(Generic[ModelT]):
    """What a target made of one compilation (`sophios.targets`): its model, the notes it made, and the resolved
    view it read, with this run's values and the outputs it keeps."""

    model: ModelT
    notes: tuple[Diagnostic, ...]
    view: ResolvedWorkflow


@dataclass(frozen=True, slots=True)
class Prepared:
    """A run's files, written: the name of the workflow the runner gets, its analyses' plans, the documents written
    for it, and the folders its machine makes before it starts."""

    name: str
    plans: tuple[realtime.Plan, ...]
    documents: tuple[Path, ...]
    folders: tuple[str, ...] = ()


# pylint: disable-next=too-many-arguments
def write(artifact: CompilationArtifact, analyses: tuple[realtime.Analysis, ...], basepath: Path, *,
          inputs_file: str, root_dir: Path, output_targets: Collection[str],
          empty_directory: EmptyDirectory) -> Prepared:
    """Write `artifact` and `analyses` under `basepath` as they are given, and nothing elsewhere.

    `artifact` is written as it is: a tree from the command line, one document under
    `--cwl_inline_runtag` and from Python. Its job is the values bound in the workflow, as `targets.for_run` made
    them for the runner, under the inputs file's. Each of `output_targets` (`runtime_inputs.output_target_inputs`)
    is a name for the run to create, written in the job as `empty_directory` gives the runner an empty directory.
    """
    input_output.write_artifacts_to_disk(artifact, basepath, inputs_file, output_targets=output_targets,
                                         empty_directory=empty_directory)
    plans = realtime.write(analyses, basepath, artifact.name, root_dir)
    return Prepared(artifact.name, plans, (basepath / f'{artifact.name}.cwl', *realtime.documents(analyses, basepath)))


# pylint: disable-next=too-many-arguments
def prepare(artifact: CompilationArtifact, analyses: tuple[realtime.Analysis, ...], basepath: Path,
            settings: Settings, output_targets: Collection[str], *,
            empty_directory: EmptyDirectory = directory_literal) -> Prepared:
    """`write`, then make this machine ready to run what it wrote. The pre-flight reads `settings.bound`, the same
    values as written.

    A check (`settings.check`) writes them into a temporary directory instead, removed
    when the check ends, so it leaves `basepath` as it found it.

    Raises:
        SophiosError: What the pre-flight found in the way, before anything is pulled.
    """
    if not settings.check:
        return _prepare(artifact, analyses, basepath, settings, output_targets, empty_directory=empty_directory)
    with tempfile.TemporaryDirectory(prefix='sophios-check-') as scratch:
        return _prepare(artifact, analyses, Path(scratch), settings, output_targets, empty_directory=empty_directory)


def inputs_job(inputs_file: str, named: str, output_targets: Collection[str] = ()) -> preflight.Job | None:
    """The values of `inputs_file`, as the runner gets them (`output_targets` as names to create), with the
    directory their relative paths are read from; None without one. `named` is how the user named the file."""
    if not inputs_file:
        return None
    return preflight.Job(runner_job_inputs(input_output.read_inputs_file(inputs_file), output_targets),
                         Path(inputs_file).parent.absolute(), named)


# pylint: disable-next=too-many-arguments
def _prepare(artifact: CompilationArtifact, analyses: tuple[realtime.Analysis, ...], basepath: Path,
             settings: Settings, output_targets: Collection[str], *, empty_directory: EmptyDirectory) -> Prepared:
    """`prepare`, writing under `basepath`."""
    inputs_file, root_dir = settings.run.inputs_file, settings.root_dir
    write_here = partial(write, basepath=basepath, inputs_file=inputs_file, root_dir=root_dir,
                         output_targets=output_targets, empty_directory=empty_directory)
    # The pre-flight reads the CWL the runner will get, so it runs after the write.
    written = write_here(artifact, analyses)
    extra = inputs_job(inputs_file, settings.inputs_file_named, output_targets)
    # The inputs file wins over a value bound in the workflow for the same input when the job is written.
    bound = runner_job_inputs(settings.bound, output_targets)
    jobs = (preflight.Job({key: value for key, value in bound.items() if extra is None or key not in extra.values},
                          root_dir, 'the workflow'), *((extra,) if extra else ()))
    preflight.ready(written.documents, settings.run, jobs, pulling=settings.pull and not settings.check)
    if settings.check or not settings.run.runner.remove_entrypoints:
        return written
    plugins.remove_entrypoints(settings.run.runner.container_engine,
                               (artifact, *(analysis.artifact for analysis in analyses)))
    # The images are built from the ones the pre-flight pulled, so the rewrite is written over the first write.
    return write_here(plugins.dockerPull_append_noentrypoint_artifact(artifact), tuple(
        replace(analysis, artifact=plugins.dockerPull_append_noentrypoint_artifact(analysis.artifact))
        for analysis in analyses))
