"""Declared entry points must resolve to real modules and attributes.

Entry points are module paths written as strings — in `pyproject.toml` console
scripts, and in container `CMD`/`command` lines. Nothing type-checks a string,
so a module rename silently invalidates them and the failure only appears when
someone runs the CLI or starts the container.

This has already happened twice in this repository: `sophios.api.http.restapi`
survived in the REST image long after the module was removed, and the contrib
relocation briefly left the same image pointing at `sophios.api.rest.api`.
Both were rename-without-updating-the-string.
"""
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from .test_zone_boundary import CONTRIB_PREFIXES

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / 'docker'
PYPROJECT = REPO_ROOT / 'pyproject.toml'

# "uvicorn some.module:attr" in a Dockerfile CMD or compose command.
UVICORN_TARGET = re.compile(r'uvicorn"?,?\s*"?([\w.]+):(\w+)"?')


def _console_scripts() -> list[tuple[str, str, str]]:
    """Return (name, module, attribute) for each declared console script."""
    with PYPROJECT.open('rb') as f:
        data = tomllib.load(f)
    scripts = data.get('project', {}).get('scripts', {})
    out = []
    for name, target in scripts.items():
        module, _, attr = target.partition(':')
        out.append((name, module, attr))
    return out


def _in_contrib(module: str) -> bool:
    """Whether a module lies in the contrib zone."""
    return any(module == p or module.startswith(p + '.') for p in CONTRIB_PREFIXES)


def _container_targets() -> list[Any]:
    """Return a parameter per container entry point, zoned by its target.

    A case whose target is a contrib module carries the `contrib` marker, so
    `pytest tests/ -m "not contrib"` really does avoid importing contrib —
    otherwise the marker would promise a core-only run while still requiring
    fastapi and uvicorn to be installed.
    """
    out: list[Any] = []
    for path in sorted(DOCKER_DIR.glob('*')):
        if not path.is_file():
            continue
        for module, attr in UVICORN_TARGET.findall(path.read_text(encoding='utf-8')):
            marks = [pytest.mark.contrib] if _in_contrib(module) else []
            out.append(pytest.param(path.name, module, attr, marks=marks))
    return out


@pytest.mark.fast
@pytest.mark.parametrize('name,module,attr', _console_scripts())
def test_console_script_targets_resolve(name: str, module: str, attr: str) -> None:
    """Every `[project.scripts]` entry names an importable module and attribute."""
    mod = importlib.import_module(module)
    assert hasattr(mod, attr), (
        f'console script {name!r} points at {module}:{attr}, '
        f'but {module} has no attribute {attr!r}'
    )


@pytest.mark.fast
@pytest.mark.parametrize('source,module,attr', _container_targets())
def test_container_entry_points_resolve(source: str, module: str, attr: str) -> None:
    """Every container entry point names an importable module and attribute.

    A container whose CMD names a missing module cannot start, and nothing else
    in the suite would notice.
    """
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f'{source} runs "uvicorn {module}:{attr}", but {module} '
            f'does not exist ({exc}). The container would fail at startup.'
        )
    assert hasattr(mod, attr), (
        f'{source} runs "uvicorn {module}:{attr}", '
        f'but {module} has no attribute {attr!r}'
    )


@pytest.mark.fast
def test_entry_point_scan_is_not_vacuous() -> None:
    """Both scans must actually find targets, or the checks pass for free."""
    assert _console_scripts(), 'no console scripts discovered in pyproject.toml'
    assert _container_targets(), 'no container entry points discovered in docker/'
