"""Private support code for the public CWL builder façade.

The public module deliberately keeps the visible API small. This helper module
holds the repetitive rendering, validation, and sanitization logic so the
main `cwl_builder.py` file can stay focused on the user-facing surface.
"""

from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
import warnings
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class _BuilderRules:
    """Immutable support namespace for CWL builder internals."""

    unset: object
    expression_markers: tuple[str, ...]
    known_namespaces: MappingProxyType[str, str]
    known_schemas: MappingProxyType[str, str]
    dangerous_raw_keys: frozenset[str]
    raw_class_name_pattern: str
    reserved_document_keys: frozenset[str]


_SUPPORT = _BuilderRules(
    unset=object(),
    expression_markers=("$(", "${"),
    known_namespaces=MappingProxyType(
        {
            "cwltool": "http://commonwl.org/cwltool#",
            "edam": "https://edamontology.org/",
        }
    ),
    known_schemas=MappingProxyType(
        {
            "edam": "https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl",
        }
    ),
    dangerous_raw_keys=frozenset({"$graph", "$import", "$include", "$mixin"}),
    raw_class_name_pattern=r"^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?$",
    reserved_document_keys=frozenset(
        {
            "$namespaces",
            "$schemas",
            "arguments",
            "baseCommand",
            "class",
            "cwlVersion",
            "doc",
            "hints",
            "id",
            "inputs",
            "intent",
            "label",
            "outputs",
            "permanentFailCodes",
            "requirements",
            "stderr",
            "stdin",
            "stdout",
            "successCodes",
            "temporaryFailCodes",
        }
    ),
)


def _render(value: Any) -> Any:
    """Render helper objects to plain CWL/YAML-compatible values."""
    match value:
        case Path() as path:
            return str(path)
        case list() as items:
            return [_render(item) for item in items]
        case tuple() as items:
            return [_render(item) for item in items]
        case dict() as mapping:
            return {key: _render(item) for key, item in mapping.items()}
        case _ if hasattr(value, "to_dict") and callable(value.to_dict):
            return _render(value.to_dict())
        case _:
            return value


def _merge_if_set(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = _render(value)


def _merge_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not _SUPPORT.unset:
        target[key] = _render(value)


def _render_doc(value: str | list[str] | None) -> str | list[str] | None:
    match value:
        case None:
            return None
        case str() as text:
            return text
        case list() as items:
            return [str(item) for item in items]


def _record_type_payload(
    fields: Any,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Build a CWL record schema payload from named or positional field specs."""
    field_defs = (
        [spec.named(field_name).to_dict() for field_name, spec in fields.items()]
        if isinstance(fields, dict)
        else [_render(field_spec) for field_spec in fields]
    )
    payload: dict[str, Any] = {"type": "record", "fields": field_defs}
    _merge_if_set(payload, "name", name)
    return payload


def _canonicalize_type(type_: Any) -> Any:
    rendered = _render(type_)
    match rendered:
        case str() as text if text.endswith("?"):
            return ["null", _canonicalize_type(text[:-1])]
        case str() as text if text.endswith("[]"):
            return {"type": "array", "items": _canonicalize_type(text[:-2])}
        case dict() as mapping if mapping.get("type") == "array" and "items" in mapping:
            return {**mapping, "items": _canonicalize_type(mapping["items"])}
        case _:
            return rendered


def _apply_required(type_: Any, required: bool) -> Any:
    if required:
        return _canonicalize_type(type_)
    canonical = _canonicalize_type(type_)
    if isinstance(canonical, list) and "null" in canonical:
        return canonical
    return ["null", canonical]


def _contains_expression(value: Any) -> bool:
    match value:
        case str() as text:
            return any(marker in text for marker in _SUPPORT.expression_markers)
        case list() as items:
            return any(_contains_expression(item) for item in items)
        case tuple() as items:
            return any(_contains_expression(item) for item in items)
        case dict() as mapping:
            return any(_contains_expression(item) for item in mapping.values())
        case _ if hasattr(value, "to_dict") and callable(value.to_dict):
            return _contains_expression(value.to_dict())
        case _:
            return False


def _input_expression(name: str) -> str:
    return f"$(inputs.{name})"


def _basename_expression(name: str) -> str:
    return f"$(inputs.{name}.basename)"


def _warn_raw_escape_hatch(context: str) -> None:
    warnings.warn(
        (
            f"{context} is using raw CWL injection. Structured helpers are safer; "
            "this path is sanitized for common misuse but still bypasses most type-level guidance."
        ),
        UserWarning,
        stacklevel=3,
    )


def _sanitize_raw_mapping(
    mapping: dict[str, Any],
    *,
    context: str,
    allow_class_key: bool = False,
    reserved_keys: set[str] | None = None,
) -> dict[str, Any]:
    rendered = _render(mapping)
    if not isinstance(rendered, dict):
        raise TypeError(f"{context} must be a mapping")
    if any(not isinstance(key, str) or not key for key in rendered):
        raise TypeError(f"{context} keys must be non-empty strings")
    blocked = sorted(key for key in rendered if key in _SUPPORT.dangerous_raw_keys)
    if blocked:
        raise ValueError(
            f"{context} does not accept SALAD document-assembly keys: {', '.join(blocked)}"
        )
    if reserved_keys is not None:
        collisions = sorted(key for key in rendered if key in reserved_keys)
        if collisions:
            raise ValueError(
                f"{context} cannot override builder-managed keys: {', '.join(collisions)}"
            )
    if not allow_class_key and "class" in rendered:
        raise ValueError(f"{context} cannot set 'class' directly")
    if any(key.startswith("$") for key in rendered):
        raise ValueError(f"{context} does not accept raw '$'-prefixed document keys")
    return rendered


def _named_parameter(reference: Any, *, kind: str) -> str:
    match reference:
        case str() as name:
            return name
        case _ if isinstance(getattr(reference, "name", None), str):
            return str(reference.name)
        case _:
            raise TypeError(f"{kind} reference must be a named Input/Output or a string")


def _optional_binding(binding: Any) -> Any:
    rendered = binding.to_dict()
    return binding if rendered else None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of validating a generated CLT with cwltool/schema-salad."""

    path: Path
    uri: str
    process: Any


class CWLBuilderValidationError(ValueError):
    """Raised when a generated CLT fails schema validation."""


def _import_cwltool_load_tool() -> Any:
    try:
        from cwltool import load_tool  # pylint: disable=import-outside-toplevel
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "cwltool/schema_salad is required to validate generated CommandLineTools"
        ) from exc
    return load_tool


def _import_cwltool_validation_support() -> tuple[Any, Any, Any]:
    try:
        from cwltool.context import RuntimeContext  # pylint: disable=import-outside-toplevel
        from cwltool.main import (  # pylint: disable=import-outside-toplevel
            get_default_args,
            setup_loadingContext,
        )
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "cwltool/schema_salad is required to validate generated CommandLineTools"
        ) from exc
    return RuntimeContext, get_default_args, setup_loadingContext


def _build_validation_loading_context(path: Path, *, skip_schemas: bool = False) -> Any:
    runtime_context_cls, get_default_args, setup_loading_context = _import_cwltool_validation_support()
    args_dict = get_default_args()
    args_dict.update(
        {
            "skip_schemas": skip_schemas,
            "validate": True,
            "workflow": str(path),
        }
    )
    args = Namespace(**args_dict)
    runtime_context = runtime_context_cls(args_dict)
    return setup_loading_context(None, runtime_context, args)


def validate_cwl_document(
    document: dict[str, Any],
    *,
    filename: str = "tool.cwl",
    skip_schemas: bool = False,
) -> ValidationResult:
    """Validate a generated CLT document through cwltool/schema-salad."""
    with tempfile.TemporaryDirectory(prefix="sophios-cwl-builder-") as tmpdir:
        temp_path = Path(tmpdir) / filename
        temp_path.write_text(
            yaml.safe_dump(_render(document), sort_keys=False, line_break="\n"),
            encoding="utf-8",
        )
        return _validate_path(temp_path, skip_schemas=skip_schemas)


def _validate_path(path: Path, *, skip_schemas: bool = False) -> ValidationResult:
    load_tool = _import_cwltool_load_tool()
    try:
        loading_context = _build_validation_loading_context(path, skip_schemas=skip_schemas)
        loading_context, workflowobj, uri = load_tool.fetch_document(str(path), loading_context)
        loading_context, uri = load_tool.resolve_and_validate_document(
            loading_context,
            workflowobj,
            uri,
            preprocess_only=False,
        )
        process = load_tool.make_tool(uri, loading_context)
    except Exception as exc:  # pylint: disable=W0718:broad-exception-caught
        raise CWLBuilderValidationError(f"Generated CommandLineTool failed validation: {path}") from exc
    return ValidationResult(path=path, uri=uri, process=process)


def _is_requirement_spec(value: Any) -> bool:
    return hasattr(value, "class_name") and callable(getattr(value, "to_fields", None))


def _normalize_requirement(
    requirement: Any,
    value: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    match requirement:
        case str() as class_name:
            if re.fullmatch(_SUPPORT.raw_class_name_pattern, class_name) is None:
                raise ValueError(f"invalid requirement class name {class_name!r}")
            payload = {} if value is None else _sanitize_raw_mapping(value, context=f"payload for {class_name}")
            return class_name, payload
        case _ if _is_requirement_spec(requirement):
            return str(requirement.class_name), requirement.to_fields()
        case dict() as payload:
            _warn_raw_escape_hatch("requirement()/hint()")
            payload_copy = _sanitize_raw_mapping(
                payload,
                context="raw requirement mapping",
                allow_class_key=True,
            )
            if "class" not in payload_copy:
                raise ValueError("raw requirement dicts must include a 'class' key")
            class_name = str(payload_copy.pop("class"))
            if re.fullmatch(_SUPPORT.raw_class_name_pattern, class_name) is None:
                raise ValueError(f"invalid requirement class name {class_name!r}")
            return class_name, payload_copy
        case _:
            raise TypeError("requirement must be a class name, requirement spec, or raw dict")
