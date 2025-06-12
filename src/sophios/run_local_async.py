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


async def run_cwl_workflow(workflow_name: str, basepath: str,
                           cwl_runner: str, container_cmd: str,
                           user_env: Dict[str, str]) -> Optional[int]:
    """Run the CWL workflow in an environment

    Args:
        workflow_name (str): Name of the .cwl workflow file to be executed
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
        use_subprocess (bool): When using cwltool, determines whether to use subprocess.run(...)
        or use the cwltool python api.
        env_commands (List[str]): environment variables and commands needed to be run before running the workflow
    Returns:
        retval: The return value
    """
    cmd = await run_in_threadpool(build_cmd, workflow_name, basepath, cwl_runner, container_cmd)

    retval = 1  # overwrite on success
    print('Running ' + (' '.join(cmd)))
    print('via command line')
    runner_cmnds = ['cwltool', 'toil-cwl-runner']
    try:
        if cwl_runner in runner_cmnds:
            print(f'Setting env vars : {user_env}')
            exec_env = create_safe_env(user_env)

            proc = await asyncio.create_subprocess_exec(*cmd,
                                                        env=exec_env,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)

            async def stream_to_file(stream: Any, filename: Path) -> None:
                filename.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(filename, mode='wb') as f:
                    while True:
                        data = await stream.read(1024)  # 1KB chunks
                        if not data:
                            break
                        await f.write(data)

            await asyncio.gather(
                stream_to_file(proc.stdout, Path(basepath) / 'LOGS' / 'stdout.txt'),
                stream_to_file(proc.stderr, Path(basepath) / 'LOGS' / 'stderr.txt')
            )
            retval = await proc.wait()
        else:
            raise ValueError(
                f'Invalid or Unsupported cwl_runner command! Only these are the supported runners {runner_cmnds}')

    except Exception as e:
        print('Failed to execute', workflow_name)
        print(
            f'See error_{workflow_name}.txt for detailed technical information.')
        # Do not display a nasty stack trace to the user; hide it in a file.
        with open(f'error_{workflow_name}.txt', mode='w', encoding='utf-8') as f:
            traceback.print_exception(type(e), value=e, tb=None, file=f)
        print(e)  # we are always running this on CI
    # only copy output files if using cwltool
    if cwl_runner == 'cwltool':
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
    output_dirs = pc.find_output_dirs(workflow)
    pc.create_output_dirs(output_dirs, basepath)
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
