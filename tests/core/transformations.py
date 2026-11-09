"""Rewrites that must not change what a workflow means, each with the strength it preserves and why (a field: it is
the claim the property checks). Not here: reordering steps, since inference takes the most recent match."""
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, cast

import yaml
from hypothesis.strategies import SearchStrategy

from sophios.utils_yaml import Key
from sophios.wic_types import Yaml

from .ast_strategies import partitionings
from .equivalence import Strength
from .hermetic import subworkflow_step


@dataclass(frozen=True, slots=True)
class Transformation:
    """A rewrite of a `.wic` document, and the claim it makes about it."""

    name: str
    #: The strongest `Strength` a compilation of `apply(w)` shares with one of `w`.
    preserves: Strength
    apply: Callable[[Yaml], Yaml]
    #: Why the claim is true: the claim the property checks, so a field.
    rationale: str


def _text_roundtrip(document: Yaml) -> Yaml:
    """`safe_dump` then `safe_load`, as a user writes a file and compiles it; `sort_keys=False`, since sorting reorders
    keys and IDENTICAL compares key order."""
    return cast(Yaml, yaml.safe_load(yaml.safe_dump(document, sort_keys=False)))


def _bare_sources(inputs: Yaml, name: str) -> list[tuple[Any, Any]]:
    """The bare-name references `inputs[name]` spells, as (container, key) slots: the string, or each string in a
    wic_raw_cwl record's `source`."""
    value = inputs[name]
    if isinstance(value, str):
        return [(inputs, name)]
    record = value.get(Key.RAW_CWL) if isinstance(value, dict) else None
    if not isinstance(record, dict):
        return []
    source = record.get('source')
    if isinstance(source, str):
        return [(record, 'source')]
    if isinstance(source, list):
        return [(source, index) for index, item in enumerate(source) if isinstance(item, str)]
    return []


def _declared_input_references(document: Yaml, steps: list[Yaml]) -> set[str]:
    """The declared `inputs:` names `steps` reference by bare name."""
    declared = document.get('inputs')
    if not declared:
        return set()
    referenced: set[str] = set()
    for step in steps:
        inputs = step.get('in', {})
        for name in inputs:
            for container, key in _bare_sources(inputs, name):
                if container[key] in declared:
                    referenced.add(container[key])
    return referenced


def _rewrite_workflow_input_references(steps: list[Yaml], formals: dict[str, str]) -> None:
    """Rename bare workflow-input references in the new child scope; test-side, so a broken inverse can show."""
    for step in steps:
        candidates = [step.get('in')]
        parentargs = step.get('parentargs')
        if isinstance(parentargs, dict):
            candidates.append(parentargs.get('in'))
        for inputs in candidates:
            if not isinstance(inputs, dict):
                continue
            for name in inputs:
                for container, key in _bare_sources(inputs, name):
                    if container[key] in formals:
                        container[key] = formals[container[key]]


def _subtree_for(document: Yaml, steps: list[Yaml], formals: dict[str, str]) -> Yaml:
    """A subworkflow body of `steps`, redeclaring the top-level inputs they name bare: a bare name resolves against the
    enclosing workflow's own `inputs:` only."""
    declared = document.get('inputs') or {}
    _rewrite_workflow_input_references(steps, formals)
    subtree: Yaml = {'steps': steps}
    if formals:
        subtree['inputs'] = {formal: copy.deepcopy(declared[actual])
                             for actual, formal in formals.items()}
    return subtree


def _wrap_steps(document: Yaml, groups: list[list[Yaml]], stems: list[str]) -> Yaml:
    """`document`'s steps replaced by one subworkflow step per group, each wired through `parentargs['in']` (compilation
    drops an `in:` on the step) to the inputs its group names: left unwired, inference may feed it from a sibling."""
    # `inputs:` stays outside too: the compiler emits a document's inputs whether used or not.
    outer = {key: value for key, value in document.items() if key != 'steps'}
    new_steps = []
    for stem, steps in zip(stems, groups):
        referenced = _declared_input_references(document, steps)
        formals = {actual: f'{Path(stem).stem}_input_{actual}' for actual in sorted(referenced)}
        wrapper = subworkflow_step(stem, _subtree_for(document, steps, formals))
        if referenced:
            wrapper['parentargs']['in'] = {formal: actual for actual, formal in formals.items()}
        new_steps.append(wrapper)
    outer['steps'] = new_steps
    return outer


def _rename_workflow(document: Yaml) -> Yaml:
    """Wrap every step in one subworkflow `renamed.wic`: the root's stem is outside the document, so nesting is how a
    group of steps gets another first namespace segment."""
    return _wrap_steps(document, [copy.deepcopy(document.get('steps', []))], ['renamed.wic'])


TRANSFORMATIONS: Final[tuple[Transformation, ...]] = (
    # A copy, not the object: the compiler mutates its input, so a second compile would see what the first left.
    Transformation(
        name='identity', preserves=Strength.IDENTICAL, apply=copy.deepcopy,
        rationale=(
            'Compiling the same input twice must give the same answer. Trivial to state and not '
            'trivial to satisfy: the compiler mutates yaml_tree, inputs_workflow, '
            'vars_workflow_output_internal, the graph, and the tools dict, so a second compilation '
            'in the same process is a real test of whether any of that leaks.')),
    Transformation(
        name='text_roundtrip', preserves=Strength.IDENTICAL, apply=_text_roundtrip,
        rationale=(
            'yaml.safe_dump then yaml.safe_load. Serialising a workflow and reading it '
            'back is what every user does between writing a file and compiling it.')),
    Transformation(
        name='rename_workflow', preserves=Strength.UP_TO_RENAMING, apply=_rename_workflow,
        rationale=(
            "The workflow's stem is the first segment of every namespace. Changing it must move no "
            "edge.")),
)


def split(cuts: tuple[tuple[int, ...], ...]) -> Transformation:
    """A `split` for one drawn partitioning: each group, one step included, becomes its own subworkflow."""
    # Boundaries, not groups: `apply` rebuilds groups for the length of the document it gets.
    points = tuple(group[0] for group in cuts[1:])

    def apply(document: Yaml) -> Yaml:
        steps: list[Yaml] = document.get('steps', [])
        total = len(steps)
        bounds = sorted({point for point in points if 0 < point < total})
        edges = [0, *bounds, total]
        groups = [tuple(range(a, b)) for a, b in zip(edges, edges[1:])] or [tuple(range(total))]
        step_groups = [[copy.deepcopy(steps[j]) for j in group] for group in groups]
        stems = [f'part{i}.wic' for i in range(len(step_groups))]
        return _wrap_steps(document, step_groups, stems)

    return Transformation(
        name='split', preserves=Strength.UP_TO_RENAMING, apply=apply,
        rationale=(
            'The first design principle. A subworkflow is the steps written in one file; grouping '
            'them differently renames every port and moves no edge.'))


#: The widest document `workflows()` draws (4 tool steps and a subworkflow); `apply` clamps cuts, so generous will do.
_SPLIT_STEP_BOUND: Final = 5


def split_transformations() -> SearchStrategy[Transformation]:
    """A freshly drawn `split`. The property parametrises over the kinds instead of drawing one: Hypothesis favours
    simple draws, so `identity` took half the budget and the thinnest rewrite about one example in fifty."""
    # pylint cannot see through `@st.composite`: it reads partitionings() as its element type, not a strategy.
    return partitionings(_SPLIT_STEP_BOUND).map(split)  # pylint: disable=no-member
