"""The CWL target's model of one emitted workflow or tool: the CWL it writes, the job inputs, and the artifacts of the
workflows it calls."""
from dataclasses import dataclass

from ..lang.spans import SourceSpan
from ..wic_types import Cwl


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
