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

from ..lang import CWL_VERSION, CwlRecord
from ..lang.nodes import OpaqueCwl
from ..wic_types import Cwl
from .declarations import written_order
from .names import Names
from .policy import RunPolicy, effective_when
from .stepin import step_inputs
from .types import (EmissionDocument, EmittedValue, Expression, PortName, Record,
                    Source, StepNode, StepOutputRef, WorkflowGraph, WorkflowPort)

EDAM_NAMESPACE = ('edam', 'https://edamontology.org/')


def _step_spelling(step: StepNode, names: Names, policy: RunPolicy) -> StepNode:
    """`step` with the `when` `policy` gives it."""
    interpreted = dict(step.interpreted)
    if (when := effective_when(step, names, policy)) is not None:
        interpreted['when'] = when
    return replace(step, interpreted=tuple(interpreted.items()))


def surface(graph: WorkflowGraph, names: Names, *, policy: RunPolicy = RunPolicy()) -> EmissionDocument:
    """Spell `graph`'s facts as the document Emit renders, for this graph alone.

    Requirements implied by the steps, the EDAM namespace (only for a
    document that spells a format `edam:`), a step's `run:` path and the
    order fields appear in: none is a fact about the workflow, and none is
    read by any phase. They are computed here, once per emitted document.

    Args:
        graph (WorkflowGraph): One completed document, without its children.
        names (Names): The spelling of every step in the tree `graph` is in.
        policy (RunPolicy): How the workflow runs, which each step's `when`
            is written for.

    Returns:
        EmissionDocument: The same graph, carrying the spelling Emit renders.
    """
    steps = [_step_spelling(step, names, policy) for step in graph.steps]

    requirements = dict(graph.requirements)
    for requirement in _implied_requirements(graph, steps):
        # An authored body is kept; an authored `Class:` with no body is `{}`, as CWL needs.
        if requirements.get(requirement) is None:
            requirements[requirement] = {}
    requirements = dict(sorted(requirements.items()))

    namespaces = dict(graph.namespaces)
    if _uses_edam(graph):
        namespaces[EDAM_NAMESPACE[0]] = EDAM_NAMESPACE[1]
    boundary_order = written_order(graph)
    workflow_inputs = tuple(sorted(graph.workflow_inputs,
                                   key=lambda port: boundary_order(port.name)))
    job_bindings = tuple(sorted(graph.job_bindings,
                                key=lambda binding: boundary_order(binding.name)))
    return EmissionDocument(replace(
        graph, steps=tuple(steps), requirements=tuple(requirements.items()),
        workflow_inputs=workflow_inputs, job_bindings=job_bindings,
        namespaces=tuple(namespaces.items())))


def _uses_edam(graph: WorkflowGraph) -> bool:
    """Whether a `format:` of `graph`'s inputs or outputs, or of a File in its job or a `default:`, is an `edam:` CURIE.

    The job and the defaults count because a runner reads their CURIEs against the workflow's `$namespaces`.
    """
    ports = (*graph.workflow_inputs, *graph.workflow_outputs)
    formats = [port.declaration.format for port in ports if port.declaration.has_format]
    formats += [found for port in ports if port.declaration.has_default
                for found in _job_formats(port.declaration.default)]
    formats += [found for step in graph.steps for binding in step.bindings if isinstance(binding.value, CwlRecord)
                for key, value in binding.value.fields if key == 'default' for found in _job_formats(value)]
    formats += [found for binding in graph.job_bindings for found in _job_formats(binding.value)]
    return any(isinstance(text, str) and text.startswith(f'{EDAM_NAMESPACE[0]}:')
               for value in formats for text in (value if isinstance(value, list) else [value]))


def _job_formats(value: OpaqueCwl) -> list[OpaqueCwl]:
    """Every `format` a job value holds, at any depth: a File's, a File's in a list or a record."""
    match value:
        case dict():
            own = [value['format']] if 'format' in value else []
            return own + [found for item in value.values() for found in _job_formats(item)]
        case list():
            return [found for item in value for found in _job_formats(item)]
    return []


def _implied_requirements(graph: WorkflowGraph, steps: list[StepNode]) -> tuple[str, ...]:
    """The requirement classes `graph`'s calls, scatters, `when`s and step-input records need."""
    records = [binding.value for step in steps for binding in step.bindings
               if isinstance(binding.value, CwlRecord)]
    value_from = [str(text) for record in records for key, text in record.fields if key == 'valueFrom']
    return tuple(requirement for requirement, needed in (
        ('SubworkflowFeatureRequirement', bool(graph.children)),
        ('ScatterFeatureRequirement', any(step.scatter_ports for step in steps)),
        ('InlineJavascriptRequirement',
         any(dict(step.interpreted).get('when') is not None for step in steps)
         or any('$(' in text or '${' in text for text in value_from)),
        ('MultipleInputFeatureRequirement',
         any(len(record.sources) > 1 or 'linkMerge' in dict(record.fields) for record in records)),
        ('StepInputExpressionRequirement', bool(value_from)),
    ) if needed)


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
        'cwlVersion': CWL_VERSION,
        'class': 'Workflow',
    }
    if graph.namespaces:
        known['$namespaces'] = {name: deepcopy(value) for name, value in graph.namespaces}
    if graph.schemas:
        # An ontology is a dependency of the document: only its author adds one.
        known['$schemas'] = deepcopy(list(graph.schemas))
    known['inputs'] = {names.port(port.name): _emit_port(port, names) for port in graph.workflow_inputs}
    known['outputs'] = {names.port(port.name): _emit_port(port, names) for port in graph.workflow_outputs}
    if graph.requirements:
        known['requirements'] = {name: deepcopy(value) for name, value in graph.requirements}
    known.update({name: deepcopy(value) for name, value in graph.passthrough})
    return known


def emit_job_inputs(graph: EmissionDocument, names: Names) -> Cwl:
    """Project the concrete job input document carried by ``graph``."""
    return {names.port(binding.name): deepcopy(binding.value) for binding in graph.job_bindings}


def _emit_step(node: StepNode, ins: tuple[tuple[PortName, EmittedValue], ...],
               names: Names) -> dict[str, Any]:
    """Render one structured step descriptor in canonical order. A call's ports are its child's boundary,
    in the child's order, so `out:` is the step's outputs whatever it runs."""
    interpreted = dict(node.interpreted)
    known: dict[str, Any] = {
        'id': names.step(node.id),
        'in': {names.port(name): _emit_binding(value, names) for name, value in ins},
        # The file the step's process is written to, beside every other document of the tree.
        'run': f'{names.file(node.id)}.cwl',
        'out': [names.port(port.id.port) for port in node.outputs],
    }
    if interpreted.get('scatter') is not None:
        # Spelled from the ports Lower resolved it to, always as a list.
        known['scatter'] = [names.port(port) for port in node.scatter_ports]
    for key in ('scatterMethod', 'when'):
        if interpreted.get(key) is not None:
            known[key] = deepcopy(interpreted[key])
    known.update({name: deepcopy(value) for name, value in node.passthrough})
    return known


def _emit_binding(value: EmittedValue, names: Names) -> str | dict[str, Any]:
    """One `in:` entry, in the spelling its value asks for.

    Returns `str | dict[str, Any]` rather than `Any` so mypy checks this
    `match` covers the union instead of a missed arm silently falling through.
    """
    match value:
        case Source(ref=ref, shorthand=True):
            return names.source(ref)
        case Source(ref=ref):
            return {'source': names.source(ref)}
        case Expression(text=text):
            return text
        case Record(sources=sources, fields=fields):
            known: dict[str, Any] = {}
            if sources:
                spelled = [names.source(source) for source in sources]
                # One source stays a list beside `linkMerge`, which merges a list.
                known['source'] = spelled if len(spelled) > 1 or 'linkMerge' in dict(fields) else spelled[0]
            known.update({key: deepcopy(field) for key, field in fields})
            return known


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
