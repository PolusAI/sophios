from urllib.parse import urlparse
from typing import Any

from .wic_types import Yaml


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


def parse_provenance_output_files(obj: Any, parentdirs: str) -> list[tuple[str, str, str]]:
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
            return parse_provenance_output_files(obj['listing'], subdir)
    if isinstance(obj, list):
        files = []
        for o in obj:
            files.append(parse_provenance_output_files(o, parentdirs))
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
