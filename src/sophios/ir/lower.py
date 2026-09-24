"""Build a `WorkflowGraph` from a parsed document.

Total in the sense `parse` is: every document either lowers or produces
diagnostics, and nothing here raises -- including on a document the parser
recovered rather than accepted, which is exactly when a caller is least able to
handle an exception.

READS THE AST AND NOTHING ELSE -- no registry, no filesystem, no config.
Resolving a step's tool against the environment is `Resolve`'s job, so a port's
type is what the document declared and inference has not run.
"""
from typing import Final
from dataclasses import dataclass

from ..lang.cwl import CWL_VERSION
from ..lang.diagnostics import Diagnostics, Locator
from ..lang.error_codes import SophiosErrorCode
from ..lang.nodes import Document, EdgeRef, InputValue, UnresolvedName
from ..lang.versions import (ANNOTATION_KEY, ANNOTATION_NAMESPACE,
                             ANNOTATION_NAMESPACE_URI)
from .declarations import port_declaration
from .resolve import ResolvedDocument, ResolvedStep
from .types import (
    Binding,
    BoundaryDeclaration,
    DeferredObligation,
    Direction,
    Edge,
    emitted_step_id,
    Namespace,
    Port,
    PortId,
    ProcessRun,
    Resolution,
    StepEmission,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)


@dataclass(frozen=True, slots=True)
class Lowered:
    """The result of lowering: a graph when one could be built, and diagnostics."""

    graph: WorkflowGraph | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        """Whether a graph was produced with no errors."""
        return self.graph is not None and not self.diagnostics.has_errors


def lower(document: ResolvedDocument,
          namespace: Namespace | None = None) -> Lowered:
    """Lower a resolved document to a graph.

    An input bound with `!*` becomes an edge when the name was defined *earlier*
    in this document, and a `DeferredObligation` when it was never defined here
    -- which is what a subworkflow's reference to a parent's edge is. A name
    defined only *later* is neither: the compiler refuses it and the language
    reference requires the definition first, so it is reported rather than
    quietly resolved.

    Args:
        document (ResolvedDocument): The resolved document.
        namespace (Namespace | None): Where this document sits. The root's
            namespace is empty.

    Returns:
        Lowered: The graph, and any diagnostics raised on the way.
    """
    return _lower_resolved(document, namespace)


# pylint: disable-next=too-many-locals
def _lower_resolved(document: ResolvedDocument,
                    namespace: Namespace | None = None) -> Lowered:
    """Lower a fully resolved document without consulting its registry again."""
    diagnostics = Diagnostics()
    here = namespace if namespace is not None else Namespace()
    identities = _step_identities(document.source, here, diagnostics)
    if identities is None or not _every_name_is_present(document.source, diagnostics):
        return Lowered(None, diagnostics)
    defined_anywhere = _edge_definitions(
        identities, Document(steps=tuple(step.source for step in document.steps)), diagnostics)
    defined_so_far: dict[str, PortId] = {}
    nodes: list[StepNode] = []
    children: list[WorkflowGraph] = []

    for identity, resolved_step in zip(identities, document.steps, strict=True):
        node = _resolved_step_node(document.name, identity, resolved_step,
                                   defined_so_far, defined_anywhere, diagnostics)
        nodes.append(node)
        for authored in resolved_step.source.outputs:
            if authored.edge_def is not None:
                defined_so_far.setdefault(
                    authored.edge_def.name,
                    PortId(identity, Direction.OUTPUT, authored.name))
        if resolved_step.process.child is not None:
            child_namespace = here.child(node.emission.id if node.emission is not None else identity.name)
            child = _lower_resolved(resolved_step.process.child, child_namespace)
            if child.graph is not None:
                children.append(child.graph)
            for diagnostic in child.diagnostics:
                diagnostics._append(diagnostic)  # pylint: disable=protected-access

    passthrough = dict(document.source.passthrough)
    workflow_inputs = _workflow_ports(passthrough.get('inputs', {}), output=False)
    workflow_outputs = _workflow_ports(passthrough.get('outputs', {}), output=True)
    workflow_input_names = {port.name for port in workflow_inputs}
    input_mapping = tuple(
        (name, tuple(binding.sink for node in nodes for binding in node.bindings
                     if _unresolved_name(binding) == name))
        for name in (port.name for port in workflow_inputs)
    )
    output_mapping = tuple(
        (port.name, source)
        for port in workflow_outputs
        for source in [_output_port(nodes, port.output_source)]
        if source is not None
    )
    namespaces_raw = passthrough.get('$namespaces', {})
    namespaces = tuple(namespaces_raw.items()) if isinstance(namespaces_raw, dict) else ()
    namespaces = tuple((str(key), value) for key, value in namespaces
                       if key != ANNOTATION_NAMESPACE) + (
                           (ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI),)
    schemas_raw = passthrough.get('$schemas', ())
    schemas = tuple(schemas_raw) if isinstance(schemas_raw, list) else ()
    # CWL also spells `requirements:` as a list, and a bare `requirements:`
    # parses to None. Sophios merges into the mapping form, so that is the one
    # shape the graph models; any other is residue it carries opaquely and
    # Emit writes back unchanged. Dropping it silently rewrote the document.
    requirements_raw = passthrough.get('requirements', {})
    requirements = (tuple(requirements_raw.items())
                    if isinstance(requirements_raw, dict) else ())
    reserved = {'inputs', 'outputs', '$namespaces', '$schemas',
                'cwlVersion', 'class', ANNOTATION_KEY}
    if isinstance(requirements_raw, dict):
        reserved.add('requirements')
    opaque = tuple((key, value) for key, value in document.source.passthrough
                   if key not in reserved)
    field_order_list = [key for key in document.source.field_order if key != 'wic']
    for key in ('cwlVersion', 'class', '$namespaces', '$schemas', 'inputs',
                ANNOTATION_KEY, 'outputs'):
        if key not in field_order_list:
            field_order_list.append(key)
    field_order: tuple[str, ...] = tuple(field_order_list)
    if requirements and 'requirements' not in field_order:
        field_order += ('requirements',)
    known_ports = {port.id for node in nodes for port in node.inputs + node.outputs}
    graph = WorkflowGraph(
        namespace=here,
        steps=tuple(nodes),
        explicit_edge_defs=tuple((name, port) for name, port in defined_anywhere.items()
                                 if port in known_ports),
        explicit_edge_calls=tuple((obligation.name, obligation.sink)
                                  for node in nodes for obligation in (
                                      binding.resolution for binding in node.bindings)
                                  if isinstance(obligation, DeferredObligation)),
        input_mapping=tuple((name, sinks) for name, sinks in input_mapping
                            if name in workflow_input_names and sinks),
        output_mapping=output_mapping,
        passthrough=opaque,
        name=document.name,
        lang_version=document.lang_version,
        cwl_version=CWL_VERSION,
        workflow_inputs=workflow_inputs,
        workflow_outputs=workflow_outputs,
        requirements=requirements,
        namespaces=namespaces,
        schemas=schemas,
        children=tuple(children),
        field_order=field_order,
    )
    return Lowered(graph if not diagnostics.has_errors else None, diagnostics)


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
#: Bound on a `python_script` step to build its tool, never passed to it.
_GENERATION_PARAMETERS: Final = ('script', 'dockerPull')


def _resolved_step_node(workflow_name: str, identity: StepId, resolved: ResolvedStep,
                        defined_so_far: dict[str, PortId], defined_anywhere: dict[str, PortId],
                        diagnostics: Diagnostics) -> StepNode:
    source = resolved.source
    declared_inputs = {port.name: port.declaration for port in resolved.process.inputs}
    declared_outputs = {port.name: port.declaration for port in resolved.process.outputs}
    # A generated process consumes its generation parameters rather than
    # declaring them: `script` becomes the command and `dockerPull` a container
    # hint, so neither survives into the tool's interface. The document still
    # binds them, and that binding is what produced the tool -- not a port the
    # step got wrong.
    consumed = _GENERATION_PARAMETERS if resolved.process.generated else ()
    for name, _ in source.inputs:
        if name not in declared_inputs and name not in consumed:
            diagnostics.error(
                SophiosErrorCode.UNDECLARED_PORT,
                f"step '{source.id}' binds '{name}', which its resolved process does not declare",
                source.span,
                Locator(step=source.id, index=identity.index, port=name),
            )
    for authored in source.outputs:
        if authored.name not in declared_outputs:
            diagnostics.error(
                SophiosErrorCode.UNDECLARED_PORT,
                f"step '{source.id}' names output '{authored.name}', which its resolved process "
                'does not declare',
                authored.span,
                Locator(step=source.id, index=identity.index, port=authored.name),
            )
    inputs = tuple(Port(PortId(identity, Direction.INPUT, name), declaration.type,
                        declaration, source.span)
                   for name, declaration in declared_inputs.items())
    outputs = tuple(Port(PortId(identity, Direction.OUTPUT, name), declaration.type,
                         declaration, source.span)
                    for name, declaration in declared_outputs.items())
    by_input = {port.id.port: port for port in inputs}
    bindings = tuple(Binding(by_input[name].id, value,
                             _resolve(value, by_input[name], defined_so_far,
                                      defined_anywhere, diagnostics))
                     for name, value in source.inputs if name in by_input)
    interpreted = dict(source.interpreted)
    emitted_id = emitted_step_id(workflow_name, identity.index, source.id)
    field_order = list(source.field_order)
    if 'id' in field_order:
        field_order.remove('id')
    field_order.insert(0, 'id')
    for key in ('run', 'in', 'out'):
        if key not in field_order:
            field_order.append(key)
    emission = StepEmission(
        id=emitted_id,
        # Empty, deliberately. Every authored input naming a declared port
        # becomes a `Binding` above, and Complete writes each one: a literal to
        # a workflow input, a raw reference to its expression, a name to
        # itself, an edge to its source. Seeding this with the spelling the
        # document used made the *default* state invalid CWL -- a `wic_alias:`
        # nobody replaced reached the runner instead of the compiler.
        inputs=(),
        run=ProcessRun(resolved.process.run_path, resolved.process.key),
        outputs=tuple(declared_outputs),
        scatter=interpreted.get('scatter'),
        scatter_method=interpreted.get('scatterMethod'),
        when=interpreted.get('when'),
        passthrough=source.passthrough,
        field_order=tuple(field_order),
    )
    return StepNode(identity, inputs, outputs, bindings, source.interpreted,
                    source.passthrough, source.span, emission,
                    _inference_rules(resolved.sidecar))


def _workflow_ports(raw: object, *, output: bool) -> tuple[WorkflowPort, ...]:
    if not isinstance(raw, dict):
        return ()
    ports: list[WorkflowPort] = []
    for name, declaration_raw in raw.items():
        # Written under the workflow's own `inputs:`/`outputs:`, so it is a
        # boundary declaration by where it appears. Asserted, not reduced:
        # `boundary_declaration` drops what a tool may say and a boundary may
        # not, and here that would discard fields the author wrote at the
        # boundary on purpose.
        declaration = BoundaryDeclaration(port_declaration(declaration_raw, output=output))
        has_source = output and isinstance(declaration_raw, dict) \
            and 'outputSource' in declaration_raw
        source = declaration_raw.get('outputSource') if has_source else None
        ports.append(WorkflowPort(str(name), declaration, source, has_source))
    return tuple(ports)


def _output_port(nodes: list[StepNode], raw: object) -> PortId | None:
    if not isinstance(raw, str) or '/' not in raw:
        return None
    step_name, port_name = raw.rsplit('/', 1)
    for node in nodes:
        emitted = node.emission.id if node.emission is not None else node.id.name
        if step_name in {emitted, node.id.name}:
            return next((port.id for port in node.outputs if port.id.port == port_name), None)
    return None


def _unresolved_name(binding: Binding) -> str | None:
    """Return a workflow-input reference without traversing opaque payloads."""
    match binding:
        case Binding(value=UnresolvedName(name=name)):
            return name
        case _:
            return None


def _inference_rules(sidecar: object) -> tuple[tuple[str, str], ...]:
    """Normalize the local output-selection policy carried by a step sidecar."""
    entries = getattr(sidecar, 'entries', ())
    raw = dict(entries).get('inference') if entries else None
    if not isinstance(raw, dict):
        return ()
    return tuple((str(name), str(rule)) for name, rule in raw.items())


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
            diagnostics.error(SophiosErrorCode.EMPTY_STEP_ID,
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
    for index, step in enumerate(document.steps, start=1):
        for name, value in step.inputs:
            if not name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"step '{step.id}' binds an input with no name", step.span,
                                  Locator(step=step.id, index=index))
                found = True
            if isinstance(value, EdgeRef) and not value.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"'!*' on '{step.id}.{name}' names no edge", value.span,
                                  Locator(step=step.id, index=index, port=name))
                found = True
        for binding in step.outputs:
            if not binding.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"step '{step.id}' declares an out: entry with no name",
                                  binding.span, Locator(step=step.id, index=index))
                found = True
            if binding.edge_def is not None and not binding.edge_def.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"'!&' on '{step.id}.{binding.name}' defines no edge",
                                  binding.edge_def.span,
                                  Locator(step=step.id, index=index, port=binding.name))
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
                    SophiosErrorCode.DUPLICATE_EDGE_DEF,
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
            SophiosErrorCode.UNDEFINED_EDGE,
            f"'!* {value.name}' is referenced before '!& {value.name}' defines it.",
            value.span)
        return None
    return DeferredObligation(port.id, value.name, port.type, span=value.span)
