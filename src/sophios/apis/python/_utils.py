"""Internal helpers for the Python API."""

from pathlib import Path
from typing import Any

import yaml

from sophios import utils_cwl

from ._errors import InvalidInputValueError
from ._types import CWLAtomicType


def normalize_parameter_name(cwl_id: str) -> str:
    """Return the local parameter name from a CWL id."""
    return cwl_id.split("#")[-1]


def normalize_parameter_type(parameter_type: Any) -> tuple[Any, bool]:
    """Return the canonicalized parameter type and whether it is required."""
    if parameter_type is None:
        return None, True
    canonical = utils_cwl.canonicalize_type(parameter_type)
    match canonical:
        case list() as options:
            null_type = CWLAtomicType.NULL.value
            required = null_type not in options
            non_null_types = [entry for entry in options if entry != null_type]
            canonical = non_null_types[0] if len(non_null_types) == 1 else non_null_types
        case _:
            required = True
    return canonical, required


def is_array_type(parameter_type: Any) -> bool:
    """Return whether a normalized CWL type expression represents an array."""
    match parameter_type:
        case {"type": "array"}:
            return True
        case list() as options:
            return any(is_array_type(option) for option in options if option != CWLAtomicType.NULL.value)
        case _:
            return False


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


def infer_literal_parameter_type(value: Any) -> Any:
    """Infer a CWL type expression from a Python literal when practical."""
    match value:
        case None:
            return CWLAtomicType.NULL.value
        case bool():
            return CWLAtomicType.BOOLEAN.value
        case int():
            return CWLAtomicType.INT.value
        case float():
            return CWLAtomicType.FLOAT.value
        case str():
            return CWLAtomicType.STRING.value
        case Path() as path if path.exists():
            return CWLAtomicType.DIRECTORY.value if path.is_dir() else CWLAtomicType.FILE.value
        case Path() as path if path.suffix:
            return CWLAtomicType.FILE.value
        case Path():
            return CWLAtomicType.DIRECTORY.value
        case list() | tuple() as items:
            if not items:
                return None
            inferred_item_types = []
            for item in items:
                inferred = infer_literal_parameter_type(item)
                if inferred is None:
                    return None
                inferred_item_types.append(inferred)
            unique_types = []
            for inferred in inferred_item_types:
                if inferred not in unique_types:
                    unique_types.append(inferred)
            if len(unique_types) != 1:
                return None
            return {"type": "array", "items": unique_types[0]}
        case {"class": "File" | "Directory" as class_name}:
            return class_name
        case _:
            return None


def _validate_fs_object(path_value: Path, *, class_name: str) -> Path:
    if class_name == "Directory":
        if not path_value.is_dir():
            raise InvalidInputValueError(f"{str(path_value)} is not a directory")
        return path_value
    if class_name == "File":
        if not path_value.is_file():
            raise InvalidInputValueError(f"{str(path_value)} is not a file")
        return path_value
    raise InvalidInputValueError(f"Unsupported CWL object class {class_name!r}")


def get_value_from_cfg(value: Any) -> Any:
    """Normalize config values into Python values accepted by the DSL.

    This supports the common CWL input-object shapes users put in YAML config
    files, notably `File`, `Directory`, and arrays/records containing them.
    """
    match value:
        case list() as items:
            return [get_value_from_cfg(item) for item in items]
        case tuple() as items:
            return [get_value_from_cfg(item) for item in items]
        case dict() as data if data.get("class") in {"Directory", "File"}:
            try:
                path_text = data.get("location", data.get("path"))
                if path_text is None:
                    raise KeyError("location")
                path_value = Path(path_text)
            except Exception as exc:
                raise InvalidInputValueError() from exc
            return _validate_fs_object(path_value, class_name=str(data["class"]))
        case dict() as data:
            return {key: get_value_from_cfg(item) for key, item in data.items()}
        case _:
            return value


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_handle:
        loaded = yaml.safe_load(file_handle)
    return loaded or {}
