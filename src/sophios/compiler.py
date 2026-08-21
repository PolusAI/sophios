import copy
import json
import os
from pathlib import Path
import sys
from typing import Any, NamedTuple, cast

import graphviz
from mergedeep import merge, Strategy
import networkx as nx
import yaml

from . import input_output as io
from . import inference, utils, utils_cwl, utils_graphs
from .utils_yaml import Key
from .wic_types import (CompilerInfo, CompilerOptions, EnvData, ExplicitEdgeCalls,
                        ExplicitEdgeDefs, GraphData, GraphReps, GraphSettings, Namespaces,
                        NodeData, RoseTree, Tool, Tools, WorkflowInputs, WorkflowInputsFile,
                        WorkflowOutputs, Yaml, YamlTagPaths, YamlTree, StepId)
from .lang.cwl import CWL_VERSION

# NOTE: This must be initialized in main.py and/or cwl_subinterpreter.py
inference_rules: dict[str, str] = {}


def compile_workflow(yaml_tree_ast: YamlTree,
                     compiler_options: CompilerOptions,
                     graph_settings: GraphSettings,
                     yaml_tag_paths: YamlTagPaths,
                     namespaces: Namespaces,
                     subgraphs_: list[GraphReps],
                     explicit_edge_defs: ExplicitEdgeDefs,
                     explicit_edge_calls: ExplicitEdgeCalls,
                     input_mapping: dict[str, list[str]],
                     output_mapping: dict[str, str],
                     tools: Tools,
                     is_root: bool,
                     relative_run_path: bool,
                     testing: bool) -> CompilerInfo:
    """fixed-point wrapper around compile_workflow_once
    See https://en.wikipedia.org/wiki/Fixed_point_(mathematics)

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        compiler_options (CompilerOptions): The core flags needed for compilation and transformation into CWL
        graph_settings (GraphSettings): The settings dict for graphpviz graphs
        yaml_tag_paths (YamlTagPaths): The paths that need to be included in (generated) yaml tags
        namespaces (Namespaces): See compile_workflow_once.
        subgraphs_ (list[GraphReps]): See compile_workflow_once.
        explicit_edge_defs (ExplicitEdgeDefs): See compile_workflow_once.
        explicit_edge_calls (ExplicitEdgeCalls): See compile_workflow_once.
        input_mapping (dict[str, list[str]]): See compile_workflow_once.
        output_mapping (dict[str, str]): See compile_workflow_once.
        tools (Tools): See compile_workflow_once.
        is_root (bool): See compile_workflow_once.
        relative_run_path (bool): See compile_workflow_once.
        testing (bool): See compile_workflow_once.

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows
        (in the Rose Tree) together with mutable cumulative environment
        information which needs to be passed through the recursion.
    """
    ast_modified = True
    yaml_tree = yaml_tree_ast
    # There ought to be at most one insertion between each step.
    # If everything is working correctly, we should thus reach the fixed point
    # in at most n-1 iterations. However, due to the possibility of bugs in the
    # implementation and/or spurious inputs, we should guarantee termination.
    max_iters = 100  # 100 ought to be plenty. TODO: calculate n-1 from steps:
    i = 0
    while ast_modified and i < max_iters:
        subgraphs = copy.deepcopy(subgraphs_)  # See comment below!
        compiler_info = compile_workflow_once(yaml_tree, compiler_options, graph_settings, yaml_tag_paths,
                                              namespaces, subgraphs, explicit_edge_defs, explicit_edge_calls,
                                              input_mapping, output_mapping,
                                              tools, is_root, relative_run_path, testing)
        node_data: NodeData = compiler_info.rose.data
        ast_modified = not yaml_tree.yml == node_data.yml
        if ast_modified:
            yaml_tree = YamlTree(yaml_tree_ast.step_id, node_data.yml)
        i += 1

    # Overwrite subgraphs_ element-wise
    # This is a terrible hack due to the fact that the graphviz library API
    # only allows appending to the body. This introduces mutable state, so each
    # time we speculatively compile we accumulate duplicate nodes and edges.
    # The 'correct' solution is to store the nodes and edges that you want to
    # add in a separate data structure, return them from compile_workflow_once,
    # and only once we have reached the fixed point then add them here. Due to
    # labeling and styling and other graphviz metadata that is not trivial, so
    # instead we simply deepcopy and overwrite the bodies here.
    # (Also note that you have to do this element-wise; you cannot simply write
    # subgraphs_ = subgraphs because that will only overwrite the local binding
    # and thus it will not affect the call site of compile_workflow!)
    for si, subgraph_ in enumerate(subgraphs_):
        subgraph_.graphviz.body = subgraphs[si].graphviz.body
        subgraph_.graphdata.name = subgraphs[si].graphdata.name
        subgraph_.graphdata.nodes = subgraphs[si].graphdata.nodes
        subgraph_.graphdata.edges = subgraphs[si].graphdata.edges
        subgraph_.graphdata.subgraphs = subgraphs[si].graphdata.subgraphs
        subgraph_.networkx.clear()
        subgraph_.networkx.update(
            subgraphs[si].networkx.edges, subgraphs[si].networkx.nodes)

    if i == max_iters:
        print(yaml.dump(node_data.yml))
        raise RuntimeError(
            f'Error! Maximum number of iterations ({max_iters}) reached in compile_workflow!')
    return compiler_info


def _arg_has_default_or_is_optional(arg: str, in_tool: dict[str, Any]) -> bool:
    """Returns True if the CommandLineTool input `arg` has a default value,\n
    or if its type permits null, using either the '?' syntactic sugar or the\n
    canonical null-union representation. See canonicalize_type in utils_cwl.py.
    """
    arg_type = in_tool[arg]['type']
    has_default = in_tool[arg].get('default')
    optional_suffix = isinstance(arg_type, str) and arg_type[-1] == '?'
    optional_union = isinstance(arg_type, list) and 'null' in arg_type
    return bool(has_default or optional_suffix or optional_union)


class _WorkflowSetup(NamedTuple):
    """Return type of _prepare_compilation_state(): normalized AST + mutable state for the per-step loop."""
    yaml_path: str
    yaml_tree: Yaml
    yaml_tree_orig: Yaml
    yaml_stem: str
    wic: Yaml
    wic_steps: Yaml
    steps: list[Yaml]
    steps_keys: list[str]
    subkeys: list[str]
    inputs_workflow: WorkflowInputs
    inputs_file_workflow: WorkflowInputsFile
    input_mapping_copy: dict[str, list[str]]
    output_mapping_copy: dict[str, str]
    outputs_workflow: WorkflowOutputs
    vars_workflow_output_internal: list[str]
    explicit_edge_defs_copy: ExplicitEdgeDefs
    explicit_edge_calls_copy: ExplicitEdgeCalls
    explicit_edge_defs_copy2: ExplicitEdgeDefs
    explicit_edge_calls_copy2: ExplicitEdgeCalls
    step_1_names: list[str]
    sibling_subgraphs: list[GraphReps]
    rose_tree_list: list[RoseTree]
    graph: GraphReps
    graph_gv: graphviz.Digraph
    graph_nx: nx.DiGraph
    graphdata: GraphData
    tools_lst: list[Tool]


def _prepare_compilation_state(yaml_tree_ast: YamlTree,
                               namespaces: Namespaces,
                               subgraphs: list[GraphReps],
                               explicit_edge_defs: ExplicitEdgeDefs,
                               explicit_edge_calls: ExplicitEdgeCalls,
                               input_mapping: dict[str, list[str]],
                               output_mapping: dict[str, str],
                               testing: bool) -> _WorkflowSetup:
    """Normalizes the yml AST and initializes the mutable state used by compile_workflow_once's per-step loop.

    Raises:
        ValueError: If the yml AST is malformed (e.g. missing steps).
        TypeError: If a top-level tag has the wrong type (e.g. $namespaces/$schemas)
    """
    # NOTE: Use deepcopy so that when we delete wic: we don't modify any call sites
    (step_id, yaml_tree) = copy.deepcopy(yaml_tree_ast)
    yaml_path = step_id.stem
    # We also want another copy of the original AST so that if we need to modify it,
    # we can return the modified AST to the call site and re-compile.
    (_, yaml_tree_orig) = copy.deepcopy(yaml_tree_ast)

    if not testing:
        print(' starting compilation of', ('  ' * len(namespaces)) + yaml_path)

    # Check for top-level yml dsl args
    wic = {'wic': yaml_tree.get('wic') or {}}
    if 'wic' in yaml_tree:
        del yaml_tree['wic']
    wic_steps = wic['wic'].get('steps', {})

    yaml_stem = Path(yaml_path).stem

    (_, yaml_tree) = utils.extract_implementation(
        yaml_tree, wic['wic'], Path(yaml_path))
    steps: list[Yaml] = yaml_tree['steps']

    steps_keys = utils.get_steps_keys(steps)

    subkeys = utils.get_subkeys(steps_keys)

    if not steps_keys:
        raise ValueError('Error! workflows must define at least one step.')

    # Add headers
    # The one substrate version, owned by lang/cwl.py. The old comment here
    # claimed cromwell as the reason to stay on 1.0; nothing in this tree
    # runs cromwell (cwltool and toil are the supported runners), and 1.2 is
    # what conditional workflows already rely on. A static scan bans version
    # literals outside the owner module (tests/core/test_cwl_version.py).
    yaml_tree['cwlVersion'] = yaml_tree.get('cwlVersion', CWL_VERSION)
    yaml_tree['class'] = 'Workflow'
    namespaces_value = yaml_tree.get('$namespaces', {})
    if namespaces_value is None:
        namespaces_value = {}
    if not isinstance(namespaces_value, dict):
        raise TypeError(
            'Error! $namespaces tag must be a dictionary if present.')
    yaml_tree['$namespaces'] = {**namespaces_value,
                                **{'edam': 'https://edamontology.org/'}}
    edam_dev_owl = 'https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'
    schemas = yaml_tree.get('$schemas', [])
    if schemas is None:
        schemas = []
    if not isinstance(schemas, list):
        raise TypeError('Error! $schemas tag must be a list if present.')
    if edam_dev_owl not in schemas:
        schemas.append(edam_dev_owl)
        yaml_tree['$schemas'] = schemas

    # Collect workflow input parameters
    inputs_workflow: WorkflowInputs = {}
    inputs_file_workflow: WorkflowInputsFile = {}

    # Collect workflow input/output to workflow step input/output mappings
    input_mapping_copy = copy.deepcopy(input_mapping)
    output_mapping_copy = copy.deepcopy(output_mapping)

    # Collect the internal workflow output variables
    outputs_workflow: WorkflowOutputs = []
    vars_workflow_output_internal: list[str] = []

    # Copy recursive explicit edge variable definitions and call sites.
    explicit_edge_defs_copy = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy = copy.deepcopy(explicit_edge_calls)
    # Unlike the first copies which are mutably updated, these are returned
    # unmodified so that we can test that compilation is embedding independent.
    explicit_edge_defs_copy2 = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy2 = copy.deepcopy(explicit_edge_calls)

    # Collect recursive subworkflow data
    step_1_names: list[str] = []
    sibling_subgraphs: list[GraphReps] = []

    rose_tree_list: list[RoseTree] = []

    graph = subgraphs[-1]  # Get the current graph
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    graphdata = graph.graphdata

    tools_lst: list[Tool] = []

    return _WorkflowSetup(yaml_path, yaml_tree, yaml_tree_orig, yaml_stem, wic, wic_steps,
                          steps, steps_keys, subkeys, inputs_workflow, inputs_file_workflow,
                          input_mapping_copy, output_mapping_copy, outputs_workflow,
                          vars_workflow_output_internal, explicit_edge_defs_copy,
                          explicit_edge_calls_copy, explicit_edge_defs_copy2,
                          explicit_edge_calls_copy2, step_1_names, sibling_subgraphs,
                          rose_tree_list, graph, graph_gv, graph_nx, graphdata, tools_lst)


def _finalize_compilation(*,
                          wic: Yaml,
                          namespaces: Namespaces,
                          yaml_stem: str,
                          graph_settings: GraphSettings,
                          graph: GraphReps,
                          sibling_subgraphs: list[GraphReps],
                          step_1_names: list[str],
                          steps_keys: list[str],
                          subkeys: list[str],
                          yaml_tree: Yaml,
                          inputs_workflow: WorkflowInputs,
                          output_mapping_copy: dict[str, str],
                          vars_workflow_output_internal: list[str],
                          is_root: bool,
                          steps: list[Yaml],
                          outputs_workflow: WorkflowOutputs,
                          tools_lst: list[Tool],
                          step_node_name: str,
                          tools: Tools,
                          wic_steps: Yaml,
                          inputs_file_workflow: WorkflowInputsFile,
                          testing: bool,
                          yaml_path: str,
                          tool_i: Tool,
                          yaml_tree_orig: Yaml,
                          explicit_edge_defs_copy2: ExplicitEdgeDefs,
                          explicit_edge_calls_copy2: ExplicitEdgeCalls,
                          rose_tree_list: list[RoseTree],
                          input_mapping_copy: dict[str, list[str]],
                          explicit_edge_defs_copy: ExplicitEdgeDefs,
                          explicit_edge_calls_copy: ExplicitEdgeCalls) -> CompilerInfo:
    """Finishes compile_workflow_once: graphviz subgraphs, workflow-level in/outs, unique step names,
    final RoseTree/EnvData/CompilerInfo. Mutates `graph`, `output_mapping_copy`, and maybe `yaml_tree` in place.
    """
    # NOTE: add_subgraphs currently mutates graph
    wic_graphviz = wic['wic'].get('graphviz', {})
    ranksame_strs = wic_graphviz.get('ranksame', [])
    ranksame_pairs = [utils.parse_int_string_tuple(x) for x in ranksame_strs]
    steps_ranksame = []
    for num, name in ranksame_pairs:
        step_name_num = utils.step_name_str(yaml_stem, num-1, name)
        step_name_nss = '___'.join(namespaces + [step_name_num])
        # Escape with double quotes.
        steps_ranksame.append(f'"{step_name_nss}"')
    utils_graphs.add_subgraphs(
        graph_settings, graph, sibling_subgraphs, namespaces, step_1_names, steps_ranksame)
    step_name_1 = utils.get_step_name_1(
        step_1_names, yaml_stem, namespaces, steps_keys, subkeys)

    # Add the provided workflow inputs to the workflow inputs from each step
    inputs_combined = {**yaml_tree.get('inputs', {}), **inputs_workflow}
    yaml_tree.update({'inputs': inputs_combined})

    # NOTE: This is a nasty hack because we don't have any syntax for mapping workflow outputs.
    for k, v in yaml_tree.get('outputs', {}).items():
        # Assume the user has manually added the correct namespaced CWL dependency.
        output_mapping_copy[k] = v['outputSource'].replace('/', '___')

    vars_workflow_output_internal = list(
        set(vars_workflow_output_internal))  # Get uniques
    workflow_outputs = utils_cwl.get_workflow_outputs(graph_settings, namespaces, is_root, yaml_stem,
                                                      steps, outputs_workflow, vars_workflow_output_internal,
                                                      graph, tools_lst, step_node_name, tools)
    # Add the provided workflow outputs to the workflow outputs from each step
    outputs_combined = {**yaml_tree.get('outputs', {}), **workflow_outputs}
    yaml_tree.update({'outputs': outputs_combined})

    # NOTE: currently mutates yaml_tree (maybe)
    utils_cwl.maybe_add_requirements(yaml_tree, steps_keys, wic_steps, subkeys)

    # Finally, rename the steps to be unique
    # and convert the list of steps into a dict / list
    steps_list = []
    for i, step_key in enumerate(steps_keys):
        wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
        plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')
        stepid = StepId(Path(step_key).stem, plugin_ns_i)

        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        step_name_or_key = step_name_i if step_key.endswith(
            '.wic') or stepid in tools else step_key
        step_i_copy = {**steps[i]}
        utils.require_step_id(step_i_copy)
        del step_i_copy['id']
        step_i_copy = {'id': step_name_or_key, **step_i_copy}
        steps_list.append(step_i_copy)
    yaml_tree.update({'steps': steps_list})

    yaml_inputs = generate_yaml_inputs(inputs_file_workflow)

    if not testing:
        print('finishing compilation of', ('  ' * len(namespaces)) + yaml_path)
    # Note: We do not necessarily need to return inputs_workflow.
    # 'Internal' inputs are encoded in yaml_tree. See Comment above.
    node_data = NodeData(namespaces, yaml_stem, yaml_tree_orig, yaml_tree, tool_i, yaml_inputs,
                         explicit_edge_defs_copy2, explicit_edge_calls_copy2,
                         graph, inputs_workflow, step_name_1)
    rose_tree = RoseTree(node_data, rose_tree_list)
    env_data = EnvData(input_mapping_copy, output_mapping_copy, inputs_file_workflow, vars_workflow_output_internal,
                       explicit_edge_defs_copy, explicit_edge_calls_copy)
    compiler_info = CompilerInfo(rose_tree, env_data)
    return compiler_info


def compile_workflow_once(yaml_tree_ast: YamlTree,
                          compiler_options: CompilerOptions,
                          graph_settings: GraphSettings,
                          yaml_tag_paths: YamlTagPaths,
                          namespaces: Namespaces,
                          subgraphs: list[GraphReps],
                          explicit_edge_defs: ExplicitEdgeDefs,
                          explicit_edge_calls: ExplicitEdgeCalls,
                          input_mapping: dict[str, list[str]],
                          output_mapping: dict[str, str],
                          tools: Tools,
                          is_root: bool,
                          relative_run_path: bool,
                          testing: bool) -> CompilerInfo:
    """STOP: Have you read the Developer's Guide?? docs/devguide.md\n
    Recursively compiles yml workflow definition ASTs to CWL file contents

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        graph_settings (GraphSettings): The settings dict for graphpviz graphs
        yaml_tag_paths (YamlTagPaths): The paths that need to be included in (generated) yaml tags
        compiler_options (CompilerOptions): The core flags needed for compilation and transformation into CWL
        namespaces (Namespaces): Specifies the path in the yml AST to the current subworkflow
        subgraphs (list[Graph]): The graphs associated with the parent workflows of the current subworkflow
        explicit_edge_defs (ExplicitEdgeDefs): Stores the (path, value) of the explicit edge definition sites
        explicit_edge_calls (ExplicitEdgeCalls): Stores the (path, value) of the explicit edge call sites
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl().\n
        yml files that have been compiled to CWL SubWorkflows are also added during compilation.
        is_root (bool): True if this is the root workflow
        relative_run_path (bool): Controls whether to use subdirectories or\n
        just one directory when writing the compiled CWL files to disk
        testing: Used to disable some optional features which are unnecessary for testing.

    Raises:
        ValueError: If the yml AST is malformed (e.g. missing steps, or a step\n
        references an input/output/tool that cannot be resolved)
        TypeError: If a top-level tag has the wrong type (e.g. $namespaces/$schemas)
        AssertionError: If an internal invariant is violated (indicates a bug)

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows\n
        (in the Rose Tree) together with mutable cumulative environment\n
        information which needs to be passed through the recursion.
    """
    setup = _prepare_compilation_state(yaml_tree_ast, namespaces, subgraphs, explicit_edge_defs,
                                       explicit_edge_calls, input_mapping, output_mapping, testing)
    # NOTE: graphdata and vars_workflow_output_internal are kept as local aliases (rather than
    # accessed via setup.graphdata / setup.vars_workflow_output_internal) because the loop below
    # rebinds/augments them directly (`graphdata = GraphData(...)` for subworkflow steps;
    # `vars_workflow_output_internal += ...`), and NamedTuple fields cannot be reassigned.
    # Every other field of `setup` is only ever read or mutated in place below, so it's
    # accessed directly as setup.<field>.
    graphdata = setup.graphdata
    vars_workflow_output_internal = setup.vars_workflow_output_internal

    for i, step_key in enumerate(setup.steps_keys):
        step_name_i = utils.step_name_str(setup.yaml_stem, i, step_key)
        stem = Path(step_key).stem
        wic_step_i = setup.wic_steps.get(f'({i+1}, {step_key})', {})
        # NOTE: See comments in read_ast_from_disk()
        plugin_ns_i = wic_step_i.get('wic', {}).get(
            'namespace', 'global')  # plugin_ns ??
        stepid = StepId(stem, plugin_ns_i)

        step_name_or_key = step_name_i if step_key.endswith(
            '.wic') or stepid in tools else step_key

        # Recursively compile subworkflows, adding compiled cwl file contents to tools
        ast_modified = False
        if step_key in setup.subkeys:
            # NOTE: step_name_or_key should always be step_name_i in this branch
            # Extract the sub yaml file that we pre-loaded from disk.
            sub_yml = setup.steps[i]['subtree']
            sub_yaml_tree = YamlTree(StepId(step_key, plugin_ns_i), sub_yml)

            # get the label (if any) from the subworkflow
            step_i_wic_graphviz = sub_yml.get('wic', {}).get('graphviz', {})
            label = step_i_wic_graphviz.get('label', step_key)
            style = step_i_wic_graphviz.get('style', '')

            subgraph_gv = graphviz.Digraph(name=f'cluster_{step_key}')
            subgraph_gv.attr(label=label)  # str(path)
            subgraph_gv.attr(color='lightblue')  # color of outline
            if style != '':
                subgraph_gv.attr(style=style)
            subgraph_nx = nx.DiGraph()
            graphdata = GraphData(step_key)
            subgraph = GraphReps(subgraph_gv, subgraph_nx, graphdata)

            sub_compiler_info = compile_workflow(sub_yaml_tree, compiler_options, graph_settings, yaml_tag_paths,
                                                 namespaces +
                                                 [step_name_or_key], subgraphs +
                                                 [subgraph],
                                                 setup.explicit_edge_defs_copy, setup.explicit_edge_calls_copy,
                                                 setup.input_mapping_copy, setup.output_mapping_copy,
                                                 tools, False, relative_run_path, testing)

            sub_rose_tree = sub_compiler_info.rose
            setup.rose_tree_list.append(sub_rose_tree)

            sub_node_data: NodeData = sub_rose_tree.data
            sub_env_data = sub_compiler_info.env

            ast_modified = not sub_yaml_tree.yml == sub_node_data.yml
            if ast_modified:
                # Propagate the updated yaml_tree (and wic: tags) upwards.
                # Since we already called ast.merge_yml_trees() before initially
                # compiling, the only way the child wic: tags can differ from
                # the parent is if there were modifications during compilation.
                # In other words, it should be safe to simply replace the
                # relevant yaml_tree and wic: tags in the parent with the child
                # values.
                print('AST modified', step_key)
                setup.wic_steps[f'({i+1}, {step_key})'] = {'wic': sub_node_data.yml.get('wic') or {}}
                wic_step_i = setup.wic_steps.get(f'({i+1}, {step_key})', {})

            # Add arguments to the compiled subworkflow (if any), being careful
            # to remove any child wic: metadata annotations. Post-compilation
            # arguments can now be added either directly inline or as metadata.
            wic_step_i_copy = copy.deepcopy(wic_step_i)
            if 'wic' in wic_step_i_copy:
                del wic_step_i_copy['wic']
            # NOTE: To support overloading, the metadata args must overwrite the parent args!
            steps_i_id = utils.require_step_id(setup.steps[i])
            args_provided_dict = merge(setup.steps[i]['parentargs'], wic_step_i_copy,
                                       strategy=Strategy.TYPESAFE_REPLACE)  # TYPESAFE_ADDITIVE ?
            setup.steps[i] = {**args_provided_dict, 'id': steps_i_id}

            # TODO: Just subgraph?
            setup.sibling_subgraphs.append(sub_node_data.graph)
            setup.step_1_names.append(sub_node_data.step_name_1)
            tool_i = Tool(stem + '.cwl', sub_node_data.compiled_cwl)

            # Initialize the above from recursive values.
            # Do not initialize inputs_workflow. See comment below.
            inputs_namespaced = {f'{step_name_or_key}___{k}': val
                                 for k, val in sub_env_data.inputs_file_workflow.items()}
            setup.inputs_file_workflow.update(inputs_namespaced)

            input_mapping_copy_namespaced = {f'{step_name_or_key}___{k}': val for k, val in
                                             sub_env_data.input_mapping.items() if k not in input_mapping}
            setup.input_mapping_copy.update(input_mapping_copy_namespaced)

            output_mapping_copy_namespaced = {f'{step_name_or_key}___{k}': val for k, val in
                                              sub_env_data.output_mapping.items() if k not in output_mapping}
            setup.output_mapping_copy.update(output_mapping_copy_namespaced)

            vars_workflow_output_internal += sub_env_data.vars_workflow_output_internal
            setup.explicit_edge_defs_copy.update(sub_env_data.explicit_edge_defs)
            setup.explicit_edge_calls_copy.update(sub_env_data.explicit_edge_calls)
        else:
            run_tag = setup.steps[i].get('run', '')
            run_tag_stem = Path(run_tag).stem if isinstance(
                run_tag, str) else ''
            stepid_runtag = StepId(run_tag_stem, 'global')

            if stepid in tools:
                # Use auto-discovery mechanism (with step_key)
                tool_i = tools[stepid]
            elif stepid_runtag in tools:
                # Use auto-discovery mechanism (with run tag)
                tool_i = tools[stepid_runtag]
            else:
                msg = f"Error! Neither {stepid.stem} nor {stepid_runtag.stem} found!"
                msg += "check your 'search_paths_cwl' in global_config.json"
                raise ValueError(msg)
            # Programmatically modify tool_i here
            graph_dummy = setup.graph  # Just use anything here to satisfy mypy
            name_stem = Path(tool_i.run_path).stem
            node_data_base_case = NodeData([step_name_or_key], name_stem, {}, tool_i.cwl,
                                           tool_i, {}, {}, {}, graph_dummy, {}, '')
            rose_tree_base_case = RoseTree(node_data_base_case, [])
            setup.rose_tree_list.append(rose_tree_base_case)
        setup.tools_lst.append(tool_i)

        # Add run tag, using relative or flat-directory paths
        # NOTE: run: path issues were causing test_cwl_embedding_independence()
        # to fail, so I simply ignore the run tag in that test.
        # Instead of referencing the original CWL CommandLineTools in-place,
        # let's copy them to autogenerated/ so we can programmatically modify them.
        # Note that this will cause name collisions if relative_run_path=False
        # and when using the 'same' CommandLineTool twice. i.e. two different
        # modifications will get written to the same cwl file, so the last one 'wins'.
        # (So don't use relative_run_path=False)
        run_path = Path(tool_i.run_path).stem + '.cwl'
        # NOTE: run_path is always relative; relative_run_path should probably
        # be called use_subdirs, because it simply determines if subworkflows
        # should be written to subdirectories or if everything should be
        # written to autogenerated/
        if relative_run_path:
            run_path = step_name_or_key + '/' + run_path
        else:
            if step_key in setup.subkeys:
                run_path = '___'.join(
                    namespaces + [step_name_or_key, run_path])
            else:
                run_path = os.path.relpath(run_path, 'autogenerated/')

        # NOTE: If 'run' is present but not a str, it contains inlined file
        # contents (currently unsupported), so leave it alone.
        if 'run' not in setup.steps[i] or isinstance(setup.steps[i]['run'], str):
            setup.steps[i]['run'] = run_path

        # Generate intermediate file names between steps.
        if 'in' not in setup.steps[i]:
            setup.steps[i]['in'] = {}

        if 'cwl_subinterpreter' == step_key:
            in_dict_in = setup.steps[i]['in']  # NOTE: Mutates in_dict_in
            # NOTE: input_output.write_absolute_yaml_tags() still takes a plain dict[str, str]
            # (input_output.py is out of scope for this typing pass); cast is a type-only, zero-runtime no-op.
            io.write_absolute_yaml_tags(cast(dict[str, str], yaml_tag_paths), in_dict_in, namespaces,
                                        step_name_or_key, setup.explicit_edge_calls_copy)

        args_provided = []
        if 'in' in setup.steps[i]:
            args_provided = list(setup.steps[i]['in'])

        in_tool = tool_i.cwl['inputs']
        if tool_i.cwl['class'] == 'CommandLineTool':
            args_required = [arg for arg in in_tool if not _arg_has_default_or_is_optional(arg, in_tool)]
        elif tool_i.cwl['class'] == 'Workflow':
            args_required = list(in_tool)

            if 'in' not in setup.steps[i]:
                setup.steps[i]['in'] = {key: key for key in args_required}
            else:
                # Add keys, but do not overwrite existing vals.
                for key in args_required:
                    if key not in setup.steps[i]['in']:
                        setup.steps[i]['in'][key] = key
        else:
            raise ValueError('Unknown class', tool_i.cwl['class'])

        # Note: Some biobb config tags are not required in the cwl files, but are in
        # fact required in the python source code! See check_mandatory_property
        # (Solution: refactor all required arguments out of config and list
        # them as explicit inputs in the cwl files, then modify the python
        # files accordingly.)

        sub_args_provided = [
            arg for arg in args_required if arg in setup.explicit_edge_calls_copy]

        label = step_key
        if graph_settings['graph_label_stepname']:
            label = step_name_or_key
        step_node_name = '___'.join(namespaces + [step_name_or_key])

        if not tool_i.cwl['class'] == 'Workflow':
            wic_graphviz_step_i = wic_step_i.get('wic', {}).get('graphviz', {})
            label = wic_graphviz_step_i.get('label', label)
            default_style = 'rounded, filled'
            style = wic_graphviz_step_i.get('style', '')
            style = default_style if style == '' else default_style + ', ' + style
            attrs = {'label': label, 'shape': 'box',
                     'style': style, 'fillcolor': 'lightblue'}
            setup.graph_gv.node(step_node_name, **attrs)
            setup.graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))
        elif not (step_key in setup.subkeys and len(namespaces) < graph_settings['graph_inline_depth']):
            nssnode = namespaces + [step_name_or_key]
            # Just like in add_graph_edge(), here we can hide all of the details
            # below a given depth by simply truncating the node's namespaces.
            nssnode = nssnode[:(1 + graph_settings['graph_inline_depth'])]
            step_node_name = '___'.join(nssnode)
            # NOTE: We intentionally do NOT get the label (if any) from the subworkflow
            # here (i.e. do NOT use wic_graphviz_step_i), because doing so causes
            # test_cwl_embedding_independence to fail.
            # TODO: For insertions, figure out why this is using the label from the parent workflow.
            default_style = 'rounded, filled'
            style = default_style
            attrs = {'label': label, 'shape': 'box',
                     'style': style, 'fillcolor': 'lightblue'}
            setup.graph_gv.node(step_node_name, **attrs)
            setup.graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))

        if 'out' not in setup.steps[i]:
            setup.steps[i]['out'] = []

        utils_cwl.validate_out_tag(setup.steps[i]['out'])
        for j in range(len(setup.steps[i]['out'])):
            out_val = setup.steps[i]['out'][j]
            if isinstance(out_val, dict):
                # NOTE: utils_cwl.validate_out_tag() above already guarantees
                # exactly one non-empty string key per dict entry.
                out_key = next(iter(out_val))
                out_val = out_val[out_key]
                if isinstance(out_val, dict) and 'wic_anchor' in out_val:
                    edgedef = out_val['wic_anchor']

                    # NOTE: There can only be one definition, but multiple call sites.
                    if not setup.explicit_edge_defs_copy.get(edgedef):
                        # discard anchor / retain string key
                        setup.steps[i]['out'][j] = out_key
                        setup.explicit_edge_defs_copy.update(
                            {edgedef: (namespaces + [step_name_or_key], out_key)})
                        # Add a 'dummy' value to explicit_edge_calls, because
                        # that determines sub_args_provided when the recursion returns.
                        setup.explicit_edge_calls_copy.update(
                            {edgedef: (namespaces + [step_name_or_key], out_key)})
                    else:
                        raise ValueError(
                            f"Error! Multiple definitions of &{edgedef}!")

        # NOTE: sub_args_provided are handled within the args_required loop below
        for arg_key in args_provided:
            # Extract input value into separate yml file
            # Replace it here with a new variable name
            arg_val = setup.steps[i]['in'][arg_key]

            if arg_key not in in_tool:
                raise ValueError(
                    f"Error! Provided input {arg_key} not found in interface of step {step_key}.")

            # Convert native YAML to a JSON-encoded string for specific tags.
            tags = ['config']
            if arg_key in tags and isinstance(arg_val, dict) and (Key.INLINE_INPUT in arg_val):
                arg_val = {Key.INLINE_INPUT: json.dumps(
                    arg_val[Key.INLINE_INPUT])}

            # Use triple underscore for namespacing so we can split later
            in_name = f'{step_name_or_key}___{arg_key}'

            # Add auxiliary inputs for scatter steps
            if str(arg_key).startswith('__') and str(arg_key).endswith('__'):
                in_dict = {'type': arg_val['type']}
                setup.inputs_workflow.update({in_name: in_dict})
                in_dict = {**in_dict, 'value': arg_val}
                setup.inputs_file_workflow.update({in_name: in_dict})
                setup.steps[i]['in'][arg_key] = {'source': in_name}
                continue

            in_dict = utils_cwl.copy_cwl_input_output_dict(
                in_tool[arg_key], True)

            # NOTE: match-mapping patterns require literal keys, so the
            # Key.ALIAS / Key.INLINE_INPUT constants cannot be used in the
            # `case` patterns themselves (only where compared/indexed below).
            match arg_val:
                case {'wic_alias': _}:
                    arg_val = arg_val[Key.ALIAS]
                    if not setup.explicit_edge_defs_copy.get(arg_val):
                        if is_root and not testing:
                            # Even if is_root, we don't want to raise an Exception
                            # here because in test_cwl_embedding_independence, we
                            # recompile all subworkflows as if they were root. That
                            # will cause this code path to be taken but it is not
                            # actually an error. Add a CWL input for testing only.
                            raise ValueError(
                                f"Error! No definition found for !&{arg_val}!")
                        setup.inputs_workflow.update({in_name: in_dict})
                        setup.steps[i]['in'][arg_key] = {'source': in_name}
                        # Add a 'dummy' value to explicit_edge_calls anyway, because
                        # that determines sub_args_provided when the recursion returns.
                        setup.explicit_edge_calls_copy.update(
                            {in_name: (namespaces + [step_name_or_key], arg_key)})
                    else:
                        (nss_def_init, var) = setup.explicit_edge_defs_copy[arg_val]

                        nss_def_embedded = var.split('___')[:-1]
                        nss_call_embedded = arg_key.split('___')[:-1]
                        nss_def = nss_def_init + nss_def_embedded
                        # [step_name_or_key] is correct; nss_def_init already contains it from the recursive call
                        nss_call = namespaces + \
                            [step_name_or_key] + nss_call_embedded

                        nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(
                            nss_def, nss_call)
                        nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(
                            nss_call, nss_def)
                        # nss_def and nss_call are paths into the abstract syntax tree 'call stack'.
                        # This defines the 'common namespace' in the call stack w.r.t. the inits.
                        assert nss_def_inits == nss_call_inits

                        # TODO: Check this comment.
                        # Relative to the common namespace, if the call site of an explicit
                        # edge is at a depth > 1, (i.e. if it is NOT simply of the form
                        # last_namespace/input_variable) then we
                        # need to create inputs in all of the intervening CWL files
                        # so we can pass in the values from the outer scope(s). Here,
                        # we simply need to use in_name and add to inputs_workflow
                        # and explicit_edge_calls. The outer scope(s) are handled by
                        # the sub_args_provided clause below.
                        # Note that the outputs from the definition site are bubbled
                        # up the call stack until they reach the common namespace.
                        if len(nss_call_tails) == 1:
                            # TODO: Check this comment.
                            # The definition site recursion (only, if any) has completed
                            # and we are already in the common namespace, thus
                            # we need to pass in the value from the definition site.
                            # Note that since len(nss_call_tails) == 1,
                            # there will not be any call site recursion in this case.
                            var_slash = nss_def_tails[0] + '/' + \
                                '___'.join(nss_def_tails[1:] + [var])
                            setup.steps[i]['in'][arg_key] = {'source': var_slash}
                        elif len(nss_call_tails) > 1:
                            setup.inputs_workflow.update({in_name: in_dict})
                            # Store explicit edge call site info up through the recursion.
                            d = {in_name: setup.explicit_edge_defs_copy[arg_val]}
                            setup.explicit_edge_calls_copy.update(d)
                            setup.steps[i]['in'][arg_key] = {'source': in_name}
                        else:
                            if nss_def == nss_call:
                                raise ValueError("Error! Cannot self-reference the same step!\n" +
                                                 f'nss_def {nss_def}\n nss_call {nss_call}')
                            # Since nss_call includes step_name_or_key, this should never happen...
                            raise AssertionError("Error! len(nss_call_tails) == 0! Please file a bug report!\n" +
                                                 f'nss_def {nss_def}\n nss_call {nss_call}')

                        arg_keys = [
                            in_name] if in_name in setup.input_mapping_copy else [arg_key]
                        arg_keys = utils.get_input_mappings(setup.input_mapping_copy, arg_keys,
                                                            (arg_key in setup.yaml_tree.get('inputs', {})))

                        out_key_init = '___'.join(nss_def_init + [var])
                        out_key = utils.get_output_mapping(
                            setup.output_mapping_copy, out_key_init)

                        nss_def_embedded = out_key.split('___')[:-1]

                        # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
                        if out_key.startswith('___'.join(nss_def_init)):
                            nss_def = nss_def_embedded

                        # Add an edge, but in a carefully chosen subgraph.
                        # If you add an edge whose head/tail is outside of the subgraph,
                        # graphviz may segfault! Moreover, even if graphviz doesn't
                        # segfault, adding an edge in a given subgraph can cause the
                        # nodes themselves to be rendered in that subgraph, even
                        # though the nodes are defined in a different subgraph!
                        # The correct thing to do is to use the graph associated with
                        # the lowest_common_ancestor of the definition and call site.
                        # (This is the only reason we need to pass in all subgraphs.)
                        label = var.split('___')[-1]
                        graph_init = subgraphs[len(nss_def_inits)]
                        # Let's use regular blue for explicit edges.
                        # Use constraint=false ?
                        for arg_key_ in arg_keys:
                            # TODO: Double check that we can use the same graph_init for all edges.
                            # Since input_mapping_copy really just factors edges through a single workflow input,
                            # this should hopefully be correct.

                            # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
                            nss_call_embedded = arg_key_.split('___')[:-1]
                            if arg_key_.startswith('___'.join(namespaces + [step_name_or_key])):
                                nss_call = nss_call_embedded
                            elif arg_key_.startswith(step_name_or_key):
                                nss_call = namespaces + nss_call_embedded
                            else:
                                nss_call = namespaces + \
                                    [step_name_or_key] + nss_call_embedded

                            utils_graphs.add_graph_edge(
                                graph_settings, graph_init, nss_def, nss_call, label, color='blue')
                case {'wic_inline_input': _}:
                    arg_val = arg_val[Key.INLINE_INPUT]

                    if arg_key in setup.steps[i].get('scatter', []):
                        # Promote scattered input types to arrays
                        in_dict['type'] = {'type': 'array',
                                           'items': in_dict['type']}

                    setup.inputs_workflow.update({in_name: in_dict})
                    in_dict = {**in_dict, 'value': arg_val}
                    setup.inputs_file_workflow.update({in_name: in_dict})
                    new_val = {'source': in_name}
                    setup.steps[i]['in'][arg_key] = new_val

                    if graph_settings['graph_show_inputs']:
                        input_node_name = '___'.join(
                            namespaces + [step_name_or_key, arg_key])
                        attrs = {'label': arg_key, 'shape': 'box',
                                 'style': 'rounded, filled', 'fillcolor': 'lightgreen'}
                        setup.graph_gv.node(input_node_name, **attrs)
                        font_edge_color = 'black' if graph_settings['graph_dark_theme'] else 'white'
                        setup.graph_gv.edge(input_node_name, step_node_name,
                                            color=font_edge_color)
                        setup.graph_nx.add_node(input_node_name)
                        setup.graph_nx.add_edge(input_node_name, step_node_name)
                        graphdata.nodes.append((input_node_name, attrs))
                        graphdata.edges.append(
                            (input_node_name, step_node_name, {}))
                case _:
                    arg_var: str = arg_val
                    # Leave un-evaluated, i.e. allow the user to inject raw CWL.
                    # The un-evaluated string should refer to either an inputs: variable
                    # or an internal CWL dependency, i.e. an output from a previous step.
                    # Since we do not want to be in the business of parsing raw CWL, if
                    # the former is not true, throw an error (with a CLI override).
                    # With the change from treating all values as inputs by default,
                    # to requiring the !ii inline input custom yaml tag, it is very
                    # likely that this code path is simply due to a missing !ii tag.

                    # TODO: check this first comment
                    # Subworkflows which use workflow inputs: variables cannot
                    # (yet) be inlined. Somehow, if they are not marked with
                    # inlineable: False, test_inline_subworkflows can still pass.
                    # This Exception will (correctly) cause such inlineing tests to fail.
                    hashable = False
                    inputs_key_dict = {}
                    try:
                        hash(arg_var)
                        hashable = True
                        inputs_key_dict = setup.yaml_tree['inputs'][arg_var]
                    except (TypeError, KeyError):
                        # TypeError: arg_var is unhashable.
                        # KeyError: 'inputs' or arg_var not present in yaml_tree['inputs'].
                        pass

                    arg_var_is_input = hashable and arg_var in setup.yaml_tree.get('inputs', {})
                    if not compiler_options['allow_raw_cwl'] and not arg_var_is_input:
                        print(
                            f"Warning! Did you forget to use !ii before {arg_var} in {setup.yaml_stem}.wic?")
                        print(
                            'If you want to compile the workflow anyway, use --allow_raw_cwl')
                        sys.exit(1)

                    if 'doc' in inputs_key_dict:
                        inputs_key_dict['doc'] += '\\n' + in_dict.get('doc', '')
                    else:
                        inputs_key_dict['doc'] = in_dict.get('doc', '')
                    if 'label' in inputs_key_dict:
                        inputs_key_dict['label'] += '\\n' + \
                            in_dict.get('label', '')
                    else:
                        inputs_key_dict['label'] = in_dict.get('label', '')

                    if not hashable:
                        pass  # Unhashable values cannot be used as input_mapping keys.
                    elif arg_var in setup.input_mapping_copy:
                        setup.input_mapping_copy[arg_var].append(in_name)
                    else:
                        setup.input_mapping_copy[arg_var] = [in_name]
                    # TODO: We can use un-evaluated variable names for input mapping; no notation for output mapping!
                    setup.steps[i]['in'][arg_key] = arg_var  # Leave un-evaluated

        for arg_key in args_required:
            in_name = f'{step_name_or_key}___{arg_key}'
            if arg_key in args_provided:
                continue  # We already covered this case above.
            if in_name in setup.inputs_file_workflow:
                # We provided an explicit argument (but not an edge) in a subworkflow,
                # and now we just need to pass it up to the root workflow.
                in_dict = utils_cwl.copy_cwl_input_output_dict(
                    in_tool[arg_key])
                setup.inputs_workflow.update({in_name: in_dict})
                arg_keyval = {arg_key: in_name}
                setup.steps[i] = utils_cwl.add_yamldict_keyval_in(
                    setup.steps[i], step_key, arg_keyval)

                # Obviously since we supplied a value, we do NOT want to perform edge inference.
                continue
            if arg_key in sub_args_provided:  # Edges have been explicitly provided
                # The definition site recursion (if any) and the call site
                # recursion (yes, see above), have both completed and we are
                # now in the common namespace, thus
                # we need to pass in the value from the definition site.
                # Extract the stored defs namespaces from explicit_edge_calls.
                # (See massive comment above.)
                (nss_def_init, var) = setup.explicit_edge_calls_copy[arg_key]

                nss_def_embedded = var.split('___')[:-1]
                nss_call_embedded = arg_key.split('___')[:-1]
                nss_def = nss_def_init + nss_def_embedded
                # [step_name_or_key] is correct; nss_def_init already contains it from the recursive call
                nss_call = namespaces + [step_name_or_key] + nss_call_embedded

                nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(
                    nss_def, nss_call)
                nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(
                    nss_call, nss_def)
                assert nss_def_inits == nss_call_inits

                nss_call_tails_stems = [
                    utils.parse_step_name_str(x)[0] for x in nss_call_tails]
                arg_val = setup.steps[i]['in'][arg_key]
                more_recursion = setup.yaml_stem in nss_call_tails_stems and nss_call_tails_stems.index(
                    setup.yaml_stem) > 0
                if (nss_call_tails_stems == []) or more_recursion:
                    # i.e. (if 'dummy' value) or (if it is possible to do more recursion)
                    in_dict = utils_cwl.copy_cwl_input_output_dict(
                        in_tool[arg_key])
                    setup.inputs_workflow.update({in_name: in_dict})
                    setup.steps[i]['in'][arg_key] = {'source': in_name}
                    # Store explicit edge call site info up through the recursion.
                    setup.explicit_edge_calls_copy.update(
                        {in_name: setup.explicit_edge_calls_copy[arg_key]})
                else:
                    # TODO: Check this comment.
                    # The definition site recursion (only, if any) has completed
                    # and we are already in the common namespace, thus
                    # we need to pass in the value from the definition site.
                    # Note that since len(nss_call_tails) == 1,
                    # there will not be any call site recursion in this case.
                    var_slash = nss_def_tails[0] + '/' + \
                        '___'.join(nss_def_tails[1:] + [var])
                    setup.steps[i]['in'][arg_key] = {'source': var_slash}

                # NOTE: We already added an edge to the appropriate subgraph above.
                # TODO: vars_workflow_output_internal?
            else:
                if compiler_options['inference_disable']:
                    continue
                insertions: list[StepId] = []
                in_name_in_inputs_file_workflow: bool = (
                    in_name in setup.inputs_file_workflow)
                arg_key_in_yaml_tree_inputs: bool = (
                    arg_key in setup.yaml_tree.get('inputs', {}))
                setup.steps[i] = inference.perform_edge_inference(
                    compiler_options['inference_use_naming_conventions'],
                    graph_settings, tools, setup.tools_lst, setup.steps_keys,
                    setup.yaml_stem, i, setup.steps, arg_key, setup.graph,
                    is_root, namespaces, vars_workflow_output_internal,
                    setup.input_mapping_copy, setup.output_mapping_copy, setup.inputs_workflow,
                    in_name, in_name_in_inputs_file_workflow,
                    arg_key_in_yaml_tree_inputs, insertions, setup.wic_steps, testing)
                # NOTE: For now, perform_edge_inference mutably appends to
                # inputs_workflow and vars_workflow_output_internal.

                # Automatically insert steps
                insertions = list(set(insertions))  # Remove duplicates
                if len(insertions) != 0 and compiler_options['insert_steps_automatically']:
                    insertion = insertions[0]
                    print('Automaticaly inserting step', insertion, i)
                    if len(insertions) != 1:
                        print('Warning! More than one step! Choosing', insertion)

                    yaml_tree_mod = insert_step_into_workflow(
                        setup.yaml_tree_orig, insertion, tools, i)

                    node_data = NodeData(namespaces, setup.yaml_stem, yaml_tree_mod, setup.yaml_tree, tool_i, {},
                                         setup.explicit_edge_defs_copy2, setup.explicit_edge_calls_copy2,
                                         setup.graph, setup.inputs_workflow, '')
                    rose_tree = RoseTree(node_data, setup.rose_tree_list)
                    env_data = EnvData(setup.input_mapping_copy, setup.output_mapping_copy,
                                       setup.inputs_file_workflow, vars_workflow_output_internal,
                                       setup.explicit_edge_defs_copy, setup.explicit_edge_calls_copy)
                    compiler_info = CompilerInfo(rose_tree, env_data)
                    return compiler_info

        # Add CommandLineTool/Subworkflow outputs tags to workflow out tags.
        # Note: Add all output tags for now, but depending on config options,
        # not all output files will be generated. This may cause an error.
        out_keyvals = {}
        for out_key, out_dict in tool_i.cwl['outputs'].items():
            out_keyvals[out_key] = utils_cwl.copy_cwl_input_output_dict(
                out_dict)
        if not out_keyvals:  # FYI out_keyvals should never be {}
            print(f'Error! no outputs for step {step_key}')
        setup.outputs_workflow.append(out_keyvals)

        setup.steps[i] = utils_cwl.add_yamldict_keyval_out(
            setup.steps[i], step_key, list(tool_i.cwl['outputs'].keys()))

        if compiler_options['partial_failure_enable']:
            when_null_clauses = []
            for arg_in in args_required:
                when_null_clauses.append(f'inputs["{arg_in}"] != null')
            when_clause = ' && '.join(when_null_clauses)
            if when_null_clauses:
                if 'when' in setup.steps[i]:
                    print('Warning! overwriting an existing "when" clause')
                setup.steps[i]['when'] = f"$({when_clause})"

    return _finalize_compilation(
        wic=setup.wic, namespaces=namespaces, yaml_stem=setup.yaml_stem, graph_settings=graph_settings,
        graph=setup.graph, sibling_subgraphs=setup.sibling_subgraphs, step_1_names=setup.step_1_names,
        steps_keys=setup.steps_keys, subkeys=setup.subkeys, yaml_tree=setup.yaml_tree,
        inputs_workflow=setup.inputs_workflow, output_mapping_copy=setup.output_mapping_copy,
        vars_workflow_output_internal=vars_workflow_output_internal, is_root=is_root,
        steps=setup.steps, outputs_workflow=setup.outputs_workflow, tools_lst=setup.tools_lst,
        step_node_name=step_node_name, tools=tools, wic_steps=setup.wic_steps,
        inputs_file_workflow=setup.inputs_file_workflow, testing=testing, yaml_path=setup.yaml_path,
        tool_i=tool_i, yaml_tree_orig=setup.yaml_tree_orig,
        explicit_edge_defs_copy2=setup.explicit_edge_defs_copy2,
        explicit_edge_calls_copy2=setup.explicit_edge_calls_copy2, rose_tree_list=setup.rose_tree_list,
        input_mapping_copy=setup.input_mapping_copy, explicit_edge_defs_copy=setup.explicit_edge_defs_copy,
        explicit_edge_calls_copy=setup.explicit_edge_calls_copy)


def generate_yaml_inputs(inputs_file_workflow: WorkflowInputsFile) -> WorkflowInputsFile:
    """Populates yaml_inputs from inputs_file_workflow for workflow execution.

    To be dumped in `*_yaml_inputs.yml` and passed to CWL workflow execution as --inputs-file

    Args:
        inputs_file_workflow (WorkflowInputsFile): The input parameters together with their
        types, and optionally formats and values, collected while compiling the workflow.

    Returns:
        WorkflowInputsFile: The concrete input values (with optional null fields omitted)
        ready to be dumped to a yaml inputs file.
    """
    def type_includes_null(cwl_type: Any) -> bool:
        return ((isinstance(cwl_type, list) and "null" in cwl_type)
                or (isinstance(cwl_type, str) and cwl_type.endswith("?")))

    def strip_null_from_union(cwl_type: Any) -> Any:
        if isinstance(cwl_type, list):
            non_null = [t for t in cwl_type if t != "null"]
            if len(non_null) == 1:
                return non_null[0]
            return non_null
        if isinstance(cwl_type, str) and cwl_type.endswith("?"):
            return cwl_type[:-1]
        return cwl_type

    def emit_file_or_dir(type_name: str, value: Any, fmt: Any = None) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "class": type_name,
            "location": value
        }
        if type_name == "File" and fmt:
            obj["format"] = fmt
        return obj

    def populate_scalar_val(cwl_type: Any, value: Any, fmt: Any = None) -> Any:
        match cwl_type:
            case "File":
                return emit_file_or_dir("File", value, fmt)

            case "Directory":
                return emit_file_or_dir("Directory", value)

            case "string":
                return str(value)

            case "int":
                return int(value)

            case "float":
                return float(value)

            case "boolean":
                return bool(value)

            case _:
                # Unknown or already structured type
                return value

    def populate_input_value(in_dict: dict[str, Any]) -> Any:
        raw_type = in_dict["type"]
        value = in_dict.get("value")
        fmt = in_dict.get("format")

        # ---------- Null handling ----------
        if value is None:
            if type_includes_null(raw_type):
                return None
            raise ValueError(
                f"Required input of type {raw_type} was not provided."
            )

        # Normalize type (strip null / ?)
        cwl_type = strip_null_from_union(raw_type)

        # ---------- Array handling ----------
        # Case: {"type": "array", "items": ...}
        if isinstance(cwl_type, dict) and cwl_type.get("type") == "array":
            item_type = strip_null_from_union(cwl_type["items"])
            # wrap scalar into list if necessary for lenient shape handling
            values = value if isinstance(value, list) else [value]
            return [
                populate_scalar_val(item_type, v, fmt)
                for v in values
            ]

        # Case: union including array
        if isinstance(cwl_type, list):
            for t in cwl_type:
                if isinstance(t, dict) and t.get("type") == "array":
                    item_type = strip_null_from_union(t["items"])
                    # wrap scalar into list if necessary for lenient shape handling
                    values = value if isinstance(value, list) else [value]
                    return [
                        populate_scalar_val(item_type, v, fmt)
                        for v in values
                    ]

        # ---------- Scalar case ----------
        return populate_scalar_val(cwl_type, value, fmt)

    yaml_inputs: WorkflowInputsFile = {}
    for key, in_dict in inputs_file_workflow.items():
        val = populate_input_value(in_dict)
        # Omit optional null fields only
        if val is None:
            continue
        yaml_inputs[key] = val
    return yaml_inputs


def insert_step_into_workflow(yaml_tree_orig: Yaml, stepid: StepId, tools: Tools, i: int) -> Yaml:
    """Inserts the step with given stepid into a workflow at the given index.

    Args:
        yaml_tree_orig (Yaml): The original Yaml tree
        stepid (StepId): The name of the workflow step to be inserted.
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl().\n
        yml files that have been compiled to CWL SubWorkflows are also added during compilation.
        i (int): The index to insert the new workflow step

    Returns:
        Yaml: A modified Yaml tree with the given stepid inserted at index i
    """
    yaml_tree_mod = yaml_tree_orig
    steps_mod: list[Yaml] = yaml_tree_mod['steps']
    steps_mod.insert(i, {stepid.stem: None})

    # Add inference rules annotations (i.e. for insertions)
    tool = tools[stepid]
    out_tool = tool.cwl['outputs']

    inference_rules_dict = {}
    for out_key, out_val in out_tool.items():
        if 'format' in out_val:
            inference_rules_dict[out_key] = inference_rules.get(
                out_val['format'], 'default')
    inf_dict = {'wic': {'inference': inference_rules_dict}}
    keystr = f'({i+1}, {stepid.stem})'  # The yml file uses 1-based indexing

    if 'wic' in yaml_tree_mod:
        if 'steps' in yaml_tree_mod['wic']:
            yaml_tree_mod['wic']['steps'] = utils.reindex_wic_steps(
                yaml_tree_mod['wic']['steps'], i+1)
            yaml_tree_mod['wic']['steps'][keystr] = inf_dict
        else:
            yaml_tree_mod['wic'].update({'steps': {keystr: inf_dict}})
    else:
        yaml_tree_mod.update({'wic': {'steps': {keystr: inf_dict}}})
    return yaml_tree_mod
