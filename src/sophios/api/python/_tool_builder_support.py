"""Private support code for the public Tool Builder façade.

The public module deliberately keeps the visible API small. This helper module
holds the repetitive rendering, validation, and sanitization logic so the
main `tool_builder.py` file can stay focused on the user-facing surface.
"""

from dataclasses import dataclass
from pathlib import Path
import tempfile
import warnings
from typing import Any

from sophios import utils_cwl
from sophios.input_output import dump_wic_yaml


def _render(value: Any) -> Any:
    """Render helper objects to plain CWL/YAML-compatible values."""
    match value:
        case Path():
            return str(value)
        case list() | tuple():
            return [_render(item) for item in value]
        case dict():
            return {key: _render(item) for key, item in value.items()}
        case _ if hasattr(value, "to_dict") and callable(value.to_dict):
            return _render(value.to_dict())
        case _:
            return value


def _merge_if_set(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = _render(value)


def _render_doc(value: str | list[str] | None) -> str | list[str] | None:
    """A `doc` as CWL takes it: text, or lines of text. Any other value is left out."""
    match value:
        case str():
            return value
        case list():
            return [str(item) for item in value]
        case _:
            return None


def _contains_expression(value: Any) -> bool:
    """Whether a value holds a CWL expression, `$(...)` or `${...}`."""
    match value:
        case str():
            return "$(" in value or "${" in value
        case list() | tuple():
            return any(map(_contains_expression, value))
        case dict():
            return any(map(_contains_expression, value.values()))
        case _ if hasattr(value, "to_dict") and callable(value.to_dict):
            return _contains_expression(value.to_dict())
        case _:
            return False


def _warn_raw_escape_hatch(context: str) -> None:
    warnings.warn(
        (
            f"{context} is using raw CWL injection. Structured helpers are safer; "
            "this path is sanitized for common misuse but still bypasses most type-level guidance."
        ),
        UserWarning,
        stacklevel=3,
    )


def _sanitize_raw_mapping(mapping: dict[str, Any], *, context: str, allow_class_key: bool = False) -> dict[str, Any]:
    rendered = _render(mapping)
    if not isinstance(rendered, dict):
        raise TypeError(f"{context} must be a mapping")
    if not all(isinstance(key, str) and key for key in rendered):
        raise TypeError(f"{context} keys must be non-empty strings")
    directives = sorted(key for key in rendered if key.startswith("$"))
    if directives:
        raise ValueError(
            f"{context} does not accept '$'-prefixed keys, which SALAD reads as directives: {', '.join(directives)}"
        )
    if not allow_class_key and "class" in rendered:
        raise ValueError(f"{context} cannot set 'class' directly")
    return rendered


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of validating a generated CLT with cwltool/schema-salad."""

    path: Path
    uri: str
    process: Any


class ToolBuilderValidationError(ValueError):
    """Raised when a generated CLT fails schema validation."""


def validate_cwl_document(
    document: dict[str, Any],
    *,
    filename: str = "tool.cwl",
) -> ValidationResult:
    """Validate a generated CLT document through cwltool/schema-salad."""
    with tempfile.TemporaryDirectory(prefix="sophios-tool-builder-") as tmpdir:
        temp_path = Path(tmpdir) / filename
        temp_path.write_text(
            dump_wic_yaml(_render(document)),
            encoding="utf-8",
        )
        return _validate_path(temp_path)


def _validate_path(path: Path) -> ValidationResult:
    try:
        uri = path.absolute().as_uri()
        process = utils_cwl.make_tool(uri)
    except Exception as exc:  # pylint: disable=W0718:broad-exception-caught
        raise ToolBuilderValidationError(f"Generated CommandLineTool failed validation: {path}") from exc
    return ValidationResult(path=path, uri=uri, process=process)
