"""Schema-backed compute-slurm payload objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from .wic_types import Json


class ComputePayloadValidationError(ValueError):
    """Raised when a compute payload does not match the checked-in schema."""


def _compact(mapping: Mapping[str, Any]) -> Json:
    """Drop `None` values and stringify paths.

    Args:
        mapping (Mapping[str, Any]): Candidate JSON mapping.

    Returns:
        Json: Compact JSON-ready mapping.
    """
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in mapping.items()
        if value is not None
    }


@dataclass(frozen=True, slots=True)
class ToilConfig:
    """Schema mirror for `computeConfig.toilConfig`."""

    log_level: str | None = None

    def to_dict(self) -> Json:
        """Render the toil configuration.

        Returns:
            Json: JSON-ready toil configuration.
        """
        return _compact({"logLevel": self.log_level})


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """Schema mirror for `computeConfig.outputConfig`."""

    mode: str | None = None
    output_dir: str | Path | None = None

    @classmethod
    def service_default(cls) -> OutputConfig:
        """Use the service-managed output directory.

        Returns:
            OutputConfig: Service-default output configuration.
        """
        return cls(mode="serviceDefault")

    @classmethod
    def workflow_declared(cls) -> OutputConfig:
        """Preserve the workflow's own output behavior.

        Returns:
            OutputConfig: Workflow-declared output configuration.
        """
        return cls(mode="workflowDeclared")

    @classmethod
    def user_specified(cls, output_dir: str | Path) -> OutputConfig:
        """Use a caller-provided output directory.

        Args:
            output_dir (str | Path): Directory that compute-slurm should use.

        Returns:
            OutputConfig: User-specified output configuration.
        """
        return cls(mode="userSpecified", output_dir=output_dir)

    @classmethod
    def from_json(
        cls,
        *,
        mode: str | None = None,
        outputDir: str | Path | None = None,
    ) -> OutputConfig:
        """Construct from schema-shaped JSON field names.

        Args:
            mode (str | None): Raw schema `mode` value such as `workflowDeclared`.
            outputDir (str | Path | None): Raw schema `outputDir` value.

        Returns:
            OutputConfig: Output configuration using JSON/schema naming.
        """
        return cls(mode=mode, output_dir=outputDir)

    def to_dict(self) -> Json:
        """Render the output configuration.

        Raises:
            ValueError: If `mode='userSpecified'` is missing `output_dir`.

        Returns:
            Json: JSON-ready output configuration.
        """
        payload = _compact({"mode": self.mode, "outputDir": self.output_dir})
        if payload.get("mode") == "userSpecified" and "outputDir" not in payload:
            raise ValueError("userSpecified output mode requires output_dir")
        return payload


@dataclass(frozen=True, slots=True)
class SlurmConfig:  # pylint: disable=too-many-instance-attributes
    """Schema mirror for `computeConfig.slurmConfig`."""

    job_name: str | None = None
    partition: str | None = None
    slurm_job_gpu_count: int | None = None
    cpus_per_task: int | None = None
    nodes: int | None = None
    tasks_per_node: int | None = None
    output: str | None = None
    error: str | None = None
    time_limit: str | None = None
    memory: str | None = None

    def to_dict(self) -> Json:
        """Render the SLURM configuration.

        Returns:
            Json: JSON-ready SLURM configuration.
        """
        return _compact(
            {
                "jobName": self.job_name,
                "partition": self.partition,
                "slurmJobGpuCount": self.slurm_job_gpu_count,
                "cpusPerTask": self.cpus_per_task,
                "nodes": self.nodes,
                "tasksPerNode": self.tasks_per_node,
                "output": self.output,
                "error": self.error,
                "time": self.time_limit,
                "memory": self.memory,
            }
        )


@dataclass(frozen=True, slots=True)
class ComputeConfig:
    """Schema mirror for `computeConfig`."""

    toil: ToilConfig | None = None
    output: OutputConfig | None = None
    slurm: SlurmConfig | None = None

    def to_dict(self) -> Json:
        """Render nested compute configuration.

        Returns:
            Json: JSON-ready `computeConfig`.
        """
        return _compact(
            {
                "toilConfig": self.toil.to_dict() if self.toil is not None else None,
                "outputConfig": self.output.to_dict() if self.output is not None else None,
                "slurmConfig": self.slurm.to_dict() if self.slurm is not None else None,
            }
        )


@dataclass(slots=True)
class ComputeWorkflowPayload:
    """Schema-backed compute-slurm request payload."""

    cwl_workflow: Json
    cwl_job_inputs: Json
    workflow_id: str | None = None
    jobs: Json = field(default_factory=dict)
    compute_config: ComputeConfig | None = None

    def get_compute_payload(self) -> Json:
        """Render and validate the compute request payload.

        Raises:
            ComputePayloadValidationError: If the rendered payload is invalid.

        Returns:
            Json: Schema-valid compute payload.
        """
        payload: Json = {
            "cwlWorkflow": self.cwl_workflow,
            "cwlJobInputs": self.cwl_job_inputs,
            "jobs": dict(self.jobs),
        }
        if self.workflow_id:
            payload["id"] = self.workflow_id
        if self.compute_config is not None:
            compute_config = self.compute_config.to_dict()
            if compute_config:
                payload["computeConfig"] = compute_config
        return validate_compute_payload(payload)


def validate_compute_payload(payload: Mapping[str, Any]) -> Json:
    """Validate a compute payload mapping against the checked-in schema.

    Args:
        payload (Mapping[str, Any]): Candidate compute payload.

    Raises:
        ComputePayloadValidationError: If the payload is invalid.

    Returns:
        Json: Schema-valid compute payload.
    """
    payload_json: Json = dict(payload)
    try:
        _validator().validate(payload_json)
    except Exception as exc:  # pragma: no cover - schema library formats the message
        raise ComputePayloadValidationError(str(exc)) from exc
    return payload_json


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema_path = Path(__file__).with_name("compute_payload_schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


__all__ = [
    "ComputeConfig",
    "ComputePayloadValidationError",
    "ComputeWorkflowPayload",
    "OutputConfig",
    "SlurmConfig",
    "ToilConfig",
    "validate_compute_payload",
]
