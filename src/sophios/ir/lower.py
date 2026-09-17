"""Build a `WorkflowGraph` from a parsed document.

Total in the sense `parse` is: every document either lowers or produces
diagnostics, and nothing here raises.

READS THE AST AND NOTHING ELSE -- no registry, no filesystem, no config.
Resolving a step's tool against the environment is `Resolve`'s job, so a port's
type is what the document declared and inference has not run.
"""
from dataclasses import dataclass
from typing import Final

from ..lang.diagnostics import Code, Diagnostics
from ..lang.nodes import Document, EdgeRef, InputValue, Step
from .types import (
    DeferredObligation,
    Edge,
    Namespace,
    Port,
    PortId,
    PortType,
    StepNode,
    WorkflowGraph,
)

#: A port whose type the document does not declare. Not invented here: a guess
#: would be indistinguishable from a declaration by the time anything read it.
UNDECLARED: Final = PortType(declared=None)


@dataclass(frozen=True, slots=True)
class Lowered:
    """The result of lowering: a graph when one could be built, and diagnostics."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        """Whether a graph was produced with no errors."""
        return self.graph is not None and not self.diagnostics.has_errors


def _input_ports(namespace: Namespace, step: Step) -> tuple[Port, ...]:
    """One port per bound input, in written order."""
    return tuple(
        Port(PortId(namespace, step.id, name), UNDECLARED, span=step.span)
        for name, _ in step.inputs
    )


def _output_ports(namespace: Namespace, step: Step) -> tuple[Port, ...]:
    """One port per `out:` entry, in written order."""
    return tuple(
        Port(PortId(namespace, step.id, binding.name), UNDECLARED, span=binding.span)
        for binding in step.outputs
    )


def _edge_definitions(namespace: Namespace, steps: tuple[Step, ...]) -> dict[str, PortId]:
    """Which port each explicit edge name is defined by.

    A name defined twice keeps the first: the compiler already reports the
    repeat as `wic026` before lowering is reached.
    """
    defined: dict[str, PortId] = {}
    for step in steps:
        for binding in step.outputs:
            if binding.edge_def is not None:
                defined.setdefault(binding.edge_def.name,
                                   PortId(namespace, step.id, binding.name))
    return defined


def lower(document: Document, namespace: Namespace | None = None) -> Lowered:
    """Lower a parsed document to a graph.

    An input bound with `!*` becomes an edge when the name is defined in this
    document, and a `DeferredObligation` when it is not — which is what a
    subworkflow's reference to a parent's edge is. Deciding between the two is
    the whole of what lowering knows about binding; discharging an obligation
    belongs to `Link`, which can see the enclosing graph.

    Args:
        document (Document): The parsed document.
        namespace (Namespace | None): Where this document sits. The root's
            namespace is empty.

    Returns:
        Lowered: The graph, and any diagnostics raised on the way.
    """
    diagnostics = Diagnostics()
    here = namespace if namespace is not None else Namespace()
    _report_repeated_steps(document, diagnostics)
    if diagnostics.has_errors:
        return Lowered(None, diagnostics)
    defined = _edge_definitions(here, document.steps)

    nodes: list[StepNode] = []
    edges: list[Edge] = []
    obligations: list[DeferredObligation] = []

    for step in document.steps:
        inputs = _input_ports(here, step)
        nodes.append(StepNode(
            namespace=here,
            name=step.id,
            inputs=inputs,
            outputs=_output_ports(here, step),
            interpreted=step.interpreted,
            passthrough=step.passthrough,
            span=step.span,
        ))
        for (name, value), port in zip(step.inputs, inputs, strict=True):
            _bind(value, port, defined, edges, obligations)

    return Lowered(WorkflowGraph(
        namespace=here,
        steps=tuple(nodes),
        edges=tuple(edges),
        obligations=tuple(obligations),
        explicit_edge_defs=dict(defined),
        passthrough=document.passthrough,
    ), diagnostics)


def _report_repeated_steps(document: Document, diagnostics: Diagnostics) -> None:
    """Report a step name used twice, at the second step rather than the graph.

    `WorkflowGraph` refuses a repeat at construction, but by then the only thing
    to point at is the whole graph.
    """
    seen: set[str] = set()
    for step in document.steps:
        if step.id in seen and step.span is not None:
            diagnostics.error(
                Code.DUPLICATE_KEY,
                f"step id '{step.id}' is used more than once; a step name identifies one step",
                step.span)
        seen.add(step.id)


def _bind(value: InputValue, port: Port, defined: dict[str, PortId],
          edges: list[Edge], obligations: list[DeferredObligation]) -> None:
    """Record what satisfies one bound input."""
    if not isinstance(value, EdgeRef):
        return
    source = defined.get(value.name)
    if source is None:
        obligations.append(DeferredObligation(port.id, value.name, port.type, span=value.span))
    else:
        edges.append(Edge(source, port.id, span=value.span))
