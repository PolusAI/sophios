"""Immutable graph-derived compilation artifacts.

The sole internal boundary after Emit: application code consumes this
immutable tree, and the typed graph is the compilation's sole authority.
"""
from dataclasses import dataclass, field

from ..lang.diagnostics import Diagnostics
from ..wic_types import Cwl, GraphReps
from .realtime import Declaration
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
    #: The notes the compile made; a result never holds an error.
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    #: The real-time analyses the workflow declares, taken out of `graph`.
    realtime: tuple[Declaration, ...] = ()

    @property
    def lang_version(self) -> str:
        """The version selected once for this compilation tree."""
        return self.graph.lang_version
