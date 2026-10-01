import json
import subprocess as sub
import sys
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Mapping
from sophios.ir.names import Names
from sophios.ir.types import DerivedName, WorkflowGraph
from sophios.wic_types import Json
from .compute_request import ComputeRequest

try:
    import cwltool.main
    import toil.cwl.cwltoil  # transitively imports cwltool
except ImportError as exc:
    print('Could not import cwltool.main and/or toil.cwl.cwltoil')
    # (pwd is imported transitively in cwltool.provenance)
    print(exc)
    if exc.msg == "No module named 'pwd'":
        print('Windows does not have a pwd module')
        print('If you want to run on windows, you need to install')
        print('Windows Subsystem for Linux')
        print('See https://pypi.org/project/cwltool/#ms-windows-users')
    else:
        raise exc

from . import auto_gen_header
from . import utils  # , utils_graphs
from .plugins import logging_filters


@dataclass(frozen=True, slots=True)
class _CompiledWorkflowForCompute:
    name: str
    cwl_workflow: Json
    cwl_job_inputs: Json


def _sanitize_env_vars(env_vars: dict[str, str]) -> dict[str, str]:
    """Drop keys that aren't valid Bash variable names and strip dangerous characters from values."""
    sanitized = {}

    # Regex for a valid Bash variable name
    valid_key_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

    # Characters to remove from values to prevent command injection
    dangerous_chars_pattern = re.compile(r'[;`\'"$()|<>&!\n\r]')

    for key, value in env_vars.items():
        # Step 1: Validate the key.
        if not valid_key_pattern.fullmatch(key):
            print(
                f"Warning: Invalid environment variable key '{key}' skipped.")
            continue

        # Step 2: Sanitize the value.
        sanitized_value = dangerous_chars_pattern.sub('', value)
        sanitized[key] = sanitized_value

    return sanitized


def create_safe_env(user_env: dict[str, str]) -> dict:
    """Generate a sanitized environment dict without applying it"""
    sanitized_user_env = _sanitize_env_vars(user_env)
    return {**os.environ, **sanitized_user_env}


@contextmanager
def _temporary_env(user_env: dict[str, str]) -> Iterator[dict[str, str]]:
    """Temporarily apply sanitized environment variables and restore them after use."""
    sanitized_user_env = _sanitize_env_vars(user_env)
    previous_values = {key: os.environ.get(key) for key in sanitized_user_env}
    os.environ.update(sanitized_user_env)
    try:
        yield {**os.environ}
    finally:
        for key, previous_value in previous_values.items():
            if previous_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous_value


def generate_run_script(cmdline: str) -> None:
    """Writes the command used to invoke the cwl-runner to run.sh
    Does not actually invoke ./run.sh

    Args:
        cmdline (str): The command line which invokes the cwl-runner
    """
    with open('run.sh', mode='w', encoding='utf-8') as f:
        f.write('#!/bin/bash -e\n')
        f.write(auto_gen_header)
        f.write(cmdline)
    # chmod +x run.sh
    st = os.stat('run.sh')
    os.chmod('run.sh', st.st_mode | stat.S_IEXEC)


def _runner_outdir(basepath: str, cwl_runner: str, date_time: str, outdir: str | None) -> str:
    """Return the explicit or default output directory for a CWL runner."""
    if outdir:
        return str(Path(outdir).absolute().resolve())
    runner_name = 'cwltool' if cwl_runner == 'cwltool' else 'toil'
    return f'{basepath}/outdir_{runner_name}_{date_time}'


def build_cmd(workflow_name: str, basepath: str, cwl_runner: str,
              container_cmd: str, passthrough_args: list[str], outdir: str | None = None,
              quiet: bool = True) -> list[str]:
    """Build the command to run the workflow in an environment

    Args:
        workflow_name (str): Name of the .cwl workflow file to be executed
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
        quiet (bool): Pass --quiet to cwltool. Turn it off so --debug and the runner's own log
        level reach it. toil-cwl-runner is never given --quiet.
    Returns:
        cmd (list[str]): The command to run the workflow
    """
    basepath = str(Path(basepath).absolute().resolve())
    quiet_flags = ['--quiet'] if quiet else []
    # NOTE: By default, cwltool will attempt to download schema files.
    # $schemas:
    #   - https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl
    # If you have connection issues (e.g. firewall, VPN, etc) then failure to download will
    # not actually cause any problems immediately (except ~30s timeout).
    # However, cwltool does not appear to cache these files, so it will attempt to download them repeatedly.
    # These ~30 second timeouts will eventually add up to >6 hours, which will cause github to terminate the CI Action!
    skip_schemas = ['--skip-schemas']
    provenance = ['--provenance', f'{basepath}/provenance/{workflow_name}']
    container_cmd_: list[str] = []
    if container_cmd == 'docker':
        container_cmd_ = []
    elif container_cmd == 'singularity':
        container_cmd_ = ['--singularity']
    else:
        container_cmd_ = ['--user-space-docker-cmd', container_cmd]
    write_summary = ['--write-summary',
                     f'{basepath}/output_{workflow_name}.json']
    path_check = ['--relax-path-checks']
    now = datetime.now()
    date_time = now.strftime("%Y_%m_%d_%H.%M.%S")
    runner_outdir = _runner_outdir(basepath, cwl_runner, date_time, outdir)
    # NOTE: Using --leave-outputs to disable --outdir
    # See https://github.com/dnanexus/dx-cwl/issues/20
    # --outdir has one or more bugs which will cause workflows to fail!!!
    # Use cwl-docker-extract to pull images
    container_pull = ['--disable-pull']
    script = 'cwltool_filterlog' if cwl_runner == 'cwltool' else cwl_runner
    cmd = [script] + container_pull + quiet_flags + provenance + \
        container_cmd_ + write_summary + skip_schemas + path_check
    if cwl_runner == 'cwltool':
        cmd += ['--move-outputs', '--enable-ext',
                '--outdir', runner_outdir]
        cmd += passthrough_args
        cmd += [f'{basepath}/{workflow_name}.cwl',
                f'{basepath}/{workflow_name}_inputs.yml']
    elif cwl_runner == 'toil-cwl-runner':
        cmd = [script] + container_cmd_ + path_check
        if 'slurm' not in passthrough_args:
            cmd += provenance

        cmd += ['--outdir', runner_outdir,
                # NOTE: This is the equivalent of --cachedir
                '--jobStore', f'file:{basepath}/jobStore_{workflow_name}',
                '--clean', 'always',
                '--noLinkImports',
                '--disableProgress',  # disable the progress bar in the terminal, saves UI cycles
                ]
        cmd += passthrough_args
        cmd += [f'{basepath}/{workflow_name}.cwl',
                f'{basepath}/{workflow_name}_inputs.yml']
    return cmd


def _execute_inprocess(cmd: list[str], cwl_runner: str, workflow_name: str,
                       run_args_dict: dict[str, str], user_env_vars: dict[str, str] | None,
                       yaml_path: Path, cachedir: str,
                       output_directories: Mapping[str, str] | None) -> int:
    """Execute the workflow in-process via the cwltool or toil python API, handling errors."""
    retval = 1
    try:
        with _temporary_env(user_env_vars or {}):
            if cwl_runner == 'cwltool':
                print('via cwltool.main.main python API')
                retval = cwltool.main.main(cmd[1:])
                print(
                    f'Final output json metadata blob is in output_{workflow_name}.json')
                if run_args_dict.get('copy_output_files', 'no') == 'yes':
                    copy_output_files(workflow_name, output_directories=output_directories)
            elif cwl_runner == 'toil-cwl-runner':
                print('via toil.cwl.cwltoil.main python API')
                retval = toil.cwl.cwltoil.main(cmd[1:])
            else:
                raise ValueError('unsupported cwl_runner')

    except Exception as e:
        retval = 1
        print('Failed to execute', yaml_path)
        print(
            f'See error_{workflow_name}.txt for detailed technical information.')
        # Do not display a nasty stack trace to the user; hide it in a file.
        with open(f'error_{workflow_name}.txt', mode='w', encoding='utf-8') as f:
            traceback.print_exception(type(e), value=e, tb=None, file=f)
        if not cachedir:  # if running on CI
            print(e)
    return retval


def _report_outcome(retval: int | None, cmd: list[str], basepath: str) -> None:
    """Print the success/failure summary message after execution."""
    if retval == 0:
        output_location = cmd[cmd.index('--outdir') + 1] if '--outdir' in cmd else basepath
        print(f'Success! Runner outputs are under {output_location}/')
    else:
        print('Failure! Please scroll up and find the FIRST error message.')
        print('(You may have to scroll up A LOT.)')


def run_local(run_args_dict: dict[str, str], use_subprocess: bool,
              passthrough_args: list[str], workflow_name: str,
              basepath: str, user_env_vars: dict[str, str] | None = None,
              output_directories: Mapping[str, str] | None = None) -> int:
    """This function runs the compiled workflow locally.

    Args:
        run_args_dict (dict[str,str]): The command line arguments dict for run_local.
        Its 'quiet' is 'yes' (the default) or 'no'.
        use_subprocess (bool): When using cwltool, determines whether to use subprocess.run(...)
        or use the cwltool python api.
        basepath (str): The path at which the workflow to be executed
        user_env_vars (dict[str, str] | None): User supplied environment variables.
        output_directories (Mapping[str, str] | None): Passed to `copy_output_files`.

    Returns:
        retval (int): 0 on success, else the runner's exit code
    """
    yaml_path = Path(basepath) / workflow_name
    cwl_runner = run_args_dict['cwl_runner']
    # 'cachedir' is the default value
    cachedir = run_args_dict.get('cachedir', 'cachedir')
    container_engine = run_args_dict['container_engine']

    # build the runner command
    cmd = build_cmd(workflow_name, basepath, cwl_runner,
                    container_engine, passthrough_args, run_args_dict.get('outdir') or None,
                    quiet=run_args_dict.get('quiet', 'yes') == 'yes')
    cmdline = ' '.join(cmd)
    exec_env = create_safe_env(user_env_vars or {})

    if run_args_dict.get('generate_run_script', 'no') == 'yes':
        generate_run_script(cmdline)
        return 0  # Do not actually run

    print('Running ' + cmdline)
    if use_subprocess:
        # To run in parallel (i.e. pytest ... --workers 8 ...), we need to
        # use separate processes. Otherwise:
        # "signal only works in main thread or with __pypy__.thread.enable_signals()"
        proc = sub.run(cmd, check=False, env=exec_env)
        return proc.returncode  # Skip copying files to outdir/ for CI

    retval = _execute_inprocess(cmd, cwl_runner, workflow_name, run_args_dict,
                                user_env_vars, yaml_path, cachedir, output_directories)

    _report_outcome(retval, cmd, basepath)

    return retval


def run_compute(workflow_name: str, workflow: Json, workflow_inputs: Json,
                submit_url: str) -> int | None:
    """Submit a compiled workflow to compute-slurm.

    Args:
        workflow_name (str): The name of the workflow.
        workflow (Json): The compiled CWL workflow.
        workflow_inputs (Json): The inputs for compiled CWL workflow.
        submit_url (str): URL of Compute where the job is to be submitted.
    Returns:
        int | None: The return value indicating if submission succeeded (`0`) or not.
    """
    now = datetime.now()
    date_time = now.strftime("%Y_%m_%d_%H.%M.%S")
    jobid = workflow_name + '__' + str(date_time) + '__'
    compute_request = ComputeRequest(
        _CompiledWorkflowForCompute(workflow_name, workflow, workflow_inputs),
        workflow_id=jobid,
    )

    # sanity check if the string has the form of an URL
    if not utils.is_valid_url(submit_url):
        print("Ill-formed URL string detected! Please provide a valid URL")
        return 1

    submission = compute_request.submit(
        submit_url,
        log_path=Path(f'compute_logs_{jobid}.txt'),
    )
    return submission.exit_code


def output_directories(graph: WorkflowGraph) -> dict[str, str]:
    """Where `copy_output_files` puts each of `graph`'s outputs, under `outdir/`.

    A derived output sits below the root workflow's name in one
    `step <i> <name>` directory per step it was exposed through, outermost
    first, numbered as the document numbers them; an authored output is its
    own directory. Keyed by the name the emitted document gives the output,
    which is the key the runner's provenance JSON uses.

    Args:
        graph (WorkflowGraph): The compiled root graph.

    Returns:
        dict[str, str]: Each root output's directory, relative to `outdir/`.
    """
    names = Names.of(graph)
    directories: dict[str, str] = {}
    for port in graph.workflow_outputs:
        name, parts = port.name, []
        while isinstance(name, DerivedName):
            parts.append(f'step {names.position(name.step)} {name.step.name}')
            name = name.port
        directories[names.port(port.name)] = '/'.join((graph.name, *parts, name)) if parts else name
    return directories


def copy_output_files(yaml_stem: str, basepath: str = '',
                      output_directories: Mapping[str, str] | None = None) -> None:
    """Copies output files from the cachedir to outdir/

    Args:
        yaml_stem (str): The --yaml filename (without .extension)
        output_directories (Mapping[str, str] | None): Each root output's directory under
            `outdir/`, as `output_directories()` computes it. An output it does not name is
            copied to `outdir/<its name>`.
    """
    output_json_file_prov = Path(
        f'provenance/{yaml_stem}/workflow/primary-output.json')
    # NOTE: The contents of --write-summary is
    # slightly different than provenance/{yaml_stem}/workflow/primary-output.json!
    # They are NOT the same file!
    if output_json_file_prov.exists():
        with open(output_json_file_prov, mode='r', encoding='utf-8') as f:
            output_json = json.loads(f.read())
        directories = output_directories or {}
        files = [file for name, obj in output_json.items()
                 for file in utils.parse_provenance_output_files(obj, directories.get(name, name))]
        dests: set[str] = set()
        for location, parentdirs, basename in files:
            Path('outdir/' + parentdirs).mkdir(parents=True, exist_ok=True)
            source = f'provenance/{yaml_stem}/workflow/' + location
            # NOTE: Even though we are using subdirectories (not just a single output directory),
            # there is still the possibility of filename collisions, i.e. when scattering.
            # For now, let's use a similar trick as cwltool of append _2, _3 etc.
            # except do it BEFORE the extension.
            # This could still cause problems with slicing, i.e. if you scatter across
            # indices 11-20 first, then 1-10 second, the output file indices will get switched.
            if basepath:
                dest = basepath + '/' + 'outdir/' + parentdirs + '/' + basename
            else:
                dest = 'outdir/' + parentdirs + '/' + basename
            if dest in dests:
                idx = 2
                while Path(dest).exists():
                    stem = Path(basename).stem
                    suffix = Path(basename).suffix
                    dest = 'outdir/' + parentdirs + \
                        '/' + stem + f'_{idx}' + suffix
                    idx += 1
            dests.add(dest)
            cmd = ['cp', source, dest]
            sub.run(cmd, check=True)


def cwltool_main() -> int:
    """Another entrypoint for filtering regular logged output"""
    logging_filters()
    cmd = sys.argv
    retval: int = cwltool.main.main(cmd[1:])
    return retval


def cwltool_main_pf() -> int:
    """Another entrypoint for filtering partial failures logged output"""
    logging_filters(True)
    cmd = sys.argv
    retval: int = cwltool.main.main(cmd[1:])
    return retval
