"""Build a `WorkflowGraph` from a parsed document.

Total in the sense `parse` is: every document either lowers or produces
diagnostics, and nothing here raises -- including on a document the parser
recovered rather than accepted, which is exactly when a caller is least able to
handle an exception.

READS THE AST AND NOTHING ELSE -- no registry, no filesystem, no config.
Resolving a step's tool against the environment is `Resolve`'s job, so a port's
type is what the document declared and inference has not run. The one fact of
the environment here is the document's directory, which Resolve recorded: a
relative location in a `default:` is read against it.
"""
import difflib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from ..lang.diagnostics import Diagnostics, Locator
from ..lang.error_codes import SophiosErrorCode
from ..lang.names import check_written_name
from ..lang.nodes import CwlRecord, Document, EdgeRef, InputValue, OpaqueCwl, Step, UnresolvedName, WicSidecar
from ..lang.parser import Grammar
from ..lang.spans import SourceSpan
from .declarations import port_declaration
from .locations import absolute_paths
from .resolve import ResolvedDocument, ResolvedStep
from .types import (
    AuthoredName,
    Binding,
    BoundaryDeclaration,
    DeferredObligation,
    Direction,
    Edge,
    Namespace,
    PhaseResult,
    Port,
    PortId,
    PortName,
    PortType,
    Resolution,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
)


# pylint: disable-next=too-many-locals
def lower(document: ResolvedDocument, here: Namespace = Namespace()) -> PhaseResult:
    """Lower a resolved document to a graph, without consulting its registry again.

    An input bound with `!*` becomes an edge when the name was defined *earlier*
    in this document, and a `DeferredObligation` when it was never defined here
    -- which is what a subworkflow's reference to a parent's edge is. A name
    defined only *later* is neither: the compiler refuses it and the language
    reference requires the definition first, so it is reported rather than
    quietly resolved.

    Args:
        document (ResolvedDocument): The resolved document.
        here (Namespace): Where this document sits. The root's namespace is empty.

    Returns:
        PhaseResult: The graph, and any diagnostics raised on the way.
    """
    diagnostics = Diagnostics()
    identities = _step_identities(document.source, here, diagnostics)
    if identities is None or not _every_name_is_present(document.source, diagnostics):
        return PhaseResult(None, diagnostics)
    _check_written_names(document, not here.parts, diagnostics)
    defined = _edge_definitions(identities, [step.source for step in document.steps], diagnostics)
    nodes: list[StepNode] = []
    children: list[WorkflowGraph] = []

    for identity, resolved_step in zip(identities, document.steps, strict=True):
        # Lowered before its step, so the child's diagnostics travel with it.
        child = (lower(resolved_step.process.child, here.child(identity))
                 if resolved_step.process.child is not None else None)
        nodes.append(_resolved_step_node(identity, resolved_step, defined, diagnostics, document.directory))
        if child is not None:
            if child.graph is not None:
                children.append(child.graph)
            for diagnostic in child.diagnostics:
                diagnostics._append(diagnostic)  # pylint: disable=protected-access

    passthrough = dict(document.source.passthrough)
    workflow_inputs = _workflow_ports(document, 'inputs', diagnostics)
    relays = {given.name: tuple(binding.sink for node in nodes for binding in node.bindings
                                if given.name in _input_names(binding)) for given in workflow_inputs}
    workflow_outputs = _workflow_ports(document, 'outputs', diagnostics)
    output_mapping = []
    for port in workflow_outputs:
        source = _output_port(nodes, port.output_source, diagnostics, document.source.span, str(port.name))
        if source is not None:
            output_mapping.append((port.name, source))
    namespaces_raw = passthrough.get('$namespaces')
    namespaces = tuple((str(key), value) for key, value in namespaces_raw.items()) \
        if isinstance(namespaces_raw, dict) else ()
    schemas_raw = passthrough.get('$schemas', ())
    schemas = tuple(schemas_raw) if isinstance(schemas_raw, list) else ()
    # Only the mapping form of `requirements:` is modeled; any other shape (a list
    # holding an `$import` or `$include`, or None from a bare key) is carried
    # opaquely and Emit writes it back unchanged.
    requirements_raw = passthrough.get('requirements', {})
    requirements = (tuple(requirements_raw.items())
                    if isinstance(requirements_raw, dict) else ())
    reserved = {'inputs', 'outputs', '$namespaces', '$schemas', 'cwlVersion', 'class'}
    if isinstance(requirements_raw, dict):
        reserved.add('requirements')
    opaque = tuple((key, value) for key, value in document.source.passthrough
                   if key not in reserved)
    if diagnostics.has_errors:
        # A graph is built only from a document the phase accepted: an edge
        # definition registered for an output the process never declared
        # (reported above as wic028) would otherwise trip the graph's own
        # invariants with a ValueError instead of this report.
        return PhaseResult(None, diagnostics)
    sidecar = document.source.sidecar
    inlineable = bool(dict(sidecar.entries).get('inlineable', True)) if sidecar is not None else True
    graph = WorkflowGraph(
        namespace=here,
        steps=tuple(nodes),
        explicit_edge_defs=tuple(defined.items()),
        input_mapping=tuple((name, sinks) for name, sinks in relays.items() if sinks),
        output_mapping=tuple(output_mapping),
        passthrough=opaque,
        span=document.source.span,
        name=document.name,
        lang_version=document.lang_version,
        workflow_inputs=workflow_inputs,
        workflow_outputs=workflow_outputs,
        requirements=requirements,
        namespaces=namespaces,
        schemas=schemas,
        children=tuple(children),
        inlineable=inlineable,
    )
    return PhaseResult(graph, diagnostics)


#: Bound on a `python_script` step to build its tool, never passed to it.
_GENERATION_PARAMETERS: Final = ('script', 'dockerPull')


def _record_only_inputs(resolved: ResolvedStep, declared: Mapping[AuthoredName, object]) -> tuple[str, ...]:
    """The inputs a step binds by record that its process does not declare.

    A record may bind such an input, which is how `when:` and `valueFrom` read
    a value; a subworkflow call's inputs are exactly the ones the subworkflow
    declares, so a call has none.
    """
    if resolved.process.child is not None:
        return ()
    return tuple(name for name, value in resolved.source.inputs
                 if isinstance(value, CwlRecord) and AuthoredName(name) not in declared)


# pylint: disable-next=too-many-locals
def _resolved_step_node(identity: StepId, resolved: ResolvedStep, defined: dict[str, PortId],
                        diagnostics: Diagnostics, directory: Path | None) -> StepNode:
    source = resolved.source
    declared_inputs = {AuthoredName(port.name): port.declaration for port in resolved.process.inputs}
    declared_outputs = {AuthoredName(port.name): port.declaration for port in resolved.process.outputs}
    # A generated process consumes `script`/`dockerPull` to build its tool;
    # neither survives into the tool's declared interface.
    consumed = _GENERATION_PARAMETERS if resolved.process.generated else ()
    extra = _record_only_inputs(resolved, declared_inputs)
    for name, _ in source.inputs:
        if name not in declared_inputs and name not in consumed and name not in extra:
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
                   for name, declaration in declared_inputs.items()) + tuple(
        Port(PortId(identity, Direction.INPUT, AuthoredName(name)), PortType('Any'), None, source.span)
        for name in extra)
    outputs = tuple(Port(PortId(identity, Direction.OUTPUT, name), declaration.type,
                         declaration, source.span)
                    for name, declaration in declared_outputs.items())
    by_input = {port.id.port: port for port in inputs}
    bindings = tuple(Binding(port.id, _with_defaults_resolved(value, directory),
                             _resolve(value, port, defined, diagnostics))
                     for name, value in source.inputs
                     if (port := by_input.get(AuthoredName(name))) is not None)
    scatter_ports = _scatter_ports(source, identity, inputs, diagnostics,
                                   calls_workflow=resolved.process.child is not None)
    return StepNode(identity, resolved.process.key, inputs, outputs, bindings, source.interpreted,
                    source.passthrough, source.span, scatter_ports,
                    _inference_rules(resolved.sidecar))


def _scatter_ports(source: Step, identity: StepId, inputs: tuple[Port, ...],
                   diagnostics: Diagnostics, *, calls_workflow: bool) -> tuple[PortName, ...]:
    """The ports an authored `scatter:` names, in the order written.

    A name must be an input of the step itself; on a subworkflow call those
    are exactly the inputs the subworkflow declares. Any other name is
    `wic032`, as is an entry that is not a name at all and a `scatter:` that is
    neither a name nor a list of them. A name the compiler generates for a port
    inside the subworkflow never resolves, so a caller cannot depend on how its
    callee is laid out.
    """
    scatter = dict(source.interpreted).get('scatter')
    locator = Locator(step=source.id, index=identity.index)
    if scatter is not None and not isinstance(scatter, (str, list)):
        diagnostics.error(SophiosErrorCode.UNKNOWN_SCATTER_PORT,
                          f"step '{source.id}' has `scatter: {scatter!r}`, which names no input. "
                          "`scatter:` is an input name or a list of them.",
                          source.span, locator)
    written: list[object] = [scatter] if isinstance(scatter, str) else (
        list(scatter) if isinstance(scatter, list) else [])
    ports: dict[str, PortName] = {str(port.id.port): port.id.port for port in inputs}
    found: list[PortName] = []
    for text in written:
        if not isinstance(text, str):
            diagnostics.error(SophiosErrorCode.UNKNOWN_SCATTER_PORT,
                              f"step '{source.id}' has the scatter entry {text!r}, which is not "
                              "a name. Each `scatter:` entry names one of the step's inputs.",
                              source.span, locator)
            continue
        if text in ports:
            found.append(ports[text])
            continue
        diagnostics.error(SophiosErrorCode.UNKNOWN_SCATTER_PORT,
                          _unknown_scatter(source.id, text, list(ports), calls_workflow),
                          source.span, locator)
    return tuple(found)


def _unknown_scatter(step: str, text: str, ports: list[str], calls_workflow: bool) -> str:
    """Name the step and the bad name, then the closest valid one, then all of them."""
    message = f"step '{step}' scatters over '{text}', which is not one of its inputs."
    close = difflib.get_close_matches(text, ports, n=1)
    if close:
        message += f" Did you mean '{close[0]}'?"
    if ports:
        shown = ', '.join(f"'{name}'" for name in ports[:_SHOWN_NAMES])
        more = f' and {len(ports) - _SHOWN_NAMES} more' if len(ports) > _SHOWN_NAMES else ''
        message += f' Its inputs: {shown}{more}.'
    if calls_workflow:
        message += (" A subworkflow call's inputs are the ones the subworkflow declares in "
                    "`inputs:`; to scatter over one of its steps' inputs, declare it there.")
    return message


#: How many valid names an unknown-scatter message lists before truncating.
_SHOWN_NAMES: Final = 8


def _resolved_default(default: OpaqueCwl, directory: Path | None) -> OpaqueCwl:
    """A `default:` with each relative File or Directory location in it read against `directory`, the
    document's own, and written absolute: the document is written elsewhere. As written when no file
    holds the document."""
    return default if directory is None else absolute_paths({'default': default}, directory)['default']


def _with_defaults_resolved(value: InputValue, directory: Path | None) -> InputValue:
    """A step input, its `!cwl {default: ...}` resolved as `_resolved_default` resolves one."""
    if not isinstance(value, CwlRecord):
        return value
    return replace(value, fields=tuple((key, _resolved_default(field, directory) if key == 'default' else field)
                                       for key, field in value.fields))


def _workflow_ports(document: ResolvedDocument, side: str, diagnostics: Diagnostics) -> tuple[WorkflowPort, ...]:
    """The ports the document declares under `side`, its `inputs:` or its `outputs:`."""
    raw: object = dict(document.source.passthrough).get(side)
    if not isinstance(raw, dict):
        return ()
    output = side == 'outputs'
    spans = {name: span for section, name, span in document.source.port_spans if section == side}
    ports: list[WorkflowPort] = []
    for name, declaration_raw in raw.items():
        if isinstance(declaration_raw, dict) and 'default' in declaration_raw:
            declaration_raw = {**declaration_raw,
                               'default': _resolved_default(declaration_raw['default'], document.directory)}
        if output and isinstance(declaration_raw, dict):
            rejected = sorted(key for key in ('linkMerge', 'pickValue') if key in declaration_raw)
            if isinstance(declaration_raw.get('outputSource'), list):
                rejected.append('a list outputSource')
            if rejected:
                diagnostics.error(
                    SophiosErrorCode.STEP_INPUT_RECORD,
                    f"workflow output {str(name)!r} uses {', '.join(rejected)}, which Sophios does not "
                    'read; an output names one step/port',
                    document.source.span)
        # Asserted, not reduced via `boundary_declaration`: it is already a
        # boundary declaration by where it's written, and reducing it would
        # discard fields the author wrote at the boundary on purpose.
        declaration = BoundaryDeclaration(port_declaration(declaration_raw, output=output))
        has_source = output and isinstance(declaration_raw, dict) \
            and 'outputSource' in declaration_raw
        source = declaration_raw.get('outputSource') if has_source else None
        ports.append(WorkflowPort(AuthoredName(str(name)), declaration, source, has_source,
                                  span=spans.get(str(name))))
    return tuple(ports)


def output_step(step_ids: Sequence[str], step_text: str) -> tuple[int, str | None] | None:
    """The step `step_text`, an `outputSource` up to its last `/`, names in a
    workflow whose steps are `step_ids`: its 1-based position, and the name a
    positional `(index, name)` key wrote, None when it named the step by id.

    A positional key names its position whatever step is there, and the caller
    checks the name; an id names its first occurrence. None when `step_text`
    names no step.
    """
    key = Grammar.WIC_STEP_KEY.match(step_text)
    if key is not None:
        return int(key.group(1)), key.group(2)
    if step_text in step_ids:
        return step_ids.index(step_text) + 1, None
    return None


def _output_port(nodes: list[StepNode], raw: object, diagnostics: Diagnostics,
                 span: SourceSpan | None, output_name: str) -> PortId | None:
    """The step output an authored `outputSource: <step>/<port>` names, if any.

    `<step>` is the authored id (its first occurrence), or `(index, name)`: that
    occurrence, which must carry that name and have that output, else a diagnostic.
    """
    if not isinstance(raw, str) or '/' not in raw:
        return None
    step_text, port_name = raw.rsplit('/', 1)
    found = output_step([node.id.name for node in nodes], step_text)
    if found is None:
        return None
    index, name = found
    if name is not None and (not 1 <= index <= len(nodes) or nodes[index - 1].id.name != name):
        actual = nodes[index - 1].id.name if 1 <= index <= len(nodes) else 'no step'
        diagnostics.error(
            SophiosErrorCode.POSITIONAL_OUTPUT_SOURCE,
            f"output {output_name!r} names step {index} as {name!r}, but step {index} is {actual}",
            span, Locator(step=name, index=index, port=port_name))
        return None
    outputs = nodes[index - 1].outputs
    port = next((port.id for port in outputs if port.id.port == AuthoredName(port_name)), None)
    if port is None and name is not None:
        declared = ', '.join(str(port.id.port) for port in outputs) or 'none'
        diagnostics.error(
            SophiosErrorCode.UNDECLARED_PORT,
            f"output {output_name!r} names '{port_name}' of step {index} ({name!r}), which has "
            f'no such output; its outputs are: {declared}',
            span, Locator(step=name, index=index, port=port_name))
    return port


def _input_names(binding: Binding) -> tuple[str, ...]:
    """The workflow inputs a binding names, without traversing opaque payloads."""
    match binding.value:
        case UnresolvedName(name=name):
            return (name,)
        case CwlRecord(sources=sources):
            return tuple(source.name for source in sources if isinstance(source, UnresolvedName))
        case _:
            return ()


def _inference_rules(sidecar: WicSidecar | None) -> tuple[tuple[str, str], ...]:
    """Normalize the local output-selection policy carried by a step sidecar."""
    raw = dict(sidecar.entries).get('inference') if sidecar is not None else None
    if not isinstance(raw, dict):
        return ()
    return tuple((str(name), str(rule)) for name, rule in raw.items())


def _step_identities(document: Document, here: Namespace,
                     diagnostics: Diagnostics) -> tuple[StepId, ...] | None:
    """One occurrence identity per step, or None when a step lacks an id.

    A repeated `id` is not an error: sequence-form `steps:` invokes one tool twice.
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
    """Report every position where the document left a name empty, all under
    one error code, so lowering stays total rather than raising `ValueError`.
    """
    before = len(diagnostics)
    for index, step in enumerate(document.steps, start=1):
        for name, value in step.inputs:
            if not name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"step '{step.id}' binds an input with no name", step.span,
                                  Locator(step=step.id, index=index))
            references = value.sources if isinstance(value, CwlRecord) else (value,)
            for reference in references:
                if isinstance(reference, EdgeRef) and not reference.name:
                    diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                      f"'!*' on '{step.id}.{name}' names no edge", reference.span,
                                      Locator(step=step.id, index=index, port=name))
        for binding in step.outputs:
            if not binding.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"step '{step.id}' declares an out: entry with no name",
                                  binding.span, Locator(step=step.id, index=index))
            if binding.edge_def is not None and not binding.edge_def.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME,
                                  f"'!&' on '{step.id}.{binding.name}' defines no edge",
                                  binding.edge_def.span,
                                  Locator(step=step.id, index=index, port=binding.name))
    return len(diagnostics) == before


def _check_written_names(document: ResolvedDocument, root: bool, diagnostics: Diagnostics) -> None:
    """Report each step id, workflow input and output, and the root's own name that
    `sophios.lang.names` does not let a document write."""
    source = document.source
    for index, step in enumerate(source.steps, start=1):
        if (problem := check_written_name(step.id)) is not None:
            diagnostics.error(SophiosErrorCode.INVALID_NAME, f'step id {step.id!r} {problem}', step.span,
                              Locator(step=step.id, index=index))
    passthrough = dict(source.passthrough)
    for side, what in (('inputs', 'input'), ('outputs', 'output')):
        declared = passthrough.get(side)
        for name in declared if isinstance(declared, dict) else ():
            if (problem := check_written_name(str(name))) is not None:
                diagnostics.error(SophiosErrorCode.INVALID_NAME, f'workflow {what} {str(name)!r} {problem}',
                                  source.span)
    if root and (problem := check_written_name(document.name)) is not None:
        diagnostics.error(SophiosErrorCode.INVALID_NAME, f'workflow name {document.name!r} {problem}',
                          source.span)


def _edge_definitions(identities: tuple[StepId, ...], steps: Sequence[Step],
                      diagnostics: Diagnostics) -> dict[str, PortId]:
    """Which port defines each explicit edge name, reporting any defined twice."""
    defined: dict[str, PortId] = {}
    for step_id, step in zip(identities, steps, strict=True):
        for binding in step.outputs:
            if binding.edge_def is None:
                continue
            name = binding.edge_def.name
            if name in defined:
                diagnostics.error(
                    SophiosErrorCode.DUPLICATE_EDGE_DEF,
                    f"'&{name}' is defined more than once. An edge name identifies one producer.",
                    binding.edge_def.span,
                    Locator(step=step.id, index=step_id.index, port=binding.name))
                continue
            defined[name] = PortId(step_id, Direction.OUTPUT, AuthoredName(binding.name))
    return defined


def _resolve(value: InputValue, port: Port, defined: dict[str, PortId], diagnostics: Diagnostics) -> Resolution:
    """Where one bound input gets its value from, if it needs a producer."""
    if isinstance(value, CwlRecord):
        items = tuple(_resolve_reference(source, port, defined, diagnostics)
                      if isinstance(source, EdgeRef) else None for source in value.sources)
        if len(items) > 1:
            for item in items:
                if isinstance(item, DeferredObligation):
                    # One step input has one relay across a workflow boundary,
                    # so it cannot carry several sources from outside.
                    diagnostics.error(
                        SophiosErrorCode.UNDEFINED_EDGE,
                        f"'!* {item.name}' is defined in no step of this document, and a record "
                        'with several sources takes each from this document or its inputs. '
                        'Declare it as an input of this workflow and name that input instead.',
                        item.span,
                        Locator(step=port.id.step.name, index=port.id.step.index, port=str(port.id.port)))
        return items
    if not isinstance(value, EdgeRef):
        return None
    return _resolve_reference(value, port, defined, diagnostics)


def _resolve_reference(value: EdgeRef, port: Port, defined: dict[str, PortId],
                       diagnostics: Diagnostics) -> Edge | DeferredObligation | None:
    """An edge to the definition an earlier step made, or an obligation on one outside this document."""
    source = defined.get(value.name)
    if source is None:
        return DeferredObligation(port.id, value.name, span=value.span)
    if source.step.index < port.id.step.index:
        return Edge(source, port.id, span=value.span)
    diagnostics.error(
        SophiosErrorCode.UNDEFINED_EDGE,
        f"'!* {value.name}' is referenced before '!& {value.name}' defines it.",
        value.span,
        Locator(step=port.id.step.name, index=port.id.step.index, port=str(port.id.port)))
    return None
