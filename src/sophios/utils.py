from typing import Any

from .wic_types import Yaml


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
