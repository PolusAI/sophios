"""The targets a workflow compiles to: one table, which every entry point reads.

A row is a target's whole contract: how it lowers a compilation, refusing what it cannot run with a `NotLowered`
whose diagnostics carry their codes; and how it writes what it lowered; its runners are those of
`sophios.runners`. The command line, `Workflow.compile`, `check`, `run`, `start` and `run_async`, and the run path
find a target here, by the name `--target` and `target=` give it, and nowhere else.

The names alone are `wic_types.TargetName`, which the command line's parser reads without importing a target.
"""
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeVar

from . import input_output, input_output_nf, realtime
from .backends import Backend, CwltoolBackend, NextflowBackend, ToilBackend
from .compiler import cwl_notes
from .ir import emit
from .ir.artifacts import CompilationArtifact
from .ir.locations import absolute_paths
from .ir.resolved import ResolvedWorkflow, bind
from .lang.nodes import OpaqueCwl
from .nf_types import ExecutableNextflowWorkflow
from .run_setup import Lowered
from .runners import RUNNERS, Cwltool, Nextflow, Runner, Toil, build, named
from .runtime_inputs import name_output_targets, output_target_inputs
from .utils_nf import NEXTFLOW as _NEXTFLOW_LOWERING, local_locations_as_paths

ModelT = TypeVar('ModelT')


class Target(Protocol[ModelT]):
    """What a target is."""

    @property
    def name(self) -> str:
        """As `--target` and `target=` name it."""

    @property
    def takes_plain_cwl(self) -> bool:
        """Whether `--allow_raw_cwl` hands it a plain CWL workflow as it is, without compiling it."""

    @property
    def write_option(self) -> str | None:
        """The command-line flag that asks it to write without running, `--<option>`; None when it writes whenever
        it does not run."""

    def run_values(self, view: ResolvedWorkflow) -> dict[str, OpaqueCwl]:
        """`view`'s root values as the target's runner reads them, before a relative path in them is made absolute:
        the rewrite that must come first, because it reads the path as written."""

    def lower(self, view: ResolvedWorkflow) -> Lowered[ModelT]:
        """The model `view` lowers to, and the notes the target makes.

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
    """CWL: it refuses nothing, and its model is the CWL it emits from the view."""

    name = 'cwl'
    write_option = 'generate_cwl_workflow'
    takes_plain_cwl = True

    def run_values(self, view: ResolvedWorkflow) -> dict[str, OpaqueCwl]:
        """Each output-target Directory a name for the run to create: a runner given an absolute path for a
        Directory would never create it."""
        return name_output_targets(view.values, output_target_inputs(view))

    def lower(self, view: ResolvedWorkflow) -> Lowered[CompilationArtifact]:
        """The artifact tree emitted from the view, and `wic056` at each `when` that cannot give `true` or `false`."""
        return Lowered(emit.artifact_tree(view), cwl_notes(view), view)

    def clear(self, basepath: Path) -> None:
        """Nothing: a write replaces every document it names."""

    def write(self, lowered: Lowered[CompilationArtifact], analyses: tuple[realtime.Analysis, ...], basepath: Path,
              *, inputs_file: str, root_dir: Path) -> None:
        """Each document under its own name, the root's job, and each analysis beside them."""
        input_output.write_artifacts_to_disk(lowered.model, basepath, inputs_file,
                                             output_targets=output_target_inputs(lowered.view))
        realtime.write(analyses, basepath, lowered.model.name, root_dir)


class _Nextflow:
    """Nextflow DSL2: the resolved view, lowered to the executable model (design_docs/sophios_nextflow_backend.md)."""

    name = 'nextflow'
    write_option = None
    takes_plain_cwl = False

    def run_values(self, view: ResolvedWorkflow) -> dict[str, OpaqueCwl]:
        """Each local `location` a `path`, and no format: the model refuses a URI, which an absolute `location` is."""
        values: dict[str, OpaqueCwl] = local_locations_as_paths(view.values)
        return values

    def lower(self, view: ResolvedWorkflow) -> Lowered[ExecutableNextflowWorkflow]:
        """The model the view lowers to, and the notes the lowering made."""
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


def for_run(row: Target[Any], view: ResolvedWorkflow, root_dir: Path) -> ResolvedWorkflow:
    """`view` for a local run of `row`: its values as the runner reads them, each relative path read against
    `root_dir` and written absolute, because a runner does not run where the workflow's paths are read."""
    return bind(view, values=absolute_paths(row.run_values(view), root_dir))


def target(name: str) -> Target[Any]:
    """The target called `name`: the one place an unknown target is reported.

    Raises:
        ValueError: No target has that name.
    """
    try:
        return TARGETS[name]
    except KeyError:
        raise ValueError(f"unknown target {name!r}; the targets are {', '.join(TARGETS)}") from None


#: The backend of each runner, by its name.
_BACKENDS: Final[Mapping[str, Backend]] = MappingProxyType({
    Cwltool.name: CwltoolBackend(), Toil.name: ToilBackend(), Nextflow.name: NextflowBackend()})


def resolve(name: str, runner: Runner | None) -> Runner:
    """`runner`, or when None the default runner of the target `name`: the one place a runner is chosen for a target.

    Raises:
        ValueError: No target has that name, or `runner` does not run it.
    """
    row = target(name)
    if runner is None:
        return build(row.name, None, ())
    if not isinstance(runner, RUNNERS):
        raise ValueError(f"runner= takes a value such as Cwltool(...), Toil(...) or Nextflow(...), not {runner!r}")
    named(row.name, runner.name)
    return runner


def backend(runner: Runner) -> Backend:
    """The backend that runs a workflow with `runner`."""
    return _BACKENDS[runner.name]
