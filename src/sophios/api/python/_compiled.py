"""Compiled workflow boundary objects for the public workflow API."""

from dataclasses import dataclass
from pathlib import Path

from sophios.input_output import dump_wic_yaml
from sophios.wic_types import Json


def _artifact_path(
    path: str | Path | None,
    *,
    default_name: str,
    suffixes: tuple[str, ...],
) -> Path:
    if path is None:
        return Path(default_name)

    output_path = Path(path)
    if output_path.suffix in suffixes:
        return output_path
    if output_path.suffix:
        joined = " or ".join(suffixes)
        raise ValueError(f"path must be a {joined} file or a directory")
    return output_path / default_name


def _write_yaml(path: Path, document: Json) -> Path:
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(dump_wic_yaml(document), encoding="utf-8")
    return path


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """Compiled CWL workflow plus its generated job inputs."""

    name: str
    cwl_workflow: Json
    cwl_job_inputs: Json

    def to_cwl_yaml(self) -> str:
        """Return the compiled CWL workflow as YAML."""
        return dump_wic_yaml(self.cwl_workflow)

    def write_cwl(self, path: str | Path | None = None) -> Path:
        """Write the compiled CWL workflow to a `.cwl` file."""
        return _write_yaml(
            _artifact_path(path, default_name=f"{self.name}.cwl", suffixes=(".cwl",)),
            self.cwl_workflow,
        )

    def to_job_inputs_yaml(self) -> str:
        """Return the generated CWL job inputs as YAML."""
        return dump_wic_yaml(self.cwl_job_inputs)

    def write_job_inputs(self, path: str | Path | None = None) -> Path:
        """Write the generated CWL job inputs to a YAML file."""
        return _write_yaml(
            _artifact_path(
                path,
                default_name=f"{self.name}_inputs.yml",
                suffixes=(".yml", ".yaml"),
            ),
            self.cwl_job_inputs,
        )
