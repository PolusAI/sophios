from typing import Any


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
