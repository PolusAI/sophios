"""Lowering a parsed document to a graph.

Five claims: lowering is total, a malformed graph cannot be constructed, every
edge connects ports that exist, passthrough is never read, and the result does
not depend on iteration order.

Nothing here compiles -- lowering reads the AST alone, so these need no tool
registry, filesystem or config.
"""
import ast as pyast
from typing import Any

import pytest
from hypothesis import given

from sophios.ir import (
    Edge,
    Namespace,
    Port,
    PortId,
    PortType,
    StepNode,
    WorkflowGraph,
)
from sophios.ir.lower import lower
from sophios.lang.nodes import Document

from . import ast_strategies as strat
from .hermetic import COVERAGE, ORACLE
from .source_scan import REPO_ROOT, parsed


def _ports_of(graph: WorkflowGraph) -> set[PortId]:
    """Every port identity the graph's steps declare."""
    return {p.id for step in graph.steps for p in step.inputs + step.outputs}


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_every_parseable_document_lowers_or_diagnoses(document: Document) -> None:
    """Lowering is total, in the sense `parse` is: what it cannot represent
    comes back as a diagnostic, never as an exception."""
    result = lower(document)
    assert result.graph is not None or len(result.diagnostics) > 0
    if result.graph is None:
        assert result.diagnostics.has_errors


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_every_edge_connects_ports_that_exist(document: Document) -> None:
    """An edge names ports the graph declares, at both ends.

    Unchecked, such an edge reaches emission as a dangling `source:`, which CWL
    accepts and a runner then fails on.
    """
    result = lower(document)
    if result.graph is None:
        return
    known = _ports_of(result.graph)
    for edge in result.graph.edges:
        assert edge.source in known and edge.sink in known


@pytest.mark.fast
@given(strat.documents())
@ORACLE
def test_lowering_is_deterministic(document: Document) -> None:
    """The same document lowers to the same graph, twice.

    Two lowerings rather than two interpreters: what could vary is set or dict
    iteration, which varies within one process as readily as across two.
    """
    first, second = lower(document), lower(document)
    assert (first.graph is None) == (second.graph is None)
    if first.graph is not None and second.graph is not None:
        assert first.graph.steps == second.graph.steps
        assert first.graph.edges == second.graph.edges
        assert first.graph.obligations == second.graph.obligations


@pytest.mark.fast
@pytest.mark.parametrize('build', [
    pytest.param(lambda p, q: Edge(p.id, p.id),
                 id='an edge from a port to itself'),
    pytest.param(lambda p, q: StepNode(Namespace(), 'elsewhere', inputs=(q,)),
                 id='a step whose port names another step'),
    pytest.param(lambda p, q: Namespace(('a___b',)),
                 id='a namespace part that cannot be split back out'),
    pytest.param(lambda p, q: PortType(declared='File', array_depth=-1),
                 id='a negative array depth'),
    pytest.param(lambda p, q: PortId(Namespace(), '', 'f'),
                 id='a port belonging to no step'),
    pytest.param(lambda p, q: WorkflowGraph(
        Namespace(('wf',)),
        steps=(StepNode(Namespace(('wf',)), 'mk', outputs=(p,)),),
        edges=(Edge(p.id, q.id),)),
        id='a graph whose edge names a port no step declares'),
    pytest.param(lambda p, q: WorkflowGraph(
        Namespace(('wf',)),
        steps=(StepNode(Namespace(('wf',)), 'mk'),
               StepNode(Namespace(('wf',)), 'mk'))),
        id='a graph with the same step name twice'),
])
def test_a_malformed_graph_cannot_be_constructed(build: Any) -> None:
    """The invariants live in `__post_init__`, so nothing can skip them:
    construction is the one moment every caller passes through."""
    ns = Namespace(('wf',))
    producer = Port(PortId(ns, 'mk', 'file'), PortType('File'))
    consumer = Port(PortId(ns, 'use', 'f'), PortType('File'))
    with pytest.raises(ValueError):
        build(producer, consumer)


@pytest.mark.fast
def test_a_repeated_step_name_is_reported_at_the_step() -> None:
    """Two steps of one name is a diagnostic, not a refused construction, so
    the report carries the second step's position rather than the graph's."""
    from sophios.lang.parser import parse  # pylint: disable=import-outside-toplevel

    document = parse('steps:\n- id: s\n  in: {}\n- id: s\n  in: {}\n', 'repeat.wic').document
    assert document is not None
    result = lower(document)
    assert result.graph is None and result.diagnostics.has_errors


@pytest.mark.fast
def test_a_reference_to_an_undefined_edge_becomes_an_obligation() -> None:
    """What a subworkflow owes its includer has a name and a type, which is
    what lets `Link` say whether every one was discharged."""
    from sophios.lang.parser import parse  # pylint: disable=import-outside-toplevel

    document = parse('steps:\n- id: s\n  in:\n    f: !* from_parent\n', 'owes.wic').document
    assert document is not None
    graph = lower(document).graph
    assert graph is not None
    assert [o.name for o in graph.obligations] == ['from_parent']
    assert graph.edges == ()


@pytest.mark.fast
def test_a_reference_to_a_defined_edge_becomes_an_edge() -> None:
    """The same reference, when this document produces what it names."""
    from sophios.lang.parser import parse  # pylint: disable=import-outside-toplevel

    document = parse(
        'steps:\n- id: mk\n  out:\n  - file: !& e\n- id: use\n  in:\n    f: !* e\n',
        'binds.wic').document
    assert document is not None
    graph = lower(document).graph
    assert graph is not None
    assert graph.obligations == ()
    assert len(graph.edges) == 1
    assert graph.edges[0].source.step == 'mk' and graph.edges[0].sink.step == 'use'


@pytest.mark.fast
def test_nothing_in_the_ir_reads_an_opaque_payload() -> None:
    """`OpaqueCwl` is carried, never inspected.

    Static rather than by example: an attribute access or subscript on a
    passthrough value is invisible to any test that only lowers documents.
    """
    carriers = {'passthrough', 'interpreted', 'declared'}
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / 'src' / 'sophios' / 'ir').rglob('*.py')):
        for node in pyast.walk(parsed(path)):
            if not isinstance(node, (pyast.Subscript, pyast.Attribute)):
                continue
            target = node.value
            if isinstance(target, pyast.Attribute) and target.attr in carriers:
                offenders.append(f'{path.name}:{node.lineno} reads {target.attr}')
    assert not offenders, 'the IR reads a payload it is supposed to carry:\n  ' + '\n  '.join(offenders)
