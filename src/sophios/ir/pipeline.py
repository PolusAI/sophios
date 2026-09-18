"""The typed front door and its temporary handoff to legacy Link/Infer."""
from dataclasses import dataclass
from ..lang import Diagnostics, ParseResult, parse, to_json
from ..wic_types import Yaml
from .. import utils_cwl
from .lower import Lowered, lower
from .resolve import RegistrySnapshot, Resolved, ResolvedDocument, resolve
from .types import WorkflowGraph


@dataclass(frozen=True, slots=True)
class FrontEndResult:
    """Each typed value in the direct Parse-to-Resolve-to-Lower chain."""

    parsed: ParseResult
    resolved: Resolved | None
    lowered: Lowered | None

    @property
    def graph(self) -> WorkflowGraph | None:
        """The lowered graph, when every front-end phase succeeded."""
        return self.lowered.graph if self.lowered is not None else None

    @property
    def diagnostics(self) -> Diagnostics:
        """Diagnostics from the furthest phase reached."""
        if self.lowered is not None:
            return self.lowered.diagnostics
        if self.resolved is not None:
            return self.resolved.diagnostics
        return self.parsed.diagnostics


def front_end(source: str, registry: RegistrySnapshot, *, name: str = 'workflow',
              lang_version: str | None = None) -> FrontEndResult:
    """Run the direct typed chain; no mapping or text adapter sits within it."""
    parsed = parse(source, f'{name}.wic')
    if parsed.document is None or parsed.diagnostics.has_errors:
        return FrontEndResult(parsed, None, None)
    resolved = resolve(parsed.document, registry, name=name, lang_version=lang_version)
    if resolved.document is None or resolved.diagnostics.has_errors:
        return FrontEndResult(parsed, resolved, None)
    return FrontEndResult(parsed, resolved, lower(resolved.document))


def legacy_after_lower(document: ResolvedDocument, graph: WorkflowGraph) -> Yaml:
    """Temporarily adapt typed Lower output into the surviving Link/Infer path.

    This is intentionally after Lower and visibly named legacy.  It is deleted
    once Link and Infer consume ``WorkflowGraph`` directly.
    """
    if graph.name != document.name or len(graph.steps) != len(document.steps):
        raise ValueError('the post-Lower bridge received a graph from another document')
    raw = utils_cwl.desugar_into_canonical_normal_form(to_json(document.source))
    steps: list[Yaml] = raw.get('steps', [])
    bridged: list[Yaml] = []
    for step, resolved in zip(steps, document.steps, strict=True):
        step_copy = dict(step)
        child = resolved.process.child
        if child is not None:
            parentargs = {key: value for key, value in step_copy.items() if key != 'id'}
            child_graph = next(
                candidate for candidate in graph.children
                if candidate.name == child.name
            )
            step_copy = {
                'id': resolved.source.id,
                'subtree': legacy_after_lower(child, child_graph),
                'parentargs': parentargs,
            }
        elif resolved.process.generated:
            step_copy['id'] = resolved.process.key.name
        bridged.append(step_copy)
    raw['steps'] = bridged
    return raw


def legacy_after_link(document: ResolvedDocument, graph: WorkflowGraph) -> Yaml:
    """Hand a linked graph to legacy Infer without asking legacy Link again.

    Edges local to each workflow are written as explicit CWL sources.  Edges
    crossing a workflow-call boundary stay in their authored form until the
    typed Infer extraction removes the final legacy consumer.
    """
    raw = legacy_after_lower(document, graph)
    edges_by_sink = {edge.sink: edge for edge in graph.edges
                     if edge.sink.step.namespace == graph.namespace
                     and edge.source.step.namespace == graph.namespace}
    steps: list[Yaml] = raw.get('steps', [])
    rewritten: list[Yaml] = []
    for raw_step, node, resolved in zip(steps, graph.steps, document.steps, strict=True):
        step = dict(raw_step)
        bindings = dict(step.get('in', {}))
        for binding in node.bindings:
            edge = edges_by_sink.get(binding.sink)
            if edge is None:
                continue
            producer = next(candidate for candidate in graph.steps
                            if candidate.id == edge.source.step)
            producer_id = producer.emission.id if producer.emission is not None else producer.id.name
            bindings[binding.sink.port] = {
                'wic_linked_source': f'{producer_id}/{edge.source.port}'}
        if bindings:
            step['in'] = bindings
        if resolved.process.child is not None:
            child = next(candidate for candidate in graph.children
                         if candidate.name == resolved.process.child.name)
            step['subtree'] = legacy_after_link(resolved.process.child, child)
        if 'out' in step:
            step['out'] = [next(iter(value)) if isinstance(value, dict) and len(value) == 1 else value
                           for value in step['out']]
        rewritten.append(step)
    raw['steps'] = rewritten
    return raw
