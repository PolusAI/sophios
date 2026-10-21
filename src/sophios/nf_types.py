"""Validated executable intermediate-representation types for Nextflow DSL2."""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import json
import math
import re
from types import MappingProxyType
from typing import Any, ClassVar, Generic, Self, TypeVar, cast, get_args

from .nf_expr import NF_FINITE_HELPER, NF_ROUND_HELPER, Expr, references, validate
from .nf_symbols import NEXTFLOW_SCRIPT_DECLARED_NAMES, validate_nextflow_identifier


_T = TypeVar("_T")
NF_SHELL_QUOTE_HELPER = "__sophios_shell_quote_9f72e"
NF_LOAD_CONTENTS_HELPER = "__sophios_load_contents_9f72e"
NF_SCATTER_INDEX_NAME = "__sophios_scatter_index_9f72e"
NF_NEST_HELPER = "__sophios_nest_9f72e"
NF_INPUT_HELPER = "__sophios_input_9f72e"
NF_STEP_INPUT_HELPER = "__sophios_step_input_9f72e"
NF_INTERNAL_IDENTIFIERS = frozenset({
    NF_SHELL_QUOTE_HELPER, NF_LOAD_CONTENTS_HELPER, NF_FINITE_HELPER, NF_ROUND_HELPER,
    NF_SCATTER_INDEX_NAME, NF_NEST_HELPER, NF_INPUT_HELPER, NF_STEP_INPUT_HELPER,
})
_PATH_KINDS = {"File": "file", "Directory": "directory"}
# A workflow parameter's declared type, which the generated pipeline checks
# its value against when it starts: an item type, `[]` for an array of it,
# and `?` when the parameter is optional.
PARAM_TYPES = frozenset(
    f"{item}{array}{optional}"
    for item in ("boolean", "int", "float", "string", *_PATH_KINDS)
    for array in ("", "[]")
    for optional in ("", "?")
)
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


def _thaw_json(value: Any) -> Any:
    match value:
        case Mapping() as mapping:
            return {str(key): _thaw_json(item) for key, item in mapping.items()}
        case list() | tuple() as items:
            return [_thaw_json(item) for item in items]
        case _:
            return value


def _validate_string_mapping(value: Mapping[str, str], *, field_name: str) -> None:
    for key, item in value.items():
        match key, item:
            case str() as name, str() if name:
                continue
            case str() as name, _ if name:
                raise ValueError(f"{field_name}[{name!r}] must be a string")
            case _:
                raise ValueError(f"{field_name} keys must be non-empty strings")


def _mapping(value: Any, *, type_name: str) -> Mapping[str, Any]:
    match value:
        case Mapping() as item:
            return item
        case _:
            raise TypeError(f"{type_name} hydration requires a mapping")


def _fields(
    value: Any,
    *,
    type_name: str,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> Mapping[str, Any]:
    """The mapping `value`, holding every `required` field and no field but those and the `optional` ones.

    An optional field is one serialization omits when it holds no value.
    """
    item = _mapping(value, type_name=type_name)
    if missing := required - set(item):
        raise ValueError(f"{type_name} is missing required fields: {', '.join(sorted(missing))}")
    if unknown := set(item) - required - optional:
        raise ValueError(f"{type_name} has unknown fields: {', '.join(sorted(unknown))}")
    return item


@dataclass(frozen=True, slots=True)
class NfLiteral:
    """Literal data inside a command or path template."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("template literal must be a string")
        if "\x00" in self.value:
            raise ValueError("template literals cannot contain NUL bytes")

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, str]: The segment as ``{"kind": "literal", "value": ...}``.
        """
        return {"kind": "literal", "value": self.value}


@dataclass(frozen=True, slots=True)
class NfInputReference:
    """Reference to one process input inside a template."""

    name: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="template input reference")

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, str]: The segment as ``{"kind": "input", "name": ...}``.
        """
        return {"kind": "input", "name": self.name}


@dataclass(frozen=True, slots=True)
class NfBasenameReference:
    """Reference to the staged file name of one path input.

    Nextflow stages an input under its original file name, so the staged
    path's name is exactly the CWL ``basename``.
    """

    name: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="template basename reference")

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, str]: The segment as ``{"kind": "basename", "name": ...}``.
        """
        return {"kind": "basename", "name": self.name}


NfTemplateSegment = NfLiteral | NfInputReference | NfBasenameReference


def _segment_from_dict(value: Mapping[str, Any]) -> NfTemplateSegment:
    item = _mapping(value, type_name="NfTemplateSegment")
    match item.get("kind"):
        case "literal":
            _fields(item, type_name="NfLiteral", required={"kind", "value"})
            return NfLiteral(item["value"])
        case "input":
            _fields(item, type_name="NfInputReference", required={"kind", "name"})
            return NfInputReference(item["name"])
        case "basename":
            _fields(item, type_name="NfBasenameReference", required={"kind", "name"})
            return NfBasenameReference(item["name"])
        case kind:
            raise ValueError(f"unsupported template segment kind {kind!r}")


@dataclass(frozen=True, slots=True)
class NfTemplate:
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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The canonical segment list under ``"segments"``.
        """
        return {"segments": [segment.to_dict() for segment in self.segments]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a template from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized template produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value or its segment list has the wrong shape.
            ValueError: If a segment kind is unsupported or a field is invalid.

        Returns:
            Self: The validated template.
        """
        item = _fields(value, type_name=cls.__name__, required={"segments"})
        if not isinstance(item["segments"], list):
            raise TypeError("NfTemplate segments must be a list")
        return cls(tuple(_segment_from_dict(segment) for segment in item["segments"]))


@dataclass(frozen=True, slots=True)
class NfFlag:
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

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, str]: The token as
                ``{"kind": "flag", "name": ..., "prefix": ...}``.
        """
        return {"kind": "flag", "name": self.name, "prefix": self.prefix}


@dataclass(frozen=True, slots=True)
class NfArrayBinding:
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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The token as
                ``{"kind": "array", "name": ..., "prefix": ...}``.
        """
        return {"kind": "array", "name": self.name, "prefix": self.prefix}


@dataclass(frozen=True, slots=True)
class NfShellLiteral:
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

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, str]: The token as ``{"kind": "shell_literal", "text": ...}``.
        """
        return {"kind": "shell_literal", "text": self.text}


NfCommandToken = NfTemplate | NfFlag | NfArrayBinding | NfShellLiteral


def _command_token_from_dict(value: Mapping[str, Any]) -> NfCommandToken:
    item = _mapping(value, type_name="NfCommandToken")
    match item.get("kind"):
        case "flag":
            _fields(item, type_name="NfFlag", required={"kind", "name", "prefix"})
            return NfFlag(item["name"], item["prefix"])
        case "array":
            _fields(item, type_name="NfArrayBinding", required={"kind", "name", "prefix"})
            return NfArrayBinding(item["name"], item["prefix"])
        case "shell_literal":
            _fields(item, type_name="NfShellLiteral", required={"kind", "text"})
            return NfShellLiteral(item["text"])
        case _:
            return NfTemplate.from_dict(item)


_SHELL_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class NfEnvVar:
    """One environment variable the task exports before its command.

    The value is literal text or one input's value, as an
    ``EnvVarRequirement`` entry lowers (design §6, Resources and containers).
    """

    name: str
    value: NfTemplate

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SHELL_VARIABLE.fullmatch(self.name):
            raise ValueError(f"environment variable name must be a shell variable name, got {self.name!r}")
        match self.value:
            case NfTemplate(segments=[NfLiteral() | NfInputReference()]):
                pass
            case _:
                raise ValueError(f"environment variable {self.name!r} must be literal text or one input reference")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The variable's name and value template.
        """
        return {"name": self.name, "value": self.value.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate an environment variable from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized variable produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value is not a mapping.
            ValueError: If fields are missing, unknown or invalid.

        Returns:
            Self: The validated variable.
        """
        item = _fields(value, type_name=cls.__name__, required={"name", "value"})
        return cls(item["name"], NfTemplate.from_dict(item["value"]))


@dataclass(frozen=True, slots=True)
class NfCommand:
    """Typed argv, approved stream redirections, and the variables exported before it, for one process."""

    tokens: Sequence[NfCommandToken]
    stdin: NfTemplate | None = None
    stdout: NfTemplate | None = None
    stderr: NfTemplate | None = None
    env: Sequence[NfEnvVar] = ()

    def __post_init__(self) -> None:
        tokens = tuple(self.tokens)
        if not tokens or not all(
            isinstance(token, (NfTemplate, NfFlag, NfArrayBinding, NfShellLiteral)) for token in tokens
        ):
            raise ValueError("command must contain at least one typed token")
        object.__setattr__(self, "tokens", tokens)
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

    @property
    def templates(self) -> tuple[NfTemplate, ...]:
        """Every template the command renders a value into: template tokens, redirections and variables."""
        return (
            *(token for token in self.tokens if isinstance(token, NfTemplate)),
            *(stream for stream in (self.stdin, self.stdout, self.stderr) if stream is not None),
            *(variable.value for variable in self.env),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: Serialized argv tokens, stream redirections and exported variables.
        """
        return {
            "tokens": [token.to_dict() for token in self.tokens],
            "stdin": self.stdin.to_dict() if self.stdin else None,
            "stdout": self.stdout.to_dict() if self.stdout else None,
            "stderr": self.stderr.to_dict() if self.stderr else None,
            "env": [variable.to_dict() for variable in self.env],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a command from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized command produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value or its token list has the wrong shape.
            ValueError: If required fields are missing or tokens are invalid.

        Returns:
            Self: The validated command.
        """
        item = _fields(value, type_name=cls.__name__, required={"tokens", "stdin", "stdout", "stderr", "env"})
        if not isinstance(item["tokens"], list) or not isinstance(item["env"], list):
            raise TypeError("NfCommand tokens and env must be lists")

        def hydrate(raw: Any) -> NfTemplate | None:
            return None if raw is None else NfTemplate.from_dict(raw)

        return cls(
            tuple(_command_token_from_dict(token) for token in item["tokens"]),
            hydrate(item["stdin"]), hydrate(item["stdout"]), hydrate(item["stderr"]),
            tuple(NfEnvVar.from_dict(variable) for variable in item["env"]),
        )


@dataclass(frozen=True, slots=True)
class NfResources:
    """Typed supported process resources; ``accelerator`` is a GPU count."""

    cpus: int | None = None
    memory_mb: int | float | None = None
    accelerator: int | None = None

    def __post_init__(self) -> None:
        cpus = self.cpus
        if (
            isinstance(cpus, bool)
            or (cpus is not None and not isinstance(cpus, int))
            or (isinstance(cpus, int) and cpus <= 0)
        ):
            raise ValueError("cpus must be a positive integer or None")
        accelerator = self.accelerator
        if accelerator is not None and (isinstance(accelerator, bool) or not isinstance(accelerator, int)
                                        or accelerator <= 0):
            raise ValueError("accelerator must be a positive integer or None")
        memory = self.memory_mb
        if memory is not None and (
            isinstance(memory, bool) or not isinstance(memory, (int, float)) or not 0 < memory < math.inf
        ):
            raise ValueError("memory_mb must be a positive finite number or None")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The ``cpus``, ``memory_mb`` and ``accelerator`` values.
        """
        return {"cpus": self.cpus, "memory_mb": self.memory_mb, "accelerator": self.accelerator}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate resources from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized resources produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value is not a mapping.
            ValueError: If required fields are missing or values are invalid.

        Returns:
            Self: The validated resources.
        """
        item = _fields(value, type_name=cls.__name__, required={"cpus", "memory_mb", "accelerator"})
        return cls(item["cpus"], item["memory_mb"], item["accelerator"])


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


@dataclass(frozen=True, slots=True)
class NfPort:
    """A typed Nextflow process port.

    ``optional`` marks a File or Directory output CWL declares optional: no
    match yields null rather than failing the task.

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
    stage_as: str | None = None
    capture: str | None = None
    optional: bool = False

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
        if self.stage_as is not None:
            if self.qualifier != "path":
                raise ValueError("only path ports may declare a stage_as rename")
            if self.is_array:
                raise ValueError("array-marked ports cannot declare a stage_as rename")
            if not isinstance(self.stage_as, str) or not self.stage_as.strip():
                raise ValueError("stage_as must be a non-empty string or None")
            if "/" in self.stage_as or "\x00" in self.stage_as:
                raise ValueError("stage_as must not contain a path separator or NUL byte")
            if "*" in self.stage_as or "?" in self.stage_as:
                raise ValueError("stage_as must not contain a Nextflow wildcard ('*' or '?')")
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
            if self.is_array or self.stage_as is not None:
                raise ValueError(
                    "a capture marker cannot combine with an array marker or a stage_as rename"
                )
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
        if self.optional and self.one_file:
            raise ValueError(
                "an optional output cannot require exactly one file (a pattern glob or a $(self[0]) "
                "capture): Nextflow fails that output when nothing matches"
            )

    @property
    def literal_glob(self) -> bool:
        """Whether Nextflow matches the output glob literally (``glob: false``)."""
        return self.capture == "single" or (self.glob is not None and _glob_names_one_file(self.glob))

    @property
    def one_file(self) -> bool:
        """Whether the output must match exactly one file (``arity: '1'``), as cwltool enforces."""
        return self.capture == "single" or (self.glob is not None and not self.literal_glob and any(
            not isinstance(segment, NfLiteral) or _GLOB_METACHARACTERS & set(segment.value)
            for segment in self.glob.segments
        ))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The port's name, qualifier, emit, glob, path
                kind, array marker, staged-name override, capture marker,
                and optional marker.
        """
        return {
            "name": self.name,
            "qualifier": self.qualifier,
            "emit": self.emit,
            "glob": self.glob.to_dict() if self.glob else None,
            "path_kind": self.path_kind,
            "is_array": self.is_array,
            "stage_as": self.stage_as,
            "capture": self.capture,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a port from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized port produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value is not a mapping.
            ValueError: If required fields are missing or values are invalid.

        Returns:
            Self: The validated port.
        """
        item = _fields(
            value,
            type_name=cls.__name__,
            required={"name", "qualifier", "emit", "glob", "path_kind", "is_array", "stage_as", "capture", "optional"},
        )
        glob = None if item["glob"] is None else NfTemplate.from_dict(item["glob"])
        return cls(
            name=item["name"],
            qualifier=item["qualifier"],
            emit=item["emit"],
            glob=glob,
            path_kind=item["path_kind"],
            is_array=item["is_array"],
            stage_as=item["stage_as"],
            capture=item["capture"],
            optional=item["optional"],
        )


# The tree roots that compute a boolean: logic, comparison, negation, a boolean literal or input.
_BOOLEAN_ROOTS = frozenset({"||", "&&", "==", "!=", "===", "!==", "<", "<=", ">", ">=", "u!", "boolean", "ref"})


@dataclass(frozen=True, slots=True)
class NfProcess:
    """An immutable executable Nextflow process."""

    name: str
    inputs: Sequence[NfPort]
    outputs: Sequence[NfPort]
    command: NfCommand
    container: str | None = None
    resources: NfResources = field(default_factory=NfResources)
    condition: Expr | None = None

    def __post_init__(self) -> None:
        _validate_ir_declaration(self.name, field_name="process name")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        if not all(isinstance(port, NfPort) for port in (*inputs, *outputs)):
            raise TypeError("process inputs and outputs must contain only NfPort values")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        for kind, ports in (("input", inputs), ("output", outputs)):
            names = [port.name for port in ports]
            if len(names) != len(set(names)):
                raise ValueError(f"process {self.name!r} has duplicate {kind} port names")
        output_emits = [port.emit or port.name for port in outputs]
        if len(output_emits) != len(set(output_emits)):
            raise ValueError(f"process {self.name!r} has duplicate output emit names")
        if any(port.emit is not None or port.glob is not None for port in inputs):
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
        input_names = {port.name for port in inputs}
        flag_names = {token.name for token in self.command.tokens if isinstance(token, NfFlag)}
        array_binding_names = {
            token.name for token in self.command.tokens if isinstance(token, NfArrayBinding)
        }
        templates = [*self.command.templates, *(port.glob for port in outputs if port.glob)]
        segments = [segment for template in templates for segment in template.segments]
        basename_names = {
            segment.name for segment in segments if isinstance(segment, NfBasenameReference)
        }
        plain_reference_names = {
            segment.name for segment in segments if isinstance(segment, NfInputReference)
        }
        referenced_names = flag_names | array_binding_names | basename_names | plain_reference_names
        if unknown := referenced_names - input_names:
            raise ValueError(
                f"process {self.name!r} templates reference unknown inputs: {', '.join(sorted(unknown))}"
            )
        input_ports = {port.name: port for port in inputs}
        env_names = {
            segment.name for variable in self.command.env for segment in variable.value.segments
            if isinstance(segment, NfInputReference)
        }
        rules: tuple[tuple[set[str], Callable[[NfPort], bool], str], ...] = (
            (flag_names, lambda port: port.qualifier == "val", "flag tokens must reference val inputs"),
            (env_names, lambda port: port.qualifier == "val", "environment variables must reference val inputs"),
            (basename_names, lambda port: port.qualifier == "path", "basename references must target path inputs"),
            (array_binding_names, lambda port: port.is_array, "array bindings must reference array-marked inputs"),
            # The converse direction: every reference to an array-marked input
            # must be an array binding. Plain and basename references render the
            # collection as a Groovy list, while a flag lets list truthiness decide
            # whether its prefix appears.
            (
                plain_reference_names | basename_names | flag_names,
                lambda port: not port.is_array,
                "array-marked inputs may only be referenced by array bindings",
            ),
        )
        for referencing, admits, rule in rules:
            if invalid := sorted(name for name in referencing if not admits(input_ports[name])):
                raise ValueError(f"process {self.name!r} {rule}: {', '.join(invalid)}")
        stage_as_names = {port.name for port in inputs if port.stage_as is not None}
        if overlap := stage_as_names & referenced_names:
            raise ValueError(
                f"process {self.name!r} references a renamed IWDR input elsewhere in its "
                f"command, stream targets, or output globs: {', '.join(sorted(overlap))}"
            )
        stage_as_values = [port.stage_as for port in inputs if port.stage_as is not None]
        if len(stage_as_values) != len(set(stage_as_values)):
            raise ValueError(
                f"process {self.name!r} stages more than one input under the same literal name"
            )
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
            if not isinstance(self.condition, Expr):
                raise TypeError("process condition must be a typed Expr or None")
            validate(self.condition)
            if self.condition.op not in _BOOLEAN_ROOTS:
                raise ValueError(f"process {self.name!r} condition must compute a boolean, not {self.condition.op!r}")
            if not inputs:
                raise ValueError(
                    f"process {self.name!r} has a condition but no inputs: a conditional step "
                    "is decided once per invocation of its inputs, so it needs at least one "
                    "input to gate on"
                )
            condition_names = references(self.condition)
            if invalid := sorted(
                name for name in condition_names
                if name not in input_ports or input_ports[name].qualifier != "val" or input_ports[name].is_array
            ):
                raise ValueError(
                    f"process {self.name!r} condition must reference scalar val inputs: "
                    f"{', '.join(invalid)}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The process name, ports, command, container,
                resources, and optional condition.
        """
        item: dict[str, Any] = {
            "name": self.name,
            "inputs": [port.to_dict() for port in self.inputs],
            "outputs": [port.to_dict() for port in self.outputs],
            "command": self.command.to_dict(),
            "container": self.container,
            "resources": self.resources.to_dict(),
        }
        if self.condition is not None:
            item["condition"] = self.condition.to_dict()
        return item

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a process from a mapping.

        Args:
            value (Mapping[str, Any]): Serialized process produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value or its port lists have the wrong shape.
            ValueError: If required fields are missing or values are invalid.

        Returns:
            Self: The validated process.
        """
        item = _fields(
            value,
            type_name=cls.__name__,
            required={"name", "inputs", "outputs", "command", "container", "resources"},
            optional={"condition"},
        )
        match item["inputs"], item["outputs"]:
            case list() as inputs, list() as outputs:
                return cls(
                    name=item["name"],
                    inputs=tuple(NfPort.from_dict(port) for port in inputs),
                    outputs=tuple(NfPort.from_dict(port) for port in outputs),
                    command=NfCommand.from_dict(item["command"]),
                    container=item["container"],
                    resources=NfResources.from_dict(item["resources"]),
                    condition=Expr.from_dict(item["condition"]) if "condition" in item else None,
                )
            case _:
                raise TypeError("NfProcess inputs and outputs must be lists")


# Adapters that combine two or more scattered whole arrays into invocations
# (design §6, Topology, Multi-input scatter); each value names its method.
MULTI_INPUT_ADAPTERS = frozenset({"dotproduct", "flat_crossproduct", "nested_crossproduct"})


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
    return NfChannel(_PATH_KINDS.get(item), base != item)


@dataclass(frozen=True, slots=True)
class NfWorkflowInputConnection:
    """Connect one workflow parameter to one process input.

    ``adapter`` marks a scattered input: ``"dotproduct"``,
    ``"flat_crossproduct"`` or ``"nested_crossproduct"`` names how the whole
    arrays of the process's scattered inputs combine into one invocation per
    shared index (dotproduct) or per combination (the crossproducts) (design
    §6, Topology); every scattered input of one process carries the same one,
    which is validated below. A single-input scatter is a dotproduct. Every
    other adaptation a topology might require is rejected before lowering.
    """

    ALLOWED_ADAPTERS: ClassVar[frozenset[str]] = MULTI_INPUT_ADAPTERS

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The connection with kind ``"workflow_input"``.
        """
        return {
            "kind": "workflow_input",
            "from_port": self.from_port,
            "to_process": self.to_process,
            "to_port": self.to_port,
            "adapter": self.adapter,
        }


@dataclass(frozen=True, slots=True)
class NfProcessConnection:
    """Connect one process output to one process input.

    ``adapter`` is set exactly when the source is a multi-input-scattered
    process (design §6, Topology, Gather): ``"gather"`` collects its outputs
    once, in invocation order, into an unscattered array-typed port; a
    multi-input scatter method means the gathered array is that method's
    whole-array source for the destination's scatter.
    """

    ALLOWED_ADAPTERS: ClassVar[frozenset[str]] = frozenset({"gather", *MULTI_INPUT_ADAPTERS})

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The connection with kind ``"process"``.
        """
        return {
            "kind": "process",
            "from_process": self.from_process,
            "from_port": self.from_port,
            "to_process": self.to_process,
            "to_port": self.to_port,
            **({"adapter": self.adapter} if self.adapter is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class NfWorkflowOutputConnection:
    """Connect one process output to one workflow output."""

    from_process: str
    from_port: str
    to_port: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_process, field_name="connection source process")
        _validate_ir_identifier(self.from_port, field_name="connection source port")
        _validate_ir_identifier(self.to_port, field_name="workflow output")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The connection with kind ``"workflow_output"``.
        """
        return {
            "kind": "workflow_output",
            "from_process": self.from_process,
            "from_port": self.from_port,
            "to_port": self.to_port,
        }


LINK_MERGES = frozenset({"merge_nested", "merge_flattened"})
PICK_VALUES = frozenset({"first_non_null", "the_only_non_null", "all_non_null"})


@dataclass(frozen=True, slots=True)
class NfStepInputConnection:
    """Connect one step input's bound values to one process input through the step-input idioms.

    ``sources`` are the values the step input binds, in the order it names
    them, each a connection into this process input. ``link_merge`` joins
    them into one list, and ``pick_value``, when set, picks from that list
    (design §6, Topology, Step inputs). Each source is one value, so the
    idioms combine values, never queue channels.
    """

    sources: Sequence[NfWorkflowInputConnection | NfProcessConnection]
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
        return self.pick_value not in ("first_non_null", "the_only_non_null")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The connection with kind ``"step_input"``.
        """
        return {
            "kind": "step_input",
            "sources": [source.to_dict() for source in self.sources],
            "link_merge": self.link_merge,
            "pick_value": self.pick_value,
        }


NfConnection = NfWorkflowInputConnection | NfProcessConnection | NfWorkflowOutputConnection | NfStepInputConnection


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
    for connection in connections:
        for edge in connection.sources if isinstance(connection, NfStepInputConnection) else (connection,):
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


def _connection_from_dict(value: Mapping[str, Any]) -> NfConnection:
    item = _mapping(value, type_name="NfConnection")
    match item.get("kind"):
        case "workflow_input":
            _fields(
                item,
                type_name="NfWorkflowInputConnection",
                required={"kind", "from_port", "to_process", "to_port", "adapter"},
            )
            return NfWorkflowInputConnection(item["from_port"], item["to_process"], item["to_port"], item["adapter"])
        case "process":
            _fields(
                item,
                type_name="NfProcessConnection",
                required={"kind", "from_process", "from_port", "to_process", "to_port"},
                optional={"adapter"},
            )
            return NfProcessConnection(
                item["from_process"], item["from_port"], item["to_process"], item["to_port"], item.get("adapter")
            )
        case "workflow_output":
            _fields(
                item,
                type_name="NfWorkflowOutputConnection",
                required={"kind", "from_process", "from_port", "to_port"},
            )
            return NfWorkflowOutputConnection(item["from_process"], item["from_port"], item["to_port"])
        case "step_input":
            _fields(
                item, type_name="NfStepInputConnection", required={"kind", "sources", "link_merge", "pick_value"}
            )
            if not isinstance(item["sources"], list):
                raise TypeError("NfStepInputConnection sources must be a list")
            return NfStepInputConnection(
                tuple(cast(Any, _connection_from_dict(source)) for source in item["sources"]),
                item["link_merge"],
                item["pick_value"],
            )
        case kind:
            raise ValueError(f"NfConnection has unsupported kind {kind!r}")


@dataclass(frozen=True, slots=True)
class ExecutableNextflowWorkflow:
    """Closed, immutable, versioned executable representation of a DSL2 workflow.

    ``param_types`` gives every parameter in ``params`` its declared type,
    one of ``PARAM_TYPES``.
    """

    # Hydration reads this version only; any change to the payload bumps it (design §5.3).
    SCHEMA_VERSION: ClassVar[int] = 18
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
        container_modes = {process.container is not None for process in processes}
        if len(container_modes) > 1:
            raise ValueError(
                "mixed container execution is not supported; "
                "every process must declare a container or no process may declare one"
            )
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
        if absent := sorted(name for name, declared in self.param_types.items()
                            if declared in ("File?", "Directory?") and frozen_params[name] == ()):
            raise ValueError(f"workflow input {absent[0]!r} is an absent optional {self.param_types[absent[0]]}, "
                             "which has no lowering; give it a value")
        object.__setattr__(self, "param_types", FrozenMapping(self.param_types))
        self._validate_graph()

    @property
    def containers_enabled(self) -> bool:
        """Return the validated workflow-wide container execution policy.

        Returns:
            bool: True when every process declares a container; construction
                rejects mixed policies, so this is uniform by invariant.
        """
        return bool(self.processes) and self.processes[0].container is not None

    def _validate_graph(self) -> None:
        process_by_name = {process.name: process for process in self.processes}
        if len(process_by_name) != len(self.processes):
            raise ValueError("workflow contains a duplicate process name")
        if self.name in process_by_name:
            raise ValueError(f"workflow name {self.name!r} is also a process name")
        if len(set(self.connections)) != len(self.connections):
            raise ValueError("workflow contains a duplicate connection")

        incoming: set[tuple[str, str]] = set()
        workflow_outputs: set[str] = set()
        adapters_by_process: dict[str, set[str]] = {}
        multi_scattered = {
            connection.to_process: connection.adapter
            for connection in self.connections
            if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
            and connection.adapter in MULTI_INPUT_ADAPTERS
        }
        for name in multi_scattered:
            if optional := [port.name for port in process_by_name[name].outputs if port.optional]:
                # A gather would drop the absent positions instead of keeping null there.
                raise ValueError(f"scattered process {name!r} cannot have the optional outputs {', '.join(optional)}")

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
                        if merge is not None:
                            self._check_merged(merge, f"workflow input {from_port!r}", declared, destination)
                            continue
                        # The adapter belongs to the scattered process, not to the
                        # parameter, so it only means the whole array is taken.
                        channel = NfChannel(destination.path_kind, destination.is_array or adapter is not None)
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
                        source = self._source_port(process_by_name, from_process, from_port)
                        destination = self._destination_port(process_by_name, to_process, to_port)
                        # Only a multi-input-scattered source carries the hidden
                        # invocation index a gather sorts by, so the adapter is
                        # present exactly when the source is one.
                        if (adapter is not None) != (from_process in multi_scattered):
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} must carry "
                                "a gather adapter exactly when its source is a multi-input scatter"
                            )
                        if adapter is not None and process_by_name[from_process].condition is not None:
                            # A skipped invocation gathers as the [] null sentinel.
                            # A workflow output reports it as null; a step's input
                            # cannot hold it (a path array fails inside Nextflow).
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} gathers "
                                f"the skipped positions of conditional process {from_process!r}; its outputs "
                                "can only reach a workflow output"
                            )
                        if adapter is not None and multi_scattered.get(from_process) == "nested_crossproduct":
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} consumes a "
                                "nested_crossproduct result, which reaches workflow outputs only"
                            )
                        if adapter == "gather" and merge is None and not destination.is_array:
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} gathers "
                                "into a port that is not array-typed"
                            )
                        if adapter in MULTI_INPUT_ADAPTERS:
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
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} "
                                "joins incompatible channel cardinalities"
                            )
                        if source.optional:
                            # Null has one representation, the [] sentinel, and no path port
                            # has an approved lowering of it; a workflow output reports it.
                            raise ValueError(
                                f"connection {from_process}.{from_port} -> {to_process}.{to_port} consumes an "
                                "optional output, which may be null; only a workflow output can take it"
                            )
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
                            )
                        else:
                            self._record_incoming(incoming, to_process, to_port)
                    case NfWorkflowOutputConnection(from_process, from_port, to_port):
                        self._source_port(process_by_name, from_process, from_port)
                        if to_port in workflow_outputs:
                            raise ValueError(f"workflow contains duplicate output emit name {to_port!r}")
                        workflow_outputs.add(to_port)

        for process_name, adapters in adapters_by_process.items():
            if len(adapters) > 1:
                raise ValueError(
                    f"process {process_name!r} mixes channel adapters "
                    f"{', '.join(sorted(adapters))}; a process may use only one scatter shape"
                )

        topological_order(
            process_dependencies(process_by_name, self.connections), error="workflow connections contain a cycle"
        )
        expected_inputs = {
            (process.name, port.name)
            for process in self.processes
            for port in process.inputs
        }
        if missing := expected_inputs - incoming:
            process_name, port_name = sorted(missing)[0]
            raise ValueError(f"process input {process_name}.{port_name} is not connected")

    @staticmethod
    def _check_merged(merge: NfStepInputConnection, source: str, channel: NfChannel, destination: NfPort) -> None:
        """A step input's source has its port's path kind, and is an array only where the idioms keep one level."""
        where = f"step input {merge.to_process}.{merge.to_port}"
        if channel.path_kind != destination.path_kind:
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
        for port in process.inputs:
            if port.name == port_name:
                return port
        raise ValueError(f"connection references unknown input port {process_name}.{port_name}")

    @staticmethod
    def _source_port(
        process_by_name: Mapping[str, NfProcess],
        process_name: str,
        port_name: str,
    ) -> NfPort:
        process = process_by_name.get(process_name)
        if process is None:
            raise ValueError(f"connection references unknown source process {process_name!r}")
        for port in process.outputs:
            if port.name == port_name:
                return port
        raise ValueError(f"connection references unknown output port {process_name}.{port_name}")

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
        return {
            "schema_version": self.SCHEMA_VERSION,
            "representation_kind": self.REPRESENTATION_KIND,
            "name": self.name,
            "processes": [process.to_dict() for process in self.processes],
            "connections": [connection.to_dict() for connection in self.connections],
            "params": _thaw_json(self.params),
            "param_types": dict(self.param_types),
        }

    def to_json(self) -> str:
        """Serialize this executable workflow deterministically.

        Returns:
            str: Indented JSON with sorted keys; byte-stable across calls.
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and strictly validate a versioned executable workflow.

        Args:
            value (Mapping[str, Any]): Serialized workflow produced by
                :meth:`to_dict`.

        Raises:
            TypeError: If the value or its collections have the wrong shape.
            ValueError: If the schema version, representation kind, fields,
                or graph invariants are invalid.

        Returns:
            Self: The validated executable workflow.
        """
        item = _fields(
            value,
            type_name=cls.__name__,
            required={
                "schema_version",
                "representation_kind",
                "name",
                "processes",
                "connections",
                "params",
                "param_types",
            },
        )
        if item["schema_version"] != cls.SCHEMA_VERSION:
            raise ValueError(
                f"executable Nextflow schema version {item['schema_version']!r} is not the current version "
                f"{cls.SCHEMA_VERSION}; regenerate the artifacts"
            )
        if item["representation_kind"] != cls.REPRESENTATION_KIND:
            raise ValueError(
                f"unsupported Nextflow representation kind {item['representation_kind']!r}"
            )
        match item["processes"], item["connections"]:
            case list() as processes, list() as connections:
                return cls(
                    name=item["name"],
                    processes=tuple(NfProcess.from_dict(process) for process in processes),
                    connections=tuple(_connection_from_dict(connection) for connection in connections),
                    params=_mapping(item["params"], type_name="workflow params"),
                    param_types=_mapping(item["param_types"], type_name="workflow param_types"),
                )
            case _:
                raise TypeError(
                    "ExecutableNextflowWorkflow processes and connections must be lists"
                )

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Hydrate and validate an executable workflow from JSON text.

        Args:
            value (str): JSON text produced by :meth:`to_json`.

        Raises:
            TypeError: If the value is not a string.
            ValueError: If the text is not valid JSON or fails
                :meth:`from_dict` validation.

        Returns:
            Self: The validated executable workflow.
        """
        if not isinstance(value, str):
            raise TypeError("ExecutableNextflowWorkflow JSON input must be a string")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid ExecutableNextflowWorkflow JSON") from exc
        return cls.from_dict(parsed)
