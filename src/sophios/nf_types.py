"""Validated executable intermediate-representation types for Nextflow DSL2."""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import json
import math
from types import MappingProxyType
from typing import Any, ClassVar, Generic, Self, TypeVar

from .nf_symbols import validate_nextflow_identifier


_T = TypeVar("_T")
NF_SHELL_QUOTE_HELPER = "__sophios_shell_quote_9f72e"
NF_INTERNAL_IDENTIFIERS = frozenset({NF_SHELL_QUOTE_HELPER})


def _validate_ir_identifier(value: object, *, field_name: str) -> str:
    identifier = validate_nextflow_identifier(value, field_name=field_name)
    if identifier in NF_INTERNAL_IDENTIFIERS:
        raise ValueError(f"{field_name} collides with a reserved Nextflow backend identifier")
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


def _check_fields(
    value: Mapping[str, Any],
    *,
    type_name: str,
    required: set[str],
) -> None:
    missing = required - set(value)
    unknown = set(value) - required
    if missing:
        raise ValueError(f"{type_name} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{type_name} has unknown fields: {', '.join(sorted(unknown))}")


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
NfReferenceSegment = NfInputReference | NfBasenameReference


def _segment_from_dict(value: Mapping[str, Any]) -> NfTemplateSegment:
    item = _mapping(value, type_name="NfTemplateSegment")
    match item.get("kind"):
        case "literal":
            _check_fields(item, type_name="NfLiteral", required={"kind", "value"})
            return NfLiteral(item["value"])
        case "input":
            _check_fields(item, type_name="NfInputReference", required={"kind", "name"})
            return NfInputReference(item["name"])
        case "basename":
            _check_fields(item, type_name="NfBasenameReference", required={"kind", "name"})
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
            if not isinstance(segment, (NfLiteral, NfInputReference, NfBasenameReference)):
                raise TypeError("template segments must be typed literal or input references")
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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(item, type_name=cls.__name__, required={"segments"})
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
            dict[str, str]: The token as ``{"kind": "flag", "name": ...,
                "prefix": ...}``.
        """
        return {"kind": "flag", "name": self.name, "prefix": self.prefix}


NfCommandToken = NfTemplate | NfFlag


def _command_token_from_dict(value: Mapping[str, Any]) -> NfCommandToken:
    item = _mapping(value, type_name="NfCommandToken")
    if item.get("kind") != "flag":
        return NfTemplate.from_dict(item)
    _check_fields(item, type_name="NfFlag", required={"kind", "name", "prefix"})
    return NfFlag(item["name"], item["prefix"])


@dataclass(frozen=True, slots=True)
class NfCommand:
    """Typed argv and approved stream redirections for one process."""

    tokens: Sequence[NfCommandToken]
    stdin: NfTemplate | None = None
    stdout: NfTemplate | None = None
    stderr: NfTemplate | None = None

    def __post_init__(self) -> None:
        tokens = tuple(self.tokens)
        if not tokens or not all(isinstance(token, (NfTemplate, NfFlag)) for token in tokens):
            raise ValueError("command must contain at least one typed token")
        object.__setattr__(self, "tokens", tokens)
        for name in ("stdin", "stdout", "stderr"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, NfTemplate):
                raise TypeError(f"command {name} must be an NfTemplate or None")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: Serialized argv tokens and stream redirections.
        """
        return {
            "tokens": [token.to_dict() for token in self.tokens],
            "stdin": self.stdin.to_dict() if self.stdin else None,
            "stdout": self.stdout.to_dict() if self.stdout else None,
            "stderr": self.stderr.to_dict() if self.stderr else None,
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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(item, type_name=cls.__name__, required={"tokens", "stdin", "stdout", "stderr"})
        if not isinstance(item["tokens"], list):
            raise TypeError("NfCommand tokens must be a list")

        def hydrate(raw: Any) -> NfTemplate | None:
            return None if raw is None else NfTemplate.from_dict(raw)

        return cls(
            tuple(_command_token_from_dict(token) for token in item["tokens"]),
            hydrate(item["stdin"]), hydrate(item["stdout"]), hydrate(item["stderr"]),
        )


@dataclass(frozen=True, slots=True)
class NfResources:
    """Typed supported process resources."""

    cpus: int | None = None
    memory_mb: int | float | None = None

    def __post_init__(self) -> None:
        cpus = self.cpus
        if (
            isinstance(cpus, bool)
            or (cpus is not None and not isinstance(cpus, int))
            or (isinstance(cpus, int) and cpus <= 0)
        ):
            raise ValueError("cpus must be a positive integer or None")
        memory = self.memory_mb
        if (
            isinstance(memory, bool)
            or (
                memory is not None
                and (
                    not isinstance(memory, (int, float))
                    or memory <= 0
                    or (isinstance(memory, float) and not math.isfinite(memory))
                )
            )
        ):
            raise ValueError("memory_mb must be a positive finite number or None")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The ``cpus`` and ``memory_mb`` values.
        """
        return {"cpus": self.cpus, "memory_mb": self.memory_mb}

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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(item, type_name=cls.__name__, required={"cpus", "memory_mb"})
        return cls(item["cpus"], item["memory_mb"])


@dataclass(frozen=True, slots=True)
class NfPort:
    """A typed Nextflow process port."""

    # Phase 1 lowers only these qualifiers; the renderer is total over them.
    ALLOWED_QUALIFIERS: ClassVar[frozenset[str]] = frozenset({"path", "val"})

    name: str
    qualifier: str
    emit: str | None = None
    glob: NfTemplate | None = None
    path_kind: str | None = None

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The port's name, qualifier, emit, glob, and
                path kind.
        """
        return {
            "name": self.name,
            "qualifier": self.qualifier,
            "emit": self.emit,
            "glob": self.glob.to_dict() if self.glob else None,
            "path_kind": self.path_kind,
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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={"name", "qualifier", "emit", "glob", "path_kind"},
        )
        glob = None if item["glob"] is None else NfTemplate.from_dict(item["glob"])
        return cls(
            name=item["name"],
            qualifier=item["qualifier"],
            emit=item["emit"],
            glob=glob,
            path_kind=item["path_kind"],
        )


@dataclass(frozen=True, slots=True)
class NfProcess:
    """An immutable executable Nextflow process."""

    name: str
    inputs: Sequence[NfPort]
    outputs: Sequence[NfPort]
    command: NfCommand
    container: str | None = None
    resources: NfResources = field(default_factory=NfResources)

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="process name")
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
        if any(port.qualifier != "path" or port.glob is None for port in outputs):
            raise ValueError("executable process outputs require path qualifier and typed glob")
        if not isinstance(self.command, NfCommand):
            raise TypeError("process command must be an NfCommand")
        input_names = {port.name for port in inputs}
        flag_names = {token.name for token in self.command.tokens if isinstance(token, NfFlag)}
        templates = [
            *(token for token in self.command.tokens if isinstance(token, NfTemplate)),
            *(template for template in (self.command.stdin, self.command.stdout, self.command.stderr) if template),
            *(port.glob for port in outputs if port.glob),
        ]
        segments = [segment for template in templates for segment in template.segments]
        basename_names = {
            segment.name for segment in segments if isinstance(segment, NfBasenameReference)
        }
        references = flag_names | basename_names | {
            segment.name for segment in segments if isinstance(segment, NfInputReference)
        }
        if unknown := references - input_names:
            raise ValueError(
                f"process {self.name!r} templates reference unknown inputs: {', '.join(sorted(unknown))}"
            )
        qualifiers = {port.name: port.qualifier for port in inputs}
        if invalid := {name for name in flag_names if qualifiers[name] != "val"}:
            raise ValueError(
                f"process {self.name!r} flag tokens must reference val inputs: "
                f"{', '.join(sorted(invalid))}"
            )
        if invalid := {name for name in basename_names if qualifiers[name] != "path"}:
            raise ValueError(
                f"process {self.name!r} basename references must target path inputs: "
                f"{', '.join(sorted(invalid))}"
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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            dict[str, Any]: The process name, ports, command, container,
                and resources.
        """
        return {
            "name": self.name,
            "inputs": [port.to_dict() for port in self.inputs],
            "outputs": [port.to_dict() for port in self.outputs],
            "command": self.command.to_dict(),
            "container": self.container,
            "resources": self.resources.to_dict(),
        }

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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={"name", "inputs", "outputs", "command", "container", "resources"},
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
                )
            case _:
                raise TypeError("NfProcess inputs and outputs must be lists")


@dataclass(frozen=True, slots=True)
class NfWorkflowInputConnection:
    """Connect one workflow parameter to one process input."""

    from_port: str
    to_process: str
    to_port: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_port, field_name="workflow input")
        _validate_ir_identifier(self.to_process, field_name="connection destination process")
        _validate_ir_identifier(self.to_port, field_name="connection destination port")

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
        }


@dataclass(frozen=True, slots=True)
class NfProcessConnection:
    """Connect one process output to one process input."""

    from_process: str
    from_port: str
    to_process: str
    to_port: str

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.from_process, field_name="connection source process")
        _validate_ir_identifier(self.from_port, field_name="connection source port")
        _validate_ir_identifier(self.to_process, field_name="connection destination process")
        _validate_ir_identifier(self.to_port, field_name="connection destination port")

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


NfConnection = NfWorkflowInputConnection | NfProcessConnection | NfWorkflowOutputConnection


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
        if isinstance(connection, NfProcessConnection):
            dependencies[connection.to_process].add(connection.from_process)
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
            _check_fields(
                item,
                type_name="NfWorkflowInputConnection",
                required={"kind", "from_port", "to_process", "to_port"},
            )
            return NfWorkflowInputConnection(item["from_port"], item["to_process"], item["to_port"])
        case "process":
            _check_fields(
                item,
                type_name="NfProcessConnection",
                required={"kind", "from_process", "from_port", "to_process", "to_port"},
            )
            return NfProcessConnection(
                item["from_process"], item["from_port"], item["to_process"], item["to_port"]
            )
        case "workflow_output":
            _check_fields(
                item,
                type_name="NfWorkflowOutputConnection",
                required={"kind", "from_process", "from_port", "to_port"},
            )
            return NfWorkflowOutputConnection(item["from_process"], item["from_port"], item["to_port"])
        case kind:
            raise ValueError(f"NfConnection has unsupported kind {kind!r}")


@dataclass(frozen=True, slots=True)
class ExecutableNextflowWorkflow:
    """Closed, immutable, versioned executable representation of a DSL2 workflow."""

    SCHEMA_VERSION: ClassVar[int] = 4
    # Earlier versions whose value space is a strict subset of the current
    # model hydrate unchanged; serialization always writes SCHEMA_VERSION.
    SUPPORTED_SCHEMA_VERSIONS: ClassVar[frozenset[int]] = frozenset({2, 3, 4})
    # Each additive token or segment kind declares the version that
    # introduced it, so the subset property is enforced rather than assumed.
    KIND_SCHEMA_VERSIONS: ClassVar[Mapping[str, int]] = MappingProxyType(
        {"flag": 3, "basename": 4}
    )
    REPRESENTATION_KIND: ClassVar[str] = "executable"

    name: str
    processes: Sequence[NfProcess]
    connections: Sequence[NfConnection]
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_ir_identifier(self.name, field_name="workflow name")
        processes = tuple(self.processes)
        connections = tuple(self.connections)
        if not all(isinstance(process, NfProcess) for process in processes):
            raise TypeError("workflow processes must contain only NfProcess values")
        connection_types = (
            NfWorkflowInputConnection,
            NfProcessConnection,
            NfWorkflowOutputConnection,
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
        try:
            json.dumps(_thaw_json(frozen_params), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow params must contain JSON-compatible values") from exc
        object.__setattr__(self, "params", frozen_params)
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
        if len(set(self.connections)) != len(self.connections):
            raise ValueError("workflow contains a duplicate connection")

        incoming: set[tuple[str, str]] = set()
        workflow_input_qualifiers: dict[str, str] = {}
        workflow_outputs: set[str] = set()
        dependencies: dict[str, set[str]] = {name: set() for name in process_by_name}

        for connection in self.connections:
            match connection:
                case NfWorkflowInputConnection(from_port, to_process, to_port):
                    if from_port not in self.params:
                        raise ValueError(f"connection references unknown workflow input {from_port!r}")
                    destination = self._destination_port(process_by_name, to_process, to_port)
                    # path_kind selects the staging policy, so it is part of the
                    # channel contract: connection order must never pick one.
                    semantics = destination.qualifier + (
                        f"[{destination.path_kind}]" if destination.path_kind else ""
                    )
                    previous = workflow_input_qualifiers.setdefault(from_port, semantics)
                    if previous != semantics:
                        raise ValueError(
                            f"workflow input {from_port!r} feeds incompatible channel qualifiers "
                            f"{previous!r} and {semantics!r}"
                        )
                    self._record_incoming(incoming, to_process, to_port)
                case NfProcessConnection(from_process, from_port, to_process, to_port):
                    self._source_port(process_by_name, from_process, from_port)
                    self._destination_port(process_by_name, to_process, to_port)
                    self._record_incoming(incoming, to_process, to_port)
                    dependencies[to_process].add(from_process)
                case NfWorkflowOutputConnection(from_process, from_port, to_port):
                    self._source_port(process_by_name, from_process, from_port)
                    if to_port in workflow_outputs:
                        raise ValueError(f"workflow contains duplicate output emit name {to_port!r}")
                    workflow_outputs.add(to_port)

        topological_order(dependencies, error="workflow connections contain a cycle")
        expected_inputs = {
            (process.name, port.name)
            for process in self.processes
            for port in process.inputs
        }
        if missing := expected_inputs - incoming:
            process_name, port_name = sorted(missing)[0]
            raise ValueError(f"process input {process_name}.{port_name} is not connected")

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
                processes, connections, and params.
        """
        return {
            "schema_version": self.SCHEMA_VERSION,
            "representation_kind": self.REPRESENTATION_KIND,
            "name": self.name,
            "processes": [process.to_dict() for process in self.processes],
            "connections": [connection.to_dict() for connection in self.connections],
            "params": _thaw_json(self.params),
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
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={
                "schema_version",
                "representation_kind",
                "name",
                "processes",
                "connections",
                "params",
            },
        )
        if item["schema_version"] not in cls.SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported executable Nextflow schema version {item['schema_version']!r}"
            )
        cls._reject_newer_kinds(item, declared=item["schema_version"])
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
                )
            case _:
                raise TypeError(
                    "ExecutableNextflowWorkflow processes and connections must be lists"
                )

    @classmethod
    def _reject_newer_kinds(cls, value: Any, *, declared: int) -> None:
        """Reject a payload carrying a kind newer than its declared version."""
        match value:
            case Mapping() as mapping:
                introduced = cls.KIND_SCHEMA_VERSIONS.get(str(mapping.get("kind")))
                if introduced is not None and declared < introduced:
                    raise ValueError(
                        f"{mapping['kind']!r} requires executable Nextflow schema version "
                        f"{introduced}, but the payload declares schema version {declared}"
                    )
                for item in mapping.values():
                    cls._reject_newer_kinds(item, declared=declared)
            case list() as items:
                for item in items:
                    cls._reject_newer_kinds(item, declared=declared)
            case _:
                pass

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
