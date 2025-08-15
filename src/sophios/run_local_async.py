from pathlib import Path
import traceback
import os
from typing import Optional, Dict, Any
import asyncio
import aiofiles
import yaml
# we are already using fastapi elsewhere in this project
# so use the run_in_threadpool to run sequential functions
# without blocking the main event loop
from fastapi.concurrency import run_in_threadpool

import sophios.post_compile as pc
from sophios.wic_types import Json
from .run_local import build_cmd, copy_output_files


def create_safe_env(user_env: Dict[str, str]) -> dict:
    """Generate a sanitized environment dict without applying it"""
    forbidden = {"PATH", "LD_", "PYTHON", "SECRET_", "BASH_ENV"}
    for key in user_env:
        if any(key.startswith(prefix) for prefix in forbidden):
            raise ValueError(f"Prohibited key: {key}")
    return {**os.environ, **user_env}


async def run_cwl_workflow(
    workflow_name: str,
    basepath: str,
    cwl_runner: str,
    container_cmd: str,
    user_env: Dict[str, str]
) -> Optional[int]:
    """
    Runs the CWL workflow in an environment using asyncio.create_subprocess_exec.

    Args:
        workflow_name (str): Name of the .cwl workflow file to be executed.
        basepath (str): The base path for the workflow execution (e.g., working directory, logs location).
        cwl_runner (str): The command for the CWL runner (e.g., 'cwltool', 'toil-cwl-runner').
        container_cmd (str): The command for the container engine (e.g., 'docker', 'podman').
        user_env (Dict[str, str]): A dictionary of environment variables to set for the subprocess.

    Returns:
        Optional[int]: The exit code of the executed workflow process, or None if an
                       unhandled Python exception occurred before the process could start.
    """

    _supported_runners = ['cwltool', 'toil-cwl-runner']

    if cwl_runner not in _supported_runners:
        raise ValueError(
            f'Invalid or unsupported cwl_runner command! Only these are supported: {list(_supported_runners)}'
        )

    # build_cmd doesn't need to be offloaded
    cmd = build_cmd(workflow_name, basepath, cwl_runner, container_cmd, passthrough_args=[])
    full_cmd_str = ' '.join(cmd)

    retval: Optional[int] = None

    print(f'Running: {full_cmd_str}')
    print('via command line')

    try:
        print(f'Setting environment variables: {user_env}')
        exec_env = create_safe_env(user_env)

        # FIX: Offload blocking mkdir to a threadpool to avoid blocking the asyncio loop
        log_dir = Path(basepath) / 'LOGS'
        await run_in_threadpool(log_dir.mkdir, parents=True, exist_ok=True)

        stdout_log_path = log_dir / 'stdout.txt'
        stderr_log_path = log_dir / 'stderr.txt'

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=exec_env,
            stdin=asyncio.subprocess.DEVNULL,  # disable stdin so that it doesn't hang randomly
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        async def stream_to_file(stream: Any, filename: Path) -> None:
            """Helper to asynchronously stream content from a subprocess pipe to a file."""
            async with aiofiles.open(filename, mode='wb') as f:
                while True:
                    data = await stream.read(4096)
                    if not data:
                        break
                    await f.write(data)

        await asyncio.gather(
            stream_to_file(proc.stdout, stdout_log_path),
            stream_to_file(proc.stderr, stderr_log_path)
        )

        retval = await proc.wait()

        if retval != 0:
            print(
                f'Workflow "{workflow_name}" finished with non-zero exit code: {retval}')
            try:
                with open(stderr_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    last_lines = f.readlines()[-50:]
                    if last_lines:
                        print("--- Last lines from stderr (for quick debug) ---")
                        for line in last_lines:
                            print(f"  {line.strip()}")
                        print("-------------------------------------------------")
            except Exception as e_read:
                print(
                    f"Could not read stderr log for immediate display: {e_read}")
            print(
                f"Full logs available at: {stdout_log_path} and {stderr_log_path}")

    except Exception as e:
        error_log_path = Path(f'error_{workflow_name}_technical.log')
        print(
            f'Failed to execute workflow "{workflow_name}". See {error_log_path} for detailed technical information.')

        await run_in_threadpool(error_log_path.parent.mkdir, parents=True, exist_ok=True)

        with open(error_log_path, mode='w', encoding='utf-8') as f:
            traceback.print_exc(file=f)

        print(
            f"An unhandled Python exception occurred: {type(e).__name__}: {e}")
        retval = None

    # only copy output files if using cwltool
    if cwl_runner == 'cwltool' and retval == 0:
        await run_in_threadpool(copy_output_files, workflow_name, basepath=basepath)

    return retval


async def run_cwl_serialized(workflow: Json, basepath: str,
                             cwl_runner: str, container_cmd: str,
                             user_env: Dict[str, str]) -> None:
    """Prepare and run compiled and serialized CWL workflow asynchronously

    Args:
        workflow_json (Json): Compiled and serialized CWL workflow
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
        env_commands (List[str]): environment variables and commands
        needed to be run before running the workflow
    """
    workflow_name = workflow['name']
    basepath = basepath.rstrip("/") if basepath != "/" else basepath
    output_dirs = await run_in_threadpool(pc.find_output_dirs, workflow)
    await run_in_threadpool(pc.create_output_dirs, output_dirs, basepath)
    # the creation of basepath parentdir (if it doesn't exist) is necessary here
    await run_in_threadpool(Path(basepath).mkdir, parents=True, exist_ok=True)
    # writing the final cwl workflow file and inputs yml file
    compiled_cwl = workflow_name + '.cwl'
    inputs_yml = workflow_name + '_inputs.yml'
    # write _input.yml file
    await run_in_threadpool(yaml.dump, workflow['yaml_inputs'],
                            open(Path(basepath) / inputs_yml, 'w', encoding='utf-8'))

    # clean up the object of tags and data that we don't need anymore
    workflow.pop('retval', None)
    workflow.pop('yaml_inputs', None)
    workflow.pop('name', None)

    # write compiled .cwl file
    await run_in_threadpool(yaml.dump, workflow,
                            open(Path(basepath) / compiled_cwl, 'w', encoding='utf-8'))

    retval = await run_cwl_workflow(workflow_name, basepath,
                                    cwl_runner, container_cmd, user_env=user_env)
    assert retval == 0
