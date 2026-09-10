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
    # `format` is the only thing separating `mk_file` from `mk_text` in the
    # synthetic registry, so a relation that compared `type` alone could not
    # tell a workflow ending in one from a workflow ending in the other. Found
    # by mutation: dropping `format` from `_SHAPE_KEYS` left this file green.
    ('an output changed format',
     {'steps': [], 'outputs': {'x': {'type': 'File', 'format': 'edam:format_2330'}}},
     {'steps': [], 'outputs': {'x': {'type': 'File', 'format': 'edam:format_3752'}}}),
    # The fixture whose absence let `outputSource` stay dropped after the same
    # argument had already been accepted for `in[].source`. Nothing else here
    # can see it: `_port_shapes` reduces the outputs to a *sorted* multiset,
    # which no permutation of the pairing changes, and workflow outputs were
    # not nodes, so the DAG check never saw them either.
    ('two workflow outputs swapped producers',
     {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                {'id': 'a__step__2__count', 'out': ['n']}],
      'outputs': {'p': {'type': 'File', 'outputSource': 'a__step__1__mk_file/file'},
                  'q': {'type': 'int', 'outputSource': 'a__step__2__count/n'}}},
     {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                {'id': 'a__step__2__count', 'out': ['n']}],
      'outputs': {'p': {'type': 'File', 'outputSource': 'a__step__2__count/n'},
                  'q': {'type': 'int', 'outputSource': 'a__step__1__mk_file/file'}}}),
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
    # Every rewrite applies to every document, the control included. An earlier
    # draft withheld the control from one-step documents on the grounds that
    # dropping their only step degenerates into `[] vs []`; it does not — the
    # workflow-level `inputs` and `outputs` the step produced are retained, and
    # the graph goes from one node to none, so the pair is rejected for real
    # reasons at all three strengths.
    name, rewrite, level = draw(st.sampled_from(REWRITES))
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
def test_the_dag_check_sees_one_of_two_edges_replaced_by_an_outside_source() -> None:
    """The companion for `_dataflow`'s edge labels, isolated from everything else.

    `join` here binds both `left` and `right`, and both documents bind exactly
    those two names, so the two `join` nodes have *identical* labels and
    `node_match` cannot tell them apart; both graphs have the same two nodes
    and the same single edge between them, so shape cannot either. The only
    difference is that on the left `join.right` is fed by the `mk_file` step
    and on the right it comes from outside — which lives entirely in the
    edge's `(output port, input name)` set.

    Written this way deliberately. The obvious fixture — `left` versus `right`
    as the bound name — stopped isolating `edge_match` once step bodies became
    part of the node label, because the bound names are in the body; mutating
    `edge_match` to `True` then left the suite green. This one still fails.
    """
    def _document(second: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                          {'id': 'a__step__2__join',
                           'in': {'left': {'source': 'a__step__1__mk_file/file'},
                                  'right': {'source': second}}}]}
    found = equivalent(_document('a__step__1__mk_file/file'),
                       _document('outside___right'), Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


#: Differences *inside* a step that carry meaning and that no renaming can
#: produce. Every one of these returned None from `UP_TO_RENAMING` in the draft
#: this file's review examined, because the relation looked only at `id` and at
#: `in[].source` and forgave the rest of a step in silence. `_step_body` now
#: compares a step's keys by exclusion, so this list is a regression guard
#: rather than an enumeration — a key nobody thought of is compared too.
MEANING_IN_A_STEP: Final[list[tuple[str, Yaml, Yaml]]] = [
    ('an output port vanished from out', {'out': ['file', 'text']}, {'out': ['file']}),
    ('a step became scattered', {'scatter': ['name']}, {}),
    ('the scatter method changed',
     {'scatter': ['a', 'b'], 'scatterMethod': 'dotproduct'},
     {'scatter': ['a', 'b'], 'scatterMethod': 'flat_crossproduct'}),
    ('a conditional flipped', {'when': '$(true)'}, {'when': '$(false)'}),
    ('an input default changed',
     {'in': {'name': {'source': 'wf___name', 'default': 1}}},
     {'in': {'name': {'source': 'wf___name', 'default': 999}}}),
    ('a binding moved to another input',
     {'in': {'name': {'source': 'wf___name'}}},
     {'in': {'other': {'source': 'wf___name'}}}),
    ('a valueFrom appeared',
     {'in': {'name': {'source': 'wf___name'}}},
     {'in': {'name': {'source': 'wf___name', 'valueFrom': '$(self + 1)'}}}),
]


@pytest.mark.fast
@pytest.mark.parametrize('why, left_step, right_step', MEANING_IN_A_STEP,
                         ids=[c[0] for c in MEANING_IN_A_STEP])
def test_up_to_renaming_forgives_nothing_inside_a_step_but_names_and_paths(
        why: str, left_step: Yaml, right_step: Yaml) -> None:
    """The weakest strength still reads the whole step.

    `UP_TO_RENAMING` forgives a step's `id`, its `run` and the `source` inside
    each binding, and the module docstring says so and says why. Anything else
    it forgave would be forgiveness with no stated reason — the thing that
    module's own thesis is against — so each of these must be rejected.
    """
    def _document(extra: Yaml) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'run': 'a/mk_file.cwl', **extra}]}
    found = equivalent(_document(left_step), _document(right_step), Strength.UP_TO_RENAMING)
    assert found is not None, f'UP_TO_RENAMING forgave a step difference silently: {why}'


@pytest.mark.fast
def test_only_identical_compares_key_order() -> None:
    """IDENTICAL is a claim about bytes; the weaker two are claims about a
    workflow, and a YAML mapping is unordered by its own specification.

    Task 5's subject is emitted key order — `requirements` built from a set,
    eight hash seeds giving eight orders — so a relation that normalised order
    away at every strength would make the regression that predicate exists to
    catch invisible to it.
    """
    left: Yaml = {'class': 'Workflow', 'steps': []}
    right: Yaml = {'steps': [], 'class': 'Workflow'}
    found = equivalent(left, right, Strength.IDENTICAL)
    assert found is not None and found.path == '<root> (key order)'
    assert (found.left, found.right) == (['class', 'steps'], ['steps', 'class'])
    assert equivalent(left, right, Strength.UP_TO_EMBEDDING) is None
    assert equivalent(left, right, Strength.UP_TO_RENAMING) is None

    nested_left: Yaml = {'steps': [{'id': 'a__step__1__mk', 'out': ['f'], 'run': 'r'}]}
    nested_right: Yaml = {'steps': [{'id': 'a__step__1__mk', 'run': 'r', 'out': ['f']}]}
    nested = equivalent(nested_left, nested_right, Strength.IDENTICAL)
    assert nested is not None and nested.path == '.steps[0] (key order)'
    assert equivalent(nested_left, nested_right, Strength.UP_TO_EMBEDDING) is None


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
def test_a_declared_step_is_not_the_same_as_one_only_referenced() -> None:
    """The companion for `_dataflow`'s `('external', ...)` node marker.

    Left declares `mk_file` and consumes its output. Right consumes the same
    output from a step it never declares — a dangling reference, which is what
    a migration that dropped a step from `steps:` while leaving its consumers
    intact produces. Both graphs are two nodes and one edge, both tool stems
    agree, and neither document has ports or requirements to compare, so
    nothing but the marker separates them.

    Found by mutation: relabelling absent producers as if they were declared
    steps left every other test in this file green.
    """
    consumer: Yaml = {'id': 'a__step__2__xform',
                      'in': {'file': {'source': 'a__step__1__mk_file/file'}}}
    declared: Yaml = {'steps': [{'id': 'a__step__1__mk_file'}, consumer]}
    dangling: Yaml = {'steps': [consumer]}
    found = equivalent(declared, dangling, Strength.UP_TO_RENAMING)
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
    """Why the matcher is labelled rather than bare.

    `test_inline_subworkflows` (tests/core/test_examples.py:418) compares two
    compilations with a bare `DiGraphMatcher` and nothing else. These two
    documents have the same shape — two nodes, one edge — and a different tool
    at every node, so a bare matcher calls them equivalent. That is precisely
    the kind of thing an IR migration could get wrong, which is why the
    relation Spec 3 imports must not be the one already in the tree.

    Asserts the *verdict*, not the path string. An earlier version asserted
    `'tool multiset' in found.path`, which is this project's recurring failure
    written into the artifact everything imports: a separate stem-multiset
    check had by then been subsumed by the labelled isomorphism, so the name of
    the test claimed a discrimination the code no longer performed and only the
    string kept it green. The check was deleted; this is what remains, and it
    fails if `node_match` stops looking at the label.
    """
    left: Yaml = {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                            {'id': 'a__step__2__xform',
                             'in': {'file': {'source': 'a__step__1__mk_file/file'}}}]}
    right: Yaml = {'steps': [{'id': 'a__step__1__mk_text', 'out': ['file']},
                             {'id': 'a__step__2__join',
                              'in': {'file': {'source': 'a__step__1__mk_text/file'}}}]}
    found = equivalent(left, right, Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_the_dag_check_sees_a_port_rewired_onto_a_different_workflow_input() -> None:
    """The companion for `_dataflow`'s workflow-level input nodes.

    Every argument a step does not receive from another step is emitted as
    `in: {arg: {source: {step_id}___{arg}}}` against a declared workflow-level
    input — the majority of the bindings in any compiled document. Those
    sources name no producer, so an earlier draft skipped them entirely, and
    with them skipped the two documents below were UP_TO_RENAMING-equal: both
    declare a `File` input and a `string` input, both have one step and no
    edges, and which port reads which was nowhere in the comparison.

    Both directions, because the fix must not overshoot. Feeding `left` from
    the `string` input instead of the `File` one is a divergence; feeding it
    from the *other* `File` input is not, because relabelling two ports of the
    same shape is precisely what re-rooting a namespace does.
    """
    def _document(left: str, right: str, second_type: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__join',
                           'in': {'left': {'source': left}, 'right': {'source': right}}}],
                'inputs': {'a___p': {'type': 'File'}, 'a___q': {'type': second_type}},
                'outputs': {}}

    swapped = equivalent(_document('a___p', 'a___q', 'string'),
                         _document('a___q', 'a___p', 'string'), Strength.UP_TO_RENAMING)
    assert swapped is not None and swapped.path == '<dag>'

    assert equivalent(_document('a___p', 'a___q', 'File'),
                      _document('a___q', 'a___p', 'File'), Strength.UP_TO_RENAMING) is None


@pytest.mark.fast
def test_the_dag_check_sees_a_workflow_output_rewired_onto_a_different_producer() -> None:
    """The companion for `_dataflow`'s workflow-level output nodes.

    The mirror of the input-side test above, and written the same way because
    the fix must not overshoot in the same direction: rewiring an output onto a
    differently-shaped producer is a divergence, and permuting two outputs of
    the *same* shape is not, that permutation being what a renaming is.

    `MUST_DIFFER` carries the swap as well. This exists beside it to assert the
    mechanism — that the pairing lives in the graph — rather than only the
    verdict, and to pin the second direction, which no `MUST_DIFFER` row can
    express.
    """
    def _document(first: str, second: str, second_type: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']},
                          {'id': 'a__step__2__count', 'out': ['n']}],
                'outputs': {'p': {'type': 'File', 'outputSource': first},
                            'q': {'type': second_type, 'outputSource': second}}}

    swapped = equivalent(_document('a__step__1__mk_file/file', 'a__step__2__count/n', 'int'),
                         _document('a__step__2__count/n', 'a__step__1__mk_file/file', 'int'),
                         Strength.UP_TO_RENAMING)
    assert swapped is not None and swapped.path == '<dag>'

    # Both outputs declared `File`, so the two producers are interchangeable
    # and relabelling which one is called `p` is a renaming.
    assert equivalent(_document('a__step__1__mk_file/file', 'a__step__2__count/n', 'File'),
                      _document('a__step__2__count/n', 'a__step__1__mk_file/file', 'File'),
                      Strength.UP_TO_RENAMING) is None


@pytest.mark.fast
def test_an_output_that_lost_its_producer_is_not_the_same_document() -> None:
    """The companion for `_dataflow` giving an output no node without a source.

    An output whose `outputSource` vanished — the shape of a lowering that
    stopped wiring one up — keeps its declared shape, so `_port_shapes` cannot
    see it go. The graph can: the node and its edge are simply absent.
    """
    def _document(**source: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'out': ['file']}],
                'outputs': {'p': {'type': 'File', **source}}}
    found = equivalent(_document(outputSource='a__step__1__mk_file/file'), _document(),
                       Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
@pytest.mark.parametrize('why, document', [
    ('mapping-form steps', {'steps': {'s': {'run': 'r'}}}),
    ('a step with no id', {'steps': [{'out': ['file']}]}),
    ('array-form in', {'steps': [{'id': 'a__step__1__join',
                                  'in': [{'id': 'left', 'source': 'x'}]}]}),
], ids=['steps mapping', 'step without id', 'in array'])
def test_a_shape_the_relation_cannot_read_is_loud(why: str, document: Yaml) -> None:
    """The module's WILL NOT READ contract, asserted rather than described.

    Each of these was previously read as "nothing here" — a non-list `steps:`
    coerced to `[]`, a step without `id` filtered out, an array-form `in:`
    forgiven by `_FORGIVEN_STEP_KEYS` and restored by neither reader — and so
    each made a pair of *different* documents compare equal. Compared against
    an empty document, which is the pair that was equal, so a coercion coming
    back returns this test to red rather than merely to a different exception.
    """
    try:
        equivalent(document, {'steps': []}, Strength.UP_TO_RENAMING)
    except TypeError:
        return
    pytest.fail(f'{why}: read as a document with nothing in it, and so found equivalent '
                'to an empty one. A shape this relation cannot read has to be loud.')


@pytest.mark.fast
def test_an_unparsable_producer_keeps_its_whole_name() -> None:
    """The companion for `_stem`'s fallback.

    `parse_step_name_str` raises on anything that is not
    `{stem}__step__{i}__{key}`, and `_stem` then falls back to the whole
    string. That fallback is the *stricter* choice, and the docstring says so —
    two unparsable ids have to be equal rather than being lumped together as
    one anonymous stem. Nothing pinned it: returning a constant instead left
    the file green, and these two documents equivalent.
    """
    def _document(producer: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__xform',
                           'in': {'file': {'source': f'{producer}/file'}}}]}
    found = equivalent(_document('handwritten_a'), _document('handwritten_b'),
                       Strength.UP_TO_RENAMING)
    assert found is not None and found.path == '<dag>'


@pytest.mark.fast
def test_up_to_embedding_forgives_run_and_nothing_else() -> None:
    """`run` is the whole of what this strength ignores.

    `_rewrite_run_paths` shows the forgiveness happens; this shows it stops
    there. A normalisation widened by one key — deleting `label` alongside
    `run`, say — is invisible to every other test in this file, and that is the
    shape of change an IR migration makes when a key starts moving around.
    """
    def _document(run: str, label: str) -> Yaml:
        return {'steps': [{'id': 'a__step__1__mk_file', 'run': run, 'label': label}]}

    assert equivalent(_document('a__step__1__mk_file/mk_file.cwl', 'x'),
                      _document('flat/mk_file.cwl', 'x'), Strength.UP_TO_EMBEDDING) is None
    found = equivalent(_document('a/mk_file.cwl', 'x'), _document('a/mk_file.cwl', 'y'),
                       Strength.UP_TO_EMBEDDING)
    assert found is not None and found.path == '.steps[0].label'


@pytest.mark.fast
def test_a_divergence_is_frozen() -> None:
    """A report Spec 3 collects into a list must not be editable afterwards."""
    found = Divergence(Strength.IDENTICAL, '.x', 1, 2)
    with pytest.raises((AttributeError, TypeError)):
        found.path = '.y'  # type: ignore[misc]
