"""The targets a workflow compiles to: one table, which every entry point reads.

A row is a target's whole contract: how it lowers a compilation, refusing what it cannot run with a `NotLowered`
whose diagnostics carry their codes; how it writes what it lowered; and the runners that run it. The command line,
`Workflow.compile`, `check`, `run`, `start` and `run_async`, and the run path find a target here, by the name
`--target` and `target=` give it, and nowhere else (design_docs/one-target-table-design.md §2).

The names alone are `wic_types.TargetName`, which the command line's parser reads without importing a target.
"""
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeVar

from . import input_output, input_output_nf, realtime
from .backends import Backend, CwltoolBackend, NextflowBackend, ToilBackend
from .compiler import cwl_notes
from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.resolved import ResolvedWorkflow
from .nf_types import ExecutableNextflowWorkflow
from .run_setup import Lowered
from .utils_nf import NEXTFLOW as _NEXTFLOW_LOWERING

ModelT = TypeVar('ModelT')


class Target(Protocol[ModelT]):
    """What a target is."""

    @property
    def name(self) -> str:
        """As `--target` and `target=` name it."""

    @property
    def runners(self) -> Mapping[str, Backend]:
        """Its runners by name, the default first."""

    @property
    def runner_option(self) -> str | None:
        """The run argument that picks the runner, `--<option>` on the command line and a `run_args_dict` key in
        Python; None for a target with one runner."""

    @property
    def run_defaults(self) -> Mapping[str, str]:
        """A Python run's `run_args_dict` defaults, which the call's own keys win over."""

    @property
    def takes_plain_cwl(self) -> bool:
        """Whether `--allow_raw_cwl` hands it a plain CWL workflow as it is, without compiling it."""

    @property
    def write_option(self) -> str | None:
        """The command-line flag that asks it to write without running, `--<option>`; None when it writes whenever
        it does not run."""

    def lower(self, result: CompilationResult, view: ResolvedWorkflow) -> Lowered[ModelT]:
        """The model `result` lowers to, read through `view`, and the notes the target makes.

        Raises:
            NotLowered: The target cannot run the workflow: every refusal, each an error diagnostic with its code.
        """

    def clear(self, basepath: Path) -> None:
        """Remove what an earlier write of this target left under `basepath`, so a refused workflow leaves none."""

    def write(self, lowered: Lowered[ModelT], analyses: tuple[realtime.Analysis, ...], basepath: Path, *,
              inputs_file: str, root_dir: Path) -> None:
        """Write the model under `basepath`, with the real-time analyses it runs; `inputs_file` holds values that
        win over the workflow's, and a relative path is read against `root_dir`."""


class _Cwl:
    """CWL: it refuses nothing, and its model is the CWL the compile emitted, as it emitted it."""

    name = 'cwl'
    runners: Mapping[str, Backend] = MappingProxyType({'cwltool': CwltoolBackend(), 'toil-cwl-runner': ToilBackend()})
    runner_option = 'cwl_runner'
    run_defaults: Mapping[str, str] = MappingProxyType({
        'cwl_runner': 'cwltool', 'container_engine': 'docker', 'pull_dir': str(Path().cwd()), 'quiet': 'yes'})
    write_option = 'generate_cwl_workflow'
    takes_plain_cwl = True

    def lower(self, result: CompilationResult, view: ResolvedWorkflow) -> Lowered[CompilationArtifact]:
        """`result.artifact`, and `wic056` at each `when` that cannot give `true` or `false`."""
        return Lowered(result.artifact, cwl_notes(view), view)

    def clear(self, basepath: Path) -> None:
        """Nothing: a write replaces every document it names."""

    def write(self, lowered: Lowered[CompilationArtifact], analyses: tuple[realtime.Analysis, ...], basepath: Path,
              *, inputs_file: str, root_dir: Path) -> None:
        """Each document under its own name, the root's job, and each analysis beside them."""
        input_output.write_artifacts_to_disk(lowered.model, basepath, inputs_file)
        realtime.write(analyses, basepath, lowered.model.name, root_dir)


class _Nextflow:
    """Nextflow DSL2: the resolved view, lowered to the executable model (design_docs/sophios_nextflow_backend.md)."""

    name = 'nextflow'
    runners: Mapping[str, Backend] = MappingProxyType({'nextflow': NextflowBackend()})
    runner_option = None
    run_defaults: Mapping[str, str] = MappingProxyType({})
    write_option = None
    takes_plain_cwl = False

    def lower(self, result: CompilationResult,  # pylint: disable=unused-argument
              view: ResolvedWorkflow) -> Lowered[ExecutableNextflowWorkflow]:
        """The model the view lowers to, and the notes the lowering made; it never reads `result`'s CWL."""
        model, notes = _NEXTFLOW_LOWERING.lower(view)
        return Lowered(model, notes, view)

    def clear(self, basepath: Path) -> None:
        """The four files a write makes."""
        input_output_nf.remove_nextflow_artifacts(basepath)

    def write(self, lowered: Lowered[ExecutableNextflowWorkflow], analyses: tuple[realtime.Analysis, ...],
              basepath: Path, *, inputs_file: str, root_dir: Path) -> None:
        """The pipeline, its configuration, its parameters and its model."""
        # A real-time analysis watches cwltool's job directories, which a Nextflow run has none of, and an inputs
        # file is a CWL runner's: neither is written, and no relative path of theirs is read.
        del analyses, inputs_file, root_dir
        input_output_nf.write_nextflow_artifacts(lowered.model, basepath)


#: Every target, by name.
TARGETS: Final[Mapping[str, Target[Any]]] = MappingProxyType({'cwl': _Cwl(), 'nextflow': _Nextflow()})


def target(name: str) -> Target[Any]:
    """The target called `name`: the one place an unknown target is reported.

    Raises:
        ValueError: No target has that name.
    """
    try:
        return TARGETS[name]
    except KeyError:
        raise ValueError(f"unknown target {name!r}; the targets are {', '.join(TARGETS)}") from None


def backend(name: str, runner: str | None = None) -> Backend:
    """The backend that runs the target `name` with `runner`, its default when None.

    Raises:
        ValueError: No target has that name, or none of its runners is `runner`.
    """
    row = target(name)
    chosen = runner if runner is not None else next(iter(row.runners))
    try:
        return row.runners[chosen]
    except KeyError:
        raise ValueError(f"No backend runs {name} with {chosen!r}; the runners are "
                         f"{', '.join(row.runners)}.") from None
