"""Lower a resolved Sophios compilation into the Nextflow representation."""

from collections.abc import Callable, Iterable, Iterator, Mapping
import copy
from dataclasses import dataclass
import math
from os import PathLike
from pathlib import Path
import posixpath
import re
from types import MappingProxyType
from typing import Any, Final

from .ir.complete import coerce_job_value
from .ir.resolved import (
    Construct, Crossing, InputRef, NotLowered, OutputRef, Refusal, Requirement, ResolvedStep, ResolvedTool,
    ResolvedWorkflow, StepInput, Through, ToolStep, WorkflowInput, WorkflowOutput, output_sources, output_unbound,
    tool_steps,
)
from .ir.types import AuthoredName, PortDeclaration, PortType
from .lang.diagnostics import Diagnostic, Locator, Severity
from .lang.error_codes import SophiosErrorCode
from .lang.subset import (
    Expr, check as check_safe_subset, is_safe_subset_text, references,
)
from .nf_support import FIELDS, REQUIREMENT_CLASSES, Row, State, consumed
from .nf_symbols import normalize_nextflow_identifier
from .nf_types import (
    ExecutableNextflowWorkflow,
    GLOB_WILDCARDS,
    LINK_MERGES,
    PICK_VALUES,
    NF_INTERNAL_IDENTIFIERS,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfConnection,
    NfEntry,
    NfEnvVar,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfOptionalBinding,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfShellLiteral,
    NfStepInputConnection,
    NfTemplate,
    NfTemplateSegment,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
    link_refusal,
    linked_reference,
)
from .utils_cwl import desugar_into_canonical_normal_form


# Runtime-proven representation of an absent optional val input. Channel.value(None)
# never binds under the pinned Nextflow runtime (the consuming process never starts
# and the run hangs), so a literal JSON null cannot be used. [] is Groovy-falsy like
# null and false, and is unambiguous here because no supported scalar value is ever
# legitimately an array.
ABSENT_VAL_SENTINEL: list[Any] = []


def _identifier(value: Any, *, context: str, variable: bool = True) -> str:
    """Normalize a CWL identifier into a stable Nextflow identifier (see normalize_nextflow_identifier)."""
    match value:
        case str() if value:
            pass
        case _:
            raise ValueError(f"{context} must be a non-empty string")
    local = value.rsplit("#", maxsplit=1)[-1]
    if not local:
        raise ValueError(f"{context} cannot be normalized to a Nextflow identifier")
    identifier = normalize_nextflow_identifier(local, variable=variable)
    while identifier in NF_INTERNAL_IDENTIFIERS:
        identifier = f"_{identifier}"
    return identifier


def _normalized_identifiers(values: Iterable[Any], *, context: str, variable: bool = True) -> dict[Any, str]:
    """Normalize source identifiers and reject every many-to-one mapping."""
    normalized_by_source: dict[Any, str] = {}
    sources_by_normalized: dict[str, list[Any]] = {}
    for source in values:
        normalized = _identifier(source, context=f"{context} identifier", variable=variable)
        normalized_by_source[source] = normalized
        sources_by_normalized.setdefault(normalized, []).append(source)

    collisions = [
        (normalized, sorted(sources, key=str))
        for normalized, sources in sources_by_normalized.items()
        if len(sources) > 1
    ]
    if collisions:
        details = "; ".join(
            f"{', '.join(repr(source) for source in sources)} normalize to {normalized!r}"
            for normalized, sources in sorted(collisions)
        )
        raise ValueError(f"{context} identifiers {details}")
    return normalized_by_source


def _identifier_normalization_findings(
    values: Iterable[Any],
    *,
    context: str,
    path: str,
    variable: bool = True,
) -> list[str]:
    """Return a capability finding when source names collapse during normalization."""
    try:
        _normalized_identifiers(values, context=context, variable=variable)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    return []


def _as_mapping(value: Any, *, error: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(error)


def _as_list(value: Any, *, error: str) -> list[Any]:
    if isinstance(value, list):
        return value
    raise ValueError(error)


def _is_optional(cwl_type: Any) -> bool:
    match cwl_type:
        case str() as type_name:
            return type_name.endswith("?")
        case list() as union:
            return "null" in union
        case _:
            return False


def _required_type(cwl_type: Any) -> Any:
    """``cwl_type`` without its null, an array written ``T[]`` read as CWL reads it: ``{type: array, items: T}``."""
    match cwl_type:
        case str() as type_name if type_name.endswith("?"):
            return _required_type(type_name[:-1])
        case str() as type_name if type_name.endswith("[]"):
            return {"type": "array", "items": type_name[:-2]}
        case list() as union if "null" in union:
            remaining = [item for item in union if item != "null"]
            if len(remaining) != 1:
                raise ValueError(f"unsupported optional CWL union {cwl_type!r}")
            return _required_type(remaining[0])
        case _:
            return cwl_type


def cwl_type_to_nf_qualifier(cwl_type: Any) -> str:
    """Map the documented CWL type subset to a Nextflow qualifier.

    Args:
        cwl_type (Any): A CWL type expression; optional forms (``"File?"``,
            ``["null", ...]``) map like their required type. Array types are
            never accepted here; callers unwrap them with
            :func:`_array_item_type` first.

    Raises:
        ValueError: If the type is outside the supported subset.

    Returns:
        str: ``"path"`` for File/Directory, ``"val"`` for supported scalars.
    """
    match _required_type(cwl_type):
        case "File" | "Directory":
            return "path"
        case "string" | "int" | "float" | "boolean":
            return "val"
        case _:
            raise ValueError(f"unsupported CWL type for the Nextflow target: {cwl_type!r}")


# The safe subset's number rules (design §6, Expressions) cover int and float only.
_UNSUPPORTED_NUMBER_TYPES = frozenset({"long", "double"})


def _type_names(cwl_type: Any) -> set[str]:
    """Every type name in a CWL type, or in a parameter that declares one, optional and array forms included."""
    match cwl_type:
        case str():
            return {cwl_type.removesuffix("?").removesuffix("[]")}
        case list():
            return set().union(*map(_type_names, cwl_type))
        case Mapping():
            return _type_names(cwl_type.get("type")) | _type_names(cwl_type.get("items"))
    return set()


def _number_type_findings(definition: Any, *, path: str) -> list[str]:
    """Name each long or double a declared type uses."""
    return [
        f"{path}.type: {name} is not supported: the Nextflow target lowers int and float; declare one of those"
        for name in sorted(_type_names(definition) & _UNSUPPORTED_NUMBER_TYPES)
    ]


_PATH_KINDS = {"File": "file", "Directory": "directory"}


def _is_array_type(cwl_type: Any) -> bool:
    """Return whether a (non-optional-wrapped) CWL type is the array mapping form."""
    return isinstance(cwl_type, Mapping) and cwl_type.get("type") == "array"


def _array_item_type(array_type: Any) -> Any:
    """Return the supported scalar item type for an array type mapping.

    Only a bare scalar type name, or a single-key ``{"type": <scalar>}``
    mapping, is representable. Nested arrays and a per-item ``inputBinding``
    inside ``items`` are explicitly deferred rather than silently mishandled.
    The array schema's own fields, a per-item ``inputBinding`` beside
    ``items`` among them, are checked against the support table by
    :func:`_parameter_findings`.
    """
    items = array_type.get("items") if isinstance(array_type, Mapping) else None
    match items:
        case "array":
            raise ValueError("nested arrays are deferred beyond this lowering")
        case Mapping() as mapping:
            if mapping.get("type") == "array":
                raise ValueError("nested arrays are deferred beyond this lowering")
            if "inputBinding" in mapping:
                raise ValueError("per-item array element bindings are deferred beyond this lowering")
            extra = set(mapping) - {"type"}
            if extra:
                raise ValueError(f"unsupported array items fields: {', '.join(sorted(extra))}")
            return mapping.get("type")
        case _:
            return items


def _ports(
    raw_ports: Any,
    *,
    outputs: bool,
    entries: Mapping[str, NfEntry] | None = None,
) -> list[NfPort]:
    port_definitions = _as_mapping(raw_ports, error="CommandLineTool ports must be a mapping")
    ports: list[NfPort] = []
    for raw_name, raw_definition in port_definitions.items():
        match raw_definition:
            case {"type": cwl_type}:
                name = _identifier(raw_name, context="port name")
                required_type = _required_type(cwl_type)
                is_array = not outputs and _is_array_type(required_type)
                element_type = _array_item_type(required_type) if is_array else cwl_type
                qualifier = cwl_type_to_nf_qualifier(element_type)
                path_kind = _PATH_KINDS.get(_required_type(element_type))
                ports.append(
                    NfPort(
                        name,
                        qualifier,
                        emit=name if outputs else None,
                        glob=_output_template(raw_name, raw_definition) if outputs else None,
                        path_kind=path_kind,
                        is_array=is_array,
                        entry=None if outputs or entries is None else entries.get(name),
                        capture=_output_capture(raw_name, raw_definition) if outputs else None,
                        optional=outputs and qualifier == "path" and _is_optional(cwl_type),
                    )
                )
            case _:
                raise ValueError(f"CWL port {raw_name!r} must declare a type")
    return ports


_INPUT_EXPRESSION = re.compile(
    r"\$\(\s*inputs\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<suffix>path|basename))?\s*\)"
)
# One token of a string CWL interpolates: an escape it rewrites (`\$(`, `\${`, `\\`), or a reference.
_INTERPOLATION = re.compile(rf"\\(?P<escaped>\$[({{]|\\)|{_INPUT_EXPRESSION.pattern}")
_EXPRESSION_START = re.compile(r"\$[({]")


def _template(value: Any, *, context: str) -> NfTemplate:
    match value:
        case bool() as boolean:
            text = "true" if boolean else "false"
        case (int() | float()) as number:
            text = str(number)
        case str() as text:
            pass
        case _:
            raise ValueError(f"unsupported CWL command value {value!r}")
    interpolated = "$(" in text or "${" in text  # cwltool interpolates only these, and strips them
    if interpolated:
        text = text.strip()
    segments: list[NfTemplateSegment] = []
    offset = 0
    for match in _INTERPOLATION.finditer(text) if interpolated else ():
        segments.append(NfLiteral(text[offset:match.start()]))
        if match["escaped"]:
            segments.append(NfLiteral(match["escaped"]))
        else:
            name = _identifier(match["name"], context="input reference")
            segments.append(NfBasenameReference(name) if match["suffix"] == "basename" else NfInputReference(name))
        offset = match.end()
    segments.append(NfLiteral(text[offset:]))
    # A `$` not followed by `(` or `{` is plain text, as in CWL. A space stands in for each token, so the text on
    # its two sides cannot join into an expression's start: `{$$(inputs.x){$` is `{$`, a reference and `{$`.
    if interpolated and _EXPRESSION_START.search(_INTERPOLATION.sub(" ", text)):
        raise ValueError(
            f"{context} contains an unsupported CWL expression; the supported forms are $(inputs.<name>) "
            "and, on a File or Directory input, $(inputs.<name>.path) and $(inputs.<name>.basename)"
        )
    return NfTemplate(tuple(segments))


def _position(value: Any, *, default: int) -> int:
    match value:
        case None:
            return default
        case bool():
            raise ValueError(f"unsupported non-integer CWL command position {value!r}")
        case int() as position:
            return position
        case str() as position:
            try:
                return int(position)
            except ValueError as exc:
                raise ValueError(f"unsupported non-integer CWL command position {value!r}") from exc
        case _:
            raise ValueError(f"unsupported CWL command position {value!r}")


def _binding_tokens(prefix: Any, value: NfTemplate, *, separate: Any = True) -> tuple[NfTemplate, ...]:
    match prefix:
        case None:
            if not separate:
                # cwltool raises for separate without prefix, type-independently.
                raise ValueError("CWL separate cannot be specified without a prefix")
            return (value,)
        case str() as text:
            prefix_template = NfTemplate((NfLiteral(text),))
            if separate is not False:
                return prefix_template, value
            return (NfTemplate((*prefix_template.segments, *value.segments)),)
        case _:
            raise ValueError("CWL command prefix must be a string")


def _shell_literal_value(
    raw_name: Any,
    binding: Mapping[str, Any],
    *,
    shell_mode: bool,
) -> NfShellLiteral:
    """Lower one shellQuote:false binding to its one supported shape.

    Approved only under ShellCommandRequirement, for a prefix-free binding
    whose valueFrom is a CWL-author literal with no input reference: the
    input's own runtime value must never be rendered unquoted, so a binding
    with no valueFrom, a prefix, or an input-referencing valueFrom is
    rejected here rather than silently losing its quoting. A `$` is rejected
    too: no helper quotes it, and the task's shell is not CWL's.
    """
    if not shell_mode:
        raise ValueError(
            f"shellQuote false for {raw_name!r} requires ShellCommandRequirement "
            "in requirements or hints"
        )
    if binding.get("prefix") is not None:
        raise ValueError(
            f"shellQuote false for {raw_name!r} does not support a prefix; "
            "the raw literal must be the binding's whole value"
        )
    if binding.get("separate") is False:
        raise ValueError("CWL separate cannot be specified without a prefix")
    value_from = binding.get("valueFrom")
    if value_from is None:
        raise ValueError(
            f"shellQuote false for {raw_name!r} has no valueFrom; a plain "
            "input value is never rendered unquoted"
        )
    template = _template(value_from, context=f"CWL shellQuote false value for {raw_name!r}")
    literal_parts: list[str] = []
    for segment in template.segments:
        if not isinstance(segment, NfLiteral):
            raise ValueError(
                f"shellQuote false for {raw_name!r} references an input; only a "
                "CWL-author literal with no input reference may be rendered unquoted"
            )
        literal_parts.append(segment.value)
    text = "".join(literal_parts)
    if "$" in text:
        raise ValueError(
            f"shellQuote false for {raw_name!r} holds a '$', which the task's shell would expand "
            "in its own environment, not CWL's"
        )
    return NfShellLiteral(text)


def _boolean_value_from(value_from: Any, tool: Mapping[str, Any]) -> str | None:
    """The boolean input a valueFrom restates whole as ``$(inputs.<name>)``, else None."""
    match = _INPUT_EXPRESSION.fullmatch(value_from.strip()) if isinstance(value_from, str) else None
    if match is None or match["suffix"]:
        return None
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    definition = inputs.get(match["name"])
    if isinstance(definition, Mapping) and _required_type(definition.get("type")) == "boolean":
        return _identifier(match["name"], context="input reference")
    return None


def _flag_tokens(name: str, prefix: Any) -> tuple[NfCommandToken, ...]:
    """CWL's boolean binding: the prefix when true, nothing when false or without a prefix."""
    return () if prefix is None else (NfFlag(name, prefix),)


def _argument_items(
    arguments: list[Any],
    *,
    shell_mode: bool,
    tool: Mapping[str, Any],
) -> list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]]:
    items: list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]] = []
    for index, argument in enumerate(arguments):
        match argument:
            case {"valueFrom": value_from}:
                item_position = _position(argument.get("position"), default=0)
                if argument.get("shellQuote") is False:
                    tokens: tuple[NfCommandToken, ...] = (
                        _shell_literal_value(f"arguments[{index}]", argument, shell_mode=shell_mode),
                    )
                elif flag := _boolean_value_from(value_from, tool):
                    tokens = _flag_tokens(flag, argument.get("prefix"))
                else:
                    value = _template(value_from, context="CWL argument valueFrom")
                    tokens = _binding_tokens(
                        argument.get("prefix"), value, separate=argument.get("separate", True)
                    )
            case Mapping():
                raise ValueError("mapped CWL arguments must contain valueFrom")
            case _:
                tokens = (_template(argument, context="CWL argument"),)
                item_position = 0
        items.append(((item_position, 0, index), tokens))
    return items


def _boolean_flag_reference(raw_name: Any, name: str, value_from: Any) -> None:
    """Validate that a boolean binding's valueFrom is a bare self-reference.

    CWL evaluates valueFrom and then applies boolean flag semantics to the
    result. The only valueFrom shape provably boolean without a JS evaluator
    is a bare $(inputs.<name>) reference restating the binding's own input,
    so that is the sole supported form; it renders identically to the
    no-valueFrom case.
    """
    template = _template(value_from, context=f"CWL input {raw_name!r} valueFrom")
    match template.segments:
        case (NfInputReference(name=reference),) if reference == name:
            return
        case _:
            raise ValueError(
                "valueFrom on a boolean inputBinding is supported only as "
                f"$(inputs.{raw_name}), restating the input's own value; "
                f"{value_from!r} does not have that shape"
            )


def _array_binding(
    raw_name: Any,
    name: str,
    required_type: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> NfArrayBinding:
    """Lower an array-typed inputBinding to its one supported shape.

    No itemSeparator, no valueFrom, and separate is not false: the array's
    optional prefix is contributed once and each element becomes its own
    argv word, or nothing at all when the array is empty.
    """
    if binding.get("valueFrom") is not None:
        raise ValueError(
            f"CWL valueFrom on an array-typed inputBinding for {raw_name!r} is "
            "deferred beyond this lowering"
        )
    if binding.get("itemSeparator") is not None:
        raise ValueError(f"CWL itemSeparator for {raw_name!r} is deferred beyond this lowering")
    if binding.get("separate") is False:
        raise ValueError(
            f"CWL separate: false on an array-typed inputBinding for {raw_name!r} "
            "is deferred beyond this lowering"
        )
    # Validates the item type is a supported, non-nested, no-per-item-binding
    # scalar; the qualifier itself is not needed here.
    cwl_type_to_nf_qualifier(_array_item_type(required_type))
    match binding.get("prefix"):
        case None:
            prefix = None
        case str() as prefix_text if prefix_text.strip():
            prefix = prefix_text
        case _:
            raise ValueError(f"CWL command prefix for {raw_name!r} must be a non-empty string")
    return NfArrayBinding(name, prefix)


_OPTIONAL_BINDING_TYPES = frozenset({"string", "int", "float"})


def _optional_binding(raw_name: Any, name: str, binding: Mapping[str, Any]) -> NfOptionalBinding:
    """Lower an optional string, int or float input's own binding: nothing when the input is absent."""
    separate = binding.get("separate", True)
    match binding.get("prefix"):
        case None:
            if separate is False:
                raise ValueError("CWL separate cannot be specified without a prefix")
            return NfOptionalBinding(name)
        case str() as prefix if prefix.strip():
            return NfOptionalBinding(name, prefix, separate is not False)
        case _:
            raise ValueError(f"CWL command prefix for {raw_name!r} must be a non-empty string")


_IWDR_ENTRY_PATTERN = re.compile(r"^\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\)$")


def _iwdr_entry_reference(value: Any) -> str:
    """Return the raw input name referenced by a bare $(inputs.<name>) IWDR entry."""
    if isinstance(value, str):
        match = _IWDR_ENTRY_PATTERN.match(value.strip())
        if match:
            return match.group(1)
    raise ValueError(
        "must be a bare $(inputs.<name>) reference to one File/Directory input; "
        "inline content construction is deferred"
    )


def _iwdr_entryname(value: Any, *, raw_name: str) -> str | None:
    """An entry's literal name, or None for the input's own basename.

    Absent, or the exact self-referencing $(inputs.<name>.basename), both
    name the entry after the input. A plain literal with no reference is the
    one supported rename shape; NfEntry validates it.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("entryname must be a string")
    template = _template(value, context="IWDR entryname")
    if template.segments == (NfBasenameReference(raw_name),):
        return None
    literal_parts: list[str] = []
    for segment in template.segments:
        if not isinstance(segment, NfLiteral):
            raise ValueError(
                "entryname is supported only as a literal string or the input's own "
                f"$(inputs.{raw_name}.basename); a computed or differently-referencing "
                "entryname is not supported"
            )
        literal_parts.append(segment.value)
    return "".join(literal_parts)


def _iwdr_listing_item(item: Any, *, tool_inputs: Mapping[str, Any]) -> tuple[str, NfEntry]:
    """Validate one InitialWorkDirRequirement listing entry.

    Returns the normalized input name and its entry. Approved shapes: a bare
    $(inputs.<name>) string, or a Dirent {entry: $(inputs.<name>), entryname:
    ..., writable: ...} whose entryname is absent, the input's own basename
    self-reference, or a literal with no path separator, and whose writable
    is a literal boolean.
    """
    match item:
        case str() as bare:
            raw_name = _iwdr_entry_reference(bare)
            rename, writable = None, False
        case Mapping() as dirent:
            writable = dirent.get("writable", False)
            if not isinstance(writable, bool):
                raise ValueError("writable must be a literal boolean")
            raw_name = _iwdr_entry_reference(dirent.get("entry"))
            rename = _iwdr_entryname(dirent.get("entryname"), raw_name=raw_name)
        case _:
            raise ValueError("must be a bare $(inputs.<name>) reference or a Dirent mapping")
    definition = tool_inputs.get(raw_name)
    if not isinstance(definition, Mapping):
        raise ValueError(f"references undeclared input {raw_name!r}")
    required = _required_type(definition.get("type"))
    if _is_array_type(required):
        raise ValueError(
            f"references array-typed input {raw_name!r}; staging an array of files is not supported"
        )
    try:
        qualifier = cwl_type_to_nf_qualifier(required)
    except ValueError:
        qualifier = "unsupported"
    if qualifier != "path":
        raise ValueError(
            f"references non-path input {raw_name!r}; only File/Directory inputs can be staged"
        )
    return _identifier(raw_name, context="IWDR listing target"), NfEntry(rename, writable)


def _iwdr_listing_findings(
    tool: Mapping[str, Any],
    requirement: Mapping[str, Any],
    *,
    path: str,
) -> list[str]:
    """Validate every InitialWorkDirRequirement listing entry independently."""
    listing = requirement.get("listing")
    if not isinstance(listing, list):
        return [f"{path}.listing: InitialWorkDirRequirement listing must be a list"]
    tool_inputs = tool.get("inputs", {})
    if not isinstance(tool_inputs, Mapping):
        return []
    findings: list[str] = []
    renamed_by: dict[str, set[str]] = {}
    listed_at: dict[str, int] = {}
    for index, item in enumerate(listing):
        if isinstance(item, Mapping) and (
            dirent_findings := _unconsumed_field_findings(item, "Dirent", path=f"{path}.listing[{index}]")
        ):
            findings.extend(dirent_findings)
            continue
        try:
            name, entry = _iwdr_listing_item(item, tool_inputs=tool_inputs)
        except ValueError as exc:
            findings.append(f"{path}.listing[{index}]: {exc}")
            continue
        if name in listed_at:
            findings.append(
                f"{path}.listing[{index}]: input {name!r} appears more than once "
                f"(first listed at {path}.listing[{listed_at[name]}])"
            )
            continue
        listed_at[name] = index
        if entry.name is not None:
            renamed_by.setdefault(entry.name, set()).add(name)
    for rename, names in renamed_by.items():
        if len(names) > 1:
            findings.append(
                f"{path}.listing: inputs {sorted(names)} are all staged under the same "
                f"literal name {rename!r}"
            )
    return findings


def _iwdr_entries(tool: Mapping[str, Any]) -> dict[str, NfEntry]:
    """Each input an InitialWorkDirRequirement listing names, with its entry; the task makes every one."""
    requirement = _requirement(tool, "InitialWorkDirRequirement")
    if requirement is None:
        return {}
    listing = requirement.get("listing")
    if not isinstance(listing, list):
        raise ValueError("InitialWorkDirRequirement listing must be a list")
    tool_inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    entries: dict[str, NfEntry] = {}
    for item in listing:
        name, entry = _iwdr_listing_item(item, tool_inputs=tool_inputs)
        if name in entries:
            raise ValueError(f"input {name!r} appears more than once in the IWDR listing")
        entries[name] = entry
    return entries


def _iwdr_rename_reference_findings(
    tool: Mapping[str, Any],
    renamed_names: set[str],
    *,
    path: str,
) -> list[str]:
    """Reject any reference to an input whose entry has a literal name.

    What such a reference means is out of scope (design §6, Inputs and
    channels); the supported pattern is for the command to name the entry by
    its literal. An entry under the input's own basename is referenced freely.
    """
    if not renamed_names:
        return []
    referenced = (_referenced_names(tool) or set()) & renamed_names
    return [
        f"{path}.run.requirements.InitialWorkDirRequirement: input {name!r} has an InitialWorkDirRequirement "
        "entry with a literal name and cannot also be referenced in the command, stream targets, or output "
        "globs; name the entry by its literal"
        for name in sorted(referenced)
    ]


def _input_binding_items(
    inputs: Mapping[str, Any],
    *,
    shell_mode: bool,
    tool: Mapping[str, Any],
) -> list[tuple[tuple[int, int, str], tuple[NfCommandToken, ...]]]:
    items: list[tuple[tuple[int, int, str], tuple[NfCommandToken, ...]]] = []
    for raw_name, definition in inputs.items():
        input_definition = _as_mapping(definition, error=f"CWL input {raw_name!r} must be a mapping")
        match input_definition.get("inputBinding"):
            case None:
                continue
            case Mapping() as binding:
                pass
            case _:
                raise ValueError(f"CWL inputBinding for {raw_name!r} must be a mapping")
        name = _identifier(raw_name, context="input binding name")
        value_from = binding.get("valueFrom")
        position = _position(binding.get("position"), default=0)
        required_type = _required_type(input_definition.get("type"))
        if binding.get("shellQuote") is False:
            items.append(
                ((position, 1, str(raw_name)), (_shell_literal_value(raw_name, binding, shell_mode=shell_mode),))
            )
            continue
        if _is_array_type(required_type):
            items.append(
                ((position, 1, str(raw_name)), (_array_binding(raw_name, name, required_type, binding),))
            )
            continue
        if required_type == "boolean":
            # CWL boolean bindings contribute their prefix, or nothing at all
            # when the flag is false or no prefix is declared. A
            # self-referencing valueFrom reduces to the same value the
            # binding already carries, so it renders identically.
            if value_from is not None:
                _boolean_flag_reference(raw_name, name, value_from)
            match binding.get("prefix"):
                case None:
                    if not binding.get("separate", True):
                        raise ValueError("CWL separate cannot be specified without a prefix")
                    continue
                case str() as prefix if prefix.strip():
                    items.append(((position, 1, str(raw_name)), (NfFlag(name, prefix),)))
                    continue
                case _:
                    raise ValueError(f"CWL command prefix for {raw_name!r} must be a non-empty string")
        if flag := _boolean_value_from(value_from, tool):
            if _is_optional(input_definition.get("type")):
                # CWL omits the whole binding for an absent input; a flag renders whatever it is.
                raise ValueError(f"a boolean valueFrom on optional input {raw_name!r} is not supported")
            items.append(((position, 1, str(raw_name)), _flag_tokens(flag, binding.get("prefix"))))
            continue
        if value_from is None and _is_optional(input_definition.get("type")) and required_type in _OPTIONAL_BINDING_TYPES:
            # CWL adds nothing for a null input; the [] sentinel stands for one.
            items.append(((position, 1, str(raw_name)), (_optional_binding(raw_name, name, binding),)))
            continue
        value = (
            _template(value_from, context=f"CWL input {raw_name!r} valueFrom")
            if value_from is not None else NfTemplate((NfInputReference(name),))
        )
        tokens = _binding_tokens(binding.get("prefix"), value, separate=binding.get("separate", True))
        items.append(((position, 1, str(raw_name)), tokens))
    return items


def _command_items(tool: Mapping[str, Any]) -> tuple[NfCommandToken, ...]:
    arguments = _as_list(tool.get("arguments", []), error="CommandLineTool arguments must be a list")
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    shell_mode = _requirement(tool, "ShellCommandRequirement") is not None
    ordered = sorted([
        *_argument_items(arguments, shell_mode=shell_mode, tool=tool),
        *_input_binding_items(inputs, shell_mode=shell_mode, tool=tool),
    ])
    return tuple(token for _key, tokens in ordered for token in tokens)


def _command(tool: Mapping[str, Any]) -> NfCommand:
    base_command = tool.get("baseCommand")
    tokens: list[NfCommandToken] = []
    match base_command:
        case str() as command if command:
            tokens.append(NfTemplate((NfLiteral(command),)))
        case list() as command:
            for token in command:
                match token:
                    case str():
                        tokens.append(NfTemplate((NfLiteral(token),)))
                    case _:
                        raise ValueError("CommandLineTool baseCommand must be a string or list of strings")
        case None:
            pass
        case _:
            raise ValueError("CommandLineTool baseCommand must be a string or list of strings")

    tokens.extend(_command_items(tool))
    if not any(isinstance(token, (NfTemplate, NfShellLiteral)) for token in tokens):
        if _requirement(tool, "DockerRequirement") is not None:
            raise ValueError(
                "a containerized tool whose command can render empty is not supported: CWL then gives the "
                "container no arguments, so the image runs its CMD, after its ENTRYPOINT if it has one, which "
                "the Nextflow task does not; name the program in baseCommand"
            )
        # On the host, a command made only of conditional tokens has nothing to execute.
        tokens.insert(0, _template("true", context="empty CWL command"))

    def stream(name: str) -> NfTemplate | None:
        return None if tool.get(name) is None else _template(tool[name], context=f"CWL {name}")

    return NfCommand(tuple(tokens), stream("stdin"), stream("stdout"), stream("stderr"), _environment(tool))


def _env_entries(requirement: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Each `envDef` entry with the suffix of its path; CWL's map form keys an entry by its `envName`."""
    match requirement.get("envDef"):
        case list() as entries:
            return [(f"envDef[{index}]", entry) for index, entry in enumerate(entries)]
        case Mapping() as entries:
            return [
                (f"envDef.{name}", {**(value if isinstance(value, Mapping) else {"envValue": value}), "envName": name})
                for name, value in entries.items()
            ]
        case _:
            raise ValueError("EnvVarRequirement envDef must be a list or a mapping")


def _env_var(entry: Any, tool: Mapping[str, Any]) -> NfEnvVar:
    """Lower one `envDef` entry: literal text, or exactly `$(inputs.<name>)` of a `string` input (design §6)."""
    match entry:
        case {"envName": str() as name, "envValue": str() as value}:
            pass
        case _:
            raise ValueError("an envDef entry needs a string envName and a string envValue")
    if whole := _INPUT_EXPRESSION.fullmatch(value.strip()):
        definition = _as_mapping(tool.get("inputs", {}), error="").get(whole["name"])
        declared = definition.get("type") if isinstance(definition, Mapping) else None
        if whole["suffix"] or _required_type(declared) != "string":
            raise ValueError(
                f"envValue {value!r} needs a string input, read whole as $(inputs.<name>); "
                f"{whole['name']} is declared {declared!r}"
            )
        return NfEnvVar(name, NfTemplate((NfInputReference(_identifier(whole["name"], context="input reference")),)))
    try:
        segments = _template(value, context="envValue").segments
    except ValueError:
        segments = ()  # an expression the template reader refuses
    if not segments or not all(isinstance(segment, NfLiteral) for segment in segments):
        raise ValueError(
            f"envValue {value!r} is not supported: write literal text or exactly $(inputs.<name>) of a string input"
        )
    return NfEnvVar(name, NfTemplate(segments))


def _environment(tool: Mapping[str, Any]) -> tuple[NfEnvVar, ...]:
    requirement = _requirement(tool, "EnvVarRequirement")
    if requirement is None:
        return ()
    return tuple(_env_var(entry, tool) for _suffix, entry in _env_entries(requirement))


def _env_var_findings(tool: Mapping[str, Any], requirement: Mapping[str, Any], *, path: str) -> list[str]:
    """Report every `envDef` entry the lowering cannot take, each at its own path."""
    try:
        entries = _env_entries(requirement)
    except ValueError as exc:
        return [f"{path}.envDef: {exc}"]
    findings: list[str] = []
    for suffix, entry in entries:
        if isinstance(entry, Mapping):
            findings.extend(_unconsumed_field_findings(entry, "EnvironmentDef", path=f"{path}.{suffix}"))
        try:
            _env_var(entry, tool)
        except ValueError as exc:
            findings.append(f"{path}.{suffix}: {exc}")
    return findings


def _requirement(tool: Mapping[str, Any], class_name: str) -> Mapping[str, Any] | None:
    for section_name in ("requirements", "hints"):
        section = tool.get(section_name, {})
        match section:
            case None:
                continue
            case Mapping() as requirements:
                match requirements.get(class_name):
                    case None:
                        continue
                    case Mapping() as requirement:
                        return requirement
                    case _:
                        raise ValueError(f"{class_name} must be a mapping")
            case list() as requirements:
                for requirement in requirements:
                    match requirement:
                        case Mapping() if requirement.get("class") == class_name:
                            return requirement
                        case _:
                            continue
            case _:
                raise ValueError(f"CommandLineTool {section_name} must be a mapping or list")
    return None


def _container(tool: Mapping[str, Any]) -> str | None:
    docker = _requirement(tool, "DockerRequirement")
    if docker is None:
        return None
    match docker.get("dockerPull", docker.get("dockerImageId")):
        case str() as image if image:
            return image
        case _:
            raise ValueError("DockerRequirement must define dockerPull or dockerImageId")


def _resource_value(resource: Mapping[str, Any], minimum: str, maximum: str) -> Any:
    return resource.get(minimum, resource.get(maximum))


def _resource_number(value: Any, *, name: str) -> int | float:
    match value:
        case bool():
            raise ValueError(f"{name} resource requirement must be numeric")
        case int() as number if number > 0:
            return number
        case float() as number if math.isfinite(number) and number > 0:
            return number
        case _:
            raise ValueError(f"{name} resource requirement must be a positive finite number")


def _cpu_resource(value: Any) -> int:
    number = _resource_number(value, name="CPU")
    if isinstance(number, float):
        if not number.is_integer():
            raise ValueError("CPU resource requirement must be a whole number")
        return int(number)
    return number


_CUDA_REQUIREMENT = "cwltool:CUDARequirement"
# The CUDA fields with no Nextflow counterpart, carried in the process's ext directive: each one present is
# reported with a note.
_UNCHECKED_CUDA_FIELDS = ("cudaVersionMin", "cudaComputeCapability")


def _cuda_version(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise ValueError(f"cudaVersionMin must be a string, got {value!r}")


def _cuda_capability(value: Any) -> str | tuple[str, ...]:
    match value:
        case str():
            return value
        case list() if all(isinstance(item, str) for item in value):
            return tuple(value)
    raise ValueError(f"cudaComputeCapability must be a string or a list of strings, got {value!r}")


def _device_count(value: Any) -> int:
    match value:
        case bool():
            pass
        case int() as count if count > 0:
            return count
    raise ValueError(f"a CUDA device count must be a positive whole number, got {value!r}")


def _gpu_count(tool: Mapping[str, Any]) -> int | None:
    """cwltool's device count: the minimum, else the maximum, else 1 (design §6, Resources and containers)."""
    cuda = _requirement(tool, _CUDA_REQUIREMENT)
    if cuda is None:
        return None
    count = _resource_value(cuda, "cudaDeviceCountMin", "cudaDeviceCountMax")
    return 1 if count is None else _device_count(count)


def _memory(value: Any) -> int | float:
    return _resource_number(value, name="memory")


# The value checks of a requirement's fields, in the order findings report them.
_REQUIREMENT_VALUES: Mapping[str, Mapping[str, Callable[[Any], Any]]] = {
    "ResourceRequirement": {"coresMin": _cpu_resource, "coresMax": _cpu_resource, "ramMin": _memory, "ramMax": _memory},
    _CUDA_REQUIREMENT: {
        "cudaDeviceCountMin": _device_count, "cudaDeviceCountMax": _device_count,
        "cudaVersionMin": _cuda_version, "cudaComputeCapability": _cuda_capability,
    },
}


def _resources(tool: Mapping[str, Any]) -> NfResources:
    resource = _requirement(tool, "ResourceRequirement") or {}
    cpus = _resource_value(resource, "coresMin", "coresMax")
    memory = _resource_value(resource, "ramMin", "ramMax")
    rendered_cpus = None if cpus is None else _cpu_resource(cpus)
    rendered_memory = None if memory is None else _memory(memory)
    cuda = _requirement(tool, _CUDA_REQUIREMENT) or {}
    version, capability = (cuda.get(name) for name in _UNCHECKED_CUDA_FIELDS)
    return NfResources(
        rendered_cpus, rendered_memory, _gpu_count(tool),
        None if version is None else _cuda_version(version),
        None if capability is None else _cuda_capability(capability),
    )


def _cuda_notes(step: str, tool: Mapping[str, Any]) -> list[Diagnostic]:
    """A note for each CUDA field the generated pipeline does not check, at the step named `step`."""
    cuda = _requirement(tool, _CUDA_REQUIREMENT) or {}
    return [
        Diagnostic(
            Severity.NOTE, SophiosErrorCode.UNCHECKED_CUDA_FIELD,
            f"{_CUDA_REQUIREMENT} {name} {cuda[name]!r} is not checked: the Nextflow accelerator directive "
            "asks only for a number of GPUs",
            locator=Locator(step=step),
        )
        for name in _UNCHECKED_CUDA_FIELDS if name in cuda
    ]


def _output_template(raw_name: Any, definition: Any) -> NfTemplate:
    output_definition = _as_mapping(definition, error=f"CWL output {raw_name!r} must be a mapping")
    match output_definition.get("outputBinding"):
        case None:
            raise ValueError(f"CWL output {raw_name!r} must define outputBinding.glob")
        case Mapping() as binding:
            pass
        case _:
            raise ValueError(f"CWL outputBinding for {raw_name!r} must be a mapping")
    match binding.get("glob"):
        case None:
            raise ValueError(f"CWL output {raw_name!r} must define outputBinding.glob")
        case str() as glob if glob:
            return _template(glob, context=f"CWL output glob for {raw_name!r}")
        case str():
            raise ValueError(f"CWL output glob for {raw_name!r} cannot be empty")
        case list():
            raise ValueError(
                f"CWL output glob lists for {raw_name!r} are not supported by the Nextflow target"
            )
        case _:
            raise ValueError(f"CWL output glob for {raw_name!r} must be a string or list")


# The complete set of CWL outputEval texts with an approved lowering, mapping
# exact source text to the closed capture marker it declares. This table is the
# single owner of the admitted forms: nothing else constructs a capture marker,
# and recognition is a lookup, never an inspection of expression text. Adding a
# form means adding a row here, which requires a design revision first.
_ADMITTED_OUTPUT_EVAL: Mapping[str, str] = MappingProxyType({
    "$(self[0])": "single",
    "$(self[0].contents)": "text",
})
# The capture markers whose admission requires loadContents: true beside them.
_LOAD_CONTENTS_CAPTURES = frozenset({"text"})
# The one CWL output type file-text capture is admitted on. Parsing text into a
# number is computation, so int and float are refused rather than deferred.
_TEXT_CAPTURE_TYPE = "string"
_NUMERIC_TEXT_CAPTURE_TYPES = frozenset({"int", "float"})
# outputEval shapes refused by name rather than by a generic table miss, so the
# diagnostic says why the shape is unattractive rather than merely absent.
_PERMANENT_PROJECTION_REASON = (
    "a produced file's directory under this backend is the Nextflow task work "
    "directory, which Nextflow owns, may relocate, and may clean up; it is never the "
    "directory the CWL author described, so this projection is refused rather than "
    "deferred"
)
_NAMED_OUTPUT_EVAL_REJECTIONS: Mapping[str, str] = MappingProxyType({
    "$(self[0].dirname)": _PERMANENT_PROJECTION_REASON,
    "$(self[0].path)": _PERMANENT_PROJECTION_REASON,
    "$(self[0].location)": (
        "a CWL location is a URI whose scheme the executable model does not represent"
    ),
    "$(self[0].checksum)": "computing a checksum is computation, not a projection",
    "$(self[0].secondaryFiles)": "secondary files have no approved lowering",
    "$(self[0].format)": "output format annotations have no approved lowering",
})


def _output_capture(raw_name: Any, definition: Mapping[str, Any]) -> str | None:
    """Return the closed capture marker one CWL ``outputBinding`` declares.

    Recognition is a lookup against the frozen admitted table: exactly
    matching text names its marker and every other text is rejected, so no
    code inspects, transforms, or composes expression text. The paired glob
    is tokenized by the ordinary template tokenizer and must reduce to one
    literal, because a capture declaration projects the first glob match and
    that is provable only where the match set has one member.

    Args:
        raw_name (Any): The CWL output name, for diagnostics.
        definition (Mapping[str, Any]): The CWL output parameter definition.

    Raises:
        ValueError: If the binding declares any unadmitted capture shape.
            Callers in capability analysis catch this and report it by path;
            lowering lets it propagate.

    Returns:
        str | None: The capture marker, or None when the binding declares
            no capture behavior at all.
    """
    binding = definition.get("outputBinding")
    if not isinstance(binding, Mapping):
        return None
    raw_eval = binding.get("outputEval")
    if raw_eval is None:
        if "loadContents" in binding:
            raise ValueError(
                f"loadContents on output {raw_name!r} has no approved lowering without its "
                "paired outputEval capture declaration"
            )
        return None
    if not isinstance(raw_eval, str):
        raise ValueError(f"outputEval for {raw_name!r} must be a string, got {raw_eval!r}")
    text = raw_eval.strip()
    if reason := _NAMED_OUTPUT_EVAL_REJECTIONS.get(text):
        raise ValueError(f"outputEval {text!r} is not representable: {reason}")
    capture = _ADMITTED_OUTPUT_EVAL.get(text)
    if capture is None:
        admitted = ", ".join(repr(shape) for shape in sorted(_ADMITTED_OUTPUT_EVAL))
        raise ValueError(
            f"unsupported outputEval {raw_eval!r}; the approved set is exactly "
            f"{admitted}, recognized as capture declarations, and no other expression is "
            "evaluated"
        )
    declared_type = definition.get("type")
    if capture in _LOAD_CONTENTS_CAPTURES:
        if binding.get("loadContents") is not True:
            raise ValueError(
                f"outputEval {text!r} captures file text and requires loadContents: true "
                "beside it"
            )
        required = _required_type(declared_type)
        if isinstance(required, str) and required in _NUMERIC_TEXT_CAPTURE_TYPES:
            raise ValueError(
                f"outputEval {text!r} captures file text, and parsing that text into a "
                f"number is computation rather than a projection; output {raw_name!r} "
                f"declares {declared_type!r}. Declare type: string and parse downstream"
            )
        if required != _TEXT_CAPTURE_TYPE:
            raise ValueError(
                f"outputEval {text!r} captures file text and is admitted only on a "
                f"type: string output; output {raw_name!r} declares {declared_type!r}"
            )
    else:
        if "loadContents" in binding:
            raise ValueError(
                f"outputEval {text!r} declares output cardinality and is not paired with "
                "loadContents"
            )
        try:
            qualifier = cwl_type_to_nf_qualifier(declared_type)
        except ValueError:
            qualifier = "unsupported"
        if qualifier != "path":
            raise ValueError(
                f"outputEval {text!r} declares single-valued File or Directory capture; "
                f"output {raw_name!r} declares {declared_type!r}"
            )
    match binding.get("glob"):
        case str() as glob if glob:
            pass
        case _:
            raise ValueError(
                f"outputEval {text!r} requires a single-literal outputBinding.glob"
            )
    segments = _template(glob, context=f"CWL output glob for {raw_name!r}").segments
    match segments:
        case [NfLiteral() as literal]:
            pass
        case _:
            raise ValueError(
                f"outputEval {text!r} requires a glob with no input reference; a "
                "reference's runtime value cannot be shown wildcard-free at compile time"
            )
    if wildcards := sorted(GLOB_WILDCARDS.intersection(literal.value)):
        found = ", ".join(repr(character) for character in wildcards)
        raise ValueError(
            f"outputEval {text!r} requires a glob with no wildcard character, but "
            f"{literal.value!r} contains {found}; projecting the first match would make "
            "CWL and Nextflow glob match ordering load-bearing, which is unproven"
        )
    return capture


# Keys a compiled CWL document carries beside its class's fields.
_DOCUMENT_KEYS = frozenset({"$namespaces", "$schemas"})
_TOOL_CONSUMED_FIELDS = consumed(FIELDS["CommandLineTool"]) | _DOCUMENT_KEYS
_WORKFLOW_CONSUMED_FIELDS = consumed(FIELDS["Workflow"]) | _DOCUMENT_KEYS | {"sophios:lang_version"}
# The step-input fields the fixed idioms lower (design §6, Topology, Step inputs).
_STEP_INPUT_IDIOM_FIELDS = frozenset({"default", "linkMerge", "pickValue"})
# The pickValue methods that give one value rather than a list.
_PICKS_ONE = frozenset({"first_non_null", "the_only_non_null"})
# The outputBinding fields that declare capture behavior, as opposed to naming
# the file to capture.
_OUTPUT_CAPTURE_FIELDS = frozenset({"loadContents", "outputEval"})


def _default_value_findings(definition: Mapping[str, Any], *, path: str) -> list[str]:
    """Validate a declared scalar default against the supported value subset.

    A long or double type already has its own finding, so its default is not judged against it.
    """
    if "default" not in definition or _type_names(definition) & _UNSUPPORTED_NUMBER_TYPES:
        return []
    default = definition["default"]
    if default is None or isinstance(default, (Mapping, list)):
        return [f"{path}.default: the Nextflow target supports JSON scalar defaults only"]
    if not _phase1_value_matches(definition.get("type"), default):
        return [f"{path}.default: value does not match its supported CWL type"]
    return []


def _rejection(rows: Mapping[str, Row], name: str, *, otherwise: str) -> str:
    """Why `name` is rejected: its row's note, or `otherwise` when the table has no rejected row for it."""
    row = rows.get(name)
    return row.note if row is not None and row.state is State.REJECTED else otherwise


def _unconsumed_field_findings(
    value: Mapping[str, Any],
    cwl_class: str,
    *,
    path: str,
    kept: frozenset[str] | None = None,
) -> list[str]:
    """Reject the fields of a `cwl_class` value the lowering does not consume.

    Args:
        value (Mapping[str, Any]): The CWL value.
        cwl_class (str): Its class, a key of `FIELDS`.
        path (str): Where the value sits, for the finding.
        kept (frozenset[str] | None): The fields consumed, when not exactly the class's unrejected rows.

    Returns:
        list[str]: One finding per field, with its row's note.
    """
    rows = FIELDS[cwl_class]
    return [
        f"{path}.{name}: {_rejection(rows, name, otherwise=f'{name} is not consumed by the Nextflow lowering')}"
        for name in sorted(set(value) - (consumed(rows) if kept is None else kept))
    ]


# The array schema class each parameter class's array types are; a class with none gets no array-schema check.
_ARRAY_SCHEMAS = {
    "WorkflowInputParameter": "InputArraySchema",
    "WorkflowOutputParameter": "OutputArraySchema",
    "CommandInputParameter": "CommandInputArraySchema",
}


def _parameter_findings(definition: Any, cwl_class: str, *, path: str) -> list[str]:
    """Reject a parameter's long or double type, and each field of it or its array schemas not consumed.

    Args:
        definition (Any): The parameter, or its bare type.
        cwl_class (str): The parameter's class, a key of `FIELDS`.
        path (str): Where the parameter sits, for the finding.

    Returns:
        list[str]: The number-type findings, then one finding per field, with its row's note.
    """
    findings = _number_type_findings(definition, path=path)
    if schema_class := _ARRAY_SCHEMAS.get(cwl_class):
        cwl_type: Any = definition.get("type") if isinstance(definition, Mapping) else definition
        members = (
            [(f"{path}.type[{index}]", member) for index, member in enumerate(cwl_type)]
            if isinstance(cwl_type, list) else [(f"{path}.type", cwl_type)]
        )
        findings.extend(
            finding for member_path, member in members if _is_array_type(member)
            for finding in _unconsumed_field_findings(member, schema_class, path=member_path)
        )
    if isinstance(definition, Mapping):
        findings.extend(_unconsumed_field_findings(definition, cwl_class, path=path))
    return findings


def local_locations_as_paths(value: Any) -> Any:
    """Rewrite a File or Directory object's plain local ``location`` as its ``path``, and drop a File's ``format``.

    The Python API writes file values in CWL's ``location`` form; for a local
    path with no URI scheme the two keys mean the same file. A URI keeps its
    ``location`` and stays rejected, since the model does not represent schemes.
    A File value's ``format`` is compile-time, as the input's is (design §3).
    """
    match value:
        case {"class": "File", "format": _}:
            return local_locations_as_paths({key: item for key, item in value.items() if key != "format"})
        case {"class": "File" | "Directory", "location": str() as location} if (
            set(value) == {"class", "location"} and "://" not in location
        ):
            return {"class": value["class"], "path": location}
        case Mapping():
            return {key: local_locations_as_paths(item) for key, item in value.items()}
        case list():
            return [local_locations_as_paths(item) for item in value]
    return value


def _phase1_value_matches(cwl_type: Any, value: Any) -> bool:
    """Return whether a concrete boundary value has the supported runtime shape."""
    required = _required_type(cwl_type)
    if _is_array_type(required):
        if not isinstance(value, list):
            return False
        try:
            item_type = _array_item_type(required)
        except ValueError:
            return False
        return all(_phase1_value_matches(item_type, item) for item in value)
    match required, value:
        case "string", str():
            return True
        case "boolean", bool():
            return True
        case "int", int() if not isinstance(value, bool):
            return True
        case "float", (int() | float()) if not isinstance(value, bool):
            return True
        case ("File" | "Directory") as expected, (str() | PathLike()):
            return True
        case ("File" | "Directory") as expected, Mapping() as mapping:
            return (
                set(mapping) <= {"class", "path"}
                and mapping.get("class", expected) == expected
                and isinstance(mapping.get("path"), (str, PathLike))
            )
        case _:
            return False


def _requirement_entries(section: Any) -> list[tuple[str, str, Mapping[str, Any] | None]]:
    """(class, path suffix, payload) of each requirement in a section; a list item without a string class is ``""``."""
    match section:
        case Mapping():
            return [
                (str(name), str(name), payload if isinstance(payload, Mapping) else None)
                for name, payload in section.items()
            ]
        case list():
            return [
                (
                    item["class"] if isinstance(item, Mapping) and isinstance(item.get("class"), str) else "",
                    str(index),
                    item if isinstance(item, Mapping) else None,
                )
                for index, item in enumerate(section)
            ]
    return []


def _requirement_findings(
    section: Any,
    owner: str,
    *,
    path: str,
    otherwise: str,
    tool: Mapping[str, Any] | None = None,
) -> list[str]:
    """Apply closed-world analysis to each requirement or hint of a section.

    Args:
        section (Any): The requirements or hints.
        owner (str): The class holding them, a key of `REQUIREMENT_CLASSES`.
        path (str): Where the section sits, for the finding.
        otherwise (str): The reason a class the table has no rejected row for is rejected; ``{}`` is the class.
        tool (Mapping[str, Any] | None): The tool holding them, whose inputs an InitialWorkDirRequirement stages.

    Returns:
        list[str]: One finding per unsupported requirement, field or value.
    """
    rows = REQUIREMENT_CLASSES[owner]
    findings: list[str] = []
    for class_name, suffix, payload in _requirement_entries(section):
        requirement_path = f"{path}.{suffix}"
        if not class_name:
            findings.append(f"{requirement_path}: a requirement or hint needs a string `class`")
        elif class_name not in consumed(rows):
            reason = _rejection(rows, class_name, otherwise=otherwise.format(class_name))
            findings.append(f"{requirement_path}: {reason}")
        elif class_name == "NetworkAccess" and (payload or {}).get("networkAccess") is not True:
            findings.append(
                f"{requirement_path}.networkAccess: only the literal true is supported: a Nextflow "
                "task has network access, and the generated pipeline cannot withhold it"
            )
        elif payload:
            findings.extend(_unconsumed_field_findings(payload, class_name, path=requirement_path))
            for field_name, check in _REQUIREMENT_VALUES.get(class_name, {}).items():
                if field_name in payload:
                    try:
                        check(payload[field_name])
                    except ValueError as exc:
                        findings.append(f"{requirement_path}.{field_name}: {exc}")
            if class_name == "EnvVarRequirement" and tool is not None:
                findings.extend(_env_var_findings(tool, payload, path=requirement_path))
            if class_name == "InitialWorkDirRequirement" and tool is not None:
                findings.extend(_iwdr_listing_findings(tool, payload, path=requirement_path))
    return findings


def _template_positions(
    tool: Mapping[str, Any],
    *,
    path: str,
) -> list[tuple[Any, str]]:
    """Pair every templated tool value with the CWL path it was written at."""
    positions: list[tuple[Any, str]] = []
    if isinstance(arguments := tool.get("arguments"), list):
        for index, argument in enumerate(arguments):
            argument_path = f"{path}.run.arguments[{index}]"
            match argument:
                case Mapping() as mapping if mapping.get("valueFrom") is not None:
                    positions.append((mapping["valueFrom"], f"{argument_path}.valueFrom"))
                case Mapping():
                    pass
                case _:
                    positions.append((argument, argument_path))
    if isinstance(inputs := tool.get("inputs"), Mapping):
        for raw_name, definition in inputs.items():
            if not isinstance(definition, Mapping):
                continue
            binding = definition.get("inputBinding")
            if not isinstance(binding, Mapping):
                continue
            if binding.get("valueFrom") is not None:
                positions.append((binding["valueFrom"], f"{path}.run.inputs.{raw_name}.inputBinding.valueFrom"))
    for stream in ("stdin", "stdout", "stderr"):
        if tool.get(stream) is not None:
            positions.append((tool[stream], f"{path}.run.{stream}"))
    if isinstance(outputs := tool.get("outputs"), Mapping):
        for raw_name, definition in outputs.items():
            if not isinstance(definition, Mapping):
                continue
            binding = definition.get("outputBinding")
            if not isinstance(binding, Mapping):
                continue
            if binding.get("glob") is not None:
                positions.append(
                    (
                        binding["glob"],
                        f"{path}.run.outputs.{raw_name}.outputBinding.glob",
                    )
                )
    return positions


def _computed_value_from_findings(value_from: Any, *, path: str) -> list[str]:
    """Reject a valueFrom that computes (design §6, Expressions): one is literal text or a projection."""
    try:
        _template(value_from, context="valueFrom")
    except ValueError:
        return [
            f"{path}.valueFrom: a computed valueFrom is not supported; write literal text or $(inputs.<name>), "
            "and on a File or Directory input $(inputs.<name>.path) or $(inputs.<name>.basename)"
        ]
    return []


#: Where CWL needs a string, what it calls the value there; every other templated value is a command word.
_STRING_POSITIONS = {"glob": "a glob", "stdin": "stdin", "stdout": "a stdout target", "stderr": "a stderr target"}


def _path_reference_findings(tool: Mapping[str, Any], entries: Mapping[str, NfEntry], *, path: str) -> list[str]:
    """Check every input reference against what CWL reads it as where it stands (design §6).

    ``.path`` and ``.basename`` need a File or Directory input. A bare File or
    Directory reference is the object: in a command word it renders as its
    path, embedded in other text as JSON, and a glob or a stream target, which
    must be a string, does not take it. ``.path`` is absolute: stdout and stderr
    must be relative names, and a glob matches only under the output directory,
    where only an entry is. stdin reads one staged file, named by its whole
    ``.path``. A renamed entry is left to its own finding, which refuses every
    reference to it. Read from the authored text, because ``_template`` lowers
    ``.path`` to a plain reference and the model could no longer tell the two
    apart.
    """
    inputs = tool.get("inputs", {})
    if not isinstance(inputs, Mapping):
        return []
    findings: list[str] = []
    for value, position in _template_positions(tool, path=path):
        if not isinstance(value, str):
            continue  # a list of globs has a finding of its own
        whole = _INPUT_EXPRESSION.fullmatch(value.strip()) is not None
        kind = next((kind for kind in _STRING_POSITIONS if position.endswith(f".{kind}")), None)
        try:
            linked = linked_reference(_template(value, context=position))
        except ValueError:
            linked = None  # an expression, which has a finding of its own
        references = (match.group("name", "suffix") for match in _INTERPOLATION.finditer(value))
        for name, suffix in dict.fromkeys(reference for reference in references if reference[0]):
            definition, entry = inputs.get(name), entries.get(name)
            if not isinstance(definition, Mapping) or entry is not None and entry.name is not None:
                continue
            try:
                is_path = cwl_type_to_nf_qualifier(definition.get("type")) == "path"
            except ValueError:
                continue  # the input pass reports a type the backend does not lower
            if suffix is not None and not is_path:
                findings.append(
                    f"{position}: $(inputs.{name}.{suffix}) requires a File or Directory input, "
                    f"and {name} is declared {definition.get('type')!r}"
                )
                continue
            if not is_path:
                continue
            # What the model takes in its place: .basename, unless it is the input's staged name or a path inside it.
            refusal = link_refusal(entry, writes=kind != "glob") if linked == name else None
            instead = (f"use $(inputs.{name}.path) or $(inputs.{name}.basename), or the reference alone"
                       if kind is None else f"use $(inputs.{name}.path) alone" if kind == "stdin"
                       else refusal or f"use $(inputs.{name}.basename)")
            if suffix is None and not whole:
                findings.append(
                    f"{position}: $(inputs.{name}) embedded in other text renders a File object as JSON in "
                    f"CWL; {instead}"
                )
            elif suffix is None and kind is not None:
                findings.append(
                    f"{position}: $(inputs.{name}) is the File or Directory object, which CWL does not take as "
                    f"{_STRING_POSITIONS[kind]}; {instead}"
                )
            elif kind == "stdin" and not whole:
                findings.append(
                    f"{position}: stdin reads a staged file by its whole path, which $(inputs.{name}.{suffix}) "
                    f"inside other text is not; {instead}"
                )
            elif kind == "stdin" and suffix == "basename":
                findings.append(
                    f"{position}: stdin names a file by its path, and $(inputs.{name}.basename) is only its "
                    f"name; {instead}"
                )
            elif kind == "stdin" and _required_type(definition.get("type")) == "Directory":
                findings.append(f"{position}: stdin reads a file, and $(inputs.{name}.path) names a Directory")
            elif suffix == "path" and kind in ("stdout", "stderr"):
                findings.append(
                    f"{position}: $(inputs.{name}.path) is an absolute path, and CWL takes only a relative name "
                    f"as {_STRING_POSITIONS[kind]}; {instead}"
                )
            elif suffix == "path" and kind == "glob" and entry is None:
                findings.append(
                    f"{position}: $(inputs.{name}.path) is an absolute path outside the output directory, which no "
                    f"CWL glob matches; {instead}"
                )
    return findings


def _tool_capability_findings(tool: Mapping[str, Any], *, path: str) -> list[str]:
    """Collect unsupported executable semantics of a step's tool without lowering it; `path` names the step."""
    findings: list[str] = []

    match tool.get("class"):
        case "CommandLineTool":
            pass
        case unsupported_class:
            findings.append(
                f"{path}.run.class: unsupported compiled step class {unsupported_class!r}"
            )
            return findings

    findings.extend(
        _unconsumed_field_findings(tool, "CommandLineTool", path=f"{path}.run", kept=_TOOL_CONSUMED_FIELDS)
    )
    try:
        entries: dict[str, NfEntry] | None = _iwdr_entries(tool)
    except ValueError:
        entries = None  # the listing's own findings report it; what it stages is unknown, so no reference is checked
    if entries is not None:
        findings.extend(_path_reference_findings(tool, entries, path=path))

    for section_name in ("requirements", "hints"):
        findings.extend(
            _requirement_findings(
                tool.get(section_name), "CommandLineTool", path=f"{path}.run.{section_name}",
                otherwise="{} is not supported by the Nextflow target", tool=tool,
            )
        )
    renamed_names = {name for name, entry in (entries or {}).items() if entry.name is not None}
    findings.extend(_iwdr_rename_reference_findings(tool, renamed_names, path=path))
    shell_mode_active = _requirement(tool, "ShellCommandRequirement") is not None
    if shell_mode_active and _requirement(tool, "DockerRequirement") is not None:
        findings.append(
            f"{path}.run: ShellCommandRequirement beside a DockerRequirement is not supported: CWL runs the "
            "command as /bin/sh -c after the image's ENTRYPOINT, which the Nextflow task does not"
        )
    if isinstance(inputs := tool.get("inputs", {}), Mapping):
        findings.extend(
            _identifier_normalization_findings(
                inputs,
                context="tool input",
                path=f"{path}.run.inputs",
            )
        )
        for raw_name, raw_definition in inputs.items():
            if not isinstance(raw_definition, Mapping):
                continue
            input_path = f"{path}.run.inputs.{raw_name}"
            findings.extend(_parameter_findings(raw_definition, "CommandInputParameter", path=input_path))
            findings.extend(_default_value_findings(raw_definition, path=input_path))
            binding = raw_definition.get("inputBinding")
            if not isinstance(binding, Mapping):
                continue
            input_binding_path = f"{input_path}.inputBinding"
            findings.extend(
                _unconsumed_field_findings(binding, "CommandLineBinding", path=input_binding_path)
            )
            required_type = _required_type(raw_definition.get("type"))
            if _is_array_type(required_type) and _type_names(required_type.get("items")) - {"null"} == {"boolean"}:
                findings.append(
                    f"{input_binding_path}: a boolean array's binding is not supported: CWL adds only its "
                    "prefix, when the array is not empty, and never an item's true or false"
                )
            if binding.get("shellQuote") is False:
                try:
                    _shell_literal_value(raw_name, binding, shell_mode=shell_mode_active)
                except ValueError as exc:
                    findings.append(f"{input_binding_path}.shellQuote: {exc}")
            if (
                required_type == "boolean"
                and binding.get("valueFrom") is not None
                and binding.get("shellQuote") is not False
            ):
                try:
                    _boolean_flag_reference(
                        raw_name,
                        _identifier(raw_name, context="input binding name"),
                        binding["valueFrom"],
                    )
                except ValueError as exc:
                    findings.append(f"{input_binding_path}.valueFrom: {exc}")
            elif binding.get("valueFrom") is not None and binding.get("shellQuote") is not False:
                findings.extend(_computed_value_from_findings(binding["valueFrom"], path=input_binding_path))

    if isinstance(arguments := tool.get("arguments", []), list):
        for argument_index, argument in enumerate(arguments):
            if not isinstance(argument, Mapping):
                continue
            argument_path = f"{path}.run.arguments[{argument_index}]"
            findings.extend(
                _unconsumed_field_findings(argument, "CommandLineBinding", path=argument_path)
            )
            if argument.get("shellQuote") is False:
                try:
                    _shell_literal_value(
                        f"arguments[{argument_index}]", argument, shell_mode=shell_mode_active
                    )
                except ValueError as exc:
                    findings.append(f"{argument_path}.shellQuote: {exc}")
            elif argument.get("valueFrom") is not None:
                findings.extend(_computed_value_from_findings(argument["valueFrom"], path=argument_path))

    if isinstance(outputs := tool.get("outputs", {}), Mapping):
        findings.extend(
            _identifier_normalization_findings(
                outputs,
                context="tool output",
                path=f"{path}.run.outputs",
            )
        )
        for raw_name, raw_definition in outputs.items():
            if not isinstance(raw_definition, Mapping):
                continue
            output_path = f"{path}.run.outputs.{raw_name}"
            findings.extend(_parameter_findings(raw_definition, "CommandOutputParameter", path=output_path))
            binding = raw_definition.get("outputBinding")
            # An outputBinding that declares capture behavior is diagnosed
            # by _output_capture alone, which reports the type it requires;
            # the generic type finding would only restate that less
            # precisely.
            declares_capture = isinstance(binding, Mapping) and bool(
                _OUTPUT_CAPTURE_FIELDS & set(binding)
            )
            try:
                qualifier = cwl_type_to_nf_qualifier(raw_definition.get("type"))
            except ValueError:
                qualifier = "unsupported"
            if qualifier != "path" and not declares_capture:
                findings.append(
                    f"{output_path}.type: an output is collected by glob as a File or Directory, "
                    "or captured as text with loadContents and outputEval"
                )
            if not isinstance(binding, Mapping):
                continue
            output_binding_path = f"{output_path}.outputBinding"
            findings.extend(
                _unconsumed_field_findings(binding, "CommandOutputBinding", path=output_binding_path)
            )
            try:
                _output_capture(raw_name, raw_definition)
            except ValueError as exc:
                findings.append(f"{output_binding_path}: {exc}")
    return findings


@dataclass(frozen=True, slots=True)
class _Step:
    """One tool step as the lowering reads it: the view's, at its index, its tool normalized on the lowering's copy."""

    index: int
    view: ToolStep
    tool: Mapping[str, Any]

    @property
    def node(self) -> ResolvedStep:
        """The step itself."""
        return self.view.step

    @property
    def path(self) -> str:
        """Where a finding names the step."""
        return f"steps[{self.index}]"

    def bound(self, *, declared: bool = True) -> Iterator[tuple[StepInput, tuple[Through | Crossing, ...]]]:
        """Each step input and its sources, in `in:` order: those the tool declares, or every one.

        An input the tool does not declare that passes the undeclared-input check is carried nowhere.
        """
        for port, sources in zip(self.node.inputs, self.view.sources, strict=True):
            if port.declared is not None or not declared:
                yield port, sources

    def wired(self) -> Iterator[tuple[StepInput, list[str]]]:
        """Each declared step input of a shape the idioms read, and its sources spelled."""
        for port, sources in self.bound():
            spelled = _wired(port, sources)
            if spelled is not None:
                yield port, spelled


def _spelled(found: Through) -> str:
    """A source as a finding names it: a workflow input's name, or `<step>/<port>`."""
    ref = found.ref
    return ref.name if isinstance(ref, InputRef) else f"{ref.step}/{ref.port}"


def _idioms(port: StepInput) -> set[str]:
    """The fixed step-input idioms a step input writes."""
    return {name for name, written in (("default", port.has_default), ("linkMerge", port.link_merge is not None),
                                       ("pickValue", port.pick_value is not None)) if written}


def _record_fields(port: StepInput) -> set[str]:
    """Every WorkflowStepInput field a step input writes besides its sources."""
    return _idioms(port) | {name for name, _value in port.fields} | (
        {"valueFrom"} if port.value_from is not None else set())


def _wired(port: StepInput, sources: tuple[Through | Crossing, ...]) -> list[str] | None:
    """The sources a step input names, spelled, or None for a shape the idioms do not read.

    A raw reference is read as the source it names; a default alone, or with a label, is the value; any other
    field the lowering does not consume is reported by the field pass.
    """
    if port.expression is not None:
        return [port.expression]
    if any(isinstance(found, Crossing) for found in sources):
        return None
    if port.sources:
        if _record_fields(port) - consumed(FIELDS["WorkflowStepInput"]):
            return None
        return [_spelled(found) for found in sources if isinstance(found, Through)]
    return [] if port.has_default and _record_fields(port) <= {"default", "label"} else None


def _gives_non_null(port: StepInput) -> bool:
    """Whether a step input's idioms give a value that is not null whatever its sources: a pickValue or a default."""
    return port.pick_value is not None or port.has_default


def _parameter(declaration: PortDeclaration) -> Any:
    """A workflow port's declaration as the CWL parameter it states: its bare type, or its fields."""
    if declaration.shorthand:
        return declaration.type.declared
    fields: dict[str, Any] = {"type": declaration.type.declared}
    if declaration.has_format:
        fields["format"] = declaration.format
    if declaration.has_default:
        fields["default"] = declaration.default
    return {**fields, **dict(declaration.passthrough)}


def _output_parameter(port: WorkflowOutput) -> Any:
    """A workflow output as the CWL parameter it states, its `outputSource` among its fields."""
    parameter = _parameter(port.declaration)
    if port.source is None:
        return parameter
    return {**(parameter if isinstance(parameter, Mapping) else {"type": parameter}), "outputSource": port.source}


def _step_fields(node: ResolvedStep) -> dict[str, Any]:
    """Every field a step writes, keyed as CWL keys a WorkflowStep: its `scatter`, `when`, requirements and hints,
    and the rest."""
    fields: dict[str, Any] = {"id": node.local, "in": None, "out": None, "run": None, **dict(node.fields)}
    if node.scatter is not None:
        fields["scatter"] = list(node.scatter.inputs)
        if node.scatter.method is not None:
            fields["scatterMethod"] = node.scatter.method
    if node.condition is not None:
        fields["when"] = node.condition.written
    for requirement in node.requirements:
        fields.setdefault("hints" if requirement.hint else "requirements", {})[requirement.cls] = requirement.body
    return fields


def _requirement_section(requirements: tuple[Requirement, ...], *, hints: bool) -> dict[str, Any] | None:
    """The requirements, or the hints, of a workflow or step, as the mapping CWL writes them."""
    section = {requirement.cls: requirement.body for requirement in requirements if requirement.hint is hints}
    return section or None


def _call_findings(workflow: ResolvedWorkflow, *, path: str = "workflow") -> list[str]:
    """Reject the semantics of a call the flat executable graph would erase.

    The core compiler owns composition, edge resolution, and namespace identity. The backend still owns its
    capability boundary: what a call or its nested workflow says must be consumed here or rejected before the call
    is inlined.
    """
    findings: list[str] = []
    for index, step in enumerate(workflow.steps):
        step_path = f"{path}.steps[{index}]"
        child = step.run
        if not isinstance(child, ResolvedWorkflow):
            continue
        if step.condition is not None:
            findings.append(f"{step_path}.when: a when on a nested-workflow step is not supported; put it on a tool "
                            "step")
        if step.scatter is not None:
            findings.append(
                f"{step_path}.scatter: scatter on a nested workflow step is deferred beyond this lowering; scattering "
                "a sub-DAG is not the single-process shape scatter supports"
            )
        elif "scatterMethod" in dict(step.fields):
            findings.append(f"{step_path}.scatterMethod: scatterMethod without scatter is not executable")
        findings.extend(_unconsumed_field_findings(_step_fields(step), "WorkflowStep", path=step_path))
        for port in step.inputs:
            input_path = f"{step_path}.in.{port.name}"
            # The inlining joins one source to each input a call binds.
            merging = sorted(_idioms(port))
            if len(port.sources) > 1:
                merging.append("source")
            findings.extend(
                f"{input_path}.{name}: a nested workflow step's input takes one source as it is; merge on a step "
                "inside the nested workflow"
                for name in merging
            )
            if port.expression is None:
                findings.extend(_unconsumed_field_findings(
                    {name: None for name in _record_fields(port) - _idioms(port)}, "WorkflowStepInput",
                    path=input_path))
        child_path = f"{step_path}.run"
        findings.extend(_unconsumed_field_findings(_workflow_fields(child), "Workflow", path=child_path,
                                                   kept=_WORKFLOW_CONSUMED_FIELDS))
        findings.extend(_workflow_requirement_findings(child, path=child_path))
        for declared in child.inputs:
            port_path = f"{child_path}.inputs.{declared.name}"
            findings.extend(_parameter_findings(_parameter(declared.declaration), "WorkflowInputParameter",
                                                path=port_path))
            # Inlining replaces a nested workflow's input with the calling step's binding, where CWL would apply the
            # input's default to a null one.
            if declared.declaration.has_default and not declared.declaration.shorthand:
                findings.append(
                    f"{port_path}.default: a nested workflow input's default is not supported: bind the input on the "
                    "calling step"
                )
        for output in child.outputs:
            findings.extend(_parameter_findings(_output_parameter(output), "WorkflowOutputParameter",
                                                path=f"{child_path}.outputs.{output.name}"))
        findings.extend(_call_findings(child, path=child_path))
    return findings


def _workflow_fields(workflow: ResolvedWorkflow) -> dict[str, Any]:
    """Every field a workflow writes, keyed as CWL keys a Workflow."""
    fields: dict[str, Any] = {"class": "Workflow", "cwlVersion": None, "inputs": None, "outputs": None,
                              "steps": None, **dict(workflow.fields)}
    for hints in (False, True):
        if (section := _requirement_section(workflow.requirements, hints=hints)) is not None:
            fields["hints" if hints else "requirements"] = section
    return fields


def _undeclared_input_findings(step: ResolvedStep, sources: tuple[tuple[Through | Crossing, ...], ...], *,
                               path: str) -> list[str]:
    """Reject each step input its tool does not declare that the lowering would have to carry.

    CWL passes such an input to nothing but the step's ``when``, ``scatter`` and ``valueFrom``. One that none of
    them reads and that only workflow inputs feed changes nothing the step runs, so the lowering leaves it out.
    """
    undeclared = [(port, found) for port, found in zip(step.inputs, sources, strict=True) if port.declared is None]
    if not undeclared:
        return []
    read_by_when = references(step.condition.expr) if step.condition is not None and step.condition.expr else set()
    scattered = set(step.scatter.inputs) if step.scatter is not None else set()
    findings: list[str] = []
    for port, found in undeclared:
        input_path = f"{path}.in.{port.name}"
        if port.name in read_by_when:
            findings.append(f"{input_path}: the step's when reads an input its tool does not declare, which is not "
                            "supported")
        elif port.name in scattered:
            findings.append(f"{input_path}: scatter over an input the step's tool does not declare is not supported")
        elif any(isinstance(source, Through) and isinstance(source.ref, OutputRef) for source in found):
            findings.append(
                f"{input_path}: an input the step's tool does not declare is not supported from a step output: CWL "
                "runs this step only after that step finishes, and the Nextflow target would not wait for it"
            )
    return findings


def _step_capability_findings(step: _Step) -> list[str]:
    """What a step says that its tool step cannot lower, before its tool is judged."""
    findings: list[str] = []
    if step.node.condition is not None and not step.tool.get("inputs"):
        findings.append(
            f"{step.path}.when: a conditional step needs at least one input to gate on, and this step's tool declares "
            "none"
        )
    findings.extend(_undeclared_input_findings(step.node, step.view.sources, path=step.path))
    return findings


def _workflow_capability_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[str]:
    """Apply closed-world analysis to the workflow, its steps, and its boundary values."""
    findings = _unconsumed_field_findings(_workflow_fields(workflow), "Workflow", path="workflow",
                                          kept=_WORKFLOW_CONSUMED_FIELDS)
    findings.extend(_workflow_requirement_findings(workflow))
    names = [port.name for port in workflow.inputs]
    findings.extend(_identifier_normalization_findings(names, context="workflow input", path="workflow.inputs"))
    for given in workflow.inputs:
        input_path = f"workflow.inputs.{given.name}"
        parameter = _parameter(given.declaration)
        findings.extend(_parameter_findings(parameter, "WorkflowInputParameter", path=input_path))
        cwl_type = given.declaration.type.declared
        has_default = given.declaration.has_default and not given.declaration.shorthand
        if isinstance(parameter, Mapping):
            findings.extend(_default_value_findings(parameter, path=input_path))
        if not given.has_value and not has_default and not _is_optional(cwl_type):
            findings.append(f"{input_path}: required workflow input value is missing")
        if given.has_value and given.value is None and not _is_optional(cwl_type):
            findings.append(f"{input_path}: explicit null input values are not valid for a required input")
        elif given.has_value and given.value is None:
            # An explicit null on an optional input is only safe for specific consuming positions;
            # _absent_optional_findings analyzes each consuming step and port for that.
            pass
        elif given.has_value and not _phase1_value_matches(cwl_type, local_locations_as_paths(given.value)):
            findings.append(f"{input_path}: supplied value does not match its supported CWL type")
    findings.extend(_identifier_normalization_findings([port.name for port in workflow.outputs],
                                                       context="workflow output", path="workflow.outputs"))
    for output in workflow.outputs:
        findings.extend(_parameter_findings(_output_parameter(output), "WorkflowOutputParameter",
                                            path=f"workflow.outputs.{output.name}"))
    findings.extend(_identifier_normalization_findings([step.node.name for step in steps], context="workflow step",
                                                       path="workflow.steps", variable=False))
    for step in steps:
        fields = _step_fields(step.node)
        findings.extend(_unconsumed_field_findings(fields, "WorkflowStep", path=step.path))
        if step.node.scatter is None and "scatterMethod" in fields:
            findings.append(f"{step.path}.scatterMethod: scatterMethod without scatter is not executable")
        for port, _sources in step.bound(declared=False):
            if port.expression is None:
                findings.extend(_unconsumed_field_findings({name: None for name in _record_fields(port)},
                                                           "WorkflowStepInput", path=f"{step.path}.in.{port.name}"))
    return findings


def _workflow_requirement_findings(workflow: ResolvedWorkflow, *, path: str = "workflow") -> list[str]:
    """Apply closed-world analysis to workflow-level requirements."""
    if "requirements" in dict(workflow.fields):
        return [f"{path}.requirements: CWL Workflow requirements must be a mapping or list"]
    return _requirement_findings(
        _requirement_section(workflow.requirements, hints=False), "Workflow", path=f"{path}.requirements",
        otherwise="{} is not supported at the Nextflow workflow level",
    )


_SCATTER_METHODS = frozenset({"dotproduct", "flat_crossproduct", "nested_crossproduct"})


def _scatter_names(node: ResolvedStep) -> list[str]:
    """The input names one step scatters over.

    One name is single-input scatter, where scatterMethod is an inert restatement. Two or more is multi-input
    scatter, where scatterMethod decides how the named inputs combine; ``_scatter_findings`` rejects the methods
    this lowering does not support.
    """
    if node.scatter is None or not node.scatter.inputs:
        raise ValueError("scatter must name one input, as a string or a non-empty list")
    return list(node.scatter.inputs)


def _step_indices_by_name(steps: tuple[_Step, ...]) -> dict[str, int]:
    """Each step's index, by its name."""
    return {step.node.name: step.index for step in steps}


def _scattered_names_by_index(steps: tuple[_Step, ...]) -> dict[int, set[str]]:
    """The scattered input names of every representably scattered step."""
    scattered: dict[int, set[str]] = {}
    for step in steps:
        if step.node.scatter is None:
            continue
        try:
            scattered[step.index] = set(_scatter_names(step.node))
        except ValueError:
            continue
    return scattered


def _scattered_element_type(declared: Any) -> Any:
    """The item type a scattered port receives from an array source."""
    required = _required_type(declared)
    if not _is_array_type(required):
        return declared
    try:
        return _array_item_type(required)
    except ValueError:
        return declared


def _scatter_source_findings(step: _Step, raw_name: str, source_types: Mapping[str, Any]) -> list[str]:
    """Require one array-typed workflow-input source for the scattered input.

    The compiler has already refused an element type the scattered port cannot take.
    """
    path = f"{step.path}.in.{raw_name}"
    sources = next((spelled for wired, spelled in step.wired() if wired.name == raw_name), None)
    if sources is None:
        # Every unrecognized source shape is already reported by path.
        return []
    if len(sources) != 1:
        return [f"{path}: a scattered input must have exactly one source"]
    source = sources[0]
    if "/" in source:
        # A process-output source is reported once, by the cross-step pass.
        return []
    declared = source_types.get(source)
    if not _is_array_type(_required_type(declared)):
        return [f"{path}: a scattered input needs an array-typed source; {source!r} declares {declared!r}"]
    return []


def _scatter_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[str]:
    """Validate every scattered step against the single- and multi-input scatter lowerings."""
    findings: list[str] = []
    source_types = _source_types(workflow, steps)
    for step in steps:
        if step.node.scatter is None:
            continue
        try:
            names = _scatter_names(step.node)
        except ValueError as exc:
            findings.append(f"{step.path}.scatter: {exc}")
            continue
        method = step.node.scatter.method
        if method is not None and method not in _SCATTER_METHODS:
            findings.append(f"{step.path}.scatterMethod: unsupported CWL scatter method {method!r}")
        elif len(names) > 1 and method is None:
            # At exactly one scattered input all three methods coincide, so a method is required to decide two or
            # more; each of them is an approved multi-input lowering (design §6, Topology).
            findings.append(
                f"{step.path}.scatterMethod: multi-input scatter over {len(names)} inputs requires an explicit "
                "scatterMethod"
            )
        tool_inputs = step.tool.get("inputs", {})
        if not isinstance(tool_inputs, Mapping):
            continue
        for raw_name in names:
            definition = tool_inputs.get(raw_name)
            if not isinstance(definition, Mapping):
                # The undeclared-input pass refuses a scatter over an input the tool does not declare.
                continue
            if _is_array_type(_required_type(definition.get("type"))):
                findings.append(
                    f"{step.path}.scatter: scattered input {raw_name!r} is array-typed; scattering over an array of "
                    "arrays is deferred beyond this lowering"
                )
                continue
            findings.extend(_scatter_source_findings(step, raw_name, source_types))
    findings.extend(_scatter_edge_findings(steps))
    return findings


def _scatter_edge_findings(steps: tuple[_Step, ...]) -> list[str]:
    """Reject every process edge whose cardinality a scattered step changes and no lowering keeps.

    A scattered step's output is gathered, in invocation order, into one array; a nested_crossproduct result
    reaches workflow outputs only. An unscattered step's output reaches a scattered step as one value: it
    broadcasts to every task, and it is not an array to scatter over, since no tool output is an array.
    """
    findings: list[str] = []
    indices = _step_indices_by_name(steps)
    scattered = _scattered_names_by_index(steps)
    if not scattered:
        return findings
    for step in steps:
        for port, sources in step.wired():
            for source in sources:
                if "/" not in source:
                    continue
                producer = indices.get(source.rsplit("/", maxsplit=1)[0])
                if producer is not None and producer in scattered:
                    # Every scatter is indexed, so its outputs are gathered once, in invocation order (design §6,
                    # Topology, Gather).
                    scatter = steps[producer].node.scatter
                    if scatter is not None and scatter.method == "nested_crossproduct" and len(scattered[producer]) > 1:
                        findings.append(
                            f"{step.path}.in.{port.name}: {source!r} is a nested_crossproduct result, which reaches "
                            "workflow outputs only"
                        )
                elif port.name in scattered.get(step.index, set()):
                    findings.append(
                        f"{step.path}.in.{port.name}: a scattered input needs an array-typed source; {source!r}, from "
                        "a step that is not scattered, is one value"
                    )
    return findings


def _step_input_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[str]:
    """Check each step input the fixed idioms lower: a default, a linkMerge, a pickValue, or two or more sources.

    Each source is one value (design §6, Topology, Step inputs). Null is the [] sentinel, so an idiom may not take
    an array source where [] would be read as null, and a merged list may not hold a source that may be null.
    """
    findings: list[str] = []
    source_types = _source_types(workflow, steps)
    indices = _step_indices_by_name(steps)
    scattered = _scattered_names_by_index(steps)

    def producer(source: str) -> int | None:
        return indices.get(source.rsplit("/", maxsplit=1)[0])

    def is_array(source: str) -> bool:
        if "/" in source:
            return producer(source) in scattered  # a scatter's outputs are gathered
        return _is_array_type(_required_type(source_types.get(source)))

    def may_be_null(source: str) -> bool:
        if "/" in source:
            index = producer(source)
            return index is not None and steps[index].node.condition is not None
        return _is_optional(source_types.get(source))

    for step in steps:
        tool_inputs = step.tool.get("inputs", {})
        if not isinstance(tool_inputs, Mapping):
            continue
        for port, sources in step.wired():
            path = f"{step.path}.in.{port.name}"
            link_merge, pick_value = port.link_merge, port.pick_value
            if len(sources) < 2 and not _idioms(port):
                continue
            if port.name in scattered.get(step.index, set()):
                findings.append(f"{path}: a scattered input takes its one source as it is")
                continue
            merges = link_merge if link_merge is not None else ("merge_nested" if len(sources) > 1 else None)
            if link_merge is not None and link_merge not in LINK_MERGES:
                findings.append(f"{path}.linkMerge: unsupported linkMerge {link_merge!r}")
                continue
            if pick_value is not None and pick_value not in PICK_VALUES:
                findings.append(f"{path}.pickValue: unsupported pickValue {pick_value!r}")
                continue
            if pick_value is not None and merges is None:
                findings.append(f"{path}.pickValue: pickValue picks from a list; one source with no linkMerge is not "
                                "one")
            declared = tool_inputs.get(port.name)
            declared_type = declared.get("type") if isinstance(declared, Mapping) else declared
            if port.has_default:
                default = port.default
                if sources and merges is not None:
                    findings.append(f"{path}.default: a default never applies beside a linkMerge or a pickValue: the "
                                    "joined value is not null")
                elif _required_type(declared_type) in ("File", "Directory"):
                    findings.append(f"{path}.default: a step-input default of a File or Directory input is not "
                                    "supported; give the path as a workflow input")
                elif default is None or isinstance(default, (Mapping, list)) or not _phase1_value_matches(
                    declared_type, default
                ):
                    findings.append(f"{path}.default: the Nextflow target supports a JSON scalar default of the tool "
                                    "input's type")
            # A default is a JSON scalar, so it never sits beside an array source of its type.
            arrays = [source for source in sources if is_array(source)]
            if arrays and pick_value is not None:
                findings.append(f"{path}: {arrays[0]!r} is an array, and [] stands for both a null and an empty "
                                "array, so pickValue cannot take it")
            elif arrays and merges == "merge_nested":
                findings.append(f"{path}: merge_nested over the array {arrays[0]!r} makes a nested array, which is "
                                "not supported")
            if merges is not None and pick_value is None and (nullable := [s for s in sources if may_be_null(s)]):
                findings.append(f"{path}: {nullable[0]!r} may be null, and a joined list has no lowering of a null "
                                "element; pick from it with pickValue")
            gives_list = pick_value not in _PICKS_ONE
            if merges is not None and gives_list != _is_array_type(_required_type(declared_type)):
                findings.append(f"{path}: the step input gives {'a list' if gives_list else 'one value'}, but its "
                                f"tool input declares {declared_type!r}")
    return findings


def _source_types(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> dict[str, Any]:
    """Every wireable source name, and the CWL type it carries: a workflow input's as declared, a tool output's as its
    tool declares it."""
    types: dict[str, Any] = {port.name: port.declaration.type.declared for port in workflow.inputs}
    for step in steps:
        outputs = step.tool.get("outputs", {})
        if not isinstance(outputs, Mapping):
            continue
        for raw_port, definition in outputs.items():
            types[f"{step.node.name}/{raw_port}"] = definition.get("type") if isinstance(definition, Mapping) \
                else definition
    return types


def _referenced_names(tool: Mapping[str, Any]) -> set[str] | None:
    """The input names a plain or basename reference reads in a command template or an output glob.

    None when the tool's command or outputs cannot be lowered; lowering reports why.
    """
    try:
        command = _command(tool)
        outputs = _ports(tool.get("outputs", {}), outputs=True)
    except (ValueError, TypeError):
        return None
    templates = [*command.templates, *(port.glob for port in outputs if port.glob)]
    return {
        segment.name
        for template in templates
        for segment in template.segments
        if isinstance(segment, (NfInputReference, NfBasenameReference))
    }


def _safe_absence_names(tool: Mapping[str, Any]) -> set[str] | None:
    """Return the input names whose absence cannot affect command rendering.

    A name is safe when its absence leaves rendering unchanged: it is not
    referenced by any command token, stream target, or output glob, it does
    not own a shell-literal or valueFrom binding that CWL would omit, or it is referenced
    only by the boolean-flag token it drives or its own optional binding
    (each tests its value, never calls ``.toString()`` on the sentinel).
    Returns None when the tool's command or
    outputs cannot be analyzed; lowering reports why, so absence is not judged.
    """
    referenced = _referenced_names(tool)
    if referenced is None:
        return None
    # Every template reference dereferences the value, so it is unsafe even
    # when the same input also drives a flag: excusing it on the strength of
    # the flag use would let the absence sentinel render into a command line.
    # A flag or an optional binding is not a template, so it never enters `referenced`.
    try:
        inputs = _as_mapping(tool.get("inputs", {}), error="")
        all_names = {_identifier(raw_name, context="input reference") for raw_name in inputs}
        # A shell literal, or a valueFrom other than a boolean's flag, renders whatever the value is.
        unconditional_names = {
            _identifier(raw_name, context="input reference")
            for raw_name, definition in inputs.items()
            if isinstance(definition, Mapping)
            and isinstance(binding := definition.get("inputBinding"), Mapping)
            and (binding.get("shellQuote") is False
                 or binding.get("valueFrom") is not None and _required_type(definition.get("type")) != "boolean")
        }
    except ValueError:
        return None
    return all_names - referenced - unconditional_names


def _absent_optional_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[str]:
    """Reject optional inputs whose value is absent and unsafe."""
    if _identifier_normalization_findings([port.name for port in workflow.inputs], context="workflow input",
                                          path="workflow.inputs"):
        # The capability pass already owns this diagnostic. There is no unambiguous parameter lookup for the
        # optional-input analysis.
        return []
    params = _workflow_params(workflow)
    findings: list[str] = []
    for step in steps:
        tool_inputs = step.tool.get("inputs", {})
        if not isinstance(tool_inputs, Mapping):
            continue
        safe_names = _safe_absence_names(step.tool)
        if safe_names is None:
            continue
        bound = {port.name: (port, sources) for port, sources in step.bound()}
        for raw_name, raw_definition in tool_inputs.items():
            if not isinstance(raw_definition, Mapping):
                continue
            optional = _is_optional(raw_definition.get("type"))
            if raw_name not in bound:
                # Unwired inputs are only a missingness problem when optional without a default; required unwired
                # inputs fail compilation.
                absent = optional and "default" not in raw_definition
            else:
                port, found = bound[raw_name]
                sources = _wired(port, found)
                if sources is None:
                    continue
                # A wired input is absent when any boundary source resolves to null, including an absent optional
                # workflow input feeding a required tool input. A pickValue or a default gives a value that is not
                # null.
                absent = not _gives_non_null(port) and any(
                    params.get(_identifier(source, context="workflow input source")) is None
                    for source in sources if "/" not in source
                )
            if not absent:
                continue
            if optional:
                try:
                    name = _identifier(raw_name, context="input reference")
                    qualifier = cwl_type_to_nf_qualifier(raw_definition.get("type"))
                except ValueError:
                    name, qualifier = None, None
                if qualifier == "val" and name in safe_names:
                    continue
                detail = "absent optional values are supported only for a val input whose absence leaves command " \
                    "rendering unchanged"
            else:
                detail = "resolves to an absent required value"
            findings.append(f"{step.path}.run.inputs.{raw_name}: {detail}")
    return findings


def _conditional_consumer_findings(steps: tuple[_Step, ...]) -> list[str]:
    """Reject every consumer of a conditional step's output the design does not admit.

    A conditional step's output may be null at runtime (design §6, Topology). That null is admitted only at a
    workflow output (which reports null) or an optional val input of a later step whose use the absent-optional
    lowering already admits (never referenced, or referenced only by a boolean-flag token or its own optional
    binding). Every other consumer is rejected by name, as is an array-typed output of a conditional step outside
    scatter, where the sentinel would collide with a genuinely empty array.
    """
    findings: list[str] = []
    conditional: set[str] = set()
    for step in steps:
        if step.node.condition is None or step.tool.get("class") != "CommandLineTool":
            continue
        conditional.add(step.node.name)
        outputs = step.tool.get("outputs", {})
        if isinstance(outputs, Mapping):
            for raw_name, definition in outputs.items():
                if isinstance(definition, Mapping) and _is_array_type(_required_type(definition.get("type"))):
                    findings.append(f"{step.path}.out.{raw_name}: array-typed output of a conditional step is not "
                                    "supported outside scatter")
    if not conditional:
        return findings
    for step in steps:
        for port, sources in step.wired():
            consumes = any("/" in source and source.rsplit("/", maxsplit=1)[0] in conditional for source in sources)
            if not consumes or _gives_non_null(port):
                continue
            path = f"{step.path}.in.{port.name}"
            tool_inputs = step.tool.get("inputs", {})
            definition = tool_inputs.get(port.name) if isinstance(tool_inputs, Mapping) else None
            if not isinstance(definition, Mapping):
                continue
            cwl_type = definition.get("type")
            if _is_array_type(_required_type(cwl_type)):
                findings.append(f"{path}: an array-typed port cannot consume a conditional step's output; [] already "
                                "stands for a skipped step")
                continue
            try:
                qualifier = cwl_type_to_nf_qualifier(cwl_type)
            except ValueError:
                qualifier = None
            if qualifier == "path":
                findings.append(f"{path}: a path consumer of a possibly-null conditional step output is not supported")
                continue
            if not _is_optional(cwl_type):
                findings.append(f"{path}: a non-optional port cannot consume a possibly-null conditional step output")
                continue
            safe_names = _safe_absence_names(step.tool)
            if safe_names is None:
                continue
            try:
                name = _identifier(port.name, context="input reference")
            except ValueError:
                name = None
            if name not in safe_names:
                findings.append(f"{path}: this input's use of a possibly-null conditional step output is not one the "
                                "absent-optional lowering admits")
    return findings


def _is_flag_binding(definition: Mapping[str, Any]) -> bool:
    """Whether a tool input lowers to a conditional flag token."""
    match definition.get("inputBinding"):
        case Mapping() as binding:
            pass
        case _:
            return False
    return (
        _required_type(definition.get("type")) == "boolean"
        and binding.get("valueFrom") is None
        and isinstance(binding.get("prefix"), str)
        and bool(binding.get("prefix"))
    )


def _flag_source_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[str]:
    """Require a boolean source for every input lowered to a flag token.

    A flag renders as a Groovy ternary over its channel value, so a non-boolean source would let Groovy truthiness
    decide the flag: a staged path is always truthy and the strings ``"false"`` and ``"0"`` are too.
    """
    source_types = _source_types(workflow, steps)
    scattered_by_index = _scattered_names_by_index(steps)
    findings: list[str] = []
    for step in steps:
        tool_inputs = step.tool.get("inputs", {})
        if not isinstance(tool_inputs, Mapping):
            continue
        bound = {port.name: (port, sources) for port, sources in step.bound()}
        scattered = scattered_by_index.get(step.index, set())
        for raw_name, definition in tool_inputs.items():
            if not isinstance(definition, Mapping) or not _is_flag_binding(definition) or raw_name not in bound:
                continue
            sources = _wired(*bound[raw_name])
            if sources is None:
                # Every shape the idioms do not read is already reported, by path, by the unconsumed-fields pass.
                continue
            for source in sources:
                declared = source_types.get(source)
                if raw_name in scattered:
                    # A scattered port receives one element, so the array's item type is what must be boolean.
                    declared = _scattered_element_type(declared)
                if declared is not None and _required_type(declared) == "boolean":
                    continue
                findings.append(f"{step.path}.in.{raw_name}: a boolean flag input requires a boolean source; "
                                f"{source!r} declares {declared!r}")
    return findings


def _text_capture_endpoints(steps: tuple[_Step, ...]) -> set[str]:
    """Every ``<step>/<port>`` source that captures file text."""
    endpoints: set[str] = set()
    for step in steps:
        if step.node.condition is not None:
            # A conditional step's captured-text output is the one proven exception (design §6, Topology):
            # _conditional_consumer_findings already governs its downstream sinks with the admission rules a
            # possibly-null value requires.
            continue
        outputs = step.tool.get("outputs", {})
        if not isinstance(outputs, Mapping):
            continue
        for raw_port, definition in outputs.items():
            if not isinstance(definition, Mapping):
                continue
            try:
                capture = _output_capture(raw_port, definition)
            except ValueError:
                continue
            if capture == "text":
                endpoints.add(f"{step.node.name}/{raw_port}")
    return endpoints


def _text_capture_sink_findings(steps: tuple[_Step, ...]) -> list[str]:
    """Restrict a captured value's sink to a workflow output.

    Every process output before this lowering carried the ``path`` qualifier, so the executable graph has no
    source-to-destination qualifier agreement check for process edges to lean on.
    """
    endpoints = _text_capture_endpoints(steps)
    return [
        f"{step.path}.in.{port.name}: {source!r} captures file text; its only approved sink is a workflow output, "
        "because the executable graph has no qualifier agreement check for process edges"
        for step in steps for port, sources in step.wired() for source in sources if source in endpoints
    ]


def _literal_text(value: Any) -> str | None:
    """The text of a template with no input reference, or None."""
    try:
        segments = _template(value, context="name").segments
    except ValueError:
        return None
    literals = [segment.value for segment in segments if isinstance(segment, NfLiteral)]
    return "".join(literals) if len(literals) == len(segments) else None


def _literal_outputs(tool: Mapping[str, Any]) -> dict[str, str]:
    """Each output whose glob is one literal name, with no wildcard, by port."""
    outputs = tool.get("outputs")
    names: dict[str, str] = {}
    for raw_port, definition in outputs.items() if isinstance(outputs, Mapping) else ():
        binding = definition.get("outputBinding") if isinstance(definition, Mapping) else None
        glob = _literal_text(binding.get("glob")) if isinstance(binding, Mapping) else None
        if glob and not GLOB_WILDCARDS & set(glob):
            names[raw_port] = glob
    return names


def _staged_names(step: _Step, produced: Mapping[str, str]) -> dict[str, tuple[set[str], bool]]:
    """The names known at compile time that each File or Directory input is linked under in the task directory, and
    whether the link is an InitialWorkDirRequirement entry.

    An entry with a literal name is linked under it; any other input not in an array is linked under its basename,
    which is a literal output name when its source produces one. A writable entry is a copy, not a link, and an
    array's files are linked in numbered directories.
    """
    inputs = step.tool.get("inputs")
    if not isinstance(inputs, Mapping):
        return {}
    try:
        entries = _iwdr_entries(step.tool)
    except ValueError:  # its own finding; what it stages is unknown
        return {}
    wired = {port.name: sources for port, sources in step.wired()}
    staged: dict[str, tuple[set[str], bool]] = {}
    for raw_name, definition in inputs.items():
        try:
            if not isinstance(definition, Mapping) or cwl_type_to_nf_qualifier(definition.get("type")) != "path":
                continue
            entry = entries.get(_identifier(raw_name, context="tool input"))
        except ValueError:  # an array, or what another finding names
            continue
        sources = wired.get(raw_name)
        if sources is None:  # unbound, or no source of a shape the idioms read
            continue
        if entry is not None and entry.copy:
            continue
        staged[raw_name] = (
            {entry.name} if entry is not None and entry.name is not None
            else {produced[source] for source in sources if source in produced},
            entry is not None,
        )
    return staged


def _staged_name_findings(steps: tuple[_Step, ...]) -> list[str]:
    """Name each file a task writes at or under a name one of its inputs is linked under.

    Nextflow links a File or Directory input into the task directory, so a command that writes the link's name, or
    a name inside a linked directory, writes through it into the file the input names: an upstream task's output, or
    a workflow input. Only a name known at compile time is checked: a literal stdout, stderr or glob against a
    literal entry name or a literal upstream glob. A glob only collects an entry, which cwltool stages in the output
    directory too, so an entry is checked against stdout and stderr alone. A name a workflow input gives, or a
    pattern or a reference makes, is known only when the task runs.
    """
    produced = {f"{step.node.name}/{raw_port}": glob.rsplit("/", maxsplit=1)[-1]
                for step in steps for raw_port, glob in _literal_outputs(step.tool).items()}
    findings: list[str] = []
    for step in steps:
        path = f"{step.path}.run"
        # Each name once, where it is first written: a stdout's glob names it again.
        written: dict[str, tuple[str, str, bool]] = {}
        for where, name, collects in [
            *((f"{path}.{stream}", _literal_text(step.tool.get(stream)), False) for stream in ("stdout", "stderr")),
            *((f"{path}.outputs.{port}.outputBinding.glob", glob, True)
              for port, glob in _literal_outputs(step.tool).items()),
        ]:
            if name:
                written.setdefault(posixpath.normpath(name), (where, name, collects))
        staged = _staged_names(step, produced)
        findings.extend(
            f"{where}: writing {name!r} writes through {link!r}, the name input {raw_name!r} is linked under in "
            "the task directory, into the file the input names; give the output another name"
            for normal, (where, name, collects) in written.items()
            for raw_name, (links, entry) in staged.items()
            if not (collects and entry)
            for link in links
            if normal == link or normal.startswith(f"{link}/")
        )
    return findings


class _WhenError(ValueError):
    """A step's `when` the target cannot lower."""


def _condition_value(step: _Step) -> Expr:
    """A step's ``when`` as a boolean tree over the inputs the step binds, as CWL types it, by the tool's declaration.

    A step's when is evaluated once per invocation, after inputs are bound.
    """
    condition = step.node.condition
    assert condition is not None
    when = condition.written
    if not isinstance(when, str) or not is_safe_subset_text(when):
        raise _WhenError(f"{when!r} must be a safe JavaScript subset $( … ) expression")
    inputs = _as_mapping(step.tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    bound = {port.name for port, _sources in step.bound()}
    input_types = {
        str(raw_name): definition.get("type") if isinstance(definition, Mapping) else definition
        for raw_name, definition in inputs.items()
        if str(raw_name) in bound
    }
    if condition.expr is None:
        raise _WhenError(f"{when!r} is outside the safe JavaScript subset: {condition.problem}")
    if unbound := sorted(references(condition.expr) - input_types.keys()):
        names = ", ".join(f"inputs.{name}" for name in unbound)
        raise _WhenError(f"{when!r} reads {names}, which the step does not bind")
    try:
        result = check_safe_subset(condition.expr, input_types)
    except ValueError as exc:
        raise _WhenError(f"{when!r} is outside the safe JavaScript subset: {exc}") from exc
    if result != "boolean":
        raise _WhenError(f"{when!r} must compute a boolean, not a {result}")
    return condition.expr


def _process(step: _Step) -> NfProcess:
    return NfProcess(
        name=_identifier(step.node.name, context="workflow step id", variable=False),
        inputs=_ports(step.tool.get("inputs", {}), outputs=False, entries=_iwdr_entries(step.tool)),
        outputs=_ports(step.tool.get("outputs", {}), outputs=True),
        command=_command(step.tool),
        container=_container(step.tool),
        resources=_resources(step.tool),
        condition=_condition_value(step) if step.node.condition is not None else None,
    )


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source, context="workflow input source")
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process)
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port, context="process output source")


def _step_connections(
    steps: tuple[_Step, ...],
    processes: list[NfProcess],
    step_names: Mapping[str, str],
    defaults: Mapping[tuple[str, str], NfWorkflowInputConnection],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    # Only a multi-input scatter carries the invocation index a gather sorts by.
    multi_scattered = {process.name for step, process in zip(steps, processes, strict=True)
                       if step.node.scatter is not None}
    for step, process in zip(steps, processes, strict=True):
        scattered = {_identifier(raw_name, context="scattered input")
                     for raw_name in (_scatter_names(step.node) if step.node.scatter is not None else [])}
        # Capability analysis already admitted only an approved method for two or more; at one scattered input the
        # three methods coincide.
        method = step.node.scatter.method if step.node.scatter is not None else None
        scatter_method = str(method or "dotproduct") if len(scattered) > 1 else "dotproduct"
        for port, found in step.bound():
            destination_port = _identifier(port.name, context="process input destination")
            spelled = _wired(port, found)
            if spelled is None:
                raise ValueError(f"step input {process.name}.{destination_port} must define source")
            sources: list[NfWorkflowInputConnection | NfProcessConnection] = []
            for source in spelled:
                source_process, source_port = _source_endpoint(source, step_names)
                if source_process is None:
                    # Every scatter is indexed, so its gather keeps input order.
                    adapter = scatter_method if destination_port in scattered else None
                    sources.append(NfWorkflowInputConnection(source_port, process.name, destination_port, adapter))
                else:
                    gathers = source_process in multi_scattered
                    sources.append(NfProcessConnection(
                        source_process, source_port, process.name, destination_port,
                        (scatter_method if destination_port in scattered else "gather") if gathers else None,
                    ))
            # Capability analysis admitted only the step inputs the idioms lower.
            link_merge = port.link_merge if port.link_merge is not None else (
                "merge_nested" if len(sources) > 1 else None)
            pick_value = port.pick_value
            if sources and port.has_default:
                # The default takes the place of a null source: the first value that is not null.
                sources.append(defaults[(process.name, destination_port)])
                link_merge, pick_value = "merge_nested", "first_non_null"
            if link_merge is None:
                connections.extend(sources)
            else:
                connections.append(NfStepInputConnection(tuple(sources), str(link_merge),
                                                         None if pick_value is None else str(pick_value)))
    return connections


def _workflow_output_connections(outputs: tuple[tuple[WorkflowOutput, tuple[Through | Crossing, ...]], ...],
                                 step_names: Mapping[str, str]) -> list[NfConnection]:
    connections: list[NfConnection] = []
    for port, found in outputs:
        destination_port = _identifier(port.name, context="workflow output destination")
        if port.source is None:
            raise ValueError(f"workflow output {port.name!r} must define outputSource")
        if isinstance(port.source, str):
            sources = [port.source]  # as written: it names nothing Link could resolve
        elif any(isinstance(source, Crossing) for source in found):
            raise ValueError(f"workflow output {port.name!r} names no step output the target can follow")
        else:
            sources = [_spelled(source) for source in found if isinstance(source, Through)]
        for source in sources:
            source_process, source_port = _source_endpoint(source, step_names)
            if source_process is None:
                raise ValueError(
                    f"workflow output {destination_port!r} directly forwards a workflow input; boundary passthrough "
                    "is not executable on the Nextflow target"
                )
            connections.append(NfWorkflowOutputConnection(source_process, source_port, destination_port))
    return connections


def _connections(
    lowering: "_Lowering",
    processes: list[NfProcess],
    defaults: list[NfWorkflowInputConnection],
) -> list[NfConnection]:
    """Every connection: a default beside a source joins that step input, and every other default is its own."""
    step_names = {step.node.name: process.name for step, process in zip(lowering.steps, processes, strict=True)}
    step_connections = _step_connections(
        lowering.steps, processes, step_names,
        {(default.to_process, default.to_port): default for default in defaults},
    )
    joined = {
        (connection.to_process, connection.to_port)
        for connection in step_connections if isinstance(connection, NfStepInputConnection)
    }
    return [
        *step_connections,
        *_workflow_output_connections(lowering.outputs, step_names),
        *(default for default in defaults if (default.to_process, default.to_port) not in joined),
    ]


def _default_bindings(
    steps: tuple[_Step, ...],
    processes: list[NfProcess],
    workflow_inputs: Iterable[str],
) -> tuple[list[NfWorkflowInputConnection], dict[str, Any], dict[str, str]]:
    """Synthesize a workflow parameter for every default, and a connection for every unwired process input.

    Covers three cases: a step input's default, a declared CWL default on an unwired input, and an unwired optional
    input with no default. Capability analysis has already proven any remaining unwired, no-default optional input
    is a safe absence, so this always contributes a null value for those rather than re-deriving safety. A step
    input's default beside a source is one of that step input's sources (``_step_connections``), so it gets a
    parameter and no connection of its own.

    Each parameter is made as the compiler makes a name, `<step>~<port>`, and normalized; one that normalizes like
    another or like a workflow input is rejected, naming both.
    """
    made: list[tuple[str, str, str, Any, str]] = []
    for step, process in zip(steps, processes, strict=True):
        bound = {port.name: port for port, _sources in step.bound()}
        inputs = _as_mapping(step.tool.get("inputs", {}), error="tool inputs must be a mapping")
        for raw_name, definition in inputs.items():
            if not isinstance(definition, Mapping):
                continue
            port = bound.get(raw_name)
            if port is not None and port.has_default:
                value = _json_value(port.default)
            elif port is not None:
                continue
            elif "default" in definition:
                value = _json_value(definition["default"])
            elif _is_optional(definition.get("type")):
                # Capability analysis has already proven this unwired, no-default optional input is a safe absence.
                # _apply_absent_sentinel converts None to the runtime-proven [] representation once, at the end of
                # the conversion.
                value = None
            else:
                continue
            port_name = _identifier(raw_name, context="defaulted process input")
            made.append((f"{step.node.name}~{raw_name}", process.name, port_name, value,
                         _param_type(definition.get("type"))))
    names = _normalized_identifiers([*workflow_inputs, *(name for name, *_ in made)], context="workflow parameter")
    return (
        [NfWorkflowInputConnection(names[name], process, port) for name, process, port, *_ in made],
        {names[name]: value for name, _process, _port, value, _type in made},
        {names[name]: param_type for name, *_, param_type in made},
    )


def _param_type(cwl_type: Any) -> str:
    """The declared type the generated pipeline checks a parameter's value against, as `PARAM_TYPES` spells it."""
    required = _required_type(cwl_type)
    base = f"{_required_type(_array_item_type(required))}[]" if _is_array_type(required) else required
    return f"{base}?" if _is_optional(cwl_type) else str(base)


def _json_value(value: Any) -> Any:
    match value:
        case Mapping() as mapping:
            return {str(key): _json_value(item) for key, item in mapping.items()}
        case list() as items:
            return [_json_value(item) for item in items]
        case tuple() as items:
            return [_json_value(item) for item in items]
        case PathLike() as path:
            return str(path)
        case _:
            return copy.deepcopy(value)


def _workflow_params(workflow: ResolvedWorkflow) -> dict[str, Any]:
    """Each workflow input's value, by parameter name: the run's, else its default, else None."""
    names = _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    params: dict[str, Any] = {}
    for port in workflow.inputs:
        if port.has_value:
            value = local_locations_as_paths(port.value)
        elif port.declaration.has_default and not port.declaration.shorthand:
            value = port.declaration.default
        else:
            value = None
        params[names[port.name]] = _json_value(value)
    return params


def _workflow_param_types(workflow: ResolvedWorkflow) -> dict[str, str]:
    """Each workflow input's declared type, by parameter name."""
    names = _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    return {names[port.name]: _param_type(port.declaration.type.declared) for port in workflow.inputs}


def _apply_absent_sentinel(params: Mapping[str, Any]) -> dict[str, Any]:
    """Replace every remaining null parameter with the runtime-proven [] sentinel.

    Capability analysis (via _absent_optional_findings, which itself calls _workflow_params and depends on None to
    detect absence) has already proven any null still present here is a safe absent-optional val input. This
    substitution happens only once, after that analysis, so it never interferes with it.
    """
    return {name: (ABSENT_VAL_SENTINEL if value is None else value) for name, value in params.items()}


@dataclass(frozen=True, slots=True)
class _Lowering:
    """A view as the lowering reads it: its tool steps, each tool on the lowering's own copy, and its outputs, each
    source resolved through call boundaries."""

    view: ResolvedWorkflow
    steps: tuple[_Step, ...]
    outputs: tuple[tuple[WorkflowOutput, tuple[Through | Crossing, ...]], ...]
    #: The (call, input) pairs a kept output relays that the call leaves unbound, with no default.
    unbound: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, view: ResolvedWorkflow) -> "_Lowering":
        """The lowering's reading of `view`."""
        if not isinstance(view, ResolvedWorkflow):
            raise TypeError("Nextflow conversion requires a resolved view of a compilation")
        steps = tuple(
            _Step(index, tool, desugar_into_canonical_normal_form(copy.deepcopy(dict(
                _as_mapping(tool.step.run.document, error="a tool step's tool must be a CWL document")))))
            for index, tool in enumerate(tool_steps(view)) if isinstance(tool.step.run, ResolvedTool)
        )
        return cls(view, steps, tuple(zip(view.outputs, output_sources(view), strict=True)),
                   tuple(dict.fromkeys(pair for pairs in output_unbound(view) for pair in pairs)))


_PATH_FIELD = re.compile(r"\.(?P<field>[^.\[]+)(?P<rest>.*)")
_INDEX = re.compile(r"\.?steps\[(?P<index>\d+)\](?P<rest>.*)")


def _with_field(rest: str, reason: str) -> str:
    """`reason`, after the field the rest of a locator names, unless the reason names it first."""
    field = rest.removeprefix(".")
    return reason if not field or reason.startswith(field.rsplit(".", maxsplit=1)[-1]) else f"{field}: {reason}"


def _tool_file(step: ResolvedStep) -> str:
    """The file a step's tool was read from, as a refusal names it."""
    return Path(step.run.path).name if isinstance(step.run, ResolvedTool) else step.name


def _step_named(workflow: ResolvedWorkflow, name: str) -> ResolvedStep | None:
    """The step of `workflow`, at any depth, whose qualified name is `name`."""
    for step in workflow.steps:
        if step.name == name:
            return step
        if isinstance(step.run, ResolvedWorkflow) and (inner := _step_named(step.run, name)) is not None:
            return inner
    return None


def _workflow_refusal(workflow: ResolvedWorkflow, rest: str, reason: str, *, root: str,
                      owner: ResolvedStep | None) -> Refusal:
    """A refusal of a construct of `workflow`, the root or the workflow `owner` calls, at the locator `rest`."""
    step = owner.name if owner is not None else None
    found = _PATH_FIELD.fullmatch(rest)
    field, after = (found["field"], found["rest"]) if found else ("", "")
    if field in ("inputs", "outputs"):
        construct = Construct.WORKFLOW_INPUT if field == "inputs" else Construct.WORKFLOW_OUTPUT
        named = _PATH_FIELD.fullmatch(after)
        if named is None:
            return Refusal(construct, None, root, step, workflow.span, reason)
        port = next((port for port in (workflow.inputs if field == "inputs" else workflow.outputs)
                     if port.name == named["field"]), None)
        made = port.made_from if isinstance(port, WorkflowInput) else None
        if made is not None and (maker := _step_named(workflow, made[0])) is not None:
            # A made input has no line of its own: its value was written on the step input it was made from.
            written = next((each.span for each in maker.inputs if each.name == made[1]), None)
            return Refusal(construct, named["field"], root, maker.name, written or maker.span,
                           _with_field(named["rest"], reason))
        return Refusal(construct, named["field"], root, step, (port.span if port is not None else None)
                       or workflow.span, _with_field(named["rest"], reason))
    if field in ("requirements", "hints"):
        named = _PATH_FIELD.fullmatch(after)
        return Refusal(Construct.REQUIREMENT, named["field"] if named else None, root, step, workflow.span,
                       _with_field(named["rest"] if named else "", reason))
    if field == "steps":
        return Refusal(Construct.STEP, None, root, step, workflow.span, reason)
    return Refusal(Construct.WORKFLOW, None, root, step, workflow.span, _with_field(rest, reason))


# pylint: disable-next=too-many-return-statements
def _step_refusal(node: ResolvedStep, rest: str, reason: str, *, root: str) -> Refusal:
    """A refusal of `node` or a construct of it, at the locator `rest`."""
    if isinstance(node.run, ResolvedWorkflow) and rest.startswith(".run"):
        return _within(node.run, rest.removeprefix(".run"), reason, root=root, owner=node)
    found = _PATH_FIELD.fullmatch(rest)
    field, after = (found["field"], found["rest"]) if found else ("", "")
    own = Construct.CALL if isinstance(node.run, ResolvedWorkflow) else Construct.STEP
    match field:
        case "when":
            return Refusal(Construct.WHEN, None, root, node.name, node.span, _with_field(after, reason))
        case "scatter" | "scatterMethod":
            return Refusal(Construct.SCATTER, None, root, node.name, node.span, reason)
        case "in":
            named = _PATH_FIELD.fullmatch(after)
            port = next((port for port in node.inputs if named and port.name == named["field"]), None)
            return Refusal(Construct.STEP_INPUT, named["field"] if named else None, root, node.name,
                           port.span or node.span if port is not None else node.span,
                           _with_field(named["rest"] if named else "", reason))
        case "run":
            field_path = after.removeprefix(".")
            subject = f"{_tool_file(node)}: {field_path}" if field_path else _tool_file(node)
            return Refusal(Construct.TOOL, subject, root, node.name, node.span, reason)
        case "requirements" | "hints":
            return Refusal(Construct.REQUIREMENT, None, root, node.name, node.span, _with_field(after, reason))
    return Refusal(own, None, root, node.name, node.span, _with_field(rest, reason))


def _within(workflow: ResolvedWorkflow, rest: str, reason: str, *, root: str, owner: ResolvedStep | None) -> Refusal:
    """A refusal at the locator `rest` within `workflow`: one of its steps', or its own."""
    if (found := _INDEX.fullmatch(rest)) is not None:
        return _step_refusal(workflow.steps[int(found["index"])], found["rest"], reason, root=root)
    return _workflow_refusal(workflow, rest, reason, root=root, owner=owner)


def _call_locator(workflow: ResolvedWorkflow, name: str, path: str = "workflow") -> str | None:
    """The locator of the call of `workflow`, at any depth, whose qualified name is `name`."""
    for index, step in enumerate(workflow.steps):
        if step.name == name:
            return f"{path}.steps[{index}]"
        inside = f"{path}.steps[{index}].run"
        if isinstance(step.run, ResolvedWorkflow) and (found := _call_locator(step.run, name, inside)):
            return found
    return None


def _unbound_output_findings(lowering: _Lowering) -> list[str]:
    """A finding for each input a call leaves unbound, with no default, that a kept workflow output relays.

    Such an output has no source to follow, so the input is refused once, at the call, however many outputs relay it
    and through whatever chain, not dropped.
    """
    reason = "a workflow output relays it, but nothing binds it and it has no default"
    return [f"{_call_locator(lowering.view, call)}.in.{name}: {reason}" for call, name in lowering.unbound]


def _refusal(finding: str, lowering: _Lowering) -> Refusal:
    """A finding, which names what it refuses by its locator, as the refusal of that construct.

    A locator is the construct's path in the view: `steps[i]` the i-th tool step, at any depth, as the lowering
    numbers them; `workflow.steps[i]` the i-th step of a workflow, `.run` a call's workflow; then the construct's
    fields. A refusal names the step and construct, by the names their author wrote, at the author's line.
    """
    locator, reason = finding.split(": ", maxsplit=1)
    root = lowering.view.name
    if (found := _INDEX.fullmatch(locator)) is not None:
        return _step_refusal(lowering.steps[int(found["index"])].node, found["rest"], reason, root=root)
    return _within(lowering.view, locator.removeprefix("workflow"), reason, root=root, owner=None)


@dataclass(frozen=True, slots=True)
class _Analysis:
    """What the analysis of a view found: every refusal, the processes it lowered, and the notes it made."""

    refusals: tuple[Refusal, ...]
    processes: tuple[NfProcess, ...]
    notes: tuple[Diagnostic, ...]


def _analysis(lowering: _Lowering) -> _Analysis:
    """Every construct of the view the target cannot lower, and each tool step lowered to its process."""
    view, steps = lowering.view, lowering.steps
    findings = _call_findings(view)
    notes: list[Diagnostic] = []
    processes: list[NfProcess] = []
    for step in steps:
        if step_findings := [*_step_capability_findings(step), *_tool_capability_findings(step.tool, path=step.path)]:
            findings.extend(step_findings)
            continue
        try:
            processes.append(_process(step))
        except _WhenError as error:
            findings.append(f"{step.path}.when: {error}")
        except ValueError as error:  # lowering and the model reject what no source pass can see
            findings.append(f"{step.path}: {error}")
        notes.extend(_cuda_notes(step.node.name, step.tool))
    findings.extend(_workflow_capability_findings(view, steps))
    findings.extend(_unbound_output_findings(lowering))
    findings.extend(_absent_optional_findings(view, steps))
    findings.extend(_conditional_consumer_findings(steps))
    findings.extend(_flag_source_findings(view, steps))
    findings.extend(_scatter_findings(view, steps))
    findings.extend(_step_input_findings(view, steps))
    findings.extend(_text_capture_sink_findings(steps))
    findings.extend(_staged_name_findings(steps))
    refusals = [_refusal(finding, lowering) for finding in findings]
    if view.policy.partial_failure:
        refusals.append(Refusal(Construct.POLICY, "partial failure", view.name, None, view.span,
                                "partial failure is a policy of the CWL target"))
    return _Analysis(tuple(refusals), tuple(processes), tuple(notes))


class NextflowTarget:  # pylint: disable=too-few-public-methods
    """The Nextflow target: it lowers a resolved view to the executable Nextflow model (design §7)."""

    name: Final = "nextflow"

    def lower(self, view: ResolvedWorkflow) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
        """Lower `view` without repeating core semantics.

        Every unsupported source semantic, including one that only lowering or model validation detects, is
        refused at its author's line and aggregated with the rest before the workflow is assembled.

        Args:
            view (ResolvedWorkflow): The resolved view of a finished compilation (`compiler.resolved_view`).

        Raises:
            TypeError: If the argument is not a resolved view.
            NotLowered: If there is any refusal; it holds every one.

        Returns:
            tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]: The validated executable representation, and
                the notes the lowering made, such as a CUDA field it does not check.
        """
        lowering = _Lowering.of(view)
        analysis = _analysis(lowering)
        if analysis.refusals:
            raise NotLowered("Nextflow", analysis.refusals)
        processes = list(analysis.processes)
        try:
            params = _workflow_params(view)
            default_connections, default_params, default_types = _default_bindings(
                lowering.steps, processes, [port.name for port in view.inputs])
            params.update(default_params)
            param_types = {**_workflow_param_types(view), **default_types}
            name = _identifier(view.name, context="workflow name", variable=False)
            while name in {process.name for process in processes}:  # a step may share the workflow's name
                name = f"_{name}"
            return ExecutableNextflowWorkflow(
                name=name,
                processes=processes,
                connections=_connections(lowering, processes, default_connections),
                # A default is as authored; each value is held as a value written on a step is (design §4.2,
                # Losses): coerced to its declared type, so an int given for a float is the float, and a local
                # location a path.
                params=_apply_absent_sentinel({key: local_locations_as_paths(coerce_job_value(
                    AuthoredName(key), PortDeclaration(PortType(param_types[key])), value))
                    for key, value in params.items()}),
                param_types=param_types,
            ), analysis.notes
        except ValueError as error:
            raise NotLowered("Nextflow", (Refusal(Construct.WORKFLOW, None, view.name, None, view.span,
                                                  str(error)),)) from None


#: The Nextflow target.
NEXTFLOW: Final = NextflowTarget()


def nextflow_names(view: ResolvedWorkflow) -> dict[str, dict[str, str]]:
    """The Sophios name of each process and workflow output the lowering of `view` makes, by its Nextflow name.

    A process is named for its step's qualified id and a workflow output for its own name, normalized as the
    lowering normalizes them.
    """
    return {
        "processes": {_identifier(tool.step.name, context="workflow step id", variable=False): tool.step.name
                      for tool in tool_steps(view) if isinstance(tool.step.run, ResolvedTool)},
        "outputs": {_identifier(port.name, context="workflow output destination"): port.name for port in view.outputs},
    }
