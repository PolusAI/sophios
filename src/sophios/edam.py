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
    accepted = _accepted_edam_formats(accepted_formats)
    match accepted:
        case []:
            return None
        case [(curie, original)]:
            return original
        case _:
            candidates = _edam_format_index()
            for extension in _file_extensions(file_input):
                if extension in candidates:
                    accepted_by_curie = dict(accepted)
                    for candidate in candidates[extension]:
                        if candidate in accepted_by_curie:
                            return accepted_by_curie[candidate]
            return None


def _accepted_edam_formats(accepted_formats: list[Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for accepted_format in accepted_formats:
        match accepted_format:
            case str() as format_value:
                match _edam_curie(format_value):
                    case str() as curie:
                        result.append((curie, format_value))
            case _:
                pass
    return result


@cache
def _edam_format_index() -> dict[str, list[str]]:
    with files("sophios").joinpath(EDAM_FORMAT_INDEX).open(encoding="utf-8") as index_file:
        payload = json.load(index_file)
    match payload:
        case {"extension_to_formats": dict() as extension_to_formats}:
            return {
                str(extension): [str(format_value) for format_value in formats]
                for extension, formats in extension_to_formats.items()
                if isinstance(formats, list)
            }
        case _:
            return {}


def _file_extensions(file_input: dict[str, Any]) -> list[str]:
    match _file_name(file_input):
        case str() as file_name:
            parts = PurePath(file_name).name.lower().split(".")
            return [
                ".".join(parts[index:])
                for index in range(1, len(parts))
                if parts[index]
            ]
        case _:
            return []


def _file_name(file_input: dict[str, Any]) -> str | None:
    for key in ("basename", "location", "path"):
        match file_input.get(key):
            case str() as value if value:
                return value
            case _:
                pass
    return None


def _edam_curie(format_value: str) -> str | None:
    match format_value:
        case str() as value if value.startswith("edam:format_"):
            return value
        case str() as value:
            for uri_prefix in EDAM_FORMAT_URI_PREFIXES:
                if value.startswith(f"{uri_prefix}format_"):
                    return f"edam:{value.removeprefix(uri_prefix)}"
            return None
