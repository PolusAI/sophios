"""Core/contrib zone boundary (CR-001, property P01).

The core compiler must never acquire a dependency on the peripheral surfaces.
See design_docs/core-refactor-design.md, Spec 0:

    contrib may import core. core may never import contrib.

This is checked over the real module graph rather than a fixed list, so a newly
added core module is covered without anyone remembering to update a whitelist.
"""
import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / 'src'
PACKAGE = 'sophios'

# The peripheral zone. Everything else under src/sophios is core.
CONTRIB_PREFIXES = (
    'sophios.api.utils.ict',
    'sophios.api.utils.converter',
    'sophios.api.rest',
)


def _module_name(path: Path) -> str:
    """Return the dotted module name for a file under src/."""
    rel = path.relative_to(SRC_ROOT).with_suffix('')
    parts = list(rel.parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _is_contrib(module: str) -> bool:
    """Return whether a dotted module name lies in the contrib zone."""
    return any(module == p or module.startswith(p + '.') for p in CONTRIB_PREFIXES)


def _resolve_relative(module: str, node: ast.ImportFrom, is_package: bool) -> str:
    """Resolve a relative `from . import x` target to an absolute module name."""
    parts = module.split('.')
    # Inside a package's __init__, level 1 refers to the package itself.
    if not is_package:
        parts.pop()
    for _ in range(node.level - 1):
        if parts:
            parts.pop()
    if node.module:
        parts.append(node.module)
    return '.'.join(parts)


def _imports_of(path: Path, module: str) -> set[str]:
    """Return the in-package modules that `path` imports, absolute and relative."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    is_package = path.name == '__init__.py'
    found: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import():
                found.update(a.name for a in node.names if a.name.startswith(PACKAGE))
            case ast.ImportFrom(level=0, module=str() as mod) if mod.startswith(PACKAGE):
                found.add(mod)
                found.update(f'{mod}.{a.name}' for a in node.names)
            case ast.ImportFrom(level=int() as level) if level > 0:
                base = _resolve_relative(module, node, is_package)
                if base:
                    found.add(base)
                    found.update(f'{base}.{a.name}' for a in node.names)
    return found


def _import_graph() -> dict[str, set[str]]:
    """Build the in-package import graph for every module under src/sophios."""
    graph: dict[str, set[str]] = {}
    for path in sorted((SRC_ROOT / PACKAGE).rglob('*.py')):
        module = _module_name(path)
        graph[module] = _imports_of(path, module)
    return graph


def _reachable(start: str, graph: dict[str, set[str]]) -> set[str]:
    """Return every in-package module transitively reachable from `start`."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for target in graph.get(current, set()):
            if target in seen:
                continue
            # An imported name may be a symbol rather than a module; keep only
            # targets that correspond to real modules in the graph.
            if target in graph:
                seen.add(target)
                stack.append(target)
    return seen


@pytest.mark.fast
def test_core_never_imports_contrib() -> None:
    """P01: no core module reaches a contrib module through any import path."""
    graph = _import_graph()
    core = [m for m in graph if not _is_contrib(m)]
    assert core, 'no core modules discovered; the zone scan is broken'

    # Report the edges that actually cross the boundary. Listing every
    # transitively reachable contrib module instead would bury the one or two
    # imports someone has to delete.
    crossings = sorted(
        (module, target)
        for module in core
        for target in graph[module]
        if _is_contrib(target) and target in graph
    )
    affected = sorted(
        module for module in core
        if any(_is_contrib(t) for t in _reachable(module, graph))
    )

    detail = '\n'.join(f'  {module} -> {target}' for module, target in crossings)
    assert not affected, (
        'core modules must never depend on contrib.\n'
        'design_docs/core-refactor-design.md, Spec 0: "contrib may import core. '
        'core may never import contrib."\n\n'
        f'Direct boundary crossings ({len(crossings)}) — remove these imports:\n'
        f'{detail}\n\n'
        f'{len(affected)} core module(s) are affected, directly or transitively.'
    )


@pytest.mark.fast
def test_zone_scan_finds_both_zones() -> None:
    """Sanity check: the scan actually discovers modules in each zone.

    Without this, an empty or misconfigured scan would make the boundary
    property pass vacuously.
    """
    graph = _import_graph()
    assert any(_is_contrib(m) for m in graph), 'no contrib modules discovered'
    assert any(not _is_contrib(m) for m in graph), 'no core modules discovered'


@pytest.mark.fast
def test_contrib_may_import_core() -> None:
    """Sanity check: the invariant is one-directional, not a ban on coupling."""
    graph = _import_graph()
    contrib = [m for m in graph if _is_contrib(m)]
    assert contrib, 'no contrib modules discovered'
    assert any(
        any(not _is_contrib(t) for t in _reachable(m, graph)) for m in contrib
    ), 'expected contrib to depend on core; the scan may be resolving nothing'
