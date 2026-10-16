import logging
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
from typing import Final, Iterator, Mapping
from sophios.wic_types import Json
from . import auto_gen_header, realtime
from . import utils
from .compute_request import ComputeRequest
from .plugins import FailedJobs, logging_filters

#: Why cwltool and Toil cannot run in this process, or None. Windows has no `pwd`, which they import
#: (transitively, in cwltool.provenance); the pre-flight says so when a run is asked for.
RUNNER_UNAVAILABLE: str | None = None
try:
    import cwltool.main
    import toil.cwl.cwltoil  # transitively imports cwltool
except ModuleNotFoundError as exc:
    if exc.name != 'pwd':
        raise
    RUNNER_UNAVAILABLE = 'cwltool and Toil need the pwd module, which Windows does not have'


@dataclass(frozen=True, slots=True)
class _CompiledWorkflowForCompute:
    name: str
    cwl_workflow: Json
    cwl_job_inputs: Json


_ENV_VAR_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _check_env_var_names(env_vars: Mapping[str, str]) -> None:
    """Raise ValueError naming each key that is not an environment variable name.

    Values are not checked: they reach the runner through an environment mapping and never
    through a shell, so every character in them is passed as given.
    """
    invalid = [key for key in env_vars if not _ENV_VAR_NAME.fullmatch(key)]
    if invalid:
        names = ', '.join(repr(key) for key in invalid)
        raise ValueError(f'Not an environment variable name: {names}. A name is letters, digits '
                         'and underscores, and does not start with a digit.')


def create_safe_env(user_env: dict[str, str]) -> dict:
    """Return the current environment with the user's variables added, without applying it."""
    _check_env_var_names(user_env)
    return {**os.environ, **user_env}


@contextmanager
def _temporary_env(user_env: dict[str, str]) -> Iterator[dict[str, str]]:
    """Temporarily apply the user's environment variables and restore them after use."""
    _check_env_var_names(user_env)
    previous_values = {key: os.environ.get(key) for key in user_env}
    os.environ.update(user_env)
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


def _runner_outdir(basepath: str, workflow_name: str, cwl_runner: str, date_time: str, outdir: str | None) -> str:
    """Return the explicit output directory, else the default one.

    The default is named after the workflow, like every other path a run writes (provenance, the
    summary, the job store), so two workflows started in the same second from one basepath never
    share it.
    """
    if outdir:
        return str(Path(outdir).absolute().resolve())
    runner_name = 'cwltool' if cwl_runner == 'cwltool' else 'toil'
    return f'{basepath}/outdir_{runner_name}_{workflow_name}_{date_time}'


def _container_flags(container_cmd: str) -> list[str]:
    """The runner flags that select the container engine `container_cmd`."""
    if container_cmd == 'docker':
        return []
    if container_cmd == 'singularity':
        return ['--singularity']
    return ['--user-space-docker-cmd', container_cmd]


def _analysis_command(container_cmd: str) -> realtime.Command:
    """How a real-time analysis is run: cwltool in a subprocess, with this run's container engine.

    Its images were pulled with the workflow's, so it does not pull; it keeps no provenance.
    This interpreter's cwltool, and not `python -m cwltool`, which exits 0 whatever happened.
    """
    def command(cwl: Path, job: Path, outdir: Path, cachedir: Path) -> list[str]:
        return [sys.executable, '-c', 'import sys, cwltool.main; sys.exit(cwltool.main.run())',
                '--disable-pull', '--skip-schemas', '--relax-path-checks', '--enable-ext',
                *_container_flags(container_cmd), '--cachedir', str(cachedir), '--outdir', str(outdir),
                str(cwl), str(job)]
    return command


def build_cmd(workflow_name: str, basepath: str, cwl_runner: str,
              container_cmd: str, passthrough_args: list[str], outdir: str | None = None,
              quiet: bool = True, documents: tuple[str, ...] | None = None,
              cachedir: str | None = None) -> list[str]:
    """Build the command to run the workflow in an environment

    Args:
        workflow_name (str): Name of the .cwl workflow file to be executed
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
        quiet (bool): Pass --quiet to cwltool. Turn it off so --debug and the runner's own log
        level reach it. toil-cwl-runner is never given --quiet.
        documents (tuple[str, ...] | None): The workflow, then its job file if it has one, as
        the runner is given them. By default `<basepath>/<workflow_name>.cwl` and
        `<basepath>/<workflow_name>_inputs.yml`, the files Sophios wrote.
        cachedir (str | None): Give cwltool this directory as `--cachedir`, so a job whose tool
        and inputs are unchanged reuses its cached outputs. None: no cache. Toil keeps its own
        job store and is not given it.
    Returns:
        cmd (list[str]): The command to run the workflow
    """
    basepath = str(Path(basepath).absolute().resolve())
    if documents is None:
        documents = (f'{basepath}/{workflow_name}.cwl', f'{basepath}/{workflow_name}_inputs.yml')
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
    container_cmd_ = _container_flags(container_cmd)
    write_summary = ['--write-summary',
                     f'{basepath}/output_{workflow_name}.json']
    path_check = ['--relax-path-checks']
    now = datetime.now()
    date_time = now.strftime("%Y_%m_%d_%H.%M.%S")
    runner_outdir = _runner_outdir(basepath, workflow_name, cwl_runner, date_time, outdir)
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
        if cachedir:
            cmd += ['--cachedir', str(Path(cachedir).absolute())]
        cmd += passthrough_args
        cmd += list(documents)
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
        cmd += list(documents)
    return cmd


def _execute_inprocess(cmd: list[str], cwl_runner: str, workflow_name: str,
                       user_env_vars: dict[str, str] | None,
                       yaml_path: Path, failed: FailedJobs) -> int:
    """Execute the workflow in-process via the cwltool or toil python API, handling errors.

    `failed` collects the jobs cwltool reports as not successful.
    """
    retval = 1
    logger = logging.getLogger('cwltool')
    logger.addFilter(failed)
    try:
        with _temporary_env(user_env_vars or {}):
            if cwl_runner == 'cwltool':
                print('via cwltool.main.main python API')
                try:
                    retval = cwltool.main.main(cmd[1:])
                except KeyboardInterrupt:
                    # cwltool handles SIGTERM itself and SIGINT not at all: the `docker run`
                    # children it tracks would keep running after we are gone. Clean up, then
                    # let the interrupt through: what Ctrl-C means is the caller's call.
                    cwltool.main._terminate_processes()  # pylint: disable=protected-access
                    print('Interrupted; the runner\'s processes were terminated.', file=sys.stderr)
                    raise
                print(
                    f'Final output json metadata blob is in output_{workflow_name}.json')
            elif cwl_runner == 'toil-cwl-runner':
                print('via toil.cwl.cwltoil.main python API')
                retval = toil.cwl.cwltoil.main(cmd[1:])
            else:
                raise ValueError('unsupported cwl_runner')

    except Exception as e:
        retval = 1
        print('Failed to execute', yaml_path)
        print(e)
        print(
            f'See error_{workflow_name}.txt for detailed technical information.')
        # Do not display a nasty stack trace to the user; hide it in a file.
        with open(f'error_{workflow_name}.txt', mode='w', encoding='utf-8') as f:
            traceback.print_exception(e, file=f)
    finally:
        logger.removeFilter(failed)
    return retval


def _runnable(plans: tuple[realtime.Plan, ...], run_args_dict: dict[str, str]) -> tuple[realtime.Plan, ...]:
    """`plans`, if this run can run them; else a line saying why not."""
    if plans and run_args_dict.get('generate_run_script', 'no') == 'yes':
        print('Real-time analysis runs only with --run_local; run.sh runs the workflow without it.',
              file=sys.stderr)
        return ()
    if plans and run_args_dict['cwl_runner'] != 'cwltool':
        print(f'Real-time analysis needs cwltool; {run_args_dict["cwl_runner"]} runs the workflow without it.',
              file=sys.stderr)
        return ()
    return plans


#: What an exit status means on its own (shell conventions), said after it.
_STATUS_MEANS: Final = {
    126: 'its command could not be executed (not executable, or built for another platform)',
    127: "its command was not found: the tool's baseCommand is not installed in its container image, "
         'or on this machine for a tool with none',
    137: 'it was killed, usually for running out of memory: give the container engine more memory '
         '(Docker Desktop: Settings, Resources), or the step less input',
}


def _failure_lines(failed: Mapping[str, int | str | None]) -> list[str]:
    """One line per failed step: who, how it ended, what that means, where its messages are."""
    if not failed:
        return ['Failure! Above, the first ERROR line, or the error the runner itself raised, says why the run failed.']
    lines = []
    for job, ended in failed.items():
        means: str | None
        if isinstance(ended, int):
            how, means = f'exited with status {ended}', _STATUS_MEANS.get(ended)
        elif ended is not None:
            how, means = f'was ended by {ended}', _STATUS_MEANS[137] if ended == 'SIGKILL' else None
        else:
            how, means = 'could not run, or its outputs could not be collected', None
        lines.append(f"Failure! step '{job}' {how}" + (f', which means {means}' if means else '')
                     + '. Its own messages are above, under its name.')
    return lines


def _report_outcome(retval: int | None, cmd: list[str], basepath: str, failed: FailedJobs) -> None:
    """Print the success/failure summary message after execution."""
    if retval == 0:
        output_location = cmd[cmd.index('--outdir') + 1] if '--outdir' in cmd else basepath
        print(f'Success! Runner outputs are under {output_location}/')
    else:
        for line in _failure_lines(failed.failed):
            print(line, file=sys.stderr)


def run_local(run_args_dict: dict[str, str], use_subprocess: bool,
              passthrough_args: list[str], workflow_name: str,
              basepath: str, user_env_vars: dict[str, str] | None = None,
              documents: tuple[str, ...] | None = None,
              realtime_plans: tuple[realtime.Plan, ...] = ()) -> int:
    """This function runs the compiled workflow locally.

    Args:
        run_args_dict (dict[str,str]): The command line arguments dict for run_local.
        Its 'quiet' is 'yes' (the default) or 'no'. Its 'cachedir', when not empty, is
        passed to cwltool as `--cachedir`.
        use_subprocess (bool): When using cwltool, determines whether to use subprocess.run(...)
        or use the cwltool python api.
        basepath (str): The path at which the workflow to be executed
        user_env_vars (dict[str, str] | None): User supplied environment variables.
        documents (tuple[str, ...] | None): Passed to `build_cmd`.
        realtime_plans (tuple[realtime.Plan, ...]): The real-time analyses to run beside the
        workflow, as `realtime.write` returned them for it. With any, cwltool gets a `--cachedir`.

    Returns:
        retval (int): 0 on success, else the runner's exit code

    Raises:
        KeyboardInterrupt: On Ctrl-C during an in-process run. With cwltool, after the runner's
            child processes have been terminated.
    """
    yaml_path = Path(basepath) / workflow_name
    cwl_runner = run_args_dict['cwl_runner']
    cachedir = run_args_dict.get('cachedir', '')
    container_engine = run_args_dict['container_engine']
    plans = _runnable(realtime_plans, run_args_dict)
    if plans and not cachedir:
        cachedir = 'cachedir'
        print(f'Real-time analysis watches the runner\'s cache, so cwltool caches in {cachedir}/')

    # build the runner command
    cmd = build_cmd(workflow_name, basepath, cwl_runner,
                    container_engine, passthrough_args, run_args_dict.get('outdir') or None,
                    quiet=run_args_dict.get('quiet', 'yes') == 'yes', documents=documents,
                    cachedir=cachedir or None)
    cmdline = ' '.join(cmd)
    exec_env = create_safe_env(user_env_vars or {})

    if run_args_dict.get('generate_run_script', 'no') == 'yes':
        generate_run_script(cmdline)
        return 0  # Do not actually run

    print('Running ' + cmdline)
    failed = FailedJobs()
    with realtime.watching(plans, Path(cachedir), _analysis_command(container_engine), env=exec_env):
        if use_subprocess:
            # To run in parallel (i.e. pytest ... --workers 8 ...), we need to
            # use separate processes. Otherwise:
            # "signal only works in main thread or with __pypy__.thread.enable_signals()"
            retval = sub.run(cmd, check=False, env=exec_env).returncode
        else:
            retval = _execute_inprocess(cmd, cwl_runner, workflow_name,
                                        user_env_vars, yaml_path, failed)
    if use_subprocess:
        return retval

    _report_outcome(retval, cmd, basepath, failed)

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
