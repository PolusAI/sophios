"""Lowering a resolved document to a graph: total, every reference names an existing port, every authored binding
kept, deterministic; and each document of tests/core/data/lower lowers to the diagnostics its expected.yaml states.
The test-side resolver needs no registry, filesystem or config."""
import json
from dataclasses import replace

import pytest
from hypothesis import example, given

from sophios.ir.declarations import port_declaration
from sophios.ir.lower import lower
from sophios.ir.resolve import ResolvedDocument, ResolvedPort, ResolvedProcess, ResolvedStep
from sophios.ir.types import AuthoredName, Direction, PortId, RegistryKey, StepId, WorkflowGraph
from sophios.lang.nodes import Document, Step
from sophios.lang.parser import parse

from . import ast_strategies as strat
from .cases import DATA, diagnostic, match
from .hermetic import COVERAGE, ORACLE
from .helpers import stated
from .strategies import shapes


def _declared_inputs(step: Step) -> tuple[str, ...]:
    """What a process declares, as far as the step shows it: the inputs it
    binds, and those it scatters over whether bound or not."""
    scatter = dict(step.interpreted).get('scatter')
    scattered = [name for name in scatter if isinstance(name, str)] if isinstance(scatter, list) else []
    named = [name for name, _ in step.inputs] + scattered
    return tuple(dict.fromkeys(named))


def _resolved(document: Document) -> ResolvedDocument:
    """Build Lower's typed input independently of production Resolve."""
    declaration = port_declaration(None)
    steps = tuple(
        ResolvedStep(
            step,
            ResolvedProcess(
                RegistryKey('test', step.id),
                f'{step.id}.cwl',
                tuple(ResolvedPort(name, declaration) for name in _declared_inputs(step)),
                tuple(ResolvedPort(binding.name, declaration) for binding in step.outputs),
                {'class': 'CommandLineTool'},
            ),
        )
        for step in document.steps
    )
    return ResolvedDocument('probe', document, steps, '0.0.1')


@pytest.mark.fast
def test_a_document_lowers_to_the_diagnostics_its_family_states() -> None:
    """Each `.wic` of tests/core/data/lower, parsed and lowered, gives exactly the diagnostics expected.yaml lists for
    it, by code, severity and line, and a graph exactly when none is an error."""
    expected = stated(DATA / 'lower')
    assert sorted(expected) == sorted(path.name for path in (DATA / 'lower').glob('*.wic'))
    for name, diagnostics in expected.items():
        document = parse((DATA / 'lower' / name).read_text(encoding='utf-8'), name).document
        assert document is not None, name
        result = lower(_resolved(document))
        found = [diagnostic(each) for each in result.diagnostics]
        assert match(diagnostics, found), (name, found)
        assert (result.graph is None) is result.diagnostics.has_errors, name


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_a_well_formed_document_lowers_whole(document: Document) -> None:
    """`documents()` draws only well-formed documents, so each lowers to a graph with a step for each of its steps,
    never a diagnostic or an exception; the same graph twice, in full, mappings included, so none varies with set
    iteration; with every binding written in `in:`, so Emit never reads the AST again; and with every reference its
    edges, obligations and mappings hold naming a port that exists, which a dangling `source:` would not. A graph
    refuses to be built holding a port no step declares, or one step twice."""
    resolved = _resolved(document)
    result = lower(resolved)
    assert result.graph is not None, [d.code.value for d in result.diagnostics]
    assert len(result.graph.steps) == len(document.steps)
    assert lower(resolved).graph == result.graph
    assert [b.value for step in result.graph.steps for b in step.bindings] == \
        [value for step in document.steps for _, value in step.inputs]
    graph = result.graph
    known = {p.id for s in graph.steps for p in s.inputs + s.outputs}
    for where, _name, port_id in graph._references():  # pylint: disable=protected-access
        assert port_id in known, f'{where} names {port_id}'
    assert {port for edge in graph.edges for port in (edge.source, edge.sink)} <= known
    ghost = PortId(StepId(graph.namespace, len(graph.steps) + 1, 'ghost'), Direction.INPUT, AuthoredName('p'))
    for held in ({'explicit_edge_defs': (('e', ghost),)}, {'output_mapping': ((AuthoredName('o'), ghost),)},
                 {'input_mapping': ((AuthoredName('i'), (ghost,)),)}):
        with pytest.raises(ValueError, match='names a port no step declares'):
            replace(graph, **held)
    if graph.steps:
        with pytest.raises(ValueError, match='injective'):
            WorkflowGraph(graph.namespace, children=(graph, graph))


@pytest.mark.fast
@given(shapes().map(lambda shape: parse(json.dumps(shape), 'recovered.wic').document))
@example(parse('steps:\n- id: ""\n  in: {}\n', 'recovered.wic').document)
@example(parse('steps:\n- \n', 'recovered.wic').document)
@example(parse('steps:\n- id:\n', 'recovered.wic').document)
@example(Document(steps=(Step(id=''),)))   # a step the parser never built, so it carries no span at all
@ORACLE
def test_a_document_the_parser_recovered_never_raises(document: Document) -> None:
    """Totality over what `parse` returns, not over what it accepts: a graph exactly when nothing reported is an error.

    A recovered document is exactly when a caller is least able to handle an
    exception, and a result carrying neither a graph nor a diagnostic is worse
    than the exception it replaced -- `ok` is False and nothing says why.
    """
    result = lower(_resolved(document))
    assert (result.graph is None) is result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
