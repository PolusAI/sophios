"""Core/contrib zone boundary: core never imports contrib.

The core compiler must never acquire a dependency on the peripheral surfaces.
See design_docs/core-refactor-design.md, Spec 0:

    contrib may import core. core may never import contrib.

This is checked over the real module graph rather than a fixed list, so a newly
added core module is covered without anyone remembering to update a whitelist.

LIMITS OF THIS CHECK — read before trusting a green result.

The scan is static. It sees `import`, `from ... import`, and relative imports,
and it also catches `import_module("sophios.contrib.x")` when the target is a
string literal. It cannot see a dynamic import whose target is computed:

    import_module(f".{name}", __name__)      # invisible to this scan

That construction is not hypothetical — `sophios/api/__init__.py` and
`sophios/api/python/__init__.py` both resolve submodules that way. A green
result therefore means "no statically visible crossing", which is weaker than
"no crossing". Enforcing the stronger claim would need import-time
instrumentation; that is not what this test does.
"""
import ast
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

SRC_ROOT = REPO_ROOT / 'src'
PACKAGE = 'sophios'

# The peripheral zone is a single subtree. Everything else under src/sophios
# is core. Core is deliberately NOT relocated: moving it would rename every
# path clients import and all four console entry points.
CONTRIB_PREFIXES = ('sophios.contrib',)


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


def _dynamic_target(node: ast.Call) -> str | None:
    """Return the target of `import_module("literal")`, if it is a literal.

    A computed target cannot be resolved statically; see the module docstring.
    """
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
    if name != 'import_module' or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value if first.value.startswith(PACKAGE) else None
    return None


def _imports_of(path: Path, module: str) -> set[str]:
    """Return the in-package modules that `path` imports, absolute and relative."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    is_package = path.name == '__init__.py'
    found: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Call():
                target = _dynamic_target(node)
                if target is not None:
                    found.add(target)
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
    """No core module reaches a contrib module through any import path."""
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


def _repo_python_files() -> list[Path]:
    """Every tracked Python file. Via git, so build output is excluded.

    Returns nothing outside a git checkout — an sdist ships `tests/` but no
    `.git` — and the guard below turns that into a skip rather than a silent
    pass. `-z` because git quotes unusual filenames otherwise, and an explicit
    encoding because git emits UTF-8 while `text=True` decodes with the
    locale's, which is cp1252 on Windows.
    """
    listing = subprocess.run(['git', 'ls-files', '-z', '*.py'], cwd=REPO_ROOT,
                             capture_output=True, text=True, encoding='utf-8',
                             check=False)
    if listing.returncode != 0:
        return []
    return [REPO_ROOT / name for name in listing.stdout.split('\0') if name]

# --------------------------------------------------------------------------
# Lines stay within the configured width
# --------------------------------------------------------------------------


#: Matches pyproject's max-line-length.
MAX_LINE_LENGTH: Final = 120


def _over_long_lines(path: Path) -> list[tuple[int, int]]:
    """Line numbers and lengths that exceed the limit. No exemptions."""
    return [(number, len(line))
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1)
            if len(line) > MAX_LINE_LENGTH]


@pytest.mark.fast
def test_the_width_scan_sees_the_repo() -> None:
    """Zero parametrized cases is a green test that enforces nothing."""
    if not (REPO_ROOT / '.git').exists():
        pytest.skip('not a git checkout; nothing to enumerate')
    assert _repo_python_files(), 'git ls-files resolved nothing; the width scan is vacuous'


@pytest.mark.fast
@pytest.mark.parametrize('path', _repo_python_files(),
                         ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_line_exceeds_the_configured_width(path: Path) -> None:
    """No Python file carries a line longer than pyproject allows.

    autopep8 does not reflow comments and pylint gates on a score, so nothing
    else enforces the setting.
    """
    offenders = _over_long_lines(path)
    assert not offenders, (
        f'{path.relative_to(REPO_ROOT)} has lines over {MAX_LINE_LENGTH} columns: '
        f'{[f"line {n} ({length})" for n, length in offenders]}'
    )
