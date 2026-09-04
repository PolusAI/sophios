"""The equivalence relation, and proof that it can tell things apart.

`equivalent` is the definition of "these two compilations mean the same
thing". Everything downstream — partition independence here, differential
equivalence in Spec 3 — is this relation with a different transformation in
front of it, so a bug here is silent everywhere at once.

That is why this file leads with discrimination rather than with agreement.
A relation that returns None unconditionally satisfies reflexivity and the
lattice law and makes every property that depends on it green and worthless;
only a case built so that a too-permissive relation is *observably* wrong can
see that. Same reasoning as the round-trip lesson from #383: inverse pairs and
reflexive laws test agreement, not correctness.

The same reasoning applies a second time, to the lattice property itself. The
brief's sketch quantifies it over two *independently drawn* documents, which
would have made it vacuous: two independent draws are essentially never
IDENTICAL, so `if equivalent(left, right, stronger) is None` would guard a body
that never runs, and the test would pass for a relation with no implications at
all. `document_rewrites()` draws a document and a *rewrite of it* instead, one
rewrite per lattice level, and `test_each_rewrite_lands_at_exactly_the_strength_
it_claims` pins each level from both sides — so the antecedents demonstrably
fire and each strength is shown to be strictly weaker than the one above it.
"""
import copy
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sophios.wic_types import Yaml

from .equivalence import Divergence, Strength, equivalent
from .hermetic import ORACLE
from .synthetic_tools import STEMS, inputs_of, outputs_of

#: Pairs no strength may call equivalent, with the reason each matters.
#: A relation that accepts any of these is broken in a way that would make
#: Spec 3's differential harness report zero divergences on a rewrite that
#: changed behaviour.
MUST_DIFFER: Final[list[tuple[str, Yaml, Yaml]]] = [
    ('a step is missing',
     {'steps': [{'id': 'a__step__1__mk', 'out': ['file']}]},
     {'steps': []}),
    ('a step names a different tool',
     {'steps': [{'id': 'a__step__1__mk', 'out': ['file']}]},
     {'steps': [{'id': 'a__step__1__xf', 'out': ['file']}]}),
    ('an edge points somewhere else',
     {'steps': [{'id': 'a__step__1__mk', 'in': {'f': 'a__step__1__mk/file'}}]},
     {'steps': [{'id': 'a__step__1__mk', 'in': {'f': 'a__step__2__xf/file'}}]}),
    ('an output disappeared',
     {'steps': [], 'outputs': {'x': {'type': 'File'}}},
     {'steps': [], 'outputs': {}}),
    ('a requirement disappeared',
     {'steps': [], 'requirements': {'ScatterFeatureRequirement': {}}},
     {'steps': [], 'requirements': {}}),
]


@pytest.mark.fast
@pytest.mark.parametrize('why, left, right', MUST_DIFFER, ids=[c[0] for c in MUST_DIFFER])
@pytest.mark.parametrize('strength', list(Strength))
def test_no_strength_accepts_a_real_difference(why: str, left: Yaml,
                                               right: Yaml, strength: Strength) -> None:
    """The weakest relation in the lattice must still reject these."""
    found = equivalent(left, right, strength)
    assert found is not None, f'{strength.name} accepted a pair that differs: {why}'


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

    Models `relative_run_path` — the compiler writes `run:` as either
    `step_name/stem.cwl` or `'___'.join(namespaces + [stem.cwl])`
    (src/sophios/compiler.py:626-634) depending on how the tree is laid out on
    disk. Nothing but the path changes, which is exactly the claim
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
    # A one-step document has nothing left after `_rewrite_drop_a_step`, and an
    # empty document is genuinely equivalent to nothing else interesting; drop
    # the control rather than let it degenerate into `[] vs []`.
    usable = [r for r in REWRITES if r[2] is not None or len(document['steps']) > 1]
    name, rewrite, level = draw(st.sampled_from(usable))
    return name, document, rewrite(document), level


@pytest.mark.fast
@given(document_rewrites())
@ORACLE
def test_each_rewrite_lands_at_exactly_the_strength_it_claims(
        case: tuple[str, Yaml, Yaml, Strength | None]) -> None:
    """Each strength accepts its own rewrite and the one above it does not.

    This is the property that makes `test_the_lattice_holds` mean something:
    it shows the antecedent of every implication actually fires, and it shows
    each strength is *strictly* weaker than the one above it. Checked in both
    directions on purpose — "accepts" alone is satisfied by a relation that
    accepts everything, and "rejects" alone by one that rejects everything.
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
    """The companion for the two properties above.

    Both quantify over `document_rewrites()`, and both are *weaker* in
    proportion to how many rewrites the strategy stops producing — a
    `sampled_from` that only ever yielded `identity` would leave them asserting
    reflexivity and nothing else, silently. Same argument as P26 for the
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
@given(document_rewrites())
@ORACLE
def test_the_lattice_holds(case: tuple[str, Yaml, Yaml, Strength | None]) -> None:
    """Stricter implies weaker: if two documents are IDENTICAL they are
    equivalent at every lower strength.

    Without this a caller cannot reason about the enum at all — asking for the
    strongest relation a transformation preserves would say nothing about the
    others, and `Strength` would be three unrelated functions wearing an
    `IntEnum` as a costume.
    """
    _name, left, right, _level = case
    for stronger in Strength:
        for weaker in Strength:
            if weaker < stronger and equivalent(left, right, stronger) is None:
                assert equivalent(left, right, weaker) is None, (
                    f'{stronger.name} held but {weaker.name} did not')


@pytest.mark.fast
def test_the_relation_is_reflexive() -> None:
    """Necessary, and nowhere near sufficient — `MUST_DIFFER` is what makes
    this meaningful rather than a property of the constant function None."""
    doc: Yaml = {'steps': [{'id': 'a__step__1__mk', 'in': {'f': 'x'}, 'out': ['file']}],
                 'requirements': {'ScatterFeatureRequirement': {}}}
    for strength in Strength:
        assert equivalent(doc, doc, strength) is None


@pytest.mark.fast
def test_a_divergence_names_where_it_found_the_difference() -> None:
    """Spec 3 runs this over thousands of inputs. A report that says only
    "not equivalent" moves the work to a human with two long documents."""
    left: Yaml = {'steps': [{'id': 'a__step__1__mk', 'in': {'f': 'left'}}]}
    right: Yaml = {'steps': [{'id': 'a__step__1__mk', 'in': {'f': 'right'}}]}
    found = equivalent(left, right, Strength.IDENTICAL)
    assert found is not None
    assert found.path == '.steps[0].in.f'
    assert (found.left, found.right) == ('left', 'right')
    assert 'left' in str(found) and 'right' in str(found)


@pytest.mark.fast
def test_a_divergence_names_a_missing_key_and_a_shorter_list() -> None:
    """The two paths `test_a_divergence_names_where_it_found_the_difference`
    does not reach. A walk that reported `<root>` for these would still pass
    that test while being useless on the two shapes a real compiler diff most
    often has: a key one side stopped emitting, and a step that vanished."""
    missing = equivalent({'steps': [], 'outputs': {'x': {'type': 'File'}}},
                         {'steps': [], 'outputs': {}}, Strength.IDENTICAL)
    assert missing is not None and missing.path == '.outputs.x'
    assert (missing.left, missing.right) == ({'type': 'File'}, None)

    shorter = equivalent({'steps': [{'id': 'a__step__1__mk'}]},
                         {'steps': []}, Strength.IDENTICAL)
    assert shorter is not None and shorter.path == '.steps[]'
    assert (shorter.left, shorter.right) == (1, 0)


@pytest.mark.fast
def test_the_dag_check_sees_a_moved_edge_without_being_handed_a_graph() -> None:
    """The companion for `_same_dag`'s derived graph.

    `MUST_DIFFER`'s "an edge points somewhere else" is the case that forced it
    to exist, so this asserts the mechanism directly rather than only through
    that table: two documents with identical step ids and identical tool
    stems, differing only in where one `in:` source points, must diverge at
    UP_TO_RENAMING with the DAG named as the place it happened.
    """
    left: Yaml = {'steps': [{'id': 'a__step__1__mk', 'out': ['file']},
                            {'id': 'a__step__2__xform',
                             'in': {'file': {'source': 'a__step__1__mk/file'}}}]}
    right: Yaml = {'steps': [{'id': 'a__step__1__mk', 'out': ['file']},
                             {'id': 'a__step__2__xform',
                              'in': {'file': {'source': 'a__step__9__mk/file'}}}]}
    found = equivalent(left, right, Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_the_dag_check_sees_the_same_tools_wired_up_differently() -> None:
    """The companion for `_dataflow`'s node labels.

    Both documents hold the same three tools, so the stem multiset agrees, and
    both graphs are three nodes with one edge and one isolate, so a bare
    `DiGraphMatcher` maps them onto each other. Only matching nodes by tool
    stem sees that the edge runs `mk_file -> xform` on one side and
    `join -> xform` on the other. Written as its own case because mutating
    `node_match` to `True` left every other test in this file green.
    """
    def _document(producer: str) -> Yaml:
        others = {'mk_file': 'join', 'join': 'mk_file'}
        return {'steps': [{'id': f'a__step__1__{producer}', 'out': ['file']},
                          {'id': f'a__step__2__{others[producer]}', 'out': ['file']},
                          {'id': 'a__step__3__xform',
                           'in': {'file': {'source': f'a__step__1__{producer}/file'}}}]}
    found = equivalent(_document('mk_file'), _document('join'), Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_the_dag_check_sees_an_edge_landing_on_a_different_input() -> None:
    """The companion for `_dataflow`'s edge labels.

    `join` takes a `left` and a `right`; feeding one file to `left` is not the
    workflow that feeds it to `right`. Same nodes, same tool stems, same single
    edge between the same pair — the difference lives entirely in which input
    the edge lands on, which is invisible to any check on the graph's shape.
    Mutating `edge_match` to `True` left every other test in this file green.
    """
    def _document(port: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                          {'id': 'a__step__2__join',
                           'in': {port: {'source': 'a__step__1__mk_file/file'}}}]}
    found = equivalent(_document('left'), _document('right'), Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_the_dag_check_sees_two_edges_dangling_at_different_places() -> None:
    """The companion for `_dataflow` keeping absent producers as nodes.

    A reference to a step the document does not contain is what a renaming
    that missed one site leaves behind, and it is the shape an IR migration
    fails at. Discard those references and the two documents below are both
    "one step, no edges" and the relation calls them equivalent; keep them and
    the tool at the far end of each edge is visibly different.
    """
    def _document(producer: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__xform',
                           'in': {'file': {'source': f'a__step__7__{producer}/file'}}}]}
    found = equivalent(_document('mk_file'), _document('join'), Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_the_array_forms_of_ports_and_requirements_are_read() -> None:
    """CWL admits `requirements` as a list of `{class: ...}` and ports as an
    array of records; the Sophios compiler emits neither today, so without
    this those branches would be code no test reaches — and a Spec 3 pipeline
    that started emitting them would silently compare `repr` of a whole list
    against `repr` of another, i.e. would still be "working"."""
    listed: Yaml = {'steps': [], 'requirements': [{'class': 'ScatterFeatureRequirement'}],
                    'outputs': [{'id': 'x', 'type': 'File'}]}
    mapped: Yaml = {'steps': [], 'requirements': {'ScatterFeatureRequirement': {}},
                    'outputs': {'y': {'type': 'File'}}}
    assert equivalent(listed, mapped, Strength.UP_TO_RENAMING) is None
    dropped: Yaml = {'steps': [], 'requirements': [], 'outputs': []}
    assert equivalent(listed, dropped, Strength.UP_TO_RENAMING) is not None


@pytest.mark.fast
def test_isomorphism_alone_would_not_be_enough() -> None:
    """Why `_same_dag` compares tool stems as well as shape.

    `test_inline_subworkflows` (tests/core/test_examples.py:418) compares two
    compilations with a bare `DiGraphMatcher` and nothing else. These two
    documents have the same shape — two nodes, one edge — and different tools
    at every node, so a bare matcher calls them equivalent. That is precisely
    the kind of thing an IR migration could get wrong, which is why the
    relation Spec 3 imports must not be the one already in the tree.
    """
    left: Yaml = {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                            {'id': 'a__step__2__xform',
                             'in': {'file': {'source': 'a__step__1__mk_file/file'}}}]}
    right: Yaml = {'steps': [{'id': 'a__step__1__mk_text', 'out': ['text']},
                             {'id': 'a__step__2__join',
                              'in': {'left': {'source': 'a__step__1__mk_text/text'}}}]}
    found = equivalent(left, right, Strength.UP_TO_RENAMING)
    assert found is not None and 'tool multiset' in found.path


@pytest.mark.fast
def test_a_divergence_is_frozen() -> None:
    """A report Spec 3 collects into a list must not be editable afterwards."""
    found = Divergence(Strength.IDENTICAL, '.x', 1, 2)
    with pytest.raises((AttributeError, TypeError)):
        found.path = '.y'  # type: ignore[misc]
