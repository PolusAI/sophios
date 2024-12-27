import argparse
import copy
import json
import os
from pathlib import Path
import sys
from typing import Dict, List, Any

import graphviz
from mergedeep import merge, Strategy
import networkx as nx
import yaml


from . import input_output as io
from . import inference, utils, utils_cwl, utils_graphs
from .wic_types import (CompilerInfo, EnvData, ExplicitEdgeCalls,
                        ExplicitEdgeDefs, GraphData, GraphReps, Namespaces,
                        NodeData, RoseTree, Tool, Tools, WorkflowInputsFile,
                        Yaml, YamlTree, StepId)

# NOTE: This must be initialized in main.py and/or cwl_subinterpreter.py
inference_rules: Dict[str, str] = {}


def compile_workflow(yaml_tree_ast: YamlTree,
                     args: argparse.Namespace,
                     namespaces: Namespaces,
                     subgraphs_: List[GraphReps],
                     explicit_edge_defs: ExplicitEdgeDefs,
                     explicit_edge_calls: ExplicitEdgeCalls,
                     input_mapping: Dict[str, List[str]],
                     output_mapping: Dict[str, str],
                     tools: Tools,
                     is_root: bool,
                     relative_run_path: bool,
                     testing: bool) -> CompilerInfo:
    """fixed-point wrapper around compile_workflow_once\n
    See https://en.wikipedia.org/wiki/Fixed_point_(mathematics)

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        args (Any): all of the other positional arguments for compile_workflow_once
        kwargs (Any): all of the other keyword arguments for compile_workflow_once

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows\n
        (in the Rose Tree) together with mutable cumulative environment\n
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
        compiler_info = compile_workflow_once(yaml_tree, args, namespaces, subgraphs,
                                              explicit_edge_defs, explicit_edge_calls,
                                              input_mapping, output_mapping,
                                              tools, is_root, relative_run_path, testing)
        node_data: NodeData = compiler_info.rose.data
        ast_modified = not yaml_tree.yml == node_data.yml
        if ast_modified:
            # import yaml
            # print(yaml.dump(node_data.yml))
            # print()
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
    # (NOTE: We now have a separate GraphData structure, so the graphviz and
    # networkx representations can probably be removed from the recursion.)
    # (Also note that you have to do this element-wise; you cannot simply write
    # subgraphs_ = subgraphs because that will only overwrite the local binding
    # and thus it will not affect the call site of compile_workflow!)
    for i, subgraph_ in enumerate(subgraphs_):
        subgraph_.graphviz.body = subgraphs[i].graphviz.body
        subgraph_.graphdata.name = subgraphs[i].graphdata.name
        subgraph_.graphdata.nodes = subgraphs[i].graphdata.nodes
        subgraph_.graphdata.edges = subgraphs[i].graphdata.edges
        subgraph_.graphdata.subgraphs = subgraphs[i].graphdata.subgraphs
        subgraph_.networkx.clear()
        subgraph_.networkx.update(subgraphs[i].networkx.edges, subgraphs[i].networkx.nodes)

    if i == max_iters:
        import yaml
        print(yaml.dump(node_data.yml))
        raise Exception(f'Error! Maximum number of iterations ({max_iters}) reached in compile_workflow!')
    return compiler_info


def compile_workflow_once(yaml_tree_ast: YamlTree,
                          args: argparse.Namespace,
                          namespaces: Namespaces,
                          subgraphs: List[GraphReps],
                          explicit_edge_defs: ExplicitEdgeDefs,
                          explicit_edge_calls: ExplicitEdgeCalls,
                          input_mapping: Dict[str, List[str]],
                          output_mapping: Dict[str, str],
                          tools: Tools,
                          is_root: bool,
                          relative_run_path: bool,
                          testing: bool) -> CompilerInfo:
    """STOP: Have you read the Developer's Guide?? docs/devguide.md\n
    Recursively compiles yml workflow definition ASTs to CWL file contents

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        args (argparse.Namespace): The command line arguments
        namespaces (Namespaces): Specifies the path in the yml AST to the current subworkflow
        subgraphs (List[Graph]): The graphs associated with the parent workflows of the current subworkflow
        explicit_edge_defs (ExplicitEdgeDefs): Stores the (path, value) of the explicit edge definition sites
        explicit_edge_calls (ExplicitEdgeCalls): Stores the (path, value) of the explicit edge call sites
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl().\n
        yml files that have been compiled to CWL SubWorkflows are also added during compilation.
        is_root (bool): True if this is the root workflow
        relative_run_path (bool): Controls whether to use subdirectories or\n
        just one directory when writing the compiled CWL files to disk
        testing: Used to disable some optional features which are unnecessary for testing.

    Raises:
        Exception: If any errors occur

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows\n
        (in the Rose Tree) together with mutable cumulative environment\n
        information which needs to be passed through the recursion.
    """
    # NOTE: Use deepcopy so that when we delete wic: we don't modify any call sites
    (step_id, yaml_tree) = copy.deepcopy(yaml_tree_ast)
    yaml_path = step_id.stem
    # We also want another copy of the original AST so that if we need to modify it,
    # we can return the modified AST to the call site and re-compile.
    (yaml_path_orig, yaml_tree_orig) = copy.deepcopy(yaml_tree_ast)

    if not testing:
        print(' starting', ('  ' * len(namespaces)) + yaml_path)

    # Check for top-level yml dsl args
    wic = {'wic': yaml_tree.get('wic', {})}
    # import yaml; print(yaml.dump(wic))
    if 'wic' in yaml_tree:
        del yaml_tree['wic']
    wic_steps = wic['wic'].get('steps', {})

    yaml_stem = Path(yaml_path).stem

    (back_name_, yaml_tree) = utils.extract_implementation(yaml_tree, wic['wic'], Path(yaml_path))
    steps: List[Yaml] = yaml_tree['steps']

    steps_keys = utils.get_steps_keys(steps)

    subkeys = utils.get_subkeys(steps_keys)

    # Add headers
    # Use 1.0 because cromwell only supports 1.0 and we are not using 1.1 / 1.2 features.
    # Eventually we will want to use 1.2 to support conditional workflows and 1.3 to support loops.
    yaml_tree['cwlVersion'] = yaml_tree.get('cwlVersion', 'v1.2')
    yaml_tree['class'] = 'Workflow'
    yaml_tree['$namespaces'] = {**yaml_tree.get('$namespaces', {}), **{'edam': 'https://edamontology.org/'}}
    edam_dev_owl = 'https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'
    schemas = yaml_tree.get('$schemas', [])
    if edam_dev_owl not in schemas:
        schemas.append(edam_dev_owl)
        yaml_tree['$schemas'] = schemas

    # Collect workflow input parameters
    inputs_workflow = {}
    inputs_file_workflow = {}

    # Collect workflow input/output to workflow step input/output mappings
    input_mapping_copy = copy.deepcopy(input_mapping)
    output_mapping_copy = copy.deepcopy(output_mapping)

    # Collect the internal workflow output variables
    outputs_workflow = []
    vars_workflow_output_internal = []

    # Copy recursive explicit edge variable definitions and call sites.
    explicit_edge_defs_copy = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy = copy.deepcopy(explicit_edge_calls)
    # Unlike the first copies which are mutably updated, these are returned
    # unmodified so that we can test that compilation is embedding independent.
    explicit_edge_defs_copy2 = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy2 = copy.deepcopy(explicit_edge_calls)

    # Collect recursive subworkflow data
    step_1_names = []
    sibling_subgraphs = []

    rose_tree_list = []

    graph = subgraphs[-1]  # Get the current graph
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    graphdata = graph.graphdata

    # plugin_ns = wic['wic'].get('namespace', 'global')

    tools_lst: List[Tool] = []

    for i, step_key in enumerate(steps_keys):
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        stem = Path(step_key).stem
        wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
        # NOTE: See comments in read_ast_from_disk()
        plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')  # plugin_ns ??
        stepid = StepId(stem, plugin_ns_i)

        step_name_or_key = step_name_i if step_key.endswith('.wic') or stepid in tools else step_key

        # Recursively compile subworkflows, adding compiled cwl file contents to tools
        ast_modified = False
        if step_key in subkeys:
            # NOTE: step_name_or_key should always be step_name_i in this branch
            # Extract the sub yaml file that we pre-loaded from disk.
            sub_yml = steps[i]['subtree']
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

            sub_compiler_info = compile_workflow(sub_yaml_tree, args, namespaces + [step_name_or_key],
                                                 subgraphs + [subgraph], explicit_edge_defs_copy,
                                                 explicit_edge_calls_copy,
                                                 input_mapping_copy, output_mapping_copy,
                                                 tools, False, relative_run_path, testing)

            sub_rose_tree = sub_compiler_info.rose
            rose_tree_list.append(sub_rose_tree)

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
                wic_steps[f'({i+1}, {step_key})'] = {'wic': sub_node_data.yml.get('wic', {})}
                wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
                # import yaml
                # print(yaml.dump(wic_steps))

            # Add arguments to the compiled subworkflow (if any), being careful
            # to remove any child wic: metadata annotations. Post-compilation
            # arguments can now be added either directly inline or as metadata.
            wic_step_i_copy = copy.deepcopy(wic_step_i)
            if 'wic' in wic_step_i_copy:
                del wic_step_i_copy['wic']
            # NOTE: To support overloading, the metadata args must overwrite the parent args!
            steps_i_id = steps[i]['id']
            args_provided_dict = merge(steps[i]['parentargs'], wic_step_i_copy,
                                       strategy=Strategy.TYPESAFE_REPLACE)  # TYPESAFE_ADDITIVE ?
            steps[i] = {**args_provided_dict, 'id': steps_i_id}

            sibling_subgraphs.append(sub_node_data.graph)  # TODO: Just subgraph?
            step_1_names.append(sub_node_data.step_name_1)
            tool_i = Tool(stem + '.cwl', sub_node_data.compiled_cwl)

            # Initialize the above from recursive values.
            # Do not initialize inputs_workflow. See comment below.
            # inputs_workflow.update(sub_node_data.inputs_workflow)
            inputs_namespaced_list = []
            for k, val in sub_env_data.inputs_file_workflow.items():
                input_namespaced = (f'{step_name_or_key}___{k}', val)  # _{step_key}_input___{k}
                inputs_namespaced_list.append(input_namespaced)
            inputs_namespaced = dict(inputs_namespaced_list)
            inputs_file_workflow.update(inputs_namespaced)

            input_mapping_copy_namespaced = dict([(f'{step_name_or_key}___{k}', val) for k, val in
                                                  sub_env_data.input_mapping.items() if k not in input_mapping])
            input_mapping_copy.update(input_mapping_copy_namespaced)

            output_mapping_copy_namespaced = dict([(f'{step_name_or_key}___{k}', val) for k, val in
                                                   sub_env_data.output_mapping.items() if k not in output_mapping])
            output_mapping_copy.update(output_mapping_copy_namespaced)

            vars_workflow_output_internal += sub_env_data.vars_workflow_output_internal
            explicit_edge_defs_copy.update(sub_env_data.explicit_edge_defs)
            explicit_edge_calls_copy.update(sub_env_data.explicit_edge_calls)
        else:
            run_tag = steps[i].get('run', '')
            run_tag_stem = Path(run_tag).stem if isinstance(run_tag, str) else ''
            stepid_runtag = StepId(run_tag_stem, 'global')

            if stepid in tools:
                # Use auto-discovery mechanism (with step_key)
                tool_i = tools[stepid]
            elif stepid_runtag in tools:
                # Use auto-discovery mechanism (with run tag)
                tool_i = tools[stepid_runtag]
            else:
                msg = f"Error! Neither {stepid.stem} nor {stepid_runtag.stem} found!, check your 'search_paths_cwl' in global_config.json"
                raise Exception(msg)
            # Programmatically modify tool_i here
            graph_dummy = graph  # Just use anything here to satisfy mypy
            name_stem = Path(tool_i.run_path).stem
            node_data_base_case = NodeData([step_name_or_key], name_stem, {}, tool_i.cwl,
                                           tool_i, {}, {}, {}, graph_dummy, {}, '')
            rose_tree_base_case = RoseTree(node_data_base_case, [])
            rose_tree_list.append(rose_tree_base_case)
        tools_lst.append(tool_i)

        if not testing:
            # Disable for testing because when testing in parallel, the *.gv Graphviz files
            # can be written/read to/from disk simultaneously, which results in
            # intermittent 'syntax errors'.
            pass
            # Actually, this is a significant performance bottleneck and isn't really necessary.
            # utils_graphs.make_tool_dag(stem, tool_i, args.graph_dark_theme)

        # Add run tag, using relative or flat-directory paths
        # NOTE: run: path issues were causing test_cwl_embedding_independence()
        # to fail, so I simply ignore the run tag in that test.
        # run_path = tool_i.run_path
        # Instead of referencing the original CWL CommandLineTools in-place,
        # let's copy them to autogenerated/ so we can programmatically modify them.
        # Note that this will cause name collisions if relative_run_path=False
        # and when using the 'same' CommandLineTool twice. i.e. two different
        # modifications will get written to the same cwl file, so the last one 'wins'.
        # (So don't use relative_run_path=False)
        # run_path = step_name_or_key + '/' + Path(tool_i.run_path).stem + '.cwl'
        run_path = Path(tool_i.run_path).stem + '.cwl'
        # NOTE: run_path is always relative; relative_run_path should probably
        # be called use_subdirs, because it simply determines if subworkflows
        # should be written to subdirectories or if everything should be
        # written to autogenerated/
        if relative_run_path:
            # if step_key in subkeys:
            #     run_path = step_name_or_key + '/' + run_path
            # else:
            #     run_path = os.path.relpath(run_path, 'autogenerated/')
            #     run_path = ('../' * len(namespaces)) + run_path
            run_path = step_name_or_key + '/' + run_path
        else:
            if step_key in subkeys:
                run_path = '___'.join(namespaces + [step_name_or_key, run_path])
            else:
                run_path = os.path.relpath(run_path, 'autogenerated/')

        if not 'run' in steps[i]:
            steps[i]['run'] = run_path
        elif isinstance(steps[i]['run'], str):
            steps[i]['run'] = run_path
        else:
            pass  # run: tag can also contain inlined file contents (currently unsupported)

        # Generate intermediate file names between steps.
        if 'in' not in steps[i]:
            steps[i]['in'] = {}

        if 'cwl_subinterpreter' == step_key:
            in_dict_in = steps[i]['in']  # NOTE: Mutates in_dict_in
            io.write_absolute_yaml_tags(args, in_dict_in, namespaces, step_name_or_key, explicit_edge_calls_copy)

        args_provided = []
        if 'in' in steps[i]:
            args_provided = list(steps[i]['in'])
        # print(args_provided)

        in_tool = tool_i.cwl['inputs']
        # print(list(in_tool.keys()))
        if tool_i.cwl['class'] == 'CommandLineTool':
            args_required = [arg for arg in in_tool if not (in_tool[arg].get('default') or
                                                            # Check for optional arguments using both the '?' syntactic sugar, as well as the
                                                            # canonical null representation. See canonicalize_type in cwl_utils.py
                                                            (isinstance(in_tool[arg]['type'], str) and in_tool[arg]['type'][-1] == '?') or
                                                            (isinstance(in_tool[arg]['type'], List) and 'null' in in_tool[arg]['type']))]
        elif tool_i.cwl['class'] == 'Workflow':
            args_required = list(in_tool)

            if 'in' not in steps[i]:
                steps[i]['in'] = {key: key for key in args_required}
            else:
                # Add keys, but do not overwrite existing vals.
                for key in args_required:
                    if key not in steps[i]['in']:
                        steps[i]['in'][key] = key
        else:
            raise Exception('Unknown class', tool_i.cwl['class'])

        # Note: Some biobb config tags are not required in the cwl files, but are in
        # fact required in the python source code! See check_mandatory_property
        # (Solution: refactor all required arguments out of config and list
        # them as explicit inputs in the cwl files, then modify the python
        # files accordingly.)
        # print(args_required)

        sub_args_provided = [arg for arg in args_required if arg in explicit_edge_calls_copy]
        # print(sub_args_provided)

        label = step_key
        if args.graph_label_stepname:
            label = step_name_or_key
        step_node_name = '___'.join(namespaces + [step_name_or_key])

        if not tool_i.cwl['class'] == 'Workflow':
            wic_graphviz_step_i = wic_step_i.get('wic', {}).get('graphviz', {})
            label = wic_graphviz_step_i.get('label', label)
            default_style = 'rounded, filled'
            style = wic_graphviz_step_i.get('style', '')
            style = default_style if style == '' else default_style + ', ' + style
            attrs = {'label': label, 'shape': 'box', 'style': style, 'fillcolor': 'lightblue'}
            graph_gv.node(step_node_name, **attrs)
            graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))
        elif not (step_key in subkeys and len(namespaces) < args.graph_inline_depth):
            nssnode = namespaces + [step_name_or_key]
            # Just like in add_graph_edge(), here we can hide all of the details
            # below a given depth by simply truncating the node's namespaces.
            nssnode = nssnode[:(1 + args.graph_inline_depth)]
            step_node_name = '___'.join(nssnode)
            # NOTE: NOT wic_graphviz_step_i
            # get the label (if any) from the subworkflow
            # TODO: This causes test_cwl_embedding_independence to fail.
            # yml = sub_node_data.yml if ast_modified else sub_yaml_tree.yml
            # step_i_wic_graphviz = yml.get('wic', {}).get('graphviz', {})
            # TODO: For insertions, figure out why this is using
            # the label from the parent workflow.
            # label = step_i_wic_graphviz.get('label', label)
            default_style = 'rounded, filled'
            style = ''  # step_i_wic_graphviz.get('style', '')
            style = default_style if style == '' else default_style + ', ' + style
            attrs = {'label': label, 'shape': 'box', 'style': style, 'fillcolor': 'lightblue'}
            graph_gv.node(step_node_name, **attrs)
            graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))

        if 'out' not in steps[i]:
            steps[i]['out'] = []

        if isinstance(steps[i]['out'], Dict):
            print('Error! The `out` tag should be a list, not a dictionary!')
        for j in range(len(steps[i]['out'])):
            out_val = steps[i]['out'][j]
            if isinstance(out_val, Dict):
                keys = list(out_val.keys())
                if len(keys) != 1:
                    print('Error! There should only be one anchor per out: list entry!')
                    # raise exception?
                out_key = keys[0]
                out_val = out_val[out_key]
                if isinstance(out_val, Dict) and 'wic_anchor' in out_val:
                    edgedef = out_val['wic_anchor']

                    # NOTE: There can only be one definition, but multiple call sites.
                    if not explicit_edge_defs_copy.get(edgedef):
                        steps[i]['out'][j] = out_key  # discard anchor / retain string key
                        explicit_edge_defs_copy.update({edgedef: (namespaces + [step_name_or_key], out_key)})
                        # Add a 'dummy' value to explicit_edge_calls, because
                        # that determines sub_args_provided when the recursion returns.
                        explicit_edge_calls_copy.update({edgedef: (namespaces + [step_name_or_key], out_key)})
                    else:
                        raise Exception(f"Error! Multiple definitions of &{edgedef}!")

        # NOTE: sub_args_provided are handled within the args_required loop below
        for arg_key in args_provided:
            # Extract input value into separate yml file
            # Replace it here with a new variable name
            arg_val = steps[i]['in'][arg_key]

            # Convert native YAML to a JSON-encoded string for specific tags.
            tags = ['config']
            if arg_key in tags and isinstance(arg_val, Dict) and ('wic_inline_input' in arg_val):
                arg_val = {'wic_inline_input': json.dumps(arg_val['wic_inline_input'])}

            # Use triple underscore for namespacing so we can split later
            in_name = f'{step_name_or_key}___{arg_key}'

            # Add auxiliary inputs for scatter steps
            if str(arg_key).startswith('__') and str(arg_key).endswith('__'):
                in_dict = {'type': arg_val['type']}
                inputs_workflow.update({in_name: in_dict})
                in_dict = {**in_dict, 'value': arg_val}
                inputs_file_workflow.update({in_name: in_dict})
                steps[i]['in'][arg_key] = {'source': in_name}
                continue

            in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key], True)

            if isinstance(arg_val, Dict) and 'wic_alias' in arg_val:
                arg_val = arg_val['wic_alias']
                if not explicit_edge_defs_copy.get(arg_val):
                    if is_root and not testing:
                        # Even if is_root, we don't want to raise an Exception
                        # here because in test_cwl_embedding_independence, we
                        # recompile all subworkflows as if they were root. That
                        # will cause this code path to be taken but it is not
                        # actually an error. Add a CWL input for testing only.
                        raise Exception(f"Error! No definition found for !&{arg_val}!")
                    inputs_workflow.update({in_name: in_dict})
                    steps[i]['in'][arg_key] = {'source': in_name}
                    # Add a 'dummy' value to explicit_edge_calls anyway, because
                    # that determines sub_args_provided when the recursion returns.
                    explicit_edge_calls_copy.update({in_name: (namespaces + [step_name_or_key], arg_key)})
                else:
                    (nss_def_init, var) = explicit_edge_defs_copy[arg_val]

                    nss_def_embedded = var.split('___')[:-1]
                    nss_call_embedded = arg_key.split('___')[:-1]
                    nss_def = nss_def_init + nss_def_embedded
                    # [step_name_or_key] is correct; nss_def_init already contains it from the recursive call
                    nss_call = namespaces + [step_name_or_key] + nss_call_embedded

                    nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(nss_def, nss_call)
                    nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(nss_call, nss_def)
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
                        var_slash = nss_def_tails[0] + '/' + '___'.join(nss_def_tails[1:] + [var])
                        steps[i]['in'][arg_key] = {'source': var_slash}
                    elif len(nss_call_tails) > 1:
                        inputs_workflow.update({in_name: in_dict})
                        # Store explicit edge call site info up through the recursion.
                        d = {in_name: explicit_edge_defs_copy[arg_val]}
                        # d = {in_name, (namespaces + [step_name_or_key], var)} # ???
                        explicit_edge_calls_copy.update(d)
                        steps[i]['in'][arg_key] = {'source': in_name}
                    else:
                        if nss_def == nss_call:
                            raise Exception("Error! Cannot self-reference the same step!\n" +
                                            f'nss_def {nss_def}\n nss_call {nss_call}')
                        else:
                            # Since nss_call includes step_name_or_key, this should never happen...
                            raise Exception("Error! len(nss_call_tails) == 0! Please file a bug report!\n" +
                                            f'nss_def {nss_def}\n nss_call {nss_call}')

                    arg_keys = [in_name] if in_name in input_mapping_copy else [arg_key]
                    arg_keys = utils.get_input_mappings(input_mapping_copy, arg_keys,
                                                        (arg_key in yaml_tree.get('inputs', {})))

                    out_key_init = '___'.join(nss_def_init + [var])
                    out_key = utils.get_output_mapping(output_mapping_copy, out_key_init)

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
                            nss_call = namespaces + [step_name_or_key] + nss_call_embedded

                        utils_graphs.add_graph_edge(args, graph_init, nss_def, nss_call, label, color='blue')
            elif isinstance(arg_val, Dict) and 'wic_inline_input' in arg_val:
                arg_val = arg_val['wic_inline_input']

                if arg_key in steps[i].get('scatter', []):
                    # Promote scattered input types to arrays
                    in_dict['type'] = {'type': 'array', 'items': in_dict['type']}

                inputs_workflow.update({in_name: in_dict})
                in_dict = {**in_dict, 'value': arg_val}
                inputs_file_workflow.update({in_name: in_dict})
                new_val = {'source': in_name}
                steps[i]['in'][arg_key] = new_val

                if args.graph_show_inputs:
                    input_node_name = '___'.join(namespaces + [step_name_or_key, arg_key])
                    attrs = {'label': arg_key, 'shape': 'box', 'style': 'rounded, filled', 'fillcolor': 'lightgreen'}
                    graph_gv.node(input_node_name, **attrs)
                    font_edge_color = 'black' if args.graph_dark_theme else 'white'
                    graph_gv.edge(input_node_name, step_node_name, color=font_edge_color)
                    graph_nx.add_node(input_node_name)
                    graph_nx.add_edge(input_node_name, step_node_name)
                    graphdata.nodes.append((input_node_name, attrs))
                    graphdata.edges.append((input_node_name, step_node_name, {}))
            else:
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
                    inputs_key_dict = yaml_tree['inputs'][arg_var]
                except Exception:
                    pass

                if not args.allow_raw_cwl and (not hashable or arg_var not in yaml_tree.get('inputs', {})):
                    if not args.allow_raw_cwl:
                        print(f"Warning! Did you forget to use !ii before {arg_var} in {yaml_stem}.wic?")
                        print('If you want to compile the workflow anyway, use --allow_raw_cwl')
                        sys.exit(1)

                    inputs = yaml_tree.get('inputs', {})
                    unbound_lit_var = 'Error! Unbound literal variable'
                    if inputs == {}:
                        raise Exception(f"{unbound_lit_var}{arg_var} not in inputs: tag in {yaml_stem}.wic")
                    inputs_dump = yaml.dump({'inputs': inputs})
                    raise Exception(f"{unbound_lit_var}{arg_var} not in\n{inputs_dump}\nin {yaml_stem}.wic")

                if 'doc' in inputs_key_dict:
                    inputs_key_dict['doc'] += '\\n' + in_dict.get('doc', '')
                else:
                    inputs_key_dict['doc'] = in_dict.get('doc', '')
                if 'label' in inputs_key_dict:
                    inputs_key_dict['label'] += '\\n' + in_dict.get('label', '')
                else:
                    inputs_key_dict['label'] = in_dict.get('label', '')

                if not hashable:
                    pass  # ???
                elif arg_var in input_mapping_copy:
                    input_mapping_copy[arg_var].append(in_name)
                else:
                    input_mapping_copy[arg_var] = [in_name]
                # TODO: We can use un-evaluated variable names for input mapping; no notation for output mapping!
                steps[i]['in'][arg_key] = arg_var  # Leave un-evaluated

        for arg_key in args_required:
            # print('arg_key', arg_key)
            in_name = f'{step_name_or_key}___{arg_key}'
            if arg_key in args_provided:
                continue  # We already covered this case above.
            if in_name in inputs_file_workflow:
                # We provided an explicit argument (but not an edge) in a subworkflow,
                # and now we just need to pass it up to the root workflow.
                # print('passing', in_name)
                in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key])
                inputs_workflow.update({in_name: in_dict})
                arg_keyval = {arg_key: in_name}
                steps[i] = utils_cwl.add_yamldict_keyval_in(steps[i], step_key, arg_keyval)

                # Obviously since we supplied a value, we do NOT want to perform edge inference.
                continue
            if arg_key in sub_args_provided:  # Edges have been explicitly provided
                # The definition site recursion (if any) and the call site
                # recursion (yes, see above), have both completed and we are
                # now in the common namespace, thus
                # we need to pass in the value from the definition site.
                # Extract the stored defs namespaces from explicit_edge_calls.
                # (See massive comment above.)
                (nss_def_init, var) = explicit_edge_calls_copy[arg_key]

                nss_def_embedded = var.split('___')[:-1]
                nss_call_embedded = arg_key.split('___')[:-1]
                nss_def = nss_def_init + nss_def_embedded
                # [step_name_or_key] is correct; nss_def_init already contains it from the recursive call
                nss_call = namespaces + [step_name_or_key] + nss_call_embedded

                nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(nss_def, nss_call)
                nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(nss_call, nss_def)
                assert nss_def_inits == nss_call_inits

                nss_call_tails_stems = [utils.parse_step_name_str(x)[0] for x in nss_call_tails]
                arg_val = steps[i]['in'][arg_key]
                more_recursion = yaml_stem in nss_call_tails_stems and nss_call_tails_stems.index(yaml_stem) > 0
                if (nss_call_tails_stems == []) or more_recursion:
                    # i.e. (if 'dummy' value) or (if it is possible to do more recursion)
                    in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key])
                    inputs_workflow.update({in_name: in_dict})
                    steps[i]['in'][arg_key] = {'source': in_name}
                    # Store explicit edge call site info up through the recursion.
                    explicit_edge_calls_copy.update({in_name: explicit_edge_calls_copy[arg_key]})
                else:
                    # TODO: Check this comment.
                    # The definition site recursion (only, if any) has completed
                    # and we are already in the common namespace, thus
                    # we need to pass in the value from the definition site.
                    # Note that since len(nss_call_tails) == 1,
                    # there will not be any call site recursion in this case.
                    var_slash = nss_def_tails[0] + '/' + '___'.join(nss_def_tails[1:] + [var])
                    steps[i]['in'][arg_key] = {'source': var_slash}

                # NOTE: We already added an edge to the appropriate subgraph above.
                # TODO: vars_workflow_output_internal?
            else:
                if args.inference_disable:
                    continue
                insertions: List[StepId] = []
                in_name_in_inputs_file_workflow: bool = (in_name in inputs_file_workflow)
                arg_key_in_yaml_tree_inputs: bool = (arg_key in yaml_tree.get('inputs', {}))
                steps[i] = inference.perform_edge_inference(args, tools, tools_lst, steps_keys,
                                                            yaml_stem, i, steps, arg_key, graph, is_root, namespaces,
                                                            vars_workflow_output_internal, input_mapping_copy, output_mapping_copy, inputs_workflow, in_name,
                                                            in_name_in_inputs_file_workflow, arg_key_in_yaml_tree_inputs, insertions, wic_steps, testing)
                # NOTE: For now, perform_edge_inference mutably appends to
                # inputs_workflow and vars_workflow_output_internal.

                # Automatically insert steps
                insertions = list(set(insertions))  # Remove duplicates
                if len(insertions) != 0 and args.insert_steps_automatically:
                    insertion = insertions[0]
                    print('Automaticaly inserting step', insertion, i)
                    if len(insertions) != 1:
                        print('Warning! More than one step! Choosing', insertion)

                    yaml_tree_mod = insert_step_into_workflow(yaml_tree_orig, insertion, tools, i)

                    node_data = NodeData(namespaces, yaml_stem, yaml_tree_mod, yaml_tree, tool_i, {},
                                         explicit_edge_defs_copy2, explicit_edge_calls_copy2,
                                         graph, inputs_workflow, '')
                    rose_tree = RoseTree(node_data, rose_tree_list)
                    env_data = EnvData(input_mapping_copy, output_mapping_copy,
                                       inputs_file_workflow, vars_workflow_output_internal,
                                       explicit_edge_defs_copy, explicit_edge_calls_copy)
                    compiler_info = CompilerInfo(rose_tree, env_data)
                    # node_data_dummy = NodeData(None, None, yaml_tree_mod, None, None, None, None, None, None, None)
                    # compiler_info_dummy = CompilerInfo(RoseTree(node_data_dummy, None), None)
                    return compiler_info

        # Add CommandLineTool/Subworkflow outputs tags to workflow out tags.
        # Note: Add all output tags for now, but depending on config options,
        # not all output files will be generated. This may cause an error.
        out_keyvals = {}
        for out_key, out_dict in tool_i.cwl['outputs'].items():
            out_keyvals[out_key] = utils_cwl.copy_cwl_input_output_dict(out_dict)
            # print(out_key, out_keyvals[out_key])
        if not out_keyvals:  # FYI out_keyvals should never be {}
            print(f'Error! no outputs for step {step_key}')
        outputs_workflow.append(out_keyvals)

        steps[i] = utils_cwl.add_yamldict_keyval_out(steps[i], step_key, list(tool_i.cwl['outputs'].keys()))

        if args.partial_failure_enable:
            when_null_clauses = []
            for arg_in in args_required:
                when_null_clauses.append(f'inputs["{arg_in}"] != null')
            when_clause = ' && '.join(when_null_clauses)
            if when_null_clauses:
                if 'when' in steps[i]:
                    print('Warning! overwriting an existing "when" clause')
                steps[i]['when'] = f"$({when_clause})"
        # print()

    # NOTE: add_subgraphs currently mutates graph
    wic_graphviz = wic['wic'].get('graphviz', {})
    ranksame_strs = wic_graphviz.get('ranksame', [])
    ranksame_pairs = [utils.parse_int_string_tuple(x) for x in ranksame_strs]
    steps_ranksame = []
    for num, name in ranksame_pairs:
        step_name_num = utils.step_name_str(yaml_stem, num-1, name)
        step_name_nss = '___'.join(namespaces + [step_name_num])
        steps_ranksame.append(f'"{step_name_nss}"')  # Escape with double quotes.
    utils_graphs.add_subgraphs(args, graph, sibling_subgraphs, namespaces, step_1_names, steps_ranksame)
    step_name_1 = utils.get_step_name_1(step_1_names, yaml_stem, namespaces, steps_keys, subkeys)

    # Add the provided workflow inputs to the workflow inputs from each step
    inputs_combined = {**yaml_tree.get('inputs', {}), **inputs_workflow}
    yaml_tree.update({'inputs': inputs_combined})

    # NOTE: This is a nasty hack because we don't have any syntax for mapping workflow outputs.
    for k, v in yaml_tree.get('outputs', {}).items():
        # Assume the user has manually added the correct namespaced CWL dependency.
        output_mapping_copy[k] = v['outputSource'].replace('/', '___')

    vars_workflow_output_internal = list(set(vars_workflow_output_internal))  # Get uniques
    # (Why are we getting uniques?)
    workflow_outputs = utils_cwl.get_workflow_outputs(args, namespaces, is_root, yaml_stem,
                                                      steps, outputs_workflow, vars_workflow_output_internal,
                                                      graph, tools_lst, step_node_name, tools)
    # Add the provided workflow outputs to the workflow outputs from each step
    outputs_combined = {**yaml_tree.get('outputs', {}), **workflow_outputs}
    yaml_tree.update({'outputs': outputs_combined})

    # NOTE: currently mutates yaml_tree (maybe)
    utils_cwl.maybe_add_requirements(yaml_tree, steps_keys, wic_steps, subkeys)

    # Finally, rename the steps to be unique
    # and convert the list of steps into a dict / list
    steps_dict = {}
    steps_list = []
    for i, step_key in enumerate(steps_keys):
        wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
        plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')
        stepid = StepId(Path(step_key).stem, plugin_ns_i)

        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        step_name_or_key = step_name_i if step_key.endswith('.wic') or stepid in tools else step_key
        # steps[i] = {steps[i], 'id': step_name_or_key}
        step_i_copy = {**steps[i]}
        del step_i_copy['id']
        steps_dict.update({step_name_or_key: step_i_copy})
        step_i_copy = {'id': step_name_or_key, **step_i_copy}
        steps_list.append(step_i_copy)
    yaml_tree.update({'steps': steps_list})  # steps_list ?

    def populate_scalar_val(in_dict: dict) -> Any:
        newval: Any = ()
        if 'File' == in_dict['type']:
            # path = Path(in_dict['value']).name # NOTE: Use .name ?
            newval = {'class': 'File', 'path': in_dict['value']}
            if 'format' in in_dict:
                in_format = in_dict['format']
                if isinstance(in_format, List):
                    in_format = list(set(in_format))  # get uniques
                    if len(in_format) > 1:
                        print(f'NOTE: More than one input file format for {key}')
                        print(f'formats: {in_format}')
                        print(f'Choosing {in_format[0]}')
                    in_format = in_format[0]
                newval['format'] = in_format
        elif 'Directory' == in_dict['type']:
            newval = {'class': 'Directory', 'location': in_dict['value']}
        elif 'string' == in_dict['type'] or 'string?' == in_dict['type']:
            # We cannot store string values as a dict, so use type: ignore
            newval = str(in_dict['value'])
        # TODO: Check for all valid types?
        else:
            newval = in_dict['value']
        return newval

    # Dump the workflow inputs to a separate yml file.
    yaml_inputs: WorkflowInputsFile = {}
    for key, in_dict in inputs_file_workflow.items():
        new_keyval: WorkflowInputsFile = {}
        if isinstance(in_dict['type'], dict) and 'array' == in_dict['type']['type']:
            val_list = []
            for val in in_dict['value']:
                val_list.append(populate_scalar_val(
                    {'type': in_dict['type']['items'], 'value': val, 'format': in_dict.get('format')}))
            new_keyval = {key: val_list}
        elif isinstance(in_dict['type'], list) and isinstance(in_dict['type'][1], dict) and 'array' == in_dict['type'][1]['type']:
            val_list = []
            for val in in_dict['value']:
                val_list.append(populate_scalar_val(
                    {'type': in_dict['type'][1]['items'][1], 'value': val, 'format': in_dict.get('format')}))
            new_keyval = {key: val_list}
        else:
            new_keyval = {key: populate_scalar_val(in_dict)}
        # else:
        #    raise Exception(f"Error! Unknown type: {in_dict['type']}")
        yaml_inputs.update(new_keyval)

    if not testing:
        print('finishing', ('  ' * len(namespaces)) + yaml_path)
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
    steps_mod: List[Yaml] = yaml_tree_mod['steps']
    steps_mod.insert(i, {stepid.stem: None})

    # Add inference rules annotations (i.e. for insertions)
    tool = tools[stepid]
    out_tool = tool.cwl['outputs']

    inference_rules_dict = {}
    for out_key, out_val in out_tool.items():
        if 'format' in out_val:
            inference_rules_dict[out_key] = inference_rules.get(out_val['format'], 'default')
    inf_dict = {'wic': {'inference': inference_rules_dict}}
    keystr = f'({i+1}, {stepid.stem})'  # The yml file uses 1-based indexing

    if 'wic' in yaml_tree_mod:
        if 'steps' in yaml_tree_mod['wic']:
            yaml_tree_mod['wic']['steps'] = utils.reindex_wic_steps(yaml_tree_mod['wic']['steps'], i+1)
            yaml_tree_mod['wic']['steps'][keystr] = inf_dict
        else:
            yaml_tree_mod['wic'].update({'steps': {keystr: inf_dict}})
    else:
        yaml_tree_mod.update({'wic': {'steps': {keystr: inf_dict}}})
    return yaml_tree_mod
