"""Build a `WorkflowGraph` from a parsed document.

Total in the sense `parse` is: every document either lowers or produces
diagnostics, and nothing here raises -- including on a document the parser
recovered rather than accepted, which is exactly when a caller is least able to
handle an exception.

READS THE AST AND NOTHING ELSE -- no registry, no filesystem, no config.
Resolving a step's tool against the environment is `Resolve`'s job, so a port's
type is what the document declared and inference has not run.
"""
from dataclasses import dataclass

from ..lang.diagnostics import Code, Diagnostics
from ..lang.nodes import Document, EdgeRef, InputValue, Step
from .types import (
    Binding,
    DeferredObligation,
    Direction,
    Edge,
    Namespace,
    Port,
    PortId,
    PortType,
    Resolution,
    StepId,
    StepNode,
    WorkflowGraph,
)

#: A port whose type the document does not declare. Not invented here: a guess
#: would be indistinguishable from a declaration by the time anything read it.
UNDECLARED = PortType(declared=None)


@dataclass(frozen=True, slots=True)
class Lowered:
    """The result of lowering: a graph when one could be built, and diagnostics."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        """Whether a graph was produced with no errors."""
        return self.graph is not None and not self.diagnostics.has_errors


def lower(document: Document, namespace: Namespace | None = None) -> Lowered:
    """Lower a parsed document to a graph.

    An input bound with `!*` becomes an edge when the name was defined *earlier*
    in this document, and a `DeferredObligation` when it was never defined here
    -- which is what a subworkflow's reference to a parent's edge is. A name
    defined only *later* is neither: the compiler refuses it and the language
    reference requires the definition first, so it is reported rather than
    quietly resolved.

    Args:
        document (Document): The parsed document.
        namespace (Namespace | None): Where this document sits. The root's
            namespace is empty.

    Returns:
        Lowered: The graph, and any diagnostics raised on the way.
    """
    diagnostics = Diagnostics()
    here = namespace if namespace is not None else Namespace()

    identities = _step_identities(document, here, diagnostics)
    if identities is None or not _every_name_is_present(document, diagnostics):
        return Lowered(None, diagnostics)

    defined_anywhere = _edge_definitions(identities, document, diagnostics)
    defined_so_far: dict[str, PortId] = {}
    nodes: list[StepNode] = []

    for step_id, step in zip(identities, document.steps, strict=True):
        inputs = tuple(Port(PortId(step_id, Direction.INPUT, name), UNDECLARED, span=step.span)
                       for name, _ in step.inputs)
        outputs = tuple(Port(PortId(step_id, Direction.OUTPUT, b.name), UNDECLARED, span=b.span)
                        for b in step.outputs)
        bindings = tuple(
            Binding(port.id, value,
                    _resolve(value, port, defined_so_far, defined_anywhere, diagnostics))
            for (_, value), port in zip(step.inputs, inputs, strict=True))
        nodes.append(StepNode(step_id, inputs, outputs, bindings,
                              step.interpreted, step.passthrough, step.span))
        for binding in step.outputs:
            if binding.edge_def is not None:
                defined_so_far.setdefault(binding.edge_def.name,
                                          PortId(step_id, Direction.OUTPUT, binding.name))

    return Lowered(WorkflowGraph(
        namespace=here,
        steps=tuple(nodes),
        explicit_edge_defs=tuple(defined_anywhere.items()),
        passthrough=document.passthrough,
    ), diagnostics)


def _step_identities(document: Document, here: Namespace,
                     diagnostics: Diagnostics) -> tuple[StepId, ...] | None:
    """One occurrence identity per step, or None when a step cannot have one.

    A repeated `id` is not an error: sequence-form `steps:` exists so that a
    tool can be invoked twice, and `docs/tutorials/append_twice.wic` does. What
    cannot be lowered is a step the parser recovered without a name, which is
    reported rather than raised.
    """
    identities: list[StepId] = []
    for index, step in enumerate(document.steps, start=1):
        if not step.id:
            diagnostics.error(Code.EMPTY_STEP_ID,
                              'a step needs an id before it can be lowered', step.span)
            return None
        identities.append(StepId(here, index, step.id))
    return tuple(identities)


def _every_name_is_present(document: Document, diagnostics: Diagnostics) -> bool:
    """Report every position where the document left a name empty.

    The types refuse an unnamed port or obligation, and refusing is right --
    but the refusal is a `ValueError`, and `parse` accepts `in: {"": ...}`.
    Checking here is what keeps lowering total: the same reason `_step_identities`
    reports an unnamed step rather than letting `StepId` raise.

    All of them report one code. They are one mistake in four positions, and a
    reader who wrote nothing where a name goes is not helped by being told which
    of the four the checker noticed first.
    """
    found = False
    for step in document.steps:
        for name, value in step.inputs:
            if not name:
                diagnostics.error(Code.EMPTY_NAME,
                                  f"step '{step.id}' binds an input with no name", step.span)
                found = True
            if isinstance(value, EdgeRef) and not value.name:
                diagnostics.error(Code.EMPTY_NAME,
                                  f"'!*' on '{step.id}.{name}' names no edge", value.span)
                found = True
        for binding in step.outputs:
            if not binding.name:
                diagnostics.error(Code.EMPTY_NAME,
                                  f"step '{step.id}' declares an out: entry with no name",
                                  binding.span)
                found = True
            if binding.edge_def is not None and not binding.edge_def.name:
                diagnostics.error(Code.EMPTY_NAME,
                                  f"'!&' on '{step.id}.{binding.name}' defines no edge",
                                  binding.edge_def.span)
                found = True
    return not found


def _edge_definitions(identities: tuple[StepId, ...], document: Document,
                      diagnostics: Diagnostics) -> dict[str, PortId]:
    """Which port defines each explicit edge name, reporting any defined twice.

    Every definition is visited before any is dropped, so a repeat is a
    diagnostic rather than a silently discarded second entry that no later phase
    could report.
    """
    defined: dict[str, PortId] = {}
    for step_id, step in zip(identities, document.steps, strict=True):
        for binding in step.outputs:
            if binding.edge_def is None:
                continue
            name = binding.edge_def.name
            if name in defined:
                diagnostics.error(
                    Code.DUPLICATE_EDGE_DEF,
                    f"'&{name}' is defined more than once. An edge name identifies one producer.",
                    binding.edge_def.span)
                continue
            defined[name] = PortId(step_id, Direction.OUTPUT, binding.name)
    return defined


def _resolve(value: InputValue, port: Port, defined_so_far: dict[str, PortId],
             defined_anywhere: dict[str, PortId], diagnostics: Diagnostics) -> Resolution:
    """Where one bound input gets its value from, if it needs a producer."""
    if not isinstance(value, EdgeRef):
        return None
    source = defined_so_far.get(value.name)
    if source is not None:
        return Edge(source, port.id, span=value.span)
    if value.name in defined_anywhere:
        diagnostics.error(
            Code.UNDEFINED_EDGE,
            f"'!* {value.name}' is referenced before '!& {value.name}' defines it.",
            value.span)
        return None
    return DeferredObligation(port.id, value.name, port.type, span=value.span)
