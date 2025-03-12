import argparse
import glob
import json
import subprocess as sub
import sys
import os
import stat
from pathlib import Path
import shutil
import platform
import traceback
import yaml
from typing import Dict, List, Optional
from datetime import datetime
import sophios.post_compile as pc
from sophios.wic_types import Json

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
from .wic_types import Yaml, RoseTree
from .plugins import logging_filters


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


def run_local(args: argparse.Namespace, rose_tree: RoseTree, cachedir: Optional[str], cwl_runner: str,
              use_subprocess: bool, passthrough_args: List[str] = []) -> int:
    """This function runs the compiled workflow locally.

    Args:
        args (argparse.Namespace): The command line arguments
        rose_tree (RoseTree): The compiled workflow
        cachedir (Optional[str]): The --cachedir to use (if any)
        cwl_runner (str): Either 'cwltool' or 'toil-cwl-runner'
        use_subprocess (bool): When using cwltool, determines whether to use subprocess.run(...)
        or use the cwltool python api.

    Returns:
        retval: The return value
    """
    docker_like_engines = ['docker', 'podman']
    docker_cmd: str = args.container_engine
    # Check that docker is installed, so users don't get a nasty runtime error.
    if docker_cmd in docker_like_engines:
        cmd = [docker_cmd, 'run', '--rm', 'hello-world']
        output = ''
        try:
            docker_cmd_exists = True
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT)
            output = proc.stdout.decode("utf-8")
        except FileNotFoundError:
            docker_cmd_exists = False
        out_d = "Hello from Docker!"
        out_p = "Hello Podman World"
        permission_denied = 'permission denied while trying to connect to the Docker daemon socket at'
        if ((not docker_cmd_exists
            or not (proc.returncode == 0 and out_d in output or out_p in output))
                and not args.ignore_docker_install):

            if permission_denied in output:
                print('Warning! docker appears to be installed, but not configured as a non-root user.')
                print('See https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user')
                print('TL;DR you probably just need to run the following command (and then restart your machine)')
                print('sudo usermod -aG docker $USER')
                sys.exit(1)

            print(f'Warning! The {docker_cmd} command does not appear to be installed.')
            print(f"""Most workflows require docker containers and
                  will fail at runtime if {docker_cmd} is not installed.""")
            print('If you want to try running the workflow anyway, use --ignore_docker_install')
            print("""Note that --ignore_docker_install does
                  NOT change whether or not any step in your workflow uses docker""")
            sys.exit(1)

        # If docker is installed, check for too many running processes. (on linux, macos)
        if docker_cmd_exists and sys.platform != "win32":
            cmd = 'pgrep com.docker | wc -l'  # type: ignore
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT, shell=True)
            output = proc.stdout.decode("utf-8")
            num_processes = int(output.strip())
            max_processes = 1000
            if num_processes > max_processes and not args.ignore_docker_processes:
                print(f'Warning! There are {num_processes} running docker processes.')
                print(f'More than {max_processes} may potentially cause intermittent hanging issues.')
                print('It is recommended to terminate the processes using the command')
                print('`sudo pkill com.docker && sudo pkill Docker`')
                print('and then restart Docker.')
                print('If you want to run the workflow anyway, use --ignore_docker_processes')
                sys.exit(1)
    else:
        cmd = [docker_cmd, '--version']
        output = ''
        try:
            docker_cmd_exists = True
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT)
            output = proc.stdout.decode("utf-8")
        except FileNotFoundError:
            docker_cmd_exists = False
        if not docker_cmd_exists and not args.ignore_docker_install:
            print(f'Warning! The {docker_cmd} command does not appear to be installed.')
            print('If you want to try running the workflow anyway, use --ignore_docker_install')
            print('Note that --ignore_docker_install does NOT change whether or not')
            print('any step in your workflow uses docker or any other containers')
            sys.exit(1)

    yaml_path = args.yaml
    yaml_stem = Path(args.yaml).stem

    yaml_inputs = rose_tree.data.workflow_inputs_file
    stage_input_files(yaml_inputs, Path(args.yaml).parent.absolute())

    retval = 1  # overwrite if successful
    provenance: List[str] = []
    # NOTE: By default, cwltool will attempt to download schema files.
    # $schemas:
    #   - https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl
    # If you have connection issues (e.g. firewall, VPN, etc) then failure to download will
    # not actually cause any problems immediately (other than a ~30 second timeout).
    # However, cwltool does not appear to cache these files, so it will attempt to download
    # them repeatedly. These ~30 second timeouts will eventually add up to >6 hours, which
    # will cause github to terminate the CI Action...
    skip_schemas = ['--skip-schemas'] if not args.no_skip_dollar_schemas else []

    yaml_stem = yaml_stem + '_inline' if args.cwl_inline_subworkflows else yaml_stem
    if cwl_runner == 'cwltool':
        parallel = ['--parallel'] if args.parallel else []
        # NOTE: --parallel is required for real-time analysis / real-time plots,
        # but it seems to cause hanging with Docker for Mac. The hanging seems
        # to be worse when using parallel scattering.
        quiet = ['--quiet'] if args.quiet else []
        cachedir_ = ['--cachedir', cachedir] if cachedir else []
        net = ['--custom-net', args.custom_net] if args.custom_net else []
        provenance = ['--provenance', f'provenance/{yaml_stem}'] if not args.no_provenance else []
        docker_cmd_: List[str] = []
        if docker_cmd == 'docker':
            docker_cmd_ = []
        elif docker_cmd == 'singularity':
            docker_cmd_ = ['--singularity']
        else:
            docker_cmd_ = ['--user-space-docker-cmd', docker_cmd]
        write_summary = ['--write-summary', f'output_{yaml_stem}.json']
        path_check = ['--relax-path-checks']
        # See https://github.com/common-workflow-language/cwltool/blob/5a645dfd4b00e0a704b928cc0bae135b0591cc1a/cwltool/command_line_tool.py#L94
        # NOTE: Using --leave-outputs to disable --outdir
        # See https://github.com/dnanexus/dx-cwl/issues/20
        # --outdir has one or more bugs which will cause workflows to fail!!!
        docker_pull = ['--disable-pull']  # Use cwl-docker-extract to pull images
        script = 'cwltool_filterlog_pf' if args.partial_failure_enable else 'cwltool_filterlog'
        cmd = [script] + docker_pull + parallel + quiet + cachedir_ + net + provenance + \
            docker_cmd_ + write_summary + skip_schemas + path_check
        cmd += ['--leave-outputs',
                # '--js-console', # "Running with support for javascript console in expressions
                # (DO NOT USE IN PRODUCTION)"
                f'autogenerated/{yaml_stem}.cwl', f'autogenerated/{yaml_stem}_inputs.yml']
        # TODO: Consider using the undocumented flag --fast-parser for known-good workflows,
        # which was recently added in the 3.1.20220913185150 release of cwltool.
        cmdline = ' '.join(cmd)

        if args.generate_run_script:
            generate_run_script(cmdline)
            return 0  # Do not actually run

        # If we are actually running, then enable --copy_output_files
        args.copy_output_files = True

        print('Running ' + cmdline)
        if use_subprocess:
            # To run in parallel (i.e. pytest ... --workers 8 ...), we need to
            # use separate processes. Otherwise:
            # "signal only works in main thread or with __pypy__.thread.enable_signals()"
            proc = sub.run(cmd, check=False)
            retval = proc.returncode
            return retval  # Skip copying files to outdir/ for CI
        else:
            print('via cwltool.main.main python API')
            try:
                retval = cwltool.main.main(cmd[1:])

                print(f'Final output json metadata blob is in output_{yaml_stem}.json')

                # See https://pypi.org/project/cwltool/#import-as-a-module
                # This also works, but doesn't easily allow using --leave-outputs, --provenence, --cachedir
                # import cwltool.factory
                # fac = cwltool.factory.Factory()
                # rootworkflow = fac.make(f'autogenerated/{yaml_stem}.cwl')
                # output_json = rootworkflow(**yaml_inputs)
                # with open('primary-output.json', mode='w', encoding='utf-8') as f:
                #     f.write(json.dumps(output_json))
            except Exception as e:
                print('Failed to execute', yaml_path)
                print(f'See error_{yaml_stem}.txt for detailed technical information.')
                # Do not display a nasty stack trace to the user; hide it in a file.
                with open(f'error_{yaml_stem}.txt', mode='w', encoding='utf-8') as f:
                    # https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
                    if sys.version_info >= (3, 10):
                        traceback.print_exception(type(e), value=e, tb=None, file=f)
                    else:
                        traceback.print_exception(etype=type(e), value=e, tb=None, file=f)
                if not cachedir:  # if running on CI
                    print(e)

    if cwl_runner == 'toil-cwl-runner':
        if platform.python_implementation().lower() == 'pypy':
            print('Error! Toil is not compatible with pypy!')
            print('Please use the standard cpython interpreter with Toil.')
            sys.exit(1)

        # NOTE: toil-cwl-runner always runs in parallel
        net = ['--custom-net', args.custom_net] if args.custom_net else []
        provenance = ['--provenance', f'provenance/{yaml_stem}'] if not args.no_provenance else []
        docker_cmd_ = []
        if docker_cmd == 'docker':
            docker_cmd_ = []
        elif docker_cmd == 'singularity':
            docker_cmd_ = ['--singularity']
        else:
            docker_cmd_ = ['--user-space-docker-cmd', docker_cmd]
        path_check = ['--relax-path-checks']
        # See https://github.com/common-workflow-language/cwltool/blob/5a645dfd4b00e0a704b928cc0bae135b0591cc1a/cwltool/command_line_tool.py#L94
        # https://github.com/DataBiosphere/toil/blob/6558c7f97fb37c6ef6f469c7ae614109050322f4/src/toil/options/cwl.py#L152
        docker_pull = []  # toil supports --force-docker-pull, but not --disable-pull
        cmd = ['toil-cwl-runner'] + docker_pull + net + provenance + docker_cmd_ + path_check
        now = datetime.now()
        date_time = now.strftime("%Y%m%d%H%M%S")
        cmd += ['--disableProgress', '--outdir', f'outdir_toil_{yaml_stem}_{date_time}',
                '--jobStore', f'file:./jobStore_{yaml_stem}',  # NOTE: This is the equivalent of --cachedir
                f'autogenerated/{yaml_stem}.cwl', f'autogenerated/{yaml_stem}_inputs.yml']
        cmd += passthrough_args
        cmdline = ' '.join(cmd)

        if args.generate_run_script:
            generate_run_script(cmdline)
            return 0  # Do not actually run

        print('Running ' + cmdline)
        if use_subprocess:
            # To run in parallel (i.e. pytest ... --workers 8 ...), we need to
            # use separate processes. Otherwise:
            # "signal only works in main thread or with __pypy__.thread.enable_signals()"
            proc = sub.run(cmd, check=False)
            retval = proc.returncode
            return retval  # Skip copying files to outdir/ for CI
        else:
            print('via toil.cwl.cwltoil.main python API')
            try:
                retval = toil.cwl.cwltoil.main(cmd[1:])

            except Exception as e:
                retval = 1
                print('Failed to execute', yaml_path)
                print(f'See error_{yaml_stem}.txt for detailed technical information.')
                # Do not display a nasty stack trace to the user; hide it in a file.
                with open(f'error_{yaml_stem}.txt', mode='w', encoding='utf-8') as f:
                    if sys.version_info >= (3, 10):
                        traceback.print_exception(type(e), value=e, tb=None, file=f)
                    else:
                        traceback.print_exception(etype=type(e), value=e, tb=None, file=f)
                if not cachedir:  # if running on CI
                    print(e)

    if retval == 0:
        print('Success! Output files should be in outdir/')
    else:
        print('Failure! Please scroll up and find the FIRST error message.')
        print('(You may have to scroll up A LOT.)')

    # Remove the annoying cachedir* directories that somehow aren't getting automatically deleted.
    # NOTE: Do NOT allow cachedir to be absolute; otherwise
    # if users pass in "/" this will delete their entire hard drive.
    cachedir_path = str(cachedir)
    if not Path(cachedir_path).is_absolute():
        for d in glob.glob(cachedir_path + '*'):
            if not d == cachedir_path:
                shutil.rmtree(d)  # Be VERY careful when programmatically deleting directories!

    return retval


def copy_output_files(yaml_stem: str) -> None:
    """Copies output files from the cachedir to outdir/

    Args:
        yaml_stem (str): The --yaml filename (without .extension)
    """
    output_json_file = Path(f'output_{yaml_stem}.json')
    if output_json_file.exists():
        pass  # TODO

    output_json_file_prov = Path(f'provenance/{yaml_stem}/workflow/primary-output.json')
    # NOTE: The contents of --write-summary is
    # slightly different than provenance/{yaml_stem}/workflow/primary-output.json!
    # They are NOT the same file!
    if output_json_file_prov.exists():
        with open(output_json_file_prov, mode='r', encoding='utf-8') as f:
            output_json = json.loads(f.read())
        files = utils.parse_provenance_output_files(output_json)

        dests = set()
        for location, namespaced_output_name, basename in files:
            try:
                yaml_stem_init, shortened = utils.shorten_namespaced_output_name(namespaced_output_name)
                parentdirs = yaml_stem_init + '/' + shortened.replace('___', '/')
            except:
                parentdirs = namespaced_output_name  # For --allow_raw_cwl
            Path('outdir/' + parentdirs).mkdir(parents=True, exist_ok=True)
            source = f'provenance/{yaml_stem}/workflow/' + location
            # NOTE: Even though we are using subdirectories (not just a single output directory),
            # there is still the possibility of filename collisions, i.e. when scattering.
            # For now, let's use a similar trick as cwltool of append _2, _3 etc.
            # except do it BEFORE the extension.
            # This could still cause problems with slicing, i.e. if you scatter across
            # indices 11-20 first, then 1-10 second, the output file indices will get switched.
            dest = 'outdir/' + parentdirs + '/' + basename
            if dest in dests:
                idx = 2
                while Path(dest).exists():
                    stem = Path(basename).stem
                    suffix = Path(basename).suffix
                    dest = 'outdir/' + parentdirs + '/' + stem + f'_{idx}' + suffix
                    idx += 1
            dests.add(dest)
            cmd = ['cp', source, dest]
            sub.run(cmd, check=True)


def build_cmd(workflow_name: str, basepath: str, cwl_runner: str, container_cmd: str) -> List[str]:
    """Build the command to run the workflow in an environment

    Args:
        workflow_name (str): Name of the .cwl workflow file to be executed
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
    Returns:
        cmd (List[str]): The command to run the workflow
    """
    quiet = ['--quiet']
    skip_schemas = ['--skip-schemas']
    provenance = ['--provenance', f'provenance/{workflow_name}']
    container_cmd_: List[str] = []
    if container_cmd == 'docker':
        container_cmd_ = []
    elif container_cmd == 'singularity':
        container_cmd_ = ['--singularity']
    else:
        container_cmd_ = ['--user-space-docker-cmd', container_cmd]
    write_summary = ['--write-summary', f'output_{workflow_name}.json']
    path_check = ['--relax-path-checks']
    # See https://github.com/common-workflow-language/cwltool/blob/5a645dfd4b00e0a704b928cc0bae135b0591cc1a/cwltool/command_line_tool.py#L94
    # NOTE: Using --leave-outputs to disable --outdir
    # See https://github.com/dnanexus/dx-cwl/issues/20
    # --outdir has one or more bugs which will cause workflows to fail!!!
    container_pull = ['--disable-pull']  # Use cwl-docker-extract to pull images
    script = 'cwltool_filterlog' if cwl_runner == 'cwltool' else cwl_runner
    cmd = [script] + container_pull + quiet + provenance + \
        container_cmd_ + write_summary + skip_schemas + path_check
    if cwl_runner == 'cwltool':
        cmd += ['--leave-outputs',
                f'{basepath}/{workflow_name}.cwl', f'{basepath}/{workflow_name}_inputs.yml']
    elif cwl_runner == 'toil-cwl-runner':
        container_pull = []
        now = datetime.now()
        date_time = now.strftime("%Y%m%d%H%M%S")
        cmd = [script] + container_pull + provenance + container_cmd_ + path_check
        cmd += ['--outdir', f'outdir_toil_{workflow_name}_{date_time}',
                '--jobStore', f'file:./jobStore_{workflow_name}',  # NOTE: This is the equivalent of --cachedir
                '--clean', 'always',  # This effectively disables caching, but is reproducible
                '--disableProgress',  # disable the progress bar in the terminal, saves UI cycle
                '--workDir', '/data1',
                '--coordinationDir', '/data1',
                '--logLevel', 'INFO',
                f'{basepath}/{workflow_name}.cwl', f'{basepath}/{workflow_name}_inputs.yml']
    else:
        pass
    return cmd


def run_cwl_workflow(workflow_name: str, basepath: str, cwl_runner: str, container_cmd: str, use_subprocess: bool, env_commands: List[str] = []) -> int:
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
    cmd = build_cmd(workflow_name, basepath, cwl_runner, container_cmd)
    cmdline = ' '.join(cmd)

    retval = 1  # overwrite on success
    print('Running ' + cmdline)
    if use_subprocess:
        # To run in parallel (i.e. pytest ... --workers 8 ...), we need to
        # use separate processes. Otherwise:
        # "signal only works in main thread or with __pypy__.thread.enable_signals()"
        proc = sub.run(cmd, check=False)
        retval = proc.returncode
    else:
        print('via cwltool.main.main python API')
        try:
            if cwl_runner == 'cwltool':
                retval = cwltool.main.main(cmd[1:])
            elif cwl_runner == 'toil-cwl-runner':
                _ = sub.run(env_commands, shell=True, check=False, executable="/bin/bash")
                retval = toil.cwl.cwltoil.main(cmd[1:])
            else:
                raise Exception("Invalid cwl_runner!")

            print(f'Final output json metadata blob is in output_{workflow_name}.json')
        except Exception as e:
            print('Failed to execute', workflow_name)
            print(f'See error_{workflow_name}.txt for detailed technical information.')
            # Do not display a nasty stack trace to the user; hide it in a file.
            with open(f'error_{workflow_name}.txt', mode='w', encoding='utf-8') as f:
                # https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
                if sys.version_info >= (3, 10):
                    traceback.print_exception(type(e), value=e, tb=None, file=f)
            print(e)  # we are always running this on CI
    # only copy output files if using cwltool
    if cwl_runner == 'cwltool':
        copy_output_files(workflow_name)
    return retval


async def run_cwl_serialized_async(workflow: Json, basepath: str,
                                   cwl_runner: str, container_cmd: str,
                                   env_commands: List[str] = []) -> None:
    """Prepare and run compiled and serialized CWL workflow asynchronously

    Args:
        workflow_json (Json): Compiled and serialized CWL workflow
        basepath (str): The path at which the workflow to be executed
        cwl_runner (str): The CWL runner used to execute the workflow
        container_cmd (str): The container engine command
        env_commands (List[str]): environment variables and commands needed to be run before running the workflow
    """
    workflow_name = workflow['name']
    output_dirs = pc.find_output_dirs(workflow)
    pc.create_output_dirs(output_dirs, basepath)
    compiled_cwl = workflow_name + '.cwl'
    inputs_yml = workflow_name + '_inputs.yml'
    # write _input.yml file
    with open(Path.cwd() / basepath / inputs_yml, 'w', encoding='utf-8') as f:
        yaml.dump(workflow['yaml_inputs'], f)
    workflow.pop('retval', None)
    workflow.pop('yaml_inputs', None)
    workflow.pop('name', None)
    # write compiled .cwl file
    with open(Path.cwd() / basepath / compiled_cwl, 'w', encoding='utf-8') as f:
        yaml.dump(workflow, f)
    retval = run_cwl_workflow(workflow_name, basepath,
                              cwl_runner, container_cmd, False, env_commands=env_commands)
    assert retval == 0


def stage_input_files(yml_inputs: Yaml, root_yml_dir_abs: Path,
                      relative_run_path: bool = True, throw: bool = True) -> None:
    """Copies the input files in yml_inputs to the working directory.

    Args:
        yml_inputs (Yaml): The yml inputs file for the root workflow.
        root_yml_dir_abs (Path): The absolute path of the root workflow yml file.
        relative_run_path (bool): Controls whether to use subdirectories or\n
        just one directory when writing the compiled CWL files to disk
        throw (bool): Controls whether to raise/throw a FileNotFoundError.

    Raises:
        FileNotFoundError: If throw and it any of the input files do not exist.
    """
    for key, val in yml_inputs.items():
        if isinstance(val, Dict) and val.get('class', '') == 'File':
            path = root_yml_dir_abs / Path(val['path'])
            if not path.exists() and throw:
                # raise FileNotFoundError(f'Error! {path} does not exist!')
                print(f'Error! {path} does not exist!')
                sys.exit(1)

            relpath = Path('autogenerated/') if relative_run_path else Path('.')
            pathauto = relpath / Path(val['path'])  # .name # NOTE: Use .name ?
            pathauto.parent.mkdir(parents=True, exist_ok=True)

            if path != pathauto:
                cmd = ['cp', str(path), str(pathauto)]
                proc = sub.run(cmd, check=False)


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
