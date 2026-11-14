import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

import sophios
import sophios.cli
import sophios.input_output as io
import sophios.plugins
import sophios.api
import sophios.api.python.workflow
from sophios.wic_types import Json, Tools


@dataclass(frozen=True, slots=True)
class CorpusRegistry:
    """The explicit environment-dependent inputs used by corpus tests."""

    tools: Tools
    workflows: dict[str, dict[str, Path]]


def corpus_config() -> Json:
    """The configuration Sophios itself would use: the one named or found, generated first when there is none."""
    args = sophios.cli.get_args()
    return io.get_config(args.config_file, Path(args.homedir))


@lru_cache(maxsize=1)
def workflow_paths() -> dict[str, dict[str, Path]]:
    """Discover workflow source paths without loading any process plugins."""
    return sophios.plugins.get_yml_paths(corpus_config())


@lru_cache(maxsize=1)
def load_test_registry() -> CorpusRegistry:
    """The corpus registry, discovered on first use and never at collection, so importing this module writes nothing."""
    return CorpusRegistry(sophios.plugins.get_tools_cwl(corpus_config()), workflow_paths())


@pytest.fixture(scope='session')
def corpus_registry() -> CorpusRegistry:
    """Explicit registry fixture for tests that exercise the discovered corpus."""
    return load_test_registry()


def home_with_shipped_tools(home: Path, repo_root: Path) -> Path:
    """`home`, given this checkout's tools under `wic/cwl_adapters`, where `sophios --generate_config` puts them and
    the documented examples name them (`Path.home() / 'wic' / 'cwl_adapters' / <tool>`)."""
    shutil.copytree(repo_root / 'cwl_adapters', home / 'wic' / 'cwl_adapters')
    return home


def use_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make `home` what `Path.home()` returns, on POSIX (`HOME`) and on Windows (`USERPROFILE`)."""
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
