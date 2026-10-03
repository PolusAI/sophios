"""The typed compiler boundary over the phase pipeline."""
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Final

import graphviz
import networkx as nx

from . import utils_graphs
from .ir.complete import complete
from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.emit import emit, emit_job_inputs, surface
from .ir.infer import InferencePolicy, InsertionCatalog, infer
from .ir.link import link
from .ir.namespaces import declare_namespaces
from .ir.frontdoor import SourceBundle
from .ir.pipeline import front_end
from .ir.realtime import extract
from .ir.resolve import RegistrySnapshot
from .ir.names import Names
from .ir.types import EdgeOrigin, WorkflowGraph
from .lang import versions
from .lang.diagnostics import Diagnostic, Locator, Severity, SophiosError
from .lang.nodes import CwlRecord, UnresolvedName
from .lang.error_codes import SophiosErrorCode
from .wic_types import (
    CompilerOptions,
    GraphData,
    GraphReps,
    GraphSettings,
)


# pylint: disable-next=too-many-locals
def compile_source(bundle: SourceBundle,
                   compiler_options: CompilerOptions,
                   graph_settings: GraphSettings,
                   *,
                   relative_run_path: bool,
                   testing: bool,
                   graph_target: GraphReps | None = None) -> CompilationResult:
    """Compile a parsed root workflow and every workflow it reaches.

    The one door. A bundle read from files carries the spans of the text its
    author wrote, so a diagnostic names a position the reader can open; one
    the Python API built carries the span of the Python line that made each node.
    """
    if not testing:
        print(' starting compilation of', bundle.name)
    selected_version = versions.resolve(
        compiler_options.get('lang_version'), bundle.lang_version_pins)
    front = front_end(bundle.parsed, bundle.registry, name=bundle.name,
                      lang_version=selected_version)
    if front.graph is None or front.resolved is None or front.resolved.document is None:
        raise SophiosError(front.diagnostics)
    if not front.graph.steps:
        raise SophiosError.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                                 'workflows must define at least one step')
    authored, realtime = extract(front.graph)
    _check_unresolved_names(authored, compiler_options['allow_raw_cwl'])
    for note in dict.fromkeys(_authored_spelling_notes(authored)):
        print(note, file=sys.stderr)

    prepared = complete(authored)
    linked = link(prepared)
    if linked.graph is None:
        raise SophiosError(linked.diagnostics)
    policy = InferencePolicy(
        disabled=compiler_options['inference_disable'],
        use_naming_conventions=compiler_options['inference_use_naming_conventions'],
        renaming_conventions=tuple(compiler_options.get('renaming_conventions', ())),
        insert_steps_automatically=compiler_options['insert_steps_automatically'],
        format_rules=tuple(compiler_options.get('inference_rules', {}).items()),
        strict=compiler_options['inference_strict'],
    )
    inferred = infer(linked.graph, policy, InsertionCatalog.from_registry(bundle.registry))
    if inferred.graph is None:
        raise SophiosError(inferred.diagnostics)
    _check_positional_sources(inferred.graph)
    graph = declare_namespaces(complete(inferred.graph), bundle.registry)
    names = Names.of(graph)
    graph_reps = _project_graph(graph, names, graph_settings, graph_target)
    artifact = _artifact_tree(graph, names, bundle.registry, graph_settings, graph_reps,
                              relative_run_path=relative_run_path,
                              partial_failure=compiler_options['partial_failure_enable'])
    if not testing:
        print('finishing compilation of', bundle.name)
    return CompilationResult(graph, artifact, inferred.diagnostics, realtime)


def _check_positional_sources(graph: WorkflowGraph) -> None:
    """A positional `outputSource` is a claim about where a step sits, which holds
    only in a document inference left alone; refuse it next to an inferred edge."""
    inferred = [edge for edge in graph.linked_edges if edge.origin is EdgeOrigin.INFERRED]
    positional = [port for port in graph.workflow_outputs if port.positional]
    if inferred and positional:
        names = ', '.join(repr(str(port.name)) for port in positional)
        raise SophiosError([Diagnostic(
            Severity.ERROR, SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE,
            f'{graph.name}.wic names its outputs {names} by step position, but inference placed '
            f'{len(inferred)} edge(s) in it; a position is reliable only in a fully explicit '
            'workflow. Bind every input with !* or !ii, or address the step by its id.',
            graph.span)])
    for child in graph.children:
        _check_positional_sources(child)


#: The shape of a name only Emit writes, `<workflow>__step__<n>__<id>`. Recognised, never taken apart.
_GENERATED_NAME: Final = re.compile(r'__step__\d+__')

#: `inputs.name` and `inputs["name"]` inside a `when:` expression.
_EXPRESSION_INPUT: Final = re.compile(r'inputs(?:\.([A-Za-z_][\w-]*)|\[\s*["\']([^"\']+)["\']\s*\])')


def _expression_inputs(expression: object) -> tuple[str, ...]:
    """Every step input a CWL expression reads, in order of first mention."""
    if not isinstance(expression, str):
        return ()
    return tuple(dict.fromkeys(a or b for a, b in _EXPRESSION_INPUT.findall(expression)))


# pylint: disable-next=too-many-locals
def _authored_spelling_notes(graph: WorkflowGraph) -> list[str]:
    """One plain line for each place a document addresses a step by a name the compiler generates.

    Such a spelling still resolves, so nothing here fails the compile; each line says what to
    write instead. Three places are checked: an `outputSource` that names its step as
    `<workflow>__step__<n>__<id>`, an `outputSource` that names a step by an id more than one
    step has (it means the first), and a `when:` that reads an input its step does not declare
    (a generated name, or one CWL evaluates as null).
    A document the Python API built names the script that built it.
    """
    notes: list[str] = []
    spans = [step.span for step in graph.steps if step.span is not None]
    if not spans:
        return [note for child in graph.children for note in _authored_spelling_notes(child)]
    file = spans[0].file
    sources = {port.name: port.output_source for port in graph.workflow_outputs}
    positional = {port.name for port in graph.workflow_outputs if port.positional}
    ids = [step.id.name for step in graph.steps]
    for name, source in graph.output_mapping:
        written = str(sources[name]).rsplit('/', 1)[0]
        if name in positional:
            continue
        repeated = ids.count(source.step.name) > 1
        position = [step.id for step in graph.steps].index(source.step) + 1
        address = f'({position}, {source.step.name})' if repeated else source.step.name
        caveat = ' (a position holds only while every edge in the workflow is explicit)' if repeated else ''
        if written == source.step.name:
            if repeated:
                notes.append(f"Warning! {file}: output {str(name)!r} has outputSource '{sources[name]}', but "
                             f'{ids.count(source.step.name)} steps have the id {written!r} and it means the first. '
                             f"Write '{address}/{source.port}' to say so{caveat}.")
            continue
        notes.append(f'Warning! {file}: output {str(name)!r} names its step {written!r}, a name the '
                     f"compiler generates. Write '{address}/{source.port}' instead{caveat}.")
    for step in graph.steps:
        declared = {str(port.id.port) for port in step.inputs}
        for read in _expression_inputs(dict(step.interpreted).get('when')):
            if _GENERATED_NAME.search(read):
                notes.append(f"Warning! {file}: step {step.id.name!r} reads inputs.{read} in `when:`, a name "
                             "the compiler generates. Declare the port in the callee's `inputs:` and read that name.")
            elif read not in declared:
                notes.append(f"Warning! {file}: step {step.id.name!r} reads inputs.{read} in `when:`, which "
                             'its process does not declare; CWL evaluates it as null.')
    for child in graph.children:
        notes.extend(_authored_spelling_notes(child))
    return notes


def _check_unresolved_names(graph: WorkflowGraph, allow_raw_cwl: bool,
                            names: Names | None = None) -> None:
    # Authored text is recognized by comparing it with what each declared
    # input is spelled as -- an author may write a derived name -- never by
    # taking the text apart.
    names = names or Names.of(graph)
    declared = {names.port(port.name) for port in graph.workflow_inputs}
    for step in graph.steps:
        for binding in step.bindings:
            value = binding.value
            for reference in value.sources if isinstance(value, CwlRecord) else (value,):
                if not isinstance(reference, UnresolvedName) or reference.name in declared or allow_raw_cwl:
                    continue
                name = reference.name
                raise SophiosError.error(
                    SophiosErrorCode.UNRESOLVED_INPUT,
                    f'Did you forget to use !ii before {name} in {graph.name}.wic?',
                    'If you want to compile the workflow anyway, use --allow_raw_cwl',
                    span=getattr(value, 'span', None),
                    locator=Locator(step=step.id.name, index=step.id.index, port=names.port(binding.sink.port)))
    for child in graph.children:
        _check_unresolved_names(child, allow_raw_cwl, names)


def _artifact_tree(graph: WorkflowGraph, names: Names, registry: RegistrySnapshot,
                   graph_settings: GraphSettings,
                   graph_reps: GraphReps | None = None,
                   *, relative_run_path: bool = True,
                   partial_failure: bool = False) -> CompilationArtifact:
    """One artifact per emitted document, each surfaced exactly once."""
    document = surface(graph, names, relative_run_path=relative_run_path,
                       partial_failure=partial_failure)
    children: list[CompilationArtifact] = []
    for step in document.steps:
        assert step.run is not None
        child = step.run.child
        if child is not None:
            child_reps = _project_graph(child, names, graph_settings)
            children.append(_artifact_tree(child, names, registry, graph_settings, child_reps,
                                           relative_run_path=relative_run_path,
                                           partial_failure=partial_failure))
            continue
        key = step.run.process_id
        definition = registry.tool(key)
        if definition is None:
            raise SophiosError.error(
                SophiosErrorCode.SUBWORKFLOW_INVALID,
                f'process {key.namespace}/{key.name} disappeared after resolution')
        leaf_graph = utils_graphs.get_graph_reps(key.name)
        # Named by the `run:` the parent emits, so the file written is the one it runs.
        children.append(CompilationArtifact(
            (names.step(step.id),), Path(step.run.target).stem,
            definition.run_path, deepcopy(definition.cwl), {}, None,
            leaf_graph,
        ))

    reps = graph_reps or _project_graph(graph, names, graph_settings)
    return CompilationArtifact(
        tuple(names.step(part) for part in graph.namespace.parts),
        graph.name,
        f'{graph.name}.cwl',
        emit(document, names),
        emit_job_inputs(document, names),
        graph,
        reps,
        tuple(children),
    )


def _project_graph(graph: WorkflowGraph, names: Names, settings: GraphSettings,
                   target: GraphReps | None = None) -> GraphReps:
    reps = target or GraphReps(graphviz.Digraph(name=f'cluster_{graph.name}'),
                               nx.DiGraph(), GraphData(graph.name))
    reps.networkx.clear()
    reps.graphdata.name = graph.name
    reps.graphdata.nodes = []
    reps.graphdata.edges = []
    reps.graphdata.subgraphs = []
    for step in graph.steps:
        assert step.run is not None
        name = names.qualified(step.id)
        label = names.step(step.id) if settings['graph_label_stepname'] else step.id.name
        attrs = {'label': label, 'shape': 'box', 'style': 'rounded, filled',
                 'fillcolor': 'lightblue'}
        reps.graphviz.node(name, **attrs)
        reps.networkx.add_node(name)
        reps.graphdata.nodes.append((name, attrs))
    for edge in graph.edges:
        source = names.qualified(edge.source.step)
        sink = names.qualified(edge.sink.step)
        # Explicit edges are blue; an inferred one takes the theme's font
        # colour, as docs/tutorials/multistep.md says, so a reader can tell
        # what the document said from what the compiler decided.
        edge_attrs: dict[str, str] = {'color': 'blue'}
        if edge.origin is EdgeOrigin.INFERRED:
            edge_attrs['color'] = 'black' if settings['graph_dark_theme'] else 'white'
        if settings['graph_label_edges']:
            edge_attrs['label'] = names.port(edge.source.port)
        if not reps.networkx.has_edge(source, sink) or settings['graph_label_edges']:
            if source != sink:
                reps.graphviz.edge(source, sink, **edge_attrs)
            reps.networkx.add_edge(source, sink)
            reps.graphdata.edges.append((source, sink, edge_attrs))
    for child in graph.children:
        child_reps = _project_graph(child, names, settings)
        reps.graphdata.subgraphs.append(child_reps.graphdata)
        reps.networkx.add_nodes_from(child_reps.networkx.nodes)
        reps.networkx.add_edges_from(child_reps.networkx.edges)
        if len(graph.namespace.parts) < settings['graph_inline_depth']:
            reps.graphviz.subgraph(child_reps.graphviz)
    return reps
