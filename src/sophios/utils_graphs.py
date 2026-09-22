import graphviz
import networkx as nx

from .wic_types import GraphData, GraphReps


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
