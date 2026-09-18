import copy
from typing import Any
import yaml

from . import utils
from .wic_types import Yaml


def validate_out_tag(out_vals: Any) -> None:
    """Validate the structure of a step `out:` tag.

    Args:
        out_vals (Any): Candidate `out:` payload.

    Raises:
        ValueError: If `out:` is not a list of strings and/or single-key dictionaries.
    """
    if not isinstance(out_vals, list):
        raise ValueError('Error! The `out` tag should be a list.')

    for out_val in out_vals:
        if isinstance(out_val, str):
            continue
        if isinstance(out_val, dict):
            keys = list(out_val.keys())
            if len(keys) != 1 or not isinstance(keys[0], str) or keys[0] == '':
                raise ValueError(
                    'Error! There should only be one non-empty string anchor per out: list entry!')
            continue
        raise ValueError(
            'Error! Each out: list entry should be a string or a single-key dictionary.')


def require_string_out_keys(out_vals: Any) -> list[str]:
    """Return validated string output names from an `out:` tag.

    Args:
        out_vals (Any): Candidate `out:` payload.

    Raises:
        ValueError: If any `out:` list entries are not strings.

    Returns:
        list[str]: Validated output names.
    """
    validate_out_tag(out_vals)
    out_keys = [out_val for out_val in out_vals if isinstance(out_val, str)]
    if len(out_keys) != len(out_vals):
        raise ValueError(
            'Error! Each out: list entry should resolve to a string output name before workflow compilation.')
    return out_keys


def maybe_add_requirements(yaml_tree: Yaml, steps_keys: list[str],
                           wic_steps: Yaml, subkeys: list[str]) -> None:
    """Adds any necessary CWL requirements

    Args:
        yaml_tree (Yaml): A tuple of name and yml AST
        steps_keys (list[str]): The name of each step in the current CWL workflow
        wic_steps (Yaml): The metadata associated with the workflow steps
        subkeys (list[str]): The keys associated with subworkflows
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
            stepinp = ['StepInputExpressionRequirement',
                       'InlineJavascriptRequirement']

    if subkeys:
        subwork = ['SubworkflowFeatureRequirement']

    reqs = subwork + scatter + stepinp + jsreq
    if reqs:
        # sorted(), not list(): a plain `set` iterates in hash order, so the
        # emitted `requirements:` key order would depend on PYTHONHASHSEED.
        # See tests/core/test_canonical_emission.py.
        reqsdict: dict[str, dict] = {r: {} for r in sorted(set(reqs))}
        # NOTE: A bare `requirements:` parses to None, so check the value, not the key.
        if isinstance(yaml_tree.get('requirements'), dict):
            yaml_tree['requirements'].update(reqsdict)
        else:
            yaml_tree['requirements'] = reqsdict


def add_yamldict_keyval_in(steps_i: Yaml, step_key: str, keyval: Yaml) -> Yaml:
    """Convenience function used to (mutably) add output keys to a step.

    Args:
        steps_i (Yaml): A partially-completed Yaml dict representing a step in a CWL workflow
        step_key (str): The name of the step in a CWL workflow
        strs (list[str]): The output keys to be added to the step's out: list

    Returns:
        Yaml: The step with the given output keys merged into its out: list.
    """
    # Compiler and inference call sites pass a single step dictionary here.
    # If that step dict is temporarily empty, preserve the single-step shape by
    # synthesizing an explicit `id` instead of the legacy `{step_key: {...}}`
    # mapping form.
    # TODO: Check whether we can just use deepmerge.merge()
    if steps_i:
        if 'in' in steps_i:
            new_keys = {**steps_i['in'], **keyval}
            new_keyvals = {k: (new_keys if k == 'in' else v) for k, v in steps_i.items()}
        else:
            new_keys = keyval
            new_keyvals = {**steps_i, 'in': new_keys}
        steps_i.update(new_keyvals)
    else:
        steps_i = {'id': step_key, 'in': keyval}
    return steps_i


def add_yamldict_keyval_out(steps_i: Yaml, step_key: str, strs: list[str]) -> Yaml:
    """Convenience function used to (mutably) merge two Yaml dicts.

    Args:
        steps_i (Yaml): A partially-completed Yaml dict representing a step in a CWL workflow
        step_key (str): The name of the step in a CWL workflow
        keyval (Yaml): A Yaml dict with additional details to be merged into the first Yaml dict

    Returns:
        Yaml: The first Yaml dict with the second Yaml dict merged into it.
    """
    # See add_yamldict_keyval_in(): keep the returned value in single-step form
    # even when `steps_i` is empty.
    # TODO: Check whether we can just use deepmerge.merge()
    if steps_i:
        if 'out' in steps_i:
            new_strs = require_string_out_keys(steps_i['out']) + strs
            # sorted(), not list(): a plain `set` iterates in hash order, so a
            # step's emitted `out:` list order would depend on PYTHONHASHSEED.
            # See tests/core/test_canonical_emission.py.
            new_strs = sorted(set(new_strs))
            new_keyvals = {k: (new_strs if k == 'out' else v) for k, v in steps_i.items()}
        else:
            new_keyvals = {**steps_i, 'out': strs}
        steps_i.update(new_keyvals)
    else:
        steps_i = {'id': step_key, 'out': strs}
    return steps_i


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


def copy_cwl_input_output_dict(io_dict: dict, remove_qmark: bool = False) -> dict:
    """Copies the type, format, label, and doc entries. Does NOT copy inputBinding and outputBinding.

    Args:
        io_dict (dict): A dictionary
        remove_qmark (bool): Determines whether to remove question marks and thus make optional types required

    Returns:
        dict: A copy of the dictionary.
    """
    io_type = io_dict['type']
    if isinstance(io_type, str) and remove_qmark:
        # Providing optional arguments makes them required
        io_type = io_type.replace('?', '')
    new_dict = {'type': canonicalize_type(io_type)}
    for key in ['format', 'label', 'doc']:
        if key in io_dict:
            new_dict[key] = io_dict[key]  # copy.deepcopy() ?
    return new_dict
