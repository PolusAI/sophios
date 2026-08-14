import graphviz
import networkx as nx

from .wic_types import (GraphData, GraphReps, GraphSettings, Namespaces)


def _collapsed_node_name(nss: Namespaces, graph_inline_depth: int) -> str:
    """Collapse a namespace path down to the node name used at the given inline depth.

    Args:
        nss (Namespaces): The namespaces associated with a node
        graph_inline_depth (int): The depth below which details are hidden

    Returns:
        str: The (possibly truncated) node name
    """
    return '___'.join(nss[:(1 + graph_inline_depth)])


def add_graph_edge(graph_settings: GraphSettings, graph: GraphReps,
                   nss1: Namespaces, nss2: Namespaces,
                   label: str, color: str = '') -> None:
    """Adds edges to (all of) our graph representations, with the ability to
    collapse all nodes below a given depth to a single node.

    This function utilizes the fact that nodes have been carefully designed to
    have unique, hierarchical names. If we want to hide all of the details
    below a given depth, we can simply truncate each of the namespaces!
    (and do the same when creating the nodes)

    Args:
        graph_settings (GraphSettings): The settings for graphviz visualization
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        nss1 (Namespaces): The namespaces associated with the first node
        nss2 (Namespaces): The namespaces associated with the second node
        label (str): The edge label
        color (str, optional): The edge color
    """
    if color == '':
        color = 'black' if graph_settings['graph_dark_theme'] else 'white'
    edge_node1 = _collapsed_node_name(nss1, graph_settings['graph_inline_depth'])
    edge_node2 = _collapsed_node_name(nss2, graph_settings['graph_inline_depth'])
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    graphdata = graph.graphdata
    edge_exists = graph_nx.has_edge(edge_node1, edge_node2)

    # A workflow can connect several ports between the same pair of steps. At
    # the collapsed step level those connections describe one dependency, and
    # drawing each port connection as a separate unlabeled edge produces
    # indistinguishable parallel arrows. Preserve parallel edges when labels
    # are requested because the labels carry the port-level distinction.
    if edge_exists and not graph_settings['graph_label_edges']:
        return

    attrs = {}
    # Hide internal self-edges
    if edge_node1 != edge_node2:
        attrs = {'color': color}
        if graph_settings['graph_label_edges']:
            attrs['label'] = label

        graph_gv.edge(edge_node1, edge_node2, **attrs)
    graph_nx.add_edge(edge_node1, edge_node2)
    graphdata.edges.append((edge_node1, edge_node2, attrs))


def flatten_graphdata(graphdata: GraphData, parent: str = '') -> GraphData:
    """Flattens graphdata by recursively inlineing all subgraphs.

    Args:
        graphdata (GraphData): A data structure which contains recursive subgraphs and other metadata.
        parent (str, optional): The name of the parent graph is encoded into the node attributes so that\n
        the subgraph information can be preserved after flattening. (Used for cytoscape) Defaults to ''.

    Returns:
        GraphData: A GraphDath instance with all of the recursive instances inlined
    """
    subgraphs = [flatten_graphdata(subgraph, str(graphdata.name)) for subgraph in graphdata.subgraphs]

    g_d = GraphData(str(graphdata.name))

    for subgraph in subgraphs:
        # We need to add a placeholder node for each subgraph first
        attrs = {} if parent == '' else {'parent': parent}
        # NOTE: This does not yet fully respect the --graph_inline_depth setting.
        g_d.nodes.append((subgraph.name, attrs))

    for subgraph in subgraphs:
        # Then we can add the nodes and edges from the subgraphs.
        # (Otherwise, cytoscape won't render the subgraphs correctly.)
        for (subnode, subattrs) in subgraph.nodes:
            g_d.nodes.append((subnode, subattrs))
        for (subnode1, subnode2, subattrs) in subgraph.edges:
            g_d.edges.append((subnode1, subnode2, subattrs))

    # Finally, add the nodes and edges from the current graph
    for (node, attrs) in graphdata.nodes:
        attrs['parent'] = graphdata.name
        g_d.nodes.append((node, attrs))
    for (node1, node2, attrs) in graphdata.edges:
        g_d.edges.append((node1, node2, attrs))

    return g_d


def _add_ranksame(graph: GraphReps, names: list[str]) -> None:
    """Align the given node names on the same graphviz rank, if there is more than one.

    See https://stackoverflow.com/questions/6824431/placing-clusters-on-the-same-rank-in-graphviz

    Args:
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        names (list[str]): The node names to align on the same rank
    """
    if len(names) > 1:
        nodes_same_rank = '\t{rank=same; ' + '; '.join(names) + '}\n'
        graph.graphviz.body.append(nodes_same_rank)
        graph.graphdata.ranksame = names


def add_subgraphs(graph_settings: GraphSettings,
                  graph: GraphReps,
                  sibling_subgraphs: list[GraphReps],
                  namespaces: Namespaces,
                  step_1_names: list[str],
                  steps_ranksame: list[str]) -> None:
    """Add all subgraphs to the current graph, except for GraphViz subgraphs
    below a given depth, which allows us to hide irrelevant details.

    Args:
        graph_settings (GraphSettings): The settings for graphviz visualization
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        sibling_subgraphs (list[Graph]): The subgraphs of the immediate children of the current workflow
        namespaces (Namespaces): Specifies the path in the AST of the current subworkflow
        step_1_names (list[str]): The names of the first step
        steps_ranksame (list[str]): Additional node names to be aligned using ranksame
    """
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    # Add the cluster subgraphs to the main graph, but we need to add them in
    # reverse order to trick the graphviz layout algorithm.
    for sibling in sibling_subgraphs[::-1]:  # Reverse!
        sib_graph_gv, sib_graph_nx, _sib_graphdata = sibling
        if len(namespaces) < graph_settings['graph_inline_depth']:
            graph_gv.subgraph(sib_graph_gv)
        graph_nx.add_nodes_from(sib_graph_nx.nodes)
        graph_nx.add_edges_from(sib_graph_nx.edges)
    for sibling in sibling_subgraphs:
        graph.graphdata.subgraphs.append(sibling.graphdata)
    # Align the cluster subgraphs using the same rank as the first node of each subgraph.
    if len(namespaces) < graph_settings['graph_inline_depth']:
        step_1_names_display = [name for name in step_1_names if len(
            name.split('___')) < 2 + graph_settings['graph_inline_depth']]
        _add_ranksame(graph, step_1_names_display)
        _add_ranksame(graph, steps_ranksame)


def get_graph_reps(name: str) -> GraphReps:
    """Initialize graph representations

    Args:
        name (str): The name of the graph

    Returns:
        GraphReps: A tuple of graph representations
    """
    graph_gv = graphviz.Digraph(name=f'cluster_{name}')
    graph_gv.attr(newrank='True')
    graph_nx = nx.DiGraph()
    graphdata = GraphData(str(name))
    return GraphReps(graph_gv, graph_nx, graphdata)
