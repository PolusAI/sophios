"""Lower a resolved Sophios compilation into the Nextflow representation."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
import math
from os import PathLike
from pathlib import Path
import posixpath
import re
from types import MappingProxyType
from typing import Any, Final, TypeVar

from cwl_utils.parser import cwl_v1_2 as standard

from .ir.complete import coerce_job_value
from .ir.resolved import (
    Construct, Crossing, InputRef, NotLowered, Ref, Refusal, Requirement, ResolvedStep,
    ResolvedWorkflow, StepInput, Through, ToolStep, WorkflowInput, WorkflowOutput, boolean_problem, output_sources,
    output_unbound,
    tool_steps,
)
from .ir.types import AuthoredName, PortDeclaration, PortType
from .lang.cwl import (
    LINK_MERGES, PICK_VALUES, PICKS_ONE, SCATTER_METHODS, Process, Written, canonical, fields_set, inherited,
    ports_named, required, requirement as requirement_of,
)
from .lang.diagnostics import Diagnostic, Locator, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .lang.subset import Expr, is_safe_subset_text, references
from .nf_support import FIELDS, REQUIREMENT_CLASSES, Row, consumed
from .nf_symbols import normalize_nextflow_identifier
from .nf_types import (
    ExecutableNextflowWorkflow,
    GLOB_WILDCARDS,
    PATH_KINDS,
    NF_INTERNAL_IDENTIFIERS,
    PARAM_TYPES,
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
    ARRAY_REFERENCE,
    absent_path_problems,
    condition_without_inputs,
    endpoint_problems,
    incoming_connections,
    inputs_read,
    entryname_problem,
    environment_name_problem,
    json_value_problem,
    link_refusal,
    linked_reference,
    literal_problem,
    optional_output_problems,
    process_dependencies,
    reference_problems,
    staged_write_problems,
    template_segments,
    topological_order,
    with_scalar_types,
)
from .wic_types import Tool

# Runtime-proven representation of an absent optional val input. Channel.value(None)
# never binds under the pinned Nextflow runtime (the consuming process never starts
# and the run hangs), so a literal JSON null cannot be used. [] is Groovy-falsy like
# null and false, and is unambiguous here because no supported scalar value is ever
# legitimately an array.
ABSENT_VAL_SENTINEL: list[Any] = []


@dataclass(frozen=True, slots=True)
class _Finding:
    """One construct the target refuses: the code of its reason, where it is, and the reason. It is at `path`, the
    fields that lead to it from `step`, or with no step from the root workflow: `.in.<port>` is a step's input,
    `.run` its tool or the workflow it calls, `.inputs.<name>` a workflow's input."""

    code: SophiosErrorCode
    path: str
    text: str
    step: ResolvedStep | None = None


class _Unsupported(ValueError):
    """What a lowering helper cannot lower, with the code of its reason; a plain ValueError is an invariant."""

    def __init__(self, code: SophiosErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _computed_or_invalid(value: Any, computed: SophiosErrorCode) -> SophiosErrorCode:
    """Why `value` is not the literal CWL wants there: `computed`, for an expression the target does not evaluate,
    or else a value CWL itself does not take."""
    expression = isinstance(value, str) and ("$(" in value or "${" in value)
    return computed if expression else SophiosErrorCode.NF_INVALID_CWL


def _prefix(raw_name: Any, binding: standard.CommandLineBinding) -> str | None:
    """A binding's prefix, or None for none: text that is not empty or blank, which cwltool skips."""
    if binding.prefix is not None and not binding.prefix.strip():
        raise _Unsupported(SophiosErrorCode.NF_EMPTY_PREFIX,
                           f"CWL command prefix for {raw_name!r} must be a non-empty string")
    return binding.prefix


@contextmanager
def _reported(findings: list[_Finding], path: str) -> Iterator[None]:
    """Report what the block refuses as a finding at `path`, and go on."""
    try:
        yield
    except _Unsupported as exc:
        findings.append(_Finding(exc.code, path, str(exc)))


def _identifier(value: Any, *, variable: bool = True) -> str:
    """Normalize a CWL identifier into a stable Nextflow identifier (see normalize_nextflow_identifier)."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"a name must be a non-empty string, not {value!r}")
    local = value.rsplit("#", maxsplit=1)[-1]
    if not local:
        raise _Unsupported(SophiosErrorCode.NF_NAME, f"{value!r} cannot be normalized to a Nextflow identifier")
    identifier = normalize_nextflow_identifier(local, variable=variable)
    while identifier in NF_INTERNAL_IDENTIFIERS:
        identifier = f"_{identifier}"
    return identifier


def _normalized_identifiers(values: Iterable[Any], *, context: str, variable: bool = True) -> dict[Any, str]:
    """Normalize source identifiers and reject every many-to-one mapping."""
    normalized_by_source: dict[Any, str] = {}
    sources_by_normalized: dict[str, list[Any]] = {}
    for source in values:
        normalized = _identifier(source, variable=variable)
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
        raise _Unsupported(SophiosErrorCode.NF_NAME, f"{context} identifiers {details}")
    return normalized_by_source


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
    match required(cwl_type)[0]:
        case "File" | "Directory":
            return "path"
        case "string" | "int" | "float" | "boolean":
            return "val"
        case _:
            raise _Unsupported(SophiosErrorCode.NF_TYPE, f"unsupported CWL type for the Nextflow target: {cwl_type!r}")


def _qualifier(cwl_type: Any) -> str | None:
    """`cwl_type_to_nf_qualifier`, or None for a type the target does not lower."""
    try:
        return cwl_type_to_nf_qualifier(cwl_type)
    except ValueError:
        return None


def _type_problem(cwl_type: Any) -> str | None:
    """Why the target does not lower a value of `cwl_type`, or None: it lowers string, int, float, boolean, File and
    Directory, optional or not, and one array of one of them. A long or double has its own finding."""
    if _type_names(cwl_type) & _UNSUPPORTED_NUMBER_TYPES:
        return None
    try:
        inner = required(cwl_type)[0]
        cwl_type_to_nf_qualifier(_array_item_type(inner) if _is_array_type(inner) else inner)
    except _Unsupported as unsupported:
        return str(unsupported)
    return None


# The safe subset's number rules (design §6, Expressions) cover int and float only.
_UNSUPPORTED_NUMBER_TYPES = frozenset({"long", "double"})


def _type_names(cwl_type: Any) -> set[str]:
    """Every type name in a canonical CWL type, optional and array forms included."""
    match cwl_type:
        case str():
            return {cwl_type}
        case list():
            return set().union(*map(_type_names, cwl_type))
        case Mapping():
            return _type_names(cwl_type.get("type")) | _type_names(cwl_type.get("items"))
    return set()


def _number_type_findings(cwl_type: Any, *, path: str) -> list[_Finding]:
    """Name each long or double a declared type uses."""
    return [
        _Finding(SophiosErrorCode.NF_TYPE,
                 f"{path}.type", f"{name} is not supported: the Nextflow target lowers int and float; declare one of "
                 "those")
        for name in sorted(_type_names(cwl_type) & _UNSUPPORTED_NUMBER_TYPES)
    ]


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
    items = array_type.get("items")
    match items:
        case "array":
            raise _Unsupported(SophiosErrorCode.NF_TYPE, "nested arrays are deferred beyond this lowering")
        case Mapping() as mapping:
            if mapping.get("type") == "array":
                raise _Unsupported(SophiosErrorCode.NF_TYPE, "nested arrays are deferred beyond this lowering")
            if "inputBinding" in mapping:
                raise _Unsupported(SophiosErrorCode.NF_ARRAY_BINDING,
                                   "per-item array element bindings are deferred beyond this lowering")
            extra = set(mapping) - {"type"}
            if extra:
                raise _Unsupported(SophiosErrorCode.NF_TYPE,
                                   f"unsupported array items fields: {', '.join(sorted(extra))}")
            return mapping.get("type")
        case _:
            return items


def _ports(
    ports: Mapping[str, Any],
    *,
    outputs: bool,
    entries: Mapping[str, NfEntry] | None = None,
) -> list[NfPort]:
    lowered: list[NfPort] = []
    for raw_name, port in ports.items():
        name = _identifier(raw_name)
        cwl_type = canonical(port.type_)
        inner = required(cwl_type)[0]
        is_array = not outputs and _is_array_type(inner)
        element_type = _array_item_type(inner) if is_array else cwl_type
        qualifier = cwl_type_to_nf_qualifier(element_type)
        lowered.append(
            NfPort(
                name,
                qualifier,
                emit=name if outputs else None,
                glob=_output_template(raw_name, port) if outputs else None,
                path_kind=PATH_KINDS.get(required(element_type)[0]),
                is_array=is_array,
                entry=None if outputs or entries is None else entries.get(name),
                capture=_output_capture(raw_name, port) if outputs else None,
                optional=outputs and qualifier == "path" and required(cwl_type)[1],
            )
        )
    return lowered


_INPUT_EXPRESSION = re.compile(
    r"\$\(\s*inputs\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<suffix>path|basename))?\s*\)"
)
# One token of a string CWL interpolates: an escape it rewrites (`\$(`, `\${`, `\\`), or a reference.
_INTERPOLATION = re.compile(rf"\\(?P<escaped>\$[({{]|\\)|{_INPUT_EXPRESSION.pattern}")
_EXPRESSION_START = re.compile(r"\$[({]")


def _literal(text: str) -> NfLiteral:
    """`text` as literal command data, which no NUL byte can be."""
    if (problem := literal_problem(text)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, problem)
    return NfLiteral(text)


def _template(text: str) -> NfTemplate:
    if (problem := literal_problem(text)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, problem)
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
            name = _identifier(match["name"])
            segments.append(NfBasenameReference(name) if match["suffix"] == "basename" else NfInputReference(name))
        offset = match.end()
    segments.append(NfLiteral(text[offset:]))
    # A `$` not followed by `(` or `{` is plain text, as in CWL. A space stands in for each token, so the text on
    # its two sides cannot join into an expression's start: `{$$(inputs.x){$` is `{$`, a reference and `{$`.
    if interpolated and _EXPRESSION_START.search(_INTERPOLATION.sub(" ", text)):
        raise _Unsupported(
            SophiosErrorCode.NF_EXPRESSION,
            f"{text!r} contains an unsupported CWL expression; the supported forms are $(inputs.<name>) "
            "and, on a File or Directory input, $(inputs.<name>.path) and $(inputs.<name>.basename)"
        )
    return NfTemplate(tuple(segments))


def _text(template: NfTemplate) -> str | None:
    """The text of a template with no input reference, which is one literal, or None."""
    match template.segments:
        case [NfLiteral(value=text)]:
            return text
    return None


def _literal_text(value: str | list[str] | None) -> str | None:
    """The text of a value that lowers to a template with no input reference, or None: a glob may be a list."""
    if not isinstance(value, str):
        return None
    try:
        return _text(_template(value))
    except ValueError:
        return None


def _position(value: int | str | None, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):  # the loader reads true as an int, which Python, not CWL, reads as 1
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, f"unsupported non-integer CWL command position {value!r}")
    try:
        return int(value)
    except ValueError as exc:
        raise _Unsupported(_computed_or_invalid(value, SophiosErrorCode.NF_EXPRESSION),
                           f"unsupported non-integer CWL command position {value!r}") from exc


def _binding_tokens(prefix: str | None, value: NfTemplate, *, separate: bool | None) -> tuple[NfTemplate, ...]:
    match prefix:
        case None:
            if separate is False:
                # cwltool raises for separate without prefix, type-independently.
                raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CWL separate cannot be specified without a prefix")
            return (value,)
        case str() as text:
            prefix_template = NfTemplate((_literal(text),))
            if separate is not False:
                return prefix_template, value
            return (NfTemplate((*prefix_template.segments, *value.segments)),)


def _shell_literal_value(
    raw_name: Any,
    binding: standard.CommandLineBinding,
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
        raise _Unsupported(
            SophiosErrorCode.NF_SHELL_QUOTE, f"shellQuote false for {raw_name!r} requires ShellCommandRequirement "
            "in requirements or hints"
        )
    if binding.prefix is not None:
        raise _Unsupported(
            SophiosErrorCode.NF_SHELL_QUOTE, f"shellQuote false for {raw_name!r} does not support a prefix; "
            "the raw literal must be the binding's whole value"
        )
    if binding.separate is False:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CWL separate cannot be specified without a prefix")
    value_from = binding.valueFrom
    if value_from is None:
        raise _Unsupported(
            SophiosErrorCode.NF_SHELL_QUOTE, f"shellQuote false for {raw_name!r} has no valueFrom; a plain "
            "input value is never rendered unquoted"
        )
    text = _text(_template(value_from))
    if text is None:
        raise _Unsupported(
            SophiosErrorCode.NF_SHELL_QUOTE, f"shellQuote false for {raw_name!r} references an input; only a "
            "CWL-author literal with no input reference may be rendered unquoted"
        )
    if "$" in text:
        raise _Unsupported(
            SophiosErrorCode.NF_SHELL_QUOTE,
            f"shellQuote false for {raw_name!r} holds a '$', which the task's shell would expand "
            "in its own environment, not CWL's"
        )
    return NfShellLiteral(text)


def _boolean_value_from(value_from: str | None, tool: "_Tool") -> str | None:
    """The boolean input a valueFrom restates whole as ``$(inputs.<name>)``, else None."""
    match = _INPUT_EXPRESSION.fullmatch(value_from.strip()) if value_from is not None else None
    if match is None or match["suffix"]:
        return None
    port = tool.inputs.get(match["name"])
    if port is not None and required(canonical(port.type_))[0] == "boolean":
        return _identifier(match["name"])
    return None


def _flag_tokens(name: str, prefix: Any) -> tuple[NfCommandToken, ...]:
    """CWL's boolean binding: the prefix when true, nothing when false or without a prefix."""
    return () if prefix is None else (NfFlag(name, prefix),)


def _argument_items(
    arguments: list[str | standard.CommandLineBinding],
    *,
    shell_mode: bool,
    tool: "_Tool",
) -> list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]]:
    items: list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]] = []
    for index, argument in enumerate(arguments):
        if isinstance(argument, standard.CommandLineBinding):
            if argument.valueFrom is None:
                raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "mapped CWL arguments must contain valueFrom")
            item_position = _position(argument.position, default=0)
            if argument.shellQuote is False:
                tokens: tuple[NfCommandToken, ...] = (
                    _shell_literal_value(f"arguments[{index}]", argument, shell_mode=shell_mode),
                )
            elif flag := _boolean_value_from(argument.valueFrom, tool):
                tokens = _flag_tokens(flag, argument.prefix)
            else:
                tokens = _binding_tokens(argument.prefix, _template(argument.valueFrom),
                                         separate=argument.separate)
        else:
            tokens = (_template(argument),)
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
    template = _template(value_from)
    match template.segments:
        case (NfInputReference(name=reference),) if reference == name:
            return
        case _:
            raise _Unsupported(
                SophiosErrorCode.NF_BOOLEAN_BINDING, "valueFrom on a boolean inputBinding is supported only as "
                f"$(inputs.{raw_name}), restating the input's own value; "
                f"{value_from!r} does not have that shape"
            )


def _array_binding(
    raw_name: Any,
    name: str,
    required_type: Mapping[str, Any],
    binding: standard.CommandLineBinding,
) -> NfArrayBinding:
    """Lower an array-typed inputBinding to its one supported shape.

    No itemSeparator, no valueFrom, and separate is not false: the array's
    optional prefix is contributed once and each element becomes its own
    argv word, or nothing at all when the array is empty.
    """
    if binding.valueFrom is not None:
        raise _Unsupported(
            SophiosErrorCode.NF_ARRAY_BINDING, f"CWL valueFrom on an array-typed inputBinding for {raw_name!r} is "
            "deferred beyond this lowering"
        )
    if binding.itemSeparator is not None:
        raise _Unsupported(SophiosErrorCode.NF_ARRAY_BINDING,
                           f"CWL itemSeparator for {raw_name!r} is deferred beyond this lowering")
    if binding.separate is False:
        raise _Unsupported(
            SophiosErrorCode.NF_ARRAY_BINDING, f"CWL separate: false on an array-typed inputBinding for {raw_name!r} "
            "is deferred beyond this lowering"
        )
    # Validates the item type is a supported, non-nested, no-per-item-binding
    # scalar; the qualifier itself is not needed here.
    cwl_type_to_nf_qualifier(_array_item_type(required_type))
    return NfArrayBinding(name, _prefix(raw_name, binding))


_OPTIONAL_BINDING_TYPES = frozenset({"string", "int", "float"})


def _optional_binding(raw_name: Any, name: str, binding: standard.CommandLineBinding) -> NfOptionalBinding:
    """Lower an optional string, int or float input's own binding: nothing when the input is absent."""
    prefix, separate = _prefix(raw_name, binding), binding.separate is not False
    if prefix is None and not separate:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CWL separate cannot be specified without a prefix")
    return NfOptionalBinding(name, prefix, separate)


_IWDR_ENTRY_PATTERN = re.compile(r"^\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\)$")


def _iwdr_entry_reference(value: str) -> str:
    """Return the raw input name referenced by a bare $(inputs.<name>) IWDR entry."""
    if match := _IWDR_ENTRY_PATTERN.match(value.strip()):
        return match.group(1)
    raise _Unsupported(
        SophiosErrorCode.NF_STAGING, "must be a bare $(inputs.<name>) reference to one File/Directory input; "
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
    template = _template(value)
    if template.segments == (NfBasenameReference(raw_name),):
        return None
    text = _text(template)
    if text is None:
        raise _Unsupported(
            SophiosErrorCode.NF_STAGING, "entryname is supported only as a literal string or the input's own "
            f"$(inputs.{raw_name}.basename); a computed or differently-referencing entryname is not supported"
        )
    return text


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
        case standard.Dirent() as dirent:
            writable = bool(dirent.writable)
            raw_name = _iwdr_entry_reference(dirent.entry)
            rename = _iwdr_entryname(dirent.entryname, raw_name=raw_name)
        case _:
            raise _Unsupported(SophiosErrorCode.NF_STAGING,
                               "must be a bare $(inputs.<name>) reference or a Dirent mapping")
    port = tool_inputs.get(raw_name)
    if port is None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, f"references undeclared input {raw_name!r}")
    inner = required(canonical(port.type_))[0]
    if _is_array_type(inner):
        raise _Unsupported(
            SophiosErrorCode.NF_STAGING,
            f"references array-typed input {raw_name!r}; staging an array of files is not supported"
        )
    if _qualifier(inner) != "path":
        raise _Unsupported(
            SophiosErrorCode.NF_STAGING,
            f"references non-path input {raw_name!r}; only File/Directory inputs can be staged"
        )
    if rename is not None and (problem := entryname_problem(rename)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, problem)
    return _identifier(raw_name), NfEntry(rename, writable)


def _iwdr_listing_findings(
    tool: "_Tool",
    requirement: standard.InitialWorkDirRequirement,
    *,
    path: str,
) -> list[_Finding]:
    """Validate every InitialWorkDirRequirement listing entry independently."""
    listing = requirement.listing
    if not isinstance(listing, list):
        return [_Finding(SophiosErrorCode.NF_STAGING,
                         f"{path}.listing", "InitialWorkDirRequirement listing must be a list")]
    findings: list[_Finding] = []
    renamed_by: dict[str, set[str]] = {}
    listed_at: dict[str, int] = {}
    for index, item in enumerate(listing):
        if isinstance(item, standard.Dirent) and (
            dirent_findings := _unconsumed_field_findings(fields_set(item), "Dirent", path=f"{path}.listing[{index}]")
        ):
            findings.extend(dirent_findings)
            continue
        try:
            name, entry = _iwdr_listing_item(item, tool_inputs=tool.inputs)
        except _Unsupported as exc:
            findings.append(_Finding(exc.code, f"{path}.listing[{index}]", str(exc)))
            continue
        if name in listed_at:
            findings.append(
                _Finding(SophiosErrorCode.NF_STAGED_NAMES,
                         f"{path}.listing[{index}]", f"input {name!r} appears more than once "
                         f"(first listed at listing[{listed_at[name]}])")
            )
            continue
        listed_at[name] = index
        if entry.name is not None:
            renamed_by.setdefault(entry.name, set()).add(name)
    for rename, names in renamed_by.items():
        if len(names) > 1:
            findings.append(
                _Finding(SophiosErrorCode.NF_STAGED_NAMES,
                         f"{path}.listing", f"inputs {sorted(names)} are all staged under the same "
                         f"literal name {rename!r}")
            )
    return findings


def _iwdr_entries(tool: "_Tool") -> dict[str, NfEntry]:
    """Each input an InitialWorkDirRequirement listing names, with its entry; the task makes every one."""
    requirement = tool.requirement(standard.InitialWorkDirRequirement)
    if requirement is None:
        return {}
    listing = requirement.listing
    if not isinstance(listing, list):
        raise _Unsupported(SophiosErrorCode.NF_STAGING, "InitialWorkDirRequirement listing must be a list")
    entries: dict[str, NfEntry] = {}
    for item in listing:
        name, entry = _iwdr_listing_item(item, tool_inputs=tool.inputs)
        if name in entries:
            raise _Unsupported(SophiosErrorCode.NF_STAGED_NAMES,
                               f"input {name!r} appears more than once in the IWDR listing")
        entries[name] = entry
    return entries


def _iwdr_rename_reference_findings(
    tool: "_Tool",
    renamed_names: set[str],
) -> list[_Finding]:
    """Reject any reference to an input whose entry has a literal name.

    What such a reference means is out of scope (design §6, Inputs and
    channels); the supported pattern is for the command to name the entry by
    its literal. An entry under the input's own basename is referenced freely.
    """
    if not renamed_names:
        return []
    referenced = (_referenced_names(tool) or set()) & renamed_names
    return [
        _Finding(SophiosErrorCode.NF_STAGED_NAMES,
                 ".run.requirements.InitialWorkDirRequirement", f"input {name!r} has an InitialWorkDirRequirement "
                 "entry with a literal name and cannot also be referenced in the command, stream targets, or output "
                 "globs; name the entry by its literal")
        for name in sorted(referenced)
    ]


def _input_binding_items(
    tool: "_Tool",
    *,
    shell_mode: bool,
) -> list[tuple[tuple[int, int, str], tuple[NfCommandToken, ...]]]:
    items: list[tuple[tuple[int, int, str], tuple[NfCommandToken, ...]]] = []
    for raw_name, port in tool.inputs.items():
        binding = port.inputBinding
        if binding is None:
            continue
        name = _identifier(raw_name)
        value_from = binding.valueFrom
        position = _position(binding.position, default=0)
        inner, optional = required(canonical(port.type_))
        if binding.shellQuote is False:
            tokens: tuple[NfCommandToken, ...] = (_shell_literal_value(raw_name, binding, shell_mode=shell_mode),)
        elif _is_array_type(inner):
            tokens = (_array_binding(raw_name, name, inner, binding),)
        elif inner == "boolean":
            # CWL boolean bindings contribute their prefix, or nothing at all
            # when the flag is false or no prefix is declared. A
            # self-referencing valueFrom reduces to the same value the
            # binding already carries, so it renders identically.
            if value_from is not None:
                _boolean_flag_reference(raw_name, name, value_from)
            prefix = _prefix(raw_name, binding)
            if prefix is None and binding.separate is False:
                raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CWL separate cannot be specified without a prefix")
            tokens = _flag_tokens(name, prefix)
        elif flag := _boolean_value_from(value_from, tool):
            if optional:
                # CWL omits the whole binding for an absent input; a flag renders whatever it is.
                raise _Unsupported(SophiosErrorCode.NF_BOOLEAN_BINDING,
                                   f"a boolean valueFrom on optional input {raw_name!r} is not supported")
            tokens = _flag_tokens(flag, binding.prefix)
        elif value_from is None and optional and inner in _OPTIONAL_BINDING_TYPES:
            # CWL adds nothing for a null input; the [] sentinel stands for one.
            tokens = (_optional_binding(raw_name, name, binding),)
        else:
            value = _template(value_from) if value_from is not None else NfTemplate((NfInputReference(name),))
            tokens = _binding_tokens(binding.prefix, value, separate=binding.separate)
        items.append(((position, 1, str(raw_name)), tokens))
    return items


def _command_items(tool: "_Tool") -> tuple[NfCommandToken, ...]:
    shell_mode = tool.requirement(standard.ShellCommandRequirement) is not None
    ordered = sorted([
        *_argument_items(tool.command_line.arguments or [], shell_mode=shell_mode, tool=tool),
        *_input_binding_items(tool, shell_mode=shell_mode),
    ])
    return tuple(token for _key, tokens in ordered for token in tokens)


def _command(tool: "_Tool") -> NfCommand:
    base_command = tool.command_line.baseCommand
    tokens: list[NfCommandToken] = []
    match base_command:
        case "":
            raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CommandLineTool baseCommand must not be empty")
        case str() as command:
            tokens.append(NfTemplate((_literal(command),)))
        case list() as command:
            tokens.extend(NfTemplate((_literal(token),)) for token in command)

    tokens.extend(_command_items(tool))
    if not any(isinstance(token, (NfTemplate, NfShellLiteral)) for token in tokens):
        if tool.requirement(standard.DockerRequirement) is not None:
            raise _Unsupported(
                SophiosErrorCode.NF_EMPTY_COMMAND,
                "a containerized tool whose command can render empty is not supported: CWL then gives the "
                "container no arguments, so the image runs its CMD, after its ENTRYPOINT if it has one, which "
                "the Nextflow task does not; name the program in baseCommand"
            )
        # On the host, a command made only of conditional tokens has nothing to execute.
        tokens.insert(0, _template("true"))

    def stream(text: str | None) -> NfTemplate | None:
        return None if text is None else _template(text)

    process = tool.command_line
    return NfCommand(tuple(tokens), stream(process.stdin), stream(process.stdout), stream(process.stderr),
                     _environment(tool))


def _env_var(entry: standard.EnvironmentDef, tool: "_Tool") -> NfEnvVar:
    """Lower one `envDef` entry: literal text, or exactly `$(inputs.<name>)` of a `string` input (design §6)."""
    name, value = entry.envName, entry.envValue
    if (problem := environment_name_problem(name)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_ENVIRONMENT, problem)
    if whole := _INPUT_EXPRESSION.fullmatch(value.strip()):
        port = tool.inputs.get(whole["name"])
        declared = canonical(port.type_) if port is not None else None
        if whole["suffix"] or required(declared)[0] != "string":
            raise _Unsupported(
                SophiosErrorCode.NF_ENVIRONMENT,
                f"envValue {value!r} needs a string input, read whole as $(inputs.<name>); "
                f"{whole['name']} is declared {declared!r}"
            )
        return NfEnvVar(name, NfTemplate((NfInputReference(_identifier(whole["name"])),)))
    text = _literal_text(value)
    if text is None:  # a reference inside other text, or an expression the template reader refuses
        raise _Unsupported(
            SophiosErrorCode.NF_ENVIRONMENT,
            f"envValue {value!r} is not supported: write literal text or exactly $(inputs.<name>) of a string input"
        )
    return NfEnvVar(name, NfTemplate((NfLiteral(text),)))


def _environment(tool: "_Tool") -> tuple[NfEnvVar, ...]:
    requirement = tool.requirement(standard.EnvVarRequirement)
    return () if requirement is None else tuple(_env_var(entry, tool) for entry in requirement.envDef)


def _env_var_findings(tool: "_Tool", requirement: standard.EnvVarRequirement, *, path: str) -> list[_Finding]:
    """Report every `envDef` entry the lowering cannot take, each at its own path."""
    findings: list[_Finding] = []
    for index, entry in enumerate(requirement.envDef):
        entry_path = f"{path}.envDef[{index}]"
        findings.extend(_unconsumed_field_findings(fields_set(entry), "EnvironmentDef", path=entry_path))
        with _reported(findings, entry_path):
            _env_var(entry, tool)
    return findings


def _container(tool: "_Tool") -> str | None:
    docker = tool.requirement(standard.DockerRequirement)
    if docker is None:
        return None
    image: str | None = docker.dockerPull or docker.dockerImageId
    if not image:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "DockerRequirement must define dockerPull or dockerImageId")
    return image


def _resource_number(value: Any, *, name: str) -> int | float:
    match value:
        case bool():
            raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, f"{name} resource requirement must be numeric")
        case int() as number if number > 0:
            return number
        case float() as number if math.isfinite(number) and number > 0:
            return number
        case _:
            raise _Unsupported(_computed_or_invalid(value, SophiosErrorCode.NF_RESOURCE),
                               f"{name} resource requirement must be a positive finite number")


def _cpu_resource(value: Any) -> int:
    number = _resource_number(value, name="CPU")
    if isinstance(number, float):
        if not number.is_integer():
            raise _Unsupported(SophiosErrorCode.NF_RESOURCE, "CPU resource requirement must be a whole number")
        return int(number)
    return number


_CUDA_REQUIREMENT = "cwltool:CUDARequirement"
# The CUDA fields with no Nextflow counterpart, carried in the process's ext directive: each one present is
# reported with a note.
_UNCHECKED_CUDA_FIELDS = ("cudaVersionMin", "cudaComputeCapability")


def _cuda_capability(value: str | list[str]) -> str | tuple[str, ...]:
    return value if isinstance(value, str) else tuple(value)


def _device_count(value: Any) -> int:
    match value:
        case bool():
            pass
        case int() as count if count > 0:
            return count
    raise _Unsupported(_computed_or_invalid(value, SophiosErrorCode.NF_RESOURCE),
                       f"a CUDA device count must be a positive whole number, got {value!r}")


def _memory(value: Any) -> int | float:
    return _resource_number(value, name="memory")


# The value checks of a requirement's fields, in the order findings report them.
_REQUIREMENT_VALUES: Mapping[str, Mapping[str, Callable[[Any], Any]]] = {
    "ResourceRequirement": {"coresMin": _cpu_resource, "coresMax": _cpu_resource, "ramMin": _memory, "ramMax": _memory},
    _CUDA_REQUIREMENT: {
        "cudaDeviceCountMin": _device_count, "cudaDeviceCountMax": _device_count,
        "cudaComputeCapability": _cuda_capability,
    },
}


def _requirement_value(values: Mapping[str, Any], class_name: str, field: str, otherwise: str = "") -> Any:
    """The checked value of a requirement's `field`, of its field `otherwise` when `field` is not written, or None."""
    value = values.get(field)
    if value is None and otherwise:
        value = values.get(otherwise)
    check = _REQUIREMENT_VALUES[class_name].get(field)
    return value if value is None or check is None else check(value)


def _resources(tool: "_Tool") -> NfResources:
    resources = tool.requirement(standard.ResourceRequirement)
    resources_given, cuda = vars(resources) if resources is not None else {}, tool.extension(_CUDA_REQUIREMENT)
    # cwltool's device count: the minimum, else the maximum, else 1 (design §6, Resources and containers).
    gpus = None if cuda is None else _requirement_value(
        cuda, _CUDA_REQUIREMENT, "cudaDeviceCountMin", "cudaDeviceCountMax") or 1
    return NfResources(
        _requirement_value(resources_given, "ResourceRequirement", "coresMin", "coresMax"),
        _requirement_value(resources_given, "ResourceRequirement", "ramMin", "ramMax"), gpus,
        *(_requirement_value(cuda or {}, _CUDA_REQUIREMENT, name) for name in _UNCHECKED_CUDA_FIELDS),
    )


def _cuda_notes(step: str, tool: "_Tool") -> list[Diagnostic]:
    """A note for each CUDA field the generated pipeline does not check, at the step named `step`."""
    cuda = tool.extension(_CUDA_REQUIREMENT) or {}
    return [
        Diagnostic(
            Severity.NOTE, SophiosErrorCode.UNCHECKED_CUDA_FIELD,
            f"{_CUDA_REQUIREMENT} {name} {cuda[name]!r} is not checked: the Nextflow accelerator directive "
            "asks only for a number of GPUs",
            locator=Locator(step=step),
        )
        for name in _UNCHECKED_CUDA_FIELDS if name in cuda
    ]


def _output_template(raw_name: Any, port: standard.CommandOutputParameter) -> NfTemplate:
    glob = port.outputBinding.glob if port.outputBinding is not None else None
    match glob:
        case None:
            raise _Unsupported(SophiosErrorCode.NF_GLOB, f"CWL output {raw_name!r} must define outputBinding.glob")
        case str() if glob:
            return _template(glob)
        case str():
            raise _Unsupported(SophiosErrorCode.NF_GLOB, f"CWL output glob for {raw_name!r} cannot be empty")
        case _:
            raise _Unsupported(
                SophiosErrorCode.NF_GLOB,
                f"CWL output glob lists for {raw_name!r} are not supported by the Nextflow target"
            )


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


def _output_capture(raw_name: Any, port: standard.CommandOutputParameter) -> str | None:
    """Return the closed capture marker one CWL ``outputBinding`` declares.

    Recognition is a lookup against the frozen admitted table: exactly
    matching text names its marker and every other text is rejected, so no
    code inspects, transforms, or composes expression text. The paired glob
    is tokenized by the ordinary template tokenizer and must reduce to one
    literal, because a capture declaration projects the first glob match and
    that is provable only where the match set has one member.

    Args:
        raw_name (Any): The CWL output name, for diagnostics.
        port (CommandOutputParameter): The CWL output parameter.

    Raises:
        ValueError: If the binding declares any unadmitted capture shape.
            Callers in capability analysis catch this and report it by path;
            lowering lets it propagate.

    Returns:
        str | None: The capture marker, or None when the binding declares
            no capture behavior at all.
    """
    binding = port.outputBinding
    if binding is None:
        return None
    raw_eval = binding.outputEval
    if raw_eval is None:
        if binding.loadContents is not None:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE,
                f"loadContents on output {raw_name!r} has no approved lowering without its "
                "paired outputEval capture declaration"
            )
        return None
    text = raw_eval.strip()
    if reason := _NAMED_OUTPUT_EVAL_REJECTIONS.get(text):
        raise _Unsupported(SophiosErrorCode.NF_OUTPUT_EVAL, f"outputEval {text!r} is not representable: {reason}")
    capture = _ADMITTED_OUTPUT_EVAL.get(text)
    if capture is None:
        admitted = ", ".join(repr(shape) for shape in sorted(_ADMITTED_OUTPUT_EVAL))
        raise _Unsupported(
            SophiosErrorCode.NF_OUTPUT_EVAL, f"unsupported outputEval {raw_eval!r}; the approved set is exactly "
            f"{admitted}, recognized as capture declarations, and no other expression is "
            "evaluated"
        )
    declared_type = canonical(port.type_)
    if capture in _LOAD_CONTENTS_CAPTURES:
        if binding.loadContents is not True:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} captures file text and requires loadContents: true "
                "beside it"
            )
        inner = required(declared_type)[0]
        if isinstance(inner, str) and inner in _NUMERIC_TEXT_CAPTURE_TYPES:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} captures file text, and parsing that text into a "
                f"number is computation rather than a projection; output {raw_name!r} "
                f"declares {declared_type!r}. Declare type: string and parse downstream"
            )
        if inner != _TEXT_CAPTURE_TYPE:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} captures file text and is admitted only on a "
                f"type: string output; output {raw_name!r} declares {declared_type!r}"
            )
    else:
        if binding.loadContents is not None:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} declares output cardinality and is not paired with "
                "loadContents"
            )
        if _qualifier(declared_type) != "path":
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} declares single-valued File or Directory capture; "
                f"output {raw_name!r} declares {declared_type!r}"
            )
    glob = binding.glob
    if not isinstance(glob, str) or not glob:
        raise _Unsupported(
            SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} requires a single-literal outputBinding.glob"
        )
    literal = _text(_template(glob))
    if literal is None:
        raise _Unsupported(
            SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} requires a glob with no input reference; a "
            "reference's runtime value cannot be shown wildcard-free at compile time"
        )
    if wildcards := sorted(GLOB_WILDCARDS.intersection(literal)):
        found = ", ".join(repr(character) for character in wildcards)
        raise _Unsupported(
            SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} requires a glob with no wildcard character, but "
            f"{literal!r} contains {found}; projecting the first match would make "
            "CWL and Nextflow glob match ordering load-bearing, which is unproven"
        )
    return capture


# Keys a compiled CWL document carries beside its class's fields.
_DOCUMENT_KEYS = frozenset({"$namespaces", "$schemas"})
_WORKFLOW_CONSUMED_FIELDS = consumed(FIELDS["Workflow"]) | _DOCUMENT_KEYS | {"sophios:lang_version"}
# The outputBinding fields that declare capture behavior, as opposed to naming
# the file to capture.
_OUTPUT_CAPTURE_FIELDS = frozenset({"loadContents", "outputEval"})


def _default_value_findings(cwl_type: Any, default: Any, *, path: str) -> list[_Finding]:
    """Validate a declared scalar default against the supported value subset.

    A long or double type already has its own finding, so its default is not judged against it.
    """
    if _type_names(cwl_type) & _UNSUPPORTED_NUMBER_TYPES \
            or _type_problem(cwl_type) is not None:  # the type has its own refusal
        return []
    if default is None or isinstance(default, (Mapping, list)):
        return [_Finding(SophiosErrorCode.NF_DEFAULT,
                         f"{path}.default", "the Nextflow target supports JSON scalar defaults only")]
    if not _phase1_value_matches(cwl_type, default):
        return [_Finding(SophiosErrorCode.NF_DEFAULT, f"{path}.default", "value does not match its supported CWL type")]
    return []


def _rejection(rows: Mapping[str, Row], name: str, *, path: str, otherwise: tuple[SophiosErrorCode, str]
               ) -> _Finding:
    """The refusal of `name` at `path`: its row's code and note, or `otherwise` when the table has no rejected row for
    it."""
    row = rows.get(name)
    code, note = (row.code, row.note) if row is not None and row.code is not None else otherwise
    return _Finding(code, path, note)


def _unconsumed_field_findings(
    value: Iterable[str],
    cwl_class: str,
    *,
    path: str,
    kept: frozenset[str] | None = None,
) -> list[_Finding]:
    """Reject the fields of a `cwl_class` value the lowering does not consume.

    Args:
        value (Iterable[str]): The CWL value, or the names of its fields.
        cwl_class (str): Its class, a key of `FIELDS`.
        path (str): Where the value sits, for the finding.
        kept (frozenset[str] | None): The fields consumed, when not exactly the class's unrejected rows.

    Returns:
        list[_Finding]: One finding per field, with its row's note.
    """
    rows = FIELDS[cwl_class]
    return [
        _rejection(rows, name, path=f"{path}.{name}",
                   otherwise=(SophiosErrorCode.NF_FIELD, f"{name} is not consumed by the Nextflow lowering"))
        for name in sorted(set(value) - (consumed(rows) if kept is None else kept))
    ]


# The array schema class each parameter class's array types are; a class with none gets no array-schema check.
_ARRAY_SCHEMAS = {
    "WorkflowInputParameter": "InputArraySchema",
    "WorkflowOutputParameter": "OutputArraySchema",
    "CommandInputParameter": "CommandInputArraySchema",
}


def _members(cwl_type: Any) -> list[Any]:
    """The members of a union, or the one type."""
    return cwl_type if isinstance(cwl_type, list) else [cwl_type]


def _parameter_findings(cwl_class: str, *, path: str, fields: Iterable[str], cwl_type: Any,
                        arrays: Iterable[Iterable[str]]) -> list[_Finding]:
    """Reject a parameter's long or double type, and each field of it or of its array schemas not consumed.

    Args:
        cwl_class (str): The parameter's class, a key of `FIELDS`.
        path (str): Where the parameter sits, for the finding.
        fields (Iterable[str]): The fields the parameter writes.
        cwl_type (Any): Its canonical type.
        arrays (Iterable[Iterable[str]]): The fields each array schema of its type writes.

    Returns:
        list[_Finding]: The number-type findings, then one finding per field, with its row's note.
    """
    findings = _number_type_findings(cwl_type, path=path)
    if schema_class := _ARRAY_SCHEMAS.get(cwl_class):
        findings.extend(finding for schema in arrays
                        for finding in _unconsumed_field_findings(schema, schema_class, path=f"{path}.type"))
    findings.extend(_unconsumed_field_findings(fields, cwl_class, path=path))
    return findings


def _port_findings(port: Any, cwl_class: str, *, path: str) -> list[_Finding]:
    """`_parameter_findings` of a tool's loaded port."""
    return _parameter_findings(
        cwl_class, path=path, fields=fields_set(port), cwl_type=canonical(port.type_),
        arrays=[fields_set(member) for member in _members(port.type_)
                if isinstance(member, (standard.CommandInputArraySchema, standard.CommandOutputArraySchema))])


def _declared_findings(declaration: PortDeclaration, cwl_class: str, *, path: str,
                       source: Ref | str | None = None) -> list[_Finding]:
    """`_parameter_findings` of a workflow's port as it states it."""
    cwl_type = declaration.type.canonical
    return _parameter_findings(
        cwl_class, path=path, fields=_port_fields(declaration, source), cwl_type=cwl_type,
        arrays=[member for member in _members(cwl_type) if _is_array_type(member)])


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
    inner = required(cwl_type)[0]
    if _is_array_type(inner):
        if not isinstance(value, list):
            return False
        try:
            item_type = _array_item_type(inner)
        except ValueError:
            return False
        return all(_phase1_value_matches(item_type, item) for item in value)
    match inner, value:
        case "string", str():
            return True
        case "boolean", bool():
            return True
        case "int", int() if not isinstance(value, bool):
            return True
        case "float", (int() | float()) if not isinstance(value, bool):
            return math.isfinite(value)
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


def _requirement_findings(tool: "_Tool", section: str, *, path: str) -> list[_Finding]:
    """Apply closed-world analysis to each requirement or hint of a tool's section.

    Args:
        tool (_Tool): The tool holding them, whose inputs an InitialWorkDirRequirement stages.
        section (str): `requirements` or `hints`.
        path (str): Where the section sits, for the finding.

    Returns:
        list[_Finding]: One finding per unsupported requirement, field or value.
    """
    findings: list[_Finding] = []
    typed = iter(getattr(tool.process, section) or ())
    for place, extension in tool.written[section].items():
        requirement_path = f"{path}.{place}"
        each: Any = None if extension else next(typed)
        if extension is None and not isinstance(each, standard.ProcessRequirement):
            # The standard lets a hint it cannot read as a class of its own go unread; the lowering cannot lower one.
            findings.append(_Finding(SophiosErrorCode.NF_REQUIREMENT, requirement_path,
                                     "a hint that is not a requirement of the standard is not supported by the "
                                     "Nextflow target"))
            continue
        class_name, fields = extension if extension else (each.class_, fields_set(each))
        values = extension[1] if extension else vars(each)
        if class_name not in consumed(REQUIREMENT_CLASSES["CommandLineTool"]):
            findings.append(_rejection(REQUIREMENT_CLASSES["CommandLineTool"], class_name, path=requirement_path,
                                       otherwise=(SophiosErrorCode.NF_REQUIREMENT,
                                                  f"{class_name} is not supported by the Nextflow target")))
        elif class_name == "NetworkAccess" and each.networkAccess is not True:
            findings.append(
                _Finding(SophiosErrorCode.NF_NETWORK_ACCESS,
                         f"{requirement_path}.networkAccess", "only the literal true is supported: a Nextflow "
                         "task has network access, and the generated pipeline cannot withhold it")
            )
        else:
            findings.extend(_unconsumed_field_findings(fields, class_name, path=requirement_path))
            for field_name, check in _REQUIREMENT_VALUES.get(class_name, {}).items():
                if (value := values.get(field_name)) is not None:
                    with _reported(findings, f"{requirement_path}.{field_name}"):
                        check(value)
            if isinstance(each, standard.EnvVarRequirement):
                findings.extend(_env_var_findings(tool, each, path=requirement_path))
            if isinstance(each, standard.InitialWorkDirRequirement):
                findings.extend(_iwdr_listing_findings(tool, each, path=requirement_path))
    return findings


def _template_positions(tool: "_Tool") -> list[tuple[Any, str]]:
    """Pair every templated tool value with the CWL path it was written at."""
    positions: list[tuple[Any, str]] = []
    for index, argument in enumerate(tool.command_line.arguments or []):
        argument_path = f".run.arguments[{index}]"
        if not isinstance(argument, standard.CommandLineBinding):
            positions.append((argument, argument_path))
        elif argument.valueFrom is not None:
            positions.append((argument.valueFrom, f"{argument_path}.valueFrom"))
    for raw_name, port in tool.inputs.items():
        if port.inputBinding is not None and port.inputBinding.valueFrom is not None:
            positions.append((port.inputBinding.valueFrom, f".run.inputs.{raw_name}.inputBinding.valueFrom"))
    for stream in ("stdin", "stdout", "stderr"):
        if (value := getattr(tool.command_line, stream)) is not None:
            positions.append((value, f".run.{stream}"))
    for raw_name, port in tool.outputs.items():
        if port.outputBinding is not None and port.outputBinding.glob is not None:
            positions.append((port.outputBinding.glob, f".run.outputs.{raw_name}.outputBinding.glob"))
    return positions


def _computed_value_from_findings(value_from: Any, *, path: str) -> list[_Finding]:
    """Reject a valueFrom that computes (design §6, Expressions): one is literal text or a projection."""
    try:
        _template(value_from)
    except ValueError:
        return [_Finding(
            SophiosErrorCode.NF_EXPRESSION,
            f"{path}.valueFrom", "a computed valueFrom is not supported; write literal text or $(inputs.<name>), "
            "and on a File or Directory input $(inputs.<name>.path) or $(inputs.<name>.basename)")]
    return []


#: Where CWL needs a string, what it calls the value there; every other templated value is a command word.
_STRING_POSITIONS = {"glob": "a glob", "stdin": "stdin", "stdout": "a stdout target", "stderr": "a stderr target"}


def _path_reference_findings(tool: "_Tool", entries: Mapping[str, NfEntry]) -> list[_Finding]:
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
    findings: list[_Finding] = []
    for value, position in _template_positions(tool):
        if not isinstance(value, str):
            continue  # a list of globs has a finding of its own
        whole = _INPUT_EXPRESSION.fullmatch(value.strip()) is not None
        kind = next((kind for kind in _STRING_POSITIONS if position.endswith(f".{kind}")), None)
        try:
            linked = linked_reference(_template(value))
        except ValueError:
            linked = None  # an expression, which has a finding of its own
        references = (match.group("name", "suffix") for match in _INTERPOLATION.finditer(value))
        for name, suffix in dict.fromkeys(reference for reference in references if reference[0]):
            port, entry = tool.inputs.get(name), entries.get(name)
            if port is None or entry is not None and entry.name is not None:
                continue
            declared = canonical(port.type_)
            qualifier = _qualifier(declared)
            if qualifier is None:
                continue  # the input pass reports a type the backend does not lower
            is_path = qualifier == "path"
            if suffix is not None and not is_path:
                findings.append(
                    _Finding(SophiosErrorCode.NF_PATH_REFERENCE,
                             position, f"$(inputs.{name}.{suffix}) requires a File or Directory input, "
                             f"and {name} is declared {declared!r}")
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
                problem = f"$(inputs.{name}) embedded in other text renders a File object as JSON in CWL; {instead}"
            elif suffix is None and kind is not None:
                problem = (f"$(inputs.{name}) is the File or Directory object, which CWL does not take as "
                           f"{_STRING_POSITIONS[kind]}; {instead}")
            elif kind == "stdin" and not whole:
                problem = ("stdin reads a staged file by its whole path, which "
                           f"$(inputs.{name}.{suffix}) inside other text is not; {instead}")
            elif kind == "stdin" and suffix == "basename":
                problem = (f"stdin names a file by its path, and $(inputs.{name}.basename) is only its name; "
                           f"{instead}")
            elif kind == "stdin" and required(declared)[0] == "Directory":
                problem = f"stdin reads a file, and $(inputs.{name}.path) names a Directory"
            elif suffix == "path" and kind in ("stdout", "stderr"):
                problem = (f"$(inputs.{name}.path) is an absolute path, and CWL takes only a relative name as "
                           f"{_STRING_POSITIONS[kind]}; {instead}")
            elif suffix == "path" and kind == "glob" and entry is None:
                problem = (f"$(inputs.{name}.path) is an absolute path outside the output directory, which no CWL "
                           f"glob matches; {instead}")
            else:
                continue
            findings.append(_Finding(SophiosErrorCode.NF_PATH_REFERENCE, position, problem))
    return findings


def _tool_capability_findings(tool: "_Tool") -> list[_Finding]:
    """Collect unsupported executable semantics of a step's tool without lowering it."""
    findings: list[_Finding] = []
    process = tool.process
    if not isinstance(process, standard.CommandLineTool):
        return [_Finding(SophiosErrorCode.NF_NOT_A_COMMAND_LINE_TOOL,
                         ".run.class", f"unsupported compiled step class {process.class_!r}")]

    findings.extend(_unconsumed_field_findings(fields_set(process), "CommandLineTool", path=".run"))
    try:
        entries: dict[str, NfEntry] | None = _iwdr_entries(tool)
    except ValueError:
        entries = None  # the listing's own findings report it; what it stages is unknown, so no reference is checked
    if entries is not None:
        findings.extend(_path_reference_findings(tool, entries))

    for section_name in ("requirements", "hints"):
        findings.extend(_requirement_findings(tool, section_name, path=f".run.{section_name}"))
    renamed_names = {name for name, entry in (entries or {}).items() if entry.name is not None}
    findings.extend(_iwdr_rename_reference_findings(tool, renamed_names))
    shell_mode_active = tool.requirement(standard.ShellCommandRequirement) is not None
    if shell_mode_active and tool.requirement(standard.DockerRequirement) is not None:
        # A pair with an inherited half is refused where that half is written, which the tool's file does not hold.
        written = next((place for name in ("ShellCommandRequirement", "DockerRequirement")
                        for place, cls in tool.inherited.items() if cls == name), None)
        findings.append(
            _Finding(SophiosErrorCode.NF_SHELL_IN_CONTAINER,
                     f".run{'' if written is None else f'.{written}'}", "ShellCommandRequirement beside a "
                     "DockerRequirement is not supported: CWL runs the command as /bin/sh -c after the image's "
                     "ENTRYPOINT, which the Nextflow task does not")
        )
    with _reported(findings, ".run.inputs"):
        _normalized_identifiers(tool.inputs, context="tool input")
    for raw_name, port in tool.inputs.items():
        input_path = f".run.inputs.{raw_name}"
        cwl_type = canonical(port.type_)
        findings.extend(_port_findings(port, "CommandInputParameter", path=input_path))
        if port.default is not None:
            findings.extend(_default_value_findings(cwl_type, port.default, path=input_path))
        binding = port.inputBinding
        if binding is None:
            continue
        input_binding_path = f"{input_path}.inputBinding"
        findings.extend(_unconsumed_field_findings(fields_set(binding), "CommandLineBinding",
                                                   path=input_binding_path))
        inner, optional = required(cwl_type)
        if optional and isinstance(inner, list):
            findings.append(_Finding(SophiosErrorCode.NF_TYPE, f"{input_path}.type",
                                     f"unsupported optional CWL union {cwl_type!r}"))
            continue
        if _is_array_type(inner) and _type_names(inner.get("items")) - {"null"} == {"boolean"}:
            findings.append(
                _Finding(SophiosErrorCode.NF_BOOLEAN_BINDING,
                         input_binding_path, "a boolean array's binding is not supported: CWL adds only its "
                         "prefix, when the array is not empty, and never an item's true or false")
            )
        if binding.shellQuote is False:
            with _reported(findings, f"{input_binding_path}.shellQuote"):
                _shell_literal_value(raw_name, binding, shell_mode=shell_mode_active)
        elif binding.valueFrom is not None:
            if inner == "boolean":
                with _reported(findings, f"{input_binding_path}.valueFrom"):
                    _boolean_flag_reference(raw_name, _identifier(raw_name), binding.valueFrom)
            else:
                findings.extend(_computed_value_from_findings(binding.valueFrom, path=input_binding_path))

    for argument_index, argument in enumerate(process.arguments or []):
        if not isinstance(argument, standard.CommandLineBinding):
            continue
        argument_path = f".run.arguments[{argument_index}]"
        findings.extend(_unconsumed_field_findings(fields_set(argument), "CommandLineBinding", path=argument_path))
        if argument.shellQuote is False:
            with _reported(findings, f"{argument_path}.shellQuote"):
                _shell_literal_value(f"arguments[{argument_index}]", argument, shell_mode=shell_mode_active)
        elif argument.valueFrom is not None:
            findings.extend(_computed_value_from_findings(argument.valueFrom, path=argument_path))

    with _reported(findings, ".run.outputs"):
        _normalized_identifiers(tool.outputs, context="tool output")
    for raw_name, port in tool.outputs.items():
        output_path = f".run.outputs.{raw_name}"
        findings.extend(_port_findings(port, "CommandOutputParameter", path=output_path))
        binding = port.outputBinding
        # An outputBinding that declares capture behavior is diagnosed
        # by _output_capture alone, which reports the type it requires;
        # the generic type finding would only restate that less
        # precisely.
        declares_capture = binding is not None and not _OUTPUT_CAPTURE_FIELDS.isdisjoint(fields_set(binding))
        if _qualifier(canonical(port.type_)) != "path" and not declares_capture:
            findings.append(
                _Finding(SophiosErrorCode.NF_OUTPUT_TYPE,
                         f"{output_path}.type", "an output is collected by glob as a File or Directory, "
                         "or captured as text with loadContents and outputEval")
            )
        if binding is None:
            continue
        output_binding_path = f"{output_path}.outputBinding"
        findings.extend(_unconsumed_field_findings(fields_set(binding), "CommandOutputBinding",
                                                   path=output_binding_path))
        with _reported(findings, output_binding_path):
            _output_capture(raw_name, port)
    return findings


#: The classes that decide only whether a workflow may use a feature, which cwltool reads at the step (resolved-view
#: design §8): on a workflow or a step each is an inert declaration, and a tool that inherits one lowers it to nothing.
_FEATURES = frozenset({"SubworkflowFeatureRequirement", "ScatterFeatureRequirement",
                       "MultipleInputFeatureRequirement", "StepInputExpressionRequirement"})

_Entry = TypeVar("_Entry")


def _classes(process: Process, written: Written, section: str) -> set[str]:
    """The classes a tool's requirements or its hints hold: the loader's, then the vendor's."""
    return {*(type(held).__name__ for held in getattr(process, section) or ()
              if isinstance(held, standard.ProcessRequirement)),
            *(found[0] for found in written[section].values() if found)}


@dataclass(frozen=True, slots=True)
class _Tool:
    """A step's tool as the lowering reads it: the registry's tool, read once, with the requirements and hints it
    inherits written into its document (design §8), so every one is lowered, or refused, as the same one written on
    the tool."""

    process: Process
    inputs: Mapping[str, Any]
    #: Only a CommandLineTool's outputs are read: a tool of another class is refused as a whole.
    outputs: Mapping[str, Any]
    #: The places its requirements and hints are written at, in order, by section, with the vendor's among them.
    written: Written
    #: Each place an inherited requirement or hint is written at, `requirements.<class>` or `hints.<class>`, with its
    #: class.
    inherited: Mapping[str, str]

    @classmethod
    def of(cls, tool: Tool, inherits: Iterable[Requirement] = ()) -> "_Tool":
        """The tool with each requirement in `inherits` written into it, unless the tool names one of that class
        that wins over it.

        Raises:
            SophiosError: `api004`, when the standard's loader refuses the tool or an inherited body.
        """
        loaded, shared = tool.loaded
        process = copy.copy(loaded)
        written = {section: dict(places) for section, places in shared.items()}
        places: dict[str, str] = {}
        for each in inherits:
            section = "hints" if each.hint else "requirements"
            if each.cls in _FEATURES or any(each.cls in _classes(process, written, kind)
                                            for kind in ("requirements", section)):
                continue
            body = each.body if isinstance(each.body, Mapping) else {}
            if ":" in each.cls:
                written[section][each.cls] = (each.cls, dict(body))
            else:
                written[section][each.cls] = None
                setattr(process, section, [*(getattr(process, section) or ()), inherited(process, each.cls, body)])
            places[f"{section}.{each.cls}"] = each.cls
        outputs = ports_named(process.outputs) if isinstance(process, standard.CommandLineTool) else {}
        return cls(process, ports_named(process.inputs), outputs, written, places)

    @property
    def command_line(self) -> standard.CommandLineTool:
        """The process, for the passes that read a command, which only a CommandLineTool is given."""
        assert isinstance(self.process, standard.CommandLineTool)
        return self.process

    def requirement(self, cls: type[_Entry]) -> _Entry | None:
        """The tool's requirement of the standard's class `cls`, else its hint of it."""
        return requirement_of(self.process, cls)

    def extension(self, name: str) -> Mapping[str, Any] | None:
        """The fields of the tool's requirement of a vendor's class, else its hint of it."""
        return next((found[1] for entries in self.written.values() for found in entries.values()
                     if found is not None and found[0] == name), None)


@dataclass(frozen=True, slots=True)
class _Step:
    """One tool step as the lowering reads it: the view's, its tool as the lowering reads it, and the step-only
    inputs its process carries (design §9)."""

    view: ToolStep
    tool: _Tool
    carried: frozenset[str]

    @property
    def node(self) -> ResolvedStep:
        """The step itself."""
        return self.view.step

    @property
    def scattered(self) -> tuple[str, ...]:
        """The inputs the step scatters over; none for a step that does not scatter."""
        return self.node.scatter.inputs if self.node.scatter is not None else ()

    def bound(self) -> Iterator[tuple[StepInput, tuple[Through | Crossing, ...]]]:
        """Each step input the process receives and its sources, in `in:` order: those the tool declares, and the
        step-only ones it carries."""
        for port, sources in zip(self.node.inputs, self.view.sources, strict=True):
            if port.declared is not None or port.name in self.carried:
                yield port, sources

    def wired(self) -> Iterator[tuple[StepInput, list[str]]]:
        """Each step input the process receives, of a shape the idioms read, and its sources spelled."""
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


def _port_fields(declaration: PortDeclaration, source: Ref | str | None = None) -> set[str]:
    """The fields a workflow port states, with its `outputSource` if it has one: a shorthand port states none."""
    fields = set() if declaration.shorthand else {"type", *(name for name, _value in declaration.passthrough)}
    if declaration.has_format and not declaration.shorthand:
        fields.add("format")
    if declaration.has_default and not declaration.shorthand:
        fields.add("default")
    return {*(fields or {"type"}), "outputSource"} if source is not None else fields


def _step_fields(node: ResolvedStep) -> set[str]:
    """The name of every field a step writes, as CWL keys a WorkflowStep: its `scatter`, `when`, requirements and
    hints, and the rest."""
    fields = {"id", "in", "out", "run", *dict(node.fields),
              *("hints" if requirement.hint else "requirements" for requirement in node.requirements)}
    if node.scatter is not None:
        fields.add("scatter")
        if node.scatter.method is not None:
            fields.add("scatterMethod")
    if node.condition is not None:
        fields.add("when")
    return fields


def _call_findings(workflow: ResolvedWorkflow) -> list[_Finding]:
    """Reject the semantics of a call the flat executable graph would erase.

    The core compiler owns composition, edge resolution, and namespace identity. The backend still owns its
    capability boundary: what a call or its nested workflow says must be consumed here or rejected before the call
    is inlined.
    """
    findings: list[_Finding] = []
    for step in workflow.steps:
        child = step.run
        if not isinstance(child, ResolvedWorkflow):
            continue
        found: list[_Finding] = []
        if step.condition is not None:
            found.append(_Finding(SophiosErrorCode.NF_CONDITIONAL_CALL, ".when",
                                  "a when on a nested-workflow step is not supported; put it on a tool step"))
        if step.scatter is not None:
            found.append(_Finding(SophiosErrorCode.NF_SCATTERED_CALL, ".scatter",
                                  "scatter on a nested workflow step is deferred beyond this lowering; scattering a "
                                  "sub-DAG is not the single-process shape scatter supports"))
        elif "scatterMethod" in dict(step.fields):
            found.append(_Finding(SophiosErrorCode.NF_SCATTER_METHOD_ALONE, ".scatterMethod",
                                  "scatterMethod without scatter is not executable"))
        found.extend(_unconsumed_field_findings(_step_fields(step), "WorkflowStep", path=""))
        found.extend(_own_requirement_findings(step, "WorkflowStep", path=""))
        for port in step.inputs:
            input_path = f".in.{port.name}"
            # The inlining joins one source to each input a call binds.
            merging = sorted(_idioms(port))
            if len(port.sources) > 1:
                merging.append("source")
            found.extend(
                _Finding(SophiosErrorCode.NF_CALL_INPUT, f"{input_path}.{name}",
                         "a nested workflow step's input takes one source as it is; merge on a step inside the "
                         "nested workflow")
                for name in merging
            )
            if port.expression is None:
                found.extend(_unconsumed_field_findings(
                    _record_fields(port) - _idioms(port), "WorkflowStepInput",
                    path=input_path))
        found.extend(_unconsumed_field_findings(_workflow_fields(child), "Workflow", path=".run",
                                                kept=_WORKFLOW_CONSUMED_FIELDS))
        found.extend(_own_requirement_findings(child, "Workflow", path=".run"))
        for declared in child.inputs:
            port_path = f".run.inputs.{declared.name}"
            found.extend(_declared_findings(declared.declaration, "WorkflowInputParameter", path=port_path))
            # Inlining replaces a nested workflow's input with the calling step's binding, where CWL would apply the
            # input's default to a null one.
            if declared.declaration.has_default and not declared.declaration.shorthand:
                found.append(_Finding(SophiosErrorCode.NF_CALL_INPUT, f"{port_path}.default",
                                      "a nested workflow input's default is not supported: bind the input on the "
                                      "calling step"))
        for output in child.outputs:
            found.extend(_declared_findings(output.declaration, "WorkflowOutputParameter",
                                            path=f".run.outputs.{output.name}", source=output.source))
        findings.extend(replace(each, step=step) for each in found)
        findings.extend(_call_findings(child))
    return findings


def _workflow_fields(workflow: ResolvedWorkflow) -> set[str]:
    """The name of every field a workflow writes, as CWL keys a Workflow."""
    return {"class", "cwlVersion", "inputs", "outputs", "steps", *dict(workflow.fields),
            *("hints" if requirement.hint else "requirements" for requirement in workflow.requirements)}


def _carried(tool: ToolStep) -> frozenset[str]:
    """The step-only inputs a step's process carries (design §9): each one but those fed only by root workflow
    inputs, through every call, that neither the step's `when` nor its `scatter` reads. Such an input cannot change
    what the step does or when it starts, so it is left out."""
    step = tool.step
    read = references(step.condition.expr) if step.condition is not None and step.condition.expr is not None else set()
    read |= set(step.scatter.inputs) if step.scatter is not None else set()
    return frozenset(port.name for port, sources in zip(step.inputs, tool.sources, strict=True)
                     if port.declared is None and (port.name in read or any(
                         not isinstance(found, Through) or not isinstance(found.ref, InputRef) for found in sources)))


def _lists(cwl_type: Any) -> bool:
    """Whether a value of `cwl_type` is a list, null aside."""
    return _is_array_type(required(cwl_type)[0])


def _workflow_capability_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Apply closed-world analysis to the workflow, its steps, and its boundary values."""
    findings = _unconsumed_field_findings(_workflow_fields(workflow), "Workflow", path="",
                                          kept=_WORKFLOW_CONSUMED_FIELDS)
    findings.extend(_own_requirement_findings(workflow, "Workflow", path=""))
    with _reported(findings, ".inputs"):
        _normalized_identifiers([port.name for port in workflow.inputs], context="workflow input")
    for given in workflow.inputs:
        input_path = f".inputs.{given.name}"
        findings.extend(_declared_findings(given.declaration, "WorkflowInputParameter", path=input_path))
        cwl_type = given.declaration.type.canonical
        has_default = given.declaration.has_default and not given.declaration.shorthand
        if has_default:
            findings.extend(_default_value_findings(cwl_type, given.declaration.default, path=input_path))
        if not given.has_value and not has_default and not required(cwl_type)[1]:
            findings.append(_Finding(SophiosErrorCode.NF_INPUT_WITHOUT_VALUE,
                            input_path, "required workflow input value is missing"))
        if given.has_value and given.value is None and not required(cwl_type)[1]:
            findings.append(_Finding(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                            input_path, "explicit null input values are not valid for a required input"))
        elif given.has_value and given.value is None:
            # An explicit null on an optional input is only safe for specific consuming positions;
            # _absent_optional_findings analyzes each consuming step and port for that.
            pass
        elif (problem := _type_problem(cwl_type)) is not None:
            findings.append(_Finding(SophiosErrorCode.NF_TYPE, f"{input_path}.type", problem))
        elif given.has_value and not _phase1_value_matches(cwl_type, local_locations_as_paths(given.value)):
            findings.append(_Finding(SophiosErrorCode.NF_INPUT_VALUE,
                                     input_path, "supplied value does not match its supported CWL type"))
    with _reported(findings, ".outputs"):
        _normalized_identifiers([port.name for port in workflow.outputs], context="workflow output")
    for output in workflow.outputs:
        findings.extend(_declared_findings(output.declaration, "WorkflowOutputParameter",
                                           path=f".outputs.{output.name}", source=output.source))
    with _reported(findings, ".steps"):
        _normalized_identifiers([step.node.name for step in steps], context="workflow step", variable=False)
    for step in steps:
        fields = _step_fields(step.node)
        found = _unconsumed_field_findings(fields, "WorkflowStep", path="")
        found.extend(_own_requirement_findings(step.node, "WorkflowStep", path=""))
        if step.node.scatter is None and "scatterMethod" in fields:
            found.append(_Finding(SophiosErrorCode.NF_SCATTER_METHOD_ALONE, ".scatterMethod",
                                  "scatterMethod without scatter is not executable"))
        for port in step.node.inputs:
            if port.expression is None:
                found.extend(_unconsumed_field_findings(_record_fields(port),
                                                        "WorkflowStepInput", path=f".in.{port.name}"))
        findings.extend(replace(each, step=step.node) for each in found)
    return findings


def _own_requirement_findings(node: ResolvedWorkflow | ResolvedStep, owner: str, *, path: str) -> list[_Finding]:
    """A workflow's or a step's own requirements and hints: a feature class is consumed there, an inert declaration
    with no field of its own; every other class is inherited by each tool step inside, and judged there as written
    on its tool (design §8)."""
    findings = [_Finding(SophiosErrorCode.NF_INVALID_CWL,
                         f"{path}.{section}", f"CWL {owner} {section} must be a mapping or list")
                for section in ("requirements", "hints") if section in dict(node.fields)]
    for requirement in node.requirements:
        if requirement.cls in _FEATURES:
            findings.extend(_unconsumed_field_findings(
                requirement.body if isinstance(requirement.body, Mapping) else {}, requirement.cls,
                path=f"{path}.{'hints' if requirement.hint else 'requirements'}.{requirement.cls}"))
    return findings


def _scatter_source_findings(step: _Step, raw_name: str, source_types: Mapping[str, Any]) -> list[_Finding]:
    """Require one array-typed workflow-input source for the scattered input.

    The compiler has already refused an element type the scattered port cannot take.
    """
    sources = next((spelled for wired, spelled in step.wired() if wired.name == raw_name), None)
    if sources is None:
        # Every unrecognized source shape is already reported by path.
        return []
    if len(sources) != 1:
        return []  # the step-input pass refuses a scattered input that joins sources
    source = sources[0]
    if "/" in source:
        # A process-output source is reported once, by the cross-step pass.
        return []
    declared = source_types.get(source)
    if not _is_array_type(required(declared)[0]):
        return [_Finding(SophiosErrorCode.NF_SCATTER_SOURCE, f".in.{raw_name}",
                         f"a scattered input needs an array-typed source; {source!r} declares {declared!r}", step.node)]
    return []


def _scatter_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Validate every scattered step against the single- and multi-input scatter lowerings."""
    findings: list[_Finding] = []
    source_types = _source_types(workflow, steps)
    for step in steps:
        if step.node.scatter is None:
            continue
        names = step.scattered
        if not names:
            findings.append(_Finding(SophiosErrorCode.NF_INVALID_CWL, ".scatter",
                                     "scatter must name one input, as a string or a non-empty list", step.node))
            continue
        method = step.node.scatter.method
        if method is not None and method not in SCATTER_METHODS:
            findings.append(_Finding(SophiosErrorCode.NF_INVALID_CWL,
                            ".scatterMethod", f"unsupported CWL scatter method {method!r}", step.node))
        elif len(names) > 1 and method is None:
            # At exactly one scattered input all three methods coincide, so a method is required to decide two or
            # more; each of them is an approved multi-input lowering (design §6, Topology).
            findings.append(
                _Finding(SophiosErrorCode.NF_INVALID_CWL,
                         ".scatterMethod", f"multi-input scatter over {len(names)} inputs requires an "
                         "explicit scatterMethod", step.node)
            )
        for raw_name in names:
            port = step.tool.inputs.get(raw_name)
            if port is None:
                # No process input receives the element: a step-only input is a val after the tool's own.
                findings.append(_Finding(SophiosErrorCode.NF_SCATTERED_STEP_ONLY_INPUT,
                                         f".in.{raw_name}", "scatter over an input the step's tool does not "
                                         "declare is not supported", step.node))
                continue
            if _is_array_type(required(canonical(port.type_))[0]):
                findings.append(
                    _Finding(SophiosErrorCode.NF_SCATTER_OF_ARRAYS,
                             ".scatter", f"scattered input {raw_name!r} is array-typed; scattering over an "
                             "array of arrays is deferred beyond this lowering", step.node)
                )
                continue
            findings.extend(_scatter_source_findings(step, raw_name, source_types))
    findings.extend(_scatter_edge_findings(steps))
    return findings


def _scatter_edge_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject every process edge whose cardinality a scattered step changes and no lowering keeps.

    A scattered step's output is gathered, in invocation order, into one array; a nested_crossproduct result
    reaches workflow outputs only. An unscattered step's output reaches a scattered step as one value: it
    broadcasts to every task, and it is not an array to scatter over, since no tool output is an array.
    """
    findings: list[_Finding] = []
    scattered = {step.node.name: step.node.scatter for step in steps if step.scattered}
    for step in steps:
        for port, sources in step.wired():
            for source in sources:
                if "/" not in source:
                    continue
                scatter = scattered.get(source.rsplit("/", maxsplit=1)[0])
                if scatter is not None:
                    # Every scatter is indexed, so its outputs are gathered once, in invocation order (design §6,
                    # Topology, Gather).
                    if scatter.method == "nested_crossproduct" and len(set(scatter.inputs)) > 1:
                        findings.append(_Finding(SophiosErrorCode.NF_NESTED_RESULT, f".in.{port.name}",
                                                 f"{source!r} is a nested_crossproduct result, which reaches "
                                                 "workflow outputs only", step.node))
                elif port.name in step.scattered:
                    findings.append(_Finding(SophiosErrorCode.NF_SCATTER_SOURCE, f".in.{port.name}",
                                             f"a scattered input needs an array-typed source; {source!r}, from a "
                                             "step that is not scattered, is one value", step.node))
    return findings


def _step_input_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Check each step input the fixed idioms lower: a default, a linkMerge, a pickValue, or two or more sources.

    Each source is one value (design §6, Topology, Step inputs). Null is the [] sentinel, so an idiom may not take
    an array source where [] would be read as null, and a merged list may not hold a source that may be null.
    """
    source_types = _source_types(workflow, steps)
    by_name = {step.node.name: step for step in steps}

    def is_array(source: str) -> bool:
        if "/" in source:  # a scatter's outputs are gathered
            return (producer := by_name.get(source.rsplit("/", maxsplit=1)[0])) is not None and bool(producer.scattered)
        return _is_array_type(required(source_types.get(source))[0])

    def may_be_null(source: str) -> bool:
        if "/" in source:
            producer = by_name.get(source.rsplit("/", maxsplit=1)[0])
            return producer is not None and producer.node.condition is not None
        return required(source_types.get(source))[1]

    def problems(step: _Step, port: StepInput, sources: list[str]) -> Iterator[tuple[SophiosErrorCode, str, str]]:
        """Each problem of one step input: its code, the field of the input it is at, and the reason."""
        link_merge, pick_value = port.link_merge, port.pick_value
        if len(sources) < 2 and not _idioms(port):
            return
        if port.name in step.scattered:
            yield SophiosErrorCode.NF_SCATTER_SOURCE, "", "a scattered input takes its one source as it is"
            return
        merges = link_merge if link_merge is not None else ("merge_nested" if len(sources) > 1 else None)
        if link_merge is not None and link_merge not in LINK_MERGES:
            yield SophiosErrorCode.NF_INVALID_CWL, ".linkMerge", f"unsupported linkMerge {link_merge!r}"
            return
        if pick_value is not None and pick_value not in PICK_VALUES:
            yield SophiosErrorCode.NF_INVALID_CWL, ".pickValue", f"unsupported pickValue {pick_value!r}"
            return
        if pick_value is not None and merges is None:
            yield (SophiosErrorCode.NF_JOIN, ".pickValue",
                   "pickValue picks from a list; one source with no linkMerge is not one")
        # A step-only input has no declaration: its type is what it delivers.
        declared_type = canonical(step.tool.inputs[port.name].type_) if port.declared is not None else port.type
        if port.has_default:
            if port.declared is None and _parameter_type(declared_type) not in PARAM_TYPES:
                yield (SophiosErrorCode.NF_DEFAULT, ".default",
                       "a default of an input the step's tool does not declare needs a source of a scalar type to be "
                       "checked against; give the value as a workflow input")
            elif sources and merges is not None:
                yield (SophiosErrorCode.NF_JOIN, ".default",
                       "a default never applies beside a linkMerge or a pickValue: the joined value is not null")
            elif required(declared_type)[0] in ("File", "Directory"):
                yield (SophiosErrorCode.NF_DEFAULT, ".default", "a step-input default of a File or Directory input "
                       "is not supported; give the path as a workflow input")
            elif port.default is None or isinstance(port.default, (Mapping, list)) or not _phase1_value_matches(
                    declared_type, port.default):
                yield (SophiosErrorCode.NF_DEFAULT, ".default",
                       "the Nextflow target supports a JSON scalar default of the tool input's type")
        # A default is a JSON scalar, so it never sits beside an array source of its type.
        arrays = [source for source in sources if is_array(source)]
        if arrays and pick_value is not None:
            yield (SophiosErrorCode.NF_JOIN, "", f"{arrays[0]!r} is an array, and [] stands for both a null and an "
                   "empty array, so pickValue cannot take it")
        elif arrays and merges == "merge_nested":
            yield (SophiosErrorCode.NF_JOIN, "",
                   f"merge_nested over the array {arrays[0]!r} makes a nested array, which is not supported")
        if merges is not None and pick_value is None and (nullable := [s for s in sources if may_be_null(s)]):
            yield (SophiosErrorCode.NF_JOIN, "", f"{nullable[0]!r} may be null, and a joined list has no lowering of "
                   "a null element; pick from it with pickValue")
        gives_list = pick_value not in PICKS_ONE
        if merges is not None and port.declared is not None and gives_list != _lists(declared_type):
            yield (SophiosErrorCode.NF_JOIN, "", f"the step input gives {'a list' if gives_list else 'one value'}, "
                   f"but its tool input declares {declared_type!r}")

    return [_Finding(code, f".in.{port.name}{field}", text, step.node)
            for step in steps for port, sources in step.wired() for code, field, text in problems(step, port, sources)]


def _source_types(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> dict[str, Any]:
    """Every wireable source name, and the CWL type it carries: a workflow input's as declared, a tool output's as its
    tool declares it."""
    types: dict[str, Any] = {port.name: port.declaration.type.canonical for port in workflow.inputs}
    for step in steps:
        for raw_port, port in step.tool.outputs.items():
            types[f"{step.node.name}/{raw_port}"] = canonical(port.type_)
    return types


def _referenced_names(tool: _Tool) -> set[str] | None:
    """The input names a plain or basename reference reads in a command template or an output glob.

    None when the tool's command or outputs cannot be lowered; lowering reports why.
    """
    if not isinstance(tool.process, standard.CommandLineTool):
        return None
    try:
        command = _command(tool)
        outputs = _ports(tool.outputs, outputs=True)
    except (ValueError, TypeError):
        return None
    return inputs_read(template_segments(command, outputs), NfInputReference, NfBasenameReference)


def _safe_absence_names(tool: _Tool) -> set[str] | None:
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
        all_names = {_identifier(raw_name) for raw_name in tool.inputs}
        # A shell literal, or a valueFrom other than a boolean's flag, renders whatever the value is.
        unconditional_names = {
            _identifier(raw_name)
            for raw_name, port in tool.inputs.items()
            if (binding := port.inputBinding) is not None
            and (binding.shellQuote is False
                 or binding.valueFrom is not None and required(canonical(port.type_))[0] != "boolean")
        }
    except ValueError:
        return None
    return all_names - referenced - unconditional_names


def _absent_optional_findings(workflow: ResolvedWorkflow, steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject optional inputs whose value is absent and unsafe."""
    try:
        params = _workflow_params(workflow)
    except _Unsupported:
        # Two inputs normalize alike, which the capability pass reports: there is no unambiguous parameter lookup
        # for the optional-input analysis.
        return []
    findings: list[_Finding] = []
    for step in steps:
        safe_names = _safe_absence_names(step.tool)
        if safe_names is None:
            continue
        bound = {port.name: (port, sources) for port, sources in step.bound()}
        for raw_name, declared in step.tool.inputs.items():
            optional = required(canonical(declared.type_))[1]
            if raw_name not in bound:
                # Unwired inputs are only a missingness problem when optional without a default; required unwired
                # inputs fail compilation.
                absent = optional and declared.default is None
            else:
                port, found = bound[raw_name]
                sources = _wired(port, found)
                if sources is None:
                    continue
                # A wired input is absent when any boundary source resolves to null, including an absent optional
                # workflow input feeding a required tool input. A pickValue or a default gives a value that is not
                # null.
                absent = not _gives_non_null(port) and any(
                    params.get(_identifier(source)) is None
                    for source in sources if "/" not in source
                )
            if not absent:
                continue
            if optional:
                # Every input's name normalizes where its absence is judged.
                if _qualifier(canonical(declared.type_)) == "val" and _identifier(raw_name) in safe_names:
                    continue
                findings.append(_Finding(
                    SophiosErrorCode.NF_ABSENT_OPTIONAL,
                    f".run.inputs.{raw_name}", "absent optional values are supported only for a val input "
                    "whose absence leaves command rendering unchanged", step.node))
            else:
                findings.append(_Finding(SophiosErrorCode.MISSING_REQUIRED_INPUT,
                                         f".run.inputs.{raw_name}", "resolves to an absent required value", step.node))
    return findings


def _conditional_consumer_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Reject every consumer of a conditional step's output the design does not admit.

    A conditional step's output may be null at runtime (design §6, Topology). That null is admitted only at a
    workflow output (which reports null) or an optional val input of a later step whose use the absent-optional
    lowering already admits (never referenced, or referenced only by a boolean-flag token or its own optional
    binding). Every other consumer is rejected by name, as is an array-typed output of a conditional step outside
    scatter, where the sentinel would collide with a genuinely empty array.
    """
    findings: list[_Finding] = []
    conditional: set[str] = set()
    for step in steps:
        if step.node.condition is None or not isinstance(step.tool.process, standard.CommandLineTool):
            continue
        conditional.add(step.node.name)
        for raw_name, produced in step.tool.outputs.items():
            if _is_array_type(required(canonical(produced.type_))[0]):
                findings.append(_Finding(SophiosErrorCode.NF_SKIPPED_OUTPUT, f".out.{raw_name}", "array-typed output "
                                         "of a conditional step is not supported outside scatter", step.node))
    if not conditional:
        return findings
    for step in steps:
        for port, sources in step.wired():
            consumes = any("/" in source and source.rsplit("/", maxsplit=1)[0] in conditional for source in sources)
            if not consumes or _gives_non_null(port):
                continue
            declared = step.tool.inputs.get(port.name)
            if declared is None:
                continue
            cwl_type = canonical(declared.type_)
            if _is_array_type(required(cwl_type)[0]):
                problem = ("an array-typed port cannot consume a conditional step's output; [] already stands for a "
                           "skipped step")
            elif _qualifier(cwl_type) == "path":
                problem = "a path consumer of a possibly-null conditional step output is not supported"
            elif not required(cwl_type)[1]:
                problem = "a non-optional port cannot consume a possibly-null conditional step output"
            elif (safe_names := _safe_absence_names(step.tool)) is None or _identifier(port.name) in safe_names:
                continue
            else:
                problem = ("this input's use of a possibly-null conditional step output is not one the "
                           "absent-optional lowering admits")
            findings.append(_Finding(SophiosErrorCode.NF_SKIPPED_OUTPUT, f".in.{port.name}", problem, step.node))
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
        for raw_port, produced in step.tool.outputs.items():
            try:
                capture = _output_capture(raw_port, produced)
            except ValueError:
                continue
            if capture == "text":
                endpoints.add(f"{step.node.name}/{raw_port}")
    return endpoints


def _text_capture_sink_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
    """Restrict a captured value's sink to a workflow output.

    Every process output before this lowering carried the ``path`` qualifier, so the executable graph has no
    source-to-destination qualifier agreement check for process edges to lean on.
    """
    endpoints = _text_capture_endpoints(steps)
    return [
        _Finding(SophiosErrorCode.NF_CAPTURED_TEXT_READ,
                 f".in.{port.name}", f"{source!r} captures file text; its only approved sink is a workflow "
                 "output, because the executable graph has no qualifier agreement check for process edges", step.node)
        for step in steps for port, sources in step.wired() for source in sources if source in endpoints
    ]


def _literal_outputs(tool: _Tool) -> dict[str, str]:
    """Each output whose glob is one literal name, with no wildcard, by port."""
    names: dict[str, str] = {}
    for raw_port, port in tool.outputs.items():
        glob = _literal_text(port.outputBinding.glob) if port.outputBinding is not None else None
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
    try:
        entries = _iwdr_entries(step.tool)
    except ValueError:  # its own finding; what it stages is unknown
        return {}
    wired = {port.name: sources for port, sources in step.wired()}
    staged: dict[str, tuple[set[str], bool]] = {}
    for raw_name, port in step.tool.inputs.items():
        try:
            if cwl_type_to_nf_qualifier(canonical(port.type_)) != "path":
                continue
            entry = entries.get(_identifier(raw_name))
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


def _staged_name_findings(steps: tuple[_Step, ...]) -> list[_Finding]:
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
    findings: list[_Finding] = []
    for step in steps:
        if not isinstance(step.tool.process, standard.CommandLineTool):
            continue
        path = ".run"
        # Each name once, where it is first written: a stdout's glob names it again.
        written: dict[str, tuple[str, str, bool]] = {}
        for where, name, collects in [
            *((f"{path}.{stream}", _literal_text(getattr(step.tool.process, stream)), False)
              for stream in ("stdout", "stderr")),
            *((f"{path}.outputs.{port}.outputBinding.glob", glob, True)
              for port, glob in _literal_outputs(step.tool).items()),
        ]:
            if name:
                written.setdefault(posixpath.normpath(name), (where, name, collects))
        staged = _staged_names(step, produced)
        findings.extend(
            _Finding(SophiosErrorCode.NF_WRITE_THROUGH_STAGED,
                     where, f"writing {name!r} writes through {link!r}, the name input {raw_name!r} is linked under "
                     "in the task directory, into the file the input names; give the output another name", step.node)
            for normal, (where, name, collects) in written.items()
            for raw_name, (links, entry) in staged.items()
            if not (collects and entry)
            for link in links
            if normal == link or normal.startswith(f"{link}/")
        )
    return findings


class _WhenError(_Unsupported):
    """A step's `when` the target cannot lower."""


def _condition_value(step: _Step) -> Expr:
    """A step's ``when`` as a boolean tree over the inputs the step binds, typed by what each one delivers (design
    §5, §9): a step-only input as any other, and through every call.

    A step's when is evaluated once per invocation, after inputs are bound.
    """
    condition = step.node.condition
    assert condition is not None
    when = condition.written
    if not isinstance(when, str):
        raise _WhenError(SophiosErrorCode.NF_INVALID_CWL,
                         f"{when!r} must be a safe JavaScript subset $( … ) expression")
    if (problem := boolean_problem(condition)) is not None:
        raise _WhenError(SophiosErrorCode.WHEN_NOT_BOOLEAN, problem)
    if not is_safe_subset_text(when):
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} must be a safe JavaScript subset $( … ) expression")
    if condition.expr is None:
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} is outside the safe JavaScript subset: {condition.problem}")
    if unbound := sorted(references(condition.expr) - {port.name for port in step.node.inputs}):
        names = ", ".join(f"inputs.{name}" for name in unbound)
        raise _WhenError(SophiosErrorCode.NF_WHEN_UNBOUND, f"{when!r} reads {names}, which the step does not bind")
    if condition.type is None:
        raise _WhenError(SophiosErrorCode.NF_WHEN_SUBSET,
                         f"{when!r} is outside the safe JavaScript subset: {condition.problem}")
    return condition.expr


#: The code of each reference rule (`nf_types.reference_problems`) a tool's command or globs can break: an array
#: read as one value; a reference to an input the tool does not declare, which is no expression the target lowers.
_REFERENCE_CODES: Final = {ARRAY_REFERENCE: SophiosErrorCode.NF_ARRAY_BINDING,
                           "unknown": SophiosErrorCode.NF_EXPRESSION}


def _process(step: _Step) -> tuple[NfProcess, Expr | None]:
    """A step's process, before the workflow types its inputs, and its condition, which needs them typed."""
    name = _identifier(step.node.name, variable=False)
    inputs = _ports(step.tool.inputs, outputs=False, entries=_iwdr_entries(step.tool))
    outputs = _ports(step.tool.outputs, outputs=True)
    command, container, resources = _command(step.tool), _container(step.tool), _resources(step.tool)
    condition = _condition_value(step) if step.node.condition is not None else None
    # A carried input is a val, staging nothing, and a list where what it delivers is one.
    carried = [NfPort(_identifier(port.name), "val", is_array=_lists(port.type))
               for port, _sources in step.bound() if port.declared is None]
    if broken := reference_problems(name, inputs, carried, outputs, command):
        rule, problem = broken[0]
        raise _Unsupported(_REFERENCE_CODES.get(rule, SophiosErrorCode.NF_INVALID_CWL), problem)
    if problems := staged_write_problems(name, inputs, outputs, command):
        raise _Unsupported(SophiosErrorCode.NF_WRITE_THROUGH_STAGED, problems[0])
    return NfProcess(name, inputs, outputs, command, container, resources, step_only_inputs=carried), condition


def _typed(processes: list[NfProcess], connections: list[NfConnection], param_types: Mapping[str, str],
           conditions: Mapping[str, Expr]) -> tuple[list[NfProcess], dict[str, _Unsupported]]:
    """Each process with its inputs' scalar types (design §9) and its condition, in its place; and why each process
    that is not, is not.

    A process is typed after the processes it reads, since what an output gives depends on its process's condition.
    """
    incoming = incoming_connections(connections)
    position = {process.name: index for index, process in enumerate(processes)}
    done: dict[str, NfProcess] = {}
    failed: dict[str, _Unsupported] = {}
    for name in topological_order(process_dependencies(position, connections), error="workflow connections contain "
                                  "a cycle", key=position.__getitem__):
        done[name] = typed = with_scalar_types(processes[position[name]], incoming, param_types, done)
        if (condition := conditions.get(name)) is None:
            continue
        if (problem := condition_without_inputs(name, typed.all_inputs)) is not None:
            failed[name] = _Unsupported(SophiosErrorCode.NF_WHEN_WITHOUT_INPUTS, problem)
            continue
        try:
            done[name] = replace(typed, condition=condition)
        except ValueError as error:  # the condition does not type as a boolean over its typed inputs
            failed[name] = _Unsupported(SophiosErrorCode.NF_WHEN_SUBSET, str(error))
    return [done[process.name] for process in processes], failed


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source)
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process)
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port)


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
        scattered = {_identifier(raw_name) for raw_name in step.scattered}
        # Capability analysis already admitted only an approved method for two or more; at one scattered input the
        # three methods coincide.
        method = step.node.scatter.method if step.node.scatter is not None else None
        scatter_method = str(method or "dotproduct") if len(scattered) > 1 else "dotproduct"
        for port, found in step.bound():
            destination_port = _identifier(port.name)
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
    """The connection into each workflow output from each of its sources. `_source_text_findings` has refused an
    output with no source and one whose source is inside a call; text that names no step's output stops here."""
    connections: list[NfConnection] = []
    for port, found in outputs:
        destination_port = _identifier(port.name)
        sources = [port.source] if isinstance(port.source, str) else [
            _spelled(source) for source in found if isinstance(source, Through)]
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
        made.extend((f"{step.node.name}~{port.name}", process.name, _identifier(port.name),
                     _json_value(port.default), _param_type(port.type))
                    for port in bound.values() if port.declared is None and port.has_default)
        for raw_name, declared in step.tool.inputs.items():
            port = bound.get(raw_name)
            if port is not None and port.has_default:
                value = _json_value(port.default)
            elif port is not None:
                continue
            elif declared.default is not None:
                value = _json_value(declared.default)
            elif required(canonical(declared.type_))[1]:
                # Capability analysis has already proven this unwired, no-default optional input is a safe absence.
                # _apply_absent_sentinel converts None to the runtime-proven [] representation once, at the end of
                # the conversion.
                value = None
            else:
                continue
            port_name = _identifier(raw_name)
            made.append((f"{step.node.name}~{raw_name}", process.name, port_name, value,
                         _param_type(canonical(declared.type_))))
    names = _normalized_identifiers([*workflow_inputs, *(name for name, *_ in made)], context="workflow parameter")
    return (
        [NfWorkflowInputConnection(names[name], process, port) for name, process, port, *_ in made],
        {names[name]: value for name, _process, _port, value, _type in made},
        {names[name]: param_type for name, *_, param_type in made},
    )


def _parameter_type(cwl_type: Any) -> str | None:
    """`_param_type`, or None for a type no parameter has."""
    try:
        return _param_type(cwl_type)
    except ValueError:
        return None


def _param_type(cwl_type: Any) -> str:
    """The declared type the generated pipeline checks a parameter's value against, as `PARAM_TYPES` spells it."""
    inner, optional = required(cwl_type)
    base = f"{required(_array_item_type(inner))[0]}[]" if _is_array_type(inner) else inner
    return f"{base}?" if optional else str(base)


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
    return {names[port.name]: _param_type(port.declaration.type.canonical) for port in workflow.inputs}


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
    #: What the target refuses of each tool the standard's loader refuses.
    refused: tuple[_Finding, ...]
    outputs: tuple[tuple[WorkflowOutput, tuple[Through | Crossing, ...]], ...]
    #: The (call, input) pairs a kept output relays that the call leaves unbound, with no default.
    unbound: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, view: ResolvedWorkflow) -> "_Lowering":
        """The lowering's reading of `view`."""
        if not isinstance(view, ResolvedWorkflow):
            raise TypeError("Nextflow conversion requires a resolved view of a compilation")
        steps: list[_Step] = []
        refused: list[_Finding] = []
        for found in tool_steps(view):
            if not isinstance(run := found.step.run, Tool):
                continue
            try:
                steps.append(_Step(found, _Tool.of(run, found.step.inherited), _carried(found)))
            except SophiosError as error:  # one refusal for a tool the standard's loader refuses, at its step
                refused.append(_Finding(SophiosErrorCode.NF_INVALID_CWL, ".run",
                                        next(iter(error.diagnostics)).message, found.step))
        return cls(view, tuple(steps), tuple(refused), tuple(zip(view.outputs, output_sources(view), strict=True)),
                   tuple(dict.fromkeys(pair for pairs in output_unbound(view) for pair in pairs)))


_PATH_FIELD = re.compile(r"\.(?P<field>[^.\[]+)(?P<rest>.*)")


def _parts(path: str) -> tuple[str, str]:
    """The first field of a path and the rest of it; nothing for a path that names no field."""
    found = _PATH_FIELD.fullmatch(path)
    return (found["field"], found["rest"]) if found else ("", "")


def _with_field(rest: str, reason: str) -> str:
    """`reason`, after the field the rest of a path names, unless the reason names it first."""
    field = rest.removeprefix(".")
    return reason if not field or reason.startswith(field.rsplit(".", maxsplit=1)[-1]) else f"{field}: {reason}"


def _tool_file(step: ResolvedStep) -> str:
    """The file a step's tool was read from, as a refusal names it."""
    return Path(step.run.run_path).name if isinstance(step.run, Tool) else step.name


def _step_named(workflow: ResolvedWorkflow, name: str) -> ResolvedStep | None:
    """The step of `workflow`, at any depth, whose qualified name is `name`."""
    for step in workflow.steps:
        if step.name == name:
            return step
        if isinstance(step.run, ResolvedWorkflow) and (inner := _step_named(step.run, name)) is not None:
            return inner
    return None


def _workflow_refusal(workflow: ResolvedWorkflow, finding: _Finding, *, root: str, step: str | None) -> Refusal:
    """The refusal of a construct of `workflow`, the root or the workflow the step named `step` calls, at the
    finding's path."""
    field, after = _parts(finding.path)
    name, within = _parts(after)
    construct, subject, span, text = Construct.WORKFLOW, None, workflow.span, _with_field(finding.path, finding.text)
    if field in ("inputs", "outputs"):
        construct = Construct.WORKFLOW_INPUT if field == "inputs" else Construct.WORKFLOW_OUTPUT
        subject, text = name or None, _with_field(within, finding.text)
        port = next((port for port in (workflow.inputs if field == "inputs" else workflow.outputs)
                     if port.name == name), None)
        made = port.made_from if isinstance(port, WorkflowInput) else None
        if made is not None and (maker := _step_named(workflow, made[0])) is not None:
            # A made input has no line of its own: its value was written on the step input it was made from.
            step = maker.name
            span = next((each.span for each in maker.inputs if each.name == made[1]), None) or maker.span
        elif port is not None:
            span = port.span or workflow.span
    elif field in ("requirements", "hints"):
        construct, subject, text = Construct.REQUIREMENT, name or None, _with_field(within, finding.text)
    elif field == "steps":
        construct, text = Construct.STEP, finding.text
    return Refusal(finding.code, construct, subject, root, step, span, text)


def _step_refusal(node: ResolvedStep, finding: _Finding, *, root: str) -> Refusal:
    """The refusal of `node` or a construct of it, at the finding's path."""
    field, after = _parts(finding.path)
    if isinstance(node.run, ResolvedWorkflow) and field == "run":
        return _workflow_refusal(node.run, replace(finding, path=after), root=root, step=node.name)
    name, within = _parts(after)
    construct = Construct.CALL if isinstance(node.run, ResolvedWorkflow) else Construct.STEP
    subject, span, text = None, node.span, _with_field(finding.path, finding.text)
    match field:
        case "when":
            construct, text = Construct.WHEN, _with_field(after, finding.text)
        case "scatter" | "scatterMethod":
            construct, text = Construct.SCATTER, finding.text
        case "in":
            construct, subject, text = Construct.STEP_INPUT, name or None, _with_field(within, finding.text)
            span = next((port.span for port in node.inputs if port.name == name), None) or node.span
        case "run":
            field_path = after.removeprefix(".")
            construct, text = Construct.TOOL, finding.text
            subject = f"{_tool_file(node)}: {field_path}" if field_path else _tool_file(node)
        case "requirements" | "hints":
            construct, subject, text = Construct.REQUIREMENT, name or None, _with_field(within, finding.text)
    return Refusal(finding.code, construct, subject, root, node.name, span, text)


def _unbound_output_findings(lowering: _Lowering) -> list[_Finding]:
    """A finding for each input a call leaves unbound, with no default, that a kept workflow output relays.

    Such an output has no source to follow, so the input is refused once, at the call, however many outputs relay it
    and through whatever chain, not dropped.
    """
    reason = "a workflow output relays it, but nothing binds it and it has no default"
    return [_Finding(SophiosErrorCode.NF_FORWARDED_INPUT, f".in.{name}", reason, _step_named(lowering.view, call))
            for call, name in lowering.unbound]


def _refusal(finding: _Finding, view: ResolvedWorkflow) -> Refusal:
    """A finding as the refusal of the construct at its path: it names the step and the construct by the names their
    author wrote, at the author's line."""
    if finding.step is not None:
        return _step_refusal(finding.step, finding, root=view.name)
    return _workflow_refusal(view, finding, root=view.name, step=None)


def _at_step(finding: _Finding, step: _Step) -> _Finding:
    """A finding of a step's tool, at the step; one about an entry the tool inherits, at the step's requirement of
    its class: the author wrote it outside the tool's file (design §8)."""
    for place, cls in step.tool.inherited.items():
        written = f".run.{place}"
        if finding.path == written or finding.path.startswith(f"{written}."):
            section = place.split(".", maxsplit=1)[0]
            return replace(finding, path=f".{section}.{cls}{finding.path.removeprefix(written)}", step=step.node)
    return replace(finding, step=step.node)


def _not_lowered(findings: Iterable[_Finding], view: ResolvedWorkflow) -> NotLowered:
    """What the target refuses of the view: the refusal of each finding."""
    return NotLowered(tuple(_refusal(finding, view) for finding in findings))


def _analysis(lowering: _Lowering) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
    """The workflow each tool step of the view lowers into, and the notes made on the way.

    Under partial failure the policy is the one refusal: the tools the view carries are written for it (design §2),
    so what the target would refuse in them, such as their `successCodes`, is the policy's, not their author's.

    Raises:
        NotLowered: Every construct of the view the target cannot lower.
    """
    view, steps = lowering.view, lowering.steps
    if view.policy.partial_failure:
        raise NotLowered((Refusal(SophiosErrorCode.NF_PARTIAL_FAILURE, Construct.POLICY, "partial failure",
                                  view.name, None, view.span, "partial failure is a policy of the CWL target"),))
    findings = _call_findings(view)
    notes: list[Diagnostic] = []
    processes: list[NfProcess] = []
    conditions: dict[str, Expr] = {}

    for step in steps:
        if step_findings := [_at_step(finding, step)
                             for finding in _tool_capability_findings(step.tool)]:
            findings.extend(step_findings)
            continue
        try:
            process, condition = _process(step)
            processes.append(process)
            if condition is not None:
                conditions[process.name] = condition
        except _WhenError as error:
            findings.append(_Finding(error.code, ".when", str(error), step.node))
        except _Unsupported as error:  # what lowering the tool finds, which no source pass reads
            findings.append(_Finding(error.code, "", str(error), step.node))
        notes.extend(_cuda_notes(step.node.name, step.tool))
    findings.extend(lowering.refused)
    findings.extend(_workflow_capability_findings(view, steps))
    findings.extend(_unbound_output_findings(lowering))
    findings.extend(_absent_optional_findings(view, steps))
    findings.extend(_conditional_consumer_findings(steps))
    findings.extend(_scatter_findings(view, steps))
    findings.extend(_step_input_findings(view, steps))
    findings.extend(_text_capture_sink_findings(steps))
    findings.extend(_staged_name_findings(steps))
    findings.extend(_source_text_findings(lowering))
    if findings:
        raise _not_lowered(findings, view)
    return _assembled(lowering, processes, conditions), tuple(notes)


def _assembled(lowering: _Lowering, processes: list[NfProcess],
               conditions: Mapping[str, Expr]) -> ExecutableNextflowWorkflow:
    """The workflow the processes make, once every source pass has admitted the view.

    Raises:
        NotLowered: Each error the lowering or the model would raise, of the step it is about where there is one.
    """
    view = lowering.view
    params = _workflow_params(view)
    try:
        default_connections, default_params, default_types = _default_bindings(
            lowering.steps, processes, [port.name for port in view.inputs])
    except _Unsupported as error:  # a parameter made for a default shares a name with another
        raise _not_lowered([_Finding(error.code, "", str(error))], view) from None
    params.update(default_params)
    param_types = {**_workflow_param_types(view), **default_types}
    connections = _connections(lowering, processes, default_connections)
    processes, failed = _typed(processes, connections, param_types, conditions)
    if failed:
        raise _not_lowered((
            _Finding(failed[process.name].code, ".when" if process.name in conditions else "",
                     str(failed[process.name]), step.node)
            for step, process in zip(lowering.steps, processes, strict=True) if process.name in failed), view)
    name = _identifier(view.name, variable=False)
    while name in {process.name for process in processes}:  # a step may share the workflow's name
        name = f"_{name}"
    # A default is as authored; each value is held as a value written on a step is (design §4.2, Losses): coerced
    # to its declared type, so an int given for a float is the float, and a local location a path.
    values = _apply_absent_sentinel({key: local_locations_as_paths(coerce_job_value(
        AuthoredName(key), PortDeclaration(PortType(param_types[key])), value)) for key, value in params.items()})
    problems = {SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP: endpoint_problems(processes, connections),
                SophiosErrorCode.NF_INPUT_VALUE: [each for each in [json_value_problem(values)] if each is not None],
                SophiosErrorCode.NF_ABSENT_OPTIONAL: absent_path_problems(values, param_types),
                SophiosErrorCode.NF_OPTIONAL_OUTPUT: optional_output_problems(processes, connections)}
    if refused := [_Finding(code, "", problem) for code, found in problems.items() for problem in found]:
        raise _not_lowered(refused, view)
    return ExecutableNextflowWorkflow(name=name, processes=processes, connections=connections, params=values,
                                      param_types=param_types)


def _names_a_step(workflow: ResolvedWorkflow, source: str) -> bool:
    """Whether `source`, `step/port` text, names a step of `workflow` by its id there."""
    step, _, _port = source.rpartition("/")
    return any(each.local == step for each in workflow.steps)


def _source_text_findings(lowering: "_Lowering") -> list[_Finding]:
    """Each source written as `step/port` text that names no tool step the lowering makes a process of, and each
    workflow output the target cannot connect: one with no source, or one that forwards a workflow input.

    Sophios keeps text it could not resolve as it was written (resolved-view design §3). Text naming a step of its
    workflow, a nested workflow's call, is valid CWL the target does not follow; text naming no step is the
    document's mistake.
    """
    view = lowering.view
    processes = {step.node.name for step in lowering.steps}
    findings: list[_Finding] = []

    def text(source: str, workflow: ResolvedWorkflow) -> None:
        if "/" in source and (process := source.rpartition("/")[0]) not in processes:
            code = (SophiosErrorCode.NF_SOURCE_AS_TEXT if _names_a_step(workflow, source)
                    else SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP)
            findings.append(_Finding(code, "", f"connection references unknown source process {process!r}"))

    for step in lowering.steps:
        level = step.view.calls[-1].run if step.view.calls else view
        assert isinstance(level, ResolvedWorkflow)
        for port, _ in step.bound():
            if port.expression is not None:
                text(port.expression, level)
    for output, found in lowering.outputs:
        if output.source is None:
            findings.append(_Finding(SophiosErrorCode.NF_OUTPUT_WITHOUT_SOURCE,
                                     "", f"workflow output {output.name!r} must define outputSource"))
        elif isinstance(output.source, str):
            text(output.source, view)
        elif crossings := [each for each in found if isinstance(each, Crossing)]:
            call = _step_named(view, crossings[0].call)
            child = call.run if call is not None and isinstance(call.run, ResolvedWorkflow) else None
            inner = next((each.source for each in child.outputs if each.name == crossings[0].port), None) \
                if child is not None else None
            code = (SophiosErrorCode.NF_SOURCE_AS_TEXT if isinstance(inner, str) and child is not None
                    and _names_a_step(child, inner) else SophiosErrorCode.NF_SOURCE_NAMES_NO_STEP)
            findings.append(_Finding(code, "", f"workflow output {output.name!r} names no step output the "
                                     "target can follow"))
        elif any(isinstance(each, Through) and isinstance(each.ref, InputRef) for each in found):
            findings.append(_Finding(SophiosErrorCode.NF_FORWARDED_INPUT,
                                     "", f"workflow output {output.name!r} directly forwards a workflow input; "
                                     "boundary passthrough is not executable on the Nextflow target"))
    return findings


class NextflowTarget:  # pylint: disable=too-few-public-methods
    """The Nextflow target: it lowers a resolved view to the executable Nextflow model (design §7)."""

    def lower(self, view: ResolvedWorkflow) -> tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]:
        """Lower `view` without repeating core semantics.

        Every unsupported source semantic, including one that only lowering or model validation detects, is
        refused at its author's line, with its code, and aggregated with the rest before the workflow is assembled.

        Args:
            view (ResolvedWorkflow): The resolved view of a finished compilation (`compiler.resolved_view`).

        Raises:
            TypeError: If the argument is not a resolved view.
            NotLowered: If there is any refusal; it holds every one.

        Returns:
            tuple[ExecutableNextflowWorkflow, tuple[Diagnostic, ...]]: The validated executable representation, and
                the notes the lowering made, such as a CUDA field it does not check.
        """
        return _analysis(_Lowering.of(view))


#: The Nextflow target.
NEXTFLOW: Final = NextflowTarget()


def nextflow_names(view: ResolvedWorkflow) -> dict[str, dict[str, str]]:
    """The Sophios name of each process and workflow output the lowering of `view` makes, by its Nextflow name.

    A process is named for its step's qualified id and a workflow output for its own name, normalized as the
    lowering normalizes them.
    """
    return {
        "processes": {_identifier(tool.step.name, variable=False): tool.step.name
                      for tool in tool_steps(view) if isinstance(tool.step.run, Tool)},
        "outputs": {_identifier(port.name): port.name for port in view.outputs},
    }
