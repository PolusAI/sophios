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
from .types import (DerivedName, EmissionDocument, EmittedValue, Expression, PortName,
                    Source, StepEmission, StepNode, StepOutputRef, WorkflowGraph, WorkflowPort)

EDAM_NAMESPACE = ('edam', 'https://edamontology.org/')
EDAM_SCHEMA = 'https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'


def _step_spelling(step: StepNode, names: Names, relative_run_path: bool,
                   partial_failure: bool) -> StepNode:
    """`step` with its `run:` path written and its field order settled.

    Under partial failure a step runs only when every required input arrived,
    so its `when` is rewritten from the ports it declares.
    """
    emission = step.emission
    assert emission is not None, 'surface refuses a graph with an unemitted step'
    if partial_failure:
        needed = [names.port(port.id.port) for port in step.inputs if required(port.declaration)]
        if needed:
            emission = replace(emission, when='$(' + ' && '.join(
                f'inputs["{name}"] != null' for name in needed) + ')')
    target = emission.run.target
    if isinstance(target, str):
        # From the resolved identity: `target` is this function's own output,
        # so a leaf read back out of it would compound.
        leaf = f'{emission.run.process_id.name}.cwl'
        if relative_run_path:
            target = f'{names.step(step.id)}/{leaf}'
        elif emission.run.child is not None:
            target = f'{names.qualified(step.id)}{NAMESPACE_SEPARATOR}{leaf}'
        else:
            target = f'../{leaf}'
    order = list(emission.field_order)
    if 'in' not in order:
        run_index = order.index('run') if 'run' in order else len(order)
        order.insert(run_index + 1, 'in')
    if emission.when is not None and 'when' not in order:
        order.append('when')
    return replace(step, emission=replace(
        emission, run=replace(emission.run, target=target), field_order=tuple(order)))


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
            step with no emission. Emit asks for the type this returns, so this
            is the only place the question is asked.
        SophiosError: `wic031` if two distinct boundary names are spelled
            alike, which the emitted document could not tell apart.
    """
    if not graph.cwl_version:
        raise ValueError('an emission graph must declare its CWL version')
    if not graph.lang_version:
        raise ValueError('an emission graph must declare its Sophios language version')
    if any(step.emission is None for step in graph.steps):
        raise ValueError('every step in an emission graph needs an emission descriptor')

    for ports in (graph.workflow_inputs, graph.workflow_outputs):
        _refuse_colliding_names(tuple(port.name for port in ports), names)
    steps = [_step_spelling(step, names, relative_run_path, partial_failure)
             for step in graph.steps]

    requirements = dict(graph.requirements)
    if graph.children:
        requirements['SubworkflowFeatureRequirement'] = {}
    if any(step.emission is not None and step.emission.scatter for step in steps):
        requirements['ScatterFeatureRequirement'] = {}
    if any(step.emission is not None and step.emission.when is not None for step in steps):
        requirements['InlineJavascriptRequirement'] = {}
    requirements = dict(sorted(requirements.items()))

    namespaces = {name: value for name, value in graph.namespaces
                  if name not in {EDAM_NAMESPACE[0], ANNOTATION_NAMESPACE}}
    namespaces[EDAM_NAMESPACE[0]] = EDAM_NAMESPACE[1]
    namespaces[ANNOTATION_NAMESPACE] = ANNOTATION_NAMESPACE_URI
    schemas = list(graph.schemas)
    if EDAM_SCHEMA not in schemas:
        schemas.append(EDAM_SCHEMA)
    order = list(graph.field_order)
    if requirements and 'requirements' not in order:
        order.append('requirements')
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
        namespaces=tuple(namespaces.items()), schemas=tuple(schemas),
        field_order=tuple(order)))


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

    The field-order tuples are part of the graph's emission surface, not an
    implicit dependency on dictionary insertion order. What makes a graph
    renderable is stated by the argument type, so nothing is checked here.
    """
    known: dict[str, Any] = {
        'steps': [_emit_step(step, names) for step in graph.steps],
        'cwlVersion': graph.cwl_version,
        'class': 'Workflow',
        '$namespaces': {name: deepcopy(value) for name, value in graph.namespaces},
        '$schemas': deepcopy(list(graph.schemas)),
        'inputs': {names.port(port.name): _emit_port(port, names)
                   for port in graph.workflow_inputs},
        versions.ANNOTATION_KEY: graph.lang_version,
        'outputs': {names.port(port.name): _emit_port(port, names)
                    for port in graph.workflow_outputs},
        'requirements': {name: deepcopy(value) for name, value in graph.requirements},
    }
    known.update({name: deepcopy(value) for name, value in graph.passthrough})
    return {name: known[name] for name in graph.field_order if name in known}


def emit_job_inputs(graph: EmissionDocument, names: Names) -> Cwl:
    """Project the concrete job input document carried by ``graph``."""
    return {names.port(binding.name): deepcopy(binding.value) for binding in graph.job_bindings}


def _emit_step(node: StepNode, names: Names) -> dict[str, Any]:
    """Render one structured step descriptor in its declared canonical order."""
    step = node.emission
    assert step is not None, 'an EmissionDocument has no unemitted step'
    known: dict[str, Any] = {
        'id': names.step(node.id),
        'in': {names.port(name): _emit_binding(value, names) for name, value in step.inputs},
        'run': deepcopy(step.run.target),
        'out': [names.port(name) for name in step.outputs],
        'scatter': _emit_scatter(step, names),
        'scatterMethod': deepcopy(step.scatter_method),
        'when': deepcopy(step.when),
    }
    known.update({name: deepcopy(value) for name, value in step.passthrough})
    return {name: known[name] for name in step.field_order if name in known}


def _emit_scatter(step: StepEmission, names: Names) -> Any:
    """`scatter:` spelled from its resolved ports, in the authored shape."""
    match step.scatter:
        case list():
            return [names.port(port) for port in step.scatter_ports]
        case str():
            return names.port(step.scatter_ports[0])
    return deepcopy(step.scatter)


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
    return {name: known[name] for name in declaration.field_order if name in known}
