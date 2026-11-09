"""When two compilations mean the same thing, at four strengths: IDENTICAL, UP_TO_ORDER, UP_TO_EMBEDDING,
UP_TO_RENAMING. Each forgives only what it names, with a reason: a normalisation without one hides real differences."""
import copy
from dataclasses import dataclass
from enum import IntEnum
from pathlib import PurePosixPath
from typing import Any, Final, Iterator

import networkx as nx
from networkx.algorithms import isomorphism

from sophios.ir.artifacts import CompilationArtifact
from sophios.utils import recursively_delete_dict_key
from sophios.wic_types import Yaml


class Strength(IntEnum):
    """How much two compilations may differ; an IntEnum, so the lattice is `<=` and higher is stricter."""

    #: Names may differ (step ids, port keys, `source` names, kept as shape-typed edges) and top-level keys beyond
    #: steps, inputs, outputs and requirements; the DAG and each node's tool may not: a split moves no edge.
    UP_TO_RENAMING = 1

    #: `run:` paths may differ: `run:` names the file a process is written to, which depends on the tree.
    UP_TO_EMBEDDING = 2

    #: Mapping key order may differ: a YAML mapping is unordered and CWL reads these documents as mappings.
    UP_TO_ORDER = 3

    #: Byte for byte, key order included, usable only because emission is canonical; order is compared here alone so an
    #: emission-order regression stays visible.
    IDENTICAL = 4


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
    """Whether two compiled workflows agree at `strength`: None, or the first `Divergence`, never a bare bool.
    Each strength forgives only what its member says; step keys are compared by exclusion, so an unknown key counts."""
    match strength:
        case Strength.IDENTICAL:
            return _first_difference(left, right, strength, '', ordered=True)
        case Strength.UP_TO_ORDER:
            return _first_difference(left, right, strength, '', ordered=False)
        case Strength.UP_TO_EMBEDDING:
            # `$namespaces` and `$schemas` go with `run:`: embedding moves a child's into its parent.
            return _first_difference(_without_embedding(left),
                                     _without_embedding(right),
                                     strength, '', ordered=False)
        case _:
            return _same_dag(left, right)


def _without_embedding(document: Any) -> Any:
    """A document with everything embedding decides removed."""
    return recursively_delete_dict_key('$schemas', recursively_delete_dict_key(
        '$namespaces', recursively_delete_dict_key('run', document)))


def _first_difference(left: Any, right: Any, strength: Strength, path: str, *,
                      ordered: bool) -> Divergence | None:
    """Where two documents first disagree, so the failure message is the diff; `ordered` (IDENTICAL only) also compares
    mapping key order."""
    # pylint: disable=too-many-return-statements  # one per node kind and way it differs
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
        # Order last: a missing key is reported as missing, not as a reordering.
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


#: The fields of a workflow-level port a renaming cannot touch; its key and `outputSource` are names.
_SHAPE_KEYS: Final[tuple[str, ...]] = ('type', 'format')

#: Prefixes no emitted step or port name can hold, so a port and a step sharing a name stay two nodes.
_INPUT_NODE: Final = '<input> '
_OUTPUT_NODE: Final = '<output> '


def _process(step: Yaml) -> Any:
    """The process a step runs, from its `run:` (a path's last segment, or the embedded process), never from its id;
    a path and an embedded copy label differently, which UP_TO_EMBEDDING forgives."""
    run = step.get('run')
    if isinstance(run, str):
        return PurePosixPath(run).name
    return _canonical(run)


def _steps_of(document: Yaml) -> list[Yaml]:
    """The document's list-form steps; any other shape, or a step without `id`, raises: read as no steps, two documents
    differing only in their steps would compare equal."""
    steps = document.get('steps', [])
    if not isinstance(steps, list):
        raise TypeError(
            f'steps: is {type(steps).__name__}, not a list. This relation compares '
            'compiled documents and the compiler emits list-form steps; a '
            'mapping-form steps: has to be desugared before it reaches here.')
    for step in steps:
        if not isinstance(step, dict) or 'id' not in step:
            raise TypeError(
                f'a step with no id: {step!r}. Emitted CWL always carries one, and a '
                'step without one has no node in the dataflow graph — so comparing '
                'the document at all would forgive it in silence.')
    return steps


def _in_of(step: Yaml) -> Yaml:
    """A step's mapping-form `in:`, or {}; the array form raises, as in `_steps_of`, or its bindings go uncompared."""
    bindings = step.get('in')
    if bindings is None:
        return {}
    if not isinstance(bindings, dict):
        raise TypeError(
            f"a step's in: is {type(bindings).__name__}, not a mapping. The compiler "
            'emits the mapping form; the array form has to be desugared before it '
            'reaches here, or it is forgiven in silence.')
    return bindings


def _bindings(step: Yaml) -> Iterator[tuple[str, str]]:
    """Every (input name, source) pair a step declares, `source` one string or a list."""
    for name, value in _in_of(step).items():
        inner = value.get('source') if isinstance(value, dict) else value
        for source in (inner if isinstance(inner, list) else [inner]):
            if isinstance(source, str):
                yield str(name), source


def _canonical(node: Any) -> Any:
    """A hashable rendering of a YAML fragment, key order normalised (UP_TO_RENAMING forgives it) and list order kept:
    `scatter: [a, b]` is not `scatter: [b, a]`."""
    if isinstance(node, dict):
        return tuple(sorted((str(k), _canonical(v)) for k, v in node.items()))
    if isinstance(node, list):
        return tuple(_canonical(v) for v in node)
    return repr(node)


#: The step keys UP_TO_RENAMING forgives, as an exclusion so an unknown key is compared: a wrongly compared key is a
#: loud false divergence, a wrongly forgiven one silent. Of `in`, only each binding's `source` is forgiven.
_FORGIVEN_STEP_KEYS: Final[tuple[str, ...]] = ('id', 'run', 'in')


def _step_body(step: Yaml) -> Any:
    """Everything in a step a renaming cannot change: `out`, `scatter`, `when`, a binding's `default`."""
    body = {k: v for k, v in step.items() if k not in _FORGIVEN_STEP_KEYS}
    if 'in' in step:
        # Only when present: `in: {}` differs from no `in:`.
        body['in'] = {str(name): {k: v for k, v in value.items() if k != 'source'}
                      if isinstance(value, dict) else {}
                      for name, value in _in_of(step).items()}
    return _canonical(body)


def _dataflow(document: Yaml) -> nx.DiGraph:
    """The labelled DAG a renaming cannot change: steps carry process and body, edges their port pairs; a workflow port
    is labelled by its shape, an absent producer by its whole name (never lumped), so a moved body or rewire
    diverges."""
    graph = nx.DiGraph()
    input_shapes = _declared_shapes(document, 'inputs')
    output_shapes = _declared_shapes(document, 'outputs')
    declared = _steps_of(document)
    for step in declared:
        graph.add_node(str(step['id']),
                       label=('step', _process(step), _step_body(step)))
    for step in declared:
        consumer = str(step['id'])
        for name, source in _bindings(step):
            found = _producer_of(graph, source, input_shapes)
            if found is not None:
                _connect(graph, found, consumer, name)
    for name, source in _output_sources(document):
        found = _producer_of(graph, source, input_shapes)
        if found is None:
            continue
        consumer = _OUTPUT_NODE + name
        if consumer not in graph:
            graph.add_node(consumer, label=('output', output_shapes.get(name)))
        # An output binds no name: the producing port alone tells two outputs of one step apart.
        _connect(graph, found, consumer, '')
    return graph


def _producer_of(graph: nx.DiGraph, source: str,
                 input_shapes: dict[str, str]) -> tuple[str, str] | None:
    """The (node, port) a `source:` comes out of, adding a node for an absent step or a workflow input; None for ''.
    Shared by step bindings and `outputSource`, so both read a reference alike."""
    producer, separator, port = source.partition('/')
    if separator and port and producer:
        if producer not in graph:
            graph.add_node(producer, label=('external', producer))
        return producer, port
    if source:
        node = _INPUT_NODE + source
        if node not in graph:
            graph.add_node(node, label=('input', input_shapes.get(source)))
        return node, ''
    return None


def _connect(graph: nx.DiGraph, producer: tuple[str, str], consumer: str, name: str) -> None:
    """Record one `(output port, input name)` pair on the producer's edge."""
    node, port = producer
    if graph.has_edge(node, consumer):
        graph.edges[node, consumer]['ports'].add((port, name))
    else:
        graph.add_edge(node, consumer, ports={(port, name)})


def _named_ports(document: Yaml, key: str) -> dict[str, Any]:
    """The ports under `key` by name, in either form CWL admits: a mapping, or records carrying `id`."""
    node = document.get(key)
    if isinstance(node, dict):
        return {str(name): port for name, port in node.items()}
    if isinstance(node, list):
        return {str(port['id']): port for port in node if isinstance(port, dict) and 'id' in port}
    return {}


def _output_sources(document: Yaml) -> Iterator[tuple[str, str]]:
    """Every (workflow output name, source) pair, `outputSource` one string or a list."""
    for name, port in _named_ports(document, 'outputs').items():
        inner = port.get('outputSource') if isinstance(port, dict) else None
        for source in (inner if isinstance(inner, list) else [inner]):
            if isinstance(source, str) and source:
                yield name, source


def _port_shapes(document: Yaml, key: str) -> list[str]:
    """The sorted shapes of the workflow-level ports under `key`, names dropped (the compiler namespaces them);
    safe since `_dataflow` carries which producer feeds which output."""
    node = document.get(key)
    ports = list(node.values()) if isinstance(node, dict) else (node if isinstance(node, list) else [])
    return sorted(_shape(port) for port in ports)


def _shape(port: Any) -> str:
    """One port reduced to `_SHAPE_KEYS`: what it is, never what it is called."""
    return (repr({k: port[k] for k in _SHAPE_KEYS if k in port})
            if isinstance(port, dict) else repr(port))


def _declared_shapes(document: Yaml, key: str) -> dict[str, str]:
    """Each declared port under `key`, by name, mapped to its shape; the name is only the lookup key."""
    return {name: _shape(port) for name, port in _named_ports(document, key).items()}


def _requirement_names(document: Yaml) -> list[str]:
    """The requirement class names, compared as names: CWL fixes them and the compiler never namespaces them."""
    node = document.get('requirements') or {}
    if isinstance(node, dict):
        return sorted(node)
    if isinstance(node, list):
        return sorted(str(r.get('class', r)) if isinstance(r, dict) else str(r) for r in node)
    return [repr(node)]


def _same_dag(left: Yaml, right: Yaml) -> Divergence | None:
    """Same shape under renaming: port shapes and requirement names, then a labelled isomorphism, since a bare one
    accepts every step swapped for another tool of the same arity."""
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


def _source_of(value: Any) -> str | None:
    """The `source` one `in:` binding names, in either surface form the compiler emits."""
    source = value.get('source') if isinstance(value, dict) else value
    return source if isinstance(source, str) else None


def _set_source(bindings: Yaml, name: str, source: str) -> None:
    """Rewrites one binding's source in place, keeping its own surface form."""
    value = bindings[name]
    if isinstance(value, dict):
        value['source'] = source
    else:
        bindings[name] = source


def _inlined_steps(wrapper_id: str, inner: Yaml, formal_to_actual: dict[str, str | None],
                   step_requirements: Yaml) -> list[Yaml]:
    """The steps of flattened `inner` as call `wrapper_id` inlines them, ids prefixed by the wrapper's: an id is unique
    in its document only."""
    inner_ids = {str(inner_step['id']) for inner_step in inner.get('steps', [])}
    inlined: list[Yaml] = []
    for inner_step in inner.get('steps', []):
        for name, value in inner_step.get('in', {}).items():
            source = _source_of(value)
            if source is not None and source in formal_to_actual:
                actual = formal_to_actual[source]
                if actual is not None:
                    _set_source(inner_step['in'], name, actual)
            elif source is not None and source.partition('/')[0] in inner_ids:
                _set_source(inner_step['in'], name, f'{wrapper_id}~{source}')
        inner_step['id'] = f'{wrapper_id}~{inner_step["id"]}'
        if step_requirements:
            inner_step['requirements'] = {**step_requirements, **(inner_step.get('requirements') or {})}
        inlined.append(inner_step)
    return inlined


def flatten_model(artifact: CompilationArtifact) -> Yaml:
    """The document with each subworkflow call whose boundary is proved removable inlined: an independent oracle for a
    split against an unsplit compilation at UP_TO_RENAMING; a call is known by its sub-tree's `class`."""
    document: Yaml = copy.deepcopy(artifact.cwl)
    steps: list[Yaml] = document.get('steps', [])
    children = artifact.children
    flat_steps: list[Yaml] = []
    #: `{wrapper}/{port}` -> the real producer's source, for each call inlined away.
    redirects: dict[str, str] = {}
    #: What the inlined subworkflows required (SubworkflowFeatureRequirement already stripped): a document declares the
    #: features its own steps use, so a requirement moves with its step.
    inherited_requirements: dict[str, Yaml] = {}
    inlined_any = False
    kept_any = False

    for step, child in zip(steps, children):
        if child.cwl.get('class') != 'Workflow':
            # A tool's file is named by where its step sits, which a split moves: label the step with the process.
            flat_steps.append({**step, 'run': copy.deepcopy(child.cwl)})
            continue
        if set(step) - {'id', 'in', 'run', 'out'} or (child.graph is not None and not child.graph.inlineable):
            # More than `in` and `out` on the call, or `inlineable: false`: the boundary is not proved removable.
            kept_any = True
            flat_steps.append(step)
            continue
        inlined_any = True
        inner = flatten_model(child)
        kept_any = kept_any or 'SubworkflowFeatureRequirement' in (inner.get('requirements') or {})
        # The engine reads a few requirements against the document; the rest go onto each step, its own entry winning.
        declared = dict(inner.get('requirements') or {})
        declared.pop('SubworkflowFeatureRequirement', None)
        document_features = ('InlineJavascriptRequirement', 'ScatterFeatureRequirement',
                             'StepInputExpressionRequirement', 'MultipleInputFeatureRequirement')
        inherited_requirements.update({k: v for k, v in declared.items() if k in document_features})
        step_requirements = {k: v for k, v in declared.items() if k not in document_features}
        # The wrapper's `in:` maps the subworkflow's formal inputs to their sources at this level.
        wrapper_id = step['id']
        inner_ids = {str(inner_step['id']) for inner_step in inner.get('steps', [])}
        flat_steps.extend(_inlined_steps(
            wrapper_id, inner, {name: _source_of(value) for name, value in step.get('in', {}).items()},
            step_requirements))
        # References to `{wrapper}/{port}` go straight to the real producer, now one of `flat_steps`.
        for out_name, out_value in inner.get('outputs', {}).items():
            producer, _, port = out_value['outputSource'].partition('/')
            redirects[f'{wrapper_id}/{out_name}'] = (
                f'{wrapper_id}~{producer}/{port}' if producer in inner_ids else out_value['outputSource'])

    for flat_step in flat_steps:
        for name, value in flat_step.get('in', {}).items():
            source = _source_of(value)
            if source is not None and source in redirects:
                _set_source(flat_step['in'], name, redirects[source])
    document['steps'] = flat_steps

    for out_value in document.get('outputs', {}).values():
        out_source = out_value.get('outputSource')
        if isinstance(out_source, str) and out_source in redirects:
            out_value['outputSource'] = redirects[out_source]

    if inlined_any:
        # No call left, so no SubworkflowFeatureRequirement; what the inlined subworkflows needed still applies.
        requirements = document.get('requirements')
        merged = dict(requirements) if isinstance(requirements, dict) else {}
        if not kept_any:
            merged.pop('SubworkflowFeatureRequirement', None)
        merged.update(inherited_requirements)
        document['requirements'] = merged

    return document
