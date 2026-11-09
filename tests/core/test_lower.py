"""Lowering a resolved document to a graph: total, every reference names an existing port, passthrough is never read,
iteration order is irrelevant. The test-side resolver needs no registry, filesystem or config."""
import json
from typing import Any

import pytest
from hypothesis import example, given

from sophios.ir import (
    Direction,
    Namespace,
    PortId,
    RegistryKey,
    ResolvedDocument,
    ResolvedPort,
    ResolvedProcess,
    ResolvedStep,
    StepId,
    WorkflowGraph,
)
from sophios.ir.lower import lower
from sophios.ir.declarations import port_declaration
from sophios.ir.types import AuthoredName
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.lang.nodes import Document, Step
from sophios.lang.parser import parse

from . import ast_strategies as strat
from .cases import cases, diagnostic, match
from .hermetic import COVERAGE, ORACLE, compile_hermetic, compile_hermetic_cwl, subworkflow_step
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


def _lower(source: str) -> Any:
    """Parse and lower one document, for the example-based claims below."""
    document = parse(source, 'probe.wic').document
    assert document is not None
    return lower(_resolved(document))


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('lower', 'source', 'graph', 'diagnostics'))
def test_lower_case(case: dict[str, Any]) -> None:
    """`source` lowers to exactly `diagnostics`, and to no graph where `graph` is false, for the case's `why`."""
    result = _lower(case['source'])
    assert match(case['diagnostics'], [diagnostic(d) for d in result.diagnostics]), list(result.diagnostics)
    if 'graph' in case:
        assert (result.graph is not None) is case['graph']


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_a_well_formed_document_actually_lowers(document: Document) -> None:
    """`documents()` draws only well-formed documents, so every one of them
    must produce a graph, never a diagnostic or an exception."""
    result = lower(_resolved(document))
    assert result.graph is not None, [d.code.value for d in result.diagnostics]
    assert len(result.graph.steps) == len(document.steps)


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_every_reference_the_graph_holds_names_a_port_that_exists(document: Document) -> None:
    """Edges, obligations and all four mappings, not the edges alone.

    An identity resolving to nothing is the same defect wherever it is stored,
    and it reaches emission as a dangling `source:` -- which CWL accepts and a
    runner then fails on.
    """
    result = lower(_resolved(document))
    if result.graph is None:
        return
    known = {p.id for s in result.graph.steps for p in s.inputs + s.outputs}
    for where, _name, port_id in result.graph._references():  # pylint: disable=protected-access
        assert port_id in known, f'{where} names {port_id}'


@pytest.mark.fast
@given(strat.documents())
@ORACLE
def test_lowering_is_deterministic(document: Document) -> None:
    """The same document lowers to the same graph, twice, in full.

    The whole graph, not a few fields: comparing only steps and edges leaves
    the mappings free to vary with set iteration, which is the thing being
    ruled out.
    """
    resolved = _resolved(document)
    first, second = lower(resolved), lower(resolved)
    assert first.graph == second.graph


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_lowering_keeps_every_authored_binding(document: Document) -> None:
    """Nothing written in `in:` is dropped.

    Keeping only the edges makes two documents differing solely in a literal
    lower to the same graph, so `Emit` would have to read the AST again.
    """
    result = lower(_resolved(document))
    if result.graph is None:
        return
    authored = [v for step in document.steps for _, v in step.inputs]
    carried = [b.value for step in result.graph.steps for b in step.bindings]
    assert carried == authored


@pytest.mark.fast
def test_an_input_and_an_output_of_one_name_are_different_ports() -> None:
    """CWL puts a step's inputs and outputs in separate namespaces.

    A tool declaring `file` on both is ordinary -- the synthetic registry has
    them. Without direction in the identity the two compare equal, an edge
    between them looks like a self-loop, and a set of port ids silently loses
    one of every such pair.
    """
    step = StepId(Namespace(), 1, 's')
    file = AuthoredName('file')
    assert PortId(step, Direction.INPUT, file) != PortId(step, Direction.OUTPUT, file)
    assert len({PortId(step, Direction.INPUT, file),
                PortId(step, Direction.OUTPUT, file)}) == 2


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


@pytest.mark.fast
def test_a_union_member_written_either_way_parses_the_same() -> None:
    """`?` on a union member makes the port optional in either spelling of that member.

    The shorthand expands; the same member written as a record does too, and a
    `?` that sits on an array's items does not make the array itself optional.
    """
    string = port_declaration(['File?', 'string']).type
    record = port_declaration([{'type': 'array', 'items': 'string?'}, 'int']).type
    assert string.optional
    assert string.canonical == ['null', 'File', 'string']
    assert not record.optional
    assert record.canonical == [
        {'type': 'array', 'items': ['null', 'string']}, 'int']


@pytest.mark.fast
def test_a_reference_to_an_undefined_edge_becomes_an_obligation() -> None:
    """What a subworkflow owes its includer has a name and a type, which is
    what lets `Link` say whether every one was discharged."""
    graph = _lower('steps:\n- id: s\n  in:\n    f: !* from_parent\n').graph
    assert graph is not None
    assert [o.name for o in graph.obligations] == ['from_parent']
    assert graph.edges == ()


@pytest.mark.fast
def test_a_reference_to_a_defined_edge_becomes_an_edge() -> None:
    """The same reference, when this document produces what it names."""
    graph = _lower('steps:\n- id: mk\n  out:\n  - file: !& e\n- id: use\n  in:\n    f: !* e\n').graph
    assert graph is not None
    assert graph.obligations == () and len(graph.edges) == 1
    assert graph.edges[0].source.step.name == 'mk' and graph.edges[0].sink.step.name == 'use'


_GHOST = PortId(StepId(Namespace(), 1, 'ghost'), Direction.INPUT, AuthoredName('p'))


@pytest.mark.fast
@pytest.mark.parametrize(('build', 'where'), [
    (lambda: WorkflowGraph(Namespace(), explicit_edge_defs=(('e', _GHOST),)), "mapping 'e'"),
    (lambda: WorkflowGraph(Namespace(), explicit_edge_calls=(('e', _GHOST),)), "mapping 'e'"),
    (lambda: WorkflowGraph(Namespace(), output_mapping=((AuthoredName('o'), _GHOST),)), 'an output mapping'),
    (lambda: WorkflowGraph(Namespace(), input_mapping=((AuthoredName('i'), (_GHOST,)),)), "input mapping 'i'"),
], ids=['edge defs', 'edge calls', 'output mapping', 'input mapping'])
def test_a_reference_to_a_port_no_step_declares_is_reported_where_it_is_held(
        build: Any, where: str) -> None:
    """The report says which mapping holds the reference and, for the ones keyed
    by a name, under what name."""
    with pytest.raises(ValueError) as caught:
        build()
    assert str(caught.value) == f'{where} names a port no step declares: {_GHOST}'


@pytest.mark.fast
def test_an_anchor_on_an_undeclared_call_output_is_reported_not_raised() -> None:
    """A call step anchoring a name its child never declares is wic028; the graph
    is not built from a document the phase rejected, so no ValueError escapes."""
    child = {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}]}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [
            {**subworkflow_step('child.wic', child),
             'parentargs': {'out': [{'child__step__1__mk_file___file': {'wic_anchor': 'e'}}]}},
            {'id': 'sink', 'in': {'file': {'wic_alias': 'e'}, 'n': {'wic_inline_input': 1}}}]})
    assert {d.code for d in caught.value.diagnostics} == {SophiosErrorCode.UNDECLARED_PORT}


@pytest.mark.fast
@pytest.mark.parametrize('extra', [{'linkMerge': 'merge_flattened'}, {'pickValue': 'first_non_null'},
                                   {'outputSource': ['a/f', 'b/f']}, {'outputSource': ['s/f']},
                                   {'outputSource': []}])
def test_link_merge_and_pick_value_on_an_output_are_wic038(extra: dict[str, Any]) -> None:
    """CWL's merge fields on a workflow output were copied out unread, and a list
    `outputSource` was emitted naming steps by their authored ids, which cwltool cannot resolve."""
    source = 'steps:\n- id: s\n  out: [f]\noutputs:\n  o:\n    type: File\n'
    for key, value in {'outputSource': 's/f', **extra}.items():
        source += f'    {key}: {value}\n'
    result = _lower(source)
    assert SophiosErrorCode.STEP_INPUT_RECORD in {d.code for d in result.diagnostics}


@pytest.mark.fast
def test_scatter_ports_keep_the_authored_order() -> None:
    """`scatter: [name, file]` on xform is emitted in that order: under
    `nested_crossproduct` the order is the nesting of the output arrays."""
    compiled = compile_hermetic_cwl({'steps': [{'id': 'xform', 'scatter': ['name', 'file'],
                                                'scatterMethod': 'nested_crossproduct',
                                                'in': {'name': {'wic_inline_input': ['a']},
                                                       'file': {'wic_inline_input': ['x.txt']}}}]})
    assert compiled['steps'][0]['scatter'] == ['name', 'file']
