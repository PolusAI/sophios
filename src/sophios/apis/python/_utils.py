"""Internal helpers for the Python API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sophios import utils_cwl

from ._errors import InvalidInputValueError


def default_dict() -> dict[str, Any]:
    return {}


def normalize_port_name(cwl_id: str) -> str:
    """Return the local port name from a CWL id."""
    return cwl_id.split("#")[-1]


def normalize_port_type(port_type: Any) -> tuple[Any, bool]:
    """Return the canonicalized port type and whether it is required."""
    canonical = utils_cwl.canonicalize_type(port_type)
    match canonical:
        case list() as options:
            required = "null" not in options
            non_null_types = [entry for entry in options if entry != "null"]
            canonical = non_null_types[0] if non_null_types else options[0]
        case _:
            required = True
    return canonical, required


def serialize_value(value: Any) -> Any:
    """Convert Path objects into YAML-safe values while preserving structure."""
    match value:
        case Path():
            return str(value)
        case list() as items:
            return [serialize_value(item) for item in items]
        case tuple() as items:
            return [serialize_value(item) for item in items]
        case dict() as items:
            return {key: serialize_value(item) for key, item in items.items()}
        case _:
            return value


def get_value_from_cfg(value: Any) -> Any:
    match value:
        case dict() as data if "Directory" in data.values():
            try:
                value_ = Path(data["location"])
            except Exception as exc:
                raise InvalidInputValueError() from exc
            if not value_.is_dir():
                raise InvalidInputValueError(f"{str(value_)} is not a directory")
            return value_
        case dict():
            return value
        case _:
            return value


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_handle:
        loaded = yaml.safe_load(file_handle)
    return loaded or {}
