"""Immutable graph-derived compilation results.

A compile ends at the graph and the tool each step runs; a target emits from them.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..lang.diagnostics import Diagnostics
from ..lang.tools import ToolNode
from .policy import RunPolicy
from .realtime import Declaration
from .types import StepId, WorkflowGraph


@dataclass(frozen=True, slots=True)
class CompilationResult:
    """The sole internal result of a successful typed compilation."""

    graph: WorkflowGraph
    #: The tool each tool step runs, at any depth: the registry's, packed where it refers to files beside it.
    tools: Mapping[StepId, ToolNode]
    #: The version selected once for this compilation tree.
    lang_version: str
    #: The notes the compile made; a result never holds an error.
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    #: The real-time analyses the workflow declares, taken out of `graph`.
    realtime: tuple[Declaration, ...] = ()
    #: How the workflow runs: the resolved view carries it to a target.
    policy: RunPolicy = RunPolicy()
