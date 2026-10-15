"""Declare the `$namespaces` a workflow's promoted ports need.

A port promoted to a workflow's boundary keeps the `format:` CURIE its process
wrote (`myns:format_1`), so the workflow has to bind `myns` as that process did.
Which prefixes those are, and which steps own the ports that use them, is known
only once every promoted port is placed, in every document: after Infer, and the
`complete` that follows it. Only the document and those steps can disagree about
a CURIE the output contains. A binding of the same prefix by any other step is
no source for it: no CURIE in the output could mean it, so it conflicts with
nothing, and which steps sit beneath a subworkflow does not change that.

A tool's bindings are read from the registry here, not carried through the
phases before: they are a fact about the tool, and the steps that count are the
ones that survive Infer, including the ones it inserted.
"""
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final

from ..lang.diagnostics import Diagnostics, Locator, SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..lang.nodes import OpaqueCwl
from ..lang.spans import SourceSpan
from ..lang.versions import ANNOTATION_NAMESPACE
from .emit import EDAM_NAMESPACE
from .link import attach_step_children
from .resolve import RegistrySnapshot
from .types import DerivedName, StepId, StepNode, WorkflowGraph

#: The prefixes Emit binds itself, whatever a document or a process says of them.
_BOUND_BY_EMIT: Final = frozenset({EDAM_NAMESPACE[0], ANNOTATION_NAMESPACE})


@dataclass(frozen=True, slots=True)
class _Binding:
    """What one source binds a prefix to, and where to point when it disagrees."""

    uri: OpaqueCwl
    source: str
    span: SourceSpan | None = None
    locator: Locator | None = None


def declare_namespaces(graph: WorkflowGraph, registry: RegistrySnapshot) -> WorkflowGraph:
    """`graph` and every workflow beneath it, each declaring the prefixes its promoted formats use.

    A workflow keeps the `$namespaces` it was authored with and adds, for each
    prefix a promoted port's `format:` uses, the URI its document or the step
    that owns the port bound it to. A prefix no promoted format uses is declared
    only if the document wrote it, whatever any tool binds it to.

    Args:
        graph (WorkflowGraph): The completed graph, every promoted port placed.
        registry (RegistrySnapshot): Where each step's tool is looked up.

    Returns:
        WorkflowGraph: The same graph, with `namespaces` set on every document.

    Raises:
        SophiosError: `wic031` for each prefix a promoted format uses that the
            document and the steps owning those ports bind to different URIs.
    """
    diagnostics = Diagnostics()
    declared = _declare(graph, registry, diagnostics)
    if diagnostics.has_errors:
        raise SophiosError(diagnostics)
    return declared


def _declare(graph: WorkflowGraph, registry: RegistrySnapshot, diagnostics: Diagnostics) -> WorkflowGraph:
    children = tuple(_declare(child, registry, diagnostics) for child in graph.children)
    current = attach_step_children(graph, children)
    owners = _promoting_steps(current)
    if not owners:
        return current
    steps = {step.id: step for step in current.steps}
    declared = dict(current.namespaces)
    for prefix, owning in owners.items():
        sources = _bindings(prefix, current, [steps[step_id] for step_id in owning], registry)
        if not sources:
            continue
        first = sources[0]
        for other in sources[1:]:
            if other.uri != first.uri:
                diagnostics.error(
                    SophiosErrorCode.DUPLICATE_DOCUMENT_NAME,
                    f"workflow '{current.name}' promotes a port whose format: uses '{prefix}:', but "
                    f"{first.source} binds the `$namespaces` prefix '{prefix}' to {first.uri!r} and "
                    f"{other.source} binds it to {other.uri!r}; the CURIE can mean only one of them. "
                    'Bind the prefix the same way in both, or use another prefix in one of them.',
                    other.span, other.locator)
        declared.setdefault(prefix, first.uri)
    return replace(current, namespaces=tuple(declared.items()))


def _promoting_steps(graph: WorkflowGraph) -> dict[str, dict[StepId, None]]:
    """For each prefix a promoted port's `format:` spells its CURIEs with, the steps whose ports use it, in order.

    A promoted port is one whose name the compiler derived: it keeps the format
    of the port it exposes, on the step that name is derived from, which is
    owned in this document by that step or by the subworkflow call it sits
    beneath. A port the author wrote states its own, with the prefixes the
    author bound.
    """
    owners: dict[str, dict[StepId, None]] = {}
    for port in (*graph.workflow_inputs, *graph.workflow_outputs):
        declaration = port.declaration
        if not isinstance(port.name, DerivedName) or not declaration.has_format:
            continue
        formats = declaration.format if isinstance(declaration.format, list) else [declaration.format]
        for text in formats:
            if isinstance(text, str) and ':' in text:
                prefix = text.partition(':')[0]
                if prefix not in _BOUND_BY_EMIT:
                    owners.setdefault(prefix, {})[_call_through(graph, port.name.step)] = None
    return owners


def _call_through(graph: WorkflowGraph, step: StepId) -> StepId:
    """The step of `graph` that is `step`, or else the subworkflow call that `step` sits beneath.

    A port an explicit edge crosses scopes with is promoted by every workflow
    between its step and the edge, each under the name derived from that step.
    """
    beneath = step.namespace.parts[len(graph.namespace.parts):]
    return beneath[0] if beneath else step


def _bindings(prefix: str, graph: WorkflowGraph, owners: Iterable[StepNode],
              registry: RegistrySnapshot) -> list[_Binding]:
    """What `prefix` is bound to by `graph`'s own `$namespaces`, then by the process behind each owning step."""
    bound = [_Binding(uri, 'this document') for name, uri in graph.namespaces if name == prefix]
    for step in owners:
        namespaces = dict(_process_namespaces(step, registry))
        if prefix in namespaces:
            bound.append(_Binding(namespaces[prefix], f"step '{step.id.name}'", step.span,
                                  Locator(step=step.id.name, index=step.id.index)))
    return bound


def _process_namespaces(step: StepNode, registry: RegistrySnapshot) -> tuple[tuple[str, OpaqueCwl], ...]:
    """The `$namespaces` the process behind `step` declares: a tool's as written, a subworkflow's as declared."""
    run = step.run
    assert run is not None, 'a completed graph has no unrun step'
    if run.child is not None:
        return run.child.namespaces
    definition = registry.tool(run.process_id)
    assert definition is not None, f'{run.process_id} is in the registry once resolved'
    declared = definition.cwl.get('$namespaces')
    return tuple((str(prefix), uri) for prefix, uri in declared.items()) if isinstance(declared, dict) else ()
