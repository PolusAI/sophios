import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mergedeep import merge, Strategy

from . import utils
from .wic_types import Namespaces, Yaml, YamlTree, StepId

# NOTE: AST = Abstract Syntax Tree

# TODO: Check for inline-ing subworkflows more than once and, if there are not
# any modifications from any parent dsl args, use yaml anchors and aliases.
# That way, we should be able to serialize back to disk without duplication.


def _call_arguments(step: Yaml, wic: Yaml, index: int, step_key: str) -> Yaml | None:
    """The effective arguments on one subworkflow invocation.

    `compile_workflow_once` merges the authored call with `wic: steps:`
    overrides after compiling the child. Inlining runs earlier, so it must make
    the same merge before deciding whether removing the call boundary is safe.
    """
    parentargs = step.get('parentargs', {})
    metadata_steps = wic.get('steps') or {}
    if not isinstance(parentargs, dict) or not isinstance(metadata_steps, dict):
        return None
    metadata = metadata_steps.get(f'({index + 1}, {step_key})', {})
    if not isinstance(metadata, dict):
        return None
    metadata = copy.deepcopy(metadata)
    metadata.pop('wic', None)
    merged: Yaml = merge(copy.deepcopy(parentargs), metadata,
                         strategy=Strategy.TYPESAFE_REPLACE)
    return merged


def _replace_input_references(inputs: Any, bindings: Yaml) -> None:
    """Replace exact bare formal references in one input mapping.

    The language has four input forms. Only a bare string is a workflow-input
    reference; aliases, literals, and raw CWL are mappings and must remain
    opaque. Looking through those mappings would turn data into syntax.
    """
    if not isinstance(inputs, dict):
        return
    for name, value in inputs.items():
        if isinstance(value, str) and value in bindings:
            inputs[name] = copy.deepcopy(bindings[value])


def _step_metadata(subtree: Yaml, index: int, step_key: str) -> Yaml | None:
    """Return mutable `wic: steps:` metadata for an immediate child step."""
    wic = subtree.get('wic', {})
    steps = wic.get('steps', {}) if isinstance(wic, dict) else {}
    metadata = steps.get(f'({index + 1}, {step_key})') if isinstance(steps, dict) else None
    return metadata if isinstance(metadata, dict) else None


def _bind_inputs(subtree: Yaml, bindings: Yaml) -> None:
    """Discharge a complete interface without crossing a nested formal scope."""
    for index, step in enumerate(subtree['steps']):
        step_key = utils.require_step_id(step)
        _replace_input_references(step.get('in'), bindings)
        parentargs = step.get('parentargs')
        if isinstance(parentargs, dict):
            _replace_input_references(parentargs.get('in'), bindings)
        metadata = _step_metadata(subtree, index, step_key)
        if metadata is not None:
            _replace_input_references(metadata.get('in'), bindings)
        if 'subtree' not in step or not isinstance(parentargs, dict):
            continue

        # The compiler supplies an omitted nested-workflow argument as the
        # same-named bare reference. Materialize that implicit capture before
        # removing this scope, or a renamed outer actual cannot reach it.
        nested_inputs = step['subtree'].get('inputs', {})
        wic = subtree.get('wic', {})
        nested_call = _call_arguments(step, wic if isinstance(wic, dict) else {}, index, step_key)
        effective_inputs = nested_call.get('in', {}) if isinstance(nested_call, dict) else {}
        if not isinstance(nested_inputs, dict) or not isinstance(effective_inputs, dict):
            continue
        implicit = {name: copy.deepcopy(bindings[name])
                    for name in nested_inputs
                    if name in bindings and name not in effective_inputs}
        if implicit:
            parent_inputs = parentargs.setdefault('in', {})
            if isinstance(parent_inputs, dict):
                parent_inputs.update(implicit)


def _output_target(subtree: Yaml, workflow_stem: str, output_name: str) -> tuple[int, str] | None:
    """Resolve one declared workflow output to an immediate child output."""
    outputs = subtree.get('outputs', {})
    output = outputs.get(output_name) if isinstance(outputs, dict) else None
    source = output.get('outputSource') if isinstance(output, dict) else None
    if not isinstance(source, str) or source.count('/') != 1:
        return None
    producer, port = source.split('/')
    try:
        stem, index, step_key = utils.parse_step_name_str(producer)
    except ValueError:
        return None
    steps = subtree.get('steps')
    if stem != workflow_stem or not isinstance(steps, list) or not 0 <= index < len(steps):
        return None
    if utils.require_step_id(steps[index]) != step_key or not port:
        return None
    return index, port


def _anchor_is_compatible(outputs: Any, port: str, anchor: str) -> bool:
    """Whether one edge definition can occupy an immediate step output."""
    if not isinstance(outputs, list):
        return False
    matches = [value for value in outputs
               if value == port or (isinstance(value, dict) and list(value) == [port])]
    if len(matches) > 1:
        return False
    if not matches:
        return True
    return bool(matches[0] == port or matches[0] == {port: {'wic_anchor': anchor}})


def _step_outputs(subtree: Yaml, index: int) -> Any:
    """Read the output list that will govern one immediate child step."""
    step = subtree['steps'][index]
    if 'subtree' not in step:
        return step.get('out', [])
    step_key = utils.require_step_id(step)
    metadata = _step_metadata(subtree, index, step_key)
    parentargs = step.get('parentargs')
    if not isinstance(parentargs, dict):
        return None
    return metadata.get('out') if metadata is not None and 'out' in metadata else parentargs.get('out', [])


@dataclass(frozen=True, slots=True)
class _OutputMove:
    """One wrapper edge definition and its proven destination."""

    index: int
    port: str
    anchor: str


@dataclass(frozen=True, slots=True)
class _InlinePlan:
    """Everything needed to remove one workflow-call boundary."""

    bindings: Yaml
    output_moves: tuple[_OutputMove, ...]


def _output_moves(subtree: Yaml, workflow_stem: str, outputs: Any) -> tuple[_OutputMove, ...] | None:
    """Plan wrapper edge-definition moves without mutating the candidate."""
    if outputs is None:
        return ()
    if not isinstance(outputs, list):
        return None
    moves: list[_OutputMove] = []
    planned: dict[tuple[int, str], str] = {}
    for output in outputs:
        if isinstance(output, str):
            continue
        if not isinstance(output, dict) or len(output) != 1:
            return None
        output_name, definition = next(iter(output.items()))
        anchor = definition.get('wic_anchor') if isinstance(definition, dict) else None
        if not isinstance(output_name, str) or not isinstance(anchor, str) or not anchor:
            return None
        target = _output_target(subtree, workflow_stem, output_name)
        if target is None:
            return None
        index, port = target
        if not _anchor_is_compatible(_step_outputs(subtree, index), port, anchor):
            return None
        prior = planned.get(target)
        if prior is not None:
            if prior != anchor:
                return None
            continue
        planned[target] = anchor
        moves.append(_OutputMove(index, port, anchor))
    return tuple(moves)


def _add_output_anchor(subtree: Yaml, move: _OutputMove) -> None:
    """Apply one output move already validated by `_output_moves`."""
    step = subtree['steps'][move.index]
    if 'subtree' not in step:
        outputs = step.setdefault('out', [])
    else:
        step_key = utils.require_step_id(step)
        metadata = _step_metadata(subtree, move.index, step_key)
        parentargs = step.get('parentargs')
        assert isinstance(parentargs, dict)
        destination = metadata if metadata is not None and 'out' in metadata else parentargs
        outputs = destination.setdefault('out', [])
    matches = [index for index, value in enumerate(outputs)
               if value == move.port or (isinstance(value, dict) and list(value) == [move.port])]
    anchored = {move.port: {'wic_anchor': move.anchor}}
    if not matches:
        outputs.append(anchored)
    elif outputs[matches[0]] == move.port:
        outputs[matches[0]] = anchored


def _inline_plan(subtree: Yaml, workflow_stem: str, call: Yaml | None) -> _InlinePlan | None:
    """Prove that one complete call boundary can be removed."""
    declared = subtree.get('inputs', {})
    if not isinstance(declared, dict) or call is None or set(call) - {'in', 'out'}:
        return None
    bindings = call.get('in', {})
    if not isinstance(bindings, dict) or set(bindings) != set(declared):
        return None
    moves = _output_moves(subtree, workflow_stem, call.get('out'))
    return _InlinePlan(bindings, moves) if moves is not None else None


def _inline_body(subtree: Yaml, plan: _InlinePlan) -> Yaml:
    """Build the body described by a validated inline plan.

    Discovery and execution share `_inline_plan`, so the inliner cannot
    advertise a boundary that the rewrite later interprets differently.
    """
    body = copy.deepcopy(subtree)
    _bind_inputs(body, plan.bindings)
    for move in plan.output_moves:
        _add_output_anchor(body, move)
    body.pop('inputs', None)
    return body


def get_inlineable_subworkflows(yaml_tree_tuple: YamlTree,
                                implementation: bool = False,
                                namespaces_init: Namespaces | None = None) -> list[Namespaces]:
    """Traverses a yml AST and finds all subworkflows which can be inlined into their parent workflow.

    Args:
        yaml_tree_tuple (YamlTree): A tuple of name and yml AST
        implementation (bool): True if the immediate parent workflow is a implementation.
        namespaces_init (Namespaces): The initial subworkflow to start the traversal ([] == root)

    Returns:
        list[Namespaces]: The subworkflows which can be inlined into their parent workflows.
    """
    namespaces_init = [] if namespaces_init is None else namespaces_init
    (step_id, yaml_tree) = yaml_tree_tuple
    yaml_name = step_id.stem

    # Check for top-level yml dsl args
    wic = {'wic': yaml_tree.get('wic') or {}}

    if 'implementations' in wic['wic']:
        # Use yaml_name (instead of back_name) and do not append to namespace_init.
        sub_namespaces_list = []
        for stepid, back in wic['wic']['implementations'].items():
            sub_namespaces = get_inlineable_subworkflows(
                YamlTree(stepid, back), implementation=True, namespaces_init=namespaces_init)
            sub_namespaces_list.append(sub_namespaces)
        return utils.flatten(sub_namespaces_list)

    steps: list[Yaml] = yaml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)
    subkeys = utils.get_subkeys(steps_keys)

    # A child's own opt-out is independent of whether its call boundary can be
    # discharged safely; `_inline_plan` proves the latter below.
    inlineable = wic['wic'].get('inlineable', True)
    namespaces = [namespaces_init] if inlineable and namespaces_init != [] and not implementation else []

    for i, step_key in enumerate(steps_keys):
        yaml_stem = Path(yaml_name).stem
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        if step_key in subkeys:
            sub_yml_tree = steps[i]['subtree']

            y_t = YamlTree(StepId(step_key, step_id.plugin_ns), sub_yml_tree)
            sub_namespaces = get_inlineable_subworkflows(
                y_t, implementation=False, namespaces_init=namespaces_init + [step_name_i])
            call = _call_arguments(steps[i], wic['wic'], i, step_key)
            if _inline_plan(sub_yml_tree, Path(step_key).stem, call) is None:
                child_namespace = namespaces_init + [step_name_i]
                sub_namespaces = [namespace for namespace in sub_namespaces
                                  if namespace != child_namespace]
            namespaces += sub_namespaces

    return namespaces


def inline_subworkflow(yaml_tree_tuple: YamlTree, namespaces: Namespaces) -> tuple[YamlTree, int]:
    """Inlines the given subworkflow into its immediate parent workflow.

    Args:
        yaml_tree_tuple (YamlTree): A tuple of name and yml AST
        namespaces (Namespaces): Specifies the path in the yml AST to the subworkflow to be inlined.

    Returns:
        YamlTree: The updated root workflow with the given subworkflow inlined into its immediate parent workflow.
    """
    if namespaces == []:
        return yaml_tree_tuple, 0

    (step_id, yaml_tree) = copy.deepcopy(yaml_tree_tuple)
    yaml_name = step_id.stem

    wic = {'wic': yaml_tree.get('wic') or {}}
    if 'implementations' in wic['wic']:
        if len(namespaces) == 1:  # and namespaces[0] == yaml_name ?
            (back_name_, yaml_tree) = utils.extract_implementation(yaml_tree, wic['wic'], Path(''))
            yaml_tree = {'steps': yaml_tree['steps']}  # Remove wic tag
            return YamlTree(StepId(back_name_, step_id.plugin_ns), yaml_tree), 0  # len_substeps  # TODO: check step_id

        # Pass namespaces through unmodified
        implementations_trees = []
        for stepid, back in wic['wic']['implementations'].items():
            implementation_tree, _len_substeps = inline_subworkflow(YamlTree(stepid, back), namespaces)
            implementations_trees.append(implementation_tree)
        yaml_tree['wic']['implementations'] = dict(implementations_trees)
        return YamlTree(step_id, yaml_tree), 0  # choose len_substeps from which implementation?

    steps: list[Yaml] = yaml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)
    yaml_stem = Path(yaml_name).stem
    step_names = [utils.step_name_str(yaml_stem, i, step_key)
                  for i, step_key in enumerate(steps_keys)]

    if namespaces[0] not in step_names:
        # This should never happen (if namespaces comes from get_inlineable_subworkflows)
        raise ValueError(f'Error! {namespaces[0]} not in {step_names}')

    # TODO: We really need to inline the wic tags as well. This may be complicated
    # because due to overloading we may need to modify parent wic tags.

    (yaml_stem, i, step_key) = utils.parse_step_name_str(namespaces[0])
    sub_yml_tree = steps[i]['subtree']
    call = _call_arguments(steps[i], wic['wic'], i, step_key)
    plan = _inline_plan(sub_yml_tree, Path(step_key).stem, call)

    len_substeps = 0
    if len(namespaces) == 1:
        if plan is None:
            raise ValueError(f'{step_key} has invocation semantics the source inliner cannot preserve')
        steps_inits = steps[:i]  # Exclude step i
        steps_tails = steps[i+1:]  # Exclude step i
        sub_yml_tree = _inline_body(sub_yml_tree, plan)
        # Inline sub-steps.
        sub_steps: list[Yaml] = sub_yml_tree['steps']
        yaml_tree['steps'] = steps_inits + sub_steps + steps_tails
        # Need to re-index both the sub-step numbers as well as the
        # subsequent steps in this workflow? No, except for wic: steps:
        len_substeps = len(sub_steps)

        parent_wic_tag = wic.get('wic', {}).get("steps", {}).get(
            f'({i + 1}, {step_key})', {}).get('wic', {})
        sub_wic_tag = sub_yml_tree.get('wic', {})

        # TODO: need cleaner code to make arbitrary-depth dictionary.
        if 'wic' not in wic:
            wic['wic'] = {}
        if 'steps' not in wic['wic']:
            wic['wic']['steps'] = {}
        if f'({i + 1}, {step_key})' not in wic['wic']['steps']:
            wic['wic']['steps'][f'({i + 1}, {step_key})'] = {}

        # Merge parent into child to support overloading.
        # TODO: Need to sort the steps by index
        wic['wic']['steps'][f'({i + 1}, {step_key})']['wic'] = \
            merge(sub_wic_tag, parent_wic_tag, strategy=Strategy.TYPESAFE_REPLACE)
    else:
        # Strip off one initial namespace
        y_t = YamlTree(StepId(step_key, step_id.plugin_ns), sub_yml_tree)
        (_, sub_yml_tree), len_substeps = inline_subworkflow(y_t, namespaces[1:])
        # TODO: re-index wic: steps: ? We probably should, although
        # inlineing after merging should not affect CWL args.
        # Re-indexing could be tricky w.r.t. overloading.
        # TODO: maintain inference boundaries (once feature is added)
        steps[i] = {'id': step_key, 'subtree': sub_yml_tree,
                    'parentargs': steps[i]['parentargs']}

    yaml_tree['wic'] = inline_subworkflow_wic_tag(wic, namespaces, len_substeps)

    return YamlTree(step_id, yaml_tree), len_substeps


def inline_subworkflow_wic_tag(wic_tag: Yaml, namespaces: Namespaces, len_substeps: int) -> Yaml:
    """Inlines the wic metadata tags associated with the given subworkflow into its immediate parent wic.

    Args:
        wic_tag (Yaml): The wicmetadata tag associated with the given workflow
        namespaces (Namespaces): Specifies the path in the yml AST to the subworkflow to be inlined.
        len_substeps (int): The number of steps in the subworkflow to be inlined.

    Returns:
        Yaml: The updated wic metadata tag with the wic metadata tag associated with the given subworkflow inlined.
    """
    tag_wic: Yaml = wic_tag['wic']

    # Note: the index after parsing is 0-based.
    step_ints_names = [utils.parse_step_name_str(ns)[1:] for ns in namespaces]

    sub_wic_parent = wic_tag  # initialize to the 'root' wic tag
    # Traverse down to the parent node of the subworkflow to the inlined
    for index, step_name in step_ints_names[:-1]:
        sub_wic_parent = sub_wic_parent.get('wic', {}).get('steps', {}).get(f'({index + 1}, {step_name})', {})
        # Note: if any of the intermediate workflows in the path in the AST tree
        # from the current workflow to the subworkflow being inlined is absent in the current
        # wic metadata tag, the inlining won't have any effect on the wic tag of this workflow.
        # Note: When there're other options like 'graphviz' but not 'steps', we can also short
        # circuit and return.
        if 'steps' not in sub_wic_parent.get('wic', {}):
            return tag_wic  # If path does not exist, do nothing and short circuit

    # Then get the wic tag of the subworkflow
    # Note: sub_index is 0-based.
    sub_index, sub_step_name = step_ints_names[-1]
    sub_wic = sub_wic_parent.get('wic', {}).get('steps', {}).get(f'({sub_index + 1}, {sub_step_name})', {})

    # Note: we should not short circuit when the subworkflow being inlined is not used in the
    # current wic tag, since inlining it will affect the indices of sibling steps following it.
    sub_wic_steps_reindexed = utils.reindex_wic_steps(sub_wic.get('wic', {}).get('steps', {}), 1, sub_index)

    # Delete the subworkflow from the parent workflow since it is replaced by its internal steps.
    # This needs to be explicitly done since the key of this subworkflow in the dict is not
    # the same as any of its inlined steps and therefore won't be overwritten by the deep merge.
    if f'({sub_index + 1}, {sub_step_name})' in sub_wic_parent.get('wic', {}).get('steps', {}):
        del sub_wic_parent['wic']['steps'][f'({sub_index + 1}, {sub_step_name})']

    # The inlining is actually a replacement of the target subworkflows by its steps.
    # Therefore, the incrementing count should be len_substeps - 1.
    sub_wic_parent_steps_reindexed = utils.reindex_wic_steps(sub_wic_parent['wic']['steps'],
                                                             sub_index + 1, len_substeps - 1)

    # Merge the wic: steps: dicts and mutably update the parent
    # Merge parent into child to support overloading.
    # TODO: The 'ranksame' in the wic tag of the inlined subworkflow is ignored
    # and not merged for now.
    sub_wic_parent['wic']['steps'] = merge(sub_wic_steps_reindexed, sub_wic_parent_steps_reindexed,
                                           strategy=Strategy.TYPESAFE_REPLACE)

    return tag_wic
