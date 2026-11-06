"""The equivalence relation, and proof that it can tell things apart.

`equivalent` is the definition of "these two compilations mean the same
thing". Everything downstream — partition independence here, differential
equivalence across a pipeline migration — is this relation with a different transformation in
front of it, so a bug here is silent everywhere at once.

That is why this file leads with discrimination rather than with agreement.
A relation that returns None unconditionally satisfies reflexivity and the
lattice law and makes every property that depends on it green and worthless;
only a case built so that a too-permissive relation is *observably* wrong can
see that. Inverse pairs and
reflexive laws test agreement, not correctness.
"""
import copy
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sophios.wic_types import Yaml

from .cases import cases
from .equivalence import Divergence, Strength, equivalent
from .hermetic import ORACLE
from .synthetic_tools import STEMS, inputs_of, outputs_of


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('equivalence'))
def test_equivalence_case(case: dict[str, Any]) -> None:
    """`equivalent(left, right, strength)` for each strength `verdicts` names (`all` names every one) is None for
    null, a divergence for `differs`, a `TypeError` for `TypeError`, or a divergence at `path` that reports `left`
    and `right`. A `right` left out is `left`; the case's `why` names the defect it was built to see.
    """
    left, right = case['left'], case.get('right', case['left'])
    for name, verdict in case['verdicts'].items():
        for strength in Strength if name == 'all' else [Strength[name]]:
            if verdict == 'TypeError':
                with pytest.raises(TypeError):
                    equivalent(left, right, strength)
                continue
            found = equivalent(left, right, strength)
            assert (found is None) is (verdict is None), f'{strength.name}: {found}'
            if isinstance(verdict, dict):
                assert found is not None and found.path == verdict['path'], found
                for side in set(verdict) & {'left', 'right'}:
                    assert getattr(found, side) == verdict[side] and repr(verdict[side]) in str(found), found


@st.composite
def compiled_documents(draw: st.DrawFn) -> Yaml:
    """A document shaped like emitted CWL: namespaced ids, ports, requirements.

    Deliberately does *not* invoke the compiler. This file tests the relation;
    routing its inputs through `compile_hermetic_cwl` would make a relation bug
    and a compiler bug indistinguishable, and would make every failure here a
    question about which of the two moved. The shape is copied from what the
    compiler actually emits (`{yaml_stem}__step__{i}__{step_key}` step ids,
    `{step_id}___{port}` workflow-level port names, `run:` as
    `{step_id}/{stem}.cwl`), verified against a hermetic compilation, not
    invented.
    """
    prefix = draw(st.sampled_from(('oracle', 'wf', 'root')))
    tools = draw(st.lists(st.sampled_from(STEMS), min_size=1, max_size=4))
    steps: list[Yaml] = []
    inputs: Yaml = {}
    outputs: Yaml = {}
    produced: list[str] = []
    for index, tool in enumerate(tools, start=1):
        step_id = f'{prefix}__step__{index}__{tool}'
        bindings: Yaml = {}
        for name in sorted(inputs_of(tool)):
            # `bool(...)` around the left operand: mypy otherwise uses it as
            # the expected-argument context for the generic `DrawFn.__call__`
            # on the right. Same reason as `ast_strategies._step`.
            if bool(produced) and draw(st.booleans()):
                bindings[name] = {'source': draw(st.sampled_from(produced))}
            else:
                port = f'{step_id}___{name}'
                inputs[port] = {'type': 'string'}
                bindings[name] = {'source': port}
        steps.append({'id': step_id, 'in': bindings, 'run': f'{step_id}/{tool}.cwl',
                      'out': sorted(outputs_of(tool))})
        for out_name in sorted(outputs_of(tool)):
            outputs[f'{step_id}___{out_name}'] = {'type': 'File',
                                                  'outputSource': f'{step_id}/{out_name}'}
            produced.append(f'{step_id}/{out_name}')
    requirements: Yaml = {name: {} for name in draw(st.lists(
        st.sampled_from(('ScatterFeatureRequirement', 'SubworkflowFeatureRequirement',
                         'InlineJavascriptRequirement')), unique=True, max_size=3))}
    return {'class': 'Workflow', 'steps': steps, 'inputs': inputs,
            'outputs': outputs, 'requirements': requirements}


def _rewrite_run_paths(document: Yaml) -> Yaml:
    """The rewrite UP_TO_EMBEDDING forgives: move every document somewhere else.

    Models where the compiler writes a process: the file name in `run:`
    depends on what else the tree holds (a stem another process took is
    numbered). Nothing but the path changes, which is exactly the claim
    UP_TO_EMBEDDING makes.
    """
    moved = copy.deepcopy(document)
    for step in moved['steps']:
        step['run'] = 'flat/' + str(step['run']).rsplit('/', maxsplit=1)[-1]
    return moved


def _rewrite_namespace(document: Yaml, prefix: str = 'renamed') -> Yaml:
    """The rewrite UP_TO_RENAMING forgives: re-root the whole namespace.

    Every namespaced name in an emitted document begins with the yaml stem of
    the document it came from, so lifting a workflow into (or out of) a
    subworkflow rewrites every step id and every workflow-level port name and
    moves no edge. Applied as a string substitution over the whole document
    precisely because it must hit `steps[].id`, `steps[].in[].source`,
    `inputs`/`outputs` keys and `outputSource` alike — a renaming that missed
    one of those would produce a document with dangling references, and the
    property below would then be passing for the wrong reason.
    """
    old = str(document['steps'][0]['id']).split('__step__', maxsplit=1)[0]
    renamed: Yaml = _substitute(document, f'{old}__step__', f'{prefix}__step__')
    return renamed


def _substitute(node: Any, old: str, new: str) -> Any:
    """Replace `old` with `new` in every string, in keys and in values."""
    if isinstance(node, dict):
        return {_substitute(k, old, new): _substitute(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, old, new) for v in node]
    return node.replace(old, new) if isinstance(node, str) else node


def _rewrite_drop_a_step(document: Yaml) -> Yaml:
    """Not a rewrite the relation forgives — the control for the three above."""
    dropped = copy.deepcopy(document)
    dropped['steps'] = dropped['steps'][:-1]
    return dropped


#: The rewrite at each lattice level, paired with the strongest strength that
#: must accept it. `None` is the control: a rewrite no strength may accept.
#: Keyed by level rather than written inline so `test_each_rewrite_lands_at_
#: exactly_the_strength_it_claims` can check both directions at once — that the
#: named strength accepts, and that the next one up rejects.
REWRITES: Final[tuple[tuple[str, Any, Strength | None], ...]] = (
    ('identity', copy.deepcopy, Strength.IDENTICAL),
    ('run paths moved', _rewrite_run_paths, Strength.UP_TO_EMBEDDING),
    ('namespace re-rooted', _rewrite_namespace, Strength.UP_TO_RENAMING),
    ('a step dropped', _rewrite_drop_a_step, None),
)


@st.composite
def document_rewrites(draw: st.DrawFn) -> tuple[str, Yaml, Yaml, Strength | None]:
    """A compiled-shaped document and a rewrite of it, with the level it claims."""
    document = draw(compiled_documents())
    # Every rewrite applies to every document, the control included. Withholding
    # the control from one-step documents looks right -- dropping their only step
    # seems to degenerate into `[] vs []` -- and is not: the workflow-level
    # `inputs` and `outputs` the step produced are retained, and the graph goes
    # from one node to none, so the pair is rejected for real reasons at all
    # three strengths.
    name, rewrite, level = draw(st.sampled_from(REWRITES))
    return name, document, rewrite(document), level


@pytest.mark.fast
@given(document_rewrites())
@ORACLE
def test_each_rewrite_lands_at_exactly_the_strength_it_claims(
        case: tuple[str, Yaml, Yaml, Strength | None]) -> None:
    """Each strength accepts its own rewrite and the one above it does not.

    It pins the exact set each strength accepts, so each strength is *strictly*
    weaker than the one above it. Checked in both directions on purpose —
    "accepts" alone is satisfied by a relation that accepts everything, and
    "rejects" alone by one that rejects everything.
    """
    name, left, right, level = case
    for strength in Strength:
        found = equivalent(left, right, strength)
        if level is not None and strength <= level:
            assert found is None, f'{strength.name} rejected "{name}", which it must accept:\n{found}'
        else:
            assert found is not None, f'{strength.name} accepted "{name}", which it must reject'


@pytest.mark.fast
def test_every_rewrite_is_actually_drawn() -> None:
    """The companion for the property above.

    It quantifies over `document_rewrites()` and is *weaker* in
    proportion to how many rewrites the strategy stops producing — a
    `sampled_from` that only ever yielded `identity` would leave them asserting
    reflexivity and nothing else, silently. Same argument as construct coverage for the
    document generator, at the same bounded sample.
    """
    seen: set[str] = set()

    @ORACLE
    @given(document_rewrites())
    def _collect(case: tuple[str, Yaml, Yaml, Strength | None]) -> None:
        seen.add(case[0])

    _collect()  # pylint: disable=no-value-for-parameter  # @given supplies `case`
    missing = {name for name, _, _ in REWRITES} - seen
    assert not missing, f'rewrites never drawn, so nothing above tested them: {sorted(missing)}'


@pytest.mark.fast
def test_a_divergence_is_frozen() -> None:
    """A report collected into a list must not be editable afterwards."""
    found = Divergence(Strength.IDENTICAL, '.x', 1, 2)
    with pytest.raises((AttributeError, TypeError)):
        found.path = '.y'  # type: ignore[misc]
