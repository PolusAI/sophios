"""Pure graph composition, reference linking, and obligation discharge.

The generated differential uses the independent source/registry model from
the Resolve suite.  Examples pin the cross-scope cases that flat generated
workflows cannot express compactly: lowest-common-ancestor ownership, wrapper
output redirection, omitted call arguments, conservative unknown types, and
both sides of scatter lifting.
"""
import copy

import pytest
from hypothesis import HealthCheck, given, settings

from sophios.ir import (
    Namespace,
    RegistrySnapshot,
    WorkflowGraph,
    front_end,
    link,
)
from sophios.lang import SophiosErrorCode
from sophios.wic_types import StepId as LegacyStepId, Tool, Yaml

from . import ast_strategies as strat
from .hermetic import ORACLE, compile_hermetic
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt
from .test_resolve import _scalar_literals_fit, _source_model


def _front(workflow: Yaml) -> WorkflowGraph:
    source, workflows = _source_model(workflow)
    registry = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows)
    result = front_end(source, registry, name='oracle')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.graph is not None, list(result.diagnostics)
    return result.graph


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_the_live_compiler_retains_linked_explicit_edges(workflow: Yaml) -> None:
    """The default path carries every linked authored edge into its final graph."""
    typed = _front(copy.deepcopy(workflow))
    linked = link(typed)
    assert linked.graph is not None, list(linked.diagnostics)
    live = compile_hermetic(copy.deepcopy(workflow)).graph
    linked_edges = {(edge.source, edge.sink) for edge in linked.graph.edges}
    assert linked_edges <= {(edge.source, edge.sink) for edge in live.edges}


def _cross_scope(source_tool: str, source_type: object, child_input: str = 'name', *,
                 scatter: bool = False) -> WorkflowGraph:
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    tools[LegacyStepId(source_tool, SYNTHETIC_NS)] = Tool(
        f'/synthetic/{source_tool}.cwl', clt({}, {'value': {'type': source_type}}))
    child_scatter = f'  scatter: {child_input}\n' if scatter else ''
    child = (f'steps:\n- id: mk_file\n  in:\n    {child_input}: !* shared\n'
             f'{child_scatter}')
    root = (f'steps:\n- id: {source_tool}\n  out:\n  - value: !& shared\n'
            '- id: child.wic\n')
    registry = RegistrySnapshot.from_tools(
        tools, workflows={(SYNTHETIC_NS, 'child'): child})
    result = front_end(root, registry, name='root')
    assert result.graph is not None and result.resolved is not None
    return result.graph


@pytest.mark.fast
def test_cross_scope_obligation_is_discharged_at_its_lca() -> None:
    """The edge belongs to the parent graph and the child owes nothing afterward."""
    typed = _cross_scope('string_source', 'string')
    linked = link(typed)
    assert linked.graph is not None, list(linked.diagnostics)
    assert linked.graph.obligations == ()
    assert len(linked.graph.composition_edges) == 1
    assert linked.graph.composition_edges[0].sink.step.namespace.parts
    assert all(not child.composition_edges for child in linked.graph.children)


@pytest.mark.fast
@pytest.mark.parametrize('definition_first', [True, False], ids=['before', 'after'])
def test_a_call_does_not_exempt_an_edge_from_document_order(definition_first: bool) -> None:
    """The order rule is one rule, whether the reference is flat or in a call.

    Reference §4.1.2: a reference resolves against the definitions before it,
    and a child may consume what its includer has *already* defined. Lower
    applies that within one scope. If Link ignored it across a call, the same
    program would be `wic025` written flat and legal once split into a
    subworkflow, which is two rules for one construct.
    """
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    tools[LegacyStepId('string_source', SYNTHETIC_NS)] = Tool(
        '/synthetic/string_source.cwl', clt({}, {'value': {'type': 'string'}}))
    child = 'steps:\n- id: mk_file\n  in:\n    name: !* shared\n'
    producer = '- id: string_source\n  out:\n  - value: !& shared\n'
    call = '- id: child.wic\n'
    root = 'steps:\n' + (producer + call if definition_first else call + producer)
    registry = RegistrySnapshot.from_tools(tools, workflows={(SYNTHETIC_NS, 'child'): child})
    typed = front_end(root, registry, name='root')
    assert typed.graph is not None, list(typed.diagnostics)

    linked = link(typed.graph)
    if definition_first:
        assert linked.graph is not None, list(linked.diagnostics)
        assert len(linked.graph.composition_edges) == 1
    else:
        assert linked.graph is None
        assert [diagnostic.code for diagnostic in linked.diagnostics] == [
            SophiosErrorCode.UNDEFINED_EDGE]


@pytest.mark.fast
def test_unresolved_root_obligation_is_exactly_undefined_edge() -> None:
    """An obligation with no producer is reported rather than silently promoted."""
    child = 'steps:\n- id: mk_file\n  in:\n    name: !* nowhere\n'
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): child})
    typed = front_end('steps:\n- id: child.wic\n', registry, name='root')
    assert typed.graph is not None
    linked = link(typed.graph)
    assert linked.graph is None
    assert [diagnostic.code for diagnostic in linked.diagnostics] == [
        SophiosErrorCode.UNDEFINED_EDGE]


@pytest.mark.fast
def test_only_proven_disjoint_cross_scope_types_are_rejected() -> None:
    """Known incompatibility rejects; opaque Any remains delegated to final CWL validation."""
    disjoint = link(_cross_scope('int_source', 'int'))
    assert disjoint.graph is None
    assert [diagnostic.code for diagnostic in disjoint.diagnostics] == [
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE]

    unknown = link(_cross_scope('any_source', 'Any'))
    assert unknown.graph is not None, list(unknown.diagnostics)


@pytest.mark.fast
def test_consuming_scatter_participates_in_reference_judgment() -> None:
    """A scattered scalar input consumes an array, so a scalar source is disjoint."""
    result = link(_cross_scope('string_source', 'string', scatter=True))
    assert result.graph is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE]


@pytest.mark.fast
def test_producing_scatter_participates_in_reference_judgment() -> None:
    """A scattered producer yields an array and cannot feed a scalar sink."""
    source = '''
steps:
- id: mk_file
  in: {name: !ii [a, b]}
  scatter: name
  out:
  - file: !& files
- id: count
  in: {file: !* files}
'''
    typed = front_end(source, RegistrySnapshot.from_tools(SYNTHETIC_TOOLS), name='root')
    assert typed.graph is not None
    linked = link(typed.graph)
    assert linked.graph is None
    assert [diagnostic.code for diagnostic in linked.diagnostics] == [
        SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE]


@pytest.mark.fast
def test_wrapper_output_anchor_moves_to_the_concrete_producer() -> None:
    """A call output is an interface; the child step is the value's producer."""
    child = '''
steps:
- id: mk_file
  in: {name: !ii child.txt}
outputs:
  result:
    type: File
    outputSource: child__step__1__mk_file/file
'''
    root = '''
steps:
- id: child.wic
  out:
  - result: !& made
- id: count
  in: {file: !* made}
'''
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): child})
    typed = front_end(root, registry, name='root')
    assert typed.graph is not None
    linked = link(typed.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    edge = next(edge for edge in linked.graph.edges if edge.sink.step.name == 'count')
    assert edge.source.step.name == 'mk_file'
    assert edge.source.step.namespace.parts


@pytest.mark.fast
def test_omitted_nested_argument_remains_available_to_infer() -> None:
    """Link does not synthesize a call binding for an omitted child input."""
    child = '''
inputs: {name: {type: string}}
steps:
- id: mk_file
  in: {name: name}
'''
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): child})
    typed = front_end('steps:\n- id: child.wic\n', registry, name='root')
    assert typed.graph is not None
    linked = link(typed.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    wrapper = linked.graph.steps[0]
    child_graph = linked.graph.children[0]
    child_step = child_graph.steps[0]
    assert wrapper.bindings == ()
    assert child_step.inputs and child_step.bindings
    assert dict(child_graph.input_mapping)['name'] == (child_step.inputs[0].id,)


@pytest.mark.fast
def test_unknown_call_argument_cannot_restore_a_deleted_formal() -> None:
    """Authored call metadata cannot widen the resolved child interface."""
    child = 'inputs: {name: {type: string}}\nsteps:\n- id: mk_file\n  in: {name: name}\n'
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): child})
    typed = front_end('steps:\n- id: child.wic\n  in: {ghost: !ii x}\n',
                      registry, name='root')
    assert typed.graph is None
    assert [diagnostic.code for diagnostic in typed.diagnostics] == [
        SophiosErrorCode.UNDECLARED_PORT]


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_composed_namespaces_are_injective(workflow: Yaml) -> None:
    """No two step occurrences in a composed graph share an identity."""
    typed = _front(workflow)
    linked = link(typed)
    assert linked.graph is not None, list(linked.diagnostics)
    identities = [step.id for step in linked.graph.all_steps]
    assert len(identities) == len(set(identities))


@pytest.mark.fast
def test_graph_rejects_a_planted_namespace_collision() -> None:
    """The injectivity assertion demonstrably detects a duplicate child graph."""
    typed = _cross_scope('string_source', 'string')
    child = typed.children[0]
    with pytest.raises(ValueError, match='injective'):
        WorkflowGraph(Namespace(), children=(child, child))
