"""Internal helpers for the Python API."""

import functools
import inspect
import keyword
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Final

import yaml

from sophios.lang.cwl import canonical_type, required
from sophios.lang.types import Atom
from sophios.lang.spans import SourceSpan

_PACKAGE_ROOT: Final = Path(__file__).resolve().parents[2]


def validate_python_identifier_name(name: str, *, context: str = "parameter name") -> str:
    """Validate that an API-facing name is a Python identifier."""
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"{context} {name!r} must be a valid Python identifier")
    return name


def is_reserved_name(name: str, owner: type) -> bool:
    """Whether attribute access on an ``owner`` cannot reach a port called ``name``: ``owner`` has an attribute of
    that name, or Python looks it up as a special name, such as ``__deepcopy__``."""
    return name in dir(owner) or (len(name) > 4 and name[:2] == name[-2:] == "__")


def normalize_parameter_type(parameter_type: Any) -> tuple[Any, bool]:
    """Return the canonicalized parameter type and whether it is required."""
    canonical, optional = required(canonical_type(parameter_type))
    return canonical, not optional


def contains_any_type(type_obj: Any) -> bool:
    """Return whether a CWL type expression includes the permissive ``Any`` type."""
    match type_obj:
        case Atom.ANY:
            return True
        case list() | tuple() as type_options:
            return any(contains_any_type(type_option) for type_option in type_options)
        case dict() as type_dict:
            return contains_any_type(type_dict.get("type"))
        case _:
            return False


def is_array_type(parameter_type: Any) -> bool:
    """Return whether a normalized CWL type expression represents an array."""
    match parameter_type:
        case {"type": "array"}:
            return True
        case list() as options:
            return any(is_array_type(option) for option in options if option != Atom.NULL.value)
        case _:
            return False


def serialize_value(value: Any) -> Any:
    """Convert Path objects into YAML-safe values while preserving structure."""
    match value:
        case Path():
            return str(value)
        case list() | tuple() as items:
            return [serialize_value(item) for item in items]
        case dict() as items:
            return {key: serialize_value(item) for key, item in items.items()}
        case _:
            return value


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict for an empty/null document."""
    with path.open("r", encoding="utf-8") as file_handle:
        loaded = yaml.safe_load(file_handle)
    return loaded or {}


@functools.lru_cache(maxsize=None)
def _user_file(code_filename: str) -> str | None:
    """The name a diagnostic gives `code_filename`, or None for a frame to skip.

    A name like `<stdin>`, `<string>` or `<ipython-input-3-abc>` is not a path
    and is kept as it is. Resolving a path costs a filesystem call, so the
    decision is made once per file.
    """
    if code_filename.startswith('<'):
        return code_filename
    path = Path(code_filename).resolve()
    if _PACKAGE_ROOT in path.parents or 'site-packages' in path.parts:
        return None
    return str(path)


#: The `.wic` position objects are being built from, while `Workflow.from_wic` builds them.
_AUTHORED: Final[ContextVar[SourceSpan | None]] = ContextVar('_AUTHORED', default=None)


@contextmanager
def authored_at(span: SourceSpan | None) -> Iterator[None]:
    """Make `caller_span` answer `span` while objects are built from that place in a `.wic` file."""
    token = _AUTHORED.set(span)
    try:
        yield
    finally:
        _AUTHORED.reset(token)


def caller_span() -> SourceSpan:
    """The user's own line: the innermost frame outside the `sophios` package.

    A workflow built in Python has no `.wic` text, but it has a file and a
    line where each step was made and each port bound, which is what a
    diagnostic names. Frames inside `sophios/` (the API, the compiler) are
    skipped; frames inside site-packages too, so a test harness or notebook
    wrapper does not become the location. Objects `Workflow.from_wic` builds
    carry the `.wic` position they were read from instead (`authored_at`).
    """
    authored = _AUTHORED.get()
    if authored is not None:
        return authored
    frame = inspect.currentframe()
    while frame is not None:
        file = _user_file(frame.f_code.co_filename)
        if file is not None:
            return SourceSpan(file, frame.f_lineno, 1, frame.f_lineno, 1)
        frame = frame.f_back
    return SourceSpan('<python>', 1, 1, 1, 1)
