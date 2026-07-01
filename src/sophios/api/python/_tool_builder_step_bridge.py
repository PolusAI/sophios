"""Small bridge between the CLT builder and the workflow Python API.

This module is intentionally the only place that imports both surfaces.
Keeping the bridge narrow lets the builder and workflow API evolve mostly
independently while still supporting an in-memory handoff.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sophios.wic_types import Tools

if TYPE_CHECKING:
    from .workflow import Step


class _CommandLineToolLike(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal protocol needed to turn a built CLT into a workflow `Step`."""

    name: str

    def to_cwl_document(self) -> dict[str, Any]:
        """Render the CLT to a plain CWL document."""


def _command_line_tool_to_step(
    tool: _CommandLineToolLike,
    *,
    step_name: str | None = None,
    run_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    tool_registry: Tools | None = None,
) -> "Step":
    """Convert a built CLT into a workflow `Step` without touching disk.

    Args:
        tool (_CommandLineToolLike): Built CLT-like object with `name` and `to_cwl_document()`.
        step_name (str | None): Optional workflow step name override.
        run_path (str | Path | None): Optional virtual `.cwl` path for compiler bookkeeping.
        config (dict[str, Any] | None): Optional input values to pre-bind on the step.
        tool_registry (Tools | None): Optional tool registry retained on the step.

    Returns:
        Step: An in-memory workflow step backed by the built CLT.
    """
    from .workflow import Step  # pylint: disable=C0415:import-outside-toplevel

    resolved_name = step_name or tool.name
    resolved_run_path = run_path or Path(f"{resolved_name}.cwl")
    return Step.from_cwl_document(
        tool.to_cwl_document(),
        process_name=resolved_name,
        run_path=resolved_run_path,
        config=config,
        tool_registry=tool_registry,
    )
