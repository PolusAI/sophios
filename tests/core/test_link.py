"""Pure graph composition, reference linking and obligation discharge: lowest-common-ancestor ownership, wrapper output
redirection, omitted call arguments (cross-scope cases by code and edge source: data/edge-binding/cross_scope.yaml)."""
import pytest

from sophios.ir import (
    Namespace,
    RegistrySnapshot,
    WorkflowGraph,
    front_end,
    link,
)
from sophios.ir.complete import complete
from sophios.ir.pipeline import FrontEndResult
from sophios.ir.types import AuthoredName
from sophios.lang import SophiosErrorCode, parse
from sophios.wic_types import StepId as LegacyStepId, Tool, Tools

from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt, registered


def _rooted(root: str, tools: Tools = SYNTHETIC_TOOLS, **children: str) -> FrontEndResult:
    """`root` through the front end, against `tools` and the named child sources."""
    registry = RegistrySnapshot.from_tools(tools, workflows={
        (SYNTHETIC_NS, name): parse(text, f'{name}.wic') for name, text in children.items()})
    return front_end(parse(root, 'root.wic'), registry, name='root')


def _cross_scope(*, wrapped: bool = False) -> WorkflowGraph:
    """`root` defines the edge `shared` with a string source and calls child.wic, whose mk_file consumes it; with
    `wrapped`, root calls wrapper.wic, which calls child.wic, so the consumer sits two documents below."""
    tools = registered({'string_source': clt({}, {'value': {'type': 'string'}})})
    root = ('steps:\n- id: string_source\n  out:\n  - value: !& shared\n'
            f'- id: {"wrapper" if wrapped else "child"}.wic\n')
    result = _rooted(root, tools, child='steps:\n- id: mk_file\n  in:\n    name: !* shared\n',
                     **({'wrapper': 'steps:\n- id: child.wic\n'} if wrapped else {}))
    assert result.graph is not None and result.resolved is not None
    return result.graph


@pytest.mark.fast
@pytest.mark.parametrize(('source_type', 'codes'), [
    pytest.param('int', [SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE], id='proven-disjoint'),
    pytest.param('Any', [], id='opaque-any'),
])
def test_only_proven_disjoint_cross_scope_types_are_rejected(source_type: str, codes: list[SophiosErrorCode]) -> None:
    """Known incompatibility rejects; opaque Any remains delegated to final CWL validation."""
    tools = {**SYNTHETIC_TOOLS, LegacyStepId('typed_source', SYNTHETIC_NS): Tool(
        '/synthetic/typed_source.cwl', clt({}, {'value': {'type': source_type}}))}
    root = 'steps:\n- id: typed_source\n  out:\n  - value: !& shared\n- id: child.wic\n'
    typed = _rooted(root, tools, child='steps:\n- id: mk_file\n  in:\n    name: !* shared\n')
    assert typed.graph is not None
    linked = link(typed.graph)
    assert [diagnostic.code for diagnostic in linked.diagnostics] == codes
    assert (linked.graph is None) is bool(codes)


@pytest.mark.fast
def test_cross_scope_obligation_discharged_two_levels_below_the_definition() -> None:
    """A child two documents under the edge's definition still compiles.

    `prod.wic`, called from `basic.wic`, called from `stability.wic`,
    consumes an edge only an ancestor's sibling defines. Building the
    intermediate `wrapper.wic` graph while placing that discharge must not
    raise, the obligation must end up fully discharged, and the boundary
    name it is threaded through must resolve to the same sink at every
    level on the way down -- never to a port that level's own steps do not
    declare.
    """
    typed = _cross_scope(wrapped=True)
    linked = link(typed)
    assert linked.graph is not None, list(linked.diagnostics)
    assert linked.graph.obligations == ()
    assert len(linked.graph.linked_edges) == 1
    edge = linked.graph.linked_edges[0]

    wrapper = linked.graph.children[0]
    child = wrapper.children[0]
    assert not wrapper.linked_edges and not child.linked_edges
    assert child.steps[0].inputs[0].id == edge.sink

    assert {port.name for port in wrapper.workflow_inputs} == \
        {name for name, _ in wrapper.input_mapping}
    for _name, sinks in wrapper.input_mapping:
        assert sinks == (edge.sink,)
    for _name, sinks in child.input_mapping:
        assert sinks == (edge.sink,)


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
    outputSource: mk_file/file
'''
    root = '''
steps:
- id: child.wic
  out:
  - result: !& made
- id: count
  in: {file: !* made}
'''
    typed = _rooted(root, child=child)
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
    typed = _rooted('steps:\n- id: child.wic\n', child=child)
    assert typed.graph is not None
    linked = link(typed.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    wrapper = linked.graph.steps[0]
    child_graph = linked.graph.children[0]
    child_step = child_graph.steps[0]
    assert wrapper.bindings == ()
    assert child_step.inputs and child_step.bindings
    assert dict(child_graph.input_mapping)[AuthoredName('name')] == (child_step.inputs[0].id,)


@pytest.mark.fast
def test_graph_rejects_a_planted_namespace_collision() -> None:
    """The injectivity assertion demonstrably detects a duplicate child graph."""
    typed = _cross_scope()
    child = typed.children[0]
    with pytest.raises(ValueError, match='injective'):
        WorkflowGraph(Namespace(), children=(child, child))


@pytest.mark.fast
def test_a_wrapper_scatters_a_declared_input_threaded_to_another_name() -> None:
    """A callee may thread a declared input to an inner port of another name.

    `gen_topol_params.wic` declares `input_receptor_xyz_path` and scatters it;
    the step that actually consumes it is `combine_structure.input_structure1`.
    The wrapper's `scatter:` list can only name the boundary, so matching it
    against the inner port's own name -- or against the mangled name an
    inferred input would get -- finds neither, drops the layer, and reports a
    producer's `File[]` as disjoint from a `File` it in fact feeds one element
    of. The child's `input_mapping` already records which boundary reaches
    which sink, and is what `complete._direct_sink` walks for the same edge.
    """
    tools = dict(SYNTHETIC_TOOLS)
    tools[LegacyStepId('make', SYNTHETIC_NS)] = Tool(
        '/synthetic/make.cwl', clt({'seed': {'type': 'string'}}, {'value': {'type': 'File'}}))
    tools[LegacyStepId('take', SYNTHETIC_NS)] = Tool(
        '/synthetic/take.cwl', clt({'inner_name': {'type': 'File'}}, {}))

    producer = ('inputs:\n  seed: {type: string}\n'
                'steps:\n- id: make\n  in: {seed: seed}\n  out:\n  - value: !& shared\n')
    # Declared as `outer_name`, consumed as `inner_name`: the two differ.
    consumer = ('inputs:\n  outer_name: {type: File}\n'
                'steps:\n- id: take\n  in: {inner_name: outer_name}\n')
    root = ('steps:\n'
            '- id: producer.wic\n  in: {seed: !ii [a, b]}\n  scatter: [seed]\n'
            '- id: consumer.wic\n  in: {outer_name: !* shared}\n  scatter: [outer_name]\n')

    typed = _rooted(root, tools, producer=producer, consumer=consumer)
    assert typed.graph is not None, list(typed.diagnostics)

    linked = link(complete(typed.graph))
    assert linked.graph is not None, [str(item) for item in linked.diagnostics]
