"""Validated intermediate-representation types for Nextflow DSL2 support."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import json
from typing import Any, ClassVar, Self

from .nf_symbols import validate_nextflow_identifier


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
    optional: set[str] | None = None,
) -> None:
    optional_fields = optional or set()
    missing = required - set(value)
    unknown = set(value) - required - optional_fields
    if missing:
        raise ValueError(f"{type_name} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{type_name} has unknown fields: {', '.join(sorted(unknown))}")


@dataclass(frozen=True, slots=True)
class NfPort:
    """A typed Nextflow process port."""

    ALLOWED_QUALIFIERS: ClassVar[frozenset[str]] = frozenset({"path", "val", "tuple", "env", "stdin"})

    name: str
    qualifier: str
    emit: str | None = None

    def __post_init__(self) -> None:
        validate_nextflow_identifier(self.name, field_name="port name")
        if self.qualifier not in self.ALLOWED_QUALIFIERS:
            allowed = ", ".join(sorted(self.ALLOWED_QUALIFIERS))
            raise ValueError(f"port qualifier must be one of {allowed}, got {self.qualifier!r}")
        if self.emit is not None:
            validate_nextflow_identifier(self.emit, field_name="port emit")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a port from a mapping."""
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(item, type_name=cls.__name__, required={"name", "qualifier", "emit"})
        return cls(name=item["name"], qualifier=item["qualifier"], emit=item["emit"])


def _validate_port_list(value: Any, *, kind: str) -> None:
    match value:
        case list() as ports:
            for port in ports:
                match port:
                    case NfPort():
                        continue
                    case _:
                        raise TypeError(f"process {kind} must be a list of NfPort values")
        case _:
            raise TypeError(f"process {kind} must be a list of NfPort values")


@dataclass(frozen=True, slots=True)
class NfProcess:
    """A Nextflow process and the supported execution metadata needed to render it."""

    name: str
    inputs: list[NfPort]
    outputs: list[NfPort]
    script: str
    container: str | None = None
    directives: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_nextflow_identifier(self.name, field_name="process name")
        _validate_port_list(self.inputs, kind="inputs")
        _validate_port_list(self.outputs, kind="outputs")
        for kind, ports in (("input", self.inputs), ("output", self.outputs)):
            names = [port.name for port in ports]
            if len(names) != len(set(names)):
                raise ValueError(f"process {self.name!r} has duplicate {kind} port names")
        if any(port.emit is not None for port in self.inputs):
            raise ValueError("process input ports cannot declare emit names")
        match self.script:
            case str() as script if script.strip():
                pass
            case _:
                raise ValueError("process script must be a non-empty string")
        match self.container:
            case None:
                pass
            case str() as container if container.strip():
                pass
            case _:
                raise ValueError("process container must be a non-empty string or None")
        match self.directives:
            case dict():
                pass
            case _:
                raise TypeError("process directives must be a dictionary")
        _validate_string_mapping(self.directives, field_name="process directives")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a process from a mapping."""
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={"name", "inputs", "outputs", "script", "container", "directives"},
        )
        match item["inputs"], item["outputs"]:
            case list() as inputs, list() as outputs:
                return cls(
                    name=item["name"],
                    inputs=[NfPort.from_dict(port) for port in inputs],
                    outputs=[NfPort.from_dict(port) for port in outputs],
                    script=item["script"],
                    container=item["container"],
                    directives=dict(item["directives"]),
                )
            case _:
                raise TypeError("NfProcess inputs and outputs must be lists")


@dataclass(frozen=True, slots=True)
class NfConnection:
    """A directed edge between a workflow boundary and/or two process ports."""

    from_process: str | None
    from_port: str
    to_process: str | None
    to_port: str

    def __post_init__(self) -> None:
        if self.from_process is not None:
            validate_nextflow_identifier(self.from_process, field_name="connection source process")
        if self.to_process is not None:
            validate_nextflow_identifier(self.to_process, field_name="connection destination process")
        validate_nextflow_identifier(self.from_port, field_name="connection source port")
        validate_nextflow_identifier(self.to_port, field_name="connection destination port")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a connection from a mapping."""
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={"from_process", "from_port", "to_process", "to_port"},
        )
        return cls(
            from_process=item["from_process"],
            from_port=item["from_port"],
            to_process=item["to_process"],
            to_port=item["to_port"],
        )


def _validate_workflow_items(processes: Any, connections: Any) -> None:
    match processes:
        case list() as process_list:
            for process in process_list:
                match process:
                    case NfProcess():
                        continue
                    case _:
                        raise TypeError("workflow processes must be a list of NfProcess values")
        case _:
            raise TypeError("workflow processes must be a list of NfProcess values")

    match connections:
        case list() as connection_list:
            for connection in connection_list:
                match connection:
                    case NfConnection():
                        continue
                    case _:
                        raise TypeError("workflow connections must be a list of NfConnection values")
        case _:
            raise TypeError("workflow connections must be a list of NfConnection values")


@dataclass(frozen=True, slots=True)
class NextflowWorkflow:
    """Validated, JSON-hydratable intermediate representation of a DSL2 workflow."""

    name: str
    processes: list[NfProcess]
    connections: list[NfConnection]
    params: dict[str, Any]
    directives: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_nextflow_identifier(self.name, field_name="workflow name")
        _validate_workflow_items(self.processes, self.connections)
        match self.params, self.directives:
            case dict(), dict():
                pass
            case dict(), _:
                raise TypeError("workflow directives must be a dictionary")
            case _:
                raise TypeError("workflow params must be a dictionary")
        _validate_string_mapping(self.directives, field_name="workflow directives")
        try:
            json.dumps(self.params, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow params must contain JSON-compatible values") from exc
        self._validate_processes_and_connections()

    def _validate_processes_and_connections(self) -> None:
        process_by_name = {process.name: process for process in self.processes}
        if len(process_by_name) != len(self.processes):
            raise ValueError("workflow contains a duplicate process name")
        if len(set(self.connections)) != len(self.connections):
            raise ValueError("workflow contains a duplicate connection")

        for connection in self.connections:
            destination = None
            if connection.to_process is not None:
                destination = process_by_name.get(connection.to_process)
                if destination is None:
                    raise ValueError(f"connection references unknown destination process {connection.to_process!r}")
                if connection.to_port not in {port.name for port in destination.inputs}:
                    raise ValueError(
                        f"connection references unknown input port "
                        f"{connection.to_process}.{connection.to_port}"
                    )

            if connection.from_process is None:
                if connection.from_port not in self.params:
                    raise ValueError(f"connection references unknown workflow input {connection.from_port!r}")
                continue

            source = process_by_name.get(connection.from_process)
            if source is None:
                raise ValueError(f"connection references unknown source process {connection.from_process!r}")
            if connection.from_port not in {port.name for port in source.outputs}:
                raise ValueError(
                    f"connection references unknown output port "
                    f"{connection.from_process}.{connection.from_port}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize this workflow deterministically."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        """Hydrate and validate a workflow from a mapping."""
        item = _mapping(value, type_name=cls.__name__)
        _check_fields(
            item,
            type_name=cls.__name__,
            required={"name", "processes", "connections", "params"},
            optional={"directives"},
        )
        match item["processes"], item["connections"]:
            case list() as processes, list() as connections:
                return cls(
                    name=item["name"],
                    processes=[NfProcess.from_dict(process) for process in processes],
                    connections=[NfConnection.from_dict(connection) for connection in connections],
                    params=dict(item["params"]),
                    directives=dict(item.get("directives", {})),
                )
            case _:
                raise TypeError("NextflowWorkflow processes and connections must be lists")

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Hydrate and validate a workflow from JSON text."""
        match value:
            case str():
                pass
            case _:
                raise TypeError("NextflowWorkflow JSON input must be a string")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid NextflowWorkflow JSON") from exc
        return cls.from_dict(parsed)
