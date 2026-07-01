"""Async subprocess adapter for prepared CWL workflow runs."""

from pathlib import Path
import asyncio
import traceback
from collections.abc import Mapping, Sequence
from typing import Any

import aiofiles
import yaml

from sophios.input_output import NoAliasDumper
from sophios.runtime_inputs import normalize_cwl_document, normalize_job_inputs
from sophios.wic_types import Json

from .run_local import build_cmd, copy_output_files, create_safe_env, generate_run_script


async def run_cwl_workflow(
    workflow_name: str,
    basepath: str,
    cwl_runner: str,
    container_cmd: str,
    user_env: Mapping[str, str],
    *,
    run_args_dict: Mapping[str, str] | None = None,
    passthrough_args: Sequence[str] | None = None,
) -> int | None:
    """Run a prepared CWL workflow without blocking the event loop."""
    if cwl_runner not in {"cwltool", "toil-cwl-runner"}:
        raise ValueError("cwl_runner must be 'cwltool' or 'toil-cwl-runner'")

    run_args = dict(run_args_dict or {})
    cmd = build_cmd(
        workflow_name,
        basepath,
        cwl_runner,
        container_cmd,
        list(passthrough_args or []),
        run_args.get("outdir") or None,
    )
    cmdline = " ".join(cmd)

    if run_args.get("generate_run_script", "no") == "yes":
        await asyncio.to_thread(generate_run_script, cmdline)
        return 0

    print(f"Running: {cmdline}")
    print("via async subprocess")

    try:
        exec_env = create_safe_env(dict(user_env))
        log_dir = Path(basepath) / "LOGS"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_log_path = log_dir / "stdout.txt"
        stderr_log_path = log_dir / "stderr.txt"

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=exec_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await asyncio.gather(
            _stream_to_file(proc.stdout, stdout_log_path),
            _stream_to_file(proc.stderr, stderr_log_path),
        )
        retval = await proc.wait()

        if retval != 0:
            await _print_stderr_tail(workflow_name, retval, stdout_log_path, stderr_log_path)
        elif cwl_runner == "cwltool" and run_args.get("copy_output_files", "no") == "yes":
            await asyncio.to_thread(copy_output_files, workflow_name, basepath=basepath)
        return retval

    except Exception as exc:  # pylint: disable=broad-exception-caught
        error_log_path = Path(f"error_{workflow_name}_technical.log")
        error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with error_log_path.open(mode="w", encoding="utf-8") as file:
            traceback.print_exc(file=file)
        print(f'Failed to execute workflow "{workflow_name}". See {error_log_path} for details.')
        print(f"Unhandled Python exception: {type(exc).__name__}: {exc}")
        return None


async def run_cwl_serialized(
    workflow: Json,
    basepath: str,
    cwl_runner: str,
    container_engine: str,
    user_env: Mapping[str, str],
    *,
    run_args_dict: Mapping[str, str] | None = None,
    passthrough_args: Sequence[str] | None = None,
) -> int | None:
    """Write a serialized compiled workflow and run it asynchronously."""
    workflow_name = str(workflow["name"])
    basepath = basepath.rstrip("/") if basepath != "/" else basepath
    base = Path(basepath)
    base.mkdir(parents=True, exist_ok=True)

    job_inputs = normalize_job_inputs(workflow, workflow.get("yaml_inputs", {}))
    cwl_document = {
        key: value
        for key, value in workflow.items()
        if key not in {"name", "retval", "yaml_inputs"}
    }
    cwl_document = normalize_cwl_document(cwl_document)

    await _write_yaml(base / f"{workflow_name}_inputs.yml", job_inputs)
    await _write_yaml(base / f"{workflow_name}.cwl", cwl_document, shebang=True)

    return await run_cwl_workflow(
        workflow_name,
        basepath,
        cwl_runner,
        container_engine,
        user_env,
        run_args_dict=run_args_dict,
        passthrough_args=passthrough_args,
    )


async def _stream_to_file(stream: Any, filename: Path) -> None:
    if stream is None:
        return
    async with aiofiles.open(filename, mode="wb") as file:
        while True:
            data = await stream.read(4096)
            if not data:
                break
            await file.write(data)


async def _write_yaml(path: Path, document: Json, *, shebang: bool = False) -> None:
    yaml_content = yaml.dump(
        document,
        sort_keys=False,
        line_break="\n",
        indent=2,
        Dumper=NoAliasDumper,
    )
    async with aiofiles.open(path, mode="w", encoding="utf-8") as file:
        if shebang:
            await file.write("#!/usr/bin/env cwl-runner\n")
        await file.write(yaml_content)


async def _print_stderr_tail(
    workflow_name: str,
    retval: int,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> None:
    print(f'Workflow "{workflow_name}" finished with non-zero exit code: {retval}')
    try:
        async with aiofiles.open(stderr_log_path, mode="r", encoding="utf-8", errors="ignore") as file:
            lines = await file.readlines()
    except OSError as exc:
        print(f"Could not read stderr log for immediate display: {exc}")
        return

    last_lines = lines[-50:]
    if last_lines:
        print("--- Last lines from stderr ---")
        for line in last_lines:
            print(f"  {line.strip()}")
    print(f"Full logs available at: {stdout_log_path} and {stderr_log_path}")
