"""The conformance corpus: every workflow Sophios can reach.

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

_args = get_args()
_config = io.get_config(Path(_args.config_file), Path(_args.config_file))

#: Every reachable workflow, sorted for stable test ids. One namespace's stem
#: shadows another's in `get_yml_paths` exactly as it does during compilation,
#: so the corpus tests precisely the files Sophios itself would use.
CORPUS: Final = tuple(sorted(
    path
    for namespace in plugins.get_yml_paths(_config).values()
    for path in namespace.values()
))


def corpus_id(path: Path) -> str:
    """Name a corpus file in test output, disambiguating repeated basenames."""
    return f'{path.parent.name}/{path.name}'
