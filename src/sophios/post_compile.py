from pathlib import Path
import sys
import copy
import shutil
import subprocess as sub
from . import plugins
from .wic_types import RoseTree, NodeData, Yaml
from .lang.diagnostics import Code, SophiosError


def verify_container_engine_config(container_engine: str, ignore_container_install: bool) -> None:
    """Verify that the container_engine is correctly installed and has
    correct permissions for the user.
    Args:
        container_engine (str): The container engine command
        ignore_container_install (bool): whether to ignore if container engine is not installed and run workflow anyway
    """
    docker_like_engines = ['docker', 'podman']
    container_cmd: str = container_engine
    # Check that docker is installed, so users don't get a nasty runtime error.
    if container_cmd in docker_like_engines:
        cmd = [container_cmd, 'run', '--rm', 'hello-world']
        output = ''
        try:
            container_cmd_exists = True
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT)
            output = proc.stdout.decode("utf-8")
        except FileNotFoundError:
            container_cmd_exists = False
        out_d = "Hello from Docker!"
        out_p = "Hello Podman World"
        permission_denied = 'permission denied while trying to connect to the Docker daemon socket at'

        # docker_ok is True iff the command exists AND the hello-world container printed
        # its expected greeting. container_cmd_exists is checked first so that `proc` is
        # never accessed when it was not assigned (i.e. when the command does not exist).
        docker_ok = container_cmd_exists and (
            (proc.returncode == 0 and out_d in output) or out_p in output)

        if not docker_ok and not ignore_container_install:

            if permission_denied in output:
                raise SophiosError.error(
                    Code.CONTAINER_ENGINE_UNAVAILABLE,
                    'Warning! docker appears to be installed, but not configured as a non-root user.',
                    'See https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user',
                    'TL;DR you probably just need to run the following command (and then restart your machine)',
                    'sudo usermod -aG docker $USER')

            raise SophiosError.error(
                Code.CONTAINER_ENGINE_UNAVAILABLE,
                f'Warning! The {container_cmd} command does not appear to be installed.',
                f"""Most workflows require docker containers and
                  will fail at runtime if {container_cmd} is not installed.""",
                'If you want to try running the workflow anyway, use --ignore_docker_install',
                """Note that --ignore_docker_install does
                  NOT change whether or not any step in your workflow uses docker""")

        # If docker is installed, check for too many running processes. (on linux, macos)
        if container_cmd_exists and sys.platform != "win32":
            cmd = 'pgrep com.docker | wc -l'  # type: ignore
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT, shell=True)
            output = proc.stdout.decode("utf-8")
            num_processes = int(output.strip())
            max_processes = 1000
            too_many_processes = num_processes > max_processes
            if too_many_processes and not ignore_container_install:
                raise SophiosError.error(
                    Code.CONTAINER_ENGINE_UNAVAILABLE,
                    f'Warning! There are {num_processes} running docker processes.',
                    f'More than {max_processes} may potentially cause intermittent hanging issues.',
                    'It is recommended to terminate the processes using the command',
                    '`sudo pkill com.docker && sudo pkill Docker`',
                    'and then restart Docker.',
                    'If you want to run the workflow anyway, use --ignore_docker_processes')
    else:
        cmd = [container_cmd, '--version']
        output = ''
        try:
            container_cmd_exists = True
            proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT)
            output = proc.stdout.decode("utf-8")
        except FileNotFoundError:
            container_cmd_exists = False
        singularity_ok = container_cmd_exists

        if not singularity_ok and not ignore_container_install:
            raise SophiosError.error(
                Code.CONTAINER_ENGINE_UNAVAILABLE,
                f'Warning! The {container_cmd} command does not appear to be installed.',
                'If you want to try running the workflow anyway, use --ignore_docker_install',
                'Note that --ignore_docker_install does NOT change whether or not',
                'any step in your workflow uses docker or any other containers')


def cwl_docker_extract(container_engine: str, pull_dir: str, cwl_path: str | Path) -> None:
    """Run `cwl-docker-extract` against a compiled CWL document.

    Args:
        container_engine (str): Container engine used for execution.
        pull_dir (str): Directory used by singularity for image pulls.
        cwl_path (str | Path): Path to the compiled CWL workflow file.
    """
    cwl_path_str = str(Path(cwl_path))
    # cwl-docker-extract recursively `docker pull`s all images in all subworkflows.
    # This is important because cwltool only uses `docker run` when executing
    # workflows, and if there is a local image available,
    # `docker run` will NOT query the remote repository for the latest image!
    # cwltool has a --force-docker-pull option, but this may cause multiple pulls in parallel.
    if container_engine == 'singularity':
        cmd = ['cwl-docker-extract', '-s', '--dir',
               f'{pull_dir}', cwl_path_str]
    else:
        cmd = ['cwl-docker-extract', '--force-download', cwl_path_str]
    sub.run(cmd, check=True)


def cwl_inline_runtag(rose_tree: RoseTree) -> RoseTree:
    """Transforms the compiled CWL within the rose_tree with inline cwl of steps in the runtag
    Args:
        rose_tree (RoseTree): The data associated with compiled subworkflows
    Returns:
        RoseTree: The updated rose_tree with inline cwl in runtag
    """
    rose_tree_mod = copy.deepcopy(rose_tree)
    node_data: NodeData = rose_tree_mod.data
    cwl_tree = node_data.compiled_cwl

    if cwl_tree.get('class', '') == 'Workflow':
        for sub_rose_tree in rose_tree_mod.sub_trees:
            # Inline descendants before embedding this child into the parent run tag.
            sub_rose_tree = cwl_inline_runtag(sub_rose_tree)
            sub_node_data: NodeData = sub_rose_tree.data
            sub_step_name = sub_node_data.namespaces[-1]
            step_to_update = next(
                item for item in cwl_tree['steps'] if item.get('id') == sub_step_name)
            step_to_update['run'] = sub_node_data.compiled_cwl
            # merge the steps/clt namespaces to global namespaces
            # as the run tag can't have namespaces and schemas
            cwl_tree['$namespaces'] = cwl_tree.get('$namespaces', {}) | step_to_update['run'].get(
                '$namespaces', {})
            # and then get rid of $namespaces and $schemas in the run tag
            step_to_update['run'].pop('$namespaces', None)
            step_to_update['run'].pop('$schemas', None)
    return rose_tree_mod


def remove_entrypoints(container_engine: str, rose_tree: RoseTree) -> RoseTree:
    """Remove entry points"""
    # Requires root, so guard behind CLI option
    if container_engine == 'docker':
        plugins.remove_entrypoints_docker()
    elif container_engine == 'podman':
        plugins.remove_entrypoints_podman()
    return plugins.dockerPull_append_noentrypoint_rosetree(rose_tree)


def stage_input_files(yml_inputs: Yaml,
                      root_yml_dir_abs: Path,
                      basepath: str,
                      use_subdirs_cwl: bool = True,
                      throw: bool = True) -> None:
    """Copies the input files in yml_inputs to the working directory.

    Args:
        yml_inputs (Yaml): The yml inputs file for the root workflow.
        root_yml_dir_abs (Path): The absolute path of the root workflow yml file.
        basepath (str): The path at which the workflow to be executed
        use_subdirs_cwl (bool): Controls whether to use subdirectories or
        just one directory when writing the compiled CWL files to disk
        throw (bool): Controls whether to raise/throw a FileNotFoundError.

    Raises:
        FileNotFoundError: If throw and any of the input files do not exist.
    """

    for val in yml_inputs.values():
        match val:
            case {"class": "File", "location": location, **_rest_val}:
                src_path = root_yml_dir_abs / Path(location)
                if not src_path.exists() and throw:
                    raise SophiosError.error(Code.MISSING_INPUT_FILE, f"Error! {src_path} does not exist!")

                relroot = Path(basepath) if use_subdirs_cwl else Path(".")
                dst_path = relroot / Path(location)
                dst_path.parent.mkdir(parents=True, exist_ok=True)

                # Avoid unnecessary copy
                if src_path.resolve() != dst_path.resolve():
                    shutil.copy2(src_path, dst_path)
