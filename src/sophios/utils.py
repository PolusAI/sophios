import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .wic_types import (Namespaces, RoseTree, StepId, Json, Yaml, YamlForest, YamlTree)


def step_name_str(yaml_stem: str, i: int, step_key: str) -> str:
    """Returns a string which uniquely and hierarchically identifies a step in a workflow

    Args:
        yaml_stem (str): The name of the workflow (filepath stem)
        i (int): The (zero-based) step number
        step_key (str): The name of the step (used as a dict key)

    Returns:
        str: The parameters (and the word 'step') joined together with double underscores
    """
    # Use double underscore so we can '__'.split() below.
    # (This should work as long as yaml_stem and step_key do not contain __)
    return f'{yaml_stem}__step__{i+1}__{step_key}'


def parse_step_name_str(step_name: str) -> Tuple[str, int, str]:
    """The inverse function to step_name_str()

    Args:
        step_name (str): A string of the same form as returned by step_name_str()

    Raises:
        Exception: If the argument is not of the same form as returned by step_name_str()

    Returns:
        Tuple[str, int, str]: The parameters used to create step_name
    """
    vals = step_name.split('__')  # double underscore
    if not len(vals) == 4:
        raise Exception(f"Error! {step_name} is not of the format \n"
                        + '{yaml_stem}__step__{i+1}__{step_key}\n'
                        + 'yaml_stem and step_key should not contain any double underscores.')
    try:
        i = int(vals[2])
    except Exception as ex:
        raise Exception(f"Error! {step_name} is not of the format \n"
                        + '{yaml_stem}__step__{i+1}__{step_key}') from ex
    return (vals[0], i-1, vals[3])


def shorten_namespaced_output_name(namespaced_output_name: str, sep: str = ' ') -> Tuple[str, str]:
    """Removes the intentionally redundant yaml_stem prefixes from the list of
    step_name_str's embedded in namespaced_output_name which allows each
    step_name_str to be context-free and unique. This is potentially dangerous,
    and the only purpose is so we can slightly shorten the output filenames.

    Args:
        namespaced_output_name (str): A string of the form:
        '___'.join(namespaces + [step_name_i, out_key])
        sep (str): The separator used to construct the shortened step name strings.

    Returns:
        Tuple[str, str]: the first yaml_stem, so this function can be inverted,
        and namespaced_output_name, with the embedded yaml_stem prefixes
        removed and double underscores replaced with a single space.
    """
    split = namespaced_output_name.split('___')
    namespaces = split[:-1]
    output_name = split[-1]
    strs = []
    yaml_stem_init = ''
    if len(namespaces) > 0:
        yaml_stem_init = parse_step_name_str(namespaces[0])[0]
        for stepnamestr in namespaces:
            _, i, step_key = parse_step_name_str(stepnamestr)
            strs.append(f'step{sep}{i+1}{sep}{step_key}')
    shortened = '___'.join(strs + [output_name])
    return (yaml_stem_init, shortened)


def restore_namespaced_output_name(yaml_stem_init: str, shortened_output_name: str, sep: Optional[str] = None) -> str:
    """The inverse function to shorten_namespaced_output_name()

    Args:
        yaml_stem_init (str): The initial yaml_stem prefix
        shortened_output_name (str): The shortened namespaced_output_name
        sep (Optional[str], optional): The separator used for shortening. Defaults to None.

    Raises:
        Exception: If the argument is not of the same form as returned by shorten_namespaced_output_name

    Returns:
        str: The original namespaced_output_name before shortening.
    """
    if yaml_stem_init == '':
        return shortened_output_name
    else:
        split = shortened_output_name.split('___')
        namespaces = split[:-1]
        output_name = split[-1]
        yaml_stem = yaml_stem_init
        strs = []
        for shortened_step_name_str in namespaces:
            words = shortened_step_name_str.split(sep)
            if len(words) != 3:
                raise Exception(f'Error! {shortened_step_name_str} is not of the correct format!')
            _, num, name_yml = words
            strs.append(f'{yaml_stem}{sep}step{sep}{num}{sep}{name_yml}')
            yaml_stem = Path(name_yml).stem
        restored = '___'.join(strs + [output_name])
        return restored


def partition_by_lowest_common_ancestor(nss1: Namespaces, nss2: Namespaces) -> Tuple[Namespaces, Namespaces]:
    """See https://en.wikipedia.org/wiki/Lowest_common_ancestor

    Args:
        nss1 (Namespaces): The namespaces associated with the first node
        nss2 (Namespaces): The namespaces associated with the second node

    Returns:
        Tuple[Namespaces, Namespaces]: nss1, partitioned by lowest common ancestor
    """
    # Only partition nss1; if you want to partition nss1
    # just switch the arguments at the call site.
    if nss1 == [] or nss2 == []:
        return ([], nss1)  # Base case
    if nss1[0] == nss2[0]:  # Keep going
        (nss1_heads, nss1_tails) = partition_by_lowest_common_ancestor(nss1[1:], nss2[1:])
        return ([nss1[0]] + nss1_heads, nss1_tails)
    return ([], nss1)


def get_steps_keys(steps: List[Yaml]) -> List[str]:
    """Returns the name (dict key) of each step in the given CWL workflow

    Args:
        steps (List[Yaml]): The steps: tag of a CWL workflow

    Returns:
        List[str]: The name of each step in the given CWL workflow
    """
    steps_keys = []
    for step_dict in steps:
        if isinstance(step_dict, dict):
            steps_keys.append(step_dict.get('id', ''))
        else:
            steps_keys.append('')
    return steps_keys


def get_subkeys(steps_keys: List[str]) -> List[str]:
    """This function determines which step keys are associated with subworkflows.\n
    This is critical for the control flow in many areas of the compiler.

    Args:
        steps_keys (List[str]): All of the step keys for the current workflow.

    Returns:
        List[str]: The list of step keys associated with subworkflows of the current workflow.
    """
    return [key for key in steps_keys if key and key.endswith('.wic')]


def extract_implementation(yaml_tree: Yaml, wic: Yaml, yaml_path: Path) -> Tuple[str, Yaml]:
    """Chooses a specific implementation for a given CWL workflow step.

    The implementations should be thought of as either 'exactly' identical, or at
    least the same high-level protocol but implemented with a different algorithm.

    Args:
        yaml_tree (Yaml): A Yaml AST dict with sub-dicts for each implementation.
        yaml_path (Path): The filepath of yaml_tree, only used for error reporting.

    Raises:
        Exception: If the steps: and/or implementation: tags are not present.

    Returns:
        Tuple[str, Yaml]: The Yaml AST dict of the chosen implementation.
    """
    yaml_tree_copy = copy.deepcopy(yaml_tree)
    implementation = ''
    if 'implementations' in wic:
        if 'default_implementation' in wic:
            implementation = wic['default_implementation']
        if 'implementation' in wic:
            implementation = wic['implementation']
        if implementation == '':
            raise Exception(f'Error! No implementation in {yaml_path}!')

        plugin_ns = wic.get('namespace', 'global')
        stepid = StepId(implementation, plugin_ns)
        if stepid not in wic['implementations']:
            print(yaml.dump(yaml_tree))
            print(yaml.dump(wic))
            print(wic['implementations'])
            raise Exception(f'Error! No steps for implementation {stepid} in {yaml_path}!')
        steps = wic['implementations'][stepid]['steps']
        yaml_tree_copy.update({'steps': steps})
        # TODO: Use the entire back_tree? Useful for inputs:
        # back_tree = wic['implementations'][stepid]
        # if 'wic' in back_tree:
        #    del back_tree['wic']
        # yaml_tree_copy.update(back_tree)
    elif 'steps' in yaml_tree_copy:
        pass  # steps = yaml_tree_copy['steps']
    else:
        raise Exception(f'Error! No implementations and/or steps in {yaml_path}!')
    return (implementation, yaml_tree_copy)


def flatten(lists: List[List[Any]]) -> List[Any]:
    """Concatenates a list of lists into a single list.

    Args:
        lists (List[List[Any]]): A list of lists

    Returns:
        List[Any]: A single list
    """
    return [x for lst in lists for x in lst]


def flatten_rose_tree(rose_tree: RoseTree) -> List[Any]:
    """Flattens the data contained in the Rose Tree into a List

    Args:
        rose_tree (RoseTree): A Rose Tree

    Returns:
        List[Any]: The list of data associated with each node in the RoseTree
    """
    sub_rose_trees = [flatten_rose_tree(r) for r in rose_tree.sub_trees]
    return [rose_tree.data] + flatten(sub_rose_trees)


def pretty_print_forest(forest: YamlForest) -> None:
    """pretty prints a YamlForest

    Args:
        forest (YamlForest): The forest to be printed
    """
    print(forest.yaml_tree.step_id)
    print(yaml.dump(forest.yaml_tree.yml))
    print(yaml.dump(forest.sub_forests))


def flatten_forest(forest: YamlForest) -> List[YamlForest]:
    """Flattens the sub-trees encountered while traversing an AST

    Args:
        forest (YamlForest): The yaml AST forest to be flattened

    Raises:
        Exception: If implementation: tags are missing.

    Returns:
        List[YamlForest]: The flattened forest
    """
    # pretty_print_forest(forest)
    if forest == {}:
        return []
    yaml_tree = forest.yaml_tree.yml
    wic = {'wic': yaml_tree.get('wic', {})}
    plugin_ns = wic['wic'].get('namespace', 'global')

    if 'implementations' in wic['wic']:
        # pretty_print_forest(forest)
        back_name = ''
        if 'default_implementation' in wic['wic']:
            back_name = wic['wic']['default_implementation']
        if 'implementation' in wic['wic']:
            back_name = wic['wic']['implementation']
        if back_name == '':
            pretty_print_forest(forest)
            raise Exception('Error! No implementation in yaml forest!\n')
        sub_forests_dict = dict(forest.sub_forests)
        step_id = StepId(back_name, plugin_ns)
        yaml_tree_back: YamlTree = sub_forests_dict[step_id].yaml_tree
        step_1 = yaml_tree_back.yml['steps'][0]
        step_name_1 = list(step_1.keys())[0]
        if Path(step_name_1).suffix == '.wic':
            # Choose a specific implementation
            return flatten_forest(sub_forests_dict[step_id])
        return [forest]

    forests = [f[1] for f in forest.sub_forests]
    sub_forests = [flatten_forest(f) for f in forests]
    # Use depth first search flattening to match flatten_rose_tree()
    # bfs = forests + flatten(sub_forests)
    dfs_lists = [[f] + fs for f, fs in zip(forests, sub_forests)]
    dfs = flatten(dfs_lists)
    return dfs


def recursively_delete_dict_key(key: str, obj: Any) -> Any:
    """Recursively deletes any dict entries with the given key.

    Args:
        key (str): The key to be deleted
        obj (Any): The object from which to delete key.

    Returns:
        Any: The original dict with the given key recursively deleted.
    """
    if isinstance(obj, List):
        return [recursively_delete_dict_key(key, x) for x in obj]
    if isinstance(obj, Dict):
        new_dict = {}
        for key_ in obj.keys():
            if not key_ == key:  # i.e. effectively delete key
                new_dict[key_] = recursively_delete_dict_key(key, obj[key_])
        return new_dict
    return obj


def recursively_contains_dict_key(key: str, obj: Any) -> bool:
    """Recursively checks whether obj contains entries with the given key.

    Args:
        key (str): The key to be checked
        obj (Any): The object from which to check the key.

    Returns:
        bool: True if key is found, else False.
    """
    if isinstance(obj, List):
        return any([recursively_contains_dict_key(key, x) for x in obj])
    if isinstance(obj, Dict):
        return (key in obj.keys()) or any(recursively_contains_dict_key(key, val) for val in obj.values())
    return False


def parse_int_string_tuple(string: str) -> Tuple[int, str]:
    """Parses a string of the form '(int, string)'

    Args:
        string (str): A string with the above encoding

    Returns:
        Tuple[int, str]: The parsed result
    """
    string_no_parens = string.strip()[1:-1]
    (str1, str2) = string_no_parens.split(',')
    return (int(str1.strip()), str2.strip())


def reindex_wic_steps(wic_steps: Yaml, index: int, num_steps: int = 1) -> Yaml:
    """ Increment 1-based step index starting from the step with the given index by num_steps.

    This function can be used to reindex steps after inserting num_steps at the given index: in\n
    the wic: metadata annotations tag whose index (before insertion) is >= the given index.

    Args:
        wic_steps (Yaml): The steps: subtag of the wic: metadata annotations tag.
        index (int): The (one-based) start index that needs to be reindexed.
        num_steps (int): The number of steps inserted.

    Returns:
        Yaml: The updated wic: steps: tag, with the appropriate indices incremented.
    """
    wic_steps_reindexed = {}
    for keystr, val in wic_steps.items():
        (i, s) = parse_int_string_tuple(keystr)
        newstr = f'({i+num_steps}, {s})' if i >= index else keystr
        wic_steps_reindexed[newstr] = val
    return wic_steps_reindexed


def get_step_name_1(step_1_names: List[str],
                    yaml_stem: str,
                    namespaces: Namespaces,
                    steps_keys: List[str],
                    subkeys: List[str]) -> str:
    """Finds the name of the first step in the current subworkflow. If the first
    step is itself subworkflow, the call site recurses until it finds a node.
    This is necessary because ranksame in GraphViz can only be applied to
    individual nodes, not cluster_subgraphs.

    Args:
        step_1_names (List[str]): The list of potential first node names
        yaml_stem (str): The name of the current subworkflow (stem of the yaml filepath)
        namespaces (Namespaces): Specifies the path in the AST of the current subworkflow
        steps_keys (List[str]): The name of each step in the current CWL workflow
        subkeys (List[str]): The keys associated with subworkflows

    Returns:
        str: The name of the first step
    """
    if steps_keys[0] in subkeys:
        step_name_1 = step_1_names[0]
    else:
        step_name_1 = step_name_str(yaml_stem, 0, steps_keys[0])
        step_name_1 = '___'.join(namespaces + [step_name_1])
    # NOTE: Since the names of subgraphs '*.wic' contain a period, we need to
    # escape them by enclosing the whole name in double quotes. Otherwise:
    # "Error: *.wic.gv: syntax error in line n near '.'"
        step_name_1 = f'"{step_name_1}"'

    return step_name_1


def parse_provenance_output_files(output_json: Json) -> List[Tuple[str, str, str]]:
    """Parses the primary workflow provenance JSON object.

    Args:
        output_json (Json): The JSON results object, containing the metadata for all output files.

    Returns:
        List[Tuple[str, str, str]]: A List of (location, parentdirs, basename) for each output file.
    """
    files = []
    for namespaced_output_name, obj in output_json.items():
        files.append(parse_provenance_output_files_(obj, namespaced_output_name))
    return [y for x in files for y in x]


def parse_provenance_output_files_(obj: Any, parentdirs: str) -> List[Tuple[str, str, str]]:
    """Parses the primary workflow provenance JSON object.

    Args:
        obj (Any): The provenance object or one of its recursive sub-objects.
        parentdirs (str): The directory associated with obj.

    Returns:
        List[Tuple[str, str, str]]: A List of (location, parentdirs, basename) for each output file.
    """
    if isinstance(obj, Dict):
        if obj.get('class', '') == 'File':
            return [(str(obj['location']), parentdirs, str(obj['basename']))]  # This basename is a file name
        if obj.get('class', '') == 'Directory':
            subdir = parentdirs + '/' + obj['basename']  # This basename is a directory name
            return parse_provenance_output_files_(obj['listing'], subdir)
    if isinstance(obj, List):
        files = []
        for o in obj:
            files.append(parse_provenance_output_files_(o, parentdirs))
        # Should we flatten?? This will lose the structure of 2D (and higher) array outputs.
        return [y for x in files for y in x]
    return []


def get_input_mappings(input_mapping: Dict[str, List[str]], arg_keys: List[str],
                       arg_key_in_yaml_tree_inputs: bool) -> List[str]:
    """Gets all of the workflow step inputs / call sites that are mapped from the given workflow inputs.

    Args:
        input_mapping (Dict[str, List[str]]): Maps workflow inputs to workflow step inputs, recursively namespaced.
        arg_keys (List[str]): A (singleton) list of root workflow inputs.
        arg_key_in_yaml_tree_inputs (bool): Determines whether at least one level of recursion has been performed.

    Returns:
        List[str]: A list of the workflow step inputs / call sites, recursively namespaced.
    """
    # print('arg_keys', arg_keys)
    # Since each workflow input can be used in many workflow steps, we
    # need to (recursively) find all of the leaves of the mapping tree
    # corresponding to the root arg_key/in_name. Since we already added all
    # sub-input_mapping's (with namespacing) after each recursive call,
    # this flattens the recursion into iteration here. The only trick is
    # that we also need to remove the intermediate variables associated
    # with subworkflow boundaries.
    if not arg_key_in_yaml_tree_inputs:
        done = False
        while not done:
            done = True
            arg_keys_accum = []
            for arg_key_ in arg_keys:
                if arg_key_ in input_mapping:
                    # Remove the intermediate variables associated with subworkflow boundaries.
                    arg_key_init_namespaces = arg_key_.split('___')[:-1]
                    temp = ['___'.join(arg_key_init_namespaces + [s]) for s in input_mapping[arg_key_]]
                    arg_keys_accum.append(temp)
                    done = False
                else:
                    arg_keys_accum.append([arg_key_])
            arg_keys = [y for x in arg_keys_accum for y in x]
            # print('arg_keys', arg_keys)

    return arg_keys


def get_output_mapping(output_mapping: Dict[str, str], out_key: str) -> str:
    """Gets the workflow step output / return location that is mapped to the given workflow output.

    Args:
        output_mapping (Dict[str, str]): Maps workflow outputs to workflow step outputs, recursively namespaced.
        out_key (str): The root workflow output.

    Returns:
        str: The workflow step output / return location, recursively namespaced.
    """
    # print('out_key', out_key)
    # Similarly, we need to find the fixed-point of output_mapping.
    # This is simpler since a workflow output can only come from one workflow step.
    # if not out_key_in_yaml_tree_outputs:
    done = False
    while not done:
        done = True
        # out_key = f'{step_name_j}___{out_key}' # TODO: Check this
        if out_key in output_mapping:
            # Remove the intermediate variables associated with subworkflow boundaries.
            out_key_init_namespaces = out_key.split('___')[:-1]
            out_key = '___'.join(out_key_init_namespaces + [output_mapping[out_key]])
            done = False
        # print('out_key', out_key)

    return out_key
