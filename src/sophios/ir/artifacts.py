"""Immutable graph-derived compilation results.

A compile ends at the graph and the tool each step runs; a target emits from them.
`CompilationArtifact` is the CWL target's model.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..lang.diagnostics import Diagnostics
from ..lang.spans import SourceSpan
from ..wic_types import Cwl, Tool
from .policy import RunPolicy
from .realtime import Declaration
from .types import StepId, WorkflowGraph


@dataclass(frozen=True, slots=True)
class Inlining:
    """What `--cwl_inline_subworkflows` needs to know of a workflow's call: whether it may be inlined (False when the
    workflow is marked `wic: inlineable: false`), and where the step that runs it was written, when it was."""

    inlineable: bool = True
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class CompilationArtifact:
    """One emitted workflow or tool and its child artifacts."""

    namespace: tuple[str, ...]
    name: str
    run_path: str
    cwl: Cwl
    job_inputs: Cwl
    children: tuple['CompilationArtifact', ...] = ()
    inlining: Inlining = Inlining()


@dataclass(frozen=True, slots=True)
class CompilationResult:
    """The sole internal result of a successful typed compilation."""

    graph: WorkflowGraph
    #: The tool each tool step runs, at any depth: the registry's, packed where it refers to files beside it.
    tools: Mapping[StepId, Tool]
    #: The notes the compile made; a result never holds an error.
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    #: The real-time analyses the workflow declares, taken out of `graph`.
    realtime: tuple[Declaration, ...] = ()
    #: How the workflow runs: the resolved view carries it to a target.
    policy: RunPolicy = RunPolicy()

    @property
    def lang_version(self) -> str:
        """The version selected once for this compilation tree."""
        return self.graph.lang_version
