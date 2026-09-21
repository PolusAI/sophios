"""The typed compiler boundary over the phase pipeline."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import graphviz
import networkx as nx
import yaml

from . import utils_graphs
from .input_output import NoAliasDumper
from .ir.complete import complete
from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.emit import emit, emit_job_inputs
from .ir.infer import InferencePolicy, InsertionCatalog, infer
from .ir.link import link
from .ir.pipeline import front_end
from .ir.resolve import RegistryKey, RegistrySnapshot
from .ir.types import PortId, WorkflowGraph
from .lang import versions
from .lang.diagnostics import SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import (
    CompilerOptions,
    GraphData,
    GraphReps,
    GraphSettings,
    StepId as LegacyStepId,
    Tools,
    Yaml,
    YamlTagPaths,
    YamlTree,
)


def compile_document(yaml_tree_ast: YamlTree,
                     compiler_options: CompilerOptions,
                     graph_settings: GraphSettings,
                     yaml_tag_paths: YamlTagPaths,
                     tools: Tools,
                     *,
                     relative_run_path: bool,
                     testing: bool,
                     graph_target: GraphReps | None = None) -> CompilationResult:
    """Compile one assembled document through the typed pipeline."""
    if not testing:
        print(' starting compilation of', yaml_tree_ast.step_id.stem)

    source_tree = _adapt_subinterpreter_source(yaml_tree_ast.yml, yaml_tag_paths)
    source, workflow_sources, source_documents = _source_bundle(source_tree)
    registry = RegistrySnapshot.from_tools(tools, workflows=workflow_sources)
    selected_version = versions.resolve(
        compiler_options.get('lang_version'), _lang_version_pins(yaml_tree_ast.yml))
    front = front_end(source, registry, name=Path(yaml_tree_ast.step_id.stem).stem,
                      lang_version=selected_version)
    if front.graph is None or front.resolved is None or front.resolved.document is None:
        raise SophiosError(front.diagnostics)
    if not front.graph.steps:
        raise SophiosError.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                                 'workflows must define at least one step')
    _check_unresolved_names(front.graph, compiler_options['allow_raw_cwl'])

    prepared = complete(
        front.graph,
        relative_run_path=relative_run_path,
        partial_failure=compiler_options['partial_failure_enable'],
    )
    linked = link(prepared)
    if linked.graph is None:
        raise SophiosError(linked.diagnostics)
    prepared = complete(
        linked.graph,
        relative_run_path=relative_run_path,
        partial_failure=compiler_options['partial_failure_enable'],
    )
    policy = InferencePolicy(
        disabled=compiler_options['inference_disable'],
        use_naming_conventions=compiler_options['inference_use_naming_conventions'],
        renaming_conventions=tuple(compiler_options.get('renaming_conventions', ())),
        insert_steps_automatically=compiler_options['insert_steps_automatically'],
        format_rules=tuple(compiler_options.get('inference_rules', {}).items()),
    )
    inferred = infer(prepared, policy, InsertionCatalog.from_registry(registry))
    if inferred.graph is None:
        raise SophiosError(inferred.diagnostics)
    graph = complete(
        inferred.graph,
        relative_run_path=relative_run_path,
        partial_failure=compiler_options['partial_failure_enable'],
    )
    compiled = emit(graph)
    graph_reps = _project_graph(graph, graph_settings, graph_target)
    artifact = _artifact_tree(graph, registry, source_documents, graph_settings,
                              yaml_tree_ast.yml, graph_reps)
    if not testing:
        print('finishing compilation of', yaml_tree_ast.step_id.stem)
    assert artifact.cwl == compiled
    return CompilationResult(graph, artifact)


def _source_bundle(root: Yaml) -> tuple[str, dict[tuple[str, str], str],
                                        dict[tuple[str, ...], Yaml]]:
    """Detach loader-attached subtrees into an immutable Resolve snapshot."""
    workflows: dict[tuple[str, str], str] = {}
    documents: dict[tuple[str, ...], Yaml] = {}
    detached_root = _detach_sources(root, (), workflows, documents)
    return _dump_source(detached_root), workflows, documents


def _adapt_subinterpreter_source(root: Yaml, yaml_tag_paths: YamlTagPaths) -> Yaml:
    """Supply the narrow runtime adapter's three location parameters.

    This compatibility concern terminates before Parse.  The typed phases see
    ordinary literal bindings and have no special case for the watcher.
    """
    copied = deepcopy(root)
    values = {
        'root_workflow_yml_path': str(Path(yaml_tag_paths['yaml']).parent.absolute()),
        'cachedir_path': str(Path(yaml_tag_paths['cachedir']).absolute()),
        'homedir': yaml_tag_paths['homedir'],
    }

    def visit(document: Yaml) -> None:
        raw_steps = document.get('steps', [])
        if isinstance(raw_steps, dict):
            steps = list(raw_steps.items())
        elif isinstance(raw_steps, list):
            steps = [(None, step) for step in raw_steps]
        else:
            return
        for authored_name, step in steps:
            if not isinstance(step, dict):
                continue
            name = step.get('id', authored_name or '')
            if Path(str(name)).stem == 'cwl_subinterpreter':
                inputs = step.setdefault('in', {})
                if isinstance(inputs, dict):
                    inputs.update({name: {'wic_inline_input': value}
                                   for name, value in values.items()})
            subtree = step.get('subtree')
            if isinstance(subtree, dict):
                visit(subtree)

    visit(copied)
    return copied


def _detach_sources(document: Yaml, path: tuple[str, ...],
                    workflows: dict[tuple[str, str], str],
                    documents: dict[tuple[str, ...], Yaml]) -> Yaml:
    """Recursively replace attached child bodies with registry entries."""
    copied = deepcopy(document)
    documents[path] = deepcopy(document)
    raw_steps = copied.get('steps', [])
    if isinstance(raw_steps, dict):
        steps = [{'id': str(name), **({} if body is None else body)}
                 for name, body in raw_steps.items()]
    else:
        steps = list(raw_steps) if isinstance(raw_steps, list) else raw_steps
    if not isinstance(steps, list):
        copied['steps'] = steps
        return copied
    sidecar = copied.get('wic') or {}
    sidecar_steps = sidecar.get('steps', {}) if isinstance(sidecar, dict) else {}
    if isinstance(sidecar, dict) and isinstance(sidecar.get('implementations'), dict):
        copied['wic'] = {**sidecar, 'implementations': _detach_implementations(
            sidecar, path, workflows, documents)}
    detached: list[Yaml] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or 'subtree' not in step:
            detached.append(step)
            continue
        step_name = str(step.get('id', ''))
        child_name = Path(step_name).stem
        metadata = sidecar_steps.get(f'({index}, {step_name})', {}) \
            if isinstance(sidecar_steps, dict) else {}
        namespace = 'global'
        if isinstance(metadata, dict) and isinstance(metadata.get('wic'), dict):
            namespace = str(metadata['wic'].get('namespace', 'global'))
        workflow_name = Path(path[-1]).stem if path else ''
        child_path = (*path, _emitted_step_name(workflow_name, index, step_name))
        child = _detach_sources(step['subtree'], child_path, workflows, documents)
        workflows[(namespace, child_name)] = _dump_source(child)
        parentargs = step.get('parentargs', {})
        detached.append({'id': step_name,
                         **(deepcopy(parentargs) if isinstance(parentargs, dict) else {})})
    copied['steps'] = detached
    return copied


def _detach_implementations(sidecar: Yaml, path: tuple[str, ...],
                            workflows: dict[tuple[str, str], str],
                            documents: dict[tuple[str, ...], Yaml]) -> Yaml:
    """Move inline implementation bodies into the registry, as subtrees are.

    The loader leaves each body attached and rekeys the mapping by ``StepId``,
    which no longer round-trips through YAML. Resolve selects an implementation
    by name from the registry, so the names are what the source needs to carry.
    """
    namespace = str(sidecar.get('namespace', 'global'))
    detached: Yaml = {}
    for key, body in sidecar['implementations'].items():
        name = Path(key.stem if isinstance(key, LegacyStepId) else str(key)).stem
        if isinstance(body, dict) and body:
            child = _detach_sources(body, (*path, name), workflows, documents)
            workflows[(namespace, name)] = _dump_source(child)
        detached[name] = {}
    return detached


def _dump_source(document: Yaml) -> str:
    return yaml.dump(document, Dumper=NoAliasDumper, sort_keys=False,
                     line_break='\n', indent=2)


def _emitted_step_name(workflow: str, index: int, name: str) -> str:
    return f'{workflow}__step__{index}__{name}'


def _check_unresolved_names(graph: WorkflowGraph, allow_raw_cwl: bool) -> None:
    declared = {port.name for port in graph.workflow_inputs}
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
        _check_unresolved_names(child, allow_raw_cwl)


def _artifact_tree(graph: WorkflowGraph, registry: RegistrySnapshot,
                   documents: dict[tuple[str, ...], Yaml], graph_settings: GraphSettings,
                   root_source: Yaml,
                   graph_reps: GraphReps | None = None) -> CompilationArtifact:
    children: list[CompilationArtifact] = []
    for step in graph.steps:
        assert step.emission is not None
        child = step.emission.run.child
        if child is not None:
            child_reps = _project_graph(child, graph_settings)
            children.append(_artifact_tree(
                child, registry, documents, graph_settings,
                documents.get(child.namespace.parts, {}), child_reps))
            continue
        namespace, name = step.emission.run.process_id.split('/', 1)
        definition = registry.tool(RegistryKey(namespace, name))
        if definition is None:
            raise SophiosError.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                                     f'process {namespace}/{name} disappeared after resolution')
        leaf_graph = utils_graphs.get_graph_reps(name)
        children.append(CompilationArtifact(
            (step.emission.id,), Path(definition.run_path).stem,
            definition.run_path, deepcopy(definition.cwl), {}, {}, None,
            leaf_graph,
        ))

    compiled = emit(graph)
    reps = graph_reps or _project_graph(graph, graph_settings)
    source = deepcopy(root_source if not graph.namespace.parts
                      else documents.get(graph.namespace.parts, root_source))
    return CompilationArtifact(
        graph.namespace.parts,
        graph.name,
        f'{graph.name}.cwl',
        compiled,
        emit_job_inputs(graph),
        source,
        graph,
        reps,
        tuple(children),
    )


def _project_graph(graph: WorkflowGraph, settings: GraphSettings,
                   target: GraphReps | None = None) -> GraphReps:
    reps = target or GraphReps(graphviz.Digraph(name=f'cluster_{graph.name}'),
                               nx.DiGraph(), GraphData(graph.name))
    reps.networkx.clear()
    reps.graphdata.name = graph.name
    reps.graphdata.nodes = []
    reps.graphdata.edges = []
    reps.graphdata.subgraphs = []
    reps.graphdata.ranksame = []
    for step in graph.steps:
        assert step.emission is not None
        name = '___'.join((*graph.namespace.parts, step.emission.id))
        label = step.emission.id if settings['graph_label_stepname'] else step.id.name
        attrs = {'label': label, 'shape': 'box', 'style': 'rounded, filled',
                 'fillcolor': 'lightblue'}
        reps.graphviz.node(name, **attrs)
        reps.networkx.add_node(name)
        reps.graphdata.nodes.append((name, attrs))
    for edge in graph.edges:
        source = _graph_step_name(graph, edge.source)
        sink = _graph_step_name(graph, edge.sink)
        edge_attrs: dict[str, str] = {}
        if settings['graph_label_edges']:
            edge_attrs['label'] = edge.source.port
        if not reps.networkx.has_edge(source, sink) or settings['graph_label_edges']:
            if source != sink:
                reps.graphviz.edge(source, sink, **edge_attrs)
            reps.networkx.add_edge(source, sink)
            reps.graphdata.edges.append((source, sink, edge_attrs))
    for child in graph.children:
        child_reps = _project_graph(child, settings)
        reps.graphdata.subgraphs.append(child_reps.graphdata)
        reps.networkx.add_nodes_from(child_reps.networkx.nodes)
        reps.networkx.add_edges_from(child_reps.networkx.edges)
        if len(graph.namespace.parts) < settings['graph_inline_depth']:
            reps.graphviz.subgraph(child_reps.graphviz)
    return reps


def _graph_step_name(graph: WorkflowGraph, port: PortId) -> str:
    step = next(step for step in graph.all_steps if step.id == port.step)
    assert step.emission is not None
    return '___'.join((*step.id.namespace.parts, step.emission.id))


def _lang_version_pins(node: Any, _path: frozenset[int] = frozenset()) -> tuple[str, ...]:
    """Collect every language pin in an assembled source tree."""
    if id(node) in _path:
        return ()
    path = _path | {id(node)}
    pins: list[str] = []
    if isinstance(node, dict):
        wic = node.get('wic')
        if isinstance(wic, dict) and 'lang_version' in wic:
            value = wic['lang_version']
            pins.append(value if isinstance(value, str) else str(value))
        for value in node.values():
            pins.extend(_lang_version_pins(value, path))
    elif isinstance(node, list):
        for value in node:
            pins.extend(_lang_version_pins(value, path))
    return tuple(pins)
