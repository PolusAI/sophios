"""Lowering a resolved document to a graph.

Five claims: lowering is total, a malformed graph cannot be constructed, every
reference the graph holds names a port that exists, passthrough is never read,
and the result does not depend on iteration order.

The test-side resolver constructs fully typed phase input without calling the
production resolver, so these need no registry, filesystem, or config.
"""
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sophios.ir import (
    Binding,
    DeferredObligation,
    Direction,
    Edge,
    Namespace,
    Port,
    PortId,
    PortType,
    RegistryKey,
    ResolvedDocument,
    ResolvedPort,
    ResolvedProcess,
    ResolvedStep,
    StepId,
    StepNode,
    WorkflowGraph,
)
from sophios.ir.lower import lower
from sophios.ir.declarations import port_declaration
from sophios.ir.types import AuthoredName
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.lang.nodes import Document, InlineLiteral, Step
from sophios.lang.parser import parse
from sophios.lang.spans import SourceSpan

from . import ast_strategies as strat
from .hermetic import COVERAGE, ORACLE, compile_hermetic, compile_hermetic_cwl, subworkflow_step

REPO_ROOT = Path(__file__).resolve().parents[2]


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
def test_a_step_may_be_invoked_twice() -> None:
    """`append` twice is the language, not a mistake.

    Sequence-form `steps:` exists so a tool can be invoked more than once, and
    a shipped tutorial does it. An IR keyed on the authored id rejects a real
    document; the occurrence index is what the compiler already means by
    `{stem}__step__{i}__{key}`.
    """
    document = parse((REPO_ROOT / 'docs' / 'tutorials' / 'append_twice.wic')
                     .read_text(encoding='utf-8'), 'append_twice.wic').document
    assert document is not None
    result = lower(_resolved(document))
    assert result.graph is not None, [d.code.value for d in result.diagnostics]
    assert [(s.id.index, s.id.name) for s in result.graph.steps] == [(1, 'append'), (2, 'append')]


@pytest.mark.fast
@pytest.mark.parametrize(('where', 'source'), [
    ('an input', 'steps:\n- id: s\n  in:\n    "": !* e\n'),
    ('an out: entry', 'steps:\n- id: s\n  out:\n  - "": f\n'),
    ('an edge definition', 'steps:\n- id: s\n  out:\n  - f: !& ""\n'),
    ('an edge reference', 'steps:\n- id: s\n  in:\n    f: !* ""\n'),
])
def test_a_name_the_document_left_empty_is_reported(where: str, source: str) -> None:
    """`parse` accepts these; the types refuse them. Lowering has to bridge that.

    Refusing an unnamed port is right, but the refusal is a `ValueError`, and
    every one of these is a document the parser hands back. Reported here, the
    caller gets a diagnostic rather than a traceback -- the same treatment an
    unnamed step already had.

    Out of reach of `documents()`, which draws names from the synthetic
    registry and so never draws an empty one. That is the right generator for
    a workflow the compiler can resolve, so these stay examples.
    """
    result = _lower(source)
    assert result.graph is None, where
    assert [d.code.value for d in result.diagnostics] == ['wic027'], where


@pytest.mark.fast
def test_both_edge_positions_treat_an_empty_name_the_same() -> None:
    """`!& ""` and `!* ""` agree.

    They did not: the reference raised while the definition was accepted and
    put `''` into the edge table as a live producer name, where it would match
    any later `!* ""` that managed not to raise first.
    """
    definition = _lower('steps:\n- id: s\n  out:\n  - f: !& ""\n')
    reference = _lower('steps:\n- id: s\n  in:\n    f: !* ""\n')
    assert (definition.graph is None) == (reference.graph is None)
    assert {d.code for d in definition.diagnostics} == {d.code for d in reference.diagnostics}


@pytest.mark.fast
def test_a_document_the_parser_recovered_never_raises() -> None:
    """Totality over what `parse` returns, not over what it accepts.

    A recovered document is exactly when a caller is least able to handle an
    exception, and a result carrying neither a graph nor a diagnostic is worse
    than the exception it replaced -- `ok` is False and nothing says why.
    """
    for source in ('steps:\n- id: ""\n  in: {}\n', 'steps:\n- \n', 'steps:\n- id:\n'):
        document = parse(source, 'recovered.wic').document
        if document is None:
            continue
        result = lower(_resolved(document))
        assert result.graph is not None or len(result.diagnostics) > 0, source

    # A step the parser never built, so it carries no span at all.
    handmade_document = Document(steps=(Step(id=''),))
    handmade = lower(_resolved(handmade_document))
    assert handmade.graph is None and len(handmade.diagnostics) > 0


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
def test_a_reference_before_its_definition_is_reported() -> None:
    """`!* e` above its `!& e` is `wic025`, as the reference and compiler say.

    Resolving it by pre-scanning every definition would make Lower accept a
    document the compiler refuses, and silently change an ordering rule.
    """
    result = _lower('steps:\n- id: use\n  in:\n    f: !* e\n- id: mk\n  out:\n  - file: !& e\n')
    assert [d.code.value for d in result.diagnostics] == ['wic025']


@pytest.mark.fast
def test_a_name_defined_twice_is_reported() -> None:
    """Keeping the first definition and dropping the second leaves nothing to
    report it with, here or in any later phase."""
    result = _lower('steps:\n- id: a\n  out:\n  - f: !& e\n- id: b\n  out:\n  - f: !& e\n')
    assert [d.code.value for d in result.diagnostics] == ['wic026']


@pytest.mark.fast
def test_a_sidecar_out_mentioning_the_name_does_not_mask_a_real_duplicate() -> None:
    """A `wic:` sidecar `out:` entry (§4.1.1, accepted since `wic019` no
    longer rejects it there) is inert passthrough, never itself a second
    definition — `basic.wic` relies on exactly this to place `min.tpr`'s one
    definition at a distance without it colliding with anything. A real
    duplicate among the document's own steps must still be reported once,
    whether or not a sidecar block happens to mention the same name.
    """
    result = _lower(
        'wic:\n  steps:\n    (1, a):\n      out:\n      - f: {wic_anchor: e}\n'
        'steps:\n- id: a\n  out:\n  - f: !& e\n- id: b\n  out:\n  - f: !& e\n')
    assert [d.code.value for d in result.diagnostics] == ['wic026']


@pytest.mark.fast
def test_a_step_with_no_id_is_reported_not_raised() -> None:
    """The parser recovers such a document, which is exactly when a caller is
    least able to handle an exception.

    `wic007` rather than `wic006`, matching what the parser already reports for
    the same document: the id is present and empty, not missing.
    """
    result = _lower('steps:\n- id: ""\n  in: {}\n')
    assert result.graph is None and [d.code.value for d in result.diagnostics] == ['wic007']


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


_SPAN = SourceSpan('probe.wic', 1, 1, 1, 1)


@st.composite
def _hostile_graphs(draw: st.DrawFn) -> Any:
    """A construction that violates one invariant, drawn rather than listed."""
    ns = Namespace(tuple(draw(st.lists(st.builds(StepId, st.just(Namespace()), st.integers(1, 4),
                                                 st.text('ab', min_size=1, max_size=2)),
                                       max_size=2))))
    one = StepId(ns, draw(st.integers(1, 4)), draw(st.text('xy', min_size=1, max_size=2)))
    two = StepId(ns, one.index + draw(st.integers(1, 3)), one.name)
    name = AuthoredName(draw(st.text('pq', min_size=1, max_size=2)))
    out = PortId(one, Direction.OUTPUT, name)
    inp = PortId(one, Direction.INPUT, name)
    elsewhere = PortId(two, Direction.INPUT, name)
    port = Port(inp, PortType(None))
    return draw(st.sampled_from([
        lambda: Edge(inp, inp),                                   # a port to itself
        lambda: Edge(inp, PortId(two, Direction.INPUT, name)),    # input to input
        lambda: Edge(out, PortId(two, Direction.OUTPUT, name)),   # output to output
        lambda: DeferredObligation(out, name, PortType(None)),    # an output awaiting a value
        lambda: StepNode(one, inputs=(Port(elsewhere, PortType(None)),)),
        lambda: StepNode(one, outputs=(port,)),                   # an input listed as an output
        lambda: StepNode(one, bindings=(Binding(inp, InlineLiteral(1, _SPAN)),)),
        lambda: StepId(ns, 0, name),                              # a zero-based occurrence
        lambda: StepId(ns, 1, ''),                                # an unnamed occurrence
        lambda: PortId(one, Direction.INPUT, AuthoredName('')),   # an unnamed port
        lambda: Namespace((StepId(Namespace(), 1, ''),)),         # an unnamed enclosing step
        lambda: WorkflowGraph(ns, steps=(StepNode(one), StepNode(one))),
        lambda: WorkflowGraph(ns, output_mapping=((name, out),)),
        lambda: WorkflowGraph(ns, input_mapping=((name, (inp,)),)),
        lambda: WorkflowGraph(ns, explicit_edge_defs=((name, out),)),
        lambda: WorkflowGraph(Namespace((StepId(Namespace(), 1, 'elsewhere'),)),
                              steps=(StepNode(one),)),
    ]))


@pytest.mark.fast
@given(_hostile_graphs())
@COVERAGE
def test_a_malformed_graph_cannot_be_constructed(build: Any) -> None:
    """The invariants live in `__post_init__`, so nothing can skip them.

    Drawn rather than listed: a fixed table only ever proves the cases someone
    thought of, and every gap this file has had was a case nobody listed.
    """
    with pytest.raises(ValueError):
        build()


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


class _Spelling(str):
    """A name that notes when it is written out."""

    spelled = False

    def __repr__(self) -> str:
        self.spelled = True
        return super().__repr__()


@pytest.mark.fast
def test_a_reference_is_not_spelled_unless_it_is_reported() -> None:
    """A derived name written out prints every step id it was exposed through,
    which is the cost that made nesting exponential, so the name a reference is
    held under is written for the report and not for every graph."""
    step = StepId(Namespace(), 1, 'step')
    source = PortId(step, Direction.OUTPUT, AuthoredName('o'))
    sink = PortId(step, Direction.INPUT, AuthoredName('i'))
    name = _Spelling('e')

    WorkflowGraph(
        Namespace(),
        steps=(StepNode(step, inputs=(Port(sink, PortType(None)),), outputs=(Port(source, PortType(None)),)),),
        explicit_edge_defs=((name, source),),
        explicit_edge_calls=((name, sink),),
        input_mapping=((AuthoredName(name), (sink,)),))

    assert not name.spelled


@pytest.mark.fast
def test_the_graph_owns_no_mutable_container() -> None:
    """A frozen graph holding a dict can have its invariants invalidated after
    the constructor checked them, which makes checking them theatre."""
    graph = _lower('steps:\n- id: s\n  in:\n    f: !ii 1\n').graph
    assert graph is not None
    for name in ('steps', 'explicit_edge_defs', 'explicit_edge_calls',
                 'input_mapping', 'output_mapping', 'passthrough'):
        assert isinstance(getattr(graph, name), tuple), name


@pytest.mark.fast
@pytest.mark.parametrize('scatter', ['[1]', '[null]', '5', '{a: 1}', '[name, 1]'])
def test_a_scatter_entry_that_is_not_a_name_is_wic032(scatter: str) -> None:
    """`scatter:` names inputs. An entry that is no name at all, or a
    `scatter:` that is neither a name nor a list, names none of them: it is
    reported rather than dropped or emitted verbatim."""
    result = _lower(f'steps:\n- id: mk_file\n  in: {{name: !ii [a, b]}}\n  scatter: {scatter}\n')

    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert SophiosErrorCode.UNKNOWN_SCATTER_PORT in codes, codes


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
def test_a_positional_output_source_addresses_the_occurrence_at_that_index() -> None:
    """Two `s` steps; `(2, s)/f` is the second one, and nothing else can say so."""
    result = _lower('steps:\n- id: s\n  out: [f]\n- id: s\n  out: [f]\n'
                    'outputs:\n  o:\n    type: File\n    outputSource: (2, s)/f\n')
    assert result.graph is not None, list(result.diagnostics)
    (_name, source), = result.graph.output_mapping
    assert source.step.index == 2


@pytest.mark.fast
@pytest.mark.parametrize('address', ['(2, t)/f', '(3, s)/f'], ids=['wrong-name', 'out-of-range'])
def test_a_positional_output_source_that_disagrees_with_the_document_is_wic039(address: str) -> None:
    """A wrong name or an index past the last step is refused as wic039."""
    result = _lower('steps:\n- id: s\n  out: [f]\n- id: s\n  out: [f]\n'
                    f'outputs:\n  o:\n    type: File\n    outputSource: {address}\n')
    assert [d.code for d in result.diagnostics] == [SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE]


@pytest.mark.fast
def test_a_positional_output_source_naming_a_port_the_step_lacks_is_wic028() -> None:
    """The step is known, so the missing port is reported with the step's outputs."""
    result = _lower('steps:\n- id: s\n  out: [f]\n- id: s\n  out: [f]\n'
                    'outputs:\n  o:\n    type: File\n    outputSource: (2, s)/nope\n')
    assert [d.code for d in result.diagnostics] == [SophiosErrorCode.UNDECLARED_PORT]
    assert all("'nope'" in d.message and 'its outputs are: f' in d.message for d in result.diagnostics)


@pytest.mark.fast
def test_a_record_source_before_its_definition_is_reported() -> None:
    """A record's `!*` source resolves as a bare `!*` does: above its `!& e` it is `wic025`."""
    result = _lower('steps:\n- id: use\n  in:\n    f: !cwl {source: !* e}\n- id: mk\n  out:\n  - file: !& e\n')
    assert [d.code.value for d in result.diagnostics] == ['wic025']


@pytest.mark.fast
def test_a_record_with_several_sources_takes_none_from_outside_its_document() -> None:
    """One input crosses a workflow boundary through one relay, so it cannot carry several outside sources."""
    result = _lower('inputs:\n  w: string\nsteps:\n- id: use\n  in:\n    f: !cwl {source: [!* e, w]}\n')
    assert [d.code.value for d in result.diagnostics] == ['wic025']


@pytest.mark.fast
def test_a_record_binds_an_input_its_process_does_not_declare() -> None:
    """The input is a port of the step with no declaration; any other value there is `wic028`."""
    workflow: dict[str, Any] = {'inputs': {'go': 'boolean'}, 'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}, 'go': {'wic_raw_cwl': {'source': 'go'}}}}]}
    (step,) = compile_hermetic(workflow).graph.steps
    (port,) = [port for port in step.inputs if port.id.port == 'go']
    assert port.declaration is None
    workflow['steps'][0]['in']['go'] = 'go'
    with pytest.raises(SophiosError) as caught:
        compile_hermetic(workflow)
    assert SophiosErrorCode.UNDECLARED_PORT in {d.code for d in caught.value.diagnostics}


@pytest.mark.fast
def test_scatter_ports_keep_the_authored_order() -> None:
    """`scatter: [name, file]` on xform is emitted in that order: under
    `nested_crossproduct` the order is the nesting of the output arrays."""
    compiled = compile_hermetic_cwl({'steps': [{'id': 'xform', 'scatter': ['name', 'file'],
                                                'scatterMethod': 'nested_crossproduct',
                                                'in': {'name': {'wic_inline_input': ['a']},
                                                       'file': {'wic_inline_input': ['x.txt']}}}]})
    assert compiled['steps'][0]['scatter'] == ['name', 'file']
