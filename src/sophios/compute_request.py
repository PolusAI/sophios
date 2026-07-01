"""Schema-backed compute request objects."""

from dataclasses import dataclass, field, fields as dataclass_fields
from functools import lru_cache
import json
from pathlib import Path
import time
from typing import Any, Mapping, Protocol, cast

from jsonschema import Draft202012Validator
import requests

from .wic_types import Json, RawJson


_TIMEOUT = (5, 30)
_STARTED = frozenset({"RUNNING", "COMPLETED", "ERROR", "CANCELLED"})
_ACCEPTED = frozenset({"RUNNING", "COMPLETED"})
_BODY_HEADERS = {"Content-Type": "application/json"}
_Body = Json | list[Any] | str


class ComputeRequestValidationError(ValueError):
    """Raised when a compute request does not match the checked-in schema."""


class CompiledWorkflowLike(Protocol):
    """Compiled workflow boundary consumed by the compute request API."""

    @property
    def name(self) -> str:
        """Compiled workflow name."""
        ...

    @property
    def cwl_workflow(self) -> Json:
        """Compiled CWL workflow document."""
        ...

    @property
    def cwl_job_inputs(self) -> Json:
        """Compiled CWL job inputs."""
        ...


def _json_value(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


def _nested_mapping(value: Any) -> Json:
    return cast(Json, value.to_mapping())


def _config_mapping(config: Any) -> Json:
    request: Json = {}
    for item in dataclass_fields(config):
        json_key = item.metadata.get("json")
        if json_key is None:
            continue
        value = getattr(config, item.name)
        if value is None:
            continue
        renderer = item.metadata.get("render", _json_value)
        request[str(json_key)] = renderer(value)
    return request


@dataclass(frozen=True, slots=True)
class ComputeSubmission:
    """Result returned by compute request submission."""

    workflow_id: str
    phase: str | None
    accepted: bool
    submit_response: _Body | None = None
    status_response: _Body | None = None
    logs: _Body | None = None

    @property
    def ok(self) -> bool:
        """Return whether Compute accepted or completed the submitted request."""
        return self.accepted

    @property
    def exit_code(self) -> int:
        """Return a process-style status code for CLI callers."""
        return 0 if self.accepted else 1


@dataclass(frozen=True, slots=True)
class _HttpResult:
    ok: bool
    status_code: int
    body: _Body


@dataclass(slots=True)
class _ComputeClient:
    base_url: str
    session: requests.Session
    timeout: tuple[int, int]

    def post(self, request_json: RawJson) -> _HttpResult:
        response = self.session.post(
            self._url(),
            data=request_json,
            headers=_BODY_HEADERS,
            timeout=self.timeout,
        )
        return _response_result(response)

    def status(self, workflow_id: str) -> _HttpResult:
        response = self.session.get(self._url(workflow_id, "status"), timeout=self.timeout)
        return _response_result(response)

    def logs(self, workflow_id: str) -> _HttpResult:
        response = self.session.get(self._url(workflow_id, "logs"), timeout=self.timeout)
        return _response_result(response)

    def _url(self, workflow_id: str | None = None, endpoint: str | None = None) -> str:
        base = self.base_url.rstrip("/") + "/"
        return base if workflow_id is None else f"{base}{workflow_id}/{endpoint}/"


def _response_result(response: requests.Response) -> _HttpResult:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body = cast(Json, body)
    elif isinstance(body, list):
        body = list(body)
    else:
        body = str(body)
    return _HttpResult(response.ok, response.status_code, body)


def _phase(body: _Body) -> str | None:
    if isinstance(body, dict) and "status" in body:
        return str(body["status"]).upper()
    return None


def _poll_status(
    client: _ComputeClient,
    workflow_id: str,
    *,
    poll_interval_seconds: int,
) -> tuple[str | None, _HttpResult]:
    while True:
        result = client.status(workflow_id)
        phase = _phase(result.body)
        if result.ok and phase in _STARTED:
            return phase, result
        time.sleep(poll_interval_seconds)


@dataclass(frozen=True, slots=True)
class ToilRuntimeConfig:
    """Schema mirror for `computeConfig.toilConfig`."""

    log_level: str | None = field(default=None, metadata={"json": "logLevel"})

    def to_mapping(self) -> Json:
        """Render the toil configuration."""
        return _config_mapping(self)


@dataclass(frozen=True, slots=True)
class ComputeOutputConfig:
    """Schema mirror for `computeConfig.outputConfig`."""

    mode: str | None = field(default=None, metadata={"json": "mode"})
    output_dir: str | Path | None = field(default=None, metadata={"json": "outputDir"})

    @classmethod
    def service_default(cls) -> "ComputeOutputConfig":
        """Use the service-managed output directory."""
        return cls(mode="serviceDefault")

    @classmethod
    def workflow_declared(cls) -> "ComputeOutputConfig":
        """Preserve the workflow's own output behavior."""
        return cls(mode="workflowDeclared")

    @classmethod
    def user_specified(cls, output_dir: str | Path) -> "ComputeOutputConfig":
        """Use a caller-provided output directory."""
        return cls(mode="userSpecified", output_dir=output_dir)

    @classmethod
    def from_mapping(
        cls,
        *,
        mode: str | None = None,
        outputDir: str | Path | None = None,
    ) -> "ComputeOutputConfig":
        """Construct from schema-shaped mapping field names."""
        return cls(mode=mode, output_dir=outputDir)

    def to_mapping(self) -> Json:
        """Render the output configuration."""
        request = _config_mapping(self)
        if request.get("mode") == "userSpecified" and "outputDir" not in request:
            raise ValueError("userSpecified output mode requires output_dir")
        return request


@dataclass(frozen=True, slots=True)
class SlurmJobConfig:  # pylint: disable=too-many-instance-attributes
    """Schema mirror for `computeConfig.slurmConfig`."""

    job_name: str | None = field(default=None, metadata={"json": "jobName"})
    partition: str | None = field(default=None, metadata={"json": "partition"})
    slurm_job_gpu_count: int | None = field(default=None, metadata={"json": "slurmJobGpuCount"})
    cpus_per_task: int | None = field(default=None, metadata={"json": "cpusPerTask"})
    nodes: int | None = field(default=None, metadata={"json": "nodes"})
    tasks_per_node: int | None = field(default=None, metadata={"json": "tasksPerNode"})
    output: str | None = field(default=None, metadata={"json": "output"})
    error: str | None = field(default=None, metadata={"json": "error"})
    time_limit: str | None = field(default=None, metadata={"json": "time"})
    memory: str | None = field(default=None, metadata={"json": "memory"})

    def to_mapping(self) -> Json:
        """Render the SLURM configuration."""
        return _config_mapping(self)


@dataclass(frozen=True, slots=True)
class ComputeExecutionConfig:
    """Schema mirror for `computeConfig`."""

    toil: ToilRuntimeConfig | None = field(default=None, metadata={"json": "toilConfig", "render": _nested_mapping})
    output: ComputeOutputConfig | None = field(
        default=None, metadata={"json": "outputConfig", "render": _nested_mapping})
    slurm: SlurmJobConfig | None = field(default=None, metadata={"json": "slurmConfig", "render": _nested_mapping})

    def to_mapping(self) -> Json:
        """Render nested compute configuration."""
        return _config_mapping(self)


class ComputeRequest:
    """Compute submission request built from a compiled workflow."""

    _compiled_name: str
    _cwl_job_inputs: Json
    _cwl_workflow: Json
    compute_config: ComputeExecutionConfig | None
    jobs: Json
    workflow_id: str | None

    __slots__ = (
        "_compiled_name",
        "_cwl_job_inputs",
        "_cwl_workflow",
        "compute_config",
        "jobs",
        "workflow_id",
    )

    def __init__(
        self,
        compiled: CompiledWorkflowLike,
        *,
        workflow_id: str | None = None,
        jobs: Mapping[str, Any] | None = None,
        compute_config: ComputeExecutionConfig | None = None,
    ) -> None:
        """Create a compute request from the public compiled-workflow boundary."""
        self._compiled_name = compiled.name
        self._cwl_workflow = dict(compiled.cwl_workflow)
        self._cwl_job_inputs = dict(compiled.cwl_job_inputs)
        self.workflow_id = workflow_id or compiled.name
        self.jobs = dict(jobs or {})
        self.compute_config = compute_config

    @property
    def name(self) -> str:
        """Return the compiled workflow name."""
        return self._compiled_name

    @property
    def cwl_workflow(self) -> Json:
        """Return a copy of the compiled CWL workflow document."""
        return dict(self._cwl_workflow)

    @property
    def cwl_job_inputs(self) -> Json:
        """Return a copy of the compiled CWL job inputs."""
        return dict(self._cwl_job_inputs)

    def resolved_workflow_id(self) -> str | None:
        """Return the workflow id used for request status polling."""
        workflow_id = self.workflow_id or self._cwl_workflow.get("id")
        return workflow_id if isinstance(workflow_id, str) and workflow_id else None

    def require_workflow_id(self) -> str:
        """Return the workflow id or raise before network submission."""
        workflow_id = self.resolved_workflow_id()
        if workflow_id is None:
            raise ValueError("ComputeRequest.submit requires workflow_id or compiled workflow name")
        return workflow_id

    def to_mapping(self) -> Json:
        """Render and validate the compute request as a Python mapping."""
        request: Json = {
            "cwlWorkflow": dict(self._cwl_workflow),
            "cwlJobInputs": dict(self._cwl_job_inputs),
            "jobs": dict(self.jobs),
        }
        workflow_id = self.resolved_workflow_id()
        if workflow_id is not None:
            request["id"] = workflow_id
        if self.compute_config is not None:
            compute_config = self.compute_config.to_mapping()
            if compute_config:
                request["computeConfig"] = compute_config
        return _validate_compute_request(request)

    def to_json(self, *, indent: int | None = None, sort_keys: bool = False) -> RawJson:
        """Render and validate the compute request as serialized JSON text."""
        return json.dumps(self.to_mapping(), indent=indent, sort_keys=sort_keys)

    def submit(
        self,
        submit_url: str,
        *,
        timeout: tuple[int, int] = _TIMEOUT,
        poll_interval_seconds: int = 15,
        fetch_logs: bool = True,
        log_path: str | Path | None = None,
    ) -> ComputeSubmission:
        """Submit this request to Compute and return structured submission state."""
        workflow_id = self.require_workflow_id()
        with requests.Session() as session:
            client = _ComputeClient(submit_url, session, timeout)
            submit_result = client.post(self.to_json())
            if not submit_result.ok:
                return ComputeSubmission(
                    workflow_id,
                    None,
                    False,
                    submit_response=submit_result.body,
                )

            phase, status_result = _poll_status(
                client,
                workflow_id,
                poll_interval_seconds=poll_interval_seconds,
            )
            logs = client.logs(workflow_id).body if fetch_logs and phase == "RUNNING" else None
            if log_path is not None and logs is not None:
                Path(log_path).write_text(str(logs), encoding="utf-8")

        return ComputeSubmission(
            workflow_id,
            phase,
            phase in _ACCEPTED,
            submit_response=submit_result.body,
            status_response=status_result.body,
            logs=logs,
        )


def _validate_compute_request(request: Mapping[str, Any]) -> Json:
    """Validate a compute request mapping against the checked-in schema."""
    request_mapping: Json = dict(request)
    try:
        _validator().validate(request_mapping)
    except Exception as exc:  # pragma: no cover - schema library formats the message
        raise ComputeRequestValidationError(str(exc)) from exc
    return request_mapping


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema_path = Path(__file__).with_name("compute_request_schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


__all__ = [
    "CompiledWorkflowLike",
    "ComputeExecutionConfig",
    "ComputeOutputConfig",
    "ComputeRequest",
    "ComputeRequestValidationError",
    "ComputeSubmission",
    "RawJson",
    "SlurmJobConfig",
    "ToilRuntimeConfig",
]
