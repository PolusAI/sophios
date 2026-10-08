"""Lower a resolved Sophios compilation into the Nextflow representation."""

from collections.abc import Callable, Iterable, Mapping
import copy
from dataclasses import dataclass, replace
import math
from os import PathLike
import re
from types import MappingProxyType
from typing import Any

from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.names import Names
from .ir.types import Direction, Edge, PortId, StepNode, WorkflowGraph
from .nf_expr import (
    Expr, check as check_safe_subset, is_safe_subset_text, parse as parse_safe_subset, references,
)
from .nf_symbols import normalize_nextflow_identifier
from .nf_types import (
    ExecutableNextflowWorkflow,
    GLOB_WILDCARDS,
    NF_INTERNAL_IDENTIFIERS,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfComputed,
    NfConnection,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfShellLiteral,
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


def _identifier(value: Any, *, context: str) -> str:
    """Normalize a CWL identifier into a stable Nextflow identifier."""
    match value:
        case str() if value:
            pass
        case _:
            raise ValueError(f"{context} must be a non-empty string")
    local = value.rsplit("#", maxsplit=1)[-1]
    if not local:
        raise ValueError(f"{context} cannot be normalized to a Nextflow identifier")
    identifier = normalize_nextflow_identifier(local)
    while identifier in NF_INTERNAL_IDENTIFIERS:
        identifier = f"_{identifier}"
    return identifier


def _normalized_identifiers(values: Iterable[Any], *, context: str) -> dict[Any, str]:
    """Normalize source identifiers and reject every many-to-one mapping."""
    normalized_by_source: dict[Any, str] = {}
    sources_by_normalized: dict[str, list[Any]] = {}
    for source in values:
        normalized = _identifier(source, context=f"{context} identifier")
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
) -> list[str]:
    """Return a capability finding when source names collapse during normalization."""
    try:
        _normalized_identifiers(values, context=context)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    return []


def _as_mapping(value: Any, *, error: str) -> Mapping[str, Any]:
    match value:
        case Mapping() as mapping:
            return mapping
        case _:
            raise ValueError(error)


def _as_list(value: Any, *, error: str) -> list[Any]:
    match value:
        case list() as items:
            return items
        case _:
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
    are explicitly deferred rather than silently mishandled.

    CWL spells a per-item binding two ways -- inside ``items``, and on the
    array schema itself beside ``items`` -- and only the second is the
    common form. Both are rejected here; accepting the array-level one
    would silently drop the per-item prefix cwltool renders.
    """
    if isinstance(array_type, Mapping) and "inputBinding" in array_type:
        raise ValueError("per-item array element bindings are deferred beyond this lowering")
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
    stage_as: Mapping[str, str] | None = None,
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
                        stage_as=None if outputs or stage_as is None else stage_as.get(name),
                        capture=_output_capture(raw_name, raw_definition) if outputs else None,
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
    if "$" in (_INTERPOLATION.sub("", text) if interpolated else text):
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
            prefix_template = _template(text, context="CWL command prefix")
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
    rejected here rather than silently losing its quoting.
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
    return NfShellLiteral("".join(literal_parts))


def _computed_value(value_from: Any, *, tool: Mapping[str, Any], field: str, integral: bool) -> NfComputed | None:
    """Lower a safe-subset valueFrom, or return None when it is a projection or literal.

    Projections keep the template lowering; any other whole-field
    ``$( … )`` text must be a number-typed safe-subset expression.
    """
    if not is_safe_subset_text(value_from) or _INPUT_EXPRESSION.fullmatch(value_from.strip()):
        return None
    inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    input_types = {
        str(raw_name): definition.get("type") if isinstance(definition, Mapping) else definition
        for raw_name, definition in inputs.items()
    }
    try:
        expression = parse_safe_subset(value_from)
        result = check_safe_subset(expression, input_types)
    except ValueError as exc:
        raise ValueError(f"{field} {value_from!r} is outside the safe JavaScript subset: {exc}") from exc
    if result != "number":
        raise ValueError(f"{field} {value_from!r} must compute a number, not a {result}")
    authored = " ".join(value_from.split())
    return NfComputed(expression, f"{tool.get('id', 'tool')} {field} {authored}", integral)


def _computed_tokens(prefix: Any, value: NfComputed, *, separate: Any = True) -> tuple[NfCommandToken, ...]:
    match prefix:
        case None:
            return (value,)
        case str() as text if separate is not False:
            return _template(text, context="CWL command prefix"), value
        case str():
            raise ValueError("separate: false beside a computed valueFrom is not supported")
        case _:
            raise ValueError("CWL command prefix must be a string")


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
                elif computed := _computed_value(
                    value_from, tool=tool, field=f"arguments[{index}].valueFrom", integral=False
                ):
                    tokens = _computed_tokens(
                        argument.get("prefix"), computed, separate=argument.get("separate", True)
                    )
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


_IWDR_DIRENT_FIELDS = frozenset({"class", "entry", "entryname", "writable"})
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
    """Classify an IWDR entryname: None for a same-basename no-op, else a literal rename.

    Absent, or the exact self-referencing $(inputs.<name>.basename), both
    mean "stage under the input's own basename" -- already Nextflow's
    default, so neither needs a stage_as override. A plain literal with no
    reference is the one supported rename shape; any other reference has no
    runtime-proven representation.
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
                "entryname is deferred"
            )
        literal_parts.append(segment.value)
    literal = "".join(literal_parts)
    if not literal.strip():
        raise ValueError("entryname must be a non-empty string")
    if "/" in literal:
        raise ValueError("entryname must not contain a path separator")
    if "*" in literal or "?" in literal:
        raise ValueError("entryname must not contain a Nextflow stageAs wildcard ('*' or '?')")
    return literal


def _iwdr_listing_item(item: Any, *, tool_inputs: Mapping[str, Any]) -> tuple[str, str | None]:
    """Validate one InitialWorkDirRequirement listing entry.

    Returns (normalized_input_name, literal_rename_or_None). Approved
    shapes: a bare $(inputs.<name>) string, or a Dirent {entry:
    $(inputs.<name>), entryname: ..., writable: false} whose entryname is
    absent, the input's own basename self-reference, or a literal with no
    path separator.
    """
    match item:
        case str() as bare:
            raw_name = _iwdr_entry_reference(bare)
            rename = None
        case Mapping() as dirent:
            extra = set(dirent) - _IWDR_DIRENT_FIELDS
            if extra:
                raise ValueError(f"has unsupported Dirent fields: {', '.join(sorted(extra))}")
            if dirent.get("writable") is True:
                raise ValueError("writable: true is deferred; copy-and-mutate staging is not supported")
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
            f"references array-typed input {raw_name!r}; staging an array of files is deferred"
        )
    try:
        qualifier = cwl_type_to_nf_qualifier(required)
    except ValueError:
        qualifier = "unsupported"
    if qualifier != "path":
        raise ValueError(
            f"references non-path input {raw_name!r}; only File/Directory inputs can be staged"
        )
    return _identifier(raw_name, context="IWDR listing target"), rename


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
        try:
            name, rename = _iwdr_listing_item(item, tool_inputs=tool_inputs)
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
        if rename is not None:
            renamed_by.setdefault(rename, set()).add(name)
    for rename, names in renamed_by.items():
        if len(names) > 1:
            findings.append(
                f"{path}.listing: inputs {sorted(names)} are all staged under the same "
                f"literal name {rename!r}"
            )
    return findings


def _iwdr_stage_as(tool: Mapping[str, Any]) -> dict[str, str]:
    """Return {input_name: literal_rename} for every approved IWDR rename entry.

    Only rename entries are represented: a self-basename entry needs no
    stage_as override, since Nextflow already stages a path input under its
    own basename by default.
    """
    requirement = _requirement(tool, "InitialWorkDirRequirement")
    if requirement is None:
        return {}
    listing = requirement.get("listing")
    if not isinstance(listing, list):
        raise ValueError("InitialWorkDirRequirement listing must be a list")
    tool_inputs = _as_mapping(tool.get("inputs", {}), error="CommandLineTool inputs must be a mapping")
    stage_as: dict[str, str] = {}
    by_rename: dict[str, set[str]] = {}
    listed_names: set[str] = set()
    for item in listing:
        name, rename = _iwdr_listing_item(item, tool_inputs=tool_inputs)
        if name in listed_names:
            raise ValueError(f"input {name!r} appears more than once in the IWDR listing")
        listed_names.add(name)
        if rename is not None:
            stage_as[name] = rename
            by_rename.setdefault(rename, set()).add(name)
    for rename, names in by_rename.items():
        if len(names) > 1:
            raise ValueError(
                f"inputs {sorted(names)} are all staged under the same literal name {rename!r}"
            )
    return stage_as


def _iwdr_rename_reference_findings(
    tool: Mapping[str, Any],
    renamed_names: set[str],
    *,
    path: str,
) -> list[str]:
    """Reject any other reference to an input IWDR stages under a different name.

    A renamed port's own .name reports the staged name, not the original
    CWL basename (runtime-proven), so resolving what a plain or basename
    reference to it would mean is out of scope; the supported pattern is
    for the command to hard-code the literal staged name directly.
    """
    if not renamed_names:
        return []
    try:
        command = _command(tool)
        outputs = _ports(tool.get("outputs", {}), outputs=True)
    except (ValueError, TypeError):
        return []
    templates = [
        *(token for token in command.tokens if isinstance(token, NfTemplate)),
        *(stream for stream in (command.stdin, command.stdout, command.stderr) if stream),
        *(port.glob for port in outputs if port.glob),
    ]
    plain, basenamed = _template_reference_names(templates)
    referenced = (plain | basenamed) & renamed_names
    if not referenced:
        return []
    return [
        f"{path}.run.requirements.InitialWorkDirRequirement: input {name!r} is staged under "
        "an explicit rename and cannot also be referenced elsewhere in the command, stream "
        "targets, or output globs"
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
        computed = _computed_value(
            value_from,
            tool=tool,
            field=f"inputs.{raw_name}.inputBinding.valueFrom",
            integral=required_type in {"int", "long"},
        )
        if computed is not None:
            if _is_optional(input_definition.get("type")):
                # CWL omits the whole binding for an absent input; a computed
                # token always renders, so the two would disagree.
                raise ValueError(
                    f"a computed valueFrom on optional input {raw_name!r} is not supported"
                )
            items.append((
                (position, 1, str(raw_name)),
                _computed_tokens(binding.get("prefix"), computed, separate=binding.get("separate", True)),
            ))
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
            tokens.append(_template(command, context="CWL baseCommand"))
        case list() as command:
            for token in command:
                match token:
                    case str():
                        tokens.append(_template(token, context="CWL baseCommand"))
                    case _:
                        raise ValueError("CommandLineTool baseCommand must be a string or list of strings")
        case None:
            pass
        case _:
            raise ValueError("CommandLineTool baseCommand must be a string or list of strings")

    tokens.extend(_command_items(tool))
    if not any(isinstance(token, (NfTemplate, NfShellLiteral)) for token in tokens):
        # A command made only of conditional flags has nothing to execute.
        tokens.insert(0, _template("true", context="empty CWL command"))

    def stream(name: str) -> NfTemplate | None:
        return None if tool.get(name) is None else _template(tool[name], context=f"CWL {name}")

    return NfCommand(tuple(tokens), stream("stdin"), stream("stdout"), stream("stderr"))


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


def _resources(tool: Mapping[str, Any]) -> NfResources:
    resource = _requirement(tool, "ResourceRequirement")
    if resource is None:
        return NfResources()
    cpus = _resource_value(resource, "coresMin", "coresMax")
    memory = _resource_value(resource, "ramMin", "ramMax")
    rendered_cpus = None if cpus is None else _cpu_resource(cpus)
    rendered_memory = None if memory is None else _resource_number(memory, name="memory")
    return NfResources(rendered_cpus, rendered_memory)


def _resource_requirement_findings(
    requirement: Mapping[str, Any],
    *,
    path: str,
) -> list[str]:
    """Validate every resource value the lowering consumes."""
    findings: list[str] = []
    for field_name in ("coresMin", "coresMax"):
        if field_name not in requirement:
            continue
        try:
            _cpu_resource(requirement[field_name])
        except ValueError as exc:
            findings.append(f"{path}.{field_name}: {exc}")
    for field_name in ("ramMin", "ramMax"):
        if field_name not in requirement:
            continue
        try:
            _resource_number(requirement[field_name], name="memory")
        except ValueError as exc:
            findings.append(f"{path}.{field_name}: {exc}")
    return findings


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
_NUMERIC_TEXT_CAPTURE_TYPES = frozenset({"int", "float", "double", "long"})
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
_LOAD_CONTENTS_INPUT_REASON = (
    "loadContents on an input is a separate lowering from the output capture form and "
    "is not implemented: an input's contents would have to become a value channel read "
    "at staging time"
)


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


_SUPPORTED_REQUIREMENTS = frozenset({
    "DockerRequirement",
    "InitialWorkDirRequirement",
    "InlineJavascriptRequirement",
    "ResourceRequirement",
    "ShellCommandRequirement",
})

_INERT_DOCUMENTATION_FIELDS = frozenset({"doc", "label"})
_TOOL_CONSUMED_FIELDS = frozenset({
    "$namespaces",
    "$schemas",
    "arguments",
    "baseCommand",
    "class",
    "cwlVersion",
    "hints",
    "id",
    "inputs",
    "outputs",
    "requirements",
    "stderr",
    "stdin",
    "stdout",
}) | _INERT_DOCUMENTATION_FIELDS
_INPUT_CONSUMED_FIELDS = (
    frozenset({"default", "inputBinding", "loadContents", "type"})
    | _INERT_DOCUMENTATION_FIELDS
)
_INPUT_BINDING_CONSUMED_FIELDS = frozenset({
    "loadContents",
    "position",
    "prefix",
    "separate",
    "shellQuote",
    "valueFrom",
})
_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputBinding", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
# loadContents and outputEval are analyzed by _output_capture rather than
# reported by the generic unconsumed-field pass, so they are consumed here.
_OUTPUT_BINDING_CONSUMED_FIELDS = frozenset({"glob", "loadContents", "outputEval"})
# The outputBinding fields that declare capture behavior, as opposed to naming
# the file to capture.
_OUTPUT_CAPTURE_FIELDS = frozenset({"loadContents", "outputEval"})
_ARGUMENT_CONSUMED_FIELDS = frozenset({
    "position",
    "prefix",
    "separate",
    "shellQuote",
    "valueFrom",
})
_SUPPORTED_REQUIREMENT_FIELDS = {
    "DockerRequirement": frozenset({"class", "dockerImageId", "dockerPull"}),
    "InitialWorkDirRequirement": frozenset({"class", "listing"}),
    "InlineJavascriptRequirement": frozenset({"class"}),
    "ResourceRequirement": frozenset({
        "class",
        "coresMax",
        "coresMin",
        "ramMax",
        "ramMin",
    }),
    "ShellCommandRequirement": frozenset({"class"}),
}
_WORKFLOW_CONSUMED_FIELDS = frozenset({
    "$namespaces",
    "$schemas",
    "class",
    "cwlVersion",
    "id",
    "inputs",
    "outputs",
    "requirements",
    "sophios:lang_version",
    "steps",
}) | _INERT_DOCUMENTATION_FIELDS
# Workflow-level requirements that only declare a feature whose lowering is
# decided per step, so they are consumed as inert no-ops. Sophios adds
# InlineJavascriptRequirement for every `when`; declaring it is not the use
# of JavaScript, and each expression is judged where it appears.
_SUPPORTED_WORKFLOW_REQUIREMENTS = frozenset({
    "InlineJavascriptRequirement",
    "ScatterFeatureRequirement",
    "SubworkflowFeatureRequirement",
})
_WORKFLOW_INPUT_CONSUMED_FIELDS = (
    frozenset({"default", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_NESTED_WORKFLOW_INPUT_CONSUMED_FIELDS = (
    frozenset({"type"}) | _INERT_DOCUMENTATION_FIELDS
)
_WORKFLOW_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputSource", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_STEP_CORE_FIELDS = frozenset({
    "id",
    "in",
    "out",
    "run",
    "scatter",
    "scatterMethod",
    "when",
})
_STEP_CONSUMED_FIELDS = _STEP_CORE_FIELDS | _INERT_DOCUMENTATION_FIELDS
_STEP_INPUT_CONSUMED_FIELDS = frozenset({"source"})


def _default_value_findings(definition: Mapping[str, Any], *, path: str) -> list[str]:
    """Validate a declared scalar default against the supported value subset."""
    if "default" not in definition:
        return []
    default = definition["default"]
    if default is None or isinstance(default, (Mapping, list)):
        return [f"{path}.default: the Nextflow target supports JSON scalar defaults only"]
    if not _phase1_value_matches(definition.get("type"), default):
        return [f"{path}.default: value does not match its supported CWL type"]
    return []


def _unconsumed_field_findings(
    value: Mapping[str, Any],
    *,
    consumed: frozenset[str],
    path: str,
) -> list[str]:
    """Reject source fields that the lowering does not consume."""
    return [
        f"{path}.{field_name}: {field_name} is not consumed by the Nextflow lowering"
        for field_name in sorted(set(value) - consumed)
    ]


def _local_locations_as_paths(value: Any) -> Any:
    """Rewrite a File or Directory object's plain local ``location`` as its ``path``.

    The Python API writes file values in CWL's ``location`` form; for a local
    path with no URI scheme the two keys mean the same file. A URI keeps its
    ``location`` and stays rejected, since the model does not represent schemes.
    """
    match value:
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


def _requirement_definition(
    section: Any,
    *,
    class_name: str,
    suffix: str,
) -> Mapping[str, Any] | None:
    """Return a requirement payload for closed-world field analysis."""
    match section:
        case Mapping() as requirements:
            definition = requirements.get(class_name)
        case list() as requirements:
            definition = requirements[int(suffix)]
        case _:
            return None
    return definition if isinstance(definition, Mapping) else None


_CLASSLESS_REQUIREMENT = "a requirement or hint needs a string `class`"


def _requirement_names(section: Any) -> list[tuple[str, str]]:
    """Return requirement names and stable path suffixes; a list item without a class gets ``""``."""
    match section:
        case None:
            return []
        case Mapping() as requirements:
            return [(str(name), str(name)) for name in requirements]
        case list() as requirements:
            names: list[tuple[str, str]] = []
            for index, requirement in enumerate(requirements):
                match requirement:
                    case Mapping() if isinstance(requirement.get("class"), str):
                        names.append((requirement["class"], str(index)))
                    case _:
                        names.append(("", str(index)))
            return names
        case _:
            return []


def _template_positions(
    tool: Mapping[str, Any],
    *,
    path: str,
) -> list[tuple[Any, str]]:
    """Pair every templated tool value with the CWL path it was written at."""
    positions: list[tuple[Any, str]] = []
    match tool.get("baseCommand"):
        case str() as command:
            positions.append((command, f"{path}.run.baseCommand"))
        case list() as commands:
            positions.extend(
                (token, f"{path}.run.baseCommand[{index}]")
                for index, token in enumerate(commands)
            )
        case _:
            pass
    match tool.get("arguments"):
        case list() as arguments:
            for index, argument in enumerate(arguments):
                argument_path = f"{path}.run.arguments[{index}]"
                match argument:
                    case Mapping() as mapping if mapping.get("valueFrom") is not None:
                        positions.append((mapping["valueFrom"], f"{argument_path}.valueFrom"))
                    case Mapping():
                        pass
                    case _:
                        positions.append((argument, argument_path))
        case _:
            pass
    match tool.get("inputs"):
        case Mapping() as inputs:
            for raw_name, definition in inputs.items():
                if not isinstance(definition, Mapping):
                    continue
                binding = definition.get("inputBinding")
                if not isinstance(binding, Mapping):
                    continue
                binding_path = f"{path}.run.inputs.{raw_name}.inputBinding"
                for field_name in ("prefix", "valueFrom"):
                    if binding.get(field_name) is not None:
                        positions.append((binding[field_name], f"{binding_path}.{field_name}"))
        case _:
            pass
    for stream in ("stdin", "stdout", "stderr"):
        if tool.get(stream) is not None:
            positions.append((tool[stream], f"{path}.run.{stream}"))
    match tool.get("outputs"):
        case Mapping() as outputs:
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
        case _:
            pass
    return positions


def _path_suffix_findings(tool: Mapping[str, Any], *, path: str) -> list[str]:
    """Require a File or Directory input for every ``.path`` and ``.basename`` reference (design §6).

    Read from the authored text, because ``_template`` lowers ``.path`` to a
    plain reference and the model could no longer tell the two apart.
    """
    inputs = tool.get("inputs", {})
    if not isinstance(inputs, Mapping):
        return []
    findings: list[str] = []
    for value, position in _template_positions(tool, path=path):
        references = (match.group("name", "suffix") for match in _INTERPOLATION.finditer(str(value)))
        for name, suffix in dict.fromkeys(reference for reference in references if reference[0]):
            definition = inputs.get(name)
            if suffix is None or not isinstance(definition, Mapping):
                continue
            try:
                if cwl_type_to_nf_qualifier(definition.get("type")) == "path":
                    continue
            except ValueError:
                continue  # the input pass reports a type the backend does not lower
            findings.append(
                f"{position}: $(inputs.{name}.{suffix}) requires a File or Directory input, "
                f"and {name} is declared {definition.get('type')!r}"
            )
    return findings


def _emitted_steps(artifact: CompilationArtifact) -> list[Mapping[str, Any]]:
    """The steps the artifact's emitted workflow document lists, in graph order."""
    raw_steps = _as_list(
        artifact.cwl.get("steps", []),
        error="compiled CWL Workflow steps must be a list",
    )
    return [
        _as_mapping(step, error="compiled CWL Workflow steps must be mappings")
        for step in raw_steps
    ]


def _graph_steps_with_artifacts(
    graph: WorkflowGraph,
    artifact: CompilationArtifact,
) -> list[tuple[StepNode, Mapping[str, Any], CompilationArtifact]]:
    """Pair every resolved leaf step with its emitted step and the tool artifact it executes."""
    emitted_steps = _emitted_steps(artifact)
    if len(graph.steps) != len(artifact.children) or len(graph.steps) != len(emitted_steps):
        raise ValueError("resolved graph steps do not match compilation artifacts")
    leaves: list[tuple[StepNode, Mapping[str, Any], CompilationArtifact]] = []
    for step, emitted, child_artifact in zip(graph.steps, emitted_steps, artifact.children, strict=True):
        if step.run is None:
            raise ValueError(f"resolved step {step.id} has no run")
        child_graph = step.run.child
        if child_graph is None:
            if child_artifact.graph is not None:
                raise ValueError(f"tool step {step.id} unexpectedly owns a workflow artifact")
            leaves.append((step, emitted, child_artifact))
            continue
        if child_artifact.graph is None:
            raise ValueError(f"workflow step {step.id} has no resolved child artifact")
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


def _replace_step_source(raw: Any, source: str) -> Any:
    """Retain completed step-input metadata while replacing its resolved source."""
    if isinstance(raw, Mapping):
        return {**copy.deepcopy(dict(raw)), "source": source}
    return source


def _leaf_step_document(
    step: StepNode,
    emitted: Mapping[str, Any],
    *,
    names: Names,
    edge_sources: Mapping[PortId, str],
    boundary_sources: Mapping[PortId, str],
) -> dict[str, Any]:
    """Project one resolved leaf step without repeating inference or inlining."""
    if step.run is None or step.run.child is not None:
        raise ValueError(f"{step.id} is not a completed executable leaf step")
    ports = {names.port(port.id.port): port for port in step.inputs}
    raw_inputs = _as_mapping(emitted.get("in", {}), error="compiled step inputs must be a mapping")
    inputs: dict[str, Any] = {}
    for port_name, raw in raw_inputs.items():
        port = ports.get(port_name)
        source = None if port is None else edge_sources.get(
            port.id, boundary_sources.get(port.id)
        )
        inputs[port_name] = copy.deepcopy(raw) if source is None else _replace_step_source(raw, source)
    document = copy.deepcopy(dict(emitted))
    document["id"] = names.qualified(step.id)
    document["in"] = inputs
    return document


def compilation_result_source(result: CompilationResult) -> CompiledNextflowSource:
    """Project the completed core graph into the backend's closed source seam."""
    if not isinstance(result, CompilationResult):
        raise TypeError("Nextflow conversion requires a CompilationResult")
    graph = result.graph
    names = Names.of(graph)
    leaves = _graph_steps_with_artifacts(graph, result.artifact)
    all_steps = {step.id: step for step in graph.all_steps}
    leaf_names = {step.id: names.qualified(step.id) for step, _emitted, _artifact in leaves}

    edge_sources: dict[PortId, str] = {}
    for edge in _graph_edges(graph):
        sources = _expanded_boundary_port(edge.source, all_steps)
        sinks = _expanded_boundary_port(edge.sink, all_steps)
        if len(sources) != 1:
            raise ValueError(f"resolved edge source {edge.source} is not singular")
        source = _source_port_name(sources[0], leaf_names, names)
        for sink in sinks:
            previous = edge_sources.setdefault(sink, source)
            if previous != source:
                raise ValueError(f"resolved input {sink} has conflicting sources")

    boundary_sources: dict[PortId, str] = {}
    for name, mapped_ports in graph.input_mapping:
        for mapped_port in mapped_ports:
            for sink in _expanded_boundary_port(mapped_port, all_steps):
                previous = boundary_sources.setdefault(sink, names.port(name))
                if previous != names.port(name):
                    raise ValueError(f"resolved input {sink} has conflicting workflow parameters")

    workflow = copy.deepcopy(dict(result.artifact.cwl))
    workflow["steps"] = [
        _leaf_step_document(
            step,
            emitted,
            names=names,
            edge_sources=edge_sources,
            boundary_sources=boundary_sources,
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
    emitted_steps = _emitted_steps(artifact)
    if len(graph.steps) != len(artifact.children) or len(graph.steps) != len(emitted_steps):
        return [f"{path}.steps: resolved graph steps do not match compilation artifacts"]
    findings: list[str] = []
    for index, (step, emitted, child_artifact) in enumerate(
        zip(graph.steps, emitted_steps, artifact.children, strict=True)
    ):
        if step.run is None or step.run.child is None:
            continue
        step_path = f"{path}.steps[{index}]"
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
            _unconsumed_field_findings(
                {name: value for name, value in emitted.items() if name not in _STEP_CORE_FIELDS},
                consumed=_INERT_DOCUMENTATION_FIELDS,
                path=step_path,
            )
        )
        for raw_name, definition in _as_mapping(
            emitted.get("in", {}), error="compiled step inputs must be a mapping"
        ).items():
            if isinstance(definition, Mapping):
                findings.extend(
                    _unconsumed_field_findings(
                        definition,
                        consumed=_STEP_INPUT_CONSUMED_FIELDS,
                        path=f"{step_path}.in.{raw_name}",
                    )
                )

        child_path = f"{step_path}.run"
        child_document = child_artifact.cwl
        findings.extend(
            _unconsumed_field_findings(
                child_document,
                consumed=_WORKFLOW_CONSUMED_FIELDS,
                path=child_path,
            )
        )
        findings.extend(_workflow_requirement_findings(child_document, path=child_path))
        for section_name, consumed in (
            ("inputs", _NESTED_WORKFLOW_INPUT_CONSUMED_FIELDS),
            ("outputs", _WORKFLOW_OUTPUT_CONSUMED_FIELDS),
        ):
            section = child_document.get(section_name, {})
            if not isinstance(section, Mapping):
                findings.append(
                    f"{child_path}.{section_name}: compiled CWL Workflow "
                    f"{section_name} must be a mapping"
                )
                continue
            for raw_name, definition in section.items():
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition,
                            consumed=consumed,
                            path=f"{child_path}.{section_name}.{raw_name}",
                        )
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

    match tool.get("class"):
        case "CommandLineTool":
            pass
        case unsupported_class:
            findings.append(
                f"{path}.run.class: unsupported compiled step class {unsupported_class!r}"
            )
            return findings

    findings.extend(
        _unconsumed_field_findings(
            tool,
            consumed=_TOOL_CONSUMED_FIELDS,
            path=f"{path}.run",
        )
    )
    findings.extend(_path_suffix_findings(tool, path=path))

    for section_name in ("requirements", "hints"):
        section = tool.get(section_name)
        for class_name, suffix in _requirement_names(section):
            requirement_path = f"{path}.run.{section_name}.{suffix}"
            if not class_name:
                findings.append(f"{requirement_path}: {_CLASSLESS_REQUIREMENT}")
            elif class_name not in _SUPPORTED_REQUIREMENTS:
                findings.append(
                    f"{requirement_path}: {class_name} is not supported by the Nextflow target"
                )
            elif definition := _requirement_definition(
                section,
                class_name=class_name,
                suffix=suffix,
            ):
                findings.extend(
                    _unconsumed_field_findings(
                        definition,
                        consumed=_SUPPORTED_REQUIREMENT_FIELDS[class_name],
                        path=requirement_path,
                    )
                )
                if class_name == "ResourceRequirement":
                    findings.extend(
                        _resource_requirement_findings(
                            definition,
                            path=requirement_path,
                        )
                    )
                if class_name == "InitialWorkDirRequirement":
                    findings.extend(
                        _iwdr_listing_findings(
                            tool,
                            definition,
                            path=requirement_path,
                        )
                    )
    try:
        renamed_names = set(_iwdr_stage_as(tool))
    except ValueError:
        renamed_names = set()
    findings.extend(_iwdr_rename_reference_findings(tool, renamed_names, path=path))
    shell_mode_active = _requirement(tool, "ShellCommandRequirement") is not None
    match tool.get("inputs", {}):
        case Mapping() as inputs:
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
                findings.extend(
                    _unconsumed_field_findings(
                        raw_definition,
                        consumed=_INPUT_CONSUMED_FIELDS,
                        path=input_path,
                    )
                )
                findings.extend(_default_value_findings(raw_definition, path=input_path))
                if "loadContents" in raw_definition:
                    findings.append(
                        f"{input_path}.loadContents: {_LOAD_CONTENTS_INPUT_REASON}"
                    )
                binding = raw_definition.get("inputBinding")
                if not isinstance(binding, Mapping):
                    continue
                input_binding_path = f"{input_path}.inputBinding"
                findings.extend(
                    _unconsumed_field_findings(
                        binding,
                        consumed=_INPUT_BINDING_CONSUMED_FIELDS,
                        path=input_binding_path,
                    )
                )
                if "loadContents" in binding:
                    findings.append(
                        f"{input_binding_path}.loadContents: {_LOAD_CONTENTS_INPUT_REASON}"
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
        case _:
            pass

    match tool.get("arguments", []):
        case list() as arguments:
            for argument_index, argument in enumerate(arguments):
                if not isinstance(argument, Mapping):
                    continue
                argument_path = f"{path}.run.arguments[{argument_index}]"
                findings.extend(
                    _unconsumed_field_findings(
                        argument,
                        consumed=_ARGUMENT_CONSUMED_FIELDS,
                        path=argument_path,
                    )
                )
                if argument.get("shellQuote") is False:
                    try:
                        _shell_literal_value(
                            f"arguments[{argument_index}]", argument, shell_mode=shell_mode_active
                        )
                    except ValueError as exc:
                        findings.append(f"{argument_path}.shellQuote: {exc}")
        case _:
            pass

    match tool.get("outputs", {}):
        case Mapping() as outputs:
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
                findings.extend(
                    _unconsumed_field_findings(
                        raw_definition,
                        consumed=_OUTPUT_CONSUMED_FIELDS,
                        path=output_path,
                    )
                )
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
                    _unconsumed_field_findings(
                        binding,
                        consumed=_OUTPUT_BINDING_CONSUMED_FIELDS,
                        path=output_binding_path,
                    )
                )
                try:
                    _output_capture(raw_name, raw_definition)
                except ValueError as exc:
                    findings.append(f"{output_binding_path}: {exc}")
        case _:
            pass
    return findings


def _workflow_capability_findings(
    workflow: Mapping[str, Any],
    provided_params: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
) -> list[str]:
    """Apply closed-world analysis to workflow, step, and boundary values."""
    findings = _unconsumed_field_findings(
        workflow,
        consumed=_WORKFLOW_CONSUMED_FIELDS,
        path="workflow",
    )
    findings.extend(_workflow_requirement_findings(workflow))
    provided = _as_mapping(
        provided_params,
        error="compiled workflow input values must be a mapping",
    )
    match workflow.get("inputs", {}):
        case Mapping() as inputs:
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
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition,
                            consumed=_WORKFLOW_INPUT_CONSUMED_FIELDS,
                            path=input_path,
                        )
                    )
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
        case _:
            pass

    match workflow.get("outputs", {}):
        case Mapping() as outputs:
            findings.extend(
                _identifier_normalization_findings(
                    outputs,
                    context="workflow output",
                    path="workflow.outputs",
                )
            )
            for raw_name, definition in outputs.items():
                if isinstance(definition, Mapping):
                    findings.extend(
                        _unconsumed_field_findings(
                            definition,
                            consumed=_WORKFLOW_OUTPUT_CONSUMED_FIELDS,
                            path=f"workflow.outputs.{raw_name}",
                        )
                    )
        case _:
            pass

    for step_index, step in enumerate(steps):
        step_path = f"steps[{step_index}]"
        findings.extend(
            _unconsumed_field_findings(
                step,
                consumed=_STEP_CONSUMED_FIELDS,
                path=step_path,
            )
        )
        if "scatterMethod" in step and "scatter" not in step:
            findings.append(
                f"{step_path}.scatterMethod: scatterMethod without scatter is not executable"
            )
        match step.get("in", {}):
            case Mapping() as step_inputs:
                for raw_name, definition in step_inputs.items():
                    if isinstance(definition, Mapping):
                        findings.extend(
                            _unconsumed_field_findings(
                                definition,
                                consumed=_STEP_INPUT_CONSUMED_FIELDS,
                                path=f"{step_path}.in.{raw_name}",
                            )
                        )
            case _:
                pass
    return findings


def _workflow_requirement_findings(
    workflow: Mapping[str, Any],
    *,
    path: str = "workflow",
) -> list[str]:
    """Apply closed-world analysis to workflow-level requirements."""
    section = workflow.get("requirements")
    match section:
        case None:
            return []
        case Mapping() | list():
            pass
        case _:
            return [f"{path}.requirements: CWL Workflow requirements must be a mapping or list"]
    findings: list[str] = []
    for class_name, suffix in _requirement_names(section):
        requirement_path = f"{path}.requirements.{suffix}"
        if not class_name:
            findings.append(f"{requirement_path}: {_CLASSLESS_REQUIREMENT}")
        elif class_name not in _SUPPORTED_WORKFLOW_REQUIREMENTS:
            findings.append(
                f"{requirement_path}: {class_name} is not supported at the Nextflow workflow level"
            )
        elif definition := _requirement_definition(section, class_name=class_name, suffix=suffix):
            findings.extend(
                _unconsumed_field_findings(
                    definition,
                    consumed=frozenset({"class"}),
                    path=requirement_path,
                )
            )
    return findings


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
    """Map every recognizable step identifier spelling to its step index."""
    indices: dict[str, int] = {}
    for index, step in enumerate(steps):
        match step.get("id"):
            case str() as raw_id:
                for candidate in (raw_id, raw_id.rsplit("#", maxsplit=1)[-1]):
                    indices[candidate] = index
            case _:
                continue
    return indices


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
    """Reject every process edge whose cardinality a scattered step changes.

    A scattered step's output is a queue channel of one value per task, which
    drives N downstream invocations where CWL gives the consumer one
    invocation receiving an array; and a process output feeding a scattered
    step is itself a queue channel, which would truncate the scatter to one
    task instead of N.
    """
    findings: list[str] = []
    indices = _step_indices_by_id(steps)
    scattered = _scattered_names_by_index(steps)
    if not scattered:
        return findings
    for step_index, step in enumerate(steps):
        step_inputs = step.get("in", {})
        if not isinstance(step_inputs, Mapping):
            continue
        for raw_name, raw_source in step_inputs.items():
            try:
                sources = _source_values(
                    raw_source,
                    context=f"step input {step_index}.{raw_name}",
                )
            except ValueError:
                continue
            for source in sources:
                if "/" not in source:
                    continue
                raw_process = source.rsplit("/", maxsplit=1)[0]
                producer = indices.get(
                    raw_process,
                    indices.get(raw_process.rsplit("#", maxsplit=1)[-1]),
                )
                if producer is not None and producer in scattered:
                    # Every scatter is indexed, so its outputs are gathered once, in
                    # invocation order (design §6, Topology, Gather).
                    if steps[producer].get("scatterMethod") == "nested_crossproduct":
                        findings.append(
                            f"steps[{step_index}].in.{raw_name}: {source!r} is a nested_crossproduct "
                            "result, which reaches workflow outputs only"
                        )
                elif step_index in scattered:
                    findings.append(
                        f"steps[{step_index}].in.{raw_name}: a scattered step takes inputs from "
                        f"workflow inputs or scattered steps; {source!r}, from a step that is not "
                        "scattered, would truncate the scatter to one task"
                    )
    return findings


def _template_reference_names(templates: Iterable[NfTemplate]) -> tuple[set[str], set[str]]:
    """Return (plainly-referenced, basename-referenced) input names across templates."""
    plain: set[str] = set()
    basenamed: set[str] = set()
    for template in templates:
        for segment in template.segments:
            if isinstance(segment, NfInputReference):
                plain.add(segment.name)
            elif isinstance(segment, NfBasenameReference):
                basenamed.add(segment.name)
    return plain, basenamed


def _safe_absence_names(tool: Mapping[str, Any]) -> set[str] | None:
    """Return the input names whose absence cannot affect command rendering.

    A name is safe when its absence leaves rendering unchanged: it is not
    referenced by any command token, stream target, or output glob, it does
    not own a shell-literal binding that CWL would omit, or it is referenced
    solely as the boolean-flag token it drives (a flag tests its value, never
    calls ``.toString()`` on it). Returns None when the tool's command or
    outputs cannot be analyzed; lowering reports why, so absence is not judged.
    """
    try:
        command = _command(tool)
        outputs = _ports(tool.get("outputs", {}), outputs=True)
    except (ValueError, TypeError):
        return None
    templates = [
        *(token for token in command.tokens if isinstance(token, NfTemplate)),
        *(stream for stream in (command.stdin, command.stdout, command.stderr) if stream),
        *(port.glob for port in outputs if port.glob),
    ]
    plain, basenamed = _template_reference_names(templates)
    # Every template reference dereferences the value, so it is unsafe even
    # when the same input also drives a flag: excusing it on the strength of
    # the flag use would let the absence sentinel render into a command line.
    # A flag-only boolean has no template reference and never enters `plain`.
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
    computed = {name for token in command.tokens if isinstance(token, NfComputed) for name in token.names}
    unsafe = plain | basenamed | shell_literal_names | computed
    return all_names - unsafe


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
                absent = any(
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


def _conditional_consumer_findings(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
) -> list[str]:
    """Reject every consumer of a conditional step's output the design does not admit.

    A conditional step's output may be null at runtime (design §6, Topology).
    That null is admitted only at a workflow output (which reports null) or an
    optional val input of a later step whose use the absent-optional lowering
    already admits (never referenced, or referenced solely as a boolean-flag
    token). Every other consumer is rejected by name, as is an array-typed
    output of a conditional step outside scatter, where the sentinel would
    collide with a genuinely empty array.
    """
    findings: list[str] = []
    conditional_ids: dict[str, int] = {}
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        if "when" not in step or tool.get("class") != "CommandLineTool":
            continue
        raw_id = step.get("id")
        if not isinstance(raw_id, str):
            continue
        conditional_ids[raw_id] = step_index
        conditional_ids[raw_id.rsplit("#", maxsplit=1)[-1]] = step_index
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

    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        step_in = step.get("in", {})
        tool_inputs = tool.get("inputs", {})
        if not isinstance(step_in, Mapping) or not isinstance(tool_inputs, Mapping):
            continue
        for raw_name, raw_source in step_in.items():
            try:
                sources = _source_values(raw_source, context="")
            except ValueError:
                continue
            consumes_conditional = any(
                "/" in source and source.rsplit("/", maxsplit=1)[0] in conditional_ids
                for source in sources
            )
            if not consumes_conditional:
                continue
            path = f"steps[{step_index}].in.{raw_name}"
            definition = tool_inputs.get(raw_name)
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
    match workflow.get("inputs", {}):
        case Mapping() as inputs:
            for raw_name, definition in inputs.items():
                declared = definition.get("type") if isinstance(definition, Mapping) else definition
                types[str(raw_name)] = declared
        case _:
            pass
    for step, tool in zip(steps, tools, strict=True):
        match step.get("id"):
            case str() as raw_id:
                pass
            case _:
                continue
        outputs = tool.get("outputs", {})
        if not isinstance(outputs, Mapping):
            continue
        for raw_port, definition in outputs.items():
            declared = definition.get("type") if isinstance(definition, Mapping) else definition
            for step_key in (raw_id, raw_id.rsplit("#", maxsplit=1)[-1]):
                types[f"{step_key}/{raw_port}"] = declared
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
        match step.get("id"):
            case str() as raw_id:
                pass
            case _:
                continue
        if "when" in step:
            # A conditional step's captured-text output is the one proven
            # exception (design §6, Topology): _conditional_consumer_findings
            # already governs its downstream sinks with the admission rules
            # a possibly-null value requires.
            continue
        outputs = tool.get("outputs", {})
        if not isinstance(outputs, Mapping):
            continue
        for raw_port, definition in outputs.items():
            if not isinstance(definition, Mapping):
                continue
            try:
                capture = _output_capture(raw_port, definition)
            except ValueError:
                continue
            if capture != "text":
                continue
            for step_key in (raw_id, raw_id.rsplit("#", maxsplit=1)[-1]):
                endpoints.add(f"{step_key}/{raw_port}")
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
    if not endpoints:
        return []
    findings: list[str] = []
    for step_index, step in enumerate(steps):
        step_inputs = step.get("in", {})
        if not isinstance(step_inputs, Mapping):
            continue
        for raw_name, raw_source in step_inputs.items():
            try:
                sources = _source_values(raw_source, context=f"steps[{step_index}].in.{raw_name}")
            except ValueError:
                continue
            for source in sources:
                if source in endpoints:
                    findings.append(
                        f"steps[{step_index}].in.{raw_name}: {source!r} captures file text; "
                        "its only approved sink is a workflow output, because the executable "
                        "graph has no qualifier agreement check for process edges"
                    )
    return findings


def _capability_error(findings: list[str]) -> ValueError:
    details = "\n".join(f"- {finding}" for finding in findings)
    return ValueError(f"Nextflow capability analysis failed:\n{details}")


def _raise_capability_findings(findings: list[str]) -> None:
    if findings:
        raise _capability_error(findings)


def _condition_value(step: Mapping[str, Any], tool: Mapping[str, Any]) -> Expr:
    """Parse and boolean-type a step's ``when`` over the inputs the step binds, as CWL does.

    Mirrors ``_computed_value``'s reuse of the safe-subset parser and
    checker; a step's when is evaluated once per invocation, after inputs
    are bound, so it is typed exactly like any other safe-subset expression.
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
    match tool.get("class"):
        case "CommandLineTool":
            pass
        case unsupported_class:
            raise ValueError(f"unsupported compiled step class {unsupported_class!r}")
    when = step.get("when")
    return NfProcess(
        name=_identifier(step.get("id"), context="workflow step id"),
        inputs=_ports(tool.get("inputs", {}), outputs=False, stage_as=_iwdr_stage_as(tool)),
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
            unsupported = set(source_definition) - {"source"}
            if unsupported:
                raise ValueError(f"unsupported {context} fields: {', '.join(sorted(unsupported))}")
            return _source_values(source, context=context)
        case Mapping():
            raise ValueError(f"{context} must define source")
        case _:
            raise ValueError(f"{context} must be a source string or list of source strings")


def _step_name_map(steps: list[Mapping[str, Any]], processes: list[NfProcess]) -> dict[str, str]:
    names: dict[str, str] = {}
    for step, process in zip(steps, processes, strict=True):
        match step.get("id"):
            case str() as raw_name:
                pass
            case _:
                raise ValueError("workflow step id must be a string")
        for candidate in (raw_name, raw_name.rsplit("#", maxsplit=1)[-1]):
            if candidate in names and names[candidate] != process.name:
                raise ValueError(f"workflow step identifier {candidate!r} is ambiguous")
            names[candidate] = process.name
    return names


def _source_endpoint(source: str, step_names: Mapping[str, str]) -> tuple[str | None, str]:
    if "/" not in source:
        return None, _identifier(source, context="workflow input source")
    raw_process, raw_port = source.rsplit("/", maxsplit=1)
    process = step_names.get(raw_process, step_names.get(raw_process.rsplit("#", maxsplit=1)[-1]))
    if process is None:
        raise ValueError(f"connection references unknown source process {raw_process!r}")
    return process, _identifier(raw_port, context="process output source")


def _step_connections(
    steps: list[Mapping[str, Any]],
    processes: list[NfProcess],
    step_names: Mapping[str, str],
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
        # Capability analysis already admitted only an approved method for two or more.
        scatter_method = str(step.get("scatterMethod", "dotproduct"))
        for raw_port, raw_source in raw_inputs.items():
            destination_port = _identifier(raw_port, context="process input destination")
            for source in _source_values(raw_source, context=f"step input {process.name}.{destination_port}"):
                source_process, source_port = _source_endpoint(source, step_names)
                if source_process is None:
                    if destination_port in scattered:
                        # Every scatter is indexed, so its gather keeps input order; at
                        # one scattered input the three methods coincide.
                        adapter = scatter_method if len(scattered) > 1 else "dotproduct"
                    else:
                        adapter = None
                    connections.append(
                        NfWorkflowInputConnection(
                            source_port,
                            process.name,
                            destination_port,
                            adapter,
                        )
                    )
                else:
                    gathers = source_process in multi_scattered
                    connections.append(
                        NfProcessConnection(
                            source_process,
                            source_port,
                            process.name,
                            destination_port,
                            (scatter_method if destination_port in scattered else "gather") if gathers else None,
                        )
                    )
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
) -> list[NfConnection]:
    step_names = _step_name_map(steps, processes)
    return [
        *_step_connections(steps, processes, step_names),
        *_workflow_output_connections(workflow, step_names),
    ]


def _default_bindings(
    steps: list[Mapping[str, Any]],
    tools: tuple[Mapping[str, Any], ...],
    processes: list[NfProcess],
) -> tuple[list[NfWorkflowInputConnection], dict[str, Any]]:
    """Synthesize workflow-input connections for every unwired process input.

    Covers two cases: a declared CWL default, and an unwired optional input
    with no default. Capability analysis has already proven any remaining
    unwired, no-default optional input is a safe absence, so this always
    contributes a null value for those rather than re-deriving safety.
    """
    connections: list[NfWorkflowInputConnection] = []
    params: dict[str, Any] = {}
    for step, tool, process in zip(steps, tools, processes, strict=True):
        bound = _as_mapping(step.get("in", {}), error="compiled step inputs must be a mapping")
        inputs = _as_mapping(tool.get("inputs", {}), error="tool inputs must be a mapping")
        for raw_name, definition in inputs.items():
            if raw_name in bound or not isinstance(definition, Mapping):
                continue
            if "default" in definition:
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
            param_name = _identifier(f"{process.name}___{port_name}", context="default parameter")
            connections.append(NfWorkflowInputConnection(param_name, process.name, port_name))
            params[param_name] = value
    return connections, params


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


def _workflow_steps(workflow: Mapping[str, Any], *, child_count: int) -> list[Mapping[str, Any]]:
    raw_steps = _as_list(workflow.get("steps", []), error="compiled CWL Workflow steps must be a list")
    steps: list[Mapping[str, Any]] = []
    for step in raw_steps:
        match step:
            case Mapping() as step_definition:
                steps.append(step_definition)
            case _:
                raise ValueError("compiled CWL Workflow steps must be mappings")
    if len(steps) != child_count:
        raise ValueError("compiled CWL steps do not match compiled tool artifacts")
    return steps


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


def _apply_absent_sentinel(params: Mapping[str, Any]) -> dict[str, Any]:
    """Replace every remaining null parameter with the runtime-proven [] sentinel.

    Capability analysis (via _absent_optional_findings, which itself calls
    _workflow_params and depends on None to detect absence) has already
    proven any null still present here is a safe absent-optional val input.
    This substitution happens only once, after that analysis, so it never
    interferes with it.
    """
    return {name: (ABSENT_VAL_SENTINEL if value is None else value) for name, value in params.items()}


def _container_policy_findings(tools: tuple[Mapping[str, Any], ...]) -> list[str]:
    """Require one workflow-wide host or container execution policy."""
    containerized: list[int] = []
    host: list[int] = []
    for step_index, tool in enumerate(tools):
        requirement_names = [
            class_name
            for section_name in ("requirements", "hints")
            for class_name, _suffix in _requirement_names(tool.get(section_name))
        ]
        target = containerized if "DockerRequirement" in requirement_names else host
        target.append(step_index)
    if not containerized or not host:
        return []
    return [
        "workflow.steps: mixed container execution is not supported; "
        "DockerRequirement must be declared by every process or by none "
        f"(containerized steps: {containerized}; host steps: {host})"
    ]


def compiled_source_to_nextflow(
    source: CompilationResult | CompiledNextflowSource,
) -> ExecutableNextflowWorkflow:
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
        ExecutableNextflowWorkflow: The validated executable representation.
    """
    if isinstance(source, CompilationResult):
        nested_findings = _nested_workflow_capability_findings(
            source.graph,
            source.artifact,
        )
        compiled = compilation_result_source(source)
    elif isinstance(source, CompiledNextflowSource):
        nested_findings = []
        compiled = source
    else:
        raise TypeError("Nextflow conversion requires a CompilationResult")
    compiled = replace(compiled, params=_local_locations_as_paths(compiled.params))
    workflow = compiled.workflow
    tools = compiled.tools
    steps = _workflow_steps(workflow, child_count=len(tools))
    findings = list(nested_findings)
    processes: list[NfProcess] = []
    for step_index, (step, tool) in enumerate(zip(steps, tools, strict=True)):
        if step_findings := _tool_capability_findings(step, tool, step_index=step_index):
            findings.extend(step_findings)
            continue
        try:
            processes.append(_process(step, tool))
        except ValueError as error:  # lowering and the model reject what no source pass can see
            findings.append(f"steps[{step_index}]: {error}")
    findings.extend(_workflow_capability_findings(workflow, compiled.params, steps))
    findings.extend(_absent_optional_findings(workflow, compiled.params, steps, tools))
    findings.extend(_conditional_consumer_findings(list(steps), tools))
    findings.extend(_flag_source_findings(workflow, steps, tools))
    findings.extend(_scatter_findings(workflow, steps, tools))
    findings.extend(_text_capture_sink_findings(steps, tools))
    findings.extend(_container_policy_findings(tools))
    _raise_capability_findings(findings)
    try:
        default_connections, default_params = _default_bindings(steps, tools, processes)
        params = _workflow_params(workflow, compiled.params)
        if collisions := sorted(set(params) & set(default_params)):
            raise ValueError(
                "lowered workflow parameter names collide: "
                f"{', '.join(collisions)}"
            )
        params.update(default_params)
        return ExecutableNextflowWorkflow(
            name=_identifier(compiled.name, context="workflow name"),
            processes=processes,
            connections=[*_connections(workflow, list(steps), processes), *default_connections],
            params=_apply_absent_sentinel(params),
        )
    except ValueError as error:
        raise _capability_error([f"workflow: {error}"]) from None
