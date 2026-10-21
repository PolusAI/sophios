import argparse
import sys
from pathlib import Path
import shutil
import subprocess as sub
import traceback
import json
from collections.abc import Iterable
from typing import Final, Iterator

import graphviz
import yaml

from sophios.lang import wic_schema
from sophios.lang.diagnostics import Diagnostic, Diagnostics, SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.ir.artifacts import CompilationResult
from . import input_output as io
from . import input_output_nf as io_nf
from . import post_compile as pc
from . import backends, cli, compiler, drawing, plugins, preflight, realtime, run_local, run_setup
from .ir import frontdoor
from .ir.frontdoor import SourceBundle
from .utils_nf import compiled_source_to_nextflow_with_notes
from .wic_types import CompilerOptions, Json, Tools


def _write_schema(path: Path) -> None:
    """Write the language's JSON Schema, generated from the AST, for editors.

    Syntax only, so it is the same on every machine: which steps exist here and
    which ports they declare are for Resolve and Lower to report, with
    positions, not for a schema to enumerate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wic_schema(), indent=2), encoding='utf-8')


def _load_source_bundle(args: argparse.Namespace,
                        yml_paths: dict[str, dict[str, Path]],
                        tools_cwl: Tools) -> tuple[str, str, SourceBundle]:
    """Read and parse the root workflow and everything it reaches.

    Nothing is spliced into anything and nothing is rebuilt, so the document
    the compiler reads is the one on disk and a diagnostic's position is a
    place the reader can open.
    """
    yaml_path = args.yaml
    yaml_stem = Path(yaml_path).stem
    basepath = Path(args.basepath)
    if (problem := preflight.unwritable(basepath, 'the compiled workflow',
                                        'give --basepath a directory you can write to')) is not None:
        raise SophiosError([problem])
    basepath.mkdir(parents=True, exist_ok=True)
    bundle = frontdoor.bundle_from_disk(Path(yaml_path), yml_paths, tools_cwl)
    return yaml_path, yaml_stem, bundle


def _compile_loaded_document(yaml_path: str, bundle: SourceBundle,
                             yml_paths: dict[str, dict[str, Path]], tools_cwl: Tools,
                             compiler_options: CompilerOptions
                             ) -> tuple[CompilationResult, tuple[realtime.Analysis, ...]]:
    """Compile to a graph-derived result, and each real-time analysis it declares."""
    try:
        result = compiler.compile_source(bundle, compiler_options, testing=False)
        analyses = realtime.compile_analyses(result.realtime, yml_paths, tools_cwl, compiler_options)
    except SophiosError:
        # The library reports; only this adapter is allowed to exit. The
        # banner has to stay: this arm intercepts failures that used to
        # reach the handler below, and a message naming neither the
        # workflow nor the file is a worse error than the one it replaced.
        # `main()` prints the diagnostics, in the form asked for. No
        # traceback file, though: a reported failure is not a crash.
        print('Failed to compile', yaml_path, file=sys.stderr)
        raise
    # The resolved language version is reported on every compile, not
    # only on failure — nobody should have to guess which language their
    # file was read as.
    print('Sophios lang_version:', result.lang_version)
    return result, analyses


def _write_nextflow_target(
    compilation: CompilationResult,
    yaml_stem: str,
    basepath: Path,
    diagnostics_form: str,
) -> tuple[Path, Path, Path, Path]:
    """Lower the completed semantic graph, write Nextflow artifacts under `basepath`, and report the notes."""
    io_nf.remove_nextflow_artifacts(basepath)
    try:
        workflow, notes = compiled_source_to_nextflow_with_notes(compilation)
        paths = io_nf.write_nextflow_artifacts(workflow, basepath)
    except ValueError as error:
        error_path = Path(f'error_{yaml_stem}.txt')
        error_path.write_text(f'{error}\n', encoding='utf-8')
        print(f'Failed to generate Nextflow artifacts. See {error_path}.')
        raise SystemExit(1) from None
    _report(notes, diagnostics_form)
    return paths


#: Flags that rewrite the CWL Sophios compiled. A plain CWL workflow is not
#: compiled, so there is nothing for them to rewrite, and asking is an error.
_REWRITING_FLAGS = ('--cwl_inline_runtag', '--cwl_inline_subworkflows', '--partial_failure_enable',
                    '--docker_remove_entrypoints')


def _entries(node: yaml.Node | None) -> dict[str, yaml.Node]:
    """A YAML mapping node's values by their scalar keys; empty for any other node."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {key.value: value for key, value in node.value if isinstance(key, yaml.ScalarNode)}


def _says(node: yaml.Node | None, *values: str) -> bool:
    """Whether `node` is a scalar spelled as one of `values`."""
    return isinstance(node, yaml.ScalarNode) and node.value in values


def _tags(root: yaml.Node) -> Iterator[str]:
    """The tag of every node under `root`, each node once (an alias shares its anchor's node)."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node.tag
        if isinstance(node, yaml.MappingNode):
            stack.extend(child for pair in node.value for child in pair)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)


def _is_plain_cwl_workflow(path: Path) -> bool:
    """Whether `path` is a CWL workflow with no Sophios syntax in it.

    Its top level says `cwlVersion`, and either `class: Workflow` or a packed
    `$graph` whose `main` process is a Workflow; no node carries a `!` tag
    (`!ii`, `!&`, `!*`, `!cwl`) and there is no top-level `wic:`. The text is
    composed, not loaded, so a Sophios tag is seen rather than failing the read.
    A file that cannot be read or composed is not plain CWL: the compiler
    reports it as it always has.
    """
    try:
        root = yaml.compose(path.read_text(encoding='utf-8'), Loader=yaml.SafeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    top = _entries(root)
    if 'cwlVersion' not in top or 'wic' in top:
        return False
    graph = top.get('$graph')
    packed = isinstance(graph, yaml.SequenceNode) and any(
        _says(_entries(entry).get('id'), 'main', '#main') and _says(_entries(entry).get('class'), 'Workflow')
        for entry in graph.value)
    if not (_says(top.get('class'), 'Workflow') or packed):
        return False
    return not any(tag.startswith('!') for tag in _tags(root))


def _draw(rootgraph: graphviz.Digraph | None, cwl_path: str, yaml_stem: str, basepath: Path) -> Diagnostics:
    """`--graphviz`: Sophios's drawing of the graph it compiled, if it compiled one, and cwltool's of `cwl_path`,
    written under `basepath`.

    Returns:
        Diagnostics: A note when `dot` is not installed and nothing was drawn.
    """
    if not shutil.which('dot'):
        notes = Diagnostics()
        notes.note(SophiosErrorCode.PROGRAM_MISSING,
                   '--graphviz needs the dot program, which is not on PATH: install Graphviz '
                   '(conda install -c conda-forge graphviz); the workflow is compiled without the drawing.')
        return notes
    if rootgraph is not None:
        # Not rootgraph.render(): it saves the png beside the original yaml_path.
        dot_file = basepath / f'{yaml_stem}.wic.gv'
        rootgraph.save(str(dot_file))
        sub.run(['dot', '-Tpng', '-o', f'{dot_file}.png', str(dot_file)], check=False)

    # For comparison, the built-in cwltool graphiz support generates a visual abomination:
    svg_file = basepath / f'{yaml_stem}.svg'
    print_dot = [sys.executable, '-m', 'cwltool', '--print-dot', cwl_path]
    dot_source = sub.run(print_dot, capture_output=True, check=False).stdout
    sub.run(['dot', '-Tsvg', '-o', str(svg_file)], input=dot_source, capture_output=True, check=False)
    return Diagnostics()


def _is_local_run(args: argparse.Namespace) -> bool:
    """Whether the command runs the workflow on this machine, or prepares to: `--run_local`,
    `--generate_run_script` and `--check`.

    The one place that decides who gets the pre-flight. It asks what this machine has (a container
    engine, paths, programs), so a compute submission, which runs elsewhere, never gets it.
    """
    return bool(args.run_local or args.generate_run_script or args.check)


def _preflight(args: argparse.Namespace, documents: list[Path], jobs: tuple[preflight.Job, ...]) -> bool:
    """Check what a local run of `documents` needs from this machine, and pull its images.

    Returns:
        bool: Whether the run goes on. `--check` stops after the checks, having pulled nothing.
    """
    settings = _run_settings(args)
    if not args.check:
        preflight.prepare(documents, settings, jobs)
        return True
    backends.backend('cwl', args.cwl_runner).check(documents, jobs, settings)
    print(f'Checked {documents[0]}: nothing in the way of a local run on this machine. '
          'Nothing was pulled or run.')
    return False


def _run_settings(args: argparse.Namespace) -> preflight.RunSettings:
    """The pre-flight's view of the run the arguments ask for."""
    writes = ((Path(args.outdir), "the run's outputs", 'give --outdir a directory you can write to'),) \
        if args.outdir else ()
    return preflight.RunSettings(args.container_engine, args.pull_dir, ignore_install=args.ignore_docker_install,
                                 ignore_processes=args.ignore_docker_processes, runner=args.cwl_runner,
                                 run_script=args.generate_run_script, writes=writes)


def _run(args: argparse.Namespace, unknown_args: list[str], workflow_name: str,
         documents: tuple[str, ...] | None = None,
         realtime_plans: tuple[realtime.Plan, ...] = ()) -> None:
    """Run the workflow, or write run.sh for it, with `args.basepath` as the base path, and its real-time analyses.

    Exits with the runner's exit code when it is not 0, and with 130 on Ctrl-C.
    """
    request = backends.RunRequest(
        workflow_name, Path(args.basepath), runner=args.cwl_runner, documents=documents,
        container_engine=args.container_engine, cachedir=args.cachedir, outdir=args.outdir, quiet=args.quiet,
        provenance=not args.no_provenance, passthrough_args=tuple(unknown_args), realtime_plans=realtime_plans,
        generate_run_script=args.generate_run_script)
    try:
        retval = run_local.run(request)
    except KeyboardInterrupt:
        # 130 is the shell's exit code for SIGINT.
        sys.exit(130)
    if retval != 0:
        sys.exit(retval)


def _pass_through(args: argparse.Namespace, unknown_args: list[str]) -> None:
    """Use a plain CWL workflow as it is: the `--yaml` file is the artifact.

    Nothing is parsed, compiled, copied or written. The runner is given the
    file, and `--inputs_file` as its job when there is one, both as they are,
    so each resolves its relative paths against its own directory. Without
    `--inputs_file` the runner gets no job file and the workflow's defaults apply.
    """
    rewriting = [flag for flag in _REWRITING_FLAGS if getattr(args, flag[2:])]
    if rewriting:
        cli.parser.error(f'{rewriting[0]} rewrites the CWL Sophios compiles; with --allow_raw_cwl, '
                         f'{args.yaml} is a CWL workflow and is used as it is')
    workflow = Path(args.yaml).absolute()
    job = (str(Path(args.inputs_file).absolute()),) if args.inputs_file else ()
    if args.graphviz:
        Path(args.basepath).mkdir(parents=True, exist_ok=True)
        _report(_draw(None, str(workflow), workflow.stem, Path(args.basepath)), args.diagnostics)
    if _is_local_run(args):
        jobs = (preflight.Job(io.read_inputs_file(args.inputs_file), Path(args.inputs_file).parent.absolute(),
                              '--inputs_file'),) if args.inputs_file else ()
        if not _preflight(args, [workflow], jobs):
            return
        Path(args.basepath).mkdir(parents=True, exist_ok=True)
        _run(args, unknown_args, workflow.stem, documents=(str(workflow), *job))
    elif args.generate_cwl_workflow:
        print(f'{args.yaml} is a CWL workflow; with --allow_raw_cwl it is used as it is, and nothing is written')
    else:
        print('Please specify either --generate_cwl_workflow (compile), --check or --run_local (run)')
        sys.exit(1)


def _report(diagnostics: Iterable[Diagnostic], form: str) -> None:
    """Print diagnostics on stderr, as text or as one JSON object per line."""
    for diagnostic in diagnostics:
        print(json.dumps(diagnostic.to_json()) if form == 'json' else diagnostic, file=sys.stderr)


#: Where a failure Sophios did not expect is reported.
ISSUES: Final = 'https://github.com/PolusAI/sophios/issues'


def _report_crash(error: Exception, stem: str) -> None:
    """Say in one line that Sophios stopped on something it did not expect, and keep the whole traceback.

    The traceback goes to `error_<stem>.txt`, or to stderr when that file cannot be written.
    """
    trace = ''.join(traceback.format_exception(error))
    path = Path(f'error_{stem}.txt')
    try:
        path.write_text(trace, encoding='utf-8')
        where = f'is in {path}'
    except OSError:
        print(trace, file=sys.stderr)
        where = 'is above'
    print(f'Sophios stopped on an unexpected {type(error).__name__}: {error}. The traceback {where}; if this '
          f'line does not say what to change, please report it at {ISSUES} with the traceback.', file=sys.stderr)


def main() -> None:
    """CLI entry point: run, and convert a reported failure to its diagnostics and exit code 1."""
    args, unknown_args = cli.parser.parse_known_args()
    cli.validate_target_args(args)
    try:
        _main(args, unknown_args)
    except SophiosError as e:
        _report(e.diagnostics, args.diagnostics)
        sys.exit(1)
    except Exception as e:  # pylint: disable=broad-exception-caught
        _report_crash(e, Path(args.yaml).stem if args.yaml else 'sophios')
        sys.exit(1)


def _main(args: argparse.Namespace, unknown_args: list[str]) -> None:
    """The command line, as docs/cli-reference.md describes it."""
    if unknown_args and args.passthrough_flags != 'yes':
        hint = '. Flags for the CWL runner need --passthrough_flags yes' if args.target == 'cwl' else ''
        cli.parser.error(f'unrecognized arguments: {" ".join(unknown_args)}{hint}')
    if unknown_args and not (args.run_local or args.generate_run_script):
        cli.parser.error(f'{" ".join(unknown_args)} would go to the CWL runner, which this command does not run. '
                         'Add --run_local or --generate_run_script, or remove them')
    plugins.logging_filters()

    # `--yaml` is required unless one of the generate modes was asked for.
    # Checked here rather than by argparse: the requirement is conditional on
    # another flag, which argparse cannot express without reading `sys.argv`
    # at import time — see the note in cli.py.
    if not args.yaml and not (args.generate_config or args.generate_schemas or args.explain):
        cli.parser.error('the following arguments are required: --yaml')
    if args.ignore_validation_errors:
        print('Warning: --ignore_validation_errors is deprecated and does nothing; '
              'nothing validates a file against a schema any more.', file=sys.stderr)

    if args.explain:
        explanation = args.explain.explanation
        print(f'{args.explain} ({explanation.kind}): {explanation.meaning}')
        print(f'Fix: {explanation.fix}')
        sys.exit(0)

    if args.generate_config:
        default_config_file = io.default_config_file(Path(args.homedir))
        if default_config_file.exists():
            print(f'Config already exists. To overwrite delete {default_config_file.parent} directory. Exiting')
        else:
            io.generate_config(Path(args.homedir))
            print('Finished generating config. Exiting.')
        sys.exit(0)
    if args.generate_schemas:
        schema_path = Path(args.basepath) / 'schemas' / 'wic.json'
        _write_schema(schema_path)
        print(f'Finished generating schemas in {schema_path}. Exiting.')
        sys.exit(0)
    if not Path(args.yaml).is_file():
        cli.parser.error(f'no workflow file at {args.yaml}')
    if args.inputs_file and not Path(args.inputs_file).is_file():
        cli.parser.error(f'no inputs file at {args.inputs_file}')
    if args.allow_raw_cwl and args.target == 'cwl' and _is_plain_cwl_workflow(Path(args.yaml)):
        _pass_through(args, unknown_args)
        return
    global_config: Json = io.get_config(args.config_file, Path(args.homedir))

    tools_cwl = plugins.get_tools_cwl(global_config, args.validate_plugins, args.basepath)
    # pass around config object instead of reading from the disk!
    yml_paths = plugins.get_yml_paths(global_config, args.basepath)

    yaml_path, yaml_stem, bundle = _load_source_bundle(args, yml_paths, tools_cwl)

    # The one conversion from parsed arguments to settings, at the boundary.
    # Everything below this line takes values; no Namespace goes further.
    compiler_options, graph_settings = cli.get_dicts_for_compilation(args)
    compiler_options['inference_rules'] = global_config.get('inference_rules', {})
    compiler_options['renaming_conventions'] = global_config.get('renaming_conventions', [])
    compilation, analyses = _compile_loaded_document(yaml_path, bundle, yml_paths, tools_cwl, compiler_options)
    _report(compilation.diagnostics, args.diagnostics)
    if args.target == 'nextflow':
        _write_nextflow_target(compilation, yaml_stem, Path(args.basepath), args.diagnostics)
        return
    root_dir = Path(args.yaml).parent.absolute()
    artifact = compilation.artifact

    if args.partial_failure_enable:
        artifact = plugins.cwl_update_outputs_optional_artifact(
            artifact, args.partial_failure_success_codes_range,
            args.partial_failure_success_codes)

    artifact = pc.apply_inline_options(artifact, subworkflows=args.cwl_inline_subworkflows,
                                       runtag=args.cwl_inline_runtag)

    # A transparent background, for slides.
    drawing_graph = drawing.draw(compilation, bundle, graph_settings, yaml_path,
                                 background=None) if args.graphviz else None

    def draw_after_write() -> None:
        # cwltool draws the CWL file, so it is drawn once this compile has written it.
        if args.graphviz:
            _report(_draw(drawing_graph, str(Path(args.basepath) / f'{yaml_stem}.cwl'), yaml_stem,
                          Path(args.basepath)), args.diagnostics)

    if _is_local_run(args):
        prepared = backends.backend('cwl', args.cwl_runner).prepare(
            artifact, analyses, Path(args.basepath),
            run_setup.Settings(_run_settings(args), root_dir, args.inputs_file, check=args.check,
                               remove_entrypoints=args.docker_remove_entrypoints))
        draw_after_write()
        if args.check:
            print(f'Checked {prepared.documents[0]}: nothing in the way of a local run on this machine. '
                  'Nothing was pulled or run.')
            return
        _run(args, unknown_args, prepared.artifact.name, realtime_plans=prepared.plans)

    elif args.generate_cwl_workflow:
        io.write_artifacts_to_disk(artifact, Path(args.basepath), args.inputs_file)
        realtime.write(analyses, Path(args.basepath), artifact.name, root_dir)
        draw_after_write()
    else:
        print('Please specify either --generate_cwl_workflow (compile), --check or --run_local (run)')
        sys.exit(1)


if __name__ == '__main__':
    main()
