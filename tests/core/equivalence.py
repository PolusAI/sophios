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

CANNOT DETECT (declared, per the negative-testing rules). Everything below is
a thing UP_TO_RENAMING forgives; nothing else in a step is forgiven, because
`_step_body` compares a step's keys by *exclusion* — a key this module has
never heard of is compared, not ignored. IDENTICAL and UP_TO_EMBEDDING forgive
only what their own docstrings name.

  * `steps[].id` and `steps[].run`, whose values are namespaced or are paths.
    The id is what renaming renames; `run` is embedding, forgiven a strength
    lower down and so forgiven here too.
  * `steps[].in[].source`, whose value names either a producing step or a
    workflow-level input. Not dropped — re-expressed as a labelled edge in
    `_dataflow`, which is the whole content of "the DAG must match". A source
    naming a workflow-level input becomes an edge out of a node carrying that
    input's *shape*, so rewiring a port onto a differently-typed input is a
    divergence, while swapping two identically-shaped inputs is not — that
    swap really is a renaming, a renaming being a bijection on names. The
    binding's *name* and everything else under it (`default`, `valueFrom`,
    ...) is compared.
  * the keys of `inputs` and `outputs` and any `outputSource`, which a
    hermetic compilation shows are namespaced (`oracle__step__1__mk___name`).
    Their count and their `type`/`format` are compared; their names are not.
  * every top-level key other than `steps`, `inputs`, `outputs`,
    `requirements` — `class`, `cwlVersion`, `$namespaces`, `$schemas`,
    document-level `label`/`doc`. Not inspected at this strength, because
    nothing yet says which parts of them a renaming may touch. Ask for
    UP_TO_EMBEDDING when the claim is about the whole document.
  * mapping key *order*, below IDENTICAL. See `Strength.IDENTICAL`.

A step key that is compared but should not be is a false divergence, which is
loud; a step key that is forgiven but should not be is silent. The list above
is deliberately the second kind, kept short and each entry given its reason.
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

    #: Byte-for-byte, after nothing — mapping *key order* included. Available
    #: as a real strength only because emission is canonical (see the
    #: canonical-emission predicate): while `requirements` key order came from
    #: a set, two compilations of one input could differ here and IDENTICAL
    #: would have been unusable.
    #:
    #: Order is compared *only* here, and that asymmetry is the point. Task 5's
    #: whole subject is emitted key order — `requirements` built from a set,
    #: eight hash seeds giving eight orders — so a relation that normalised
    #: order away would make an emission-order regression invisible to the
    #: predicate written to catch it. Below IDENTICAL order is forgiven,
    #: legitimately: a YAML mapping is unordered by its own specification and
    #: CWL reads these documents as mappings, so two orderings are the same
    #: workflow. IDENTICAL is not a claim about the workflow, it is a claim
    #: about the bytes, and it is the only strength that makes one.
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


def equivalent(left: Yaml, right: Yaml, strength: Strength) -> Divergence | None:
    """Whether two compiled workflows agree at the given strength.

    Returns None when they agree, and the first divergence otherwise.

    Takes no graphs. An earlier draft let a caller supply the compiler's own
    `NodeData.graph.networkx` pair in place of the graph derived here, and
    that was strictly worse in three ways: nothing in the suite passed them,
    so the branch was untested; the compiler's nodes are namespaced strings
    carrying no tool label, so they can only be matched with a bare
    `DiGraphMatcher` — the exact weak check this module exists to replace,
    blind to a graph whose every step was swapped for a different tool of the
    same arity; and the report told Spec 3 to use them, which would have
    routed every downstream consumer onto the unverified path. If a
    restructuring rewrite (inlining, partitioning) later needs supplied
    graphs, they come back labelled and tested, as a deliberate change.
    """
    match strength:
        case Strength.IDENTICAL:
            return _first_difference(left, right, strength, '', ordered=True)
        case Strength.UP_TO_EMBEDDING:
            return _first_difference(recursively_delete_dict_key('run', left),
                                     recursively_delete_dict_key('run', right),
                                     strength, '', ordered=False)
        case _:
            return _same_dag(left, right)


def _first_difference(left: Any, right: Any, strength: Strength, path: str, *,
                      ordered: bool) -> Divergence | None:
    """Structural walk reporting where two documents first disagree.

    A plain `!=` would be correct and useless: the caller learns that two
    documents differ and nothing about where, which is the whole reason
    `test_cwl_embedding_independence` writes both sides to disk and shells out
    to `diff` (tests/core/test_examples.py:336-355). Walking here means the
    failure message is already the diff.

    `ordered` decides whether two mappings with the same entries in a different
    sequence are the same mapping. Only IDENTICAL passes True — see that
    member's docstring for why the asymmetry is legitimate and why Task 5
    needs it.
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
            found = _first_difference(left[key], right[key], strength, f'{path}.{key}',
                                      ordered=ordered)
            if found is not None:
                return found
        # Order last, so a mapping that is missing a key is reported as missing
        # that key rather than as a reordering — the entries have to agree
        # before their sequence is the interesting thing about them.
        if ordered and list(left) != list(right):
            return Divergence(strength, f'{path or "<root>"} (key order)',
                              list(left), list(right))
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return Divergence(strength, f'{path}[]', len(left), len(right))
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_difference(a, b, strength, f'{path}[{index}]', ordered=ordered)
            if found is not None:
                return found
        return None
    return None if left == right else Divergence(strength, path or '<root>', left, right)


#: The fields of a workflow-level port that a renaming cannot touch. `type` and
#: `format` are values, not names; `outputSource` and the port's own key are
#: names and are deliberately absent — see `_port_shapes`.
_SHAPE_KEYS: Final[tuple[str, ...]] = ('type', 'format')

#: Prefix distinguishing a workflow-level input's node in `_dataflow` from a
#: step's. Emitted step ids are `{stem}__step__{i}__{key}` and emitted port
#: names are `{step_id}___{port}`, so neither can contain a space or an angle
#: bracket; without the prefix an input and a step sharing a name would silently
#: become one node.
_INPUT_NODE: Final = '<input> '


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


def _canonical(node: Any) -> Any:
    """A hashable, order-normalised rendering of a YAML fragment.

    Mapping key order is normalised away here because this feeds
    UP_TO_RENAMING, which forgives order (see `Strength.IDENTICAL` for why
    only the top strength does not). List order is kept: a list in CWL is a
    sequence, and `scatter: [a, b]` is not `scatter: [b, a]` to a reader who
    has not proved otherwise.
    """
    if isinstance(node, dict):
        return tuple(sorted((str(k), _canonical(v)) for k, v in node.items()))
    if isinstance(node, list):
        return tuple(_canonical(v) for v in node)
    return repr(node)


#: Keys inside a step that UP_TO_RENAMING forgives, and the only ones. `id` is
#: what a renaming renames; `run` is a path, forgiven a strength lower down and
#: so forgiven here too; `in` is handled separately because only the `source`
#: inside a binding is a name — the binding's own key and everything else under
#: it is compared. Written as an exclusion so that a step key nobody here has
#: heard of is *compared*: a wrongly-compared key is a loud false divergence, a
#: wrongly-forgiven one is silent, and this file's thesis is that silent
#: forgiveness is where a real difference hides.
_FORGIVEN_STEP_KEYS: Final[tuple[str, ...]] = ('id', 'run', 'in')


def _step_body(step: Yaml) -> Any:
    """Everything in a step that a renaming cannot change.

    Covers `out`, `scatter`, `scatterMethod`, `when`, `label`, `doc`, a
    binding's `default` and `valueFrom` — all of which are name-stable and
    meaning-carrying, and all of which an earlier draft compared not at all,
    so that flipping `when` from `$(true)` to `$(false)` was equivalence.
    """
    body = {k: v for k, v in step.items() if k not in _FORGIVEN_STEP_KEYS}
    bindings = step.get('in')
    if isinstance(bindings, dict):
        body['in'] = {str(name): {k: v for k, v in value.items() if k != 'source'}
                      if isinstance(value, dict) else {}
                      for name, value in bindings.items()}
    return _canonical(body)


def _dataflow(document: Yaml) -> nx.DiGraph:
    """The document's dataflow DAG, in a form renaming cannot change.

    Nodes are steps, labelled with the tool stem *and the whole step body*
    (`_step_body`); edges are `producer/port` references out of `in:`,
    labelled with the `(output port, input name)` pairs that justify them.
    Every one of those labels is built from tool port names and literal
    values, which a namespace re-rooting does not touch, so the labelled graph
    is exactly the part of the document UP_TO_RENAMING is willing to compare.

    Folding the step body into the *node* label rather than comparing step
    bodies as a separate multiset is what keeps a body attached to its
    position in the graph: a workflow where the scattered step is the first
    and one where it is the third are then not equivalent, which a multiset
    could not see.

    A separate multiset-of-tool-stems check used to sit alongside this and has
    been deleted. It discriminated nothing: a stem-labelled node isomorphism
    already implies equal stem multisets, so the only thing the check
    contributed was the string in the `Divergence.path` — and the test that
    named it was asserting on that string rather than on a verdict, which is
    this project's recurring failure, in the artifact everything imports.

    A reference to a step this document does not contain becomes a node of its
    own rather than being dropped. Dropping it is what makes the "an edge
    points somewhere else" case in `MUST_DIFFER` invisible: with the target
    discarded, a document whose edge went nowhere looks the same as one with
    no edge at all. Such a node is labelled `('external', stem)` so it can
    never match a step the document really declares.

    A source that names no step at all — no `producer/port` split — is a
    workflow-level input, and it too becomes a node rather than being dropped.
    Dropping it made a whole class of rewiring invisible: the compiler emits
    `in: {n: {source: oracle__step__2__sink___n}}` for every unbound argument,
    so with those sources discarded a document feeding `sink.n` from the `int`
    input and `mk_file.name` from the `string` one was UP_TO_RENAMING-equal to
    the document that fed each from the other. The node carries the *shape*
    the document declares for that input, never its name, which is what keeps
    the forgiveness honest in both directions: differently-typed inputs cannot
    be swapped silently, and swapping two identically-shaped ones stays
    equivalent, because that swap is exactly what re-rooting a namespace does.
    """
    graph = nx.DiGraph()
    shapes = _declared_shapes(document, 'inputs')
    steps = document.get('steps')
    steps = steps if isinstance(steps, list) else []
    declared = [s for s in steps if isinstance(s, dict) and 'id' in s]
    for step in declared:
        graph.add_node(str(step['id']),
                       label=('step', _stem(str(step['id'])), _step_body(step)))
    for step in declared:
        consumer = str(step['id'])
        for name, source in _bindings(step):
            producer, separator, port = source.partition('/')
            if separator and port and producer:
                if producer not in graph:
                    graph.add_node(producer, label=('external', _stem(producer)))
            elif source:
                producer, port = _INPUT_NODE + source, ''
                if producer not in graph:
                    graph.add_node(producer, label=('input', shapes.get(source)))
            else:
                continue  # `in: {name: ''}` names nothing; there is no edge
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
    return sorted(_shape(port) for port in ports)


def _shape(port: Any) -> str:
    """One port reduced to `_SHAPE_KEYS`: what it is, never what it is called."""
    return (repr({k: port[k] for k in _SHAPE_KEYS if k in port})
            if isinstance(port, dict) else repr(port))


def _declared_shapes(document: Yaml, key: str) -> dict[str, str]:
    """Every declared port under `key`, by name, mapped to its shape.

    The name is the lookup key and never the compared value — `_dataflow` uses
    this only to answer "what shape is the input this source names?", which is
    the part of a workflow-level reference that survives a renaming. Both
    surface forms, for the same reason `_port_shapes` reads both: the compiler
    emits the mapping form, and CWL admits the array-of-records form.
    """
    node = document.get(key)
    if isinstance(node, dict):
        return {str(name): _shape(port) for name, port in node.items()}
    if isinstance(node, list):
        return {str(port['id']): _shape(port) for port in node
                if isinstance(port, dict) and 'id' in port}
    return {}


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


def _same_dag(left: Yaml, right: Yaml) -> Divergence | None:
    """Whether two workflows have the same shape under renaming.

    Isomorphism alone is too weak and `test_inline_subworkflows` uses it alone
    (tests/core/test_examples.py:418): a bare `DiGraphMatcher` accepts a graph
    in which every step has been replaced by a *different tool of the same
    arity*, which is precisely the kind of thing an IR migration could get
    wrong. The answer here is a *labelled* matcher — see `_dataflow` — not a
    second check bolted on beside a bare one.

    Two checks live outside the graph, because neither subject is a node:

      * `requirements` class names, compared as a set. These come from the CWL
        specification, not from the namespace, so unlike everything else at
        this strength their names really are comparable.
      * `inputs`/`outputs`, compared by count and by `type`/`format` with
        names dropped. A hermetic compilation shows those keys are namespaced
        (`oracle__step__1__mk_file___name`), so comparing them by name would
        reject every renaming this strength exists to forgive.
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

    one, two = _dataflow(left), _dataflow(right)
    matcher = isomorphism.DiGraphMatcher(
        one, two,
        node_match=lambda a, b: a.get('label') == b.get('label'),
        edge_match=lambda a, b: a.get('ports') == b.get('ports'))
    if not matcher.is_isomorphic():
        return Divergence(Strength.UP_TO_RENAMING, '<dag>',
                          sorted(one.nodes), sorted(two.nodes))
    return None
