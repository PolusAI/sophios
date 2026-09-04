"""What it means for two compilations to mean the same thing.

Three strengths, because there is no single honest answer. Each names exactly
what it is allowed to ignore, and why ignoring it is legitimate rather than
convenient — a normalisation without a reason is a place for a real difference
to hide.

    IDENTICAL          nothing is ignored.
    UP_TO_EMBEDDING    `run:` paths are ignored.
    UP_TO_RENAMING     namespaced names are ignored; the DAG must match.

The order is a lattice: IDENTICAL implies UP_TO_EMBEDDING implies
UP_TO_RENAMING, and `test_the_lattice_holds` asserts it, so a caller can always
ask for the strongest relation a transformation is claimed to preserve and know
the weaker ones follow.

Returns a `Divergence`, not a bool. Spec 3 runs this over thousands of inputs
during the IR migration; "not equivalent" is not an actionable report, and a
harness that could only say False would push every investigation back onto a
human reading two 500-line documents.

CANNOT DETECT (declared, per the negative-testing rules): at UP_TO_RENAMING,
a difference confined to a *name* that the compiler does not namespace and
that this module does not enumerate — anything outside `steps`, `inputs`,
`outputs`, `requirements` is not inspected at that strength at all, because
nothing yet says which parts of it renaming may touch. Ask for
UP_TO_EMBEDDING when the claim is about the whole document; UP_TO_RENAMING is
for transformations that genuinely re-root the namespace, and it is the
weakest relation in the lattice on purpose.
"""
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final, Iterator

import networkx as nx
from networkx.algorithms import isomorphism

from sophios.utils import parse_step_name_str, recursively_delete_dict_key
from sophios.wic_types import Yaml


class Strength(IntEnum):
    """How much two compilations are allowed to differ.

    `IntEnum` so the lattice is expressible as `<=`. Higher is stricter.
    """

    #: Namespaced names may differ; the DAG and the tools at each node may not.
    #: Legitimate because a namespace encodes *nesting depth*
    #: (`{stem}__step__{i}__{key}`, joined with `'___'`, docs/dev/algorithms.md),
    #: so splitting a workflow into subworkflows renames every port and moves
    #: no edge. This is the only strength partitioning can preserve.
    UP_TO_RENAMING = 1

    #: `run:` paths may differ; nothing else may. Legitimate because `run:`
    #: encodes where a document sits relative to its parent — `relative_run_path`
    #: writes `step_name/stem.cwl` or `'___'.join(namespaces + [stem.cwl])`
    #: (src/sophios/compiler.py:626-634) — which is embedding, not meaning.
    UP_TO_EMBEDDING = 2

    #: Byte-for-byte, after nothing. Available as a real strength only because
    #: emission is canonical (see the canonical-emission predicate): while
    #: `requirements` key order came from a set, two compilations of one input
    #: could differ here and IDENTICAL would have been unusable.
    IDENTICAL = 3


@dataclass(frozen=True, slots=True)
class Divergence:
    """The first place two compilations disagree."""

    strength: Strength
    path: str
    left: Any
    right: Any

    def __str__(self) -> str:
        return (f'{self.strength.name} divergence at {self.path}\n'
                f'  left : {self.left!r}\n'
                f'  right: {self.right!r}')


def equivalent(left: Yaml, right: Yaml, strength: Strength, *,
               left_graph: nx.DiGraph | None = None,
               right_graph: nx.DiGraph | None = None) -> Divergence | None:
    """Whether two compiled workflows agree at the given strength.

    Returns None when they agree, and the first divergence otherwise. The
    graphs are optional and only consulted at `UP_TO_RENAMING`, where the
    claim is about the DAG rather than about the document.
    """
    match strength:
        case Strength.IDENTICAL:
            return _first_difference(left, right, strength, '')
        case Strength.UP_TO_EMBEDDING:
            return _first_difference(recursively_delete_dict_key('run', left),
                                     recursively_delete_dict_key('run', right),
                                     strength, '')
        case _:
            return _same_dag(left, right, left_graph, right_graph)


def _first_difference(left: Any, right: Any, strength: Strength, path: str) -> Divergence | None:
    """Structural walk reporting where two documents first disagree.

    A plain `!=` would be correct and useless: the caller learns that two
    documents differ and nothing about where, which is the whole reason
    `test_cwl_embedding_independence` writes both sides to disk and shells out
    to `diff` (tests/core/test_examples.py:336-355). Walking here means the
    failure message is already the diff.
    """
    # pylint: disable=too-many-return-statements  # one per node kind and per
    # way that kind can disagree; collapsing them costs the path, which is the
    # whole point of walking rather than writing `left != right`.
    if type(left) is not type(right):
        return Divergence(strength, path or '<root>', left, right)
    if isinstance(left, dict):
        for key in dict.fromkeys([*left, *right]):
            if key not in left or key not in right:
                return Divergence(strength, f'{path}.{key}', left.get(key), right.get(key))
            found = _first_difference(left[key], right[key], strength, f'{path}.{key}')
            if found is not None:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return Divergence(strength, f'{path}[]', len(left), len(right))
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_difference(a, b, strength, f'{path}[{index}]')
            if found is not None:
                return found
        return None
    return None if left == right else Divergence(strength, path or '<root>', left, right)


#: The fields of a workflow-level port that a renaming cannot touch. `type` and
#: `format` are values, not names; `outputSource` and the port's own key are
#: names and are deliberately absent — see `_port_shapes`.
_SHAPE_KEYS: Final[tuple[str, ...]] = ('type', 'format')


def _stem(step_id: str) -> str:
    """The tool key inside a namespaced step id, or the id itself.

    `parse_step_name_str` raises on anything that is not
    `{yaml_stem}__step__{i}__{step_key}`. Falling back to the whole string is
    the *stricter* choice — two unparsable ids then have to be equal rather
    than being lumped together as "unknown" — and emitted CWL has no such ids
    anyway, so the fallback only ever fires on hand-written fixtures.
    """
    try:
        return str(parse_step_name_str(step_id)[2])
    except ValueError:
        return step_id


def _bindings(step: Yaml) -> Iterator[tuple[str, str]]:
    """Every `(input name, source)` pair a step declares.

    Both surface forms the compiler emits: `in: {name: 'src'}` and
    `in: {name: {source: 'src'}}`, the latter with `source` possibly a list
    (CWL's multiple-inbound-links form).
    """
    node = step.get('in')
    if not isinstance(node, dict):
        return
    for name, value in node.items():
        inner = value.get('source') if isinstance(value, dict) else value
        for source in (inner if isinstance(inner, list) else [inner]):
            if isinstance(source, str):
                yield str(name), source


def _dataflow(document: Yaml) -> nx.DiGraph:
    """The document's dataflow DAG, in a form renaming cannot change.

    Nodes are steps, labelled with the *tool stem* rather than the step id;
    edges are `producer/port` references out of `in:`, labelled with the
    `(output port, input name)` pairs that justify them. Both labels are tool
    port names, which a namespace re-rooting does not touch, so the labelled
    graph is exactly the part of the document UP_TO_RENAMING is willing to
    compare.

    A reference to a step this document does not contain becomes a node of its
    own rather than being dropped. Dropping it is what makes the "an edge
    points somewhere else" case in `MUST_DIFFER` invisible: with the target
    discarded, a document whose edge went nowhere looks the same as one with
    no edge at all.
    """
    graph = nx.DiGraph()
    steps = document.get('steps')
    steps = steps if isinstance(steps, list) else []
    for step in steps:
        if isinstance(step, dict) and 'id' in step:
            graph.add_node(str(step['id']), stem=_stem(str(step['id'])))
    for step in steps:
        if not isinstance(step, dict) or 'id' not in step:
            continue
        consumer = str(step['id'])
        for name, source in _bindings(step):
            producer, separator, port = source.partition('/')
            if not separator or not port or not producer:
                continue  # a workflow-level input, not an edge
            if producer not in graph:
                graph.add_node(producer, stem=_stem(producer))
            if graph.has_edge(producer, consumer):
                graph.edges[producer, consumer]['ports'].add((port, name))
            else:
                graph.add_edge(producer, consumer, ports={(port, name)})
    return graph


def _port_shapes(document: Yaml, key: str) -> list[str]:
    """The multiset of workflow-level port shapes under `key`, names dropped.

    Verified against a hermetic compilation rather than assumed: the compiler
    emits workflow-level `inputs`/`outputs` keys as `{step_id}___{port}` —
    `oracle__step__1__mk_file___name` — so those keys are namespaced and are
    *not* stable under renaming. This is the correction to the brief's
    expectation that they would be. What renaming cannot change is how many
    ports there are (a renaming is a bijection on names) and what type each
    one has, so that is what is compared. `outputSource` is excluded for the
    same reason the key is: it is a name.
    """
    node = document.get(key)
    ports = list(node.values()) if isinstance(node, dict) else (node if isinstance(node, list) else [])
    shapes = []
    for port in ports:
        shapes.append(repr({k: port[k] for k in _SHAPE_KEYS if k in port})
                      if isinstance(port, dict) else repr(port))
    return sorted(shapes)


def _requirement_names(document: Yaml) -> list[str]:
    """The requirement class names, which renaming genuinely cannot touch.

    Unlike port names these are CWL class names — `ScatterFeatureRequirement`,
    `SubworkflowFeatureRequirement` — fixed by the CWL specification and never
    namespaced by the compiler. So here, and only here, the *names* are
    compared as a set rather than reduced to a count.
    """
    node = document.get('requirements') or {}
    if isinstance(node, dict):
        return sorted(node)
    if isinstance(node, list):
        return sorted(str(r.get('class', r)) if isinstance(r, dict) else str(r) for r in node)
    return [repr(node)]


def _same_dag(left: Yaml, right: Yaml,
              left_graph: nx.DiGraph | None, right_graph: nx.DiGraph | None) -> Divergence | None:
    """Whether two workflows have the same shape under renaming.

    Isomorphism alone is too weak and `test_inline_subworkflows` uses it alone
    (tests/core/test_examples.py:418): a bare `DiGraphMatcher` would accept a
    graph in which every step had been replaced by a *different tool of the
    same arity*, which is precisely the kind of thing an IR migration could get
    wrong. So the multiset of tool stems is checked too — recovered with
    `parse_step_name_str`, whose third element is the step key.

    Three checks the brief's sketch did not have, each added because a
    `MUST_DIFFER` case walked straight past it (Step 4's "a finding about the
    relation, not about the test"):

      * `requirements` class names, because a requirement is not a step and
        the DAG check cannot see one disappear. Compared as a set: these names
        come from the CWL specification, not from the namespace.
      * `inputs`/`outputs`, because an output is not a step either. Compared
        by *shape* and count, not by name — a hermetic compilation shows those
        keys are namespaced (`oracle__step__1__mk_file___name`), so comparing
        them by name would reject every renaming this strength exists to
        forgive.
      * the dataflow graph derived from the document itself, because an edge
        that moves changes no step id and no tool stem. The brief's version
        returned None whenever the caller passed no graphs, which made "an
        edge points somewhere else" pass at this strength.

    Supplied graphs replace the derived pair rather than adding to it. They are
    the compiler's own `NodeData.graph.networkx`, flattened across
    subworkflows, which is the only comparable object when the transformation
    under test *restructures* the document (inlining, partitioning) rather than
    merely renaming it. They carry no `stem` attribute, so they are matched
    bare, exactly as `test_inline_subworkflows` matches them — the stem
    multiset above is what keeps that from being the weak check it is there.
    """
    for key in ('inputs', 'outputs'):
        left_ports, right_ports = _port_shapes(left, key), _port_shapes(right, key)
        if left_ports != right_ports:
            return Divergence(Strength.UP_TO_RENAMING, f'.{key} (shapes, names ignored)',
                              left_ports, right_ports)

    left_reqs, right_reqs = _requirement_names(left), _requirement_names(right)
    if left_reqs != right_reqs:
        return Divergence(Strength.UP_TO_RENAMING, '.requirements (class names)',
                          left_reqs, right_reqs)

    left_stems = sorted(_stem(str(s['id'])) for s in left.get('steps', []) if 'id' in s)
    right_stems = sorted(_stem(str(s['id'])) for s in right.get('steps', []) if 'id' in s)
    if left_stems != right_stems:
        return Divergence(Strength.UP_TO_RENAMING, '.steps[].id (tool multiset)',
                          left_stems, right_stems)

    if left_graph is not None and right_graph is not None:
        one, two = left_graph, right_graph
        matcher = isomorphism.DiGraphMatcher(one, two)
    else:
        one, two = _dataflow(left), _dataflow(right)
        matcher = isomorphism.DiGraphMatcher(
            one, two,
            node_match=lambda a, b: a.get('stem') == b.get('stem'),
            edge_match=lambda a, b: a.get('ports') == b.get('ports'))
    if not matcher.is_isomorphic():
        return Divergence(Strength.UP_TO_RENAMING, '<dag>',
                          sorted(one.nodes), sorted(two.nodes))
    return None
