"""Default runtime values for the Python workflow API."""

from pathlib import Path

DEFAULT_RUN_ARGS: dict[str, str] = {
    "cwl_runner": "cwltool",
    "container_engine": "docker",
    "pull_dir": str(Path().cwd()),
}
