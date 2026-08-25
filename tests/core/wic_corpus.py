"""The Sophios conformance corpus: every workflow the compiler can reach

There is no discovery mechanism here — Sophios already has one. The config's
`search_paths_wic` defines where workflows live, `plugins.get_yml_paths` finds
them (excluding generated `*_inputs*` files), and CI provisions the config to
reach `docs/tutorials`, `mm-workflows/examples`, and
`image-workflows/workflows` (`.github/update_sophios_config.py`). The corpus
is exactly that reachable set — the same set §3 of the design promises still
parses — so the corpus and the compiler can never disagree about what exists.

Whatever the corpus repositories' `main` contains, those are the tests, for
better or worse. Locally you test what your config reaches; CI reaches
everything and is the arbiter.
"""
from pathlib import Path
from typing import Final

from sophios import input_output as io
from sophios import plugins
from sophios.cli import get_args

#: In-repo fallback when no config exists: a pure read, so collecting the
#: suite on a fresh machine provisions nothing (`pytest --collect-only` used
#: to write ~/wic/global_config.json and copy adapters as a side effect —
#: the #383 review's P8).
_IN_REPO_DIRS: Final = (
    Path(__file__).resolve().parents[2] / 'docs' / 'tutorials',
    Path(__file__).resolve().parents[2] / 'examples',
)


def _discover() -> tuple[Path, ...]:
    """Every reachable workflow, without side effects.

    When a config exists, discovery is the compiler's own (`get_yml_paths`
    reads `search_paths_wic`), so the corpus and the compiler cannot disagree.
    When none exists — a fresh checkout — the in-repo directories are read
    directly rather than provisioning the user's home to ask the config; CI
    always provisions a config first and remains the arbiter of full coverage.
    """
    config_path = Path(get_args().config_file)
    if config_path.exists():
        config = io.get_config(config_path, config_path)  # read-only: the file exists
        return tuple(sorted(
            path
            for namespace in plugins.get_yml_paths(config).values()
            for path in namespace.values()
        ))
    return tuple(sorted(
        path for directory in _IN_REPO_DIRS if directory.is_dir()
        for path in directory.rglob('*.wic')
    ))


#: Sorted for stable test ids. One namespace's stem shadows another's in
#: `get_yml_paths` exactly as it does during compilation, so the corpus tests
#: precisely the files Sophios itself would use.
CORPUS: Final = _discover()


def corpus_id(path: Path) -> str:
    """Name a corpus file in test output, disambiguating repeated basenames."""
    return f'{path.parent.name}/{path.name}'
