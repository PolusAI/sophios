import argparse
import copy
from pathlib import Path
from typing import Any, Dict, List
import yaml

from . import utils
from .wic_types import (GraphReps, InternalOutputs, Namespaces, Tool, Tools,
                        WorkflowOutputs, Yaml, StepId)


def maybe_add_requirements(yaml_tree: Yaml, steps_keys: List[str],
                           wic_steps: Yaml, subkeys: List[str]) -> None:
    """Adds any necessary CWL requirements

    Args:
        yaml_tree (Yaml): A tuple of name and yml AST
        steps_keys (List[str]): The name of each step in the current CWL workflow
        wic_steps (Yaml): The metadata associated with the workflow steps
        subkeys (List[str]): The keys associated with subworkflows
    """
    subwork = []
    scatter = []
    stepinp = []
    jsreq = []
    for i, step_key in enumerate(steps_keys):
        sub_wic = wic_steps.get(f'({i+1}, {step_key})', {})

        if 'scatter' in yaml_tree['steps'][i]:
            scatter = ['ScatterFeatureRequirement']
        if 'when' in yaml_tree['steps'][i]:
            jsreq = ['InlineJavascriptRequirement']

        in_step = yaml_tree['steps'][i].get('in')
        sub_wic_copy = copy.deepcopy(sub_wic)
        if 'wic' in sub_wic_copy:
            del sub_wic_copy['wic']
        if (utils.recursively_contains_dict_key('valueFrom', in_step) or
                utils.recursively_contains_dict_key('valueFrom', sub_wic_copy)):
            stepinp = ['StepInputExpressionRequirement', 'InlineJavascriptRequirement']

    if not subkeys == []:
        subwork = ['SubworkflowFeatureRequirement']

    reqs = subwork + scatter + stepinp + jsreq
    if reqs:
        reqsdict: Dict[str, Dict] = {r: {} for r in set(reqs)}
        if 'requirements' in yaml_tree:
            new_reqs = dict(list(yaml_tree['requirements'].items()) + list(reqsdict.items()))
            yaml_tree['requirements'].update(new_reqs)
        else:
            yaml_tree['requirements'] = reqsdict


def add_yamldict_keyval_in(steps_i: Yaml, step_key: str, keyval: Yaml) -> Yaml:
    """Convenience function used to (mutably) merge two Yaml dicts.

    Args:
        steps_i (Yaml): A partially-completed Yaml dict representing a step in a CWL workflow
        step_key (str): The name of the step in a CWL workflow
        keyval (Yaml): A Yaml dict with additional details to be merged into the first Yaml dict

    Returns:
        Yaml: The first Yaml dict with the second Yaml dict merged into it.
    """
    # TODO: Check whether we can just use deepmerge.merge()
    if steps_i:
        if 'in' in steps_i:
            new_keys = dict(list(steps_i['in'].items()) + list(keyval.items()))
            new_keyvals = dict([(k, v) if k != 'in' else (k, new_keys) for k, v in steps_i.items()])
        else:
            new_keys = keyval
            new_keyvals = dict(list(steps_i.items()) + [('in', new_keys)])
        steps_i.update(new_keyvals)
    else:
        steps_i = {step_key: {'in': keyval}}
    return steps_i


def add_yamldict_keyval_out(steps_i: Yaml, step_key: str, strs: List[str]) -> Yaml:
    """Convenience function used to (mutably) merge two Yaml dicts.

    Args:
        steps_i (Yaml): A partially-completed Yaml dict representing a step in a CWL workflow
        step_key (str): The name of the step in a CWL workflow
        keyval (Yaml): A Yaml dict with additional details to be merged into the first Yaml dict

    Returns:
        Yaml: The first Yaml dict with the second Yaml dict merged into it.
    """
    # TODO: Check whether we can just use deepmerge.merge()
    if steps_i:
        if 'out' in steps_i:
            new_strs = steps_i['out'] + strs
            new_strs = list(set(new_strs))
            new_keyvals = dict([(k, v) if k != 'out' else (k, new_strs) for k, v in steps_i.items()])
        else:
            new_keyvals = dict(list(steps_i.items()) + [('out', strs)])
        steps_i.update(new_keyvals)
    else:
        steps_i = {step_key: {'out': strs}}
    return steps_i


def get_workflow_outputs(args: argparse.Namespace,
                         namespaces: Namespaces,
                         is_root: bool,
                         yaml_stem: str,
                         steps: List[Yaml],
                         outputs_workflow: WorkflowOutputs,
                         vars_workflow_output_internal: InternalOutputs,
                         graph: GraphReps,
                         tools_lst: List[Tool],
                         step_node_name: str,
                         tools: Tools) -> Dict[str, Dict[str, str]]:
    """Chooses a subset of the CWL outputs: to actually output

    Args:
        args (argparse.Namespace): The command line arguments
        namespaces (Namespaces): Specifies the path in the AST of the current subworkflow
        is_root (bool): True if this is the root workflow
        yaml_stem (str): The name of the current subworkflow (stem of the yaml filepath)
        steps (List[Yaml]): The steps: tag of a CWL workflow
        outputs_workflow (WorkflowOutputs): Contains the contents of the out: tags for each step.
        vars_workflow_output_internal (InternalOutputs): Keeps track of output\n
        variables which are internal to the root workflow, but not necessarily to subworkflows.
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        tools_lst (List[Tool]): A list of the CWL CommandLineTools or compiled subworkflows for the current workflow.
        step_node_name (str): The namespaced name of the current step
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()

    Returns:
        Dict[str, Dict[str, str]]: The actual outputs to be specified in the generated CWL file
    """
    # Add the outputs of each step to the workflow outputs
    workflow_outputs = {}
    steps_keys = utils.get_steps_keys(steps)
    for i, step_key in enumerate(steps_keys):
        tool_i = tools_lst[i].cwl
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        step_id = StepId(Path(step_key).stem, 'global')
        step_name_or_key = step_name_i if step_key.endswith('.wic') \
            or step_id in tools \
            or Path(step_key).stem == Path(tools_lst[i].run_path).stem else step_key
        # step_name_or_key = step_name_i if stepid in tools else step_key
        out_keys = steps[i]['out']
        for out_key in out_keys:
            out_var = f'{step_name_or_key}/{out_key}'
            # Avoid duplicating intermediate outputs in GraphViz
            out_key_no_namespace = out_key.split('___')[-1]
            if args.graph_show_outputs:
                vars_nss = [var.replace('/', '___') for var in vars_workflow_output_internal]
                case1 = (tool_i['class'] == 'Workflow') and (not out_key in vars_nss)
                # Avoid duplicating outputs from subgraphs in parent graphs.
                namespaced_output_name = '___'.join(namespaces + [step_name_or_key, out_key])
                lengths_off_by_one = (len(step_node_name.split('___')) + 1 == len(namespaced_output_name.split('___')))
                # TODO: check is_root here
                case1 = case1 and not is_root and lengths_off_by_one
                case2 = (tool_i['class'] == 'CommandLineTool') and (not out_var in vars_workflow_output_internal)
                if case1 or case2:
                    graph_gv = graph.graphviz
                    graph_nx = graph.networkx
                    graphdata = graph.graphdata
                    attrs = {'label': out_key_no_namespace, 'shape': 'box',
                             'style': 'rounded, filled', 'fillcolor': 'lightyellow'}
                    graph_gv.node(namespaced_output_name, **attrs)
                    font_edge_color = 'black' if args.graph_dark_theme else 'white'
                    if args.graph_label_edges:
                        graph_gv.edge(step_node_name, namespaced_output_name, color=font_edge_color,
                                      label=out_key_no_namespace)  # Is labeling necessary?
                    else:
                        graph_gv.edge(step_node_name, namespaced_output_name, color=font_edge_color)
                    graph_nx.add_node(namespaced_output_name)
                    graph_nx.add_edge(step_node_name, namespaced_output_name)
                    graphdata.nodes.append((namespaced_output_name, attrs))
                    graphdata.edges.append((step_node_name, namespaced_output_name, {}))
            # NOTE: Unless we are in the root workflow, we always need to
            # output everything. This is because while we are within a
            # subworkflow, we do not yet know if a subworkflow output will be used as
            # an input in a parent workflow (either explicitly, or using inference).
            # Thus, we have to output everything.
            # However, once we reach the root workflow, we can choose whether
            # we want to pollute our output directory with intermediate files.
            # (You can always enable --provenance to get intermediate files.)
            # NOTE: Remove is_root for now because in test_cwl_embedding_independence,
            # we recompile all subworkflows as if they were root.
            # Exclude intermediate 'output' files.
            if out_var in vars_workflow_output_internal:  # and is_root
                continue
            out_name = f'{step_name_or_key}___{out_key}'  # Use triple underscore for namespacing so we can split later
            # print('out_name', out_name)

        for out_key, out_dict in outputs_workflow[i].items():
            out_dict['type'] = canonicalize_type(out_dict['type'])
            if 'scatter' in steps[i]:
                # Promote scattered output types to arrays
                out_dict['type'] = {'type': 'array', 'items': out_dict['type']}

            out_name = f'{step_name_or_key}___{out_key}'  # Use triple underscore for namespacing so we can split later
            out_var = f'{step_name_or_key}/{out_key}'
            workflow_outputs.update({out_name: {**out_dict, 'outputSource': out_var}})
        # print('workflow_outputs', workflow_outputs)
    # NOTE: The fix_conflicts 'feature' of cwltool prevents files from being
    # overwritten by appending _2, _3 etc.
    # The problem is that these renamed files now no longer match the glob
    # patterns in the outputBinding tags, thus they are not copied to the
    # final output folder in relocateOutputs() and/or stage_files().
    # Note that this error is not detected using --validate.
    # One workaround is to simply output all files.
    # TODO: glob "." is still returning null; need to use InitialWorkDirRequirement??
    output_all = {'output_all':
                  {'type':
                   {'type': 'array',
                    'items': ['Directory', 'File']},
                   'outputBinding': {'glob': '\".\"'},
                   'format': 'edam:format_2330'}}  # 'Textual format'
    # This crashes toil-cwl-runner, but not cwltool.
    # workflow_outputs.update(output_all) # type: ignore
    return workflow_outputs


def canonicalize_type(type_obj: Any) -> Any:
    """Recursively desugars the CWL type: field into a canonical normal form.\n
    In particular, CWL automatically desugars File[] into {'type': 'array', 'items': File},
    but File[][] causes a syntax error! Etc.

    Args:
        type_obj (Any): An object that is a syntactic hodgepodge of valid CWL types.

    Returns:
        Any: The JSON canonical normal form associated with type_obj
    """
    match type_obj:
        case str() as str_obj:
            if len(str_obj) >= 1 and str_obj[-1:] == '?':
                return ['null', canonicalize_type(str_obj[:-1])]
            if len(str_obj) >= 2 and str_obj[-2:] == '[]':
                return {'type': 'array', 'items': canonicalize_type(str_obj[:-2])}
            return str_obj
        case dict() as dict_obj:
            if dict_obj.get('type') == 'array':
                return {**dict_obj, 'items': canonicalize_type(dict_obj['items'])}
            return dict_obj
        case _:
            return type_obj


def canonicalize_steps_list(steps: Yaml) -> List[Yaml]:
    if isinstance(steps, list):
        all_dicts = all([isinstance(elt, dict) for elt in steps])
        if not all_dicts:
            msg = 'Error! If steps: tag is a List then all its elements should be Dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(steps)}")
        return steps
    if isinstance(steps, dict):
        items = [(key, {}) if val is None else (key, val) for key, val in steps.items()]
        all_dicts = all([isinstance(val, dict) for key, val in items])
        if not all_dicts:
            msg = 'Error! If steps: tag is a Dictionary then all its values should be Dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(steps)}")
        return [{'id': key, **val} for key, val in items]
    # steps should either be a list or a dict, but...
    return steps


def remove_id_tags(list_of_dicts_with_id_keys: list) -> dict[str, Yaml]:
    d_canon = {}
    for d in list_of_dicts_with_id_keys:
        id_tag = d['id']
        del d['id']
        # NOTE: The order of the steps may not be preserved!
        d_canon[id_tag] = d
    return d_canon


def canonicalize_steps_dict(steps: Yaml) -> Dict[str, Yaml]:
    if isinstance(steps, list):
        all_dicts = all([isinstance(elt, dict) for elt in steps])
        if not all_dicts:
            msg = 'Error! If steps: tag is a List then all its elements should be Dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(steps)}")
        return remove_id_tags(steps)
    if isinstance(steps, dict):
        all_dicts = all([isinstance(val, dict) for val in steps.values()])
        if not all_dicts:
            msg = 'Error! If steps: tag is a dictionary then all its values should be dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(steps)}")
        return steps
    # steps should either be a list or a dict, but...
    return steps


def canonicalize_inputs_dict(inputs: Yaml) -> Dict[str, Yaml]:
    inputs_canon = {}
    if isinstance(inputs, dict):
        for key, val in inputs.items():
            if isinstance(val, dict):
                inputs_canon[key] = val
            elif isinstance(val, str):
                inputs_canon[key] = {'type': val}  # NOTICE
            else:
                msg = 'Error! If inputs: tag is a dictionary, then all its values should be either strings (representing types) or dictionaries.'
                raise Exception(f"{msg}\n{yaml.dump(inputs)}")
    if isinstance(inputs, list):
        all_dicts = all([isinstance(elt, dict) for elt in inputs])
        if not all_dicts:
            msg = 'Error! If inputs: tag is a list then all its elements should be dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(inputs)}")
        return remove_id_tags(inputs)
    return inputs_canon


def canonicalize_outputs_dict(outputs: Yaml) -> Dict[str, Yaml]:
    outputs_canon = {}
    if isinstance(outputs, dict):
        for key, val in outputs.items():
            if isinstance(val, dict):
                outputs_canon[key] = val
            elif isinstance(val, str):
                # TODO need to lookup output file mapping!
                outputs_canon[key] = val  # type: ignore
            else:
                msg = 'Error! outputs: tag should be a dictionary whose values are either strings (representing output files) or dictionaries.'
                raise Exception(f"{msg}\n{yaml.dump(outputs)}")
    if isinstance(outputs, list):
        all_dicts = all([isinstance(elt, dict) for elt in outputs])
        if not all_dicts:
            msg = 'Error! If outputs: tag is a list then all its elements should be dictionaries!'
            raise Exception(f"{msg}\n{yaml.dump(outputs)}")
        return remove_id_tags(outputs)
    return outputs_canon


def desugar_into_canonical_normal_form(cwl: Yaml) -> Yaml:
    if 'inputs' in cwl:
        # Arbitrarily choose dict form
        cwl['inputs'] = canonicalize_inputs_dict(cwl['inputs'])
    if 'outputs' in cwl:
        cwl['outputs'] = canonicalize_outputs_dict(cwl['outputs'])
    if 'steps' in cwl:
        # NOTE: No steps: to canonicalize for class: CommandLineTool
        # Choose list form due to
        # 1. dict keys must be unique (thus cannot use the same CLT twice)
        # 2. Some AST transformations (i.e. python_script) need to mutate the step id in-place
        # (which is not possible in dict form)
        # 3. Inlineing a subworkflow dict into the parent workflow dict may also cause key collisions.
        cwl['steps'] = canonicalize_steps_list(cwl['steps'])
    return cwl


def copy_cwl_input_output_dict(io_dict: Dict, remove_qmark: bool = False) -> Dict:
    """Copies the type, format, label, and doc entries. Does NOT copy inputBinding and outputBinding.

    Args:
        io_dict (Dict): A dictionary
        remove_qmark (bool): Determines whether to remove question marks and thus make optional types required

    Returns:
        Dict: A copy of the dictionary.
    """
    io_type = io_dict['type']
    if isinstance(io_type, str) and remove_qmark:
        io_type = io_type.replace('?', '')  # Providing optional arguments makes them required
    new_dict = {'type': canonicalize_type(io_type)}
    for key in ['format', 'label', 'doc']:
        if key in io_dict:
            new_dict[key] = io_dict[key]  # copy.deepcopy() ?
    return new_dict
