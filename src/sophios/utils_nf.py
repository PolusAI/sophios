"""Lower a resolved Sophios compilation into the Nextflow representation."""

from collections.abc import Callable, Iterable, Iterator, Mapping
import copy
from dataclasses import dataclass, replace
import math
from os import PathLike
import re
from types import MappingProxyType
from typing import Any

from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.names import Names
from .ir.types import Direction, Edge, PortId, SourceList, StepNode, WorkflowGraph
from .lang.diagnostics import Diagnostic, Locator, Severity
from .lang.error_codes import SophiosErrorCode
from .nf_expr import (
    Expr, check as check_safe_subset, is_safe_subset_text, parse as parse_safe_subset, references,
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
)
from .utils_cwl import desugar_into_canonical_normal_form


@dataclass(frozen=True, slots=True)
class CompiledNextflowSource:
    """Closed compiler output consumed by capability analysis and lowering.

    Production values are projected from :class:`CompilationResult`. Tests may
    build this small boundary directly when they need an intentionally invalid
    compiled document that the core compiler correctly refuses to construct.
    """

    name: str
    workflow: Mapping[str, Any]
    tools: tuple[Mapping[str, Any], ...]
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.workflow.get("class") != "Workflow":
            raise ValueError("compiled source root must be a CWL Workflow")
        steps = self.workflow.get("steps", [])
        if not isinstance(steps, list):
            raise TypeError("compiled CWL Workflow steps must be a list")
        if len(steps) != len(self.tools):
            raise ValueError("compiled CWL steps do not match compiled tool artifacts")


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
    match cwl_type:
        case str() as type_name if type_name.endswith("?"):
            return type_name[:-1]
        case list() as union if "null" in union:
            remaining = [item for item in union if item != "null"]
            if len(remaining) != 1:
                raise ValueError(f"unsupported optional CWL union {cwl_type!r}")
            return remaining[0]
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


def _channel_shape(cwl_type: Any) -> str:
    """Return the channel semantics one non-array CWL type lowers to.

    The same qualifier/path-kind pair the executable graph compares when it
    checks that every sink of one workflow parameter agrees, rendered for
    diagnostics.
    """
    qualifier = cwl_type_to_nf_qualifier(cwl_type)
    path_kind = _PATH_KINDS.get(_required_type(cwl_type))
    return f"{qualifier}[{path_kind}]" if path_kind else qualifier


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
    # A `$` not followed by `(` or `{` is plain text, as in CWL.
    if interpolated and _EXPRESSION_START.search(_INTERPOLATION.sub("", text)):
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
    if _requirement(tool, "DockerRequirement") is not None:
        # In a container, the image's entrypoint runs a command made only of conditional tokens.
        if not tokens:
            raise ValueError(
                "a containerized tool with no baseCommand, arguments or bound inputs is not supported: CWL "
                "gives the container no arguments, so the image runs its CMD, after its ENTRYPOINT if it has one, "
                "which the Nextflow task does not; name the program in baseCommand"
            )
    elif not any(isinstance(token, (NfTemplate, NfShellLiteral)) for token in tokens):
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
# The CUDA fields with no Nextflow counterpart: each one present is reported with a note.
_UNCHECKED_CUDA_FIELDS = ("cudaVersionMin", "cudaComputeCapability")


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
    _CUDA_REQUIREMENT: {"cudaDeviceCountMin": _device_count, "cudaDeviceCountMax": _device_count},
}


def _resources(tool: Mapping[str, Any]) -> NfResources:
    resource = _requirement(tool, "ResourceRequirement") or {}
    cpus = _resource_value(resource, "coresMin", "coresMax")
    memory = _resource_value(resource, "ramMin", "ramMax")
    rendered_cpus = None if cpus is None else _cpu_resource(cpus)
    rendered_memory = None if memory is None else _memory(memory)
    return NfResources(rendered_cpus, rendered_memory, _gpu_count(tool))


def _cuda_notes(step: Mapping[str, Any], tool: Mapping[str, Any]) -> list[Diagnostic]:
    """A note for each CUDA field the generated pipeline does not check."""
    cuda = _requirement(tool, _CUDA_REQUIREMENT) or {}
    return [
        Diagnostic(
            Severity.NOTE, SophiosErrorCode.UNCHECKED_CUDA_FIELD,
            f"{_CUDA_REQUIREMENT} {name} {cuda[name]!r} is not checked: the Nextflow accelerator directive "
            "asks only for a number of GPUs",
            locator=Locator(step=str(step.get("id"))),
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
        if _required_type(declared_type) in _NUMERIC_TEXT_CAPTURE_TYPES:
            raise ValueError(
                f"outputEval {text!r} captures file text, and parsing that text into a "
                f"number is computation rather than a projection; output {raw_name!r} "
                f"declares {declared_type!r}. Declare type: string and parse downstream"
            )
        if _required_type(declared_type) != _TEXT_CAPTURE_TYPE:
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


def _local_locations_as_paths(value: Any) -> Any:
    """Rewrite a File or Directory object's plain local ``location`` as its ``path``, and drop a File's ``format``.

    The Python API writes file values in CWL's ``location`` form; for a local
    path with no URI scheme the two keys mean the same file. A URI keeps its
    ``location`` and stays rejected, since the model does not represent schemes.
    A File value's ``format`` is compile-time, as the input's is (design §3).
    """
    match value:
        case {"class": "File", "format": _}:
            return _local_locations_as_paths({key: item for key, item in value.items() if key != "format"})
        case {"class": "File" | "Directory", "location": str() as location} if (
            set(value) == {"class", "location"} and "://" not in location
        ):
            return {"class": value["class"], "path": location}
        case Mapping():
            return {key: _local_locations_as_paths(item) for key, item in value.items()}
        case list():
            return [_local_locations_as_paths(item) for item in value]
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


def _path_reference_findings(tool: Mapping[str, Any], *, path: str) -> list[str]:
    """Check every input reference against the type it reads (design §6).

    ``.path`` and ``.basename`` need a File or Directory input. A bare File or
    Directory reference must be the whole value: embedded in other text, CWL
    renders the File object as JSON where the backend would render its staged
    name. Read from the authored text, because ``_template`` lowers ``.path``
    to a plain reference and the model could no longer tell the two apart.
    """
    inputs = tool.get("inputs", {})
    if not isinstance(inputs, Mapping):
        return []
    findings: list[str] = []
    for value, position in _template_positions(tool, path=path):
        whole = isinstance(value, str) and _INPUT_EXPRESSION.fullmatch(value.strip()) is not None
        references = (match.group("name", "suffix") for match in _INTERPOLATION.finditer(str(value)))
        for name, suffix in dict.fromkeys(reference for reference in references if reference[0]):
            definition = inputs.get(name)
            if not isinstance(definition, Mapping):
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
            elif suffix is None and is_path and not whole:
                findings.append(
                    f"{position}: $(inputs.{name}) embedded in other text renders a File object as JSON in "
                    f"CWL; use $(inputs.{name}.path) or $(inputs.{name}.basename), or the reference alone"
                )
    return findings


def _graph_steps_with_artifacts(
    graph: WorkflowGraph,
    artifact: CompilationArtifact,
) -> list[tuple[StepNode, Mapping[str, Any], CompilationArtifact]]:
    """Pair every resolved leaf step with its emitted step and the tool artifact it executes."""
    leaves: list[tuple[StepNode, Mapping[str, Any], CompilationArtifact]] = []
    steps = zip(graph.steps, _workflow_steps(artifact.cwl), artifact.children, strict=True)
    for step, emitted, child_artifact in steps:
        if step.run is None:
            raise ValueError(f"resolved step {step.id} has no run")
        child_graph = step.run.child
        if child_graph is None:
            leaves.append((step, emitted, child_artifact))
            continue
        leaves.extend(_graph_steps_with_artifacts(child_graph, child_artifact))
    return leaves


def _graph_edges(graph: WorkflowGraph) -> tuple[Edge, ...]:
    """Return the already-resolved edges in the whole semantic graph tree."""
    return graph.edges + tuple(edge for child in graph.children for edge in _graph_edges(child))


def _expanded_boundary_port(
    port_id: PortId,
    steps: Mapping[Any, StepNode],
) -> tuple[PortId, ...]:
    """Follow a resolved workflow-call boundary to its declared leaf port."""
    step = steps.get(port_id.step)
    if step is None or step.run is None or step.run.child is None:
        return (port_id,)
    child = step.run.child
    if port_id.direction is Direction.INPUT:
        mapped = next((ports for name, ports in child.input_mapping if name == port_id.port), ())
        return tuple(
            leaf
            for mapped_port in mapped
            for leaf in _expanded_boundary_port(mapped_port, steps)
        ) or (port_id,)
    mapped_output = next(
        (mapped for name, mapped in child.output_mapping if name == port_id.port),
        None,
    )
    return (
        _expanded_boundary_port(mapped_output, steps)
        if mapped_output is not None
        else (port_id,)
    )


def _source_port_name(port_id: PortId, leaf_names: Mapping[Any, str], names: Names) -> str:
    process = leaf_names.get(port_id.step)
    if process is None:
        raise ValueError(f"resolved edge names non-executable source step {port_id.step}")
    return f"{process}/{names.port(port_id.port)}"


def _replace_step_source(raw: Any, source: str | list[str]) -> Any:
    """Retain completed step-input metadata while replacing its resolved source, or sources in order."""
    if isinstance(raw, Mapping):
        return {**copy.deepcopy(dict(raw)), "source": source}
    return source


def _record_sources(
    step: StepNode,
    *,
    names: Names,
    leaf_names: Mapping[Any, str],
    all_steps: Mapping[Any, StepNode],
    feeders: Mapping[PortId, str | PortId],
) -> dict[PortId, list[str]]:
    """The resolved sources of each of a leaf step's step-input records, in the order they were written.

    An entry naming an input of the leaf's own workflow is fed through the
    step that calls that workflow: by an edge, by a root input, or by that
    step's own workflow's input in turn (``feeders``).
    """
    caller = step.id.namespace.parts[-1] if step.id.namespace.parts else None

    def fed(port: PortId) -> str:
        feeder = feeders.get(port)
        if feeder is None:
            raise ValueError(f"resolved input {port} has no source")
        return feeder if isinstance(feeder, str) else fed(feeder)

    def resolved(item: Any, written: Any) -> str:
        match item:
            case Edge(source=source):
                expanded = _expanded_boundary_port(source, all_steps)
                if len(expanded) != 1:
                    raise ValueError(f"resolved edge source {source} is not singular")
                return _source_port_name(expanded[0], leaf_names, names)
            case None:
                if caller is None:
                    return names.port(written.name)
                return fed(PortId(caller, Direction.INPUT, written.name))
            case _:
                raise ValueError(f"step input of {step.id} has an unresolved source")

    return {
        binding.sink: [
            resolved(item, written)
            for item, written in zip(binding.resolution.items, getattr(binding.value, "sources", ()), strict=True)
        ]
        for binding in step.bindings
        if isinstance(binding.resolution, SourceList)
    }


def _leaf_step_document(
    step: StepNode,
    emitted: Mapping[str, Any],
    *,
    names: Names,
    edge_sources: Mapping[PortId, str],
    boundary_sources: Mapping[PortId, str],
    record_sources: Mapping[PortId, list[str]],
) -> dict[str, Any]:
    """Project one resolved leaf step without repeating inference or inlining."""
    ports = {names.port(port.id.port): port for port in step.inputs}
    raw_inputs = _as_mapping(emitted.get("in", {}), error="compiled step inputs must be a mapping")
    inputs: dict[str, Any] = {}
    for port_name, raw in raw_inputs.items():
        port = ports.get(port_name)
        source: str | list[str] | None = None
        if port is not None and port.id in record_sources:
            ordered = record_sources[port.id]
            # A record keeps the spelling it was written in: a list, or one source.
            source = ordered if isinstance(raw.get("source"), list) else (ordered[0] if ordered else None)
        elif port is not None:
            source = edge_sources.get(port.id, boundary_sources.get(port.id))
        inputs[port_name] = copy.deepcopy(raw) if source is None else _replace_step_source(raw, source)
    document = copy.deepcopy(dict(emitted))
    document["id"] = names.qualified(step.id)
    document["in"] = inputs
    return document


def compilation_result_source(result: CompilationResult) -> CompiledNextflowSource:
    """Project the completed core graph into the backend's closed source seam."""
    graph = result.graph
    names = Names.of(graph)
    leaves = _graph_steps_with_artifacts(graph, result.artifact)
    all_steps = {step.id: step for step in graph.all_steps}
    leaf_names = {step.id: names.qualified(step.id) for step, _emitted, _artifact in leaves}
    # A record's several sources reach one port; they are resolved in order below.
    records = {
        binding.sink for step, _emitted, _artifact in leaves for binding in step.bindings
        if isinstance(binding.resolution, SourceList)
    }
    # What feeds each input of a nested workflow's calling step.
    feeders: dict[PortId, str | PortId] = {}

    edge_sources: dict[PortId, str] = {}
    for edge in _graph_edges(graph):
        sources = _expanded_boundary_port(edge.source, all_steps)
        sinks = _expanded_boundary_port(edge.sink, all_steps)
        if len(sources) != 1:
            raise ValueError(f"resolved edge source {edge.source} is not singular")
        source = _source_port_name(sources[0], leaf_names, names)
        feeders[edge.sink] = source
        for sink in sinks:
            if sink in records:
                continue
            previous = edge_sources.setdefault(sink, source)
            if previous != source:
                raise ValueError(f"resolved input {sink} has conflicting sources")

    boundary_sources: dict[PortId, str] = {}
    for name, mapped_ports in graph.input_mapping:
        for mapped_port in mapped_ports:
            feeders.setdefault(mapped_port, names.port(name))
            for sink in _expanded_boundary_port(mapped_port, all_steps):
                if sink in records:
                    continue
                previous = boundary_sources.setdefault(sink, names.port(name))
                if previous != names.port(name):
                    raise ValueError(f"resolved input {sink} has conflicting workflow parameters")
    for caller in all_steps.values():
        if caller.run is not None and caller.run.child is not None:
            for name, mapped_ports in caller.run.child.input_mapping:
                for mapped_port in mapped_ports:
                    feeders.setdefault(mapped_port, PortId(caller.id, Direction.INPUT, name))

    workflow = copy.deepcopy(dict(result.artifact.cwl))
    workflow["steps"] = [
        _leaf_step_document(
            step,
            emitted,
            names=names,
            edge_sources=edge_sources,
            boundary_sources=boundary_sources,
            record_sources=_record_sources(
                step, names=names, leaf_names=leaf_names, all_steps=all_steps, feeders=feeders
            ),
        )
        for step, emitted, _artifact in leaves
    ]

    outputs = workflow.get("outputs", {})
    if isinstance(outputs, Mapping):
        projected_outputs: dict[str, Any] = {}
        mapped_outputs = {names.port(name): port for name, port in graph.output_mapping}
        for name, raw_definition in outputs.items():
            definition = copy.deepcopy(raw_definition)
            output_port = mapped_outputs.get(str(name))
            if output_port is not None:
                expanded = _expanded_boundary_port(output_port, all_steps)
                if len(expanded) != 1:
                    raise ValueError(f"workflow output {name!r} is not singular")
                source = _source_port_name(expanded[0], leaf_names, names)
                if isinstance(definition, Mapping):
                    definition = {**dict(definition), "outputSource": source}
            projected_outputs[str(name)] = definition
        workflow["outputs"] = projected_outputs

    return CompiledNextflowSource(
        graph.name or result.artifact.name,
        workflow,
        tuple(
            desugar_into_canonical_normal_form(copy.deepcopy(dict(artifact.cwl)))
            for _step, _emitted, artifact in leaves
        ),
        copy.deepcopy(dict(result.artifact.job_inputs)),
    )


def _nested_workflow_capability_findings(
    graph: WorkflowGraph,
    artifact: CompilationArtifact,
    *,
    path: str = "workflow",
) -> list[str]:
    """Reject nested-workflow semantics the flat executable graph would erase.

    The core compiler owns composition, edge resolution, and namespace
    identity.  The backend still owns its capability boundary: metadata on a
    workflow call or child workflow must be either consumed here or rejected
    before the call node is projected away.
    """
    findings: list[str] = []
    for index, (step, emitted, child_artifact) in enumerate(
        zip(graph.steps, _workflow_steps(artifact.cwl), artifact.children, strict=True)
    ):
        step_path = f"{path}.steps[{index}]"
        if step.run is None or step.run.child is None:
            continue
        if emitted.get("when") is not None:
            findings.append(
                f"{step_path}.when: a when on a nested-workflow step is not supported; "
                "put it on a tool step"
            )
        if emitted.get("scatter") is not None:
            findings.append(
                f"{step_path}.scatter: scatter on a nested workflow step is deferred "
                "beyond this lowering; scattering a sub-DAG is not the single-process "
                "shape scatter supports"
            )
        elif emitted.get("scatterMethod") is not None:
            findings.append(
                f"{step_path}.scatterMethod: scatterMethod without scatter is not executable"
            )
        findings.extend(
            _unconsumed_field_findings(emitted, "WorkflowStep", path=step_path)
        )
        for raw_name, definition in _as_mapping(
            emitted.get("in", {}), error="compiled step inputs must be a mapping"
        ).items():
            if isinstance(definition, Mapping):
                input_path = f"{step_path}.in.{raw_name}"
                # The projection joins one source to each input a nested workflow step binds.
                merging = sorted(_STEP_INPUT_IDIOM_FIELDS & set(definition))
                if isinstance(definition.get("source"), list) and len(definition["source"]) > 1:
                    merging.append("source")
                findings.extend(
                    f"{input_path}.{name}: a nested workflow step's input takes one source as it is; "
                    "merge on a step inside the nested workflow"
                    for name in merging
                )
                findings.extend(_unconsumed_field_findings(definition, "WorkflowStepInput", path=input_path))

        child_path = f"{step_path}.run"
        child_document = child_artifact.cwl
        findings.extend(
            _unconsumed_field_findings(child_document, "Workflow", path=child_path, kept=_WORKFLOW_CONSUMED_FIELDS)
        )
        findings.extend(_workflow_requirement_findings(child_document, path=child_path))
        for section_name, cwl_class in (("inputs", "WorkflowInputParameter"), ("outputs", "WorkflowOutputParameter")):
            section = child_document.get(section_name, {})
            if not isinstance(section, Mapping):
                findings.append(
                    f"{child_path}.{section_name}: compiled CWL Workflow "
                    f"{section_name} must be a mapping"
                )
                continue
            for raw_name, definition in section.items():
                port_path = f"{child_path}.{section_name}.{raw_name}"
                findings.extend(_parameter_findings(definition, cwl_class, path=port_path))
                # Inlining replaces a nested workflow's input with the calling step's binding,
                # where CWL would apply the input's default to a null one.
                if section_name == "inputs" and isinstance(definition, Mapping) and "default" in definition:
                    findings.append(
                        f"{port_path}.default: a nested workflow input's default is not supported: "
                        "bind the input on the calling step"
                    )
        findings.extend(
            _nested_workflow_capability_findings(
                step.run.child,
                child_artifact,
                path=child_path,
            )
        )
    return findings


def _tool_capability_findings(
    step: Mapping[str, Any],
    tool: Mapping[str, Any],
    *,
    step_index: int,
) -> list[str]:
    """Collect unsupported executable semantics without lowering the tool."""
    path = f"steps[{step_index}]"
    findings: list[str] = []
    if "when" in step and not tool.get("inputs"):
        findings.append(
            f"{path}.when: a conditional step needs at least one input to gate on, "
            "and this step's tool declares none"
        )
    match step.get("in", {}), tool.get("inputs", {}):
        case Mapping() as step_inputs, Mapping() as tool_inputs:
            findings.extend(
                f"{path}.in.{raw_name}: an input the step's tool does not declare is not supported"
                for raw_name in step_inputs
                if raw_name not in tool_inputs
            )
        case _:
            pass

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
    findings.extend(_path_reference_findings(tool, path=path))

    for section_name in ("requirements", "hints"):
        findings.extend(
            _requirement_findings(
                tool.get(section_name), "CommandLineTool", path=f"{path}.run.{section_name}",
                otherwise="{} is not supported by the Nextflow target", tool=tool,
            )
        )
    try:
        renamed_names = {name for name, entry in _iwdr_entries(tool).items() if entry.name is not None}
    except ValueError:
        renamed_names = set()
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
            if binding.get("shellQuote") is False:
                try:
                    _shell_literal_value(raw_name, binding, shell_mode=shell_mode_active)
                except ValueError as exc:
                    findings.append(f"{input_binding_path}.shellQuote: {exc}")
            if (
                _required_type(raw_definition.get("type")) == "boolean"
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


def _workflow_capability_findings(
    workflow: Mapping[str, Any],
    provided_params: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
) -> list[str]:
    """Apply closed-world analysis to workflow, step, and boundary values."""
    findings = _unconsumed_field_findings(workflow, "Workflow", path="workflow", kept=_WORKFLOW_CONSUMED_FIELDS)
    findings.extend(_workflow_requirement_findings(workflow))
    provided = _as_mapping(
        provided_params,
        error="compiled workflow input values must be a mapping",
    )
    if isinstance(inputs := workflow.get("inputs", {}), Mapping):
        findings.extend(
            _identifier_normalization_findings(
                inputs,
                context="workflow input",
                path="workflow.inputs",
            )
        )
        declared_names = {str(name) for name in inputs}
        for raw_name, definition in inputs.items():
            input_path = f"workflow.inputs.{raw_name}"
            findings.extend(_parameter_findings(definition, "WorkflowInputParameter", path=input_path))
            if isinstance(definition, Mapping):
                has_default = "default" in definition
                cwl_type = definition.get("type")
                findings.extend(_default_value_findings(definition, path=input_path))
            else:
                has_default = False
                cwl_type = definition
            if raw_name not in provided and not has_default and not _is_optional(cwl_type):
                findings.append(f"{input_path}: required workflow input value is missing")
            if raw_name in provided and provided[raw_name] is None and not _is_optional(cwl_type):
                findings.append(
                    f"{input_path}: explicit null input values are not valid for a required input"
                )
            elif raw_name in provided and provided[raw_name] is None:
                # An explicit null on an optional input is only safe for
                # specific consuming positions; _absent_optional_findings
                # analyzes each consuming step and port for that.
                pass
            elif raw_name in provided and not _phase1_value_matches(
                cwl_type, provided[raw_name]
            ):
                findings.append(
                    f"{input_path}: supplied value does not match its supported CWL type"
                )
        for extra_name in sorted(set(map(str, provided)) - declared_names):
            findings.append(
                f"workflow.input_values.{extra_name}: value has no declared workflow input"
            )

    if isinstance(outputs := workflow.get("outputs", {}), Mapping):
        findings.extend(
            _identifier_normalization_findings(
                outputs,
                context="workflow output",
                path="workflow.outputs",
            )
        )
        for raw_name, definition in outputs.items():
            findings.extend(
                _parameter_findings(definition, "WorkflowOutputParameter", path=f"workflow.outputs.{raw_name}")
            )

    findings.extend(
        _identifier_normalization_findings(
            [step.get("id") for step in steps],
            context="workflow step",
            path="workflow.steps",
            variable=False,
        )
    )
    for step_index, step in enumerate(steps):
        step_path = f"steps[{step_index}]"
        findings.extend(
            _unconsumed_field_findings(step, "WorkflowStep", path=step_path)
        )
        if "scatterMethod" in step and "scatter" not in step:
            findings.append(
                f"{step_path}.scatterMethod: scatterMethod without scatter is not executable"
            )
        if isinstance(step_inputs := step.get("in", {}), Mapping):
            for raw_name, definition in step_inputs.items():
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition, "WorkflowStepInput", path=f"{step_path}.in.{raw_name}"
                        )
                    )
    return findings


def _workflow_requirement_findings(
    workflow: Mapping[str, Any],
    *,
    path: str = "workflow",
) -> list[str]:
    """Apply closed-world analysis to workflow-level requirements."""
    section = workflow.get("requirements")
    if section is not None and not isinstance(section, (Mapping, list)):
        return [f"{path}.requirements: CWL Workflow requirements must be a mapping or list"]
    return _requirement_findings(
        section, "Workflow", path=f"{path}.requirements",
        otherwise="{} is not supported at the Nextflow workflow level",
    )


_SCATTER_METHODS = frozenset({"dotproduct", "flat_crossproduct", "nested_crossproduct"})


def _scatter_names(step: Mapping[str, Any]) -> list[str]:
    """Return the raw input names one step scatters over.

    A bare name or a one-element list is single-input scatter, where
    scatterMethod is an inert restatement. A list of two or more names is
    multi-input scatter, where scatterMethod decides how the named inputs
    combine; ``_scatter_findings`` rejects the methods this lowering does
    not support.
    """
    match step.get("scatter"):
        case str() as name if name:
            return [name]
        case list() as items if items and all(isinstance(item, str) and item for item in items):
            return list(items)
        case _:
            raise ValueError(
                "scatter must name one input, as a string or a non-empty list"
            )


def _step_indices_by_id(steps: list[Mapping[str, Any]]) -> dict[str, int]:
    """Map every step id to its step index."""
    return {step["id"]: index for index, step in enumerate(steps) if isinstance(step.get("id"), str)}


def _scattered_names_by_index(steps: list[Mapping[str, Any]]) -> dict[int, set[str]]:
    """Return the scattered raw input names of every representably scattered step."""
    scattered: dict[int, set[str]] = {}
    for index, step in enumerate(steps):
        if "scatter" not in step:
            continue
        try:
            scattered[index] = set(_scatter_names(step))
        except ValueError:
            continue
    return scattered


def _scattered_element_type(declared: Any) -> Any:
    """Return the item type a scattered port receives from an array source."""
    required = _required_type(declared)
    if not _is_array_type(required):
        return declared
    try:
        return _array_item_type(required)
    except ValueError:
        return declared


def _scatter_source_findings(
    step_index: int,
    raw_name: Any,
    raw_source: Any,
    definition: Mapping[str, Any],
    source_types: Mapping[str, Any],
) -> list[str]:
    """Require an array-typed workflow-input source matching the scattered port."""
    path = f"steps[{step_index}].in.{raw_name}"
    try:
        sources = _source_values(raw_source, context=f"step input {step_index}.{raw_name}")
    except ValueError:
        # Every unrecognized source shape is already reported by path.
        return []
    if len(sources) != 1:
        return [f"{path}: a scattered input must have exactly one source"]
    source = sources[0]
    if "/" in source:
        # A process-output source is reported once, by the cross-step pass.
        return []
    declared = source_types.get(source)
    required = _required_type(declared)
    if not _is_array_type(required):
        return [
            f"{path}: a scattered input needs an array-typed source; {source!r} declares {declared!r}"
        ]
    try:
        element = _channel_shape(_array_item_type(required))
        port = _channel_shape(definition.get("type"))
    except ValueError:
        # An unsupported item or port type is already reported by the type passes.
        return []
    if element != port:
        return [
            f"{path}: scattered source {source!r} carries {element!r} elements but the "
            f"port takes {port!r}"
        ]
    return []


def _scatter_findings(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Validate every scattered step against the single- and multi-input scatter lowerings."""
    findings: list[str] = []
    source_types = _source_types(workflow, steps, tools)
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        path = f"steps[{step_index}]"
        if "scatter" not in step:
            continue
        try:
            names = _scatter_names(step)
        except ValueError as exc:
            findings.append(f"{path}.scatter: {exc}")
            continue
        method = step.get("scatterMethod")
        if method is not None and method not in _SCATTER_METHODS:
            findings.append(
                f"{path}.scatterMethod: unsupported CWL scatter method {method!r}"
            )
        elif len(names) > 1:
            # At exactly one scattered input all three methods coincide, so a
            # method is required to decide two or more; each of them is an
            # approved multi-input lowering (design §6, Topology).
            if method is None:
                findings.append(
                    f"{path}.scatterMethod: multi-input scatter over {len(names)} inputs "
                    "requires an explicit scatterMethod"
                )
        tool_inputs = tool.get("inputs", {})
        step_inputs = step.get("in", {})
        if not isinstance(tool_inputs, Mapping) or not isinstance(step_inputs, Mapping):
            continue
        for raw_name in names:
            definition = tool_inputs.get(raw_name)
            if not isinstance(definition, Mapping):
                findings.append(
                    f"{path}.scatter: scattered input {raw_name!r} is not declared by the "
                    "step's tool"
                )
                continue
            if _is_array_type(_required_type(definition.get("type"))):
                findings.append(
                    f"{path}.scatter: scattered input {raw_name!r} is array-typed; "
                    "scattering over an array of arrays is deferred beyond this lowering"
                )
                continue
            if raw_name not in step_inputs:
                findings.append(
                    f"{path}.scatter: scattered input {raw_name!r} has no source; wire it to an "
                    "array-typed workflow input or a scattered step's output"
                )
                continue
            findings.extend(
                _scatter_source_findings(
                    step_index,
                    raw_name,
                    step_inputs[raw_name],
                    definition,
                    source_types,
                )
            )
    findings.extend(_scatter_edge_findings(steps))
    return findings


def _scatter_edge_findings(steps: list[Mapping[str, Any]]) -> list[str]:
    """Reject every process edge whose cardinality a scattered step changes and no lowering keeps.

    A scattered step's output is gathered, in invocation order, into one
    array; a nested_crossproduct result reaches workflow outputs only. An
    unscattered step's output reaches a scattered step as one value: it
    broadcasts to every task, and it is not an array to scatter over, since
    no tool output is an array.
    """
    findings: list[str] = []
    indices = _step_indices_by_id(steps)
    scattered = _scattered_names_by_index(steps)
    if not scattered:
        return findings
    for step_index, raw_name, _raw_source, sources in _wired_inputs(steps):
        for source in sources:
            if "/" not in source:
                continue
            producer = indices.get(source.rsplit("/", maxsplit=1)[0])
            if producer is not None and producer in scattered:
                # Every scatter is indexed, so its outputs are gathered once, in
                # invocation order (design §6, Topology, Gather).
                if steps[producer].get("scatterMethod") == "nested_crossproduct" and len(scattered[producer]) > 1:
                    findings.append(
                        f"steps[{step_index}].in.{raw_name}: {source!r} is a nested_crossproduct "
                        "result, which reaches workflow outputs only"
                    )
            elif raw_name in scattered.get(step_index, set()):
                findings.append(
                    f"steps[{step_index}].in.{raw_name}: a scattered input needs an array-typed source; "
                    f"{source!r}, from a step that is not scattered, is one value"
                )
    return findings


def _step_input_findings(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Check each step input the fixed idioms lower: a default, a linkMerge, a pickValue, or two or more sources.

    Each source is one value (design §6, Topology, Step inputs). Null is the
    [] sentinel, so an idiom may not take an array source where [] would be
    read as null, and a merged list may not hold a source that may be null.
    """
    findings: list[str] = []
    source_types = _source_types(workflow, steps, tools)
    indices = _step_indices_by_id(steps)
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
            return index is not None and "when" in steps[index]
        return _is_optional(source_types.get(source))

    for step_index, raw_name, definition, sources in _wired_inputs(steps):
        tool_inputs = tools[step_index].get("inputs", {})
        if not isinstance(definition, Mapping) or not isinstance(tool_inputs, Mapping):
            continue
        path = f"steps[{step_index}].in.{raw_name}"
        link_merge, pick_value = definition.get("linkMerge"), definition.get("pickValue")
        if len(sources) < 2 and not _STEP_INPUT_IDIOM_FIELDS & set(definition):
            continue
        if raw_name in scattered.get(step_index, set()):
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
            findings.append(
                f"{path}.pickValue: pickValue picks from a list; one source with no linkMerge is not one"
            )
        declared = tool_inputs.get(raw_name)
        declared_type = declared.get("type") if isinstance(declared, Mapping) else declared
        if "default" in definition:
            default = definition["default"]
            if sources and merges is not None:
                findings.append(f"{path}.default: a default never applies beside a linkMerge or a pickValue: "
                                "the joined value is not null")
            elif _required_type(declared_type) in ("File", "Directory"):
                findings.append(f"{path}.default: a step-input default of a File or Directory input is not "
                                "supported; give the path as a workflow input")
            elif default is None or isinstance(default, (Mapping, list)) or not _phase1_value_matches(
                declared_type, default
            ):
                findings.append(f"{path}.default: the Nextflow target supports a JSON scalar default of "
                                "the tool input's type")
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
    not own a shell-literal binding that CWL would omit, or it is referenced
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
        shell_literal_names = {
            _identifier(raw_name, context="input reference")
            for raw_name, definition in inputs.items()
            if isinstance(definition, Mapping)
            and isinstance(definition.get("inputBinding"), Mapping)
            and definition["inputBinding"].get("shellQuote") is False
        }
    except ValueError:
        return None
    return all_names - referenced - shell_literal_names


def _absent_optional_findings(
    workflow: Mapping[str, Any],
    provided_params: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Reject optional inputs whose compiled workflow value is absent and unsafe."""
    workflow_inputs = _as_mapping(
        workflow.get("inputs", {}),
        error="compiled CWL Workflow inputs must be a mapping",
    )
    if _identifier_normalization_findings(
        workflow_inputs,
        context="workflow input",
        path="workflow.inputs",
    ):
        # The capability pass already owns this diagnostic.  There is no
        # unambiguous parameter lookup for the optional-input analysis.
        return []
    params = _workflow_params(workflow, provided_params)
    findings: list[str] = []
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        tool_inputs = tool.get("inputs", {})
        step_inputs = step.get("in", {})
        if not isinstance(tool_inputs, Mapping) or not isinstance(step_inputs, Mapping):
            continue
        safe_names = _safe_absence_names(tool)
        if safe_names is None:
            continue
        for raw_name, raw_definition in tool_inputs.items():
            if not isinstance(raw_definition, Mapping):
                continue
            raw_source = step_inputs.get(raw_name)
            optional = _is_optional(raw_definition.get("type"))
            if raw_source is None:
                # Unwired inputs are only a missingness problem when optional
                # without a default; required unwired inputs fail compilation.
                absent = optional and "default" not in raw_definition
            else:
                # A wired input is absent when any boundary source resolves to
                # null — including an absent optional *workflow* input feeding
                # a required tool input.
                try:
                    sources = _source_values(
                        raw_source,
                        context=f"step input {step_index}.{raw_name}",
                    )
                except ValueError:
                    continue
                boundary_sources = [source for source in sources if "/" not in source]
                # A pickValue or a default gives a value that is not null.
                absent = not _gives_non_null(raw_source) and any(
                    params.get(_identifier(source, context="workflow input source")) is None
                    for source in boundary_sources
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
                detail = (
                    "absent optional values are supported only for a val input "
                    "whose absence leaves command rendering unchanged"
                )
            else:
                detail = "resolves to an absent required value"
            findings.append(f"steps[{step_index}].run.inputs.{raw_name}: {detail}")
    return findings


def _gives_non_null(step_input: Any) -> bool:
    """Whether a step input's idioms give a value that is not null whatever its sources: a pickValue or a default."""
    return isinstance(step_input, Mapping) and bool({"pickValue", "default"} & set(step_input))


def _conditional_consumer_findings(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Reject every consumer of a conditional step's output the design does not admit.

    A conditional step's output may be null at runtime (design §6, Topology).
    That null is admitted only at a workflow output (which reports null) or an
    optional val input of a later step whose use the absent-optional lowering
    already admits (never referenced, or referenced only by a boolean-flag
    token or its own optional binding). Every other consumer is rejected by name, as is an array-typed
    output of a conditional step outside scatter, where the sentinel would
    collide with a genuinely empty array.
    """
    findings: list[str] = []
    conditional_ids: dict[str, int] = {}
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        if "when" not in step or tool.get("class") != "CommandLineTool" or not isinstance(step.get("id"), str):
            continue
        conditional_ids[step["id"]] = step_index
        outputs = tool.get("outputs", {})
        if isinstance(outputs, Mapping):
            for raw_name, definition in outputs.items():
                if not isinstance(definition, Mapping):
                    continue
                if _is_array_type(_required_type(definition.get("type"))):
                    findings.append(
                        f"steps[{step_index}].out.{raw_name}: array-typed output of a "
                        "conditional step is not supported outside scatter"
                    )

    if not conditional_ids:
        return findings

    for step_index, raw_name, raw_source, sources in _wired_inputs(steps):
        consumes_conditional = any(
            "/" in source and source.rsplit("/", maxsplit=1)[0] in conditional_ids
            for source in sources
        )
        if not consumes_conditional or _gives_non_null(raw_source):
            continue
        path = f"steps[{step_index}].in.{raw_name}"
        tool = tools[step_index]
        tool_inputs = tool.get("inputs", {})
        definition = tool_inputs.get(raw_name) if isinstance(tool_inputs, Mapping) else None
        if not isinstance(definition, Mapping):
            continue
        cwl_type = definition.get("type")
        if _is_array_type(_required_type(cwl_type)):
            findings.append(
                f"{path}: an array-typed port cannot consume a conditional step's output; "
                "[] already stands for a skipped step"
            )
            continue
        try:
            qualifier = cwl_type_to_nf_qualifier(cwl_type)
        except ValueError:
            qualifier = None
        if qualifier == "path":
            findings.append(
                f"{path}: a path consumer of a possibly-null conditional step "
                "output is not supported"
            )
            continue
        if not _is_optional(cwl_type):
            findings.append(
                f"{path}: a non-optional port cannot consume a possibly-null "
                "conditional step output"
            )
            continue
        safe_names = _safe_absence_names(tool)
        if safe_names is None:
            continue
        try:
            name = _identifier(raw_name, context="input reference")
        except ValueError:
            name = None
        if name not in safe_names:
            findings.append(
                f"{path}: this input's use of a possibly-null conditional step "
                "output is not one the absent-optional lowering admits"
            )
    return findings


def _is_flag_binding(definition: Mapping[str, Any]) -> bool:
    """Return whether a tool input will lower to a conditional flag token."""
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


def _source_types(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Map every wireable source name to the CWL type it carries."""
    types: dict[str, Any] = {}
    if isinstance(inputs := workflow.get("inputs", {}), Mapping):
        for raw_name, definition in inputs.items():
            declared = definition.get("type") if isinstance(definition, Mapping) else definition
            types[str(raw_name)] = declared
    for step, tool in zip(steps, tools, strict=True):
        outputs = tool.get("outputs", {})
        if not isinstance(outputs, Mapping) or not isinstance(step.get("id"), str):
            continue
        for raw_port, definition in outputs.items():
            declared = definition.get("type") if isinstance(definition, Mapping) else definition
            types[f"{step['id']}/{raw_port}"] = declared
    return types


def _flag_source_findings(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Require a boolean source for every input lowered to a flag token.

    A flag renders as a Groovy ternary over its channel value, so a
    non-boolean source would let Groovy truthiness decide the flag: a staged
    path is always truthy and the strings ``"false"`` and ``"0"`` are too.
    """
    source_types = _source_types(workflow, steps, tools)
    scattered_by_index = _scattered_names_by_index(steps)
    findings: list[str] = []
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        tool_inputs = tool.get("inputs", {})
        step_inputs = step.get("in", {})
        if not isinstance(tool_inputs, Mapping) or not isinstance(step_inputs, Mapping):
            continue
        scattered = scattered_by_index.get(step_index, set())
        for raw_name, definition in tool_inputs.items():
            if not isinstance(definition, Mapping) or not _is_flag_binding(definition):
                continue
            raw_source = step_inputs.get(raw_name)
            if raw_source is None:
                continue
            try:
                sources = _source_values(
                    raw_source,
                    context=f"step input {step_index}.{raw_name}",
                )
            except ValueError:
                if isinstance(raw_source, Mapping):
                    # Every mapping shape _source_values rejects is already
                    # reported, by path, by the unconsumed-fields pass. A
                    # non-mapping shape is genuinely unrecognized and must not
                    # be silently exempted from the boolean-source requirement.
                    continue
                raise
            for source in sources:
                declared = source_types.get(source, source_types.get(str(source)))
                if raw_name in scattered:
                    # A scattered port receives one element, so the array's
                    # item type is what must be boolean.
                    declared = _scattered_element_type(declared)
                if declared is not None and _required_type(declared) == "boolean":
                    continue
                findings.append(
                    f"steps[{step_index}].in.{raw_name}: a boolean flag input requires a "
                    f"boolean source; {source!r} declares {declared!r}"
                )
    return findings


def _text_capture_endpoints(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> set[str]:
    """Return every ``<step>/<port>`` source that captures file text."""
    endpoints: set[str] = set()
    for step, tool in zip(steps, tools, strict=True):
        if "when" in step:
            # A conditional step's captured-text output is the one proven
            # exception (design §6, Topology): _conditional_consumer_findings
            # already governs its downstream sinks with the admission rules
            # a possibly-null value requires.
            continue
        outputs = tool.get("outputs", {})
        if not isinstance(outputs, Mapping) or not isinstance(step.get("id"), str):
            continue
        for raw_port, definition in outputs.items():
            if not isinstance(definition, Mapping):
                continue
            try:
                capture = _output_capture(raw_port, definition)
            except ValueError:
                continue
            if capture == "text":
                endpoints.add(f"{step['id']}/{raw_port}")
    return endpoints


def _text_capture_sink_findings(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Restrict a captured value's sink to a workflow output.

    Every process output before this lowering carried the ``path`` qualifier,
    so the executable graph has no source-to-destination qualifier agreement
    check for process edges to lean on.
    """
    endpoints = _text_capture_endpoints(steps, tools)
    return [
        f"steps[{step_index}].in.{raw_name}: {source!r} captures file text; "
        "its only approved sink is a workflow output, because the executable "
        "graph has no qualifier agreement check for process edges"
        for step_index, raw_name, _raw_source, sources in _wired_inputs(steps)
        for source in sources
        if source in endpoints
    ]


def _capability_error(findings: list[str]) -> ValueError:
    details = "\n".join(f"- {finding}" for finding in findings)
    return ValueError(f"Nextflow capability analysis failed:\n{details}")


def _raise_capability_findings(findings: list[str]) -> None:
    if findings:
        raise _capability_error(findings)


def _condition_value(step: Mapping[str, Any], tool: Mapping[str, Any]) -> Expr:
    """Parse and boolean-type a step's ``when`` over the inputs the step binds, as CWL does.

    A step's when is evaluated once per invocation, after inputs are bound.
    """
    when = step["when"]
    if not isinstance(when, str) or not is_safe_subset_text(when):
        raise ValueError(f"step when {when!r} must be a safe JavaScript subset $( … ) expression")
    bound = _as_mapping(step.get("in", {}), error="compiled step inputs must be a mapping")
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    input_types = {
        str(raw_name): definition.get("type") if isinstance(definition, Mapping) else definition
        for raw_name, definition in inputs.items()
        if str(raw_name) in bound
    }
    try:
        expression = parse_safe_subset(when)
    except ValueError as exc:
        raise ValueError(f"step when {when!r} is outside the safe JavaScript subset: {exc}") from exc
    if unbound := sorted(references(expression) - input_types.keys()):
        names = ", ".join(f"inputs.{name}" for name in unbound)
        raise ValueError(f"step when {when!r} reads {names}, which the step does not bind")
    try:
        result = check_safe_subset(expression, input_types)
    except ValueError as exc:
        raise ValueError(f"step when {when!r} is outside the safe JavaScript subset: {exc}") from exc
    if result != "boolean":
        raise ValueError(f"step when {when!r} must compute a boolean, not a {result}")
    return expression


def _process(step: Mapping[str, Any], tool: Mapping[str, Any]) -> NfProcess:
    when = step.get("when")
    return NfProcess(
        name=_identifier(step.get("id"), context="workflow step id", variable=False),
        inputs=_ports(tool.get("inputs", {}), outputs=False, entries=_iwdr_entries(tool)),
        outputs=_ports(tool.get("outputs", {}), outputs=True),
        command=_command(tool),
        container=_container(tool),
        resources=_resources(tool),
        condition=_condition_value(step, tool) if when is not None else None,
    )


def _source_values(value: Any, *, context: str) -> list[str]:
    match value:
        case str() as source:
            return [source]
        case list() as sources if sources:
            normalized: list[str] = []
            for source in sources:
                match source:
                    case str():
                        normalized.append(source)
                    case _:
                        raise ValueError(f"{context} must be a source string or list of source strings")
            return normalized
        case {"source": source} as source_definition:
            unsupported = set(source_definition) - consumed(FIELDS["WorkflowStepInput"])
            if unsupported:
                raise ValueError(f"unsupported {context} fields: {', '.join(sorted(unsupported))}")
            return _source_values(source, context=context)
        case {"default": _} as source_definition if set(source_definition) <= {"default", "label"}:
            return []  # the default is the value
        case Mapping():
            raise ValueError(f"{context} must define source")
        case _:
            raise ValueError(f"{context} must be a source string or list of source strings")


def _wired_inputs(steps: list[Mapping[str, Any]]) -> Iterator[tuple[int, Any, Any, list[str]]]:
    """Yield (step index, input name, raw source, sources) for every step input of a recognized shape.

    Every other shape is reported, by path, by the passes that own it.
    """
    for step_index, step in enumerate(steps):
        step_inputs = step.get("in", {})
        if not isinstance(step_inputs, Mapping):
            continue
        for raw_name, raw_source in step_inputs.items():
            try:
                sources = _source_values(raw_source, context=f"steps[{step_index}].in.{raw_name}")
            except ValueError:
                continue
            yield step_index, raw_name, raw_source, sources


def _step_name_map(steps: list[Mapping[str, Any]], processes: list[NfProcess]) -> dict[str, str]:
    return {step["id"]: process.name for step, process in zip(steps, processes, strict=True)}


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source, context="workflow input source")
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process)
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port, context="process output source")


def _step_connections(
    steps: list[Mapping[str, Any]],
    processes: list[NfProcess],
    step_names: Mapping[str, str],
    defaults: Mapping[tuple[str, str], NfWorkflowInputConnection],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    # Only a multi-input scatter carries the invocation index a gather sorts by.
    multi_scattered = {
        process.name
        for step, process in zip(steps, processes, strict=True)
        if "scatter" in step
    }
    for step, process in zip(steps, processes, strict=True):
        match step.get("in", {}):
            case Mapping() as raw_inputs:
                pass
            case _:
                raise ValueError(f"compiled step {process.name!r} inputs must be a mapping")
        scattered = {
            _identifier(raw_name, context="scattered input")
            for raw_name in (_scatter_names(step) if "scatter" in step else [])
        }
        # Capability analysis already admitted only an approved method for two or more;
        # at one scattered input the three methods coincide.
        scatter_method = str(step.get("scatterMethod", "dotproduct")) if len(scattered) > 1 else "dotproduct"
        for raw_port, raw_source in raw_inputs.items():
            destination_port = _identifier(raw_port, context="process input destination")
            sources: list[NfWorkflowInputConnection | NfProcessConnection] = []
            for source in _source_values(raw_source, context=f"step input {process.name}.{destination_port}"):
                source_process, source_port = _source_endpoint(source, step_names)
                if source_process is None:
                    # Every scatter is indexed, so its gather keeps input order.
                    adapter = scatter_method if destination_port in scattered else None
                    sources.append(
                        NfWorkflowInputConnection(
                            source_port,
                            process.name,
                            destination_port,
                            adapter,
                        )
                    )
                else:
                    gathers = source_process in multi_scattered
                    sources.append(
                        NfProcessConnection(
                            source_process,
                            source_port,
                            process.name,
                            destination_port,
                            (scatter_method if destination_port in scattered else "gather") if gathers else None,
                        )
                    )
            # Capability analysis admitted only the step inputs the idioms lower.
            fields = raw_source if isinstance(raw_source, Mapping) else {}
            link_merge = fields.get("linkMerge", "merge_nested" if len(sources) > 1 else None)
            pick_value = fields.get("pickValue")
            if sources and "default" in fields:
                # The default takes the place of a null source: the first value that is not null.
                sources.append(defaults[(process.name, destination_port)])
                link_merge, pick_value = "merge_nested", "first_non_null"
            if link_merge is None:
                connections.extend(sources)
            else:
                connections.append(NfStepInputConnection(tuple(sources), link_merge, pick_value))
    return connections


def _workflow_output_connections(
    workflow: Mapping[str, Any],
    step_names: Mapping[str, str],
) -> list[NfConnection]:
    connections: list[NfConnection] = []
    match workflow.get("outputs", {}):
        case Mapping() as raw_outputs:
            pass
        case _:
            raise ValueError("compiled CWL Workflow outputs must be a mapping")
    for raw_port, definition in raw_outputs.items():
        match definition:
            case {"outputSource": output_source}:
                destination_port = _identifier(raw_port, context="workflow output destination")
                for source in _source_values(output_source, context=f"workflow output {destination_port}"):
                    source_process, source_port = _source_endpoint(source, step_names)
                    if source_process is None:
                        raise ValueError(
                            f"workflow output {destination_port!r} directly forwards a workflow input; "
                            "boundary passthrough is not executable on the Nextflow target"
                        )
                    connections.append(
                        NfWorkflowOutputConnection(source_process, source_port, destination_port)
                    )
            case _:
                raise ValueError(f"workflow output {raw_port!r} must define outputSource")
    return connections


def _connections(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    processes: list[NfProcess],
    defaults: list[NfWorkflowInputConnection],
) -> list[NfConnection]:
    """Every connection: a default beside a source joins that step input, and every other default is its own."""
    step_names = _step_name_map(steps, processes)
    step_connections = _step_connections(
        steps, processes, step_names, {(default.to_process, default.to_port): default for default in defaults}
    )
    joined = {
        (connection.to_process, connection.to_port)
        for connection in step_connections if isinstance(connection, NfStepInputConnection)
    }
    return [
        *step_connections,
        *_workflow_output_connections(workflow, step_names),
        *(default for default in defaults if (default.to_process, default.to_port) not in joined),
    ]


def _default_bindings(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
    processes: list[NfProcess],
    workflow_inputs: Iterable[str],
) -> tuple[list[NfWorkflowInputConnection], dict[str, Any], dict[str, str]]:
    """Synthesize a workflow parameter for every default, and a connection for every unwired process input.

    Covers three cases: a step input's default, a declared CWL default on an
    unwired input, and an unwired optional input with no default. Capability
    analysis has already proven any remaining unwired, no-default optional
    input is a safe absence, so this always contributes a null value for
    those rather than re-deriving safety. A step input's default beside a
    source is one of that step input's sources (``_step_connections``), so it
    gets a parameter and no connection of its own.

    Each parameter is made as the compiler makes a name, `<step>~<port>`, and
    normalized; one that normalizes like another or like a workflow input is
    rejected, naming both.
    """
    made: list[tuple[str, str, str, Any, str]] = []
    for step, tool, process in zip(steps, tools, processes, strict=True):
        bound = _as_mapping(step.get("in", {}), error="compiled step inputs must be a mapping")
        inputs = _as_mapping(tool.get("inputs", {}), error="tool inputs must be a mapping")
        for raw_name, definition in inputs.items():
            step_input = bound.get(raw_name)
            if not isinstance(definition, Mapping):
                continue
            if isinstance(step_input, Mapping) and "default" in step_input:
                value = _json_value(step_input["default"])
            elif raw_name in bound:
                continue
            elif "default" in definition:
                value = _json_value(definition["default"])
            elif _is_optional(definition.get("type")):
                # Capability analysis has already proven this unwired,
                # no-default optional input is a safe absence.
                # _apply_absent_sentinel converts None to the runtime-proven
                # [] representation once, at the end of the conversion.
                value = None
            else:
                continue
            port_name = _identifier(raw_name, context="defaulted process input")
            param_type = _param_type(definition.get("type"))
            made.append((f"{step['id']}~{raw_name}", process.name, port_name, value, param_type))
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


def _workflow_steps(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The steps a workflow document lists, in order."""
    return [
        _as_mapping(step, error="compiled CWL Workflow steps must be mappings")
        for step in _as_list(workflow.get("steps", []), error="compiled CWL Workflow steps must be a list")
    ]


def _workflow_params(
    workflow: Mapping[str, Any],
    provided_params: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_inputs = _as_mapping(
        workflow.get("inputs", {}),
        error="compiled CWL Workflow inputs must be a mapping",
    )
    provided = _as_mapping(
        provided_params,
        error="compiled workflow input values must be a mapping",
    )
    params: dict[str, Any] = {}
    normalized_names = _normalized_identifiers(workflow_inputs, context="workflow input")
    missing = object()
    for name, definition in workflow_inputs.items():
        value = provided.get(name, missing)
        if value is missing and isinstance(definition, Mapping) and "default" in definition:
            value = definition["default"]
        elif value is missing:
            value = None
        params[normalized_names[name]] = _json_value(value)
    return params


def _workflow_param_types(workflow: Mapping[str, Any]) -> dict[str, str]:
    """Each workflow input's declared type, by parameter name."""
    workflow_inputs = _as_mapping(workflow.get("inputs", {}), error="compiled CWL Workflow inputs must be a mapping")
    names = _normalized_identifiers(workflow_inputs, context="workflow input")
    return {
        names[name]: _param_type(definition.get("type") if isinstance(definition, Mapping) else definition)
        for name, definition in workflow_inputs.items()
    }


def _apply_absent_sentinel(params: Mapping[str, Any]) -> dict[str, Any]:
    """Replace every remaining null parameter with the runtime-proven [] sentinel.

    Capability analysis (via _absent_optional_findings, which itself calls
    _workflow_params and depends on None to detect absence) has already
    proven any null still present here is a safe absent-optional val input.
    This substitution happens only once, after that analysis, so it never
    interferes with it.
    """
    return {name: (ABSENT_VAL_SENTINEL if value is None else value) for name, value in params.items()}


def compiled_source_to_nextflow(
    source: CompilationResult | CompiledNextflowSource,
) -> ExecutableNextflowWorkflow:
    """Lower a completed Sophios compilation, leaving out the notes the lowering makes.

    Args:
        source (CompilationResult | CompiledNextflowSource): As
            :func:`compiled_source_to_nextflow_with_notes` takes it.

    Returns:
        ExecutableNextflowWorkflow: The validated executable representation.
    """
    return compiled_source_to_nextflow_with_notes(source)[0]


def compiled_source_to_nextflow_with_notes(
    source: CompilationResult | CompiledNextflowSource,
) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
    """Lower a completed Sophios compilation without repeating core semantics.

    Every unsupported source semantic, including one that only lowering or
    model validation detects, is reported with its source path and
    aggregated with the rest before the workflow is assembled.

    Args:
        source (CompilationResult | CompiledNextflowSource): The production
            compiler result, or the narrow compiled-document seam used by
            capability unit tests.

    Raises:
        TypeError: If the argument is not a supported compiled source.
        ValueError: If capability analysis finds unsupported semantics, or
            lowering/validation rejects the workflow.

    Returns:
        tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]: The
            validated executable representation, and the notes the lowering
            made, such as a CUDA field it does not check.
    """
    if isinstance(source, CompilationResult):
        nested_findings = _nested_workflow_capability_findings(
            source.graph,
            source.artifact,
        )
        try:
            compiled = compilation_result_source(source)
        except ValueError:
            _raise_capability_findings(nested_findings)  # a finding names what the projection cannot carry
            raise
    elif isinstance(source, CompiledNextflowSource):
        nested_findings = []
        compiled = source
    else:
        raise TypeError("Nextflow conversion requires a CompilationResult")
    compiled = replace(compiled, params=_local_locations_as_paths(compiled.params))
    workflow = compiled.workflow
    tools = compiled.tools
    steps = _workflow_steps(workflow)
    findings = list(nested_findings)
    notes: list[Diagnostic] = []
    processes: list[NfProcess] = []
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        if step_findings := _tool_capability_findings(step, tool, step_index=step_index):
            findings.extend(step_findings)
            continue
        try:
            processes.append(_process(step, tool))
        except ValueError as error:  # lowering and the model reject what no source pass can see
            findings.append(f"steps[{step_index}]: {error}")
        notes.extend(_cuda_notes(step, tool))
    findings.extend(_workflow_capability_findings(workflow, compiled.params, steps))
    findings.extend(_absent_optional_findings(workflow, compiled.params, steps, tools))
    findings.extend(_conditional_consumer_findings(list(steps), tools))
    findings.extend(_flag_source_findings(workflow, steps, tools))
    findings.extend(_scatter_findings(workflow, steps, tools))
    findings.extend(_step_input_findings(workflow, steps, tools))
    findings.extend(_text_capture_sink_findings(steps, tools))
    _raise_capability_findings(findings)
    try:
        params = _workflow_params(workflow, compiled.params)
        default_connections, default_params, default_types = _default_bindings(
            steps, tools, processes, workflow.get("inputs", {})
        )
        params.update(default_params)
        name = _identifier(compiled.name, context="workflow name", variable=False)
        while name in {process.name for process in processes}:  # a step may share the workflow's name
            name = f"_{name}"
        return ExecutableNextflowWorkflow(
            name=name,
            processes=processes,
            connections=_connections(workflow, list(steps), processes, default_connections),
            params=_apply_absent_sentinel(params),
            param_types={**_workflow_param_types(workflow), **default_types},
        ), tuple(notes)
    except ValueError as error:
        raise _capability_error([f"workflow: {error}"]) from None
