import re
from typing import Any, Final

#: A terminal control sequence: a colour or cursor code, or a character-set switch, as cwltool and Nextflow print.
_TERMINAL_CODE: Final = re.compile(r'\x1b(?:\[[0-9;]*[A-Za-z]|\([A-Z0-9])')


def strip_terminal_codes(text: str) -> str:
    """`text` without the terminal's control sequences."""
    return _TERMINAL_CODE.sub('', text)


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
