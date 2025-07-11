import json
import subprocess as sub
from pathlib import Path
import signal
import sys
from typing import List
import argparse

import pytest
import yaml
from networkx.algorithms import isomorphism
from mergedeep import merge, Strategy

import sophios.cli
import sophios.compiler
import sophios.inlineing
import sophios.input_output
import sophios.run_local
import sophios.utils
import sophios.ast
import sophios.plugins
from sophios import auto_gen_header
from sophios.cli import get_args
from sophios.utils_yaml import wic_loader
from sophios.post_compile import cwl_docker_extract, remove_entrypoints, find_and_create_output_dirs
from sophios.wic_types import NodeData, StepId, Yaml, YamlTree, Json
from sophios.utils_graphs import get_graph_reps

from .test_setup import tools_cwl, yml_paths, validator, yml_paths_tuples

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
            print(f'Reading {config_ci_json}')
            with open(config_ci_json) as f:
                contents = f.read().splitlines()
                # Strip out comments. (Comments are not allowed in JSON)
                contents = [line for line in contents if not line.strip().startswith('//')]
                config_ci_tmp = json.loads('\n'.join(contents))
            # Use the Additive Strategy to e.g. concatenate lists
            config_ci = merge(config_ci, config_ci_tmp, strategy=Strategy.TYPESAFE_ADDITIVE)

# Due to the computational complexity of the graph isomorphism problem, we
# need to manually exclude large workflows.
# See https://en.wikipedia.org/wiki/Graph_isomorphism_problem
large_workflows: List[str] = config_ci.get("large_workflows", [])
yml_paths_tuples_not_large = [(s, p) for (s, p) in yml_paths_tuples if s not in large_workflows]

# NOTE: Most of the workflows in this list have free variables because they are subworkflows
# i.e. if you try to run them, you will get "Missing required input parameter"
run_blacklist: List[str] = config_ci.get("run_blacklist", [])
run_weekly: List[str] = config_ci.get("run_weekly", [])
run_partial_failures: List[str] = config_ci.get("run_partial_failures", [])

yml_paths_tuples_weekly = [(s, p) for (s, p) in yml_paths_tuples if s in run_weekly]

yml_paths_tuples_not_blacklist_on_push = [(s, p) for (s, p) in yml_paths_tuples
                                          if s not in run_blacklist and s not in run_weekly]

yml_paths_partial_failure = [(s, p) for (s, p) in yml_paths_tuples
                             if s in run_partial_failures]

# currently [vs_demo_2, vs_demo_3, vs_demo_4, elm, nmr,
#            multistep1, multistep2, multistep3, helloworld, scattering_scaling]


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


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_run_workflows_on_push(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Runs all of the workflows auto-discovered from the various
       directories in 'search_paths_wic', excluding all workflows which have been
       blacklisted in the various config_ci.json files and excluding the weekly
       workflows."""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_run_inlined_workflows_on_push(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Inlines and runs all of the workflows auto-discovered from the various
       directories in 'search_paths_wic', excluding all workflows which have been
       blacklisted in the various config_ci.json files and excluding the weekly
       workflows."""
    args = get_args(str(yml_path), ['--cwl_inline_subworkflows'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


# partial failure tests
@pytest.mark.skip_pypi_ci
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_partial_failure)
def test_run_partial_failures_pass(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Run workflows allowing partial failures. yml files of workflows which are known to have failure steps"""
    args = get_args(str(yml_path), ['--partial_failure_enable'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


@pytest.mark.xfail()
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_partial_failure)
def test_run_partial_failures_xfail(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Run workflows with known failures but without partial failure cli flag. It is expected to fail"""
    args = get_args(str(yml_path), [])
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
def test_run_workflows_weekly(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Runs all of the run_weekly workflows whitelisted in the various config_ci.json files."""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
def test_run_inlined_workflows_weekly(yml_path_str: str, yml_path: Path, cwl_runner: str) -> None:
    """Inlines and runs all of the run_weekly workflows whitelisted in the various config_ci.json files."""
    args = get_args(str(yml_path), ['--cwl_inline_subworkflows'])
    run_workflows(yml_path_str, yml_path, cwl_runner, args)


@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_blacklist_on_push)
def test_cwl_docker_extract(yml_path_str: str, yml_path: Path) -> None:
    """ Uses cwl-docker-extract to recursively `docker pull`"""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, 'cwltool', args, True)
    return


def run_workflows(yml_path_str: str, yml_path: Path, cwl_runner: str, args: argparse.Namespace, docker_pull_only: bool = False) -> None:
    """Runs all of the given workflows."""

    # First compile the workflow.
    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.load(y.read(), Loader=wic_loader())
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str, plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = sophios.ast.read_ast_from_disk(args.homedir, y_t, yml_paths, tools_cwl, validator,
                                                   args.ignore_validation_errors)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_raw.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = sophios.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_merged.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))
    root_yml_dir_abs = Path(args.yaml).parent.absolute()
    yaml_tree = sophios.ast.python_script_generate_cwl(yaml_tree, root_yml_dir_abs, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_python_script.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))

    if args.cwl_inline_subworkflows:
        while True:
            # Inlineing changes the namespaces, so we have to get new namespaces after each inlineing operation.
            namespaces_list = sophios.inlineing.get_inlineable_subworkflows(yaml_tree, tools_cwl, False, [])
            if namespaces_list == []:
                break

            yaml_tree, _len_substeps = sophios.inlineing.inline_subworkflow(yaml_tree, namespaces_list[0])

        # Append _inline here instead of in input_output.write_to_disk()
        step_id = StepId(yaml_tree.step_id.stem + '_inline', yaml_tree.step_id.plugin_ns)
        yaml_tree = YamlTree(step_id, yaml_tree.yml)

        with open(f'autogenerated/{Path(yml_path).stem}_tree_merged_inlined.wic', mode='w', encoding='utf-8') as f:
            f.write(yaml.dump(yaml_tree.yml))

    graph = get_graph_reps(str(yml_path))
    compiler_info = sophios.compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {}, {},
                                                      tools_cwl, True, relative_run_path=True, testing=True)
    rose_tree = compiler_info.rose
    sub_node_data: NodeData = rose_tree.data
    yaml_stem = sub_node_data.name

    rose_tree = sophios.plugins.cwl_prepend_dockerFile_include_path_rosetree(rose_tree)
    sophios.input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)

    if docker_pull_only:
        cwl_docker_extract(args.container_engine, args.pull_dir, Path(yml_path).stem)
        return

    if args.docker_remove_entrypoints:
        rose_tree = remove_entrypoints(args.container_engine, rose_tree)
    sophios.input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)

    if args.partial_failure_enable:
        rose_tree = sophios.plugins.cwl_update_outputs_optional_rosetree(rose_tree)
        sophios.input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)
    # NOTE: Do not use --cachedir; we want to actually test everything.
    find_and_create_output_dirs(rose_tree)
    retval = sophios.run_local.run_local(args, rose_tree, None, cwl_runner, True)
    assert retval == 0


@pytest.mark.fast
@pytest.mark.serial
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_cwl_embedding_independence(yml_path_str: str, yml_path: Path) -> None:
    """Tests that compiling a subworkflow is independent of how it is embedded
    into a parent workflow. Specifically, this compiles the root workflow and
    re-compiles every subworkflow (individually) as if it were a root workflow,
    then checks that the CWL for each subworkflow remains identical and checks
    that the embedded subworkflow DAGs and the re-compiled DAGs are isomorphic.
    """
    args = get_args(str(yml_path))

    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.load(y.read(), Loader=wic_loader())
    # Write the combined workflow (with all subworkflows as children) to disk.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str + '.wic', plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = sophios.ast.read_ast_from_disk(args.homedir, y_t, yml_paths, tools_cwl, validator,
                                                   args.ignore_validation_errors)
    with open(f'autogenerated/{yml_path.stem}_tree_raw.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = sophios.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{yml_path.stem}_tree_merged.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))
    root_yml_dir_abs = Path(args.yaml).parent.absolute()
    yaml_tree = sophios.ast.python_script_generate_cwl(yaml_tree, root_yml_dir_abs, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_python_script.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))

    # NOTE: The entire purpose of parsing an entire yaml forest is so we
    # can easily access the subtrees here. (i.e. without re-walking the AST)
    yaml_forest = sophios.ast.tree_to_forest(yaml_tree, tools_cwl)
    yaml_forest_lst = sophios.utils.flatten_forest(yaml_forest)
    yaml_forest_lst = [yf for yf in yaml_forest_lst if ".wic" not in yf.yaml_tree.step_id.stem]

    graph = get_graph_reps(str(yml_path))
    is_root = True
    compiler_info = sophios.compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {}, {},
                                                      tools_cwl, is_root, relative_run_path=False, testing=True)
    rose_tree = compiler_info.rose
    node_data_lst: List[NodeData] = sophios.utils.flatten_rose_tree(rose_tree)
    node_data_lst = [node for node in node_data_lst if ".wic" in node.name[0]]

    # This test doesn't necessarily need to write to disk, but useful for debugging.
    sophios.input_output.write_to_disk(rose_tree, Path('autogenerated/'), False, args.inputs_file)

    # Now, for each subworkflow of the given root workflow, compile the
    # subworkflow again from scratch, as if it were the root workflow,
    # and check that the generated CWL is identical. In other words,
    # check that the generated CWL of a subworkflow is independent of its
    # embedding into a parent workflow.
    assert len(node_data_lst[1:]) == len(yaml_forest_lst)  # There is an additional node
    for sub_node_data, sub_yaml_forest in zip(node_data_lst[1:], yaml_forest_lst):
        sub_name = sub_node_data.name
        assert sub_yaml_forest.yaml_tree.step_id.stem == sub_name + '.wic'

        # NOTE: Do we want to also test embedding independence with args.graph_inline_depth?
        # If so, we will need to patch testargs depending on len(sub_node_data.namespaces)
        # (due to the various instances of `if len(namespaces) < args.graph_inline_depth`)

        graph_fakeroot = get_graph_reps(str(sub_name))
        fake_root = True
        compiler_info_fakeroot = sophios.compiler.compile_workflow(sub_yaml_forest.yaml_tree, get_args(str(yml_path)),
                                                                   [], [graph_fakeroot], {}, {}, {}, {}, tools_cwl, fake_root, relative_run_path=False, testing=True)
        sub_node_data_fakeroot: NodeData = compiler_info_fakeroot.rose.data
        sub_cwl_fakeroot = sub_node_data_fakeroot.compiled_cwl

        # NOTE: Relative run: paths cause this test to fail, so remove them.
        # Using namespaced filenames in a single flat directory also
        # doesn't work because the namespaces will be of different lengths.
        sub_cwl_embedded = sophios.utils.recursively_delete_dict_key('run', sub_node_data.compiled_cwl)
        sub_cwl_fakeroot = sophios.utils.recursively_delete_dict_key('run', sub_cwl_fakeroot)

        if sub_cwl_embedded != sub_cwl_fakeroot:
            # Before we crash and burn, write out files for debugging.
            with open(f'{sub_name}_forest_embedded.wic', mode='w', encoding='utf-8') as w:
                w.write(yaml.dump(yaml_forest))
            with open(f'{sub_name}_forest_fakeroot.wic', mode='w', encoding='utf-8') as w:
                w.write(yaml.dump(sub_yaml_forest))
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
        sub_graph_nx = sub_node_data.graph.networkx
        sub_graph_fakeroot_nx = sub_node_data_fakeroot.graph.networkx
        # assert isomorphism.faster_could_be_isomorphic(sub_graph_nx, sub_graph_fakeroot_nx)
        g_m = isomorphism.GraphMatcher(sub_graph_nx, sub_graph_fakeroot_nx)
        print('is_isomorphic()?', yml_path_str, sub_name)
        is_isomorphic_with_timeout(g_m, yml_path_str)


@pytest.mark.serial
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_not_large)
def test_inline_subworkflows(yml_path_str: str, yml_path: Path) -> None:
    """Tests that compiling a workflow is independent of how subworkflows are inlined.
    Specifically, this inlines every subworkflow (individually) and checks that
    the original DAG and the inlined DAGs are isomorphic.
    """
    args = get_args(str(yml_path))
    # Load the high-level yaml workflow file.
    with open(yml_path, mode='r', encoding='utf-8') as y:
        root_yaml_tree: Yaml = yaml.load(y.read(), Loader=wic_loader())
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_tag = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_tag['wic'].get('namespace', 'global')
    step_id = StepId(yml_path_str, plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = sophios.ast.read_ast_from_disk(args.homedir, y_t, yml_paths, tools_cwl, validator,
                                                   args.ignore_validation_errors)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_raw.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree_raw.yml))
    yaml_tree = sophios.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_merged.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))
    root_yml_dir_abs = Path(args.yaml).parent.absolute()
    yaml_tree = sophios.ast.python_script_generate_cwl(yaml_tree, root_yml_dir_abs, tools_cwl)
    with open(f'autogenerated/{Path(yml_path).stem}_tree_python_script.wic', mode='w', encoding='utf-8') as f:
        f.write(yaml.dump(yaml_tree.yml))

    namespaces_list = sophios.inlineing.get_inlineable_subworkflows(
        yaml_tree, tools_cwl, 'implementation' in wic_tag, [])
    if namespaces_list == []:
        assert True  # There's nothing to test

    graph = get_graph_reps(str(yml_path))
    compiler_info = sophios.compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {}, {},
                                                      tools_cwl, True, relative_run_path=True, testing=True)
    rose_tree = compiler_info.rose
    sub_node_data: NodeData = rose_tree.data

    sophios.input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)

    # Inline each subworkflow individually and check that the graphs are isomorphic.
    for namespaces in namespaces_list:
        inline_yaml_tree, _len_substeps = sophios.inlineing.inline_subworkflow(yaml_tree, namespaces)

        inline_graph = get_graph_reps(str(yml_path))
        inline_compiler_info = sophios.compiler.compile_workflow(inline_yaml_tree, args,
                                                                 [], [inline_graph], {}, {}, {}, {}, tools_cwl, True, relative_run_path=True, testing=True)
        inline_rose_tree = inline_compiler_info.rose
        inline_sub_node_data: NodeData = inline_rose_tree.data

        # Check that the subgraphs are isomorphic.
        sub_graph_nx = sub_node_data.graph.networkx
        sub_graph_fakeroot_nx = inline_sub_node_data.graph.networkx
        # assert isomorphism.faster_could_be_isomorphic(sub_graph_nx, sub_graph_fakeroot_nx)
        g_m = isomorphism.DiGraphMatcher(sub_graph_nx, sub_graph_fakeroot_nx)
        print('is_isomorphic()?', yml_path_str, namespaces)
        is_isomorphic_with_timeout(g_m, yml_path_str)
