"""Draw a compiled workflow, for `--graphviz` and `Workflow.draw`.

A projection made after compiling, and only when a drawing is asked for: the
compiled graph gives the steps and the edges between them, and the `wic: graphviz:`
block Resolve read for each workflow and step, the labels, styles and rank=same groups.
Nothing in the compiler reads a drawing.
"""

import graphviz

from .ir.artifacts import CompilationResult
from .ir.names import Names
from .ir.types import StepNode, WorkflowGraph
from .lang import StepKey
from .wic_types import GraphSettings


def draw(compiled: CompilationResult, settings: GraphSettings, name: str, *,
         background: str | None) -> graphviz.Digraph:
    """The drawing of `compiled`.

    Args:
        compiled (CompilationResult): A compilation.
        settings (GraphSettings): The `--graph_*` flags.
        name (str): The drawing's name, and its title when the root document gives none.
        background (str | None): The Graphviz colour the drawing is painted on, or None for a
            transparent one, which a slide takes the colour of. `--graph_dark_theme` picks the
            foreground, so a dark background goes with it.

    Returns:
        graphviz.Digraph: The drawing, ready to save as DOT.
    """
    root = graphviz.Digraph(name=name)
    # newrank='True' ranks nodes globally (rather than per-cluster), which is
    # required for rank=same constraints to work across subgraphs/clusters.
    root.attr(newrank='True')
    root.attr(bgcolor=background or 'transparent')
    root.attr(fontcolor=_foreground(settings))
    root.subgraph(_cluster(compiled.graph, Names.of(compiled.graph), settings, name))
    return root


def _foreground(settings: GraphSettings) -> str:
    """The colour of text, box outlines and inferred edges: black, or white with `--graph_dark_theme`."""
    return 'white' if settings['graph_dark_theme'] else 'black'


def _explicit(settings: GraphSettings) -> str:
    """The colour of an explicit edge: blue, or with `--graph_dark_theme` a lighter one, as `blue` is too dark."""
    return 'lightskyblue' if settings['graph_dark_theme'] else 'blue'


def _cluster(graph: WorkflowGraph, names: Names, settings: GraphSettings, root: str) -> graphviz.Digraph:
    """`graph` as a cluster, titled unless its `wic: graphviz:` says otherwise, and each workflow it
    calls as a cluster of its own, down to `--graph_inline_depth`. The outermost is named and titled
    `root`; a called workflow takes its title and its DOT name from the step that calls it, so two
    calls to one workflow stay apart."""
    look = graph.graphviz
    call = graph.namespace.parts[-1] if graph.namespace.parts else None
    cluster = graphviz.Digraph(name=f'cluster_{names.qualified(call) if call else root}')
    cluster.attr(label=look.label if look.label is not None else (call.name if call else root))
    cluster.attr(color='lightblue')  # color of cluster subgraph outline
    if look.style is not None:
        cluster.attr(style=look.style)
    for step in graph.steps:
        cluster.node(names.qualified(step.id), **_box(graph, step, names, settings))
    _edges(cluster, graph, names, settings)
    if len(graph.namespace.parts) < settings['graph_inline_depth']:
        for child in graph.children:
            cluster.subgraph(_cluster(child, names, settings, root))
    authored = {StepKey(step.id.index, step.id.name): step.id for step in graph.steps if not step.synthesized}
    # An entry that addresses no step was reported when the workflow was compiled.
    same = [f'"{names.qualified(authored[key])}"' for key in look.ranksame or () if key in authored]
    if len(same) > 1:
        cluster.body.append(f'\t{{rank=same; {"; ".join(same)}}}\n')
    return cluster


def _box(graph: WorkflowGraph, step: StepNode, names: Names, settings: GraphSettings) -> dict[str, str]:
    """The attributes of `step`'s box. `--graph_label_stepname` labels it with its compiled CWL name;
    without it, a tool step's `wic: graphviz: label` wins over its id, and its `style` is appended.
    The box of a call to a subworkflow styled `invis` is hidden with it. A box is not filled, so it takes
    the background's colour, and its outline and label take the foreground's."""
    label = names.step(step.id) if settings['graph_label_stepname'] else step.id.name
    style = 'rounded'
    called = graph.child(step.id)
    if called is not None:
        style += ', invis' if 'invis' in (called.graphviz.style or '') else ''
    else:
        look = step.graphviz
        label = label if settings['graph_label_stepname'] or look.label is None else look.label
        style += f", {look.style}" if look.style is not None else ''
    return {'label': label, 'shape': 'box', 'style': style,
            'color': _foreground(settings), 'fontcolor': _foreground(settings)}


def _edges(cluster: graphviz.Digraph, graph: WorkflowGraph, names: Names, settings: GraphSettings) -> None:
    """Draw `graph`'s edges into `cluster`: one per pair of steps, or one per port with `--graph_label_edges`."""
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        source = names.qualified(edge.source.step)
        sink = names.qualified(edge.sink.step)
        # Explicit edges are blue; an inferred one takes the foreground
        # colour, as docs/drawing.md says, so a reader can tell
        # what the document said from what the compiler decided.
        attrs = {'color': _explicit(settings)}
        if edge.inferred:
            attrs['color'] = _foreground(settings)
        if settings['graph_label_edges']:
            attrs['label'] = names.port(edge.source.port)
            attrs['fontcolor'] = _foreground(settings)
        elif (source, sink) in seen:
            continue
        seen.add((source, sink))
        if source != sink:
            cluster.edge(source, sink, **attrs)
