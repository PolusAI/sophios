"""The direct typed front end: Parse, then Resolve, then Lower."""
from dataclasses import dataclass

from ..lang import Diagnostics, ParseResult, parse
from .lower import Lowered, lower
from .resolve import RegistrySnapshot, Resolved, resolve
from .types import WorkflowGraph


@dataclass(frozen=True, slots=True)
class FrontEndResult:
    """Each typed value in the direct Parse-to-Resolve-to-Lower chain."""

    parsed: ParseResult
    resolved: Resolved | None
    lowered: Lowered | None

    @property
    def graph(self) -> WorkflowGraph | None:
        """The lowered graph, when every front-end phase succeeded."""
        return self.lowered.graph if self.lowered is not None else None

    @property
    def diagnostics(self) -> Diagnostics:
        """Diagnostics from the furthest phase reached."""
        if self.lowered is not None:
            return self.lowered.diagnostics
        if self.resolved is not None:
            return self.resolved.diagnostics
        return self.parsed.diagnostics


def front_end(source: str, registry: RegistrySnapshot, *, name: str = 'workflow',
              lang_version: str | None = None) -> FrontEndResult:
    """Run the direct typed chain; no mapping or text adapter sits within it."""
    parsed = parse(source, f'{name}.wic')
    if parsed.document is None or parsed.diagnostics.has_errors:
        return FrontEndResult(parsed, None, None)
    resolved = resolve(parsed.document, registry, name=name, lang_version=lang_version)
    if resolved.document is None or resolved.diagnostics.has_errors:
        return FrontEndResult(parsed, resolved, None)
    return FrontEndResult(parsed, resolved, lower(resolved.document))
