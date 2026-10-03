"""The typed compiler boundary over the phase pipeline."""
import sys
from copy import deepcopy
from dataclasses import replace
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
from .ir.resolve import RegistrySnapshot
from .ir.names import Names
from .ir.types import AuthoredName, Binding, EdgeOrigin, PortName, WorkflowGraph
from .lang import versions
from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.nodes import InlineLiteral
from .lang.spans import SourceSpan
from .lang.error_codes import SophiosErrorCode
from .wic_types import (
    CompilerOptions,
    GraphData,
    GraphReps,
    GraphSettings,
    YamlTagPaths,
)


# pylint: disable-next=too-many-locals
def compile_source(bundle: SourceBundle,
                   compiler_options: CompilerOptions,
                   graph_settings: GraphSettings,
                   yaml_tag_paths: YamlTagPaths,
                   *,
                   relative_run_path: bool,
                   testing: bool,
                   graph_target: GraphReps | None = None) -> CompilationResult:
    """Compile a parsed root workflow and every workflow it reaches.

    The one door. A bundle read from files carries the spans of the text its
    author wrote, so a diagnostic names a position the reader can open; one
    the Python API built carries none, and its diagnostics name a `Locator`.
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
    _check_unresolved_names(front.graph, compiler_options['allow_raw_cwl'])
    for note in dict.fromkeys(_authored_spelling_notes(front.graph)):
        print(note, file=sys.stderr)

    prepared = complete(_bind_subinterpreter_locations(front.graph, yaml_tag_paths))
    linked = link(prepared)
    if linked.graph is None:
        raise SophiosError(linked.diagnostics)
    policy = InferencePolicy(
        disabled=compiler_options['inference_disable'],
        use_naming_conventions=compiler_options['inference_use_naming_conventions'],
        renaming_conventions=tuple(compiler_options.get('renaming_conventions', ())),
        insert_steps_automatically=compiler_options['insert_steps_automatically'],
        format_rules=tuple(compiler_options.get('inference_rules', {}).items()),
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
    return CompilationResult(graph, artifact)


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


#: The runtime adapter's own declared inputs, whose values come from the
#: invocation rather than from the document.
_LOCATION_SPAN: Final = SourceSpan('<subinterpreter locations>', 1, 1, 1, 1)


def _bind_subinterpreter_locations(graph: WorkflowGraph,
                                   yaml_tag_paths: YamlTagPaths) -> WorkflowGraph:
    """Bind the three locations the runtime adapter declares as inputs, leaving any existing binding alone."""
    values: dict[PortName, str] = {
        AuthoredName('root_workflow_yml_path'): str(Path(yaml_tag_paths['yaml']).parent.absolute()),
        AuthoredName('cachedir_path'): str(Path(yaml_tag_paths['cachedir']).absolute()),
        AuthoredName('homedir'): yaml_tag_paths['homedir'],
    }
    steps = []
    for step in graph.steps:
        if Path(step.id.name).stem != 'cwl_subinterpreter':
            steps.append(step)
            continue
        already = {binding.sink.port for binding in step.bindings}
        supplied = tuple(
            Binding(port.id, InlineLiteral(values[port.id.port], _LOCATION_SPAN))
            for port in step.inputs
            if port.id.port in values and port.id.port not in already)
        steps.append(replace(step, bindings=step.bindings + supplied))
    return replace(
        graph, steps=tuple(steps),
        children=tuple(_bind_subinterpreter_locations(child, yaml_tag_paths)
                       for child in graph.children))


def _authored_spelling_notes(graph: WorkflowGraph) -> list[str]:
    """One plain line for each place a document addresses a step by a name the compiler generates.

    Such a spelling still resolves, so nothing here fails the compile; each line says what to
    write instead. The Python API builds its documents without spans: nobody wrote that text,
    so there is no author to tell.
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
        if name in positional or written == source.step.name:
            continue
        repeated = ids.count(source.step.name) > 1
        position = [step.id for step in graph.steps].index(source.step) + 1
        address = f'({position}, {source.step.name})' if repeated else source.step.name
        caveat = ' (a position holds only while every edge in the workflow is explicit)' if repeated else ''
        notes.append(f'Warning! {file}: output {str(name)!r} names its step {written!r}, a name the '
                     f"compiler generates. Write '{address}/{source.port}' instead{caveat}.")
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
            if value.__class__.__name__ != 'UnresolvedName':
                continue
            name = getattr(value, 'name')
            if name not in declared and not allow_raw_cwl:
                raise SophiosError.error(
                    SophiosErrorCode.UNRESOLVED_INPUT,
                    f'Warning! Did you forget to use !ii before {name} in {graph.name}.wic?',
                    'If you want to compile the workflow anyway, use --allow_raw_cwl')
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
