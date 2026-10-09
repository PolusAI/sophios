"""Draw a compiled workflow, for `--graphviz`.

A projection made after compiling, and only when a drawing is asked for: the
compiled graph gives the steps and the edges between them, and the `wic: graphviz:`
block of each document it was compiled from, read as Resolve reads that document's
`wic:` block, the labels, styles and rank=same groups. Nothing in the compiler reads a drawing.
"""

import graphviz

from .ir.artifacts import CompilationResult
from .ir.frontdoor import SourceBundle
from .ir.names import Names
from .ir.resolve import RegistrySnapshot, called_document, graphviz_entry, ranksame, step_sidecar
from .ir.types import EdgeOrigin, Namespace, StepNode, WorkflowGraph
from .lang import StepKey, WicSidecar
from .wic_types import GraphSettings

#: The `wic:` block each workflow of a compiled tree is drawn with, by the workflow's namespace.
_Sidecars = dict[Namespace, WicSidecar | None]


def draw(compiled: CompilationResult, bundle: SourceBundle, settings: GraphSettings,
         name: str) -> graphviz.Digraph:
    """The drawing of `compiled`, the result of compiling `bundle`.

    Args:
        compiled (CompilationResult): What compiling `bundle` returned.
        bundle (SourceBundle): The documents `compiled` was compiled from.
        settings (GraphSettings): The `--graph_*` flags.
        name (str): The drawing's name, and its title when the root document gives none.

    Returns:
        graphviz.Digraph: The drawing, ready to save as DOT.
    """
    root = graphviz.Digraph(name=name)
    # newrank='True' ranks nodes globally (rather than per-cluster), which is
    # required for rank=same constraints to work across subgraphs/clusters.
    root.attr(newrank='True')
    root.attr(bgcolor='transparent')  # Useful for making slides
    root.attr(fontcolor='black' if settings['graph_dark_theme'] else 'white')
    assert bundle.parsed.document is not None  # `compiled` was compiled from it
    document = called_document(bundle.parsed.document, None, bundle.registry)
    sidecars = _sidecars(compiled.graph, document.sidecar if document else None, bundle.registry)
    root.subgraph(_cluster(compiled.graph, sidecars, Names.of(compiled.graph), settings, name))
    return root


def _sidecars(graph: WorkflowGraph, sidecar: WicSidecar | None, registry: RegistrySnapshot) -> _Sidecars:
    """`sidecar` for `graph`, and for each workflow it calls, that workflow's `wic:` block with
    the calling step's entry merged in."""
    found: _Sidecars = {graph.namespace: sidecar}
    calls = {step.id: step for step in graph.steps}
    for child in graph.children:
        step = calls[child.namespace.parts[-1]]
        assert step.run is not None  # a compiled step runs something
        workflow = registry.workflow(step.run.process_id)
        assert workflow is not None and workflow.parsed.document is not None  # Resolve read it
        called = called_document(workflow.parsed.document, _entry(graph, sidecar, step), registry)
        found |= _sidecars(child, called.sidecar if called else None, registry)
    return found


def _entry(graph: WorkflowGraph, sidecar: WicSidecar | None, step: StepNode) -> WicSidecar | None:
    """`step`'s entry under `sidecar`'s `steps:`, addressed as Resolve addresses it; none for a step Infer inserted."""
    if step.synthesized:
        return None
    occurrences = sum(not other.synthesized and other.id.name == step.id.name for other in graph.steps)
    return step_sidecar(sidecar, step.id.index, step.id.name, occurrences)


def _cluster(graph: WorkflowGraph, sidecars: _Sidecars, names: Names, settings: GraphSettings,
             title: str) -> graphviz.Digraph:
    """`graph` as a cluster, titled `title` unless its `wic: graphviz:` says otherwise, and each
    workflow it calls as a cluster of its own, down to `--graph_inline_depth`."""
    sidecar = sidecars[graph.namespace]
    look = graphviz_entry(sidecar)
    cluster = graphviz.Digraph(name=f'cluster_{graph.name}')
    cluster.attr(label=look.get('label', title))
    cluster.attr(color='lightblue')  # color of cluster subgraph outline
    if 'style' in look:
        cluster.attr(style=look['style'])
    for step in graph.steps:
        cluster.node(names.qualified(step.id), **_box(graph, step, sidecars, names, settings))
    _edges(cluster, graph, names, settings)
    if len(graph.namespace.parts) < settings['graph_inline_depth']:
        for child in graph.children:
            cluster.subgraph(_cluster(child, sidecars, names, settings, child.namespace.parts[-1].name))
    authored = {StepKey(step.id.index, step.id.name): step.id for step in graph.steps if not step.synthesized}
    # An entry that addresses no step was reported when the workflow was compiled.
    same = [f'"{names.qualified(authored[key])}"' for key in ranksame(sidecar) if key in authored]
    if len(same) > 1:
        cluster.body.append(f'\t{{rank=same; {"; ".join(same)}}}\n')
    return cluster


def _box(graph: WorkflowGraph, step: StepNode, sidecars: _Sidecars, names: Names,
         settings: GraphSettings) -> dict[str, str]:
    """The attributes of `step`'s box. `--graph_label_stepname` labels it with its generated name;
    without it, a tool step's `wic: graphviz: label` wins over its id, and its `style` is appended.
    The box of a call to a subworkflow styled `invis` is hidden with it."""
    label = names.step(step.id) if settings['graph_label_stepname'] else step.id.name
    style = 'rounded, filled'
    if step.run is not None and step.run.child is not None:
        called = graphviz_entry(sidecars[graph.namespace.child(step.id)])
        style += ', invis' if 'invis' in called.get('style', '') else ''
    else:
        look = graphviz_entry(_entry(graph, sidecars[graph.namespace], step))
        label = label if settings['graph_label_stepname'] else look.get('label', label)
        style += f", {look['style']}" if 'style' in look else ''
    return {'label': label, 'shape': 'box', 'style': style, 'fillcolor': 'lightblue'}


def _edges(cluster: graphviz.Digraph, graph: WorkflowGraph, names: Names, settings: GraphSettings) -> None:
    """Draw `graph`'s edges into `cluster`: one per pair of steps, or one per port with `--graph_label_edges`."""
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        source = names.qualified(edge.source.step)
        sink = names.qualified(edge.sink.step)
        # Explicit edges are blue; an inferred one takes the theme's font
        # colour, as docs/tutorials/multistep.md says, so a reader can tell
        # what the document said from what the compiler decided.
        attrs = {'color': 'blue'}
        if edge.origin is EdgeOrigin.INFERRED:
            attrs['color'] = 'black' if settings['graph_dark_theme'] else 'white'
        if settings['graph_label_edges']:
            attrs['label'] = names.port(edge.source.port)
        elif (source, sink) in seen:
            continue
        seen.add((source, sink))
        if source != sink:
            cluster.edge(source, sink, **attrs)
