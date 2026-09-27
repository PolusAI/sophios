"""Immutable graph-derived compilation artifacts.

The sole internal boundary after Emit: application code consumes this
immutable tree, and the typed graph is the compilation's sole authority.
"""
from dataclasses import dataclass

from ..wic_types import Cwl, GraphReps
from .types import WorkflowGraph


@dataclass(frozen=True, slots=True)
class CompilationArtifact:
    """One emitted workflow or tool and its child artifacts."""

    namespace: tuple[str, ...]
    name: str
    run_path: str
    cwl: Cwl
    job_inputs: Cwl
    graph: WorkflowGraph | None
    graph_view: GraphReps
    children: tuple['CompilationArtifact', ...] = ()


@dataclass(frozen=True, slots=True)
class CompilationResult:
    """The sole internal result of a successful typed compilation."""

    graph: WorkflowGraph
    artifact: CompilationArtifact

    @property
    def lang_version(self) -> str:
        """The version selected once for this compilation tree."""
        return self.graph.lang_version
