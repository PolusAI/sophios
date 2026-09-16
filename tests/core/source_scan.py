"""Where the package lives, and how the static scans read it.

Eight test modules scan `src/sophios` for what the type checker cannot see — a
version literal, a `sys.exit`, a set iteration reaching the output, a contrib
import — and each had spelled the repo root, the file list and the empty-list
guard for itself.

Imports `ast` and `pathlib` only, which is a constraint: two consumers are
oracle modules whose transitive imports are walked, so anything reaching
`test_setup`, `compile_harness` or `wic_corpus` from here would make the oracle
environment-dependent through the back door.
"""
import ast
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO_ROOT / 'src' / 'sophios'


def package_files(*, exclude: Path | None = None) -> list[Path]:
    """Every module in the package, sorted, optionally without one.

    `exclude` is for a scan whose rule does not apply to the file that owns it —
    `lang/cwl.py` is allowed to contain the CWL version literal that
    `test_cwl_version` forbids everywhere else.
    """
    return sorted(path for path in SRC.rglob('*.py') if path != exclude)


def parsed(path: Path) -> ast.Module:
    """The module at `path`, parsed, with the filename kept for diagnostics."""
    return ast.parse(path.read_text(encoding='utf-8'), str(path))


def not_vacuous(files: list[Path], what: str) -> None:
    """Fail when a scan found nothing to scan.

    A static scan over an empty file list passes, which is the one result that
    means nothing at all. Every scan calls this so a broken path expression
    fails loudly rather than reporting success over zero files.
    """
    assert files, f'no {what} discovered; the scan is vacuous'
