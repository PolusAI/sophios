"""Pure projections from a finished workflow graph.

Emit is intentionally boring.  Every semantic decision belongs to Resolve,
Lower, Link, or Infer; this module only spells the facts already present on a
``WorkflowGraph`` as CWL v1.2, a job input document, or a visualization model.
It imports no compiler, registry, filesystem, configuration, or mutable
process state.

Opaque CWL is the exception to the IR's no-inspection rule at this boundary:
Emit may traverse it to make an owned copy, but never branches on its meaning.

`surface` is the one thing here that computes rather than copies, and it is
here because what it computes -- requirements, `$namespaces`, `$schemas`, a
`run:` path, field order -- is a property of the document, not of the workflow.
Nothing upstream reads it, so it runs once per document and never again.

Every step, port and source reaches Emit as a structural identity and leaves
as text through one `Names` table, so a definition and each reference to it
are spelled by the same entry.
"""
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..lang import versions
from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..lang.versions import ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from ..wic_types import Cwl
from .declarations import required
from .names import NAMESPACE_SEPARATOR, Names
from .stepin import step_inputs, step_out
from .types import (DerivedName, EmissionDocument, EmittedValue, Expression, PortName,
                    Source, StepNode, StepOutputRef, WorkflowGraph, WorkflowPort)

EDAM_NAMESPACE = ('edam', 'https://edamontology.org/')
EDAM_SCHEMA = 'https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'


def _step_spelling(step: StepNode, names: Names, relative_run_path: bool,
                   partial_failure: bool) -> StepNode:
    """`step` with its `run:` path written.

    Under partial failure a step runs only when every required input arrived,
    so its `when` is rewritten from the ports it declares.
    """
    run = step.run
    assert run is not None, 'surface refuses a graph with an unrun step'
    interpreted = dict(step.interpreted)
    if partial_failure:
        needed = [names.port(port.id.port) for port in step.inputs if required(port.declaration)]
        if needed:
            interpreted['when'] = '$(' + ' && '.join(
                f'inputs["{name}"] != null' for name in needed) + ')'
    target = run.target
    if isinstance(target, str):
        # From the resolved identity: `target` is this function's own output,
        # so a leaf read back out of it would compound.
        leaf = f'{run.process_id.name}.cwl'
        if relative_run_path:
            target = f'{names.step(step.id)}/{leaf}'
        elif run.child is not None:
            target = f'{names.qualified(step.id)}{NAMESPACE_SEPARATOR}{leaf}'
        else:
            target = f'../{leaf}'
    return replace(step, interpreted=tuple(interpreted.items()), run=replace(run, target=target))


def surface(graph: WorkflowGraph, names: Names, *,
            relative_run_path: bool = True, partial_failure: bool = False) -> EmissionDocument:
    """Spell `graph`'s facts as the document Emit renders, for this graph alone.

    Requirements implied by the steps, the EDAM namespace and schema, a step's
    `run:` path and the order fields appear in: none is a fact about the
    workflow, and none is read by any phase. They are computed here, once per
    emitted document, rather than in `complete`, which runs whenever a phase
    needs the facts current.

    Args:
        graph (WorkflowGraph): One completed document, without its children.
        names (Names): The spelling of every step in the tree `graph` is in.
        relative_run_path (bool): Whether a `run:` target is written relative
            to the step directory or namespaced beside the root.
        partial_failure (bool): Whether each step runs only when its required
            inputs arrived.

    Returns:
        EmissionDocument: The same graph, carrying the spelling Emit renders.

    Raises:
        ValueError: If the graph states no document -- a missing version, or a
            step with no run. Emit asks for the type this returns, so this
            is the only place the question is asked.
        SophiosError: `wic031` if two distinct boundary names are spelled
            alike, which the emitted document could not tell apart.
    """
    if not graph.cwl_version:
        raise ValueError('an emission graph must declare its CWL version')
    if not graph.lang_version:
        raise ValueError('an emission graph must declare its Sophios language version')
    if any(step.run is None for step in graph.steps):
        raise ValueError('every step in an emission graph needs a run')

    for ports in (graph.workflow_inputs, graph.workflow_outputs):
        _refuse_colliding_names(tuple(port.name for port in ports), names)
    steps = [_step_spelling(step, names, relative_run_path, partial_failure)
             for step in graph.steps]

    requirements = dict(graph.requirements)
    for requirement in _implied_requirements(graph, steps):
        # An authored body is kept; an authored `Class:` with no body is `{}`, as CWL needs.
        if requirements.get(requirement) is None:
            requirements[requirement] = {}
    requirements = dict(sorted(requirements.items()))

    namespaces = {name: value for name, value in graph.namespaces
                  if name not in {EDAM_NAMESPACE[0], ANNOTATION_NAMESPACE}}
    namespaces[EDAM_NAMESPACE[0]] = EDAM_NAMESPACE[1]
    namespaces[ANNOTATION_NAMESPACE] = ANNOTATION_NAMESPACE_URI
    schemas = list(graph.schemas)
    if EDAM_SCHEMA not in schemas:
        schemas.append(EDAM_SCHEMA)
    positions = {step.id: index for index, step in enumerate(steps)}

    def boundary_order(name: PortName) -> tuple[int, int]:
        """Names exposing one of this document's steps last, in step order.

        A name Link relays from a step nested further down exposes no step of
        this document, and sorts with the authored names.
        """
        if isinstance(name, DerivedName) and name.step in positions:
            return (1, positions[name.step])
        return (0, 0)

    workflow_inputs = tuple(sorted(graph.workflow_inputs,
                                   key=lambda port: boundary_order(port.name)))
    job_bindings = tuple(sorted(graph.job_bindings,
                                key=lambda binding: boundary_order(binding.name)))
    return EmissionDocument(replace(
        graph, steps=tuple(steps), requirements=tuple(requirements.items()),
        workflow_inputs=workflow_inputs, job_bindings=job_bindings,
        namespaces=tuple(namespaces.items()), schemas=tuple(schemas)))


def _implied_requirements(graph: WorkflowGraph, steps: list[StepNode]) -> tuple[str, ...]:
    """The requirement classes `graph`'s calls, scatters and `when`s need."""
    return tuple(requirement for requirement, needed in (
        ('SubworkflowFeatureRequirement', bool(graph.children)),
        ('ScatterFeatureRequirement', any(step.scatter_ports for step in steps)),
        ('InlineJavascriptRequirement',
         any(dict(step.interpreted).get('when') is not None for step in steps)),
    ) if needed)


def _refuse_colliding_names(declared: tuple[PortName, ...], names: Names) -> None:
    """Raise `wic031` when two distinct names in one namespace render alike."""
    spelled: dict[str, PortName] = {}
    for name in declared:
        text = names.port(name)
        if text in spelled and spelled[text] != name:
            raise SophiosError.error(
                SophiosErrorCode.DUPLICATE_DOCUMENT_NAME,
                f'{text!r} names two different ports in the emitted document')
        spelled[text] = name


def emit(graph: EmissionDocument, names: Names) -> Cwl:
    """Render ``graph`` as canonical CWL v1.2.

    Known keys are written in one fixed order (dict insertion order, below),
    then authored passthrough keys in the order they appear in
    ``graph.passthrough``. What makes a graph renderable is stated by the
    argument type, so nothing is checked here.
    """
    ins = step_inputs(graph)
    known: dict[str, Any] = {
        'steps': [_emit_step(step, ins[step.id], names) for step in graph.steps],
        'cwlVersion': graph.cwl_version,
        'class': 'Workflow',
        '$namespaces': {name: deepcopy(value) for name, value in graph.namespaces},
        '$schemas': deepcopy(list(graph.schemas)),
        'inputs': {names.port(port.name): _emit_port(port, names)
                   for port in graph.workflow_inputs},
        versions.ANNOTATION_KEY: graph.lang_version,
        'outputs': {names.port(port.name): _emit_port(port, names)
                    for port in graph.workflow_outputs},
    }
    if graph.requirements:
        known['requirements'] = {name: deepcopy(value) for name, value in graph.requirements}
    known.update({name: deepcopy(value) for name, value in graph.passthrough})
    return known


def emit_job_inputs(graph: EmissionDocument, names: Names) -> Cwl:
    """Project the concrete job input document carried by ``graph``."""
    return {names.port(binding.name): deepcopy(binding.value) for binding in graph.job_bindings}


def _emit_step(node: StepNode, ins: tuple[tuple[PortName, EmittedValue], ...],
               names: Names) -> dict[str, Any]:
    """Render one structured step descriptor in canonical order."""
    run = node.run
    assert run is not None, 'an EmissionDocument has no unrun step'
    interpreted = dict(node.interpreted)
    known: dict[str, Any] = {
        'id': names.step(node.id),
        'in': {names.port(name): _emit_binding(value, names) for name, value in ins},
        'run': deepcopy(run.target),
        'out': [names.port(name) for name in step_out(node)],
    }
    scatter = interpreted.get('scatter')
    if scatter is not None:
        known['scatter'] = _emit_scatter(scatter, node.scatter_ports, names)
    scatter_method = interpreted.get('scatterMethod')
    if scatter_method is not None:
        known['scatterMethod'] = deepcopy(scatter_method)
    when = interpreted.get('when')
    if when is not None:
        known['when'] = deepcopy(when)
    known.update({name: deepcopy(value) for name, value in node.passthrough})
    return known


def _emit_scatter(scatter: Any, ports: tuple[PortName, ...], names: Names) -> Any:
    """`scatter:` spelled from its resolved `ports`, in the authored shape."""
    match scatter:
        case list():
            return [names.port(port) for port in ports]
        case str():
            return names.port(ports[0])
    return deepcopy(scatter)


def _emit_binding(value: EmittedValue, names: Names) -> str | dict[str, str]:
    """One `in:` entry, in the spelling its value asks for.

    Returns `str | dict[str, str]` rather than `Any` so mypy checks this
    `match` covers the union instead of a missed arm silently falling through.
    """
    match value:
        case Source(ref=ref, shorthand=True):
            return names.source(ref)
        case Source(ref=ref):
            return {'source': names.source(ref)}
        case Expression(text=text):
            return text


def _emit_port(port: WorkflowPort, names: Names) -> Any:
    """Render a workflow port without assigning meaning to opaque fields."""
    declaration = port.declaration
    if declaration.shorthand:
        return deepcopy(declaration.type.declared)
    known: dict[str, Any] = {'type': deepcopy(declaration.type.declared)}
    if declaration.has_format:
        known['format'] = deepcopy(declaration.format)
    if declaration.has_default:
        known['default'] = deepcopy(declaration.default)
    if port.has_output_source:
        known['outputSource'] = (names.source(port.output_source)
                                 if isinstance(port.output_source, StepOutputRef)
                                 else deepcopy(port.output_source))
    known.update({name: deepcopy(value) for name, value in declaration.passthrough})
    return known
