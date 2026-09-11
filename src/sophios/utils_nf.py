"""Convert compiled Sophios RoseTrees into the Nextflow intermediate representation."""

from collections.abc import Callable, Iterable, Mapping
import copy
import math
from os import PathLike
import re
from typing import Any

from .nf_symbols import normalize_nextflow_identifier
from .nf_types import (
    ExecutableNextflowWorkflow,
    NF_INTERNAL_IDENTIFIERS,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
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
from .wic_types import NodeData, RoseTree


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
    """Map the documented Phase 1 CWL type subset to a Nextflow qualifier.

    Args:
        cwl_type (Any): A CWL type expression; optional forms (``"File?"``,
            ``["null", ...]``) map like their required type. Array types are
            never accepted here; callers unwrap them with
            :func:`_array_item_type` first.

    Raises:
        ValueError: If the type is outside the supported Phase 1 subset.

    Returns:
        str: ``"path"`` for File/Directory, ``"val"`` for supported scalars.
    """
    match _required_type(cwl_type):
        case "File" | "Directory":
            return "path"
        case "string" | "int" | "float" | "boolean":
            return "val"
        case _:
            raise ValueError(f"unsupported CWL type for Nextflow Phase 1: {cwl_type!r}")


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
                    )
                )
            case _:
                raise ValueError(f"CWL port {raw_name!r} must declare a type")
    return ports


_INPUT_EXPRESSION = re.compile(
    r"\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)(?:\.(path|basename))?\s*\)"
)


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
    segments: list[NfTemplateSegment] = []
    offset = 0
    for match in _INPUT_EXPRESSION.finditer(text):
        if match.start() > offset:
            segments.append(NfLiteral(text[offset:match.start()]))
        name = _identifier(match.group(1), context="input reference")
        segments.append(
            NfBasenameReference(name) if match.group(2) == "basename" else NfInputReference(name)
        )
        offset = match.end()
    if offset < len(text):
        segments.append(NfLiteral(text[offset:]))
    residual = _INPUT_EXPRESSION.sub("", text)
    if "$" in residual:
        raise ValueError(
            f"{context} contains an unsupported CWL expression; the supported form is "
            "$(inputs.<name>), optionally followed by .path or .basename"
        )
    return NfTemplate(tuple(segments or [NfLiteral(text)]))


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


def _argument_items(
    arguments: list[Any],
    *,
    shell_mode: bool,
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
    for index, item in enumerate(listing):
        try:
            name, rename = _iwdr_listing_item(item, tool_inputs=tool_inputs)
        except ValueError as exc:
            findings.append(f"{path}.listing[{index}]: {exc}")
            continue
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
    for item in listing:
        name, rename = _iwdr_listing_item(item, tool_inputs=tool_inputs)
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
        *_argument_items(arguments, shell_mode=shell_mode),
        *_input_binding_items(inputs, shell_mode=shell_mode),
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
    if not any(isinstance(token, NfTemplate) for token in tokens):
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
    return resource.get(maximum, resource.get(minimum))


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
    """Validate every resource value consumed by the Phase 1 lowering."""
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
                f"CWL output glob lists for {raw_name!r} are deferred beyond Nextflow Phase 1"
            )
        case _:
            raise ValueError(f"CWL output glob for {raw_name!r} must be a string or list")


_SUPPORTED_REQUIREMENTS = frozenset({
    "DockerRequirement",
    "InitialWorkDirRequirement",
    "InlineJavascriptRequirement",
    "ResourceRequirement",
    "ShellCommandRequirement",
})
_DEFERRED_REQUIREMENTS: dict[str, str] = {}

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
    frozenset({"default", "inputBinding", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_INPUT_BINDING_CONSUMED_FIELDS = frozenset({
    "position",
    "prefix",
    "separate",
    "shellQuote",
    "valueFrom",
})
_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputBinding", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_OUTPUT_BINDING_CONSUMED_FIELDS = frozenset({"glob"})
_OUTPUT_BINDING_DEFERRED_FIELDS = frozenset({"loadContents", "outputEval"})
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
    "steps",
}) | _INERT_DOCUMENTATION_FIELDS
# Workflow-level requirements that only declare a feature whose lowering is
# decided per step, so they are consumed as inert no-ops.
_SUPPORTED_WORKFLOW_REQUIREMENTS = frozenset({
    "ScatterFeatureRequirement",
    "SubworkflowFeatureRequirement",
})
_WORKFLOW_INPUT_CONSUMED_FIELDS = (
    frozenset({"default", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_WORKFLOW_OUTPUT_CONSUMED_FIELDS = (
    frozenset({"outputSource", "type"}) | _INERT_DOCUMENTATION_FIELDS
)
_STEP_CONSUMED_FIELDS = frozenset({
    "id",
    "in",
    "out",
    "run",
    "scatter",
    "scatterMethod",
    "when",
}) | _INERT_DOCUMENTATION_FIELDS
_STEP_INPUT_CONSUMED_FIELDS = frozenset({"source"})


def _default_value_findings(definition: Mapping[str, Any], *, path: str) -> list[str]:
    """Validate a declared scalar default against the Phase 1 value subset."""
    if "default" not in definition:
        return []
    default = definition["default"]
    if default is None or isinstance(default, (Mapping, list)):
        return [f"{path}.default: Phase 1 supports JSON scalar defaults only"]
    if not _phase1_value_matches(definition.get("type"), default):
        return [f"{path}.default: value does not match its supported CWL type"]
    return []


def _unconsumed_field_findings(
    value: Mapping[str, Any],
    *,
    consumed: frozenset[str],
    path: str,
) -> list[str]:
    """Reject source fields that the Phase 1 lowering does not consume."""
    return [
        f"{path}.{field_name}: {field_name} is not consumed by Nextflow Phase 1 lowering"
        for field_name in sorted(set(value) - consumed)
    ]


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


def _requirement_names(section: Any) -> list[tuple[str, str]]:
    """Return requirement names and stable path suffixes for capability analysis."""
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
                        continue
            return names
        case _:
            return []


def _basename_template_positions(
    tool: Mapping[str, Any],
    *,
    path: str,
) -> list[tuple[Any, str]]:
    """Pair every templated tool value with the CWL path it was written at.

    A basename reference is legal in any template position, so the
    source-level requirement has to look everywhere one can appear rather
    than only in output globs.
    """
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


def _basename_source_findings(tool: Mapping[str, Any], *, path: str) -> list[str]:
    """Require a File or Directory source for every basename reference.

    A basename reference renders the staged path's ``name`` property, which
    equals the CWL ``basename`` only because Nextflow stages a path input
    under its original file name; a val-qualified input is never staged, so
    the reference has nothing to read. ``NfProcess`` rejects the same shape
    as a model invariant, but that raise names normalized identifiers,
    carries no source location, and stops at the first offender, so the
    source-level requirement is reported here by CWL path and aggregates
    with every other finding.
    """
    raw_inputs = tool.get("inputs", {})
    if not isinstance(raw_inputs, Mapping):
        return []
    declared: dict[str, Any] = {}
    for raw_name, definition in raw_inputs.items():
        if not isinstance(definition, Mapping):
            continue
        try:
            declared[_identifier(raw_name, context="tool input")] = definition.get("type")
        except ValueError:
            continue
    findings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for value, position in _basename_template_positions(tool, path=path):
        try:
            template = _template(value, context="basename source")
        except ValueError:
            # An unsupported expression form is reported by the pass that
            # owns it; this one only judges the references it can read.
            continue
        for segment in template.segments:
            if not isinstance(segment, NfBasenameReference):
                continue
            if segment.name not in declared or (position, segment.name) in seen:
                continue
            try:
                qualifier = cwl_type_to_nf_qualifier(declared[segment.name])
            except ValueError:
                continue
            if qualifier == "path":
                continue
            seen.add((position, segment.name))
            findings.append(
                f"{position}: $(inputs.{segment.name}.basename) requires a File or "
                f"Directory input; {segment.name} lowers to a {qualifier} channel"
            )
    return findings


def _tool_capability_findings(
    step: Mapping[str, Any],
    child: RoseTree,
    *,
    step_index: int,
) -> list[str]:
    """Collect unsupported executable semantics without lowering the tool."""
    path = f"steps[{step_index}]"
    findings: list[str] = []
    if "when" in step:
        findings.append(f"{path}.when: CWL step when conditions are not supported in Nextflow Phase 1")

    match child:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            return findings
    match node_data.compiled_cwl:
        case Mapping() as tool:
            pass
        case _:
            return findings
    match tool.get("class"):
        case "CommandLineTool":
            # Inlining consumes every CWL Workflow child before this pass, so
            # children here belong to a tool that cannot own them.
            if sub_trees:
                findings.append(
                    f"{path}.run: a CommandLineTool step cannot carry nested children"
                )
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
    findings.extend(_basename_source_findings(tool, path=path))

    for section_name in ("requirements", "hints"):
        section = tool.get(section_name)
        for class_name, suffix in _requirement_names(section):
            requirement_path = f"{path}.run.{section_name}.{suffix}"
            if class_name in _DEFERRED_REQUIREMENTS:
                findings.append(f"{requirement_path}: {_DEFERRED_REQUIREMENTS[class_name]}")
            elif class_name not in _SUPPORTED_REQUIREMENTS:
                findings.append(
                    f"{requirement_path}: {class_name} is not supported by Nextflow Phase 1"
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
                try:
                    qualifier = cwl_type_to_nf_qualifier(raw_definition.get("type"))
                except ValueError:
                    qualifier = "unsupported"
                if qualifier != "path":
                    findings.append(
                        f"{output_path}.type: primitive and non-path output capture is deferred to Phase 2"
                    )
                binding = raw_definition.get("outputBinding")
                if not isinstance(binding, Mapping):
                    continue
                output_binding_path = f"{output_path}.outputBinding"
                findings.extend(
                    _unconsumed_field_findings(
                        binding,
                        consumed=(
                            _OUTPUT_BINDING_CONSUMED_FIELDS
                            | _OUTPUT_BINDING_DEFERRED_FIELDS
                        ),
                        path=output_binding_path,
                    )
                )
                for field_name in ("loadContents", "outputEval"):
                    if field_name in binding:
                        findings.append(
                            f"{output_binding_path}.{field_name}: "
                            f"{field_name} output capture is deferred to Phase 2"
                        )
        case _:
            pass
    return findings


def _workflow_capability_findings(
    workflow: Mapping[str, Any],
    node_data: NodeData,
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
        node_data.workflow_inputs_file,
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
        if class_name not in _SUPPORTED_WORKFLOW_REQUIREMENTS:
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


_SUBWORKFLOW_NAMESPACE = "___"


def _local_name(raw_id: str) -> str:
    """Return the identifier fragment a CWL id ends with."""
    return raw_id.rsplit("#", maxsplit=1)[-1]


def _map_sources(value: Any, transform: Callable[[str], str]) -> Any:
    """Rewrite every source string in one step-input binding, preserving its shape.

    An unrecognized binding shape is returned untouched so the closed-world
    field analysis still sees and reports it.
    """
    match value:
        case str() as source:
            return transform(source)
        case list() as sources:
            return [transform(item) if isinstance(item, str) else item for item in sources]
        case Mapping() as mapping if "source" in mapping:
            return {**mapping, "source": _map_sources(mapping["source"], transform)}
        case _:
            return value


def _subworkflow_document(child: Any) -> tuple[Mapping[str, Any], list[Any]] | None:
    """Return a step child's subworkflow document and children, or None for a tool."""
    match child:
        case RoseTree(data=NodeData(compiled_cwl=Mapping() as document), sub_trees=sub_trees):
            if document.get("class") == "Workflow":
                return document, list(sub_trees)
        case _:
            pass
    return None


def _subworkflow_bindings(
    step: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    path: str,
) -> tuple[dict[str, str], list[str]]:
    """Bind every declared subworkflow input to exactly one outer source."""
    findings: list[str] = []
    bindings: dict[str, str] = {}
    step_inputs = step.get("in", {})
    if not isinstance(step_inputs, Mapping):
        return bindings, [f"{path}.in: compiled step inputs must be a mapping"]
    for raw_name, raw_source in step_inputs.items():
        if raw_name not in inputs:
            findings.append(
                f"{path}.in.{raw_name}: the subworkflow declares no input named {raw_name!r}"
            )
            continue
        try:
            sources = _source_values(raw_source, context=f"{path}.in.{raw_name}")
        except ValueError as exc:
            findings.append(f"{path}.in.{raw_name}: {exc}")
            continue
        if len(sources) != 1:
            findings.append(
                f"{path}.in.{raw_name}: a subworkflow input must have exactly one source"
            )
            continue
        bindings[str(raw_name)] = sources[0]
    for raw_name in inputs:
        if raw_name not in bindings and raw_name not in step_inputs:
            findings.append(
                f"{path}.run.inputs.{raw_name}: the step does not bind subworkflow input "
                f"{raw_name!r}; a subworkflow input is never defaulted from outside"
            )
    return bindings, findings


def _subworkflow_output_endpoints(
    document: Mapping[str, Any],
    inner_ids: set[str],
    *,
    namespace: str,
    path: str,
) -> tuple[dict[str, str], list[str]]:
    """Resolve every declared subworkflow output to one inlined step endpoint."""
    findings: list[str] = []
    endpoints: dict[str, str] = {}
    outputs = document.get("outputs", {})
    if not isinstance(outputs, Mapping):
        return endpoints, [f"{path}.run.outputs: compiled CWL Workflow outputs must be a mapping"]
    for raw_name, definition in outputs.items():
        output_path = f"{path}.run.outputs.{raw_name}"
        match definition:
            case {"outputSource": output_source}:
                pass
            case _:
                findings.append(f"{output_path}: subworkflow output must define outputSource")
                continue
        try:
            sources = _source_values(output_source, context=output_path)
        except ValueError as exc:
            findings.append(f"{output_path}: {exc}")
            continue
        if len(sources) != 1:
            findings.append(
                f"{output_path}: a subworkflow output must have exactly one outputSource"
            )
            continue
        source = sources[0]
        if "/" not in source:
            findings.append(
                f"{output_path}: subworkflow output {raw_name!r} forwards subworkflow input "
                f"{source!r}; boundary passthrough is not executable"
            )
            continue
        raw_process, raw_port = source.rsplit("/", maxsplit=1)
        if _local_name(raw_process) not in inner_ids:
            findings.append(
                f"{output_path}: outputSource {source!r} names no step of the subworkflow"
            )
            continue
        endpoints[str(raw_name)] = (
            f"{namespace}{_SUBWORKFLOW_NAMESPACE}{_local_name(raw_process)}/{raw_port}"
        )
    return endpoints, findings


def _inlined_step(
    inner_step: Mapping[str, Any],
    *,
    namespace: str,
    inner_ids: set[str],
    bindings: Mapping[str, str],
    declared_inputs: Iterable[str],
    path: str,
) -> tuple[dict[str, Any], list[str]]:
    """Rewrite one subworkflow step into an equivalent outer-workflow step."""
    findings: list[str] = []
    inner_inputs = inner_step.get("in", {})

    def resolve(source: str) -> str:
        if "/" in source:
            raw_process, raw_port = source.rsplit("/", maxsplit=1)
            local = _local_name(raw_process)
            if local in inner_ids:
                return f"{namespace}{_SUBWORKFLOW_NAMESPACE}{local}/{raw_port}"
            return source
        return bindings.get(source, source)

    if isinstance(inner_inputs, Mapping):
        for raw_name, raw_source in inner_inputs.items():
            try:
                sources = _source_values(raw_source, context=f"{path}.in.{raw_name}")
            except ValueError:
                # Every unrecognized binding shape is reported by field analysis.
                continue
            for source in sources:
                if "/" in source:
                    if _local_name(source.rsplit("/", maxsplit=1)[0]) not in inner_ids:
                        findings.append(
                            f"{path}.in.{raw_name}: {source!r} names no step of the subworkflow"
                        )
                elif source not in bindings and source not in set(declared_inputs):
                    # A declared but unbound input is reported once, by the
                    # binding-totality check.
                    findings.append(
                        f"{path}.in.{raw_name}: {source!r} is not a subworkflow input"
                    )
    rewritten = dict(inner_step)
    match inner_step.get("id"):
        case str() as raw_id:
            rewritten["id"] = f"{namespace}{_SUBWORKFLOW_NAMESPACE}{_local_name(raw_id)}"
        case _:
            findings.append(f"{path}.id: compiled workflow step id must be a string")
    if isinstance(inner_inputs, Mapping):
        rewritten["in"] = {
            raw_name: _map_sources(raw_source, resolve)
            for raw_name, raw_source in inner_inputs.items()
        }
    return rewritten, findings


def _composition_findings_for_step(
    step: Mapping[str, Any],
    *,
    path: str,
) -> list[str]:
    """Reject the outer-step fields a subworkflow step has no lowering for."""
    findings = _unconsumed_field_findings(step, consumed=_STEP_CONSUMED_FIELDS, path=path)
    if "when" in step:
        findings.append(
            f"{path}.when: CWL step when conditions are not supported in Nextflow Phase 1"
        )
    if "scatter" in step:
        findings.append(
            f"{path}.scatter: scatter on a nested workflow step is deferred beyond this "
            "lowering; scattering an inlined sub-DAG is not the single-process shape "
            "scatter supports"
        )
    elif "scatterMethod" in step:
        findings.append(
            f"{path}.scatterMethod: scatterMethod without scatter is not executable"
        )
    return findings


def _flatten_subworkflows(
    workflow: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    sub_trees: list[Any],
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], list[Any], list[str]]:
    """Inline every one-level subworkflow step into the outer workflow.

    Returns the rewritten workflow document, steps, and children, plus every
    composition finding. The rewritten values are meaningful only when no
    finding is reported: the flat graph the remaining passes analyze cannot
    be built while its composition is unsupported.
    """
    findings: list[str] = []
    flat_steps: list[Mapping[str, Any]] = []
    flat_children: list[Any] = []
    substitutions: dict[str, str] = {}
    for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True)):
        path = f"steps[{step_index}]"
        nested = _subworkflow_document(child)
        if nested is None:
            flat_steps.append(step)
            flat_children.append(child)
            continue
        document, inner_children = nested
        findings.extend(_composition_findings_for_step(step, path=path))
        findings.extend(
            _unconsumed_field_findings(
                document,
                consumed=_WORKFLOW_CONSUMED_FIELDS,
                path=f"{path}.run",
            )
        )
        findings.extend(_workflow_requirement_findings(document, path=f"{path}.run"))
        match step.get("id"):
            case str() as raw_id:
                namespace = _local_name(raw_id)
            case _:
                findings.append(f"{path}.id: compiled workflow step id must be a string")
                continue
        inputs = document.get("inputs", {})
        if not isinstance(inputs, Mapping):
            findings.append(f"{path}.run.inputs: compiled CWL Workflow inputs must be a mapping")
            continue
        bindings, binding_findings = _subworkflow_bindings(step, inputs, path=path)
        findings.extend(binding_findings)
        try:
            inner_steps = _workflow_steps(document, child_count=len(inner_children))
        except ValueError as exc:
            findings.append(f"{path}.run.steps: {exc}")
            continue
        inner_ids = {
            _local_name(inner_step["id"])
            for inner_step in inner_steps
            if isinstance(inner_step.get("id"), str)
        }
        endpoints, endpoint_findings = _subworkflow_output_endpoints(
            document,
            inner_ids,
            namespace=namespace,
            path=path,
        )
        findings.extend(endpoint_findings)
        findings.extend(_exported_output_findings(step, endpoints, path=path))
        for name, endpoint in endpoints.items():
            for spelling in (raw_id, namespace):
                substitutions[f"{spelling}/{name}"] = endpoint
        for inner_index, (inner_step, inner_child) in enumerate(
            zip(inner_steps, inner_children, strict=True)
        ):
            inner_path = f"{path}.run.steps[{inner_index}]"
            if _subworkflow_document(inner_child) is not None:
                findings.append(
                    f"{inner_path}.run: nested workflows deeper than one level are "
                    "deferred beyond this lowering"
                )
                continue
            rewritten, inner_findings = _inlined_step(
                inner_step,
                namespace=namespace,
                inner_ids=inner_ids,
                bindings=bindings,
                declared_inputs=inputs,
                path=inner_path,
            )
            findings.extend(inner_findings)
            flat_steps.append(rewritten)
            flat_children.append(inner_child)
    if not substitutions:
        return workflow, flat_steps, flat_children, findings

    def substitute(source: str) -> str:
        return substitutions.get(source, source)

    rewritten_steps: list[Mapping[str, Any]] = []
    for step in flat_steps:
        step_inputs = step.get("in")
        if not isinstance(step_inputs, Mapping):
            rewritten_steps.append(step)
            continue
        rewritten_steps.append({
            **step,
            "in": {
                raw_name: _map_sources(raw_source, substitute)
                for raw_name, raw_source in step_inputs.items()
            },
        })
    rewritten_workflow = dict(workflow)
    rewritten_workflow["steps"] = rewritten_steps
    outputs = workflow.get("outputs")
    if isinstance(outputs, Mapping):
        rewritten_workflow["outputs"] = {
            raw_name: (
                {**definition, "outputSource": _map_sources(definition["outputSource"], substitute)}
                if isinstance(definition, Mapping) and "outputSource" in definition
                else definition
            )
            for raw_name, definition in outputs.items()
        }
    return rewritten_workflow, rewritten_steps, flat_children, findings


def _exported_output_findings(
    step: Mapping[str, Any],
    endpoints: Mapping[str, str],
    *,
    path: str,
) -> list[str]:
    """Require every name in a subworkflow step's out to be a declared output."""
    match step.get("out"):
        case list() as exported:
            pass
        case None:
            return []
        case _:
            return [f"{path}.out: compiled step out must be a list"]
    return [
        f"{path}.out: the subworkflow declares no output named {_local_name(name)!r}"
        for name in exported
        if isinstance(name, str) and _local_name(name) not in endpoints
    ]


_SCATTER_METHODS = frozenset({"dotproduct", "flat_crossproduct", "nested_crossproduct"})


def _scatter_names(step: Mapping[str, Any]) -> list[str]:
    """Return the raw input names one step scatters over.

    Only the single-input forms are representable: a bare name, or a
    one-element list. Multi-input scatter is the only shape where
    scatterMethod is load-bearing, and it is deferred rather than guessed.
    """
    match step.get("scatter"):
        case str() as name if name:
            names = [name]
        case list() as items if items and all(isinstance(item, str) and item for item in items):
            names = list(items)
        case _:
            raise ValueError(
                "scatter must name one input, as a string or a one-element list"
            )
    if len(names) > 1:
        raise ValueError(
            f"multi-input scatter over {len(names)} inputs is deferred beyond this lowering; "
            "exactly one scattered input is supported"
        )
    return names


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
            f"{path}: a scattered input must be sourced from an array-typed workflow "
            f"input; {source!r} declares {declared!r}"
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
    sub_trees: list[Any],
) -> list[str]:
    """Validate every scattered step against the single-input scatter lowering."""
    findings: list[str] = []
    source_types = _source_types(workflow, steps, sub_trees)
    for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True)):
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
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                tool_inputs = tool.get("inputs", {})
            case _:
                continue
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
                    f"{path}.scatter: scattered input {raw_name!r} has no source; a "
                    "scattered input must be wired to an array-typed workflow input"
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
                    findings.append(
                        f"steps[{step_index}].in.{raw_name}: {source!r} is an output of "
                        f"scattered step steps[{producer}]; a scattered step's outputs can "
                        "only reach a workflow output, because gathering them back into one "
                        "value is deferred beyond this lowering"
                    )
                elif step_index in scattered:
                    findings.append(
                        f"steps[{step_index}].in.{raw_name}: a scattered step's inputs must "
                        f"come from workflow inputs; the process output {source!r} would "
                        "truncate the scatter to one task"
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

    A name is safe when it is never dereferenced: not referenced by any
    command token, stream target, or output glob, or referenced solely as
    the boolean-flag token it drives (a flag tests its value, never calls
    ``.toString()`` on it). Returns None when the tool's command or outputs
    cannot be analyzed — absence is then never treated as safe.
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
    unsafe = plain | basenamed
    try:
        all_names = {
            _identifier(raw_name, context="input reference")
            for raw_name in _as_mapping(tool.get("inputs", {}), error="")
        }
    except ValueError:
        return None
    return all_names - unsafe


def _absent_optional_findings(
    workflow: Mapping[str, Any],
    node_data: NodeData,
    steps: list[Mapping[str, Any]],
    sub_trees: list[Any],
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
    params = _workflow_params(workflow, node_data)
    findings: list[str] = []
    for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True)):
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                pass
            case _:
                continue
        tool_inputs = tool.get("inputs", {})
        step_inputs = step.get("in", {})
        if not isinstance(tool_inputs, Mapping) or not isinstance(step_inputs, Mapping):
            continue
        safe_names = _safe_absence_names(tool)
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
                if safe_names is not None and qualifier == "val" and name in safe_names:
                    continue
                detail = (
                    "absent optional values are supported only for a val input that is "
                    "unreferenced in its command, or drives a boolean flag and is "
                    "referenced nowhere else"
                )
            else:
                detail = "resolves to an absent required value"
            findings.append(f"steps[{step_index}].run.inputs.{raw_name}: {detail}")
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
    sub_trees: list[Any],
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
    for step, child in zip(steps, sub_trees, strict=True):
        match step.get("id"), child:
            case str() as raw_id, RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
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
    sub_trees: list[Any],
) -> list[str]:
    """Require a boolean source for every input lowered to a flag token.

    A flag renders as a Groovy ternary over its channel value, so a
    non-boolean source would let Groovy truthiness decide the flag: a staged
    path is always truthy and the strings ``"false"`` and ``"0"`` are too.
    """
    source_types = _source_types(workflow, steps, sub_trees)
    scattered_by_index = _scattered_names_by_index(steps)
    findings: list[str] = []
    for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True)):
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                pass
            case _:
                continue
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


def _raise_capability_findings(findings: list[str]) -> None:
    if findings:
        details = "\n".join(f"- {finding}" for finding in findings)
        raise ValueError(f"Nextflow Phase 1 capability analysis failed:\n{details}")


def _process(step: Mapping[str, Any], child: RoseTree) -> NfProcess:
    match child:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            raise TypeError("each compiled workflow step must have a RoseTree[NodeData] child")
    if "when" in step:
        raise ValueError("CWL step when conditions are not supported in Nextflow Phase 1")
    if sub_trees:
        raise ValueError("nested workflows are deferred to Phase 2")
    match node_data.compiled_cwl:
        case Mapping() as tool:
            pass
        case _:
            raise ValueError("compiled CommandLineTool must be a mapping")
    match tool.get("class"):
        case "Workflow":
            raise ValueError("nested workflows are deferred to Phase 2")
        case "CommandLineTool":
            pass
        case unsupported_class:
            raise ValueError(f"unsupported compiled step class {unsupported_class!r}")
    return NfProcess(
        name=_identifier(step.get("id"), context="workflow step id"),
        inputs=_ports(tool.get("inputs", {}), outputs=False, stage_as=_iwdr_stage_as(tool)),
        outputs=_ports(tool.get("outputs", {}), outputs=True),
        command=_command(tool),
        container=_container(tool),
        resources=_resources(tool),
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
        for raw_port, raw_source in raw_inputs.items():
            destination_port = _identifier(raw_port, context="process input destination")
            for source in _source_values(raw_source, context=f"step input {process.name}.{destination_port}"):
                source_process, source_port = _source_endpoint(source, step_names)
                if source_process is None:
                    connections.append(
                        NfWorkflowInputConnection(
                            source_port,
                            process.name,
                            destination_port,
                            "scatter" if destination_port in scattered else None,
                        )
                    )
                else:
                    connections.append(
                        NfProcessConnection(
                            source_process,
                            source_port,
                            process.name,
                            destination_port,
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
                            "boundary passthrough is not executable in Nextflow Phase 1"
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
    sub_trees: list[Any],
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
    for step, child, process in zip(steps, sub_trees, processes, strict=True):
        bound = _as_mapping(step.get("in", {}), error="compiled step inputs must be a mapping")
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                inputs = _as_mapping(tool.get("inputs", {}), error="tool inputs must be a mapping")
            case _:
                continue
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


def _compiled_workflow(rose_tree: RoseTree) -> tuple[NodeData, list[Any], Mapping[str, Any]]:
    match rose_tree:
        case RoseTree(data=NodeData() as node_data, sub_trees=sub_trees):
            pass
        case _:
            raise TypeError("cwl_rosetree_to_nextflow requires a RoseTree[NodeData]")
    match node_data.compiled_cwl:
        case Mapping() as workflow if workflow.get("class") == "Workflow":
            pass
        case _:
            raise ValueError("RoseTree root must contain a compiled CWL Workflow")
    return node_data, sub_trees, workflow


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
        raise ValueError("compiled CWL steps do not match RoseTree children")
    return steps


def _workflow_params(workflow: Mapping[str, Any], node_data: NodeData) -> dict[str, Any]:
    workflow_inputs = _as_mapping(
        workflow.get("inputs", {}),
        error="compiled CWL Workflow inputs must be a mapping",
    )
    provided_params = _as_mapping(
        node_data.workflow_inputs_file,
        error="compiled workflow input values must be a mapping",
    )
    params: dict[str, Any] = {}
    normalized_names = _normalized_identifiers(workflow_inputs, context="workflow input")
    missing = object()
    for name, definition in workflow_inputs.items():
        value = provided_params.get(name, missing)
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


def _container_policy_findings(sub_trees: list[Any]) -> list[str]:
    """Require one workflow-wide host or container execution policy."""
    containerized: list[int] = []
    host: list[int] = []
    for step_index, child in enumerate(sub_trees):
        match child:
            case RoseTree(data=NodeData(compiled_cwl=Mapping() as tool)):
                requirement_names = [
                    class_name
                    for section_name in ("requirements", "hints")
                    for class_name, _suffix in _requirement_names(tool.get(section_name))
                ]
                target = containerized if "DockerRequirement" in requirement_names else host
                target.append(step_index)
            case _:
                continue
    if not containerized or not host:
        return []
    return [
        "workflow.steps: mixed container execution is not supported; "
        "DockerRequirement must be declared by every process or by none "
        f"(containerized steps: {containerized}; host steps: {host})"
    ]


def cwl_rosetree_to_nextflow(rose_tree: RoseTree) -> ExecutableNextflowWorkflow:
    """Convert a compiled flat CWL RoseTree without invoking inference again.

    Runs closed-world capability analysis first; every unsupported source
    semantic is aggregated and rejected before any lowering happens.

    Args:
        rose_tree (RoseTree): Compiler output whose root holds a compiled
            CWL ``Workflow`` and whose children hold the compiled tools.

    Raises:
        TypeError: If the argument is not a ``RoseTree[NodeData]``.
        ValueError: If capability analysis finds unsupported semantics, or
            lowering/validation rejects the workflow.

    Returns:
        ExecutableNextflowWorkflow: The validated executable representation.
    """
    node_data, sub_trees, workflow = _compiled_workflow(rose_tree)
    steps = _workflow_steps(workflow, child_count=len(sub_trees))
    # Composition is resolved first: every remaining pass analyzes the flat
    # graph, which cannot be built while its composition is unsupported.
    workflow, steps, sub_trees, composition_findings = _flatten_subworkflows(
        workflow, steps, sub_trees
    )
    _raise_capability_findings(composition_findings)
    findings = [
        finding
        for step_index, (step, child) in enumerate(zip(steps, sub_trees, strict=True))
        for finding in _tool_capability_findings(step, child, step_index=step_index)
    ]
    findings.extend(_workflow_capability_findings(workflow, node_data, steps))
    findings.extend(_absent_optional_findings(workflow, node_data, steps, sub_trees))
    findings.extend(_flag_source_findings(workflow, steps, sub_trees))
    findings.extend(_scatter_findings(workflow, steps, sub_trees))
    findings.extend(_container_policy_findings(sub_trees))
    _raise_capability_findings(findings)
    processes = [
        _process(step, child)
        for step, child in zip(steps, sub_trees, strict=True)
    ]
    default_connections, default_params = _default_bindings(steps, sub_trees, processes)
    params = _workflow_params(workflow, node_data)
    if collisions := sorted(set(params) & set(default_params)):
        raise ValueError(
            "lowered workflow parameter names collide: "
            f"{', '.join(collisions)}"
        )
    params.update(default_params)
    return ExecutableNextflowWorkflow(
        name=_identifier(node_data.name, context="workflow name"),
        processes=processes,
        connections=[*_connections(workflow, list(steps), processes), *default_connections],
        params=_apply_absent_sentinel(params),
    )
