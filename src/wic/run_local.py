import argparse
import glob
import json
import subprocess as sub
import sys
from pathlib import Path
import shutil
import signal
import traceback
from typing import Dict

import cwltool.load_tool
import cwltool.main

from . import utils  # , utils_graphs
from .wic_types import Yaml, RoseTree


def run_local(args: argparse.Namespace, rose_tree: RoseTree) -> None:
    """This function runs the compiled workflow locally.

    Args:
        args (argparse.Namespace): The command line arguments
        rose_tree (RoseTree): The compiled workflow
    """

    # Check that docker is installed, so users don't get a nasty runtime error.
    cmd = ['docker', 'run', 'hello-world']
    try:
        docker_cmd = True
        proc = sub.run(cmd, check=False, stdout=sub.PIPE, stderr=sub.STDOUT)
        output = proc.stdout.decode("utf-8")
    except FileNotFoundError:
        docker_cmd = False
    hello_output = "Hello from Docker!"
    if (not docker_cmd) or (not (proc.returncode == 0 and hello_output in output) and not args.ignore_docker):
        print('Warning! Docker does not appear to be installed.')
        print('Most workflows require Docker and will fail at runtime if Docker is not installed.')
        print('If you want to run the workflow anyway, use --ignore_docker')
        sys.exit(1)

    yaml_path = args.yaml
    yaml_stem = Path(args.yaml).stem

    yaml_inputs = rose_tree.data.workflow_inputs_file
    stage_input_files(yaml_inputs, Path(args.yaml).parent.absolute())

    yaml_stem = yaml_stem + '_inline' if args.cwl_inline_subworkflows else yaml_stem
    if args.cwl_runner == 'cwltool':
        parallel = ['--parallel'] if args.parallel else []
        # NOTE: --parallel is required for real-time analysis / real-time plots,
        # but it seems to cause hanging with Docker for Mac. The hanging seems
        # to be worse when using parallel scattering.
        quiet = ['--quiet'] if args.quiet else []
        # NOTE: Using --leave-outputs to disable --outdir
        # See https://github.com/dnanexus/dx-cwl/issues/20
        # --outdir has one or more bugs which will cause workflows to fail!!!
        cmd = ['cwltool'] + parallel + quiet + ['--leave-outputs',
                                                '--provenance', 'provenance', '--cachedir', args.cachedir,
                                                # '--js-console', # "Running with support for javascript console in expressions (DO NOT USE IN PRODUCTION)"
                                                f'autogenerated/{yaml_stem}.cwl', f'autogenerated/{yaml_stem}_inputs.yml']
        # TODO: Consider using the undocumented flag --fast-parser for known-good workflows,
        # which was recently added in the 3.1.20220913185150 release of cwltool.

        print('running cwltool via python API')
        try:
            # NOTE: cwltool.main.run (currently) calls sys.exit().
            # Until https://github.com/common-workflow-language/cwltool/pull/1772
            # is released, we need to copy & paste the body below.
            # cwltool.main.run(cmd[1:])

            cwltool.main.windows_check()
            signal.signal(signal.SIGTERM, cwltool.main._signal_handler)  # pylint: disable=protected-access
            retval = cwltool.main.main(cmd[1:])
            assert retval == 0

            # This also works, but doesn't easily allow using --leave-outputs, --provenence, --cachedir
            # import cwltool.factory
            # fac = cwltool.factory.Factory()
            # rootworkflow = fac.make(f'autogenerated/{yaml_stem}.cwl')
            # output_json = rootworkflow(**yaml_inputs)
            # with open('primary-output.json', mode='w', encoding='utf-8') as f:
            #     f.write(json.dumps(output_json))

            print('Success! Output files should be in outdir/')
        except Exception as e:
            print('Failed to execute', yaml_path)
            print(f'See error_{yaml_stem}.txt for detailed technical information.')
            # Do not display a nasty stack trace to the user; hide it in a file.
            with open(f'error_{yaml_stem}.txt', mode='w', encoding='utf-8') as f:
                traceback.print_exception(etype=type(e), value=e, tb=None, file=f)
        finally:
            cwltool.main._terminate_processes()  # pylint: disable=protected-access

    if args.cwl_runner == 'toil-cwl-runner':
        # NOTE: toil-cwl-runner always runs in parallel
        cmd = ['toil-cwl-runner', '--provenance', 'provenance', '--outdir', 'outdir_toil',
               '--jobStore', 'file:./jobStore',  # NOTE: This is the equivalent of --cachedir
               # TODO: Check --clean, --cleanWorkDir, --restart
               f'autogenerated/{yaml_stem}.cwl', f'autogenerated/{yaml_stem}_inputs.yml']

        print('Running ' + ' '.join(cmd))
        proc = sub.run(cmd, check=False)
        if proc.returncode == 0:
            print('Success! Output files should be in outdir/')
        else:
            print('Failure! Please scroll up and find the FIRST error message.')
            print('(You may have to scroll up A LOT.)')

    # Remove the annoying cachedir* directories that somehow aren't getting automatically deleted.
    # NOTE: Do NOT allow args.cachedir_path to be absolute; otherwise
    # if users pass in "/" this will delete their entire hard drive.
    cachedir_path = str(args.cachedir)
    if not Path(cachedir_path).is_absolute():
        for d in glob.glob(cachedir_path + '*'):
            if not d == cachedir_path:
                shutil.rmtree(d)  # Be VERY careful when programmatically deleting directories!

    # Finally, since there is an output file copying bug in cwltool,
    # we need to copy the output files manually. See comment above.
    output_json_file = Path('provenance/workflow/primary-output.json')
    if output_json_file.exists():
        with open(output_json_file, mode='r', encoding='utf-8') as f:
            output_json = json.loads(f.read())
        files = utils.parse_provenance_output_files(output_json)

        dests = set()
        for location, namespaced_output_name, basename in files:
            yaml_stem_init, shortened = utils.shorten_namespaced_output_name(namespaced_output_name)
            parentdirs = yaml_stem_init + '/' + shortened.replace('___', '/')
            Path('outdir/' + parentdirs).mkdir(parents=True, exist_ok=True)
            source = 'provenance/workflow/' + location
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
                print('(Did you forget to use an explicit edge?)')
                print('See https://workflow-inference-compiler.readthedocs.io/en/latest/userguide.html#explicit-edges')
                sys.exit(1)

            relpath = Path('autogenerated/') if relative_run_path else Path('.')
            pathauto = relpath / Path(val['path'])  # .name # NOTE: Use .name ?
            pathauto.parent.mkdir(parents=True, exist_ok=True)

            if path != pathauto:
                cmd = ['cp', str(path), str(pathauto)]
                proc = sub.run(cmd, check=False)
