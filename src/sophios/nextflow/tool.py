"""Lower individual tools, read as nodes, into Nextflow process pieces and tool-level capability findings."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
from os import PathLike
import re
from types import MappingProxyType
from typing import Any, TypeGuard

from ..ir.resolved import Ref, ResolvedStep
from ..lang.diagnostics import Diagnostic, Locator, Severity
from ..lang.error_codes import SophiosErrorCode
from ..lang.needs import Inherited
from ..lang.nodes import OpaqueCwl
from ..lang.subset import EXPRESSION
from ..lang.tools import (
    Accelerator, BodyKind, Capture, Collect, Container, Dirent, Generated, Need, NeedKind, Network, Part, Placement,
    Reference, Resources, Staging, Stream, ToolBody, ToolInput, ToolNode, ToolOutput, Variable, Variables, Word,
)
from ..lang.types import PortDeclaration, required, spell
from ..lang.values import FileKind, FileValue, Location
from .model import (
    NF_INTERNAL_IDENTIFIERS, NfArrayBinding, NfBasenameReference, NfCommand, NfCommandToken, NfEntry, NfEnvVar, NfFile,
    NfFlag, NfInputReference, NfLiteral, NfOptionalBinding, NfPort, NfResources, NfShellLiteral, NfTemplate,
    NfTemplateSegment, PATH_KINDS, blank_prefix, entryname_problem, environment_name_problem, file_name_problem,
    inputs_read, lacks_prefix, link_refusal, linked_reference, literal_problem, template_segments, wildcard_problem,
)
from .support import FIELDS, REQUIREMENT_CLASSES, Row, consumed
from .symbols import normalize_nextflow_identifier


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
    return computed if isinstance(value, str) and EXPRESSION.search(value) else SophiosErrorCode.NF_INVALID_CWL


def _refuse_separate_without_prefix(prefix: str | None, separate: bool | None) -> None:
    if lacks_prefix(prefix, separate):
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CWL separate cannot be specified without a prefix")


def _prefix(raw_name: Any, binding: Placement) -> str | None:
    """A binding's prefix, or None for none: text that is not empty or blank, which cwltool skips."""
    if binding.prefix is not None and blank_prefix(binding.prefix):
        raise _Unsupported(SophiosErrorCode.NF_EMPTY_PREFIX,
                           f"CWL command prefix for {raw_name!r} must be a non-empty string")
    _refuse_separate_without_prefix(binding.prefix, binding.separate)
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


def _is_array_type(cwl_type: Any) -> TypeGuard[Mapping[str, OpaqueCwl]]:
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
    if items == "array" or _is_array_type(items):
        raise _Unsupported(SophiosErrorCode.NF_TYPE, "nested arrays are deferred beyond this lowering")
    if not isinstance(items, Mapping):
        return items
    if "inputBinding" in items:
        raise _Unsupported(SophiosErrorCode.NF_ARRAY_BINDING,
                           "per-item array element bindings are deferred beyond this lowering")
    extra = set(items) - {"type"}
    if extra:
        raise _Unsupported(SophiosErrorCode.NF_TYPE, f"unsupported array items fields: {', '.join(sorted(extra))}")
    return items.get("type")


def _ports(
    ports: Mapping[str, Any],
    *,
    outputs: bool,
    entries: Mapping[str, NfEntry] | None = None,
    body: ToolBody | None = None,
) -> list[NfPort]:
    lowered: list[NfPort] = []
    for raw_name, port in ports.items():
        name = _identifier(raw_name)
        cwl_type = spell(port.type)
        inner = required(cwl_type)[0]
        is_array = not outputs and _is_array_type(inner)
        element_type = _array_item_type(inner) if is_array else cwl_type
        qualifier = cwl_type_to_nf_qualifier(element_type)
        lowered.append(
            NfPort(
                name,
                qualifier,
                emit=name if outputs else None,
                glob=_output_template(raw_name, port, body) if outputs else None,
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


def _literal(text: str) -> NfLiteral:
    """`text` as literal command data, which no NUL byte can be."""
    if (problem := literal_problem(text)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, problem)
    return NfLiteral(text)


def _word_template(word: Word) -> NfTemplate:
    """A word the front door read, as a template: its references are the tool's inputs."""
    return NfTemplate(tuple(
        _literal(part) if isinstance(part, str)
        else NfBasenameReference(_identifier(part.name)) if part.part is Part.BASENAME
        else NfInputReference(_identifier(part.name)) for part in word.parts))


def _value_from(binding: Placement) -> Word | str | None:
    """A binding's `valueFrom`: the word the front door read, or else the text no word holds, which `_template`
    refuses as it does any text it cannot lower."""
    return binding.value_from if binding.value_from is not None else binding.unread_value


def _template(text: str | Word) -> NfTemplate:
    if isinstance(text, Word):
        return _word_template(text)
    _literal(text)  # refuses what no literal can be
    interpolated = EXPRESSION.search(text)  # cwltool interpolates only these, and strips them
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
    if interpolated and EXPRESSION.search(_INTERPOLATION.sub(" ", text)):
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


def _literal_text(value: Word | str | list[str] | tuple[str, ...] | None) -> str | None:
    """The text of a value that lowers to a template with no input reference, or None: a glob may be a list."""
    if not isinstance(value, (Word, str)):
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
    _refuse_separate_without_prefix(prefix, separate)
    match prefix:
        case None:
            return (value,)
        case str() as text:
            prefix_template = NfTemplate((_literal(text),))
            if separate is not False:
                return prefix_template, value
            return (NfTemplate((*prefix_template.segments, *value.segments)),)


def _shell_literal_value(
    raw_name: Any,
    binding: Placement,
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
    _refuse_separate_without_prefix(binding.prefix, binding.separate)
    value_from = _value_from(binding)
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


def _boolean_value_from(value_from: Word | str | None, tool: "_Tool") -> str | None:
    """The boolean input a valueFrom restates whole as ``$(inputs.<name>)``, else None."""
    match value_from:
        case Word(parts=("", Reference(name=name, part=Part.VALUE), "")):
            pass
        case str() if (found := _INPUT_EXPRESSION.fullmatch(value_from.strip())) and not found["suffix"]:
            name = found["name"]
        case _:
            return None
    port = tool.inputs.get(name)
    if port is not None and required(spell(port.type))[0] == "boolean":
        return _identifier(name)
    return None


def _flag_tokens(name: str, prefix: Any) -> tuple[NfCommandToken, ...]:
    """CWL's boolean binding: the prefix when true, nothing when false or without a prefix."""
    return () if prefix is None else (NfFlag(name, prefix),)


def _argument_items(
    arguments: Iterable[str | Placement],
    *,
    shell_mode: bool,
    tool: "_Tool",
) -> list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]]:
    items: list[tuple[tuple[int, int, int], tuple[NfCommandToken, ...]]] = []
    for index, argument in enumerate(arguments):
        if isinstance(argument, Placement):
            value_from = _value_from(argument)
            if value_from is None:
                raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "mapped CWL arguments must contain valueFrom")
            item_position = _position(argument.position, default=0)
            if argument.shell_quote is False:
                tokens: tuple[NfCommandToken, ...] = (
                    _shell_literal_value(f"arguments[{index}]", argument, shell_mode=shell_mode),
                )
            elif flag := _boolean_value_from(value_from, tool):
                tokens = _flag_tokens(flag, argument.prefix)
            else:
                tokens = _binding_tokens(argument.prefix, _template(value_from), separate=argument.separate)
        else:
            tokens = (_template(argument),)
            item_position = 0
        items.append(((item_position, 0, index), tokens))
    return items


def _boolean_flag_reference(raw_name: Any, name: str, value_from: Word | str) -> None:
    """Validate that a boolean binding's valueFrom is a bare self-reference.

    CWL evaluates valueFrom and then applies boolean flag semantics to the
    result. The only valueFrom shape provably boolean without a JS evaluator
    is a bare $(inputs.<name>) reference restating the binding's own input,
    so that is the sole supported form; it renders identically to the
    no-valueFrom case.
    """
    if _template(value_from).segments != (NfInputReference(name),):
        raise _Unsupported(
            SophiosErrorCode.NF_BOOLEAN_BINDING, "valueFrom on a boolean inputBinding is supported only as "
            f"$(inputs.{raw_name}), restating the input's own value; "
            f"{value_from.spelled if isinstance(value_from, Word) else value_from!r} does not have that shape"
        )


def _array_binding(
    raw_name: Any,
    name: str,
    required_type: Mapping[str, Any],
    binding: Placement,
) -> NfArrayBinding:
    """Lower an array-typed inputBinding to its one supported shape.

    No itemSeparator, no valueFrom, and separate is not false: the array's
    optional prefix is contributed once and each element becomes its own
    argv word, or nothing at all when the array is empty.
    """
    if _value_from(binding) is not None:
        raise _Unsupported(
            SophiosErrorCode.NF_ARRAY_BINDING, f"CWL valueFrom on an array-typed inputBinding for {raw_name!r} is "
            "deferred beyond this lowering"
        )
    if binding.item_separator is not None:
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


def _iwdr_entry_reference(value: str) -> str:
    """Return the raw input name referenced by a bare $(inputs.<name>) IWDR entry."""
    if (match := _INPUT_EXPRESSION.fullmatch(value.strip())) and not match["suffix"]:
        return str(match["name"])
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
        case Dirent() as dirent:
            writable = bool(dirent.writable)
            raw_name = _iwdr_entry_reference(dirent.entry)
            rename = _iwdr_entryname(dirent.entryname, raw_name=raw_name)
        case _:
            raise _Unsupported(SophiosErrorCode.NF_STAGING,
                               "must be a bare $(inputs.<name>) reference or a Dirent mapping")
    port = tool_inputs.get(raw_name)
    if port is None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, f"references undeclared input {raw_name!r}")
    inner = required(spell(port.type))[0]
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


def _generated_file(item: Generated) -> NfFile:
    """The file a staged entry of literal text makes: its name and its text, which no NUL byte can be."""
    if (problem := file_name_problem(item.name)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, problem)
    if (problem := literal_problem(item.text)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, problem)
    return NfFile(item.name, item.text)


def _iwdr_listing_findings(
    tool: "_Tool",
    requirement: Staging,
    *,
    path: str,
) -> list[_Finding]:
    """Validate every InitialWorkDirRequirement listing entry independently."""
    listing = requirement.entries
    if listing is None:
        return [_Finding(SophiosErrorCode.NF_STAGING,
                         f"{path}.listing", "InitialWorkDirRequirement listing must be a list")]
    findings: list[_Finding] = []
    renamed_by: dict[str, set[str]] = {}
    listed_at: dict[str, int] = {}
    made_at: dict[str, int] = {}
    for index, item in enumerate(listing):
        if isinstance(item, Dirent) and (
            dirent_findings := _unconsumed_field_findings(item.unread, "Dirent", path=f"{path}.listing[{index}]")
        ):
            findings.extend(dirent_findings)
            continue
        if isinstance(item, Generated):
            try:
                made = _generated_file(item)
            except _Unsupported as exc:
                findings.append(_Finding(exc.code, f"{path}.listing[{index}]", str(exc)))
                continue
            if made.name in made_at:
                findings.append(
                    _Finding(SophiosErrorCode.NF_STAGED_NAMES,
                             f"{path}.listing[{index}]", f"file {made.name!r} appears more than once "
                             f"(first listed at listing[{made_at[made.name]}])")
                )
                continue
            made_at[made.name] = index
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


def _staged_entries(tool: "_Tool") -> tuple[Any, ...]:
    """The entries an InitialWorkDirRequirement lists, none for no requirement."""
    requirement = tool.requirement(NeedKind.STAGING)
    if requirement is None:
        return ()
    assert isinstance(requirement.body, Staging)
    if requirement.body.entries is None:
        raise _Unsupported(SophiosErrorCode.NF_STAGING, "InitialWorkDirRequirement listing must be a list")
    return requirement.body.entries


def _generated_files(tool: "_Tool") -> tuple[NfFile, ...]:
    """The files an InitialWorkDirRequirement listing makes of literal text; the task writes each one."""
    return tuple(_generated_file(item) for item in _staged_entries(tool) if isinstance(item, Generated))


def _iwdr_entries(tool: "_Tool") -> dict[str, NfEntry]:
    """Each input an InitialWorkDirRequirement listing names, with its entry; the task makes every one."""
    entries: dict[str, NfEntry] = {}
    for item in _staged_entries(tool):
        if isinstance(item, Generated):
            continue
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
        binding = port.placement
        if binding is None:
            continue
        name = _identifier(raw_name)
        value_from = _value_from(binding)
        position = _position(binding.position, default=0)
        inner, optional = required(spell(port.type))
        if binding.shell_quote is False:
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
            tokens = _flag_tokens(name, _prefix(raw_name, binding))
        elif flag := _boolean_value_from(value_from, tool):
            if optional:
                # CWL omits the whole binding for an absent input; a flag renders whatever it is.
                raise _Unsupported(SophiosErrorCode.NF_BOOLEAN_BINDING,
                                   f"a boolean valueFrom on optional input {raw_name!r} is not supported")
            tokens = _flag_tokens(flag, binding.prefix)
        elif value_from is None and optional and inner in _OPTIONAL_BINDING_TYPES:
            # CWL adds nothing for a null input; the [] sentinel stands for one.
            tokens = (NfOptionalBinding(name, _prefix(raw_name, binding), binding.separate is not False),)
        else:
            value = _template(value_from) if value_from is not None else NfTemplate((NfInputReference(name),))
            tokens = _binding_tokens(binding.prefix, value, separate=binding.separate)
        items.append(((position, 1, str(raw_name)), tokens))
    return items


def _command_items(tool: "_Tool") -> tuple[NfCommandToken, ...]:
    shell_mode = tool.requirement(NeedKind.SHELL) is not None
    ordered = sorted([
        *_argument_items(tool.body.arguments, shell_mode=shell_mode, tool=tool),
        *_input_binding_items(tool, shell_mode=shell_mode),
    ])
    return tuple(token for _key, tokens in ordered for token in tokens)


def _command(tool: "_Tool") -> NfCommand:
    tokens: list[NfCommandToken] = []
    match tool.body.base:
        case "":
            raise _Unsupported(SophiosErrorCode.NF_INVALID_CWL, "CommandLineTool baseCommand must not be empty")
        case str() as command:
            tokens.append(NfTemplate((_literal(command),)))
        case tuple() as command:
            tokens.extend(NfTemplate((_literal(token),)) for token in command)

    tokens.extend(_command_items(tool))
    if not any(isinstance(token, (NfTemplate, NfShellLiteral)) for token in tokens):
        if tool.requirement(NeedKind.CONTAINER) is not None:
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

    body = tool.body
    return NfCommand(tuple(tokens), stream(body.stdin), stream(_stream_file(body, Stream.STDOUT)),
                     stream(_stream_file(body, Stream.STDERR)), _environment(tool))


def _env_var(entry: Variable, tool: "_Tool") -> NfEnvVar:
    """Lower one `envDef` entry: literal text, or exactly `$(inputs.<name>)` of a `string` input (design §6)."""
    name, value = entry.name, entry.value
    if (problem := environment_name_problem(name)) is not None:
        raise _Unsupported(SophiosErrorCode.NF_ENVIRONMENT, problem)
    if whole := _INPUT_EXPRESSION.fullmatch(value.strip()):
        port = tool.inputs.get(whole["name"])
        declared = spell(port.type) if port is not None else None
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
    requirement = tool.requirement(NeedKind.ENVIRONMENT)
    if requirement is None:
        return ()
    assert isinstance(requirement.body, Variables)
    return tuple(_env_var(entry, tool) for entry in requirement.body.entries)


def _env_var_findings(tool: "_Tool", requirement: Variables, *, path: str) -> list[_Finding]:
    """Report every `envDef` entry the lowering cannot take, each at its own path."""
    findings: list[_Finding] = []
    for index, entry in enumerate(requirement.entries):
        entry_path = f"{path}.envDef[{index}]"
        findings.extend(_unconsumed_field_findings(entry.unread, "EnvironmentDef", path=entry_path))
        with _reported(findings, entry_path):
            _env_var(entry, tool)
    return findings


def _container(tool: "_Tool") -> str | None:
    docker = tool.requirement(NeedKind.CONTAINER)
    if docker is None:
        return None
    assert isinstance(docker.body, Container)
    image: str | None = docker.body.pull or docker.body.image_id
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
    _CUDA_REQUIREMENT: {"cudaDeviceCountMin": _device_count, "cudaDeviceCountMax": _device_count},
}


def _min_else_max(low: Any, high: Any) -> Any:
    return high if low is None else low


def _resources(tool: "_Tool") -> NfResources:
    need = tool.requirement(NeedKind.RESOURCES)
    resources = need.body if need is not None else None
    assert resources is None or isinstance(resources, Resources)
    cuda = tool.extension(_CUDA_REQUIREMENT)
    gpus = cores = ram = None
    if cuda is not None:
        # cwltool's device count: the minimum, else the maximum, else 1 (design §6, Resources and containers).
        count = _min_else_max(cuda.get("cudaDeviceCountMin"), cuda.get("cudaDeviceCountMax"))
        gpus = 1 if count is None else _device_count(count)
    if resources is not None:
        cores = _min_else_max(resources.cores_min, resources.cores_max)
        ram = _min_else_max(resources.ram_min, resources.ram_max)
        cores = None if cores is None else _cpu_resource(cores)
        ram = None if ram is None else _memory(ram)
    capability = (cuda or {}).get("cudaComputeCapability")
    if not isinstance(capability, str) and capability is not None:
        capability = tuple(capability)
    return NfResources(cores, ram, gpus, (cuda or {}).get("cudaVersionMin"), capability)


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


def _stream_file(body: ToolBody, stream: Stream) -> str | None:
    """The file a command's stream is written to: the one the tool names, else a name of ours when an output is that
    stream, else none."""
    named = body.stdout if stream is Stream.STDOUT else body.stderr
    if named is None and any(each.capture is not None and each.capture.stream is stream for each in body.outputs):
        return f"{stream.value}.txt"
    return named


def _glob(capture: Capture | None) -> Word | str | list[str] | None:
    """An output's glob: the word the front door read, or else the text, the empty text or the list no word holds,
    which `_output_template` refuses as it does any glob it cannot lower."""
    if capture is None:
        return None
    return capture.glob if capture.glob is not None else capture.unread_glob


def _output_template(raw_name: Any, port: ToolOutput, body: ToolBody | None) -> NfTemplate:
    capture = port.capture
    if capture is not None and capture.stream is not None:
        assert body is not None  # the stream's file is the body's
        file = _stream_file(body, capture.stream)
        assert file is not None
        return _template(file)
    glob = _glob(capture)
    match glob:
        case None:
            raise _Unsupported(SophiosErrorCode.NF_GLOB, f"CWL output {raw_name!r} must define outputBinding.glob")
        case Word() | str() if glob:
            return _template(glob)
        case str():
            raise _Unsupported(SophiosErrorCode.NF_GLOB, f"CWL output glob for {raw_name!r} cannot be empty")
        case _:
            raise _Unsupported(
                SophiosErrorCode.NF_GLOB,
                f"CWL output glob lists for {raw_name!r} are not supported by the Nextflow target"
            )


# The capture marker each way an output file is made into a value declares, and the text that says it, which a
# refusal quotes. This table is the single owner of the admitted forms: nothing else constructs a capture marker.
# Adding a form means adding a row here, which requires a design revision first.
_ADMITTED_OUTPUT_EVAL: Mapping[Collect, tuple[str, str]] = MappingProxyType({
    Collect.FIRST: ("single", "$(self[0])"),
    Collect.TEXT: ("text", "$(self[0].contents)"),
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


def _output_capture(raw_name: Any, port: ToolOutput) -> str | None:
    """Return the closed capture marker one CWL ``outputBinding`` declares.

    Recognition is a lookup against the frozen admitted table: exactly
    matching text names its marker and every other text is rejected, so no
    code inspects, transforms, or composes expression text. The paired glob
    is tokenized by the ordinary template tokenizer and must reduce to one
    literal, because a capture declaration projects the first glob match and
    that is provable only where the match set has one member.

    Args:
        raw_name (Any): The CWL output name, for diagnostics.
        port (ToolOutput): The output.

    Raises:
        ValueError: If the binding declares any unadmitted capture shape.
            Callers in capability analysis catch this and report it by path;
            lowering lets it propagate.

    Returns:
        str | None: The capture marker, or None when the binding declares
            no capture behavior at all.
    """
    binding = port.capture
    if binding is None:
        return None
    raw_eval = binding.unread_eval
    if raw_eval is None and binding.output_eval is None:
        if binding.load_contents is not None:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE,
                f"loadContents on output {raw_name!r} has no approved lowering without its "
                "paired outputEval capture declaration"
            )
        return None
    if binding.output_eval is None:
        text = str(raw_eval).strip()
        if reason := _NAMED_OUTPUT_EVAL_REJECTIONS.get(text):
            raise _Unsupported(SophiosErrorCode.NF_OUTPUT_EVAL, f"outputEval {text!r} is not representable: {reason}")
        admitted = ", ".join(repr(text) for _, text in sorted(_ADMITTED_OUTPUT_EVAL.values(), key=lambda row: row[1]))
        raise _Unsupported(
            SophiosErrorCode.NF_OUTPUT_EVAL, f"unsupported outputEval {raw_eval!r}; the approved set is exactly "
            f"{admitted}, recognized as capture declarations, and no other expression is "
            "evaluated"
        )
    capture, text = _ADMITTED_OUTPUT_EVAL[binding.output_eval]
    declared_type = spell(port.type)
    if capture in _LOAD_CONTENTS_CAPTURES:
        if binding.load_contents is not True:
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
        if binding.load_contents is not None:
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} declares output cardinality and is not paired with "
                "loadContents"
            )
        if _qualifier(declared_type) != "path":
            raise _Unsupported(
                SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} declares single-valued File or Directory capture; "
                f"output {raw_name!r} declares {declared_type!r}"
            )
    glob = _glob(binding)
    if not isinstance(glob, (Word, str)) or not glob:
        raise _Unsupported(
            SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} requires a single-literal outputBinding.glob"
        )
    literal = _text(_template(glob))
    if literal is None:
        raise _Unsupported(
            SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r} requires a glob with no input reference; a "
            "reference's runtime value cannot be shown wildcard-free at compile time"
        )
    if problem := wildcard_problem(literal):
        raise _Unsupported(SophiosErrorCode.NF_CAPTURE, f"outputEval {text!r}: {problem}")
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
    if default is None or isinstance(default, (Mapping, list, FileValue)):
        note = "the Nextflow target supports JSON scalar defaults only"
    elif not _phase1_value_matches(cwl_type, default):
        note = "value does not match its supported CWL type"
    else:
        return []
    return [_Finding(SophiosErrorCode.NF_DEFAULT, f"{path}.default", note)]


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
    """One finding, with its row's note, for each field of a `cwl_class` value at `path` that the lowering does not
    consume. `value` is the CWL value or the names of its fields; `kept` is the fields consumed, when they are not
    exactly the class's unrejected rows."""
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
    """Reject a parameter's long or double type, then each field that it writes (`fields`) or that an array schema of
    its type writes (`arrays`) and the lowering does not consume. `cwl_type` is its canonical type."""
    findings = _number_type_findings(cwl_type, path=path)
    if schema_class := _ARRAY_SCHEMAS.get(cwl_class):
        findings.extend(finding for schema in arrays
                        for finding in _unconsumed_field_findings(schema, schema_class, path=f"{path}.type"))
    findings.extend(_unconsumed_field_findings(fields, cwl_class, path=path))
    return findings


def _port_findings(port: ToolInput | ToolOutput, cwl_class: str, *, path: str) -> list[_Finding]:
    """`_parameter_findings` of a tool's port."""
    return _parameter_findings(
        cwl_class, path=path, fields=port.unread, cwl_type=spell(port.type),
        arrays=port.schemas if isinstance(port, ToolInput) else [])


def _declared_findings(declaration: PortDeclaration, cwl_class: str, *, path: str,
                       source: Ref | str | None = None) -> list[_Finding]:
    """`_parameter_findings` of a workflow's port as it states it."""
    cwl_type = spell(declaration.type, declaration.unread)
    return _parameter_findings(
        cwl_class, path=path, fields=_port_fields(declaration, source), cwl_type=cwl_type,
        arrays=[member for member in _members(cwl_type) if _is_array_type(member)])


def local_paths(value: Any) -> Any:
    """`value` with a File or Directory's plain local location as its path, and a File's ``format`` dropped.

    The Python API writes file values by location; for a local path with no URI scheme the two mean the same
    file. A URI keeps its location and stays rejected, since the model does not represent schemes. A File value's
    ``format`` is compile-time, as the input's is (design §3).
    """
    match value:
        case FileValue(kind=FileKind.FILE, has_format=True):
            return local_paths(replace(value, format=None, has_format=False))
        case FileValue(location=Location(text=text, uri=True), companions=(), written=()) if "://" not in text:
            return replace(value, location=Location(text, False))
        case FileValue():
            return replace(value, companions=tuple(local_paths(item) for item in value.companions),
                           written=tuple((key, local_paths(item)) for key, item in value.written))
        case Mapping():
            return {key: local_paths(item) for key, item in value.items()}
        case list():
            return [local_paths(item) for item in value]
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
        case ("File" | "Directory") as expected, FileValue() as found:
            return (found.kind == expected and found.location is not None and not found.location.uri
                    and not found.companions and not found.written)
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
    for each in (need for need in tool.body.needs if need.hint == (section == "hints")):
        requirement_path = f"{path}.{each.place}"
        if not each.understood:
            # The standard lets a hint it cannot read as a class of its own go unread; the lowering cannot lower one.
            findings.append(_Finding(SophiosErrorCode.NF_REQUIREMENT, requirement_path,
                                     "a hint that is not a requirement of the standard is not supported by the "
                                     "Nextflow target"))
            continue
        class_name, values = each.name, _checked_values(each)
        if class_name not in consumed(REQUIREMENT_CLASSES["CommandLineTool"]):
            findings.append(_rejection(REQUIREMENT_CLASSES["CommandLineTool"], class_name, path=requirement_path,
                                       otherwise=(SophiosErrorCode.NF_REQUIREMENT,
                                                  f"{class_name} is not supported by the Nextflow target")))
        elif isinstance(each.body, Network) and each.body.allowed is not True:
            findings.append(
                _Finding(SophiosErrorCode.NF_NETWORK_ACCESS,
                         f"{requirement_path}.networkAccess", "only the literal true is supported: a Nextflow "
                         "task has network access, and the generated pipeline cannot withhold it")
            )
        else:
            findings.extend(_unconsumed_field_findings(each.unread, class_name, path=requirement_path))
            for field_name, check in _REQUIREMENT_VALUES.get(class_name, {}).items():
                if (value := values.get(field_name)) is not None:
                    with _reported(findings, f"{requirement_path}.{field_name}"):
                        check(value)
            if isinstance(each.body, Variables):
                findings.extend(_env_var_findings(tool, each.body, path=requirement_path))
            if isinstance(each.body, Staging):
                findings.extend(_iwdr_listing_findings(tool, each.body, path=requirement_path))
    return findings


def _checked_values(need: Need) -> Mapping[str, Any]:
    """The values of a requirement's fields that a finding checks, by the name the field is written with."""
    match need.body:
        case Resources() as resources:
            return {"coresMin": resources.cores_min, "coresMax": resources.cores_max, "ramMin": resources.ram_min,
                    "ramMax": resources.ram_max}
        case Accelerator() as accelerator:
            return accelerator.fields
    return {}


def _template_positions(tool: "_Tool") -> list[tuple[Any, str]]:
    """Pair every templated tool value with the CWL path it was written at."""
    positions: list[tuple[Any, str]] = []
    for index, argument in enumerate(tool.body.arguments):
        argument_path = f".run.arguments[{index}]"
        if not isinstance(argument, Placement):
            positions.append((argument, argument_path))
        elif _value_from(argument) is not None:
            positions.append((_value_from(argument), f"{argument_path}.valueFrom"))
    for raw_name, port in tool.inputs.items():
        if port.placement is not None and _value_from(port.placement) is not None:
            positions.append((_value_from(port.placement), f".run.inputs.{raw_name}.inputBinding.valueFrom"))
    for stream in ("stdin", "stdout", "stderr"):
        if (value := getattr(tool.body, stream)) is not None:
            positions.append((value, f".run.{stream}"))
    for raw_name, produced in tool.outputs.items():
        if produced.capture is not None and _glob(produced.capture) is not None:
            positions.append((_glob(produced.capture), f".run.outputs.{raw_name}.outputBinding.glob"))
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


def _whole_reference(value: str | Word) -> bool:
    """Whether `value` is one reference to an input and nothing else."""
    if isinstance(value, Word):
        significant = [part for part in value.parts if part != ""]
        return len(significant) == 1 and isinstance(significant[0], Reference)
    return _INPUT_EXPRESSION.fullmatch(value.strip()) is not None


def _references(value: str | Word) -> Iterator[tuple[str, str | None]]:
    """Each reference `value` makes to an input, by its name and what it names of it (`path`, `basename`, or none)."""
    if isinstance(value, Word):
        yield from ((part.name, None if part.part is Part.VALUE else part.part.value)
                    for part in value.parts if isinstance(part, Reference))
    else:
        yield from ((match["name"], match["suffix"]) for match in _INTERPOLATION.finditer(value) if match["name"])


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
        if not isinstance(value, (str, Word)):
            continue  # a list of globs has a finding of its own
        whole = _whole_reference(value)
        kind = next((kind for kind in _STRING_POSITIONS if position.endswith(f".{kind}")), None)
        try:
            linked = linked_reference(_template(value))
        except ValueError:
            linked = None  # an expression, which has a finding of its own
        for name, suffix in dict.fromkeys(_references(value)):
            port, entry = tool.inputs.get(name), entries.get(name)
            if port is None or entry is not None and entry.name is not None:
                continue
            declared = spell(port.type)
            qualifier = _qualifier(declared)
            if qualifier is None:
                continue  # the input pass reports a type the backend does not lower
            if qualifier != "path":
                if suffix is not None:
                    findings.append(
                        _Finding(SophiosErrorCode.NF_PATH_REFERENCE,
                                 position, f"$(inputs.{name}.{suffix}) requires a File or Directory input, "
                                 f"and {name} is declared {declared!r}")
                    )
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
    body = tool.body
    if body.kind is not BodyKind.COMMAND:
        return [_Finding(SophiosErrorCode.NF_NOT_A_COMMAND_LINE_TOOL,
                         ".run.class", f"unsupported compiled step class {body.written_kind!r}")]

    findings.extend(_unconsumed_field_findings(body.unread, "CommandLineTool", path=".run"))
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
    shell_mode_active = tool.requirement(NeedKind.SHELL) is not None
    if shell_mode_active and tool.requirement(NeedKind.CONTAINER) is not None:
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
        cwl_type = spell(port.type)
        findings.extend(_port_findings(port, "CommandInputParameter", path=input_path))
        if port.default is not None:
            findings.extend(_default_value_findings(cwl_type, port.default, path=input_path))
        binding = port.placement
        if binding is None:
            continue
        input_binding_path = f"{input_path}.inputBinding"
        findings.extend(_unconsumed_field_findings(binding.unread, "CommandLineBinding",
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
        if binding.shell_quote is False:
            with _reported(findings, f"{input_binding_path}.shellQuote"):
                _shell_literal_value(raw_name, binding, shell_mode=shell_mode_active)
        elif (value_from := _value_from(binding)) is not None:
            if inner == "boolean":
                with _reported(findings, f"{input_binding_path}.valueFrom"):
                    _boolean_flag_reference(raw_name, _identifier(raw_name), value_from)
            else:
                findings.extend(_computed_value_from_findings(value_from, path=input_binding_path))

    for argument_index, argument in enumerate(body.arguments):
        if not isinstance(argument, Placement):
            continue
        argument_path = f".run.arguments[{argument_index}]"
        findings.extend(_unconsumed_field_findings(argument.unread, "CommandLineBinding", path=argument_path))
        if argument.shell_quote is False:
            with _reported(findings, f"{argument_path}.shellQuote"):
                _shell_literal_value(f"arguments[{argument_index}]", argument, shell_mode=shell_mode_active)
        elif _value_from(argument) is not None:
            findings.extend(_computed_value_from_findings(_value_from(argument), path=argument_path))

    with _reported(findings, ".run.outputs"):
        _normalized_identifiers(tool.outputs, context="tool output")
    for raw_name, output in tool.outputs.items():
        output_path = f".run.outputs.{raw_name}"
        findings.extend(_port_findings(output, "CommandOutputParameter", path=output_path))
        capture = output.capture
        # An outputBinding that declares capture behavior is diagnosed
        # by _output_capture alone, which reports the type it requires;
        # the generic type finding would only restate that less
        # precisely.
        declares_capture = capture is not None and (capture.load_contents is not None
                                                    or capture.output_eval is not None
                                                    or capture.unread_eval is not None)
        if _qualifier(spell(output.type)) != "path" and not declares_capture:
            findings.append(
                _Finding(SophiosErrorCode.NF_OUTPUT_TYPE,
                         f"{output_path}.type", "an output is collected by glob as a File or Directory, "
                         "or captured as text with loadContents and outputEval")
            )
        if capture is None:
            continue
        output_capture_path = f"{output_path}.outputBinding"
        findings.extend(_unconsumed_field_findings(capture.unread, "CommandOutputBinding",
                                                   path=output_capture_path))
        with _reported(findings, output_capture_path):
            _output_capture(raw_name, output)
    return findings


@dataclass(frozen=True, slots=True)
class _Tool:
    """A step's tool as the lowering reads it: the tool's node, its body read once with the requirements and hints it
    inherits written into it, so every one is lowered, or refused, as the same one written on the tool."""

    node: ToolNode
    body: ToolBody
    inputs: Mapping[str, ToolInput]
    #: Only a command line's outputs are read: a tool of another class is refused as a whole.
    outputs: Mapping[str, ToolOutput]

    @classmethod
    def of(cls, node: ToolNode, inherits: Iterable[Inherited] = ()) -> "_Tool":
        """The tool with each requirement in `inherits` written into it, unless the tool names one of that class
        that wins over it.

        Raises:
            SophiosError: `api004`, when the standard's loader refuses the tool or an inherited body.
        """
        body = node.body(tuple(inherits))
        return cls(node, body, {each.name: each for each in body.inputs}, {each.name: each for each in body.outputs})

    @property
    def inherited(self) -> Mapping[str, str]:
        """Each place an inherited requirement or hint is written at, `requirements.<class>` or `hints.<class>`, with
        its class."""
        return dict(self.body.inherited)

    def requirement(self, kind: NeedKind) -> Need | None:
        """The tool's requirement of `kind`, else its hint of it."""
        return self.body.need(kind)

    def extension(self, name: str) -> Mapping[str, Any] | None:
        """The fields of the tool's requirement of a vendor's class, else its hint of it."""
        accelerator = self.body.extension(name)
        return None if accelerator is None else accelerator.fields


def _port_fields(declaration: PortDeclaration, source: Ref | str | None = None) -> set[str]:
    """The fields a workflow port states, with its `outputSource` if it has one: a shorthand port states none."""
    fields = set() if declaration.shorthand else {"type", *(name for name, _value in declaration.passthrough)}
    if declaration.has_format and not declaration.shorthand:
        fields.add("format")
    if declaration.has_default and not declaration.shorthand:
        fields.add("default")
    return {*(fields or {"type"}), "outputSource"} if source is not None else fields


def _referenced_names(tool: _Tool) -> set[str] | None:
    """The input names a plain or basename reference reads in a command template or an output glob.

    None when the tool's command or outputs cannot be lowered; lowering reports why.
    """
    if tool.body.kind is not BodyKind.COMMAND:
        return None
    try:
        command = _command(tool)
        outputs = _ports(tool.outputs, outputs=True, body=tool.body)
    except (ValueError, TypeError):
        return None
    return inputs_read(template_segments(command, outputs), NfInputReference, NfBasenameReference)
