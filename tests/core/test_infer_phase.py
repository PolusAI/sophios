"""Typed fixed-point inference and speculative insertion properties.

The candidate model below is intentionally local to the test suite.  It does
not import the production phase or either production type predicate.  The
differential then carries the stronger claim: after typed Infer has made every
candidate decision, the legacy compiler emits byte-identical artifacts.

BLIND SPOTS: the generated workflows never put a tool after a workflow call,
and their registry has no converter processes. Workflow-call candidates,
converter selection, ambiguity, falsy defaults, scatter, and exhaustion
therefore have separate planted examples.
"""
import copy
from typing import Any

import pytest
from hypothesis import given

from sophios.ir import (
    InferencePolicy,
    InsertionCatalog,
    RegistrySnapshot,
    front_end,
    infer,
    legacy_after_infer,
    link,
)
from sophios.ir.types import Port, PortDeclaration, PortId, StepNode, WorkflowGraph
from sophios.lang import SophiosErrorCode
from sophios.wic_types import StepId as LegacyStepId, Tool, Tools, Yaml

from . import ast_strategies as strat
from .differential import assert_compilations_equivalent
from .equivalence import Strength
from .hermetic import ORACLE, compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt
from .test_resolve import _scalar_literals_fit, _source_model


def _typed(workflow: Yaml, tools: Tools = SYNTHETIC_TOOLS):  # type: ignore[no-untyped-def]
    source, workflows = _source_model(workflow)
    registry = RegistrySnapshot.from_tools(tools, workflows=workflows)
    result = front_end(source, registry, name='oracle')
    assert result.graph is not None and result.resolved is not None
    linked = link(result.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    return result, linked.graph, registry


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_infer_is_identical_to_the_legacy_candidate_search(workflow: Yaml) -> None:
    """Typed inference changes neither artifacts nor their byte order."""
    typed, linked, _ = _typed(copy.deepcopy(workflow))
    inferred = infer(linked)
    assert inferred.graph is not None, list(inferred.diagnostics)
    bridged = legacy_after_infer(typed.resolved.document, inferred.graph)
    old = compile_hermetic(copy.deepcopy(workflow))
    new = compile_hermetic(bridged)
    assert_compilations_equivalent(old, new, Strength.IDENTICAL)


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_every_inferred_edge_is_the_independent_models_choice(workflow: Yaml) -> None:
    """Removing candidate selection or changing its order breaks this predicate."""
    _, linked, _ = _typed(workflow)
    inferred = infer(linked)
    assert inferred.graph is not None, list(inferred.diagnostics)
    for graph in _graphs(inferred.graph):
        original = next(item for item in _graphs(linked) if item.namespace == graph.namespace)
        for edge in graph.inferred_edges:
            position = next(index for index, step in enumerate(original.steps)
                            if step.id == edge.sink.step)
            sink = _port(original.steps[position].inputs, edge.sink.port)
            assert _model_candidate(original.steps, position, sink) == edge.source


@pytest.mark.fast
def test_most_recent_step_and_last_declared_output_win() -> None:
    """Candidate order is semantic, including ambiguity within one producer."""
    multi = clt({}, {
        'first': {'type': 'File', 'outputBinding': {'glob': 'first'}},
        'last': {'type': 'File', 'outputBinding': {'glob': 'last'}},
    })
    tools = {**SYNTHETIC_TOOLS,
             LegacyStepId('multi_file', SYNTHETIC_NS):
                 Tool('/synthetic/multi_file.cwl', multi)}
    workflow = {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}},
                          {'id': 'multi_file'}, {'id': 'count'}]}
    _, linked, _ = _typed(workflow, tools)
    result = infer(linked)
    assert result.graph is not None
    count_edge = next(edge for edge in result.graph.inferred_edges
                      if edge.sink.step.name == 'count')
    assert count_edge.source.step.name == 'multi_file'
    assert count_edge.source.port == 'last'


@pytest.mark.fast
@pytest.mark.parametrize('default', [0, False, '', []])
def test_falsy_defaults_satisfy_inputs(default: object) -> None:
    """Presence, not truthiness, suppresses inference for a defaulted port."""
    tool = clt({'value': {'type': 'string', 'default': default}}, {})
    tools = {LegacyStepId('defaulted', SYNTHETIC_NS):
             Tool('/synthetic/defaulted.cwl', tool)}
    _, linked, _ = _typed({'steps': [{'id': 'defaulted'}]}, tools)
    result = infer(linked)
    assert result.graph is not None
    assert result.graph.inferred_edges == ()
    assert result.graph.workflow_inputs == ()


@pytest.mark.fast
def test_scatter_lifts_both_sides_of_candidate_selection() -> None:
    """A producing scatter matches an array sink, while a scalar sink does not."""
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    tools[LegacyStepId('file_array_sink', SYNTHETIC_NS)] = Tool(
        '/synthetic/file_array_sink.cwl', clt({'files': {'type': 'File[]'}}, {}, canonical=True))
    workflow = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a', 'b']}},
         'scatter': 'name'},
        {'id': 'file_array_sink'},
        {'id': 'count'},
    ]}
    _, linked, _ = _typed(workflow, tools)
    result = infer(linked)
    assert result.graph is not None
    sinks = {(edge.sink.step.name, edge.sink.port) for edge in result.graph.inferred_edges}
    assert ('file_array_sink', 'files') in sinks
    assert ('count', 'file') not in sinks


@pytest.mark.fast
def test_converter_insertion_reaches_the_same_fixed_point() -> None:
    """Two speculative insertions agree exactly with legacy recompilation."""
    workflow, tools = _insertion_registry()
    typed, linked, registry = _typed(workflow, tools)
    result = infer(linked, InferencePolicy(insert_steps_automatically=True),
                   InsertionCatalog.from_registry(registry))
    assert result.graph is not None, list(result.diagnostics)
    assert result.iterations == 3
    assert sum(step.synthesized for step in result.graph.steps) == 2
    bridged = legacy_after_infer(typed.resolved.document, result.graph)
    old = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                           insert_steps_automatically=True)
    new = compile_hermetic(bridged, tools=copy.deepcopy(tools))
    assert_compilations_equivalent(old, new, Strength.IDENTICAL)


@pytest.mark.fast
def test_workflow_call_outputs_are_inference_candidates() -> None:
    """A child output remains visible to a later tool in its parent."""
    child = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'child.txt'}}},
    ]}
    workflow = {'steps': [subworkflow_step('sub.wic', child), {'id': 'count'}]}
    typed, linked, _ = _typed(workflow)
    result = infer(linked)
    assert result.graph is not None, list(result.diagnostics)
    assert any(edge.sink.step.name == 'count' for edge in result.graph.inferred_edges)
    bridged = legacy_after_infer(typed.resolved.document, result.graph)
    assert_compilations_equivalent(
        compile_hermetic(copy.deepcopy(workflow)),
        compile_hermetic(bridged),
        Strength.IDENTICAL,
    )


@pytest.mark.fast
def test_workflow_call_outputs_can_feed_inserted_converters() -> None:
    """Converter search sees formats exported through a workflow call."""
    _, tools = _insertion_registry()
    child = {'steps': [{'id': 'mk_2'}]}
    workflow = {'steps': [subworkflow_step('sub.wic', child), {'id': 'use_2'}]}
    typed, linked, registry = _typed(workflow, tools)
    result = infer(linked, InferencePolicy(insert_steps_automatically=True),
                   InsertionCatalog.from_registry(registry))
    assert result.graph is not None, list(result.diagnostics)
    assert sum(step.synthesized for step in result.graph.steps) == 1
    bridged = legacy_after_infer(typed.resolved.document, result.graph)
    assert_compilations_equivalent(
        compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                         insert_steps_automatically=True),
        compile_hermetic(bridged, tools=copy.deepcopy(tools)),
        Strength.IDENTICAL,
    )


@pytest.mark.fast
def test_converter_search_stops_at_the_candidate_break() -> None:
    """Insertion cannot reuse formats hidden behind a local break rule."""
    _, tools = _insertion_registry()
    workflow = {
        'steps': [{'id': 'mk_1'}, {'id': 'mk_2'}, {'id': 'use_1'}],
        'wic': {'steps': {
            '(2, mk_2)': {'wic': {'inference': {'file': 'break'}}},
        }},
    }
    _, linked, registry = _typed(workflow, tools)
    result = infer(linked, InferencePolicy(insert_steps_automatically=True),
                   InsertionCatalog.from_registry(registry))
    assert result.graph is not None, list(result.diagnostics)
    assert not any(step.synthesized for step in result.graph.steps)
    assert [port.name for port in result.graph.workflow_inputs] == [
        'oracle__step__3__use_1___file']
    legacy = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                              insert_steps_automatically=True)
    legacy_steps = legacy.rose.data.compiled_cwl['steps']
    assert not any('insert_steps_automatically_' in step['id'] for step in legacy_steps)


@pytest.mark.fast
def test_iteration_exhaustion_is_exactly_wic022() -> None:
    """The limit cannot be removed or exhausted silently."""
    _, linked, _ = _typed({'steps': [{'id': 'mk_file'}]})
    result = infer(linked, InferencePolicy(iteration_limit=0))
    assert result.graph is None
    assert [item.code for item in result.diagnostics] == [
        SophiosErrorCode.FIXED_POINT_NOT_REACHED]


@pytest.mark.fast
def test_inference_policy_is_not_process_global() -> None:
    """Two policies coexist; neither compiler module exposes writable policy state."""
    from sophios import compiler  # pylint: disable=import-outside-toplevel
    from sophios import inference as legacy  # pylint: disable=import-outside-toplevel
    assert not hasattr(compiler, 'inference_rules')
    assert not hasattr(legacy, 'renaming_conventions')


def _model_candidate(steps: tuple[StepNode, ...], position: int,
                     sink: Port) -> PortId | None:
    """Independent, deliberately small model of default candidate selection."""
    sink_type = _model_sink_type(steps[position], sink)
    sink_formats = _model_formats(sink.declaration)
    for producer in reversed(steps[:position]):
        for output in reversed(producer.outputs):
            source_type = _model_source_type(producer, output)
            source_formats = _model_formats(output.declaration)
            if ('_log_' not in output.id.port
                    and _model_types_match(sink_type, source_type)
                    and _model_formats_match(sink_formats, source_formats, source_type)):
                return output.id
    return None


def _model_types_match(sink: Any, source: Any) -> bool:
    if sink == source:
        return True
    sink_members = sink if isinstance(sink, list) else [sink]
    source_members = source if isinstance(source, list) else [source]
    return any(member in sink_members for member in source_members)


def _model_formats(declaration: PortDeclaration | None) -> tuple[Any, ...]:
    if declaration is None or not declaration.has_format:
        return ()
    return tuple(declaration.format) if isinstance(declaration.format, list) \
        else (declaration.format,)


def _model_formats_match(sink: tuple[Any, ...], source: tuple[Any, ...],
                         source_type: Any) -> bool:
    if not sink:
        return True
    if source:
        return any(item in sink for item in source)
    return not _model_permits_format(source_type)


def _model_permits_format(value: Any) -> bool:
    if value == 'File':
        return True
    if isinstance(value, list):
        return any(_model_permits_format(item) for item in value)
    if isinstance(value, dict) and value.get('type') == 'array':
        return _model_permits_format(value.get('items'))
    return False


def _model_source_type(step: StepNode, port: Port) -> Any:
    raw = _model_type(port)
    return {'type': 'array', 'items': raw} \
        if step.emission is not None and step.emission.scatter else raw


def _model_sink_type(step: StepNode, port: Port) -> Any:
    raw = _model_type(port)
    scatter = step.emission.scatter if step.emission is not None else None
    keys = [scatter] if isinstance(scatter, str) else (
        [item for item in scatter if isinstance(item, str)]
        if isinstance(scatter, list) else [])
    return {'type': 'array', 'items': raw} if port.id.port in keys else raw


def _model_type(port: Port) -> Any:
    raw = port.type.declared
    if not isinstance(raw, str):
        return raw
    base = raw.removesuffix('?')
    while base.endswith('[]'):
        base = base[:-2]
    value: Any = base
    for _ in range(port.type.array_depth):
        value = {'type': 'array', 'items': value}
    return ['null', value] if port.type.optional else value


def _port(ports: tuple[Port, ...], name: str) -> Port:
    return next(port for port in ports if port.id.port == name)


def _graphs(graph: WorkflowGraph) -> tuple[WorkflowGraph, ...]:
    return (graph,) + tuple(item for child in graph.children for item in _graphs(child))


def _insertion_registry() -> tuple[Yaml, Tools]:
    """Hermetic two-converter catalog; independent of the benchmark harness."""
    specs = {}
    steps: list[Yaml] = []
    for index in (1, 2):
        source_format = f'edam:format_src{index}'
        sink_format = f'edam:format_dst{index}'
        specs[f'mk_{index}'] = clt(
            {}, {'file': {'type': 'File', 'format': source_format}}, canonical=True)
        specs[f'use_{index}'] = clt(
            {'file': {'type': 'File', 'format': sink_format}}, {}, canonical=True)
        specs[f'insert_steps_automatically_conv_{index}'] = clt(
            {'file': {'type': 'File', 'format': source_format}},
            {'file': {'type': 'File', 'format': sink_format}}, canonical=True)
        steps.extend(({'id': f'mk_{index}'}, {'id': f'use_{index}'}))
    tools = {LegacyStepId(name, SYNTHETIC_NS): Tool(f'/synthetic/{name}.cwl', cwl)
             for name, cwl in specs.items()}
    return {'steps': steps}, tools
