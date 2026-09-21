from typing import Any
import yaml

from . import utils
from .wic_types import Yaml


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


def _require_list_of_dicts(items: list, msg: str) -> None:
    """Raise ValueError (with a yaml dump of items appended) unless every element of items is a dict."""
    if not all(isinstance(elt, dict) for elt in items):
        raise ValueError(f"{msg}\n{yaml.dump(items)}")


def canonicalize_steps_list(steps: Yaml) -> list[Yaml]:
    """Converts the steps: tag (either a List or a Dictionary) into canonical List form."""
    if isinstance(steps, list):
        _require_list_of_dicts(
            steps, 'Error! If steps: tag is a List then all its elements should be Dictionaries!')
        for step in steps:
            utils.require_step_id(step)
        return steps
    if isinstance(steps, dict):
        invalid_keys = [key for key in steps if not isinstance(
            key, str) or key == '']
        if invalid_keys:
            msg = 'Error! If steps: tag is a Dictionary then all its keys should be non-empty strings!'
            raise ValueError(f"{msg}\n{yaml.dump(steps)}")
        items = [(key, {}) if val is None else (key, val)
                 for key, val in steps.items()]
        msg = 'Error! If steps: tag is a Dictionary then all its values should be Dictionaries!'
        _require_list_of_dicts([val for _, val in items], msg)
        return [{'id': key, **val} for key, val in items]
    # steps should either be a list or a dict, but...
    return steps


def remove_id_tags(list_of_dicts_with_id_keys: list) -> dict[str, Yaml]:
    """Converts a List of Dictionaries with `id` tags into a Dictionary keyed on those `id` tags."""
    d_canon = {}
    for d in list_of_dicts_with_id_keys:
        id_tag = utils.require_step_id(d)
        del d['id']
        # NOTE: The order of the steps may not be preserved!
        d_canon[id_tag] = d
    return d_canon


def canonicalize_inputs_dict(inputs: Yaml) -> dict[str, Yaml]:
    """Converts the inputs: tag (either a List or a Dictionary) into canonical Dictionary form."""
    inputs_canon = {}
    if isinstance(inputs, dict):
        for key, val in inputs.items():
            match val:
                case dict():
                    inputs_canon[key] = val
                case str():
                    inputs_canon[key] = {'type': val}  # NOTICE
                case _:
                    msg = ('Error! If inputs: tag is a dictionary, then all its values should be '
                           'either strings (representing types) or dictionaries.')
                    raise ValueError(f"{msg}\n{yaml.dump(inputs)}")
    if isinstance(inputs, list):
        _require_list_of_dicts(
            inputs, 'Error! If inputs: tag is a list then all its elements should be dictionaries!')
        return remove_id_tags(inputs)
    return inputs_canon


def canonicalize_outputs_dict(outputs: Yaml) -> dict[str, Yaml]:
    """Converts the outputs: tag (either a List or a Dictionary) into canonical Dictionary form."""
    outputs_canon: dict[str, Yaml] = {}
    if isinstance(outputs, dict):
        for key, val in outputs.items():
            if isinstance(val, (dict, str)):
                # TODO need to lookup output file mapping for the str (output file) case!
                outputs_canon[key] = val  # type: ignore
            else:
                msg = ('Error! outputs: tag should be a dictionary whose values are either '
                       'strings (representing output files) or dictionaries.')
                raise ValueError(f"{msg}\n{yaml.dump(outputs)}")
    if isinstance(outputs, list):
        _require_list_of_dicts(
            outputs, 'Error! If outputs: tag is a list then all its elements should be dictionaries!')
        return remove_id_tags(outputs)
    return outputs_canon


def desugar_into_canonical_normal_form(cwl: Yaml) -> Yaml:
    """Desugars the inputs:, outputs:, and steps: tags of a CWL AST into their canonical forms."""
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
