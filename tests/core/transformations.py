"""Rewrites that must not change what a workflow means.

Each carries the strength it preserves and the reason. The rationale is a
field rather than a comment because it is the load-bearing claim: a
transformation on this list asserts that Sophios's semantics are invariant
under it, and that assertion is exactly what the property checks. A
transformation nobody can justify is a test that will one day fail for a good
reason and be "fixed" by weakening the property.

Deliberately NOT on this list, and worth recording because it looks like it
belongs: **reordering independent steps.** Sophios's inference scans backwards
over previous steps (`for j in range(0, i)[::-1]`, src/sophios/inference.py:216)
and takes the most recent match, so step order is part of the meaning of a
workflow, not an accident of how it was written. A "reorder independent steps"
transformation would be a property asserting something false about this
language.

The five, with their strengths, are `identity`, `text_roundtrip`, `split`,
`inline_all` and `rename_workflow`. `split` needs a drawn argument (which
steps go in which file), so it is built by a strategy rather than being a
constant: `TRANSFORMATIONS` holds the other four, and `transformations()`
unions them with a `split` built from `ast_strategies.partitionings`.
"""
import copy
from dataclasses import dataclass
from typing import Callable, Final

import yaml
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sophios.inlineing import get_inlineable_subworkflows, inline_subworkflow
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Namespaces, StepId, Yaml, YamlTree

from .ast_strategies import partitionings
from .equivalence import Strength
from .hermetic import subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


@dataclass(frozen=True, slots=True)
class Transformation:
    """A rewrite of a `.wic` document, and the claim it makes about it."""

    name: str
    #: The strongest `equivalence.Strength` a compilation of `apply(w)` is
    #: claimed to share with a compilation of `w`.
    preserves: Strength
    apply: Callable[[Yaml], Yaml]
    #: Why the claim is true. Not a comment: see the module docstring.
    rationale: str


def _identity(document: Yaml) -> Yaml:
    """A deep copy, not the same object.

    Not `lambda w: w`. Returning the very object the caller handed in would
    make `compile_hermetic(yml, ...)` and `compile_hermetic(rewrite.apply(yml), ...)`
    share one mutable dict, and the compiler mutates its input
    (`yaml_tree`) in place — so a bare identity function would make the second
    compile a test of whatever the first compile left behind, not a test of
    compiling the same input twice. A deep copy is a second, independent input
    with the same value, which is the actual claim.
    """
    copied: Yaml = copy.deepcopy(document)
    return copied


def _text_roundtrip(document: Yaml) -> Yaml:
    """Serialise, then parse through the compiler's own loader.

    This is what every user does between writing a `.wic` file and running
    `sophios --graph`, and it is the mechanism `to_yml` itself relies on
    (`ast_strategies.py`) — so a divergence here would mean the generator's own
    round trip is unsound, not just this rewrite.

    `sort_keys=False`: `yaml.safe_dump`'s default sorts mapping keys
    alphabetically, which is a real reordering — the IDENTICAL strength this
    rewrite claims compares key order (`equivalence.Strength.IDENTICAL`) — and
    would make this rewrite fail its own claim regardless of the compiler,
    for a reason that has nothing to do with what it is testing. Matches the
    convention this suite already uses for a faithful dump
    (`test_hermeticity.py`'s `yaml.safe_dump(_cwl(stem), sort_keys=False)`).
    """
    dumped = yaml.safe_dump(document, sort_keys=False)
    loaded: Yaml = yaml.load(dumped, Loader=wic_loader())
    return loaded


#: How many times `_inline_all` will inline a subworkflow before giving up.
#: Every call strictly reduces the number of `.wic` steps remaining (a
#: subworkflow, once inlined, cannot reappear), and `workflows()` never
#: generates more than one, so this is generous headroom, not a tuned budget —
#: it exists so a defect in `get_inlineable_subworkflows`/`inline_subworkflow`
#: that stopped making progress would raise here instead of hanging the suite.
_MAX_INLINE_PASSES: Final = 20


def _inline_all(document: Yaml) -> Yaml:
    """Inline every subworkflow this document contains, via `sophios.inlineing`.

    Repeats rather than inlining once: `get_inlineable_subworkflows` can name
    more than one namespace, and inlining one shifts the indices of whatever
    is left, so the list is recomputed after each inline instead of consumed
    in one pass.
    """
    tree = YamlTree(StepId('workflow', SYNTHETIC_NS), copy.deepcopy(document))
    for _ in range(_MAX_INLINE_PASSES):
        found: list[Namespaces] = get_inlineable_subworkflows(tree, SYNTHETIC_TOOLS, False, [])
        if not found:
            return tree.yml
        tree, _len_substeps = inline_subworkflow(tree, found[0])
    raise AssertionError(f'_inline_all did not converge in {_MAX_INLINE_PASSES} passes')


def _declared_input_references(document: Yaml, steps: list[Yaml]) -> set[str]:
    """The declared `inputs:` names `steps` reference by bare name."""
    declared = document.get('inputs')
    if not declared:
        return set()
    referenced: set[str] = set()
    for step in steps:
        for value in step.get('in', {}).values():
            source = value.get('source') if isinstance(value, dict) else value
            if isinstance(source, str) and source in declared:
                referenced.add(source)
    return referenced


def _subtree_for(document: Yaml, steps: list[Yaml]) -> Yaml:
    """A subworkflow body carrying `steps`, plus whatever declared top-level
    `inputs:` those steps themselves reference by bare name.

    A step may reference a bare declared name (`UnresolvedName`,
    `ast_strategies.py`), and that resolves against the *immediately
    enclosing* workflow's own `inputs:` mapping (`arg_var_is_input`,
    src/sophios/compiler.py:878) — a subworkflow does not inherit its
    parent's, so the reference needs the declaration repeated here to resolve
    at all. See `_wrap_steps` for how the *outer* level keeps the same
    reference resolvable too.
    """
    declared = document.get('inputs') or {}
    referenced = _declared_input_references(document, steps)
    subtree: Yaml = {'steps': steps}
    if referenced:
        subtree['inputs'] = {name: copy.deepcopy(declared[name]) for name in referenced}
    return subtree


def _wrap_steps(document: Yaml, groups: list[list[Yaml]], stems: list[str]) -> Yaml:
    """One new subworkflow step per group of `document`'s own steps, replacing
    `document['steps']` outright — the shared engine behind `split` and
    `rename_workflow`.

    Each wrapper is wired to whichever of `document`'s own declared `inputs:`
    its own group references, via `parentargs['in']` rather than a plain
    `in:` key on the step dict: `compile_workflow_once` builds a subworkflow
    step's final `in:` by merging `parentargs` over any `wic:`-supplied
    overrides (src/sophios/compiler.py:560-567), discarding whatever the step
    dict already had — checked directly, an `in:` set any other way is simply
    gone by the time the step is compiled. Left unwired instead, the compiler
    auto-fills a missing formal parameter as a same-named bare reference of
    its own (`args_required`, src/sophios/compiler.py:665-671), and *that*
    reference is not the explicit one this function writes: checked directly,
    an unwired wrapper argument can get backward-inferred to a preceding
    sibling group's output instead, whenever their types happen to unify —
    silently rewiring an argument that named a specific declared input in the
    unsplit document to a different producer entirely once split. `split`
    wiring every reference explicitly is what keeps that inference path from
    ever being reached for an argument this function already knows the answer
    to.

    `document`'s own `inputs:` stays in place at the outer level too,
    unconditionally: a referenced name needs it there for the bare reference
    above to resolve, and an unreferenced one is still a real port in an
    unsplit compilation regardless — the compiler copies a document's
    `inputs:` into its compiled `inputs:` regardless of use (checked
    directly) — so dropping either kind would be its own extra divergence.
    """
    outer = {key: value for key, value in document.items() if key != 'steps'}
    new_steps = []
    for stem, steps in zip(stems, groups):
        wrapper = subworkflow_step(stem, _subtree_for(document, steps))
        referenced = _declared_input_references(document, steps)
        if referenced:
            wrapper['parentargs']['in'] = {name: name for name in referenced}
        new_steps.append(wrapper)
    outer['steps'] = new_steps
    return outer


def _rename_workflow(document: Yaml) -> Yaml:
    """Wrap every step in one new subworkflow, under a fixed, different stem.

    The root's own stem is supplied to `compile_hermetic` as the `name`
    argument, outside the `Yaml` this function receives, so there is nothing
    in the document itself that *is* "the root's stem" to edit in place.
    Introducing one level of nesting is the mechanism available at this level
    for giving a group of steps a stem other than the one they would
    otherwise inherit, and it is the same claim: everything under
    `renamed.wic` now has `renamed` as the first segment of its namespace
    instead of the root's name, and that must move no edge.
    """
    return _wrap_steps(document, [copy.deepcopy(document.get('steps', []))], ['renamed.wic'])


TRANSFORMATIONS: Final[tuple[Transformation, ...]] = (
    Transformation(
        name='identity', preserves=Strength.IDENTICAL, apply=_identity,
        rationale=(
            'Compiling the same input twice must give the same answer. Trivial to state and not '
            'trivial to satisfy: the compiler mutates yaml_tree, inputs_workflow, '
            'vars_workflow_output_internal, the graph, and the tools dict, so a second compilation '
            'in the same process is a real test of whether any of that leaks.')),
    Transformation(
        name='text_roundtrip', preserves=Strength.IDENTICAL, apply=_text_roundtrip,
        rationale=(
            'yaml.safe_dump then load through wic_loader. Serialising a workflow and reading it '
            'back is what every user does between writing a file and compiling it.')),
    Transformation(
        name='inline_all', preserves=Strength.UP_TO_RENAMING, apply=_inline_all,
        rationale=(
            "The inverse of split, via sophios.inlineing. Included because it is the direction the "
            "existing corpus test takes, and because a split/inline pair that agreed only with each "
            "other would be an inverse-pair blind spot (#383's P4 lesson).")),
    Transformation(
        name='rename_workflow', preserves=Strength.UP_TO_RENAMING, apply=_rename_workflow,
        rationale=(
            "The workflow's stem is the first segment of every namespace. Changing it must move no "
            "edge.")),
)


def _cut_points(partitioning: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
    """The boundary before every group but the first, as plain indices.

    `partitionings(n)` is shaped for a document with exactly `n` steps, but
    `transformations()` has no document in hand when it builds a `split` — it
    hands out `Transformation`s, and only the property that calls `apply`
    knows how many steps the drawn workflow actually has. Reducing a
    partitioning to its boundary *positions* (rather than its groups) is what
    lets `split.apply` rebuild groups for whatever length it is actually
    given, by clamping and deduplicating these positions against that length.
    """
    return tuple(group[0] for group in partitioning[1:])


def split(cuts: tuple[tuple[int, ...], ...]) -> Transformation:
    """A `split` transformation for one drawn set of cut points.

    Every group, including a group of one, becomes its own subworkflow via
    `hermetic.subworkflow_step` — a subworkflow is the steps written in one
    file, and a one-step file is still a file.
    """
    points = _cut_points(cuts)

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


#: How many steps `partitionings` is asked to cut. `workflows()` generates at
#: most 4 ordinary steps plus one optional subworkflow step (`ast_strategies.documents`),
#: so 5 covers the widest document `test_a_meaning_preserving_rewrite_preserves_meaning`
#: can draw; `split.apply` clamps its cut points to whatever length it is
#: actually given, so this bound only needs to be generous, not exact.
_SPLIT_STEP_BOUND: Final = 5


def transformations() -> SearchStrategy[Transformation]:
    """Every transformation this module knows: the constant four, plus a
    freshly drawn `split`."""
    # pylint cannot see through @st.composite and reads partitionings() as
    # returning its element type rather than a SearchStrategy.
    return st.one_of(st.sampled_from(TRANSFORMATIONS),
                     partitionings(_SPLIT_STEP_BOUND).map(split))  # pylint: disable=no-member
