import argparse
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import utils, utils_cwl, utils_graphs
from .wic_types import (GraphReps, InternalOutputs, Namespaces, StepId, Tool, Tools,
                        WorkflowInputs, Yaml)

# NOTE: This must be initialized in main.py and/or cwl_subinterpreter.py
renaming_conventions: List[Tuple[str, str]] = []


def types_match(in_type: Any, out_type: Any) -> bool:
    if in_type == out_type:
        return True
    if isinstance(in_type, list) and not isinstance(out_type, list):
        return any([x == out_type for x in in_type])
    if isinstance(out_type, list) and not isinstance(in_type, list):
        # If only some of the output types match the input type, and
        # if at runtime the actual output is one of those matches,
        # then great! But we can't know that at compile time.
        # If we want to guarantee this won't fail (due to format mismatch)
        # at runtime then use all, not any. However, using all excludes many
        # common use cases (format: ["null", ...]) and there are plenty of
        # other ways to fail at runtime, so for now let's allow it.
        # (If inference makes the 'wrong' choice, just use an explicit edge!)
        return any([x == in_type for x in out_type])
    if isinstance(in_type, list) and isinstance(out_type, list):
        # Same comment here
        return any([x in in_type for x in out_type])
    return False


def perform_edge_inference(args: argparse.Namespace,
                           tools: Tools,
                           tools_lst: List[Tool],
                           steps_keys: List[str],
                           yaml_stem: str,
                           i: int,
                           steps: List[Yaml],
                           arg_key: str,
                           graph: GraphReps,
                           is_root: bool,
                           namespaces: Namespaces,
                           vars_workflow_output_internal: InternalOutputs,
                           input_mapping: Dict[str, List[str]],
                           output_mapping: Dict[str, str],
                           inputs_workflow: WorkflowInputs,
                           in_name: str,
                           in_name_in_inputs_file_workflow: bool,
                           arg_key_in_yaml_tree_inputs: bool,
                           insertions: List[StepId],
                           wic_steps: Yaml,
                           testing: bool) -> Yaml:
    """This function implements the core edge inference feature.
    NOTE: steps[i], vars_workflow_output_internal, inputs_workflow are mutably updated.

    Args:
        args (argparse.Namespace): The command line arguments
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()
        tools_lst (List[Tool]): A list of the CWL CommandLineTools or compiled subworkflows for the current workflow.
        steps_keys (List[str]): The name of each step in the current CWL workflow
        yaml_stem (str): The name (filename without extension) of the current CWL workflow
        i (int): The (zero-based) step number w.r.t. the current subworkflow.\n
        Since we are trying to infer inputs from previous outputs, this will not\n
        perform any inference (again, w.r.t. the current subworkflow) if i == 0.
        steps (List[Yaml]): The steps: tag of the current CWL workflow
        arg_key (str): The name of the CWL input tag that needs a concrete input value inferred
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        is_root (bool): True if this is the root workflow (for debugging only)
        namespaces (Namespaces): Specifies the path in the AST of the current subworkflow
        vars_workflow_output_internal (InternalOutputs): Keeps track of output\n
        variables which are internal to the root workflow, but not necessarily to subworkflows.
        input_mapping (Dict[str, List[str]]): Maps workflow inputs to workflow step inputs, recursively namespaced.
        output_mapping (Dict[str, str]): Maps workflow outputs to workflow step outputs, recursively namespaced.
        inputs_workflow (WorkflowInputs): Keeps track of CWL inputs: variables for the current workflow.
        in_name (str): The input name
        in_name_in_inputs_file_workflow (bool): Used to determine whether\n
        failure to find a match should be considered an error.
        arg_key_in_yaml_tree_inputs (bool): Determines whether at least one level of recursion has been performed.
        insertions (List[StepId]): If exact inference fails, a list of possible steps to automatically insert is stored here.
        wic_steps (Yaml): The metadata associated with the given workflow.
        testing: Used to disable some optional features which are unnecessary for testing.

    Returns:
        Yaml: steps[i] with the input tag arg_key updated with an inferred input value.
    """
    # Use in_name_in_inputs_file_workflow: bool so that at the call site, we don't
    # have to question whether or not this function modifies inputs_file_workflow
    # gmx_trjconv_str assumes gro, but other tools assume tpr; For now,
    # instead of figuring out how to replace('structure_', 'gro_') for
    # gmx_trjconv_str only, just require the user to specify filename.
    step_key = steps_keys[i]
    tool_i = tools_lst[i]
    step_name_i = utils.step_name_str(yaml_stem, i, step_key)

    in_tool = tool_i.cwl['inputs']
    in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key], True)

    if arg_key in steps[i].get('scatter', []):
        # Promote scattered input types to arrays
        in_dict['type'] = {'type': 'array', 'items': in_dict['type']}

    in_formats = []
    if 'format' in in_tool[arg_key]:
        in_formats = in_tool[arg_key]['format']
        in_dict['format'] = in_formats
    # print('step_name_i, arg_key, in_formats', step_name_i, arg_key, in_formats)
    format_matches_all = []
    attempted_matches_all = []
    break_inference = False
    for j in range(0, i)[::-1]:  # Reverse order!
        wic_step_j = wic_steps.get(f'({j+1}, {steps_keys[j]})', {})
        tool_j = tools_lst[j]
        out_tool = tool_j.cwl['outputs']
        # NOTE: The outputs of a CommandLineTool are all made available
        # simultaneously. Although that is technically also true for subworkflows,
        # the CommandLineTools within the subworkflow are certainly ordered,
        # and so we definitely want to use reverse order here. As mentioned,
        # it doesn't necessarily make sense for CommandLineTools, but the
        # important thing is that we just define the order for users some way.
        out_keys = list(tool_j.cwl['outputs'])[::-1]  # Reverse order!
        format_matches = []
        attempted_matches = []
        inference_rules = get_inference_rules(wic_step_j, Path(steps_keys[j]).stem)
        namespace_emb_last_break = ''
        for out_key in out_keys:
            namespaces_embedded = out_key.split('___')
            namespace_emb_last = '' if len(namespaces_embedded) <= 1 else namespaces_embedded[:-1][-1]  # -2?
            if break_inference and namespace_emb_last != namespace_emb_last_break:
                break  # Only break once the namespace changes, i.e. on the next step
            inference_rule = inference_rules.get(out_key, 'default')
            # Apply 'continue' rule before iteration, to prevent matching
            # TODO: This currently causes an infinite loop.
            # if inference_rule == 'continue':
            #    continue

            out_dict = utils_cwl.copy_cwl_input_output_dict(out_tool[out_key])

            if 'scatter' in steps[j]:
                # Promote scattered output types to arrays
                out_dict['type'] = {'type': 'array', 'items': out_dict['type']}

            out_format = ''
            if 'format' in out_tool[out_key]:
                out_format = out_tool[out_key]['format']
                out_dict['format'] = out_format
            # if out_format == '':
            #    #print('Warning! No output format! Cannot possibly match!')
            #    print('Warning! No output format! Will match anything!')
            #    print(f'out_key {out_key}')
            # print('out_key, out_format, rule', out_key, out_format, inference_rule)
            attempted_matches.append((out_key, out_format))
            # Great! We found an 'exact' type and format match.
            if types_match(in_dict['type'], out_dict['type']):  # First we have to match the types.
                if in_formats:
                    # Then, if we have an input format or formats, the output format has to match.
                    if out_format == in_formats or out_format in in_formats:
                        format_matches.append((out_key, out_format))
                else:
                    # Otherwise, formats are optional and we match only on type.
                    format_matches.append((out_key, out_format))

            # Apply 'break' rule after iteration, to allow matching
            if inference_rule == 'break':
                break_inference = True
                namespace_emb_last_break = namespace_emb_last

        format_matches_all.append(format_matches)
        attempted_matches_all.append(attempted_matches)
        # Most log files just have format_2330 "Textual format", but this can
        # conflict with other structured text files that also have format_2330.
        # Unfortunately, the edam formats are not very finely curated, so they
        # are not very 'exact'. For now, we can perform additional matching
        # based on naming conventions and/or we can simply exclude log files
        # (which are not usually parsed or otherwise used as inputs).
        # Eventually, we will want to improve the format curation.
        # NOTE: Use underscores to prevent excluding e.g. 'topology'
        # This isn't great, but works for now (until someone uses '_log_' ...)
        format_matches = [x for x in format_matches if not '_log_' in x[0]]
        # print('format_matches', format_matches)
        if not len(format_matches) == 0:
            # By default, simply choose the first (i.e. most-recent) matching format
            out_key = format_matches[0][0]

            if args.inference_use_naming_conventions:  # default False
                if len(format_matches) == 1:
                    # Great! We found a unique format match.
                    out_key = format_matches[0][0]
                else:
                    name_matches = []
                    # NOTE: The biobb CWL files do not use consistent naming
                    # conventions, so we need to perform some renamings here.
                    # Eventually, the CWL files themselves should be fixed.
                    arg_key_no_namespace = arg_key.split('___')[-1]
                    arg_key_renamed = arg_key_no_namespace.replace('input_', '')
                    for name1, name2 in renaming_conventions:
                        arg_key_renamed = arg_key_renamed.replace(name1, name2)

                    for out_key, out_format in format_matches:
                        out_key_no_namespace = out_key.split('___')[-1]
                        out_key_renamed = out_key_no_namespace.replace('output_', '')
                        if arg_key_renamed == out_key_renamed:
                            name_matches.append((out_key, out_format))

                    if len(name_matches) == 0:
                        # s = f"""Found multiple outputs with compatible types and formats
                        # (but no matching names) for input {arg_key}"""
                        # print(s)
                        # for m in format_matches:
                        #    print(m)
                        # print(f'Arbitrarily choosing the first match {format_matches[0][0]}')
                        out_key = format_matches[0][0]
                    elif len(name_matches) == 1:
                        # NOTE: This clause currently causes problems with insertions.
                        # Specifically, we want to convert from format A to B, perform the calculation
                        # in format B, then convert the results back to format A. However, if the
                        # naming conventions of the result do not match the second insertion
                        # (but DO match the first insertion), the files will be directly converted
                        # from A to B to A, thus skipping the calculation in B entirely!
                        # Great! We found a unique match.
                        out_key = name_matches[0][0]
                        # print('unique match', out_key)
                    else:
                        # s = f"""Found multiple outputs with compatible types and formats
                        # (and multiple matching names) for input {arg_key}"""
                        # print(s)
                        # for m in name_matches:
                        #    print(m)
                        # print(f'Arbitrarily choosing the first match {name_matches[0][0]}')
                        out_key = name_matches[0][0]

            # print('match!', j)  # We found a match!
            # Generate a new namespace for out_key using the step number and add to inputs
            step_name_j = utils.step_name_str(yaml_stem, j, steps_keys[j])

            # We also need to keep track of the 'internal' output variables
            if tool_j.cwl['class'] == 'Workflow':
                vars_workflow_output_internal.append(out_key)
            else:
                vars_workflow_output_internal.append(f'{step_name_j}/{out_key}')

            arg_val = f'{step_name_j}/{out_key}'
            arg_keyval = {arg_key: arg_val}
            steps_i = utils_cwl.add_yamldict_keyval_in(steps[i], step_key, arg_keyval)
            # print(f'inference i {i} y arg_key {arg_key}')

            arg_keys = [in_name] if in_name in input_mapping else [arg_key]
            arg_keys = utils.get_input_mappings(input_mapping, arg_keys, arg_key_in_yaml_tree_inputs)

            out_key = utils.get_output_mapping(output_mapping, out_key)

            nss_embedded1 = out_key.split('___')[:-1]

            # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
            if out_key.startswith('___'.join(namespaces + [step_name_j])):
                nss1 = nss_embedded1
            elif out_key.startswith(step_name_j):
                nss1 = namespaces + nss_embedded1
            else:
                nss1 = namespaces + [step_name_j] + nss_embedded1

            for arg_key_ in arg_keys:
                # Determine which head and tail node to use for the new edge
                # First we need to extract the embedded namespaces
                nss_embedded2 = arg_key_.split('___')[:-1]

                # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
                if arg_key_.startswith('___'.join(namespaces + [step_name_i])):
                    nss2 = nss_embedded2
                elif arg_key_.startswith(step_name_i):
                    nss2 = namespaces + nss_embedded2
                else:
                    nss2 = namespaces + [step_name_i] + nss_embedded2

                # TODO: check this
                out_key_no_namespace = out_key.split('___')[-1]
                label = out_key_no_namespace if tool_j.cwl['class'] == 'Workflow' else out_key
                utils_graphs.add_graph_edge(args, graph, nss1, nss2, label)

            return steps_i  # Short circuit

        # Stop performing inference if the inference rule is 'break'
        if break_inference:
            break

    # If no match yet, we can look for a potential steps to insert automatically.
    # NOTE: PLEASE READ docs/advanced.md#program-synthesis
    out_formats = [out_format for attempted_matches in attempted_matches_all
                   for (out_key_, out_format) in attempted_matches]
    # print('out_formats', out_formats)
    for in_format in in_formats:
        for out_format in out_formats:
            # Obviously we don't need an insertion if the file formats are the same.
            if in_format == out_format:
                continue

            for step_id, tool in tools.items():
                # For now, let's restrict to a whitelist.
                # Otherwise, there are way too many solutions.
                # (In principle, this can be used to insert arbitrary subworkflows.)
                if not step_id.stem.startswith('insert_steps_automatically_'):
                    continue

                in_tool = tool.cwl['inputs']
                tool_in_formats = [arg_val['format'] for arg_key, arg_val in in_tool.items() if 'format' in arg_val]
                tool_in_formats_flat = utils.flatten(tool_in_formats)

                out_tool = tool.cwl['outputs']
                tool_out_formats = [out_val['format'] for out_key, out_val in out_tool.items() if 'format' in out_val]

                # NOTE: Ideally, the second condition should really check that
                # ALL required inputs for the intermediate tool match with the
                # an element of out_formats. Since we cannot easily do that here,
                # we need to tentatively insert tool into the AST and re-compile.
                # However, that can easily fail (i.e. if there is one transitive
                # match). See docs/algorithms.md for more details.
                if in_format in tool_out_formats and out_format in tool_in_formats_flat:
                    # We may have found an insertion.
                    # print('step_id.stem, in_format, out_format:', step_id.stem, in_format, out_format)
                    insertions.append(step_id)

    match = False
    if not match:
        # print(f'inference i {i} n arg_key {arg_key}')
        # Use triple underscore for namespacing so we can split later
        in_name = f'{step_name_i}___{arg_key}'  # {step_name_i}_input___{arg_key}

        # This just means we need to defer to the parent workflow.
        # There will actually be an error only if no parent supplies an
        # input value and thus there is no entry in inputs_file_workflow.
        if is_root and not in_name_in_inputs_file_workflow and not testing:
            print('Error! No match found for input', i + 1, step_key, arg_key)
            # NOTE: The following print statement is a bit misleading because
            # we don't print out any attempted matches in the recursive case.
            print('number of attempted matches ', len(utils.flatten(attempted_matches_all)))
            for a_m in attempted_matches_all:
                for m in a_m:
                    print(m)

        # Add an input name to this subworkflow (only). Do not add to
        # inputs_file_workflow because this may be an internal input,
        # i.e. it may simply be split across subworkflow boundaries and
        # the input may be supplied in one of the parent workflows.
        # We also do not (necessarily) need to explicitly pass this list
        # to the parent workflow since we are compiling the 'in' tag here,
        # which should match in the parent workflow.
        inputs_workflow.update({in_name: in_dict})

        arg_keyval = {arg_key: in_name}
        steps_i = utils_cwl.add_yamldict_keyval_in(steps[i], step_key, arg_keyval)
        return steps_i

    # Add an explicit return statement here so mypy doesn't complain.
    # (mypy's static analysis cannot determine that this is dead code.)
    return steps[i]


def get_inference_rules(wic: Yaml, step_key_parent: str) -> Dict[str, str]:
    """Recursively traverses the wic: metadata annotation AST and extracts any inference rules.\n
    See docs/userguide.md for more information.

    Args:
        wic (Yaml): The contents of the wic: metadata annotations tag (if any)
        step_key_parent (str): The name of one of the steps in the current workflow.

    Returns:
        Dict[str, str]: A dictionary of the inference rules for the workflow step named step_key_parent.
    """
    # NOTE: Here, we simply return all inference rules. The call site then
    # determines whether to apply any of the rules by doing a lookup.
    rules: Dict[str, str] = {}
    if 'steps' in wic.get('wic', {}):
        wic_steps = wic['wic']['steps']
        for keystr, wic_child in wic_steps.items():
            (step_num, step_key) = utils.parse_int_string_tuple(keystr)
            namespace = utils.step_name_str(Path(step_key_parent).stem, step_num - 1, step_key)
            rules_child = get_inference_rules(wic_child, step_key)
            for rule_key, rule_val in rules_child.items():
                rules[namespace + '___' + rule_key] = rule_val  # Namespaceing
        return rules
    if 'inference' in wic.get('wic', {}):
        rules = wic['wic']['inference']
        return rules
    return {}
