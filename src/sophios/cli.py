import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .compiler import CompileSettings
from .ir.policy import RunPolicy
from .lang.error_codes import SophiosErrorCode
from .wic_types import DEFAULT_BASEPATH, TARGET_NAMES, GraphSettings


def _config_file(path: str) -> Path:
    """The `--config_file` argparse type: a file that exists, since it is only ever read."""
    if not Path(path).is_file():
        raise argparse.ArgumentTypeError(f'no config file at {path}')
    return Path(path)


def _error_code(text: str) -> SophiosErrorCode:
    """The `--explain` argparse type: a code Sophios reports."""
    try:
        return SophiosErrorCode(text.lower())
    except ValueError:
        raise argparse.ArgumentTypeError(f'{text} is not a Sophios error code; '
                                         'docs/error_codes.md lists them') from None


parser = argparse.ArgumentParser(prog='main', description='Convert a high-level yaml workflow file to CWL or Nextflow.',
                                 allow_abbrev=False)

# Each option is declared in exactly one group, so its target is stated where it
# is declared. `--target` offers the targets' names (`wic_types.TARGET_NAMES`, the
# names of `sophios.targets.TARGETS`), one group each.
neutral = parser.add_argument_group('options for every target')
target_options = {name: parser.add_argument_group(f'options for --target {name} only') for name in TARGET_NAMES}
cwl = target_options['cwl']

# Not `required=`: whether `--yaml` is needed depends on whether one of the
# generate modes was asked for, and argparse cannot express that. The old
# spelling computed it at *import* time from the host process's `sys.argv`, so
# importing sophios from a program whose own command line happened to carry
# `--generate_config` silently made `--yaml` optional. That is the defect this
# module was cleaned up to remove, in the one place a scan for writes to
# `sys.argv` cannot see, because it is a read. `_main` enforces it instead,
# where both generate flags are already parsed and known.
neutral.add_argument('--yaml', type=str, default='',
                     help='Yaml workflow file')
group_gen = neutral.add_mutually_exclusive_group()
group_gen.add_argument('--generate_schemas', default=False, action="store_true",
                       help='Write the JSON Schema of the language to <basepath>/schemas/wic.json, for editors')
group_gen.add_argument('--generate_config', default=False, action="store_true",
                       help='''Generate default config in wic/global_config.json
                       with default search_paths_wic and search_paths_cwl''')
group_gen.add_argument('--explain', type=_error_code, default=None, metavar='CODE',
                       help='Print what an error code (wic011, api002, ...) means and how to fix it')
neutral.add_argument('--config_file', type=_config_file, required=False, default=None,
                     help='User provided (JSON) config file. Without it, <homedir>/wic/global_config.json '
                     'is used, and generated the first time it is needed')
# version action exits the parser
# Ref : https://github.com/python/cpython/blob/1f515e8a109204f7399d85b7fd806135166422d9/Lib/argparse.py#L1167
neutral.add_argument('--version', action='version', version=__version__,
                     default='==SUPPRESS==', help='Current version of the Sophios Compiler')
neutral.add_argument('--homedir', type=str, required=False, default=str(Path().home()),
                     help='''The users home directory.
                     This is necessary because CWL clears environment variables (e.g. HOME)''')
neutral.add_argument('--insert_steps_automatically', default=False, action="store_true",
                     help='''Attempt to fix inference failures by speculatively
                     inserting workflow steps from a curated whitelist.''')
neutral.add_argument('--lang_version', type=str, default=None,
                     help='Pin the Sophios language version for this compilation. '
                     'Defaults to inferring the newest version that accepts the source.')
neutral.add_argument('--allow_raw_cwl', default=False, action="store_true",
                     help='''Do not check whether the input to a workflow step refers to the workflow inputs: tag.
                     A --yaml file that is a plain CWL workflow (cwlVersion and class: Workflow, or a $graph
                     whose main is a Workflow, and no Sophios syntax) is not compiled under --target cwl: it is used as it is''')
neutral.add_argument('--target', choices=TARGET_NAMES, default='cwl',
                     help='Compilation artifact target. Defaults to cwl.')
neutral.add_argument('--inference_disable', default=False, action="store_true",
                     help='Disables use of the inference algorithm when compiling.')
neutral.add_argument('--no_inference_naming_conventions', dest='inference_use_naming_conventions',
                     default=True, action="store_false",
                     help='Do not let an input\'s name decide between equally typed outputs of the same step; '
                     'the output declared first wins')
neutral.add_argument('--inference_strict', default=False, action="store_true",
                     help='Treat a guessed edge (wic043) as an error.')
neutral.add_argument('--validate_plugins', default=False, action="store_true",
                     help='Validate all CWL CommandLineTools')
neutral.add_argument('--diagnostics', choices=['text', 'json'], default='text',
                     help='''How diagnostics are printed on stderr: text, or one JSON object per line
                     for programs and agents (see docs/error_codes.md)''')
# What to do with the workflow. --generate_cwl_workflow, declared with the CWL options, excludes these too.
group_run = neutral.add_mutually_exclusive_group()
group_run.add_argument('--generate_run_script', default=False, action="store_true",
                       help='Just generates run.sh, the runner command, and exits. Does not actually invoke ./run.sh')
group_run.add_argument('--check', default=False, action="store_true",
                       help='''Compile the workflow and check that this machine can run it (the container engine
                       when a step uses a container, the input paths, the directories Sophios writes, the programs
                       it calls; under --target nextflow, the input paths, nextflow and a Java it can run, and the
                       container engine when a process has a container), then stop: nothing is pulled and nothing is
                       run.''')
group_run.add_argument('--run_local', default=False, action="store_true",
                       help='''After compiling, run the workflow on your local machine: with --cwl_runner, or under
                       --target nextflow with Nextflow.''')
neutral.add_argument('--passthrough_flags', type=str, default='no', choices=['yes', 'no'], required=False,
                     help='''With 'yes', arguments sophios does not recognise are sent to the runner (the CWL
                     runner, or Nextflow) unchecked. With 'no' (the default) an unrecognised argument is an error.''')
neutral.add_argument('--basepath', type=str, required=False, default=DEFAULT_BASEPATH,
                     help='Directory, relative to the working directory, for the compiled workflow and the job file, '
                     'or under --target nextflow the Nextflow files, '
                     'the real-time plans, provenance, the drawings, the schema and the default --outdir. '
                     'Workflow.run(basepath=) in Python is the same')

cwl.add_argument('--inputs_file', type=str, required=False, default='',
                 help='Additional inputs Yaml file. For a plain CWL workflow under --allow_raw_cwl, '
                 'its job file, given to the runner as it is')
cwl.add_argument('--pull_dir', type=str, required=False, default=str(Path().cwd()),
                 help='''The user specified pull directory for (singularity) image pull.
                 The default is the current working directory i.e. `pwd`''')
cwl.add_argument('--no_provenance', default=False, action="store_true",
                 help='''Do not use the --provenance feature of CWL.
                 (You should only disable provenance if absolutely necessary.)''')
cwl.add_argument('--cwl_inline_runtag', default=False, action="store_true",
                 help='''Write one self-contained file: embed the CWL of every step, tools and
                 subworkflows, in its run field. The shape of the workflow is unchanged.''')
cwl.add_argument('--parallel', default=False, action="store_true",
                 help='''When running locally, execute independent steps in parallel.
                 \nThis may cause issues with hanging (particularly when scattering).''')
cwl.add_argument('--cwl_runner', type=str, required=False, default='cwltool', choices=['cwltool', 'toil-cwl-runner'],
                 help='The CWL runner to use for running workflows locally.')
cwl.add_argument('--ignore_docker_install', default=False, action="store_true",
                 help='''Do not check whether docker is installed before running workflows.
                 \n--ignore_docker_install does NOT change whether or not any step in your workflow uses docker!''')
cwl.add_argument('--ignore_docker_processes', default=False, action="store_true",
                 help='Do not check whether there are too many running docker processes before running workflows.')
cwl.add_argument('--container_engine', default='docker',
                 help='Specify which command to use to run OCI containers.')
cwl.add_argument('--docker_remove_entrypoints', default=False, action="store_true",
                 help='''Remove entrypoints from docker images before running workflows.
                 See https://cwl.discourse.group/t/override-docker-entrypoint-in-command-line-tool/695/2
                 If your CWL CommandLineTools rely on non-portable docker entrypoints
                 (i.e. if they do NOT explicitly have baseCommand: and/or arguments: tags)
                 then this flag will almost certainly cause your workflows to fail.
                 In other words, this flag can be used to tell you if your workflows are
                 trapped inside a walled garden of docker containers or if there is at least a chance that
                 they can be run in dev environments (conda, etc) and/or directly on the host machine.''')
cwl.add_argument('--partial_failure_enable', default=False, action="store_true",
                 help='''Let workflows to continue with partial failures
                 \n(i.e. run next steps after failure step by guarding for possible no input)''')
cwl.add_argument('--partial_failure_success_codes', nargs='*', type=int, default=[0, 1], required=False,
                 help='Let users add custom error codes to be treated as success')
cwl.add_argument('--partial_failure_success_codes_range', nargs=2, type=int, default=[0, 1], required=False,
                 help='Let users add custom error code range to be treated as success')
# A mutually exclusive group's actions are declared in the group it was made in, so this one is added to it after.
group_run._group_actions.append(  # pylint: disable=protected-access
    cwl.add_argument('--generate_cwl_workflow', required=False, default=False, action="store_true",
                     help='Compile the workflow without pulling the docker image'))
cwl.add_argument('--quiet', default=False, action="store_true",
                 help='''With --run_local or --generate_run_script: hide cwltool's own log (cwltool is
                 given --quiet). Each tool's own messages still print as the run goes.
                 The compiled CWL is the same with or without it.''')
cwl.add_argument('--cwl_inline_subworkflows', default=False, action="store_true",
                 help='''Write one flat workflow: replace every step that runs a subworkflow by that
                 subworkflow's own steps. Tools stay separate files. A subworkflow call that has
                 scatter, when or any other key besides in and out, or that is marked
                 inlineable: false, stays a subworkflow, and each one is named on stderr.''')
cwl.add_argument('--cachedir', type=str, required=False, default='',
                 help='''With --run_local and cwltool, the directory cwltool caches each step's outputs in:
                 a step whose tool and inputs are unchanged reuses them. No cache unless given.''')
cwl.add_argument('--outdir', type=str, required=False, default='',
                 help='Workflow output directory passed to the CWL runner.')
cwl.add_argument('--graphviz', default=False, action="store_true",
                 help='Generate a DAG using graphviz.')
cwl.add_argument('--graph_label_edges', default=False, action="store_true",
                 help='Label the graph edges with the name of the intermediate input/output.')
cwl.add_argument('--graph_label_stepname', default=False, action="store_true",
                 help='Label each step node with its name in the compiled CWL instead of '
                 'its wic: graphviz: label.')
cwl.add_argument('--graph_show_inputs', default=False, action="store_true",
                 help='Add nodes to the graph representing the workflow inputs.')
cwl.add_argument('--graph_show_outputs', default=False, action="store_true",
                 help='Add nodes to the graph representing the workflow outputs.')
cwl.add_argument('--graph_inline_depth', type=int, required=False, default=sys.maxsize,
                 help='Controls the depth of subgraphs which are displayed.')
cwl.add_argument('--graph_dark_theme', default=False, action="store_true",
                 help='Draw text, box outlines and inferred edges in white, for a dark background.')


def validate_target_args(args: argparse.Namespace) -> None:
    """Reject an option of another target that is set away from its default."""
    foreign = [
        f'{action.option_strings[0]} (--target {target})'
        for target, group in target_options.items() if target != args.target
        for action in group._group_actions  # pylint: disable=protected-access
        if getattr(args, action.dest) != action.default
    ]
    if foreign:
        parser.error(f"--target {args.target} cannot be combined with {', '.join(foreign)}")


def validate_success_code_range(args: argparse.Namespace) -> None:
    """Reject a success-code range whose lower bound is above its upper, which names no code."""
    low, high = args.partial_failure_success_codes_range
    if low > high:
        parser.error(f'--partial_failure_success_codes_range {low} {high}: the lower bound is above the upper')


def _argv(yaml_path: str = '', suppliedargs: list[str] | None = None) -> list[str]:
    """The arguments, without a program name, that `get_args` and `get_known_and_unknown_args` parse."""
    return ['--yaml', yaml_path] + (suppliedargs or [])


def get_args(yaml_path: str = '', suppliedargs: list[str] | None = None) -> argparse.Namespace:
    """This is used to get mock command line arguments, default + suppled args

    Returns:
        argparse.Namespace: The mocked command line arguments
    """
    args = parser.parse_args(_argv(yaml_path, suppliedargs))
    validate_target_args(args)
    validate_success_code_range(args)
    return args


def get_known_and_unknown_args(
    yaml_path: str = '', suppliedargs: list[str] | None = None
) -> tuple[argparse.Namespace, list[str]]:
    """This is used to get mock command line arguments, default + suppled args

    Returns:
        tuple[argparse.Namespace, list[str]]: The mocked, recognized command line arguments,
            and the list of remaining unrecognized argument strings.
    """
    args, unknown = parser.parse_known_args(_argv(yaml_path, suppliedargs))
    validate_target_args(args)
    validate_success_code_range(args)
    return args, unknown


def settings_from_args(args: argparse.Namespace) -> tuple[CompileSettings, GraphSettings]:
    """Split parsed command-line arguments into the compile's settings, with the shipped renamings, and the drawing's
    settings: the one conversion from an `argparse.Namespace` to the values everything after `main` takes."""
    settings = CompileSettings(
        replace(CompileSettings().inference,
                disabled=args.inference_disable,
                use_naming_conventions=args.inference_use_naming_conventions,
                insert_steps_automatically=args.insert_steps_automatically,
                strict=args.inference_strict),
        RunPolicy(args.partial_failure_enable,
                  (*args.partial_failure_success_codes, *range(*args.partial_failure_success_codes_range))),
        args.lang_version,
        args.allow_raw_cwl)

    # for drawing.draw, after the compile
    graph_settings: GraphSettings = {
        'graph_dark_theme': args.graph_dark_theme,
        'graph_inline_depth': args.graph_inline_depth,
        'graph_label_edges': args.graph_label_edges,
        'graph_label_stepname': args.graph_label_stepname,
        'graph_show_outputs': args.graph_show_outputs,
        'graph_show_inputs': args.graph_show_inputs,
    }
    return (settings, graph_settings)
