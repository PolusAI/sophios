import argparse
from pathlib import Path
from typing import List, Dict, Any

import graphviz
import networkx as nx

from .wic_types import (GraphData, GraphReps, Json, Namespaces, Tool, Tools)


def add_graph_edge(graph_settings: Dict[str, Any], graph: GraphReps,
                   nss1: Namespaces, nss2: Namespaces,
                   label: str, color: str = '') -> None:
    """Adds edges to (all of) our graph representations, with the ability to
    collapse all nodes below a given depth to a single node.

    This function utilizes the fact that nodes have been carefully designed to
    have unique, hierarchical names. If we want to hide all of the details
    below a given depth, we can simply truncate each of the namespaces!
    (and do the same when creating the nodes)

    Args:
        args (argparse.Namespace): The command line arguments
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        nss1 (Namespaces): The namespaces associated with the first node
        nss2 (Namespaces): The namespaces associated with the second node
        label (str): The edge label
        color (str, optional): The edge color
    """
    if color == '':
        color = 'black' if graph_settings['graph_dark_theme'] else 'white'
    nss1 = nss1[:(1 + graph_settings['graph_inline_depth'])]
    edge_node1 = '___'.join(nss1)
    nss2 = nss2[:(1 + graph_settings['graph_inline_depth'])]
    edge_node2 = '___'.join(nss2)
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    graphdata = graph.graphdata
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

    # NOTE: Even though all of the following default list arguments are [],
    # you MUST explicitly supply the empty lists!!! Otherwise, after
    # instantiation, the lists will contain values from previous instances!!!
    # This shallow copy causes an infinite loop because as we copy nodes,
    # they end up getting appended to the original lists!
    # This makes absolutely no sense. Since the lists are defined at the
    # instance level (NOT the class level), there should be zero sharing!
    g_d = GraphData(str(graphdata.name), [], [], [], [])  # This is fine
    # g_d = GraphData(str(graphdata.name)) # This is NOT fine!
    # i.e. The following statement will NOT print zeros (in the second case)!
    # print(g_d.name, len(g_d.nodes), len(g_d.edges), len(g_d.subgraphs), len(g_d.ranksame))

    for subgraph in subgraphs:
        # We need to add a placeholder node for each subgraph first
        attrs = {} if parent == '' else {'parent': parent}
        # NOTE: This does not yet work with args.graph_inline_depth
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


def graphdata_to_cytoscape(graphdata: GraphData) -> Json:
    """Converts a flattened graph into cytoscape json format.

    Args:
        graphdata (GraphData): A flattened GraphData instance

    Returns:
        Json: A Json object compatible with cytoscape.
    """
    nodes = []
    for (node, attrs) in list(graphdata.nodes):
        nodes.append({'data': {'id': node, **attrs}})
    edges = []
    for (node1, node2, attrs) in list(graphdata.edges):
        edges.append({'data': {'source': node1, 'target': node2, **attrs}})
    return {'nodes': nodes, 'edges': edges}


def make_tool_dag(tool_stem: str, tool: Tool, graph_dark_theme: bool) -> None:
    """Uses the `dot` executable from the graphviz package to make a Directed
    Acyclic Graph corresponding to the given CWL CommandLineTool

    Args:
        tool_stem (str): The name of the Tool
        tool (Tool): The CWL ComandLineTool
        graph_dark_theme (bool): See args.graph_dark_theme
    """
    (tool_path, tool_cwl) = tool
    yaml_path = f'autogenerated/DAG/{tool_path}'
    Path(yaml_path).parent.mkdir(parents=True, exist_ok=True)
    graph = graphviz.Digraph(name=yaml_path)
    graph.attr(bgcolor="transparent")  # Useful for making slides
    font_edge_color = 'black' if graph_dark_theme else 'white'
    graph.attr(fontcolor=font_edge_color)
    graph.attr(rankdir='LR')
    attrs = {'shape': 'box', 'style': 'rounded, filled'}
    graph.node(tool_stem, fillcolor='lightblue', **attrs)
    for input_cwl in tool_cwl['inputs']:
        input_initial_ns = input_cwl.split('___')[0].split('__')[0]
        input_no_initial_ns = input_cwl.replace(f'{input_initial_ns}__', '')
        # Hide optional inputs that could be confusing.
        if not 'output' in input_no_initial_ns:
            graph.node(f'input_{input_cwl}', label=input_no_initial_ns, fillcolor='lightgreen', **attrs)
            graph.edge(f'input_{input_cwl}', tool_stem, color=font_edge_color)
    for output_cwl in tool_cwl['outputs']:
        output_initial_ns = output_cwl.split('___')[0].split('__')[0]
        output_no_initial_ns = output_cwl.replace(f'{output_initial_ns}__', '')
        graph.node(f'output_{output_cwl}', label=output_no_initial_ns, fillcolor='lightyellow', **attrs)
        graph.edge(tool_stem, f'output_{output_cwl}', color=font_edge_color)
    # NOTE: Since there may be many inputs/outputs and thus edges in complex subworkflows,
    # the layout algorithm for this this .render() call may be very slow! (10+ seconds)
    graph.render(format='png')


def make_plugins_dag(tools: Tools, graph_dark_theme: bool) -> None:
    """Uses the `neato` executable from the graphviz package to make a Directed
    Acyclic Graph consisting of a node for each CWL CommandLineTool and no edges.

    Args:
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()
        graph_dark_theme (bool): See args.graph_dark_theme
    """
    # NOTE: Do not use the default 'dot' engine. Use neato / fdp / sfdp
    # and set pack=0 to remove the massive blank space around each node.
    # Also note that despite my best efforts, I cannot force graphviz to
    # change the aspect ratio (to double the width for making slides)
    # without simply stretching it and distorting the nodes, so we can just
    # partition the tools into two squares and display them side by side.
    num_tools_half = int(len(list(tools)) / 2)
    for i in [0, 1]:
        yaml_path = f'autogenerated/DAG/plugins{i}'
        Path(yaml_path).mkdir(parents=True, exist_ok=True)
        graph = graphviz.Digraph(name=yaml_path)
        graph.engine = 'neato'
        graph.attr(pack='0')
        graph.attr(bgcolor="transparent")  # Useful for making slides
        font_edge_color = 'black' if graph_dark_theme else 'white'
        graph.attr(fontcolor=font_edge_color)
        for tool in list(tools)[i*num_tools_half:(i+1)*num_tools_half]:
            (tool_path, tool_cwl) = tools[tool]
            attrs = {'shape': 'box', 'style': 'rounded, filled'}
            graph.node(Path(tool_path).stem, fillcolor='lightblue', fontsize="24", width='0.75', **attrs)
        # NOTE: Since there are no edges in this DAG and thus no edge constraints,
        # the layout algorithm for this .render() call is fast. (about 1 second)
        graph.render(format='png')


def add_subgraphs(args: argparse.Namespace,
                  graph: GraphReps,
                  sibling_subgraphs: List[GraphReps],
                  namespaces: Namespaces,
                  step_1_names: List[str],
                  steps_ranksame: List[str]) -> None:
    """Add all subgraphs to the current graph, except for GraphViz subgraphs
    below a given depth, which allows us to hide irrelevant details.

    Args:
        args (argparse.Namespace): The command line arguments
        graph (GraphReps): A tuple of a GraphViz DiGraph and a networkx DiGraph
        sibling_subgraphs (List[Graph]): The subgraphs of the immediate children of the current workflow
        namespaces (Namespaces): Specifies the path in the AST of the current subworkflow
        step_1_names (List[str]): The names of the first step
        steps_ranksame (List[str]): Additional node names to be aligned using ranksame
    """
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    # Add the cluster subgraphs to the main graph, but we need to add them in
    # reverse order to trick the graphviz layout algorithm.
    for sibling in sibling_subgraphs[::-1]:  # Reverse!
        (sib_graph_gv, sib_graph_nx, sib_graphdata) = sibling
        if len(namespaces) < args.graph_inline_depth:
            graph_gv.subgraph(sib_graph_gv)
        graph_nx.add_nodes_from(sib_graph_nx.nodes)
        graph_nx.add_edges_from(sib_graph_nx.edges)
    for sibling in sibling_subgraphs:
        graph.graphdata.subgraphs.append(sibling.graphdata)
    # Align the cluster subgraphs using the same rank as the first node of each subgraph.
    # See https://stackoverflow.com/questions/6824431/placing-clusters-on-the-same-rank-in-graphviz
    if len(namespaces) < args.graph_inline_depth:
        step_1_names_display = [name for name in step_1_names if len(name.split('___')) < 2 + args.graph_inline_depth]
        if len(step_1_names_display) > 1:
            nodes_same_rank = '\t{rank=same; ' + '; '.join(step_1_names_display) + '}\n'
            graph_gv.body.append(nodes_same_rank)
            graph.graphdata.ranksame = step_1_names_display
        if len(steps_ranksame) > 1:
            nodes_same_rank = '\t{rank=same; ' + '; '.join(steps_ranksame) + '}\n'
            graph_gv.body.append(nodes_same_rank)
            graph.graphdata.ranksame = steps_ranksame


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
