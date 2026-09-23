import json
# pylint: disable=redefined-outer-name
import subprocess as sub
from pathlib import Path
import signal
import sys
import argparse

import pytest
import yaml
from networkx.algorithms import isomorphism
from mergedeep import merge, Strategy

import sophios.compiler
import sophios.input_output
import sophios.run_local
import sophios.utils
from sophios.ir import frontdoor
import sophios.plugins
from sophios import auto_gen_header
from sophios.cli import get_args
from sophios.utils_yaml import wic_loader
from sophios.post_compile import (cwl_docker_extract, inline_artifact_runs,
                                  remove_artifact_entrypoints, stage_input_files)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.post_compile import verify_container_engine_config
from sophios.ir.artifacts import CompilationArtifact
from sophios.wic_types import Json
from sophios.utils_graphs import get_graph_reps

# pylint: disable-next=unused-import  # `corpus_registry` is a pytest fixture
from .test_setup import (CorpusRegistry, corpus_registry, workflow_paths,
                         yml_path_is_workflow)
from .equivalence import Strength, equivalent

yml_paths = workflow_paths()
yml_paths_tuples = [(name, path) for paths in yml_paths.values()
                    for name, path in paths.items()]

# Look in each directory of search_paths_wic tag in global_config.json
# for separate config_ci.json files and combine them.
config_ci: Json = {}
global_config = sophios.input_output.get_config(Path(get_args().config_file), Path(get_args().config_file))
search_paths_wic_tag = global_config['search_paths_wic']
for _yml_namespaces in search_paths_wic_tag:
    yml_dirs = search_paths_wic_tag[_yml_namespaces]
    for yml_dir in yml_dirs:
        config_ci_json = Path(yml_dir) / 'config_ci.json'
        if config_ci_json.exists():
            with open(config_ci_json, encoding='utf-8') as f:
                contents = f.read().splitlines()
                # Strip out comments. (Comments are not allowed in JSON)
                contents = [line for line in contents if not line.strip().startswith('//')]
                config_ci_tmp = json.loads('\n'.join(contents))
            # Use the Additive Strategy to e.g. concatenate lists
            config_ci = merge(config_ci, config_ci_tmp, strategy=Strategy.TYPESAFE_ADDITIVE)

# Due to the computational complexity of the graph isomorphism problem, we
# need to manually exclude large workflows.
# See https://en.wikipedia.org/wiki/Graph_isomorphism_problem
large_workflows: list[str] = config_ci.get("large_workflows", [])


def _is_workflow_document(yml_path: Path) -> bool:
    with open(yml_path, mode='r', encoding='utf-8') as y:
        match yaml.load(y.read(), Loader=wic_loader()):
            case {"steps": _}:
                return True
            case {"wic": {"implementations": _}}:
                return True
            case _:
                return False


yml_paths_tuples_not_large = [
    (s, p) for (s, p) in yml_paths_tuples
    if s not in large_workflows and _is_workflow_document(p) and yml_path_is_workflow(p)
]

# NOTE: Most of the workflows in this list have free variables because they are subworkflows
# i.e. if you try to run them, you will get "Missing required input parameter"
run_blacklist: list[str] = config_ci.get("run_blacklist", [])
run_weekly: list[str] = config_ci.get("run_weekly", [])
run_partial_failures: list[str] = config_ci.get("run_partial_failures", [])

yml_paths_tuples_weekly = [(s, p) for (s, p) in yml_paths_tuples if s in run_weekly]

yml_paths_tuples_not_blacklist_on_push = [(s, p) for (s, p) in yml_paths_tuples
                                          if s not in run_blacklist and s not in run_weekly]

yml_paths_partial_failure = [(s, p) for (s, p) in yml_paths_tuples
                             if s in run_partial_failures]


def is_isomorphic_with_timeout(g_m: isomorphism.GraphMatcher, yml_path_str: str) -> None:
    """Calls the .is_isomorphic() method with a timeout of 10 seconds.

    Args:
        gm (isomorphism.GraphMatcher): The graph isomorphism object.
        yml_path_str (str): The root yml workflow file for error reporting.
    """
    def handler(_signum, _frame):  # type: ignore
        line1 = 'Graph isomorphism check timed out after 10 seconds.'
        line2 = f'Consider adding {yml_path_str} to the large_workflows list.'
        raise Exception(f'{line1}\n{line2}')

    # See https://github.com/bokeh/bokeh/issues/11627#issuecomment-921576787
    if sys.platform == 'win32':
        # Windows does not support alarm, so just use a regular call here.
        # Note that there is a backup timeout in the github action workflow
        assert g_m.is_isomorphic()  # See top-level comment above!
    else:
        # See https://docs.python.org/3/library/signal.html#examples
        # NOTE: You CANNOT use `pytest --workers 8 ...` with this. Otherwise:
        # "ValueError: signal only works in main thread of the main interpreter"
        signal.signal(signal.SIGALRM, handler)

        signal.alarm(10)  # timeout after 10 seconds
        assert g_m.is_isomorphic()  # See top-level comment above!
        signal.alarm(0)  # Disable the alarm


def _artifacts(root: CompilationArtifact) -> list[CompilationArtifact]:
    """Return a stable depth-first view of an emitted artifact tree."""
    return [root, *(item for child in root.children for item in _artifacts(child))]


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_run_workflows_on_push(yml_path_str: str, yml_path: Path, cwl_runner: str,
                               corpus_registry: CorpusRegistry) -> None:
    """Runs all of the workflows auto-discovered from the various
       directories in 'search_paths_wic', excluding all workflows which have been
       blacklisted in the various config_ci.json files and excluding the weekly
       workflows."""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_run_inlined_workflows_on_push(yml_path_str: str, yml_path: Path, cwl_runner: str,
                                       corpus_registry: CorpusRegistry) -> None:
    """Inlines and runs all of the workflows auto-discovered from the various
       directories in 'search_paths_wic', excluding all workflows which have been
       blacklisted in the various config_ci.json files and excluding the weekly
       workflows."""
    args = get_args(str(yml_path), ['--cwl_inline_subworkflows'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


# partial failure tests
@pytest.mark.skip_pypi_ci
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_partial_failure)
def test_run_partial_failures_pass(yml_path_str: str, yml_path: Path, cwl_runner: str,
                                   corpus_registry: CorpusRegistry) -> None:
    """Run workflows allowing partial failures. yml files of workflows which are known to have failure steps"""
    args = get_args(str(yml_path), ['--partial_failure_enable'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_partial_failure)
def test_run_partial_failures_fail_without_flag(yml_path_str: str, yml_path: Path, cwl_runner: str,
                                                corpus_registry: CorpusRegistry) -> None:
    """Run workflows with known failures but without partial failure cli flag. It is expected to fail"""
    args = get_args(str(yml_path), [])
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry,
                  expect_success=False)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
def test_run_workflows_weekly(yml_path_str: str, yml_path: Path, cwl_runner: str,
                              corpus_registry: CorpusRegistry) -> None:
    """Runs all of the run_weekly workflows whitelisted in the various config_ci.json files."""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
def test_run_inlined_workflows_weekly(yml_path_str: str, yml_path: Path, cwl_runner: str,
                                      corpus_registry: CorpusRegistry) -> None:
    """Inlines and runs all of the run_weekly workflows whitelisted in the various config_ci.json files."""
    args = get_args(str(yml_path), ['--cwl_inline_subworkflows'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_cwl_docker_extract(yml_path_str: str, yml_path: Path,
                            corpus_registry: CorpusRegistry) -> None:
    """ Uses cwl-docker-extract to recursively `docker pull`"""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, 'cwltool', args, corpus_registry,
                  docker_pull_only=True)


# pylint: disable-next=too-many-arguments,too-many-locals
def run_workflows(
    yml_path_str: str,
    yml_path: Path,
    cwl_runner: str,
    args: argparse.Namespace,
    registry: CorpusRegistry,
    *,
    docker_pull_only: bool = False,
    expect_success: bool = True,
) -> None:
    """Runs all of the given workflows."""
    tools_cwl = registry.tools
    validator = registry.validator

    # First compile the workflow.
    # Load the high-level yaml workflow file.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    bundle = frontdoor.bundle_from_disk(Path(yml_path), yml_paths, tools_cwl)

    compiler_options, graph_settings, yaml_tag_paths = sophios.cli.get_dicts_for_compilation(args)

    graph = get_graph_reps(str(yml_path))
    result = sophios.compiler.compile_source(
        bundle, compiler_options, graph_settings, yaml_tag_paths,
        relative_run_path=True, testing=True, graph_target=graph)
    artifact = result.artifact
    if args.cwl_inline_subworkflows:
        artifact = inline_artifact_runs(artifact)
    yaml_stem = artifact.name
    basepath = 'autogenerated'

    artifact = sophios.plugins.cwl_prepend_dockerFile_include_path_artifact(artifact)
    sophios.input_output.write_artifacts_to_disk(
        artifact, Path(basepath), True, args.inputs_file)

    # verify container_engine install and config
    verify_container_engine_config(args.container_engine, args.ignore_docker_install)

    if docker_pull_only:
        cwl_docker_extract(args.container_engine, args.pull_dir, Path(basepath) / f'{Path(yml_path).stem}.cwl')
        return

    if args.docker_remove_entrypoints:
        artifact = remove_artifact_entrypoints(args.container_engine, artifact)
    sophios.input_output.write_artifacts_to_disk(
        artifact, Path(basepath), True, args.inputs_file)

    if args.partial_failure_enable:
        artifact = sophios.plugins.cwl_update_outputs_optional_artifact(
            artifact, args.partial_failure_success_codes_range,
            args.partial_failure_success_codes)
        sophios.input_output.write_artifacts_to_disk(
            artifact, Path(basepath), True, args.inputs_file)
    # NOTE: Do not use --cachedir; we want to actually test everything.
    # stage input files for run
    stage_input_files(artifact.job_inputs, Path(args.yaml).parent.absolute(), basepath)
    run_args_dict = {}
    run_args_dict['container_engine'] = args.container_engine
    run_args_dict['cwl_runner'] = cwl_runner
    run_args_dict['copy_output_files'] = str(args.copy_output_files)
    retval = sophios.run_local.run_local(run_args_dict, True,
                                         workflow_name=yaml_stem, passthrough_args=[], basepath=basepath)
    if expect_success:
        assert retval == 0
    else:
        assert retval != 0


def _discover(yml_paths: dict[str, dict[str, Path]], stem: str) -> Path | None:
    """The file a discovered workflow stem names, in whichever namespace holds it."""
    for paths in yml_paths.values():
        if stem in paths:
            return paths[stem]
    return None


def _is_includer_fragment(error: SophiosError) -> bool:
    """Whether a document failed only for edges an includer would supply.

    Such a document has no standalone compilation, so comparing one against
    an embedded one compares nothing. `sophios --generate_schemas` skips the
    same outcome for the same reason.
    """
    return bool(error.diagnostics) and all(
        item.code is SophiosErrorCode.UNDEFINED_EDGE for item in error.diagnostics)


@pytest.mark.fast
@pytest.mark.serial
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_cwl_embedding_independence(yml_path_str: str, yml_path: Path,
                                    corpus_registry: CorpusRegistry) -> None:
    """Tests that compiling a subworkflow is independent of how it is embedded
    into a parent workflow. Specifically, this compiles the root workflow and
    re-compiles every subworkflow (individually) as if it were a root workflow,
    then checks that the CWL for each subworkflow remains identical and checks
    that the embedded subworkflow DAGs and the re-compiled DAGs are isomorphic.
    """
    tools_cwl = corpus_registry.tools
    validator = corpus_registry.validator
    args = get_args(str(yml_path))

    # Load the high-level yaml workflow file.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    bundle = frontdoor.bundle_from_disk(Path(yml_path), yml_paths, tools_cwl)

    graph = get_graph_reps(str(yml_path))
    compiler_options, graph_settings, yaml_tag_paths = sophios.cli.get_dicts_for_compilation(args)

    try:
        result = sophios.compiler.compile_source(
            bundle, compiler_options, graph_settings, yaml_tag_paths,
            relative_run_path=False, testing=True, graph_target=graph)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise
    workflow_artifacts = [artifact for artifact in _artifacts(result.artifact)
                          if artifact.graph is not None]

    # This test doesn't necessarily need to write to disk, but useful for debugging.
    sophios.input_output.write_artifacts_to_disk(
        result.artifact, Path('autogenerated/'), False, args.inputs_file)

    # Now, for each subworkflow of the given root workflow, compile the
    # subworkflow again from scratch, as if it were the root workflow,
    # and check that the generated CWL is identical. In other words,
    # check that the generated CWL of a subworkflow is independent of its
    # embedding into a parent workflow.
    compared = 0
    for embedded_artifact in workflow_artifacts[1:]:
        sub_name = embedded_artifact.name
        # Its own file, compiled as a root: the subworkflow embedded above came
        # from this same text, so this is the comparison the test names. The
        # forest that used to supply the subtrees was an artifact of splicing
        # every document into one tree, which no longer happens.
        sub_path = _discover(yml_paths, sub_name)
        assert sub_path is not None, f'{sub_name} is embedded but has no file of its own'

        # NOTE: Do we want to also test embedding independence with args.graph_inline_depth?
        # If so, we will need to patch testargs depending on len(sub_node_data.namespaces)
        # (due to the various instances of `if len(namespaces) < args.graph_inline_depth`)

        graph_fakeroot = get_graph_reps(str(sub_name))
        try:
            fake_result = sophios.compiler.compile_source(
                frontdoor.bundle_from_disk(sub_path, yml_paths, tools_cwl),
                compiler_options, graph_settings, yaml_tag_paths,
                relative_run_path=False, testing=True,
                graph_target=graph_fakeroot)
        except SophiosError as error:
            if _is_includer_fragment(error):
                continue
            raise
        sub_cwl_fakeroot = fake_result.artifact.cwl

        # NOTE: Relative run: paths cause this test to fail, so remove them.
        # Using namespaced filenames in a single flat directory also
        # doesn't work because the namespaces will be of different lengths.
        sub_cwl_embedded = sophios.utils.recursively_delete_dict_key(
            'run', embedded_artifact.cwl)
        sub_cwl_fakeroot = sophios.utils.recursively_delete_dict_key('run', sub_cwl_fakeroot)

        if sub_cwl_embedded != sub_cwl_fakeroot:
            # Before we crash and burn, write out files for debugging. The
            # source itself, not a spliced forest: it is what both sides were
            # compiled from, and it is the file a reader would open.
            with open(f'{sub_name}_source.wic', mode='w', encoding='utf-8') as w:
                w.write(sub_path.read_text(encoding='utf-8'))
            # NOTE: Use _dot_cwl so we don't glob these files in get_tools_cwl()
            yaml_content = yaml.dump(sub_cwl_embedded, sort_keys=False, line_break='\n', indent=2)
            filename_emb = f'{sub_name}_embedded_dot_cwl'
            with open(filename_emb, mode='w', encoding='utf-8') as w:
                w.write('#!/usr/bin/env cwl-runner\n')
                w.write(auto_gen_header)
                w.write(''.join(yaml_content))
            yaml_content = yaml.dump(sub_cwl_fakeroot, sort_keys=False, line_break='\n', indent=2)
            filename_fake = f'{sub_name}_fakeroot_dot_cwl'
            with open(filename_fake, mode='w', encoding='utf-8') as w:
                w.write('#!/usr/bin/env cwl-runner\n')
                w.write(auto_gen_header)
                w.write(''.join(yaml_content))
            cmd = f'diff {filename_emb} {filename_fake} > {sub_name}.diff'
            sub.run(cmd, shell=True, check=False)
            print(f'Error! Check {filename_emb} and {filename_fake} and {sub_name}.diff')
        assert sub_cwl_embedded == sub_cwl_fakeroot

        # Check that the subgraphs are isomorphic.
        sub_graph_nx = embedded_artifact.graph_view.networkx
        sub_graph_fakeroot_nx = fake_result.artifact.graph_view.networkx
        g_m = isomorphism.DiGraphMatcher(sub_graph_nx, sub_graph_fakeroot_nx)
        is_isomorphic_with_timeout(g_m, yml_path_str)
        compared += 1

    # A root every one of whose subworkflows classifies as a fragment reaches
    # here having compared nothing, and used to be green for it. The `continue`
    # above is per subworkflow; this reports the case where it fired for all of
    # them, so the parametrization says what it did rather than passing
    # silently. `test_rand_fail` is one: its only child is `fail.wic`, whose
    # own first line says it is not a standalone workflow.
    if workflow_artifacts[1:] and not compared:
        pytest.skip(f'{yml_path_str}: every subworkflow is a fragment, none compiles alone')


@pytest.mark.serial
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_inline_subworkflows(yml_path_str: str, yml_path: Path,
                             corpus_registry: CorpusRegistry) -> None:
    """Embedding linked child graphs changes placement, not workflow meaning."""
    tools_cwl = corpus_registry.tools
    validator = corpus_registry.validator
    args = get_args(str(yml_path))
    # Load the high-level yaml workflow file.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    bundle = frontdoor.bundle_from_disk(Path(yml_path), yml_paths, tools_cwl)

    compiler_options, graph_settings, yaml_tag_paths = sophios.cli.get_dicts_for_compilation(args)

    graph = get_graph_reps(str(yml_path))
    try:
        result = sophios.compiler.compile_source(
            bundle, compiler_options, graph_settings, yaml_tag_paths,
            relative_run_path=True, testing=True, graph_target=graph)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise
    embedded = inline_artifact_runs(result.artifact)
    divergence = equivalent(result.artifact.cwl,
                            embedded.cwl,
                            Strength.UP_TO_EMBEDDING)
    assert divergence is None, divergence
