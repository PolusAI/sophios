"""The `.wic` conformance corpus: real workflows the language must accept.

Shared by every syntax-layer test, so adding a corpus directory is one edit
rather than one per test module.

SCOPE. This collects only the `.wic` files inside this repository. The full
corpus the parser was developed against is larger — 82 files, most of them in
the sibling `mm-workflows` and `image-workflows` repositories — but those are
not on CI's path, so they cannot be a guard here. What this module returns is
the part that is actually enforced.
"""
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Directories searched for corpus files.
CORPUS_DIRS: Final = (REPO_ROOT / 'docs' / 'tutorials', REPO_ROOT / 'examples')

#: Every in-repository `.wic`, sorted so test ids are stable across runs.
CORPUS: Final = tuple(
    sorted(path for directory in CORPUS_DIRS if directory.is_dir() for path in directory.rglob('*.wic'))
)


def corpus_id(path: Path) -> str:
    """Name a corpus file in test output."""
    return path.name
