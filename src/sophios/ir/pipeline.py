"""The direct typed front end: a parse, then Resolve, then Lower."""
from ..lang import ParseResult
from .lower import lower
from .resolve import RegistrySnapshot, resolve
from .types import PhaseResult


def front_end(parsed: ParseResult, registry: RegistrySnapshot, *, name: str = 'workflow',
              lang_version: str | None = None) -> PhaseResult:
    """Run the direct typed chain from a root already parsed; no mapping or
    text adapter sits within it. The graph, when every phase succeeded, and
    the diagnostics of the furthest phase reached."""
    if parsed.document is None or parsed.diagnostics.has_errors:
        return PhaseResult(None, parsed.diagnostics)
    resolved = resolve(parsed.document, registry, name=name, lang_version=lang_version)
    if resolved.document is None:
        return PhaseResult(None, resolved.diagnostics)
    return lower(resolved.document)
