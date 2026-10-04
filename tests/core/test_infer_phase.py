"""Typed fixed-point inference and speculative insertion properties.

The candidate model below is intentionally local to the test suite.  It does
not import the production phase or either production type predicate.  The
live-path check proves that the compiler retains the typed phase's decisions.
Exact spelling is not the compatibility promise; the completed pipeline is
judged at ``UP_TO_EMBEDDING``.

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
    front_end,
    infer,
    link,
)
from sophios.ir.names import Names
from sophios.ir.declarations import input_rank, layered, output_rank
from sophios.ir.types import (DerivedName, EdgeOrigin, Port, PortDeclaration, PortId, PortName,
                              StepNode, WorkflowGraph)
from sophios.lang import SophiosErrorCode
from sophios.lang.diagnostics import Locator, Severity, SophiosError
from sophios.wic_types import StepId as LegacyStepId, Tool, Tools, Yaml

from . import ast_strategies as strat
from .hermetic import ORACLE, bundle, compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt


def _typed(workflow: Yaml, tools: Tools = SYNTHETIC_TOOLS):  # type: ignore[no-untyped-def]
    model = bundle(workflow, 'oracle', tools)
    result = front_end(model.parsed, model.registry, name='oracle')
    assert result.graph is not None and result.resolved is not None
    linked = link(result.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    return result, linked.graph, model.registry


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_every_inferred_edge_is_the_independent_models_choice(workflow: Yaml) -> None:
    """Removing candidate selection or changing its order breaks this predicate."""
    _, linked, _ = _typed(workflow)
    inferred = infer(linked)
    assert inferred.graph is not None, list(inferred.diagnostics)
    for graph in _graphs(inferred.graph):
        original = next(item for item in _graphs(linked) if item.namespace == graph.namespace)
        for edge in graph.linked_edges:
            if edge.origin is not EdgeOrigin.INFERRED:
                continue
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
    count_edge = next(edge for edge in result.graph.linked_edges
                      if edge.origin is EdgeOrigin.INFERRED and edge.sink.step.name == 'count')
    assert count_edge.source.step.name == 'multi_file'
    assert count_edge.source.port == 'last'


def _multi_file_tools() -> Tools:
    """The synthetic registry plus a tool offering two `File` outputs."""
    multi = clt({}, {'first': {'type': 'File', 'outputBinding': {'glob': 'first'}},
                     'last': {'type': 'File', 'outputBinding': {'glob': 'last'}}})
    return {**SYNTHETIC_TOOLS,
            LegacyStepId('multi_file', SYNTHETIC_NS): Tool('/synthetic/multi_file.cwl', multi)}


_TWO_PRODUCERS: Yaml = {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
                                  {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'b'}}},
                                  {'id': 'count'}]}


@pytest.mark.fast
def test_a_tie_within_one_producer_is_noted_with_the_alternatives() -> None:
    """The losing output and the pin that would make the choice explicit are named."""
    _, linked, _ = _typed({'steps': [{'id': 'multi_file'}, {'id': 'count'}]}, _multi_file_tools())
    result = infer(linked)
    assert result.graph is not None
    (note,) = [d for d in result.diagnostics if d.severity is Severity.NOTE]
    assert note.code is SophiosErrorCode.INFERENCE_TIE
    assert "'first'" in note.message and '!&' in note.message
    assert note.locator == Locator(step='count', index=2, port='file')


@pytest.mark.fast
def test_recency_between_producers_is_noted() -> None:
    """An earlier producer that also matched is named; the choice itself is unchanged."""
    _, linked, _ = _typed(_TWO_PRODUCERS)
    result = infer(linked)
    assert result.graph is not None
    (note,) = [d for d in result.diagnostics if d.severity is Severity.NOTE]
    assert note.code is SophiosErrorCode.INFERENCE_RECENCY
    assert "'mk_file/file'" in note.message
    edge = next(e for e in result.graph.linked_edges if e.sink.step.name == 'count')
    assert edge.source.step.index == 2, 'the choice itself is unchanged'


def _across_calls(pinned: bool) -> Yaml:
    """`multi_file` inside `make.wic` feeds `count` inside `use.wic`; pinned, the
    edge is written on those two inner steps."""
    producer: Yaml = {'id': 'multi_file'}
    consumer: Yaml = {'id': 'count'}
    if pinned:
        producer['out'] = [{'last': {'wic_anchor': 'f'}}]
        consumer['in'] = {'file': {'wic_alias': 'f'}}
    return {'steps': [subworkflow_step('make.wic', {'steps': [producer]}),
                      subworkflow_step('use.wic', {'steps': [consumer]})]}


@pytest.mark.fast
def test_a_note_across_subworkflow_calls_names_the_steps_an_author_can_pin() -> None:
    """A call exposes inner ports under names nobody can write, so the note
    points at the inner steps, and writing exactly the pin it gives compiles
    without a note."""
    result = compile_hermetic(_across_calls(pinned=False), tools=_multi_file_tools())
    (note,) = list(result.diagnostics)
    assert note.code is SophiosErrorCode.INFERENCE_TIE
    assert "inferred from 'make.wic/multi_file/last'" in note.message
    assert "also offers 'multi_file/first'" in note.message
    assert ("`out: - last: !& <name>` on step 'make.wic/multi_file' and "
            "`in: file: !* <name>` on step 'use.wic/count'") in note.message

    pinned = compile_hermetic(_across_calls(pinned=True), tools=_multi_file_tools())
    assert not list(pinned.diagnostics)


@pytest.mark.fast
def test_strict_mode_turns_notes_into_errors() -> None:
    """`InferencePolicy.strict` refuses a choice between equals."""
    _, linked, _ = _typed(_TWO_PRODUCERS)
    result = infer(linked, InferencePolicy(strict=True))
    assert result.graph is None and result.diagnostics.has_errors
    assert [d.code for d in result.diagnostics] == [SophiosErrorCode.INFERENCE_RECENCY]


@pytest.mark.fast
def test_a_pinned_edge_is_not_noted() -> None:
    """An explicit `!&`/`!*` edge is not a choice, so nothing is said."""
    _, linked, _ = _typed({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
                                     {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'b'}},
                                      'out': [{'file': {'wic_anchor': 'f'}}]},
                                     {'id': 'count', 'in': {'file': {'wic_alias': 'f'}}}]})
    assert not list(infer(linked).diagnostics)


@pytest.mark.fast
def test_a_tie_settled_by_naming_conventions_is_not_noted() -> None:
    """When the naming conventions single out one output, order did not decide."""
    tool = clt({}, {'output_file': {'type': 'File', 'outputBinding': {'glob': 'a'}},
                    'output_other': {'type': 'File', 'outputBinding': {'glob': 'b'}}})
    tools = {**SYNTHETIC_TOOLS, LegacyStepId('named', SYNTHETIC_NS): Tool('/synthetic/named.cwl', tool)}
    _, linked, _ = _typed({'steps': [{'id': 'named'}, {'id': 'count'}]}, tools)
    result = infer(linked, InferencePolicy(use_naming_conventions=True))
    assert result.graph is not None
    assert not list(result.diagnostics)


@pytest.mark.fast
def test_a_scalar_literal_under_scatter_is_wic020() -> None:
    """A scatter splits its value; a scalar is not wrapped into a one-element list."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}},
                                     'scatter': ['name']}]})
    assert caught.value.diagnostics[0].code is SophiosErrorCode.LITERAL_TYPE_MISMATCH
    assert 'scattered' in caught.value.diagnostics[0].message


@pytest.mark.fast
def test_a_list_literal_under_scatter_is_scattered_over() -> None:
    """The list form is unchanged: each element is one scattered value."""
    result = compile_hermetic({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a', 'b']}},
                                          'scatter': ['name']}]})
    assert list(result.artifact.job_inputs.values()) == [['a', 'b']]


@pytest.mark.fast
def test_strict_compile_refuses_and_lenient_compile_carries_the_note() -> None:
    """The compiler forwards the strict flag and returns the notes with its result."""
    result = compile_hermetic(_TWO_PRODUCERS)
    assert [d.code for d in result.diagnostics] == [SophiosErrorCode.INFERENCE_RECENCY]
    with pytest.raises(SophiosError) as caught:
        compile_hermetic(_TWO_PRODUCERS, inference_strict=True)
    assert caught.value.diagnostics[0].code is SophiosErrorCode.INFERENCE_RECENCY


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
    assert not any(edge.origin is EdgeOrigin.INFERRED for edge in result.graph.linked_edges)
    assert result.graph.workflow_inputs == ()


@pytest.mark.fast
@pytest.mark.parametrize('expression', [
    '$(inputs.source.format)',
    '${ return inputs.source.format; }',
])
def test_promoted_input_preserves_a_cwl_format_expression(expression: str) -> None:
    """A lifted input carries the tool's `format` verbatim, in the graph and
    in the emitted document -- the one test that a promoted boundary keeps
    `format` at all. An expression is the spelling a rewrite would most
    likely break.
    """
    tool = clt({'file': {'type': 'File', 'format': expression}}, {}, canonical=True)
    tools = {LegacyStepId('formatted_sink', SYNTHETIC_NS):
             Tool('/synthetic/formatted_sink.cwl', tool)}
    workflow = {'steps': [{'id': 'formatted_sink'}]}
    _, linked, _ = _typed(workflow, tools)
    result = infer(linked)
    assert result.graph is not None, list(result.diagnostics)
    assert result.graph.workflow_inputs[0].declaration.format == expression
    compiled = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools))
    boundary = next(iter(compiled.artifact.cwl['inputs'].values()))
    assert boundary['format'] == expression


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
    sinks = {(edge.sink.step.name, edge.sink.port) for edge in result.graph.linked_edges
             if edge.origin is EdgeOrigin.INFERRED}
    assert ('file_array_sink', 'files') in sinks
    assert ('count', 'file') not in sinks


@pytest.mark.fast
def test_converter_insertion_reaches_the_same_fixed_point() -> None:
    """Two speculative insertions agree with the live typed compiler."""
    workflow, tools = _insertion_registry()
    _, linked, registry = _typed(workflow, tools)
    result = infer(linked, InferencePolicy(insert_steps_automatically=True),
                   InsertionCatalog.from_registry(registry))
    assert result.graph is not None, list(result.diagnostics)
    assert result.iterations == 3
    assert sum(step.synthesized for step in result.graph.steps) == 2
    live = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                            insert_steps_automatically=True).graph
    assert sum(step.synthesized for step in live.steps) == 2


@pytest.mark.fast
def test_workflow_call_outputs_are_inference_candidates() -> None:
    """A child output remains visible to a later tool in its parent."""
    child = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'child.txt'}}},
    ]}
    workflow = {'steps': [subworkflow_step('sub.wic', child), {'id': 'count'}]}
    _, linked, _ = _typed(workflow)
    result = infer(linked)
    assert result.graph is not None, list(result.diagnostics)
    assert any(edge.sink.step.name == 'count' for edge in result.graph.linked_edges
               if edge.origin is EdgeOrigin.INFERRED)
    # The live compiler infers the same edge from the same document.
    live = compile_hermetic(copy.deepcopy(workflow)).graph
    assert any(edge.sink.step.name == 'count' for edge in live.linked_edges
               if edge.origin is EdgeOrigin.INFERRED)


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
    # The live compiler reaches the same conclusion from the same document.
    live = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                            insert_steps_automatically=True)
    assert sum(step.synthesized for step in live.graph.steps) == 1


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
    assert [Names.of(result.graph).port(port.name) for port in result.graph.workflow_inputs] == [
        'oracle__step__3__use_1___file']
    live = compile_hermetic(copy.deepcopy(workflow), tools=copy.deepcopy(tools),
                            insert_steps_automatically=True)
    assert not any('insert_steps_automatically_' in step['id']
                   for step in live.artifact.cwl['steps'])


@pytest.mark.fast
def test_iteration_exhaustion_is_exactly_wic022() -> None:
    """The limit cannot be removed or exhausted silently."""
    _, linked, _ = _typed({'steps': [{'id': 'mk_file'}]})
    result = infer(linked, InferencePolicy(iteration_limit=0))
    assert result.graph is None
    assert [item.code for item in result.diagnostics] == [
        SophiosErrorCode.FIXED_POINT_NOT_REACHED]


@pytest.mark.fast
def test_format_substrings_do_not_match() -> None:
    """A format name that is merely a substring is not a candidate."""
    tools = {
        LegacyStepId('producer', SYNTHETIC_NS): Tool(
            '/synthetic/producer.cwl',
            clt({}, {'file': {'type': 'File', 'format': 'edam:format_123'}},
                canonical=True)),
        LegacyStepId('consumer', SYNTHETIC_NS): Tool(
            '/synthetic/consumer.cwl',
            clt({'file': {'type': 'File', 'format': 'edam:format_1234'}}, {},
                canonical=True)),
    }
    _, linked, _ = _typed({'steps': [{'id': 'producer'}, {'id': 'consumer'}]}, tools)
    result = infer(linked)
    assert result.graph is not None
    assert not any(edge.origin is EdgeOrigin.INFERRED for edge in result.graph.linked_edges)


@pytest.mark.fast
def test_non_file_output_without_format_can_satisfy_formatted_input() -> None:
    """A non-File cannot declare a format, so omission is unconstrained."""
    tools = {
        LegacyStepId('producer', SYNTHETIC_NS): Tool(
            '/synthetic/producer.cwl', clt({}, {'value': {'type': 'string'}},
                                           canonical=True)),
        LegacyStepId('consumer', SYNTHETIC_NS): Tool(
            '/synthetic/consumer.cwl',
            clt({'value': {'type': 'string', 'format': 'someformat'}}, {},
                canonical=True)),
    }
    _, linked, _ = _typed({'steps': [{'id': 'producer'}, {'id': 'consumer'}]}, tools)
    result = infer(linked)
    assert result.graph is not None
    assert sum(edge.origin is EdgeOrigin.INFERRED for edge in result.graph.linked_edges) == 1


@pytest.mark.fast
def test_unknown_file_format_does_not_outrank_an_exact_match() -> None:
    """A formatless File is unknown, not a wildcard over exact formats."""
    fmt = 'edam:format_3816'
    tools = {
        LegacyStepId('producer', SYNTHETIC_NS): Tool(
            '/synthetic/producer.cwl', clt({}, {
                'matching': {'type': 'File', 'format': fmt},
                'unknown': {'type': 'File'},
            }, canonical=True)),
        LegacyStepId('consumer', SYNTHETIC_NS): Tool(
            '/synthetic/consumer.cwl',
            clt({'file': {'type': 'File', 'format': [fmt]}}, {}, canonical=True)),
    }
    _, linked, _ = _typed({'steps': [{'id': 'producer'}, {'id': 'consumer'}]}, tools)
    result = infer(linked)
    assert result.graph is not None
    assert next(edge for edge in result.graph.linked_edges
                if edge.origin is EdgeOrigin.INFERRED).source.port == 'matching'


def _model_candidate(steps: tuple[StepNode, ...], position: int,
                     sink: Port) -> PortId | None:
    """Independent, deliberately small model of default candidate selection."""
    sink_type = _model_sink_type(steps[position], sink)
    sink_formats = _model_formats(sink.declaration)
    for producer in reversed(steps[:position]):
        for output in reversed(producer.outputs):
            source_type = _model_source_type(producer, output)
            source_formats = _model_formats(output.declaration)
            if (not any('_log_' in name for name in _model_names(output.id.port))
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
    """CWL v1.2: `nested_crossproduct` nests one array per scattered input;
    every other method yields one flat array. Rank is `output_rank`."""
    return layered(port.type, output_rank(step)).canonical


def _model_sink_type(step: StepNode, port: Port) -> Any:
    """One array per time `scatter:` names the input, which is `input_rank`."""
    return layered(port.type, input_rank(step, port.id.port)).canonical


def _model_names(name: PortName) -> tuple[str, ...]:
    return (name.step.name, *_model_names(name.port)) if isinstance(name, DerivedName) else (name,)


def _port(ports: tuple[Port, ...], name: PortName) -> Port:
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


@pytest.mark.fast
def test_a_positional_output_source_is_refused_beside_an_inferred_edge() -> None:
    """`count` leaves `file` to inference, so the document is not fully explicit."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'outputs': {'n': {'type': 'int', 'outputSource': '(2, count)/n'}},
                          'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}},
                                    {'id': 'count'}]})
    assert caught.value.diagnostics[0].code is SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE


@pytest.mark.fast
def test_a_positional_output_source_is_refused_in_a_subworkflow_beside_an_inferred_edge() -> None:
    """The check descends into child workflows."""
    sub = {'outputs': {'n': {'type': 'int', 'outputSource': '(2, count)/n'}},
           'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}, {'id': 'count'}]}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [subworkflow_step('sub.wic', sub)]})
    assert caught.value.diagnostics[0].code is SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE


@pytest.mark.fast
def test_a_positional_output_source_compiles_in_a_fully_explicit_workflow() -> None:
    """With every input bound explicitly, the position is reliable and compiles."""
    compiled = compile_hermetic({'outputs': {'n': {'type': 'int', 'outputSource': '(2, count)/n'}},
                                 'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}},
                                            'out': [{'file': {'wic_anchor': 'f'}}]},
                                           {'id': 'count', 'in': {'file': {'wic_alias': 'f'}}}]})
    assert compiled.artifact.cwl['outputs']['n']['outputSource'] == 'oracle__step__2__count/n'


@pytest.mark.fast
def test_an_untyped_positional_output_source_suggests_the_positional_spelling() -> None:
    """A repeated id is addressed by position, so the hint must keep the position."""
    step = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'outputs': {'o': {'outputSource': '(2, mk_file)/nope'}},
                          'steps': [step, step]})
    assert "Did you mean '(2, mk_file)/file'?" in caught.value.diagnostics[0].message


@pytest.mark.fast
def test_naming_conventions_pick_the_name_matched_output_over_the_last_declared() -> None:
    """With conventions on, `input_structure` takes `output_structure` although
    `output_other` is declared last, which is the one taken with them off."""
    producer = clt({}, {'output_structure': {'type': 'File', 'outputBinding': {'glob': 's'}},
                        'output_other': {'type': 'File', 'outputBinding': {'glob': 'o'}}}, canonical=True)
    consumer = clt({'input_structure': {'type': 'File', 'inputBinding': {'position': 1}}}, {}, canonical=True)
    tools = {**SYNTHETIC_TOOLS,
             LegacyStepId('producer', SYNTHETIC_NS): Tool('/synthetic/producer.cwl', producer),
             LegacyStepId('consumer', SYNTHETIC_NS): Tool('/synthetic/consumer.cwl', consumer)}
    workflow: Yaml = {'steps': [{'id': 'producer'}, {'id': 'consumer'}]}
    chosen: dict[bool, PortName] = {}
    for conventions in (True, False):
        _, linked, _ = _typed(copy.deepcopy(workflow), tools)
        result = infer(linked, InferencePolicy(use_naming_conventions=conventions))
        assert result.graph is not None, list(result.diagnostics)
        (edge,) = [edge for edge in result.graph.linked_edges if edge.sink.step.name == 'consumer']
        chosen[conventions] = edge.source.port
    assert chosen == {True: 'output_structure', False: 'output_other'}
