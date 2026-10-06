"""What a compile or a run needs from this machine, checked before the work starts.

Each check returns diagnostics rather than raising, so a caller reports every
problem at once, each on one line: what is wrong, then what to do.
"""
import os
from pathlib import Path

from .lang.diagnostics import Diagnostic, Severity
from .lang.error_codes import SophiosErrorCode


def unwritable(directory: Path, holds: str, remedy: str) -> Diagnostic | None:
    """A `wic021` error when this user cannot create `directory` or write into it, else None.

    The nearest part of the path that exists decides: a directory that does not
    exist yet is fine when it can be created.

    Args:
        directory (Path): Where Sophios is about to write.
        holds (str): What it writes there, for the message: 'the compiled workflow'.
        remedy (str): What to do instead: 'run Sophios from a directory you can write to'.

    Returns:
        Diagnostic | None: The error, or None when `directory` can be written.
    """
    target = directory.absolute()
    existing = next(path for path in (target, *target.parents) if path.exists())
    if existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
        return None
    why = 'is not writable by you' if existing.is_dir() else 'is a file'
    return Diagnostic(Severity.ERROR, SophiosErrorCode.DIRECTORY_NOT_WRITABLE,
                      f'Sophios writes {holds} to {target}, but {existing} {why}: {remedy}.')
