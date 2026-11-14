"""Validated executable intermediate-representation types for Nextflow DSL2."""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from graphlib import CycleError, TopologicalSorter
import json
import math
import posixpath
import re
from types import MappingProxyType
from typing import Any, ClassVar, Final, Generic, TypeVar, get_args

from .lang.cwl import LINK_MERGES, PICK_VALUES, PICKS_ONE, SCATTER_METHODS
from .lang.subset import Expr, canonical, check, references, validate
from .nf_expr import NF_FINITE_HELPER, NF_ROUND_HELPER
from .nf_symbols import NEXTFLOW_SCRIPT_DECLARED_NAMES, validate_nextflow_identifier


_T = TypeVar("_T")
NF_SHELL_QUOTE_HELPER = "__sophios_shell_quote_9f72e"
NF_LOAD_CONTENTS_HELPER = "__sophios_load_contents_9f72e"
NF_SCATTER_INDEX_NAME = "__sophios_scatter_index_9f72e"
NF_NEST_HELPER = "__sophios_nest_9f72e"
NF_INPUT_HELPER = "__sophios_input_9f72e"
NF_STEP_INPUT_HELPER = "__sophios_step_input_9f72e"
NF_FLOAT_TEXT_HELPER = "__sophios_float_text_9f72e"
NF_OUTPUT_HELPER = "__sophios_output_9f72e"
NF_INTERNAL_IDENTIFIERS = frozenset({
    NF_SHELL_QUOTE_HELPER, NF_LOAD_CONTENTS_HELPER, NF_FINITE_HELPER, NF_ROUND_HELPER,
    NF_SCATTER_INDEX_NAME, NF_NEST_HELPER, NF_INPUT_HELPER, NF_STEP_INPUT_HELPER, NF_FLOAT_TEXT_HELPER,
    NF_OUTPUT_HELPER,
})
PATH_KINDS = {"File": "file", "Directory": "directory"}
# A workflow parameter's declared type, which the generated pipeline checks
# its value against when it starts: an item type, `[]` for an array of it,
# and `?` when the parameter is optional.
PARAM_TYPES = frozenset(
    f"{item}{array}{optional}"
    for item in ("boolean", "int", "float", "string", *PATH_KINDS)
    for array in ("", "[]")
    for optional in ("", "?")
)
#: The type a `val` port receives when that is one scalar (design §9): the CWL type, `?` when it may be null.
SCALAR_TYPES = frozenset(f"{item}{optional}" for item in ("boolean", "int", "float", "string")
                         for optional in ("", "?"))
# An InitialWorkDirRequirement entry's input is staged under this prefix and
# its port name, and the task makes the entry from it (design §6).
NF_ENTRY_STAGING_PREFIX = ".sophios_entry_"
# The file a containerized task reads its image's ENTRYPOINT from: under the
# reserved prefix, and not an identifier, so no entry's staging directory and
# no literal entryname can take its name.
NF_ENTRYPOINT_FILE = f"{NF_ENTRY_STAGING_PREFIX}.entrypoint"
# CWL v1.2 requires a loadContents file to be a UTF-8 text file of this many
# bytes or fewer, read entirely, with a fatal error above the limit.
NF_LOAD_CONTENTS_LIMIT = 64 * 1024


def _validate_ir_identifier(value: object, *, field_name: str) -> str:
    identifier = validate_nextflow_identifier(value, field_name=field_name)
    if identifier in NF_INTERNAL_IDENTIFIERS:
        raise ValueError(f"{field_name} collides with a reserved Nextflow backend identifier")
    return identifier


def _validate_ir_declaration(value: object, *, field_name: str) -> str:
    identifier = _validate_ir_identifier(value, field_name=field_name)
    if identifier in NEXTFLOW_SCRIPT_DECLARED_NAMES:
        raise ValueError(f"{field_name} {identifier!r} is already declared by every Nextflow script")
    return identifier


class FrozenMapping(Mapping[str, _T], Generic[_T]):
    """Small immutable and hashable string-keyed mapping."""

    __slots__ = ("_data", "_hash")

    def __init__(self, value: Mapping[str, _T] | None = None) -> None:
        data = dict(value or {})
        self._data = MappingProxyType(data)
        self._hash = hash(tuple(sorted(data.items())))

    def __getitem__(self, key: str) -> _T:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenMapping({dict(self._data)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return False
        return bool(_thaw_json(self) == _thaw_json(other))


def _freeze_json(value: Any) -> Any:
    match value:
        case Mapping() as mapping:
            if not all(isinstance(key, str) for key in mapping):
                raise ValueError("JSON object keys must be strings")
            return FrozenMapping({key: _freeze_json(item) for key, item in mapping.items()})
        case list() | tuple() as items:
            return tuple(_freeze_json(item) for item in items)
        case None | bool() | int() | str():
            return value
        case float() as number if math.isfinite(number):
            return number
        case _:
            raise ValueError("workflow params must contain JSON-compatible values")


def json_value_problem(value: Any) -> str | None:
    """Why `value` cannot be held as a workflow parameter, or None: the parameters are JSON, which has no NaN, no
    infinity and no object key that is not a string."""
    try:
        _freeze_json(value)
    except ValueError as error:
        return str(error)
    return None


def _thaw_json(value: Any) -> Any:
    match value:
        case _Model() | Expr():
            return value.to_dict()
        case Mapping() as mapping:
            return {str(key): _thaw_json(item) for key, item in mapping.items()}
        case list() | tuple() as items:
            return [_thaw_json(item) for item in items]
        case _:
            return value


@dataclass(frozen=True, slots=True)
class _Model:
    """A value of the model, whose JSON form is its fields by name."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: Each field by name. A segment, a command token and a connection state their ``kind``;
                a process's condition and step-only inputs and a process connection's adapter are left out when
                it has none.
        """
        item = {each.name: _thaw_json(getattr(self, each.name)) for each in fields(self)
                if getattr(self, each.name) or (type(self), each.name) not in _LEFT_OUT}
        return {"kind": _KINDS[type(self)], **item} if type(self) in _KINDS else item


def _validate_string_mapping(value: Mapping[str, str], *, field_name: str) -> None:
    for key, item in value.items():
        match key, item:
            case str() as name, str() if name:
                continue
            case str() as name, _ if name:
                raise ValueError(f"{field_name}[{name!r}] must be a string")
            case _:
                raise ValueError(f"{field_name} keys must be non-empty strings")


def literal_problem(text: str) -> str | None:
    """Why `text` cannot be literal data in a command or path, or None: no program takes a NUL byte."""
    return "template literals cannot contain NUL bytes" if "\x00" in text else None


@dataclass(frozen=True, slots=True)
class NfLiteral(_Model):
    """Literal data inside a command or path template."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("template literal must be a string")
        if (problem := literal_problem(self.value)) is not None:
            raise ValueError(problem)


@dataclass(frozen=True, slots=True)
class NfInputReference(_Model):
    """Reference to one process input inside a template."""

    name: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="template input reference")


@dataclass(frozen=True, slots=True)
class NfBasenameReference(_Model):
    """Reference to the staged file name of one path input.

    Nextflow stages an input under its original file name, alone or under a
    directory, so the last part of the staged path is exactly the CWL
    ``basename``: for an entry's input (:class:`NfEntry`), the entry's name.
    The staged path's own ``.name`` is not that part when there is a
    directory: it is the whole staged path.
    """

    name: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="template basename reference")


NfTemplateSegment = NfLiteral | NfInputReference | NfBasenameReference


@dataclass(frozen=True, slots=True)
class NfTemplate(_Model):
    """Canonical immutable sequence of literal and input-reference segments."""

    segments: Sequence[NfTemplateSegment]

    def __post_init__(self) -> None:
        canonical: list[NfTemplateSegment] = []
        for segment in self.segments:
            if not isinstance(segment, get_args(NfTemplateSegment)):
                accepted = ", ".join(kind.__name__ for kind in get_args(NfTemplateSegment))
                raise TypeError(f"template segments must be one of: {accepted}")
            if isinstance(segment, NfLiteral) and canonical and isinstance(canonical[-1], NfLiteral):
                canonical[-1] = NfLiteral(canonical[-1].value + segment.value)
            elif not isinstance(segment, NfLiteral) or segment.value:
                canonical.append(segment)
        if not canonical:
            canonical.append(NfLiteral(""))
        object.__setattr__(self, "segments", tuple(canonical))


@dataclass(frozen=True, slots=True)
class NfFlag(_Model):
    """Conditional command-line flag contributed by a boolean input.

    Renders the prefix as exactly one argv word when the referenced input is
    true, and nothing when it is false. Valid only in command token position.
    """

    name: str
    prefix: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="flag input reference")
        if not isinstance(self.prefix, str) or not self.prefix.strip():
            raise ValueError("flag prefix must be a non-empty string")


@dataclass(frozen=True, slots=True)
class NfArrayBinding(_Model):
    """Command-line binding for a CWL array-typed input with no itemSeparator.

    Renders nothing at all when the referenced array is empty. Otherwise
    contributes the optional prefix once, then each element as its own
    shell-quoted argv word, mirroring CWL's per-item array binding without
    itemSeparator. Valid only in command token position, and only against
    array-marked ports.
    """

    name: str
    prefix: str | None = None

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="array binding input reference")
        if self.prefix is not None and (not isinstance(self.prefix, str) or not self.prefix.strip()):
            raise ValueError("array binding prefix must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class NfOptionalBinding(_Model):
    """Command-line binding of an optional string, int or float input.

    Renders nothing at all when the input holds the ``[]`` sentinel of an
    absent value. Otherwise contributes the optional prefix and the value as
    two argv words, or as one when ``separate`` is false, as the same binding
    of a required input does. Valid only in command token position, and
    only against val ports that are not array-marked.
    """

    name: str
    prefix: str | None = None
    separate: bool = True

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="optional binding input reference")
        if self.prefix is not None and (not isinstance(self.prefix, str) or not self.prefix.strip()):
            raise ValueError("optional binding prefix must be a non-empty string or None")
        if not isinstance(self.separate, bool):
            raise TypeError("optional binding separate must be a bool")
        if not self.separate and self.prefix is None:
            raise ValueError("an optional binding with separate false needs a prefix")


@dataclass(frozen=True, slots=True)
class NfShellLiteral(_Model):
    """Raw, unquoted shell text from an approved ``shellQuote: false`` binding.

    Renders exactly as written, bypassing the generated shell-quoting
    helper. Valid only in command token position, and only for a binding
    whose text is entirely CWL-author literal: no input reference of any
    kind ever reaches this token, so unquoting it never exposes runtime
    data as shell syntax.
    """

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("shell literal text must be a string")
        if "\x00" in self.text:
            raise ValueError("shell literal text cannot contain NUL bytes")


NfCommandToken = NfTemplate | NfFlag | NfArrayBinding | NfOptionalBinding | NfShellLiteral


_SHELL_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def environment_name_problem(name: Any) -> str | None:
    """Why `name` cannot name an environment variable a task exports, or None: it must be a shell variable name."""
    if isinstance(name, str) and _SHELL_VARIABLE.fullmatch(name):
        return None
    return f"environment variable name must be a shell variable name, got {name!r}"


@dataclass(frozen=True, slots=True)
class NfEnvVar(_Model):
    """One environment variable the task exports before its command.

    The value is literal text or one input's value, as an
    ``EnvVarRequirement`` entry lowers (design §6, Resources and containers).
    """

    name: str
    value: NfTemplate

    def __post_init__(self) -> None:
        if (problem := environment_name_problem(self.name)) is not None:
            raise ValueError(problem)
        match self.value:
            case NfTemplate(segments=[NfLiteral() | NfInputReference()]):
                pass
            case _:
                raise ValueError(f"environment variable {self.name!r} must be literal text or one input reference")


@dataclass(frozen=True, slots=True)
class NfCommand(_Model):
    """Typed argv, approved stream redirections, and the variables exported before it, for one process."""

    tokens: Sequence[NfCommandToken]
    stdin: NfTemplate | None = None
    stdout: NfTemplate | None = None
    stderr: NfTemplate | None = None
    env: Sequence[NfEnvVar] = ()

    def __post_init__(self) -> None:
        tokens = tuple(self.tokens)
        if not tokens or not all(
            isinstance(token, get_args(NfCommandToken)) for token in tokens
        ):
            raise ValueError("command must contain at least one typed token")
        for name in ("stdin", "stdout", "stderr"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, NfTemplate):
                raise TypeError(f"command {name} must be an NfTemplate or None")
        env = tuple(self.env)
        if not all(isinstance(variable, NfEnvVar) for variable in env):
            raise TypeError("command env must contain only NfEnvVar values")
        names = [variable.name for variable in env]
        if len(names) != len(set(names)):
            raise ValueError("command env sets one variable more than once")
        object.__setattr__(self, "env", env)
        # Adjacent shell literals render as one text with a space between them, so they are one (design §5.2).
        merged: list[NfCommandToken] = []
        for token in tokens:
            if isinstance(token, NfShellLiteral) and merged and isinstance(merged[-1], NfShellLiteral):
                merged[-1] = NfShellLiteral(f"{merged[-1].text} {token.text}")
            else:
                merged.append(token)
        object.__setattr__(self, "tokens", tuple(merged))

    @property
    def templates(self) -> tuple[NfTemplate, ...]:
        """Every template the command renders a value into: template tokens, redirections and variables."""
        return (
            *(token for token in self.tokens if isinstance(token, NfTemplate)),
            *(stream for stream in (self.stdin, self.stdout, self.stderr) if stream is not None),
            *(variable.value for variable in self.env),
        )


@dataclass(frozen=True, slots=True)
class NfResources(_Model):
    """Typed supported process resources; ``accelerator`` is a GPU count.

    ``cuda_version_min`` and ``cuda_compute_capability`` are a
    ``cwltool:CUDARequirement``'s ``cudaVersionMin`` and
    ``cudaComputeCapability``, as written: the process carries them in its
    ``ext`` directive, which nothing in the pipeline reads (design §4.2,
    Carriers).
    """

    cpus: int | None = None
    memory_mb: int | float | None = None
    accelerator: int | None = None
    cuda_version_min: str | None = None
    cuda_compute_capability: str | tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name in ("cpus", "accelerator"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        memory = self.memory_mb
        if memory is not None and (
            isinstance(memory, bool) or not isinstance(memory, (int, float)) or not 0 < memory < math.inf
        ):
            raise ValueError("memory_mb must be a positive finite number or None")
        if not isinstance(self.cuda_version_min, str | None):
            raise ValueError("cuda_version_min must be a string or None")
        match self.cuda_compute_capability:
            case None | str():
                pass
            case list() | tuple() as items if all(isinstance(item, str) for item in items):
                object.__setattr__(self, "cuda_compute_capability", tuple(items))
            case _:
                raise ValueError("cuda_compute_capability must be a string, a list of strings, or None")


GLOB_WILDCARDS = frozenset("*?[")
# CWL glob metacharacters; a CWL glob has no brace expansion.
_GLOB_METACHARACTERS = frozenset("*?[]")


def _glob_names_one_file(template: NfTemplate) -> bool:
    """True when an assembled output name is meant literally, not as a pattern.

    Nextflow pattern-matches an output ``path`` name, so a name assembled
    from a staged file's own name is matched against whatever metacharacters
    that name happens to carry: a staged ``a[1].txt`` yields the pattern
    ``a[1].txt``, whose ``[1]`` is a character class that cannot match the
    file the process wrote.

    Only a basename reference makes the name literal by construction -- it
    is some real file's name, never a pattern. A plain input reference
    carries arbitrary data, and a glob *is* a legitimate thing to pass as
    an input (``glob: $(inputs.pattern)`` with ``pattern="*.txt")``, so
    turning globbing off there would break a pattern that is meant to
    match. Which reference kind is present is decidable here, at compile
    time, where the assembled value is not; so the rule keys on the kind.

    A metacharacter written into a literal part is the author asking for a
    pattern and is left alone, and an all-literal name is unchanged.
    """
    references = [segment for segment in template.segments if not isinstance(segment, NfLiteral)]
    if not references:
        return False
    if not all(isinstance(segment, NfBasenameReference) for segment in references):
        return False
    return not any(
        isinstance(segment, NfLiteral) and _GLOB_METACHARACTERS & set(segment.value)
        for segment in template.segments
    )


def entryname_problem(name: Any) -> str | None:
    """Why `name` cannot name an InitialWorkDirRequirement entry in a task's directory, or None."""
    if not isinstance(name, str) or not name.strip():
        return "entryname must be a non-empty string"
    if "/" in name or "\x00" in name:
        return "entryname must not contain a path separator or NUL byte"
    if name in {".", ".."}:
        return f"entryname must not be {name!r}, which names a directory, not an entry"
    if name.startswith(NF_ENTRY_STAGING_PREFIX):
        return f"entryname must not begin with {NF_ENTRY_STAGING_PREFIX!r}, which Sophios reserves"
    return None


@dataclass(frozen=True, slots=True)
class NfEntry(_Model):
    """An ``InitialWorkDirRequirement`` entry the task makes from its input before the command runs.

    The input is staged under a hidden name. ``name`` is the entry's literal
    CWL name, or None for the input's own basename; ``copy`` makes the entry a
    copy the command may change, and otherwise it is a symlink (design §6,
    Inputs and channels).
    """

    name: str | None = None
    copy: bool = False

    def __post_init__(self) -> None:
        if self.name is not None and (problem := entryname_problem(self.name)) is not None:
            raise ValueError(problem)
        if not isinstance(self.copy, bool):
            raise TypeError("entry copy must be a bool")


def linked_reference(template: NfTemplate | None) -> str | None:
    """The input whose staged name a path is, or lies inside: a reference to it, alone or before a path.

    The path is normalised as a literal output name is, so ``./`` before the
    reference changes nothing; a literal holds no NUL byte to mark one with.
    """
    if template is None:
        return None
    marks = [segment.value if isinstance(segment, NfLiteral) else f"\x00{index}\x00"
             for index, segment in enumerate(template.segments)]
    names = {mark: segment.name for mark, segment in zip(marks, template.segments, strict=True)
             if not isinstance(segment, NfLiteral)}
    return names.get(posixpath.normpath("".join(marks)).split("/")[0])


def staged_write_problems(process: str, inputs: Sequence["NfPort"], outputs: Sequence["NfPort"],
                          command: "NfCommand") -> list[str]:
    """Each glob, stdout or stderr of a process at or inside the name an input is staged under, which names the link
    the input is staged under, whatever that name is (design §6, Basename references)."""
    problems: list[str] = []
    paths = {port.name: port.entry for port in inputs if port.qualifier == "path"}
    for port in outputs:
        if (name := linked_reference(port.glob)) in paths and (advice := link_refusal(paths[name], writes=False)):
            problems.append(f"process {process!r} output {port.name!r} globs the staged input {name!r}, or a name "
                            f"inside it, which a CWL glob does not match: {advice}")
    for stream in ("stdout", "stderr"):
        name = linked_reference(getattr(command, stream))
        if name in paths and (advice := link_refusal(paths[name], writes=True)):
            problems.append(f"process {process!r} {stream} writes through the link input {name!r} is staged under, "
                            f"into what the input names: {advice}")
    return problems


def link_refusal(entry: NfEntry | None, *, writes: bool) -> str | None:
    """What to write instead of a path at or inside an input's staged name, or None where CWL takes it.

    The task links an input that is not an entry under its name, where
    cwltool stages it outside the output directory, so a CWL glob there
    matches nothing; the task makes an entry, which a glob collects. Writing
    there writes through the link into what the input names, unless the
    entry is a copy (design §6, Basename references).
    """
    if entry is not None and (entry.copy or not writes):
        return None
    return "derive a new name from it, or make the input " + (
        "a writable InitialWorkDirRequirement entry" if writes else "an InitialWorkDirRequirement entry"
    )


@dataclass(frozen=True, slots=True)
class NfPort(_Model):  # pylint: disable=too-many-instance-attributes
    """A typed Nextflow process port.

    ``entry`` marks a path input made an ``InitialWorkDirRequirement`` entry.

    ``optional`` marks a File or Directory output CWL declares optional: no
    match yields null rather than failing the task.

    ``scalar_type`` is the CWL type of what a ``val`` input receives when
    that is one scalar, one of ``SCALAR_TYPES``, else None; a workflow fills
    it from the input's connection, and refuses a value given that disagrees.

    ``capture`` names the approved output-capture declarations an
    ``outputBinding`` may carry. The approved set is closed data: ``"single"``
    declares that the port carries one value rather than a list, and ``"text"``
    declares that the port carries the globbed file's decoded text. The field
    holds a marker from that set and nothing else, so no CWL expression text
    can be smuggled through it.
    """

    # Phase 1 lowers only these qualifiers; the renderer is total over them.
    ALLOWED_QUALIFIERS: ClassVar[frozenset[str]] = frozenset({"path", "val"})
    # The qualifier each capture marker requires of the port declaring it.
    CAPTURE_QUALIFIERS: ClassVar[Mapping[str, str]] = MappingProxyType(
        {"single": "path", "text": "val"}
    )

    name: str
    qualifier: str
    emit: str | None = None
    glob: NfTemplate | None = None
    path_kind: str | None = None
    is_array: bool = False
    entry: NfEntry | None = None
    capture: str | None = None
    optional: bool = False
    scalar_type: str | None = None

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="port name")
        if self.qualifier not in self.ALLOWED_QUALIFIERS:
            allowed = ", ".join(sorted(self.ALLOWED_QUALIFIERS))
            raise ValueError(f"port qualifier must be one of {allowed}, got {self.qualifier!r}")
        if self.qualifier == "path":
            path_kind = self.path_kind or "file"
            if path_kind not in {"file", "directory"}:
                raise ValueError("path port kind must be 'file' or 'directory'")
            object.__setattr__(self, "path_kind", path_kind)
        elif self.path_kind is not None:
            raise ValueError("only path ports may declare a path kind")
        if self.emit is not None:
            _validate_ir_identifier(self.emit, field_name="port emit")
        if self.glob is not None and not isinstance(self.glob, NfTemplate):
            raise TypeError("port glob must be an NfTemplate or None")
        if not isinstance(self.is_array, bool):
            raise TypeError("port is_array must be a bool")
        if not isinstance(self.optional, bool):
            raise TypeError("port optional must be a bool")
        if self.optional and (self.qualifier != "path" or self.glob is None):
            raise ValueError("only a path output may be optional")
        if self.scalar_type is not None and (self.scalar_type not in SCALAR_TYPES or self.qualifier != "val"
                                             or self.is_array or self.glob is not None):
            raise ValueError(f"a scalar type is one of {', '.join(sorted(SCALAR_TYPES))} on a val input that is not "
                             f"array-marked, not {self.scalar_type!r} on a {self.qualifier} "
                             f"{'port' if self.glob is None else 'output'}")
        if self.entry is not None:
            if not isinstance(self.entry, NfEntry):
                raise TypeError("port entry must be an NfEntry or None")
            if self.qualifier != "path" or self.is_array or self.glob is not None:
                raise ValueError("only a path input that is not array-marked may be an InitialWorkDirRequirement entry")
        if self.capture is not None:
            if self.capture not in self.CAPTURE_QUALIFIERS:
                allowed = ", ".join(sorted(self.CAPTURE_QUALIFIERS))
                raise ValueError(
                    f"port capture must be one of {allowed}, got {self.capture!r}"
                )
            required = self.CAPTURE_QUALIFIERS[self.capture]
            if self.qualifier != required:
                raise ValueError(
                    f"capture {self.capture!r} requires a {required} port, "
                    f"got {self.qualifier!r}"
                )
            if self.is_array:
                raise ValueError("a capture marker cannot combine with an array marker")
            match self.glob:
                case NfTemplate(segments=[NfLiteral() as literal]):
                    pass
                case _:
                    # A capture declaration projects the first glob match, so it
                    # is only provable where the match set has one member by
                    # construction. A reference-bearing glob's runtime value
                    # could carry a wildcard, so it is not decidable here.
                    raise ValueError(
                        "a capture marker requires a single-literal glob with no input reference"
                    )
            if wildcards := sorted(GLOB_WILDCARDS.intersection(literal.value)):
                raise ValueError(
                    "a capture marker requires a glob with no wildcard character; "
                    f"found {', '.join(repr(character) for character in wildcards)}"
                )

    @property
    def literal_glob(self) -> bool:
        """Whether Nextflow matches the output glob literally (``glob: false``)."""
        return self.capture == "single" or (self.glob is not None and _glob_names_one_file(self.glob))

    @property
    def one_file(self) -> bool:
        """Whether the output must match one file, or at most one when optional, as cwltool enforces."""
        return self.capture == "single" or (self.glob is not None and not self.literal_glob and any(
            not isinstance(segment, NfLiteral) or _GLOB_METACHARACTERS & set(segment.value)
            for segment in self.glob.segments
        ))


def template_segments(command: "NfCommand", outputs: Sequence["NfPort"]) -> list[NfTemplateSegment]:
    """Every segment of a process's command templates and its output globs."""
    templates = [*command.templates, *(port.glob for port in outputs if port.glob)]
    return [segment for template in templates for segment in template.segments]


def inputs_read(items: Iterable[Any], *kinds: type[Any]) -> set[str]:
    """The inputs that the segments or tokens of ``kinds`` among ``items`` reference."""
    return {item.name for item in items if isinstance(item, kinds)}


def _referenced_names(command: "NfCommand", outputs: Sequence["NfPort"]) -> set[str]:
    """Every input a process's command tokens, its templates and its output globs reference."""
    return inputs_read(command.tokens, NfFlag, NfArrayBinding, NfOptionalBinding) | inputs_read(
        template_segments(command, outputs), NfBasenameReference, NfInputReference)


#: What `reference_problems` names a broken rule, so a caller can tell an array referenced as one value apart.
ARRAY_REFERENCE: Final = "array-marked inputs may only be referenced by array bindings"


def reference_problems(process: str, inputs: Sequence["NfPort"], step_only: Sequence["NfPort"],
                       outputs: Sequence["NfPort"], command: "NfCommand") -> list[tuple[str, str]]:
    """Each reference of a process's command, environment and globs that its inputs cannot take: the rule it
    breaks, and why. A step-only input is never read; a name is a declared input; a flag, an optional binding and an
    environment variable read a value, a basename a path, an array binding an array; and an array is read only by
    an array binding, since a plain or basename reference renders it as a Groovy list and a flag lets list
    truthiness decide."""
    problems: list[tuple[str, str]] = []
    segments = template_segments(command, outputs)
    flag_names, optional_binding_names, array_binding_names = (
        inputs_read(command.tokens, kind) for kind in (NfFlag, NfOptionalBinding, NfArrayBinding))
    basename_names = inputs_read(segments, NfBasenameReference)
    plain_reference_names = inputs_read(segments, NfInputReference)
    # An environment variable's value is one of the command's templates, so the plain references hold its input.
    env_names = inputs_read((segment for variable in command.env for segment in variable.value.segments),
                            NfInputReference)
    referenced_names = _referenced_names(command, outputs)
    input_ports = {port.name: port for port in inputs}
    if carried := referenced_names & {port.name for port in step_only}:
        problems.append(("step-only", f"process {process!r} command reads the step-only inputs "
                         f"{', '.join(sorted(carried))}, which its tool does not declare"))
    if unknown := referenced_names - input_ports.keys():
        problems.append(("unknown", f"process {process!r} templates reference unknown inputs: "
                         f"{', '.join(sorted(unknown))}"))
        return problems
    rules: tuple[tuple[set[str], Callable[[NfPort], bool], str], ...] = (
        (flag_names, lambda port: port.qualifier == "val", "flag tokens must reference val inputs"),
        (optional_binding_names, lambda port: port.qualifier == "val", "optional bindings must reference val inputs"),
        (env_names, lambda port: port.qualifier == "val", "environment variables must reference val inputs"),
        (basename_names, lambda port: port.qualifier == "path", "basename references must target path inputs"),
        (array_binding_names, lambda port: port.is_array, "array bindings must reference array-marked inputs"),
        (plain_reference_names | basename_names | flag_names | optional_binding_names, lambda port: not port.is_array,
         ARRAY_REFERENCE),
    )
    for referencing, admits, rule in rules:
        if invalid := sorted(name for name in referencing if not admits(input_ports[name])):
            problems.append((rule, f"process {process!r} {rule}: {', '.join(invalid)}"))
    return problems


@dataclass(frozen=True, slots=True)
class NfProcess(_Model):
    """An immutable executable Nextflow process.

    ``step_only_inputs`` are the step inputs its tool does not declare that
    the lowering carries (design §6, Step inputs): ``val`` inputs after the
    tool's own, which only the condition may read.
    """

    name: str
    inputs: Sequence[NfPort]
    outputs: Sequence[NfPort]
    command: NfCommand
    container: str | None = None
    resources: NfResources = field(default_factory=NfResources)
    condition: Expr | None = None
    step_only_inputs: Sequence[NfPort] = ()

    @property
    def all_inputs(self) -> tuple[NfPort, ...]:
        """Every input the process receives, in the order its block declares them: the tool's, then the
        step-only ones."""
        return (*self.inputs, *self.step_only_inputs)

    def __post_init__(self) -> None:
        _validate_ir_declaration(self.name, field_name="process name")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        step_only = tuple(self.step_only_inputs)
        if not all(isinstance(port, NfPort) for port in (*inputs, *outputs, *step_only)):
            raise TypeError("process inputs and outputs must contain only NfPort values")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "step_only_inputs", step_only)
        for kind, ports in (("input", (*inputs, *step_only)), ("output", outputs)):
            names = [port.name for port in ports]
            if len(names) != len(set(names)):
                raise ValueError(f"process {self.name!r} has duplicate {kind} port names")
        if any(port.qualifier != "val" for port in step_only):
            raise ValueError(f"process {self.name!r} step-only inputs must be val inputs, which stage nothing")
        output_emits = [port.emit or port.name for port in outputs]
        if len(output_emits) != len(set(output_emits)):
            raise ValueError(f"process {self.name!r} has duplicate output emit names")
        if any(port.emit is not None or port.glob is not None for port in (*inputs, *step_only)):
            raise ValueError("process input ports cannot declare output metadata")
        # A text-capture output carries the globbed file's decoded text, so it
        # is the one output kind with the val qualifier; NfPort pairs each
        # capture marker with the qualifier it requires.
        if any(
            port.qualifier != ("val" if port.capture == "text" else "path") or port.glob is None
            for port in outputs
        ):
            raise ValueError("executable process outputs require path qualifier and typed glob")
        if any(port.is_array for port in outputs):
            raise ValueError("array-typed outputs are deferred beyond this lowering")
        if not isinstance(self.command, NfCommand):
            raise TypeError("process command must be an NfCommand")
        if broken := reference_problems(self.name, inputs, step_only, outputs, self.command):
            raise ValueError(broken[0][1])
        referenced_names = _referenced_names(self.command, outputs)
        entry_names = [port.entry.name for port in inputs if port.entry is not None and port.entry.name is not None]
        renamed = {port.name for port in inputs if port.entry is not None and port.entry.name is not None}
        if overlap := renamed & referenced_names:
            raise ValueError(
                f"process {self.name!r} references an input whose InitialWorkDirRequirement entry has a literal "
                f"name in its command, stream targets, or output globs: {', '.join(sorted(overlap))}"
            )
        if len(entry_names) != len(set(entry_names)):
            raise ValueError(f"process {self.name!r} makes two InitialWorkDirRequirement entries with one literal name")
        if staged := staged_write_problems(self.name, inputs, outputs, self.command):
            raise ValueError(staged[0])
        match self.container:
            case None:
                pass
            case str() as container if container.strip():
                pass
            case _:
                raise ValueError("process container must be a non-empty string or None")
        if not isinstance(self.resources, NfResources):
            raise TypeError("process resources must be NfResources")
        if self.condition is not None:
            self._check_condition(self.condition)
            object.__setattr__(self, "condition", canonical(self.condition))

    def _check_condition(self, condition: Expr) -> None:
        """A condition is a valid tree over scalar val inputs, step-only ones included, that types as a boolean
        over their scalar types once they have them."""
        if not isinstance(condition, Expr):
            raise TypeError("process condition must be a typed Expr or None")
        validate(condition)
        ports = {port.name: port for port in self.all_inputs}
        if (problem := condition_without_inputs(self.name, self.all_inputs)) is not None:
            raise ValueError(problem)
        if invalid := sorted(
            name for name in references(condition)
            if name not in ports or ports[name].qualifier != "val" or ports[name].is_array
        ):
            raise ValueError(f"process {self.name!r} condition must reference scalar val inputs: {', '.join(invalid)}")
        # The workflow gives a val input with no scalar type the one its connection gives, and types the condition
        # then.
        if all(ports[name].scalar_type is not None for name in references(condition)):
            _type_condition(self.name, condition, ports.values())


def condition_without_inputs(process: str, inputs: Sequence[NfPort]) -> str | None:
    """Why a process with `inputs` cannot carry a condition, or None: it is decided once per invocation of its
    inputs, so it needs at least one."""
    if inputs:
        return None
    return (f"process {process!r} has a condition but no inputs: a conditional step is decided once per invocation "
            "of its inputs, so it needs at least one input to gate on")


def _type_condition(name: str, condition: Expr, ports: Iterable[NfPort]) -> None:
    """Refuse a process condition that does not type as a boolean over its inputs' scalar types."""
    try:
        typed = check(condition, {port.name: port.scalar_type for port in ports})
    except ValueError as exc:
        raise ValueError(f"process {name!r} condition does not type over its inputs: {exc}") from None
    if typed != "boolean":
        raise ValueError(f"process {name!r} condition must compute a boolean, not a {typed}")


# Adapters that combine two or more scattered whole arrays into invocations
# (design §6, Topology, Multi-input scatter); each value names its method.


@dataclass(frozen=True, slots=True)
class NfChannel:
    """How a workflow parameter's channel is built (design §5.2).

    ``path_kind`` is the kind each value is staged as, or None for a value;
    ``carries_list`` is whether the parameter holds a list: an array port's,
    or the whole array a scatter adapter splits into tasks.
    """

    path_kind: str | None
    carries_list: bool

    def __str__(self) -> str:
        return ("val" if self.path_kind is None else f"path[{self.path_kind}]") + ("[]" if self.carries_list else "")


def param_channel(param_type: str) -> NfChannel:
    """The channel a workflow parameter of ``param_type`` is built as."""
    base = param_type.removesuffix("?")
    item = base.removesuffix("[]")
    return NfChannel(PATH_KINDS.get(item), base != item)


@dataclass(frozen=True, slots=True)
class NfWorkflowInputConnection(_Model):
    """Connect one workflow parameter to one process input.

    ``adapter`` marks a scattered input: ``"dotproduct"``,
    ``"flat_crossproduct"`` or ``"nested_crossproduct"`` names how the whole
    arrays of the process's scattered inputs combine into one invocation per
    shared index (dotproduct) or per combination (the crossproducts) (design
    §6, Topology); every scattered input of one process carries the same one,
    which is validated below. A single-input scatter is a dotproduct. Every
    other adaptation a topology might require is rejected before lowering.
    """

    ALLOWED_ADAPTERS: ClassVar[frozenset[str]] = SCATTER_METHODS

    from_port: str
    to_process: str
    to_port: str
    adapter: str | None = None

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_port, field_name="workflow input")
        _validate_ir_identifier(self.to_process, field_name="connection destination process")
        _validate_ir_identifier(self.to_port, field_name="connection destination port")
        if self.adapter is not None and self.adapter not in self.ALLOWED_ADAPTERS:
            allowed = ", ".join(sorted(self.ALLOWED_ADAPTERS))
            raise ValueError(
                f"channel adapter must be one of {allowed}, got {self.adapter!r}"
            )


@dataclass(frozen=True, slots=True)
class NfProcessConnection(_Model):
    """Connect one process output to one process input.

    ``adapter`` is set exactly when the source is a multi-input-scattered
    process (design §6, Topology, Gather): ``"gather"`` collects its outputs
    once, in invocation order, into an unscattered array-typed port; a
    multi-input scatter method means the gathered array is that method's
    whole-array source for the destination's scatter.
    """

    ALLOWED_ADAPTERS: ClassVar[frozenset[str]] = frozenset({"gather", *SCATTER_METHODS})

    from_process: str
    from_port: str
    to_process: str
    to_port: str
    adapter: str | None = None

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_process, field_name="connection source process")
        _validate_ir_identifier(self.from_port, field_name="connection source port")
        _validate_ir_identifier(self.to_process, field_name="connection destination process")
        _validate_ir_identifier(self.to_port, field_name="connection destination port")
        if self.adapter is not None and self.adapter not in self.ALLOWED_ADAPTERS:
            raise ValueError(
                f"process connection adapter must be one of {', '.join(sorted(self.ALLOWED_ADAPTERS))}, "
                f"got {self.adapter!r}"
            )


@dataclass(frozen=True, slots=True)
class NfWorkflowOutputConnection(_Model):
    """Connect one process output to one workflow output."""

    from_process: str
    from_port: str
    to_port: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_process, field_name="connection source process")
        _validate_ir_identifier(self.from_port, field_name="connection source port")
        _validate_ir_identifier(self.to_port, field_name="workflow output")


#: A connection that carries one source's values, a workflow input's or a process output's, into a process input:
#: the connection a scatter adapter sits on, and each source of a step input.
NfSourceConnection = NfWorkflowInputConnection | NfProcessConnection


@dataclass(frozen=True, slots=True)
class NfStepInputConnection(_Model):
    """Connect one step input's bound values to one process input through the step-input idioms.

    ``sources`` are the values the step input binds, in the order it names
    them, each a connection into this process input. ``link_merge`` joins
    them into one list, and ``pick_value``, when set, picks from that list
    (design §6, Topology, Step inputs). Each source is one value, so the
    idioms combine values, never queue channels.
    """

    sources: Sequence[NfSourceConnection]
    link_merge: str
    pick_value: str | None = None

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        if not sources or not all(isinstance(source, (NfWorkflowInputConnection, NfProcessConnection))
                                  for source in sources):
            raise TypeError("step input sources must be one or more workflow input or process connections")
        if len({(source.to_process, source.to_port) for source in sources}) != 1:
            raise ValueError("step input sources must all reach one process input")
        if any(source.adapter not in (None, "gather") for source in sources):
            raise ValueError("a step input's sources cannot be scattered")
        if self.link_merge not in LINK_MERGES:
            raise ValueError(f"linkMerge must be one of {', '.join(sorted(LINK_MERGES))}, got {self.link_merge!r}")
        if self.pick_value is not None and self.pick_value not in PICK_VALUES:
            raise ValueError(f"pickValue must be one of {', '.join(sorted(PICK_VALUES))}, got {self.pick_value!r}")
        object.__setattr__(self, "sources", sources)

    @property
    def to_process(self) -> str:
        """The process the step input belongs to."""
        return self.sources[0].to_process

    @property
    def to_port(self) -> str:
        """The process input the step input binds."""
        return self.sources[0].to_port

    @property
    def gives_list(self) -> bool:
        """Whether the idioms give a list: every linkMerge does, unless a pickValue picks one value."""
        return self.pick_value not in PICKS_ONE


#: A connection into a process input.
NfIncomingConnection = NfSourceConnection | NfStepInputConnection
NfConnection = NfIncomingConnection | NfWorkflowOutputConnection
#: The ``kind`` each variant of a union is written with.
_KINDS: Final = {
    NfLiteral: "literal", NfInputReference: "input", NfBasenameReference: "basename", NfFlag: "flag",
    NfArrayBinding: "array", NfOptionalBinding: "optional_binding", NfShellLiteral: "shell_literal",
    NfWorkflowInputConnection: "workflow_input", NfProcessConnection: "process",
    NfWorkflowOutputConnection: "workflow_output", NfStepInputConnection: "step_input",
}
#: The fields the JSON form leaves out when they hold nothing.
_LEFT_OUT: Final = frozenset({(NfProcess, "condition"), (NfProcess, "step_only_inputs"),
                             (NfProcessConnection, "adapter")})


def incoming_connections(connections: Iterable[NfConnection]) -> dict[tuple[str, str], NfIncomingConnection]:
    """The connection into each process input, by process and port: the model admits one."""
    return {(connection.to_process, connection.to_port): connection for connection in connections
            if not isinstance(connection, NfWorkflowOutputConnection)}


def edges(connections: Iterable[NfConnection]) -> Iterator[NfSourceConnection | NfWorkflowOutputConnection]:
    """Each connection that carries one source's values: every source of a step input, and every other as it is."""
    for connection in connections:
        yield from connection.sources if isinstance(connection, NfStepInputConnection) else (connection,)


def scatter_edges(connections: Iterable[NfConnection]) -> list[NfSourceConnection]:
    """The connections into scattered inputs, each of which carries its process's scatter method."""
    return [connection for connection in connections
            if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
            and connection.adapter in SCATTER_METHODS]


def scalar_type(connection: NfIncomingConnection, param_types: Mapping[str, str],
                processes: Mapping[str, NfProcess]) -> str | None:
    """The type of the one scalar a ``val`` input receives over ``connection``, or None for a path or a list.

    A parameter gives its declared type, and one element of it to a scattered
    input; a process output gives the text it captures, which may be null
    when its process has a condition; a step input's idioms give one value
    only when its ``pickValue`` picks one, which is never null, and of the
    type its sources share.

    Args:
        connection (NfIncomingConnection): The connection into the input.
        param_types (Mapping[str, str]): Each parameter's declared type.
        processes (Mapping[str, NfProcess]): The processes a connection may come from, by name.

    Returns:
        str | None: One of ``SCALAR_TYPES``, or None.
    """
    match connection:
        case NfStepInputConnection(sources=sources):
            if connection.gives_list:
                return None
            picked = {scalar_type(source, param_types, processes) for source in sources}
            shared = {found.removesuffix("?") for found in picked if found is not None}
            return shared.pop() if None not in picked and len(shared) == 1 else None
        case NfWorkflowInputConnection(from_port=name, adapter=adapter):
            declared = param_types[name]
            if adapter is not None:  # one element of the array
                declared = declared.removesuffix("?").removesuffix("[]")
            return declared if declared in SCALAR_TYPES else None
        case NfProcessConnection(from_process=name, from_port=emit, adapter=adapter):
            producer = processes[name]
            output = next((port for port in producer.outputs if port.name == emit), None)
            if adapter == "gather" or output is None or output.capture != "text":
                return None
            # A gathered array scattered again gives one element; a conditional process's array is never gathered.
            return "string?" if producer.condition is not None and adapter is None else "string"


def with_scalar_types(process: NfProcess, incoming: Mapping[tuple[str, str], NfIncomingConnection],
                      param_types: Mapping[str, str], processes: Mapping[str, NfProcess]) -> NfProcess:
    """``process`` with each val input given the scalar type its connection gives (design §9); a type the input
    was given that disagrees is refused."""
    def filled(port: NfPort) -> NfPort:
        if port.qualifier != "val":
            return port
        received = scalar_type(incoming[(process.name, port.name)], param_types, processes)
        if port.scalar_type not in (None, received):
            raise ValueError(f"process input {process.name}.{port.name} receives "
                             f"{'no one scalar' if received is None else repr(received)}, but its scalar type is "
                             f"{port.scalar_type!r}")
        return replace(port, scalar_type=received)
    return replace(process, inputs=[filled(port) for port in process.inputs],
                   step_only_inputs=[filled(port) for port in process.step_only_inputs])


def process_dependencies(
    names: Iterable[str],
    connections: Iterable[NfConnection],
) -> dict[str, set[str]]:
    """Map each process name to the process names it depends on.

    Args:
        names (Iterable[str]): Every process name in the workflow.
        connections (Iterable[NfConnection]): Workflow connections; only
            process-to-process connections contribute dependencies.

    Returns:
        dict[str, set[str]]: Dependency sets keyed by process name.
    """
    dependencies: dict[str, set[str]] = {name: set() for name in names}
    for edge in edges(connections):
        if isinstance(edge, NfProcessConnection):
            dependencies[edge.to_process].add(edge.from_process)
    return dependencies


def topological_order(
    dependencies: Mapping[str, Iterable[str]],
    *,
    error: str,
    key: Callable[[str], Any] | None = None,
) -> list[str]:
    """Order process names topologically or raise ``ValueError`` on a cycle.

    Ready names are emitted in batches; ``key`` orders each batch so callers
    that need it get a deterministic order.

    Args:
        dependencies (Mapping[str, Iterable[str]]): Dependency sets keyed by
            process name, as built by :func:`process_dependencies`.
        error (str): Message for the ``ValueError`` raised on a cycle.
        key (Callable[[str], Any] | None): Optional sort key applied to each
            ready batch for deterministic output.

    Raises:
        ValueError: If the dependency graph contains a cycle.

    Returns:
        list[str]: Every process name in topological order.
    """
    sorter = TopologicalSorter(dependencies)
    try:
        sorter.prepare()
    except CycleError as exc:
        raise ValueError(error) from exc
    ordered: list[str] = []
    while sorter.is_active():
        batch = sorter.get_ready()
        ordered.extend(sorted(batch, key=key) if key else batch)
        sorter.done(*batch)
    return ordered


def absent_path_problems(params: Mapping[str, Any], param_types: Mapping[str, str]) -> list[str]:
    """Each parameter that holds the absent sentinel for a File? or a Directory?: no path parameter has a lowering of an
    absent value."""
    return [f"workflow input {name!r} is an absent optional {declared}, which has no lowering; give it a value"
            for name, declared in sorted(param_types.items())
            if declared in ("File?", "Directory?") and isinstance(params.get(name), (list, tuple)) and not params[name]]


def optional_output_problems(processes: Sequence[NfProcess], connections: Sequence["NfConnection"]) -> list[str]:
    """Each optional output a workflow cannot take as it is (design §6, Outputs and globs): one of a multi-input
    scattered process, whose gather would drop the absent positions; one that may match more than one file and reaches
    no workflow output, where at most one match is checked; and one another process reads, since null has one
    representation, the [] sentinel, and no input has an approved lowering of it."""
    by_name = {process.name: process for process in processes}
    problems: list[str] = []
    for name in sorted({edge.to_process for edge in scatter_edges(connections)}):
        if name in by_name and (optional := [port.name for port in by_name[name].outputs if port.optional]):
            problems.append(f"scattered process {name!r} cannot have the optional outputs {', '.join(optional)}")
    checked = {(connection.from_process, connection.from_port)
               for connection in connections if isinstance(connection, NfWorkflowOutputConnection)}
    for process in processes:
        if unchecked := [port.name for port in process.outputs
                         if port.optional and port.one_file and (process.name, port.name) not in checked]:
            problems.append(f"process {process.name!r} optional outputs {', '.join(unchecked)} must match at most one "
                            "file and must reach a workflow output, where that is checked")
    for source in edges(connections):
        if not isinstance(source, NfProcessConnection) or source.from_process not in by_name:
            continue
        port = next((port for port in by_name[source.from_process].outputs if port.name == source.from_port), None)
        if port is not None and port.optional:
            problems.append(f"connection {source.from_process}.{source.from_port} -> {source.to_process}."
                            f"{source.to_port} consumes an optional output, which may be null; only a workflow "
                            "output can take it")
    return problems


def endpoint_problems(processes: Sequence[NfProcess], connections: Sequence["NfConnection"]) -> list[str]:
    """Each connection end that names no process, or no port of its process: an output a source names, an input a
    destination names."""
    by_name = {process.name: process for process in processes}
    problems: list[str] = []
    for each in edges(connections):
        if isinstance(each, (NfProcessConnection, NfWorkflowOutputConnection)):
            source = by_name.get(each.from_process)
            if source is None:
                problems.append(f"connection references unknown source process {each.from_process!r}")
            elif each.from_port not in {port.name for port in source.outputs}:
                problems.append(f"connection references unknown output port {each.from_process}.{each.from_port}")
    return problems


@dataclass(frozen=True, slots=True)
class ExecutableNextflowWorkflow(_Model):
    """Closed, immutable, versioned executable representation of a DSL2 workflow.

    ``param_types`` gives every parameter in ``params`` its declared type,
    one of ``PARAM_TYPES``.
    """

    # Written into nextflow_workflow.json, which nothing reads back; any change to the payload bumps it (design §5.3).
    SCHEMA_VERSION: ClassVar[int] = 21
    REPRESENTATION_KIND: ClassVar[str] = "executable"

    name: str
    processes: Sequence[NfProcess]
    connections: Sequence[NfConnection]
    params: Mapping[str, Any]
    param_types: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_ir_declaration(self.name, field_name="workflow name")
        processes = tuple(self.processes)
        connections = tuple(self.connections)
        if not all(isinstance(process, NfProcess) for process in processes):
            raise TypeError("workflow processes must contain only NfProcess values")
        connection_types = (
            NfWorkflowInputConnection,
            NfProcessConnection,
            NfWorkflowOutputConnection,
            NfStepInputConnection,
        )
        if not all(isinstance(connection, connection_types) for connection in connections):
            raise TypeError("workflow connections must use a typed connection variant")
        object.__setattr__(self, "processes", processes)
        object.__setattr__(self, "connections", connections)
        if not isinstance(self.params, Mapping):
            raise TypeError("workflow params must be a mapping")
        frozen_params = _freeze_json(self.params)
        object.__setattr__(self, "params", frozen_params)
        if not isinstance(self.param_types, Mapping):
            raise TypeError("workflow param_types must be a mapping")
        _validate_string_mapping(self.param_types, field_name="workflow param_types")
        if self.param_types.keys() != frozen_params.keys():
            raise ValueError(
                "workflow param_types must give a type to exactly the parameters in params: "
                f"{', '.join(sorted(self.param_types.keys() ^ frozen_params.keys()))}"
            )
        if unknown := sorted(set(self.param_types.values()) - PARAM_TYPES):
            raise ValueError(f"workflow param_types must be one of PARAM_TYPES, not {', '.join(map(repr, unknown))}")
        # The entry check stops a [] given for a File? or a Directory?: no path
        # parameter has a lowering of an absent value.
        if absent := absent_path_problems(frozen_params, self.param_types):
            raise ValueError(absent[0])
        object.__setattr__(self, "param_types", FrozenMapping(self.param_types))
        self._validate_graph()
        self._fill_scalar_types()
        self._canonicalize()

    @property
    def containers_enabled(self) -> bool:
        """Whether Docker is enabled: some process declares a container.

        Returns:
            bool: True when any process declares a container. Nextflow runs a
                process with no container directive on the host either way.
        """
        return any(process.container is not None for process in self.processes)

    def _validate_graph(self) -> None:
        process_by_name = {process.name: process for process in self.processes}
        if len(process_by_name) != len(self.processes):
            raise ValueError("workflow contains a duplicate process name")
        if self.name in process_by_name:
            raise ValueError(f"workflow name {self.name!r} is also a process name")
        if len(set(self.connections)) != len(self.connections):
            raise ValueError("workflow contains a duplicate connection")
        if unknown := endpoint_problems(self.processes, self.connections):
            raise ValueError(unknown[0])

        incoming: set[tuple[str, str]] = set()
        workflow_outputs: set[str] = set()
        adapters_by_process: dict[str, set[str]] = {}
        multi_scattered = {edge.to_process: edge.adapter for edge in scatter_edges(self.connections)}
        if optional := optional_output_problems(self.processes, self.connections):
            raise ValueError(optional[0])
        # A val input takes a path's value and stages nothing, so a step-only input takes any source.
        carried = {(process.name, port.name) for process in self.processes for port in process.step_only_inputs}

        for connection in self.connections:
            merge = connection if isinstance(connection, NfStepInputConnection) else None
            if merge is not None:
                destination = self._destination_port(process_by_name, merge.to_process, merge.to_port)
                if merge.gives_list != destination.is_array:
                    raise ValueError(
                        f"step input {merge.to_process}.{merge.to_port} gives "
                        f"{'a list' if merge.gives_list else 'one value'} to a port that "
                        f"{'is not' if merge.gives_list else 'is'} array-typed"
                    )
                self._record_incoming(incoming, merge.to_process, merge.to_port)
            for edge in merge.sources if merge is not None else (connection,):
                match edge:
                    case NfWorkflowInputConnection(from_port, to_process, to_port, adapter):
                        if from_port not in self.params:
                            raise ValueError(f"connection references unknown workflow input {from_port!r}")
                        destination = self._destination_port(process_by_name, to_process, to_port)
                        if adapter is not None and destination.is_array:
                            raise ValueError(
                                f"channel adapter {adapter!r} cannot target the array-marked port "
                                f"{to_process}.{to_port}"
                            )
                        # The renderer builds one channel per parameter, from its
                        # declared type, so every sink must need that one channel:
                        # connection order never picks it. A step input's idioms
                        # take the parameter's own value.
                        declared = param_channel(self.param_types[from_port])
                        if adapter is not None and (to_process, to_port) in carried:
                            raise ValueError(f"step-only input {to_process}.{to_port} cannot be scattered: no input "
                                             "of the process receives the element")
                        if merge is not None:
                            self._check_merged(merge, f"workflow input {from_port!r}", declared, destination,
                                               (to_process, to_port) in carried)
                            continue
                        # The adapter belongs to the scattered process, not to the
                        # parameter, so it only means the whole array is taken.
                        channel = NfChannel(declared.path_kind if (to_process, to_port) in carried
                                            else destination.path_kind, destination.is_array or adapter is not None)
                        if channel != declared:
                            raise ValueError(
                                f"workflow input {from_port!r} of type {self.param_types[from_port]!r} cannot feed "
                                f"{to_process}.{to_port}: incompatible channel qualifiers "
                                f"{str(declared)!r} and {str(channel)!r}"
                            )
                        if adapter is not None:
                            adapters_by_process.setdefault(to_process, set()).add(adapter)
                        self._record_incoming(incoming, to_process, to_port)
                    case NfProcessConnection(from_process, from_port, to_process, to_port, adapter):
                        # Every source names an output of a process: `endpoint_problems` found none that does not.
                        source = next(port for port in process_by_name[from_process].outputs if port.name == from_port)
                        destination = self._destination_port(process_by_name, to_process, to_port)
                        where = f"connection {from_process}.{from_port} -> {to_process}.{to_port}"
                        # Only a multi-input-scattered source carries the hidden
                        # invocation index a gather sorts by, so the adapter is
                        # present exactly when the source is one.
                        if (adapter is not None) != (from_process in multi_scattered):
                            raise ValueError(f"{where} must carry a gather adapter exactly when its source is a "
                                             "multi-input scatter")
                        if adapter is not None and process_by_name[from_process].condition is not None:
                            # A skipped invocation gathers as the [] null sentinel.
                            # A workflow output reports it as null; a step's input
                            # cannot hold it (a path array fails inside Nextflow).
                            raise ValueError(f"{where} gathers the skipped positions of conditional process "
                                             f"{from_process!r}; its outputs can only reach a workflow output")
                        if adapter is not None and multi_scattered.get(from_process) == "nested_crossproduct":
                            raise ValueError(f"{where} consumes a nested_crossproduct result, which reaches workflow "
                                             "outputs only")
                        if adapter == "gather" and merge is None and not destination.is_array:
                            raise ValueError(f"{where} gathers into a port that is not array-typed")
                        if adapter in SCATTER_METHODS:
                            if (to_process, to_port) in carried:
                                raise ValueError(f"step-only input {to_process}.{to_port} cannot be scattered: no "
                                                 "input of the process receives the element")
                            if destination.is_array:
                                raise ValueError(
                                    f"channel adapter {adapter!r} cannot target the array-marked port "
                                    f"{to_process}.{to_port}"
                                )
                            adapters_by_process.setdefault(to_process, set()).add(adapter)
                        # Cardinality is half the channel contract, so it is
                        # checked on a process edge too: a scalar output driving
                        # an array-marked port renders list operations against a
                        # single value, failing inside Nextflow rather than here.
                        # A gather is the one approved scalar-to-array edge.
                        if adapter is None and merge is None and source.is_array != destination.is_array:
                            raise ValueError(f"{where} joins incompatible channel cardinalities")
                        if merge is None and source.path_kind != destination.path_kind \
                                and (to_process, to_port) not in carried:
                            raise ValueError(f"{where} joins a {source.path_kind or 'value'} output to a "
                                             f"{destination.path_kind or 'value'} input")
                        if source.capture == "text" and process_by_name[from_process].condition is None:
                            # The qualifier axis still has no general agreement
                            # check for process edges, because every process
                            # output carried the path qualifier until this one; a
                            # captured value's only approved sink is a workflow
                            # output. A conditional step's captured-text output is
                            # the one proven exception (design §6, Topology): its
                            # value is either the captured string or the []
                            # sentinel, never colliding with a real string, so it
                            # may reach a later step's admitted optional val port.
                            raise ValueError(
                                f"process output {from_process}.{from_port} captures file text; "
                                "its only approved sink is a workflow output"
                            )
                        if merge is not None:
                            self._check_merged(
                                merge, f"process output {from_process}.{from_port}",
                                NfChannel(source.path_kind, adapter == "gather"), destination,
                                (to_process, to_port) in carried,
                            )
                        else:
                            self._record_incoming(incoming, to_process, to_port)
                    case NfWorkflowOutputConnection(to_port=to_port):
                        if to_port in workflow_outputs:
                            raise ValueError(f"workflow contains duplicate output emit name {to_port!r}")
                        workflow_outputs.add(to_port)

        for process_name, adapters in adapters_by_process.items():
            if len(adapters) > 1:
                raise ValueError(
                    f"process {process_name!r} mixes channel adapters "
                    f"{', '.join(sorted(adapters))}; a process may use only one scatter shape"
                )

        expected_inputs = {
            (process.name, port.name)
            for process in self.processes
            for port in process.all_inputs
        }
        if missing := expected_inputs - incoming:
            process_name, port_name = sorted(missing)[0]
            raise ValueError(f"process input {process_name}.{port_name} is not connected")

    def _fill_scalar_types(self) -> None:
        """Give each val input the scalar type its connection gives (design §9), refusing one given that disagrees,
        and type each condition over the filled inputs."""
        process_by_name = {process.name: process for process in self.processes}
        incoming = incoming_connections(self.connections)
        processes = []
        for process in self.processes:
            process = with_scalar_types(process, incoming, self.param_types, process_by_name)
            if process.condition is not None:
                _type_condition(process.name, process.condition, process.all_inputs)
            processes.append(process)
        object.__setattr__(self, "processes", tuple(processes))

    def _canonicalize(self) -> None:
        """Put the orders the rendering does not show in their one canonical form (design §5.2).

        Processes are in stable topological order, the order the workflow
        calls them; a multi-input-scattered process declares its scattered
        inputs first; connections are in the order of their destination
        process and port, and the workflow outputs follow in their own order.
        """
        position = {process.name: index for index, process in enumerate(self.processes)}
        order = topological_order(
            process_dependencies(position, self.connections), error="workflow connections contain a cycle",
            key=position.__getitem__,
        )
        scattered = {(edge.to_process, edge.to_port) for edge in scatter_edges(self.connections)}
        processes = []
        for name in order:
            process = self.processes[position[name]]
            inputs = [port for port in process.inputs if (name, port.name) in scattered]
            inputs += [port for port in process.inputs if (name, port.name) not in scattered]
            processes.append(process if inputs == list(process.inputs) else replace(process, inputs=inputs))
        destinations = {
            (process.name, port.name): (index, port_index)
            for index, process in enumerate(processes) for port_index, port in enumerate(process.all_inputs)
        }
        outputs = [connection for connection in self.connections if isinstance(connection, NfWorkflowOutputConnection)]
        incoming = incoming_connections(self.connections)
        object.__setattr__(self, "processes", tuple(processes))
        object.__setattr__(self, "connections", (
            *(incoming[key] for key in sorted(incoming, key=destinations.__getitem__)), *outputs))

    @staticmethod
    def _check_merged(merge: NfStepInputConnection, source: str, channel: NfChannel, destination: NfPort,
                      carried: bool) -> None:
        """A step input's source has its port's path kind, unless the port is a step-only input, which takes any;
        and it is an array only where the idioms keep one level."""
        where = f"step input {merge.to_process}.{merge.to_port}"
        if channel.path_kind != destination.path_kind and not carried:
            raise ValueError(f"{where}: {source} is {channel}, which its port's {destination.qualifier} cannot take")
        if channel.carries_list and (merge.link_merge != "merge_flattened" or merge.pick_value is not None):
            # merge_nested would nest arrays, and [] stands for both a null and an empty array.
            raise ValueError(f"{where}: an array source joins only by merge_flattened with no pickValue")

    @staticmethod
    def _destination_port(
        process_by_name: Mapping[str, NfProcess],
        process_name: str,
        port_name: str,
    ) -> NfPort:
        process = process_by_name.get(process_name)
        if process is None:
            raise ValueError(f"connection references unknown destination process {process_name!r}")
        for port in process.all_inputs:
            if port.name == port_name:
                return port
        raise ValueError(f"connection references unknown input port {process_name}.{port_name}")

    @staticmethod
    def _record_incoming(incoming: set[tuple[str, str]], process_name: str, port_name: str) -> None:
        endpoint = (process_name, port_name)
        if endpoint in incoming:
            raise ValueError(f"process input {process_name}.{port_name} has more than one source")
        incoming.add(endpoint)

    def to_dict(self) -> dict[str, Any]:
        """Return the strict versioned executable representation.

        Returns:
            dict[str, Any]: Schema version, representation kind, name,
                processes, connections, params, and param types.
        """
        return {"schema_version": self.SCHEMA_VERSION, "representation_kind": self.REPRESENTATION_KIND,
                **_Model.to_dict(self)}

    def to_json(self) -> str:
        """Serialize this executable workflow deterministically.

        Returns:
            str: Indented JSON with sorted keys; byte-stable across calls.
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)
