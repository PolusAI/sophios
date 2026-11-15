"""EDAM format helpers."""

from __future__ import annotations

from functools import cache
from importlib.resources import files
import json
from pathlib import PurePath
from typing import Any


EDAM_FORMAT_INDEX = "edam_format_index.json"
EDAM_FORMAT_URI_PREFIXES = (
    "http://edamontology.org/",
    "https://edamontology.org/",
)


def resolve_file_format(file_input: dict[str, Any], accepted_formats: list[Any]) -> str | None:
    """Return the accepted EDAM format matching a runtime CWL File object."""
    accepted: list[tuple[str, str]] = []
    for written in accepted_formats:
        curie = _edam_curie(written) if isinstance(written, str) else None
        if curie is not None:
            accepted.append((curie, written))
    if not accepted:
        return None
    if len(accepted) == 1:
        return accepted[0][1]
    accepted_by_curie = dict(accepted)
    for extension in _file_extensions(file_input):
        for candidate in _edam_format_index().get(extension, ()):
            if candidate in accepted_by_curie:
                return accepted_by_curie[candidate]
    return None


@cache
def _edam_format_index() -> dict[str, list[str]]:
    with files("sophios").joinpath(EDAM_FORMAT_INDEX).open(encoding="utf-8") as index_file:
        index: dict[str, list[str]] = json.load(index_file)["extension_to_formats"]
    return index


def _file_extensions(file_input: dict[str, Any]) -> list[str]:
    for key in ("basename", "location", "path"):
        name = file_input.get(key)
        if isinstance(name, str) and name:
            parts = PurePath(name).name.lower().split(".")
            return [".".join(parts[index:]) for index in range(1, len(parts)) if parts[index]]
    return []


def _edam_curie(format_value: str) -> str | None:
    if format_value.startswith("edam:format_"):
        return format_value
    for uri_prefix in EDAM_FORMAT_URI_PREFIXES:
        if format_value.startswith(f"{uri_prefix}format_"):
            return f"edam:{format_value.removeprefix(uri_prefix)}"
    return None
