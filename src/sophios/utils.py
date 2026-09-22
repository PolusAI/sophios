from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import yaml

from .wic_types import (Namespaces, StepId,
                        Json, Yaml, YamlTree)


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


def parse_step_name_str(step_name: str) -> tuple[str, int, str]:
    """The inverse function to step_name_str()

    Args:
        step_name (str): A string of the same form as returned by step_name_str()

    Raises:
        ValueError: If the argument is not of the same form as returned by step_name_str()

    Returns:
        tuple[str, int, str]: The parameters used to create step_name
    """
    vals = step_name.split('__')  # double underscore
    if len(vals) != 4:
        raise ValueError(f"Error! {step_name} is not of the format \n"
                         + '{yaml_stem}__step__{i+1}__{step_key}\n'
                         + 'yaml_stem and step_key should not contain any double underscores.')
    try:
        i = int(vals[2])
    except ValueError as ex:
        raise ValueError(f"Error! {step_name} is not of the format \n"
                         + '{yaml_stem}__step__{i+1}__{step_key}') from ex
    return (vals[0], i-1, vals[3])


def shorten_namespaced_output_name(namespaced_output_name: str, sep: str = ' ') -> tuple[str, str]:
    """Removes the intentionally redundant yaml_stem prefixes from the list of
    step_name_str's embedded in namespaced_output_name which allows each
    step_name_str to be context-free and unique. This is potentially dangerous,
    and the only purpose is so we can slightly shorten the output filenames.

    Args:
        namespaced_output_name (str): A string of the form:
        '___'.join(namespaces + [step_name_i, out_key])
        sep (str): The separator used to construct the shortened step name strings.

    Returns:
        tuple[str, str]: the first yaml_stem, so this function can be inverted,
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


def get_steps_keys(steps: list[Yaml]) -> list[str]:
    """Returns the name (dict key) of each step in the given CWL workflow

    Args:
        steps (list[Yaml]): The steps: tag of a CWL workflow

    Returns:
        list[str]: The name of each step in the given CWL workflow
    """
    steps_keys = []
    for step_dict in steps:
        if isinstance(step_dict, dict):
            steps_keys.append(step_dict.get('id', ''))
        else:
            steps_keys.append('')
    return steps_keys


def require_step_id(step_dict: Yaml, context: str = "step") -> str:
    """Return a validated step id from a step dictionary.

    Args:
        step_dict (Yaml): Candidate step object.
        context (str): Context string used in error messages.

    Raises:
        TypeError: If `step_dict` is not a dictionary.
        ValueError: If `step_dict` does not contain a non-empty string `id`.

    Returns:
        str: The validated step identifier.
    """
    if not isinstance(step_dict, dict):
        raise TypeError(f"Error! {context} should be a dictionary.")
    step_id = step_dict.get('id')
    if not isinstance(step_id, str) or step_id == '':
        raise ValueError(
            'Error! Each step dictionary must contain a non-empty string id: tag.')
    return step_id


def get_subkeys(steps_keys: list[str]) -> list[str]:
    """This function determines which step keys are associated with subworkflows.\n
    This is critical for the control flow in many areas of the compiler.

    Args:
        steps_keys (list[str]): All of the step keys for the current workflow.

    Returns:
        list[str]: The list of step keys associated with subworkflows of the current workflow.
    """
    return [key for key in steps_keys if key and key.endswith('.wic')]


def flatten(lists: list[list[Any]]) -> list[Any]:
    """Concatenates a list of lists into a single list.

    Args:
        lists (list[list[Any]]): A list of lists

    Returns:
        list[Any]: A single list
    """
    return [x for lst in lists for x in lst]


def recursively_delete_dict_key(key: str, obj: Any) -> Any:
    """Recursively deletes any dict entries with the given key.

    Args:
        key (str): The key to be deleted
        obj (Any): The object from which to delete key.

    Returns:
        Any: The original dict with the given key recursively deleted.
    """
    if isinstance(obj, list):
        return [recursively_delete_dict_key(key, x) for x in obj]
    if isinstance(obj, dict):
        new_dict = {}
        for key_ in obj.keys():
            if not key_ == key:  # i.e. effectively delete key
                new_dict[key_] = recursively_delete_dict_key(key, obj[key_])
        return new_dict
    return obj


def parse_provenance_output_files(output_json: Json) -> list[tuple[str, str, str]]:
    """Parses the primary workflow provenance JSON object.

    Args:
        output_json (Json): The JSON results object, containing the metadata for all output files.

    Returns:
        list[tuple[str, str, str]]: A List of (location, parentdirs, basename) for each output file.
    """
    files = []
    for namespaced_output_name, obj in output_json.items():
        files.append(parse_provenance_output_files_(
            obj, namespaced_output_name))
    return [y for x in files for y in x]


def parse_provenance_output_files_(obj: Any, parentdirs: str) -> list[tuple[str, str, str]]:
    """Parses the primary workflow provenance JSON object.

    Args:
        obj (Any): The provenance object or one of its recursive sub-objects.
        parentdirs (str): The directory associated with obj.

    Returns:
        list[tuple[str, str, str]]: A List of (location, parentdirs, basename) for each output file.
    """
    if isinstance(obj, dict):
        if obj.get('class', '') == 'File':
            # This basename is a file name
            return [(str(obj['location']), parentdirs, str(obj['basename']))]
        if obj.get('class', '') == 'Directory':
            # This basename is a directory name
            subdir = parentdirs + '/' + obj['basename']
            return parse_provenance_output_files_(obj['listing'], subdir)
    if isinstance(obj, list):
        files = []
        for o in obj:
            files.append(parse_provenance_output_files_(o, parentdirs))
        # Should we flatten?? This will lose the structure of 2D (and higher) array outputs.
        return [y for x in files for y in x]
    return []


def convert_args_dict_to_args_list(
    args_dict: dict[str, Any],
    boolean_flags: set[str] | None = None,
) -> list[str]:
    """Convert an argument dictionary into CLI-style tokens.

    Args:
        args_dict: A dictionary containing args and values
        boolean_flags: Keys that should be emitted as store-true CLI flags.

    Returns:
        list[str]: A syntactically correct list of arguments (CLI flags) and values
    """
    args_list: list[str] = []
    boolean_flags = set(boolean_flags or ())
    for arg_name, arg_value in args_dict.items():
        flag = '--' + arg_name
        if arg_name in boolean_flags:
            normalized = str(arg_value).strip().lower()
            if normalized in {'1', 'true', 'yes', 'on'}:
                args_list.append(flag)
            elif normalized not in {'0', 'false', 'no', 'off', ''}:
                args_list += [flag, str(arg_value)]
            continue
        args_list += [flag, str(arg_value)]
    return args_list


def is_valid_url(url: str) -> bool:
    """A simple utility that tells if the string is a valid url

    Args:
        url(str): A string that is supposed to be an URL

    Returns:
        bool: True if it is an URL
    """
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ('http', 'https')
    except ValueError:
        return False
