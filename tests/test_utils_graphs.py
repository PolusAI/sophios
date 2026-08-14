from typing import Any

from sophios.utils_graphs import add_graph_edge, get_graph_reps


def _graph_settings(*, label_edges: bool) -> dict[str, Any]:
    return {
        'graph_dark_theme': False,
        'graph_inline_depth': 1,
        'graph_label_edges': label_edges,
        'graph_label_stepname': False,
        'graph_show_inputs': False,
        'graph_show_outputs': False,
    }


def test_unlabeled_graph_deduplicates_collapsed_step_dependencies() -> None:
    """Multiple port links should render as one unlabeled step dependency."""
    graph = get_graph_reps('workflow')
    settings = _graph_settings(label_edges=False)

    add_graph_edge(settings, graph, ['workflow', 'prepare'], ['workflow', 'report'], 'summary')
    add_graph_edge(settings, graph, ['workflow', 'prepare'], ['workflow', 'report'], 'status')

    graphviz_edges = [line for line in graph.graphviz.body if '->' in line]
    assert len(graphviz_edges) == 1
    assert len(graph.graphdata.edges) == 1
    assert list(graph.networkx.edges) == [('workflow___prepare', 'workflow___report')]


def test_labeled_graph_preserves_port_level_edges() -> None:
    """Port labels should preserve distinct connections between the same steps."""
    graph = get_graph_reps('workflow')
    settings = _graph_settings(label_edges=True)

    add_graph_edge(settings, graph, ['workflow', 'prepare'], ['workflow', 'report'], 'summary')
    add_graph_edge(settings, graph, ['workflow', 'prepare'], ['workflow', 'report'], 'status')

    graphviz_edges = [line for line in graph.graphviz.body if '->' in line]
    assert len(graphviz_edges) == 2
    assert len(graph.graphdata.edges) == 2
