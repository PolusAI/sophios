"""What a local run does between compiling and starting the runner.

The one path from a compiled artifact to the files a runner is given, shared by
the command line and `Workflow.run()`: bind the job values, write the workflow
and its real-time analyses, check the machine and pull the images, and name the
images without entrypoints when asked.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile

from sophios import input_output, post_compile, preflight, realtime
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.locations import absolute_paths
from sophios.lang.diagnostics import Diagnostic
from sophios.runtime_inputs import normalize_artifact_job_inputs


@dataclass(frozen=True, slots=True)
class Settings:
    """How one run is prepared.

    Attributes:
        run: What the pre-flight checks the machine for.
        root_dir: Where a relative path bound in the workflow is read: the `.wic`'s
            directory from the command line, the working directory from Python.
        inputs_file: A job file whose values win over the workflow's, or ''.
        inputs_file_named: How the user named `inputs_file`, for the pre-flight's messages.
        check: Check the machine and stop: nothing is pulled.
        pull: Pull the images the run uses; off when an earlier step pulled them.
        remove_entrypoints: Run every image without its entrypoint.
    """

    run: preflight.RunSettings
    root_dir: Path
    inputs_file: str = ''
    inputs_file_named: str = '--inputs_file'
    check: bool = False
    pull: bool = True
    remove_entrypoints: bool = False


@dataclass(frozen=True, slots=True)
class Prepared:
    """A run ready to start: the workflow as the runner gets it, its analyses' plans, and the notes
    preparing it made, for the front door to report."""

    artifact: CompilationArtifact
    plans: tuple[realtime.Plan, ...]
    documents: tuple[Path, ...]
    notes: tuple[Diagnostic, ...] = ()


def prepare(artifact: CompilationArtifact, analyses: tuple[realtime.Analysis, ...], basepath: Path,
            settings: Settings) -> Prepared:
    """Write `artifact` and `analyses` under `basepath` as they are given, and make the machine ready to run them.

    `artifact` is written as it is: a tree from the command line, one document under
    `--cwl_inline_runtag` and from Python. Its job is the values bound in the workflow,
    a relative path read against `settings.root_dir`, under the inputs file's.

    A check (`settings.check`) writes them into a temporary directory instead, removed
    when the check ends, so it leaves `basepath` as it found it.

    Raises:
        SophiosError: What the pre-flight found in the way, before anything is pulled.
    """
    if not settings.check:
        return _prepare(artifact, analyses, basepath, settings)
    with tempfile.TemporaryDirectory(prefix='sophios-check-') as scratch:
        return _prepare(artifact, analyses, Path(scratch), settings)


def _prepare(artifact: CompilationArtifact, analyses: tuple[realtime.Analysis, ...], basepath: Path,
             settings: Settings) -> Prepared:
    """`prepare`, writing under `basepath`."""
    # As the runner gets them, in which an output-target Directory is a name for the run to create.
    bound = normalize_artifact_job_inputs(artifact, artifact.job_inputs)
    # The runner reads the job from `basepath`, so a relative path is written absolute.
    artifact = replace(artifact, job_inputs=absolute_paths(bound, settings.root_dir))
    input_output.write_artifacts_to_disk(artifact, basepath, settings.inputs_file)
    plans = realtime.write(analyses, basepath, artifact.name, settings.root_dir)
    # The pre-flight reads the CWL the runner will get, so it runs after the write.
    documents = (basepath / f'{artifact.name}.cwl', *realtime.documents(analyses, basepath))
    inputs_file = settings.inputs_file
    extra = normalize_artifact_job_inputs(artifact, input_output.read_inputs_file(inputs_file)) if inputs_file else {}
    # The inputs file wins over a value bound in the workflow for the same input when the job is written.
    jobs = (preflight.Job({key: value for key, value in bound.items() if key not in extra},
                          settings.root_dir, 'the workflow'),
            *((preflight.Job(extra, Path(inputs_file).parent.absolute(), settings.inputs_file_named),)
              if inputs_file else ()))
    if settings.check:
        preflight.check(preflight.needs(list(documents), jobs), settings.run)
        return Prepared(artifact, plans, documents)
    if settings.pull:
        # The real-time analyses' images too: they run with --disable-pull.
        preflight.prepare(list(documents), settings.run, jobs)
    else:
        preflight.check(preflight.needs(list(documents), jobs), settings.run)
    if settings.remove_entrypoints:
        artifact = post_compile.remove_artifact_entrypoints(settings.run.container_engine, artifact)
        # The images are built from the ones the pre-flight pulled, so the rewrite is written over the first write.
        input_output.write_artifacts_to_disk(artifact, basepath, settings.inputs_file)
        plans = realtime.write(realtime.without_entrypoints(analyses), basepath, artifact.name, settings.root_dir)
    return Prepared(artifact, plans, documents)
