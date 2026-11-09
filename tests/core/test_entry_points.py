"""Declared entry points must resolve to real modules and attributes: nothing type-checks the strings, so a rename
silently invalidates them (it has happened twice, in the REST image's CMD)."""
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

CONTRIB_PREFIXES = ('sophios.contrib',)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / 'docker'
PYPROJECT = REPO_ROOT / 'pyproject.toml'

# "uvicorn some.module:attr" in a Dockerfile CMD or compose command.
UVICORN_TARGET = re.compile(r'uvicorn"?,?\s*"?([\w.]+):(\w+)"?')


def _console_scripts() -> list[Any]:
    """A parameter per declared console script: `[project.scripts]` in pyproject.toml."""
    with PYPROJECT.open('rb') as f:
        scripts = tomllib.load(f).get('project', {}).get('scripts', {})
    return [pytest.param(f'console script {name!r}', *target.split(':', 1)) for name, target in scripts.items()]


def _in_contrib(module: str) -> bool:
    """Whether a module lies in the contrib zone."""
    return any(module == p or module.startswith(p + '.') for p in CONTRIB_PREFIXES)


def _container_targets() -> list[Any]:
    """A parameter per container entry point, zoned by its target; a contrib target carries `contrib`, so `-m "not
    contrib"` imports no fastapi."""
    out: list[Any] = []
    for path in sorted(DOCKER_DIR.glob('*')):
        if not path.is_file():
            continue
        for module, attr in UVICORN_TARGET.findall(path.read_text(encoding='utf-8')):
            marks = [pytest.mark.contrib] if _in_contrib(module) else []
            out.append(pytest.param(f'{path.name} runs "uvicorn {module}:{attr}"', module, attr, marks=marks))
    return out


@pytest.mark.fast
@pytest.mark.parametrize('declared,module,attr', _console_scripts() + _container_targets())
def test_each_entry_point_names_an_importable_module_and_attribute(declared: str, module: str, attr: str) -> None:
    """Every `[project.scripts]` entry and every container entry point names an importable module and attribute.

    A container whose CMD names a missing module cannot start, and nothing else
    in the suite would notice.
    """
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        pytest.fail(f'{declared}, but {module} does not exist ({exc}): it would fail at startup.')
    assert hasattr(mod, attr), f'{declared}, but {module} has no attribute {attr!r}'


@pytest.mark.fast
def test_entry_point_scan_is_not_vacuous() -> None:
    """Both scans must actually find targets, or the checks pass for free."""
    assert _console_scripts(), 'no console scripts discovered in pyproject.toml'
    assert _container_targets(), 'no container entry points discovered in docker/'
