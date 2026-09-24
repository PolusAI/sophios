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
"""
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..lang import versions
from ..lang.versions import ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from ..wic_types import Cwl
from .types import (EmittedValue, Expression, namespaced, NAMESPACE_SEPARATOR, Source,
                    StepEmission, StepNode, WorkflowGraph, WorkflowPort)

EDAM_NAMESPACE = ('edam', 'https://edamontology.org/')
EDAM_SCHEMA = 'https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'


def _step_spelling(step: StepNode, namespace: tuple[str, ...],
                   relative_run_path: bool) -> StepNode:
    """`step` with its `run:` path written and its field order settled."""
    emission = step.emission
    if emission is None:
        return step
    target = emission.run.target
    if isinstance(target, str):
        # From the resolved identity: `target` is this function's own output,
        # so a leaf read back out of it would compound.
        leaf = f'{emission.run.process_id.name}.cwl'
        if relative_run_path:
            target = f'{emission.id}/{leaf}'
        elif emission.run.child is not None:
            target = namespaced(*namespace, emission.id, leaf)
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


def surface(graph: WorkflowGraph, *, relative_run_path: bool = True) -> WorkflowGraph:
    """Spell `graph`'s facts as the document Emit renders, for this graph alone.

    Requirements implied by the steps, the EDAM namespace and schema, a step's
    `run:` path and the order fields appear in: none is a fact about the
    workflow, and none is read by any phase. They are computed here, once per
    emitted document, rather than in `complete`, which runs whenever a phase
    needs the facts current.

    Args:
        graph (WorkflowGraph): One completed document, without its children.
        relative_run_path (bool): Whether a `run:` target is written relative
            to the step directory or namespaced beside the root.

    Returns:
        WorkflowGraph: The same graph, carrying the spelling Emit renders.
    """
    steps = [_step_spelling(step, graph.namespace.parts, relative_run_path)
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
    generated_prefixes = tuple(step.emission.id + NAMESPACE_SEPARATOR for step in steps
                               if step.emission is not None)

    def boundary_order(name: str) -> tuple[int, int]:
        for index, prefix in enumerate(generated_prefixes):
            if name.startswith(prefix):
                return (1, index)
        return (0, 0)

    workflow_inputs = tuple(sorted(graph.workflow_inputs,
                                   key=lambda port: boundary_order(port.name)))
    job_bindings = tuple(sorted(graph.job_bindings,
                                key=lambda binding: boundary_order(binding.name)))
    return replace(graph, steps=tuple(steps), requirements=tuple(requirements.items()),
                   workflow_inputs=workflow_inputs, job_bindings=job_bindings,
                   namespaces=tuple(namespaces.items()), schemas=tuple(schemas),
                   field_order=tuple(order))


def emit(graph: WorkflowGraph) -> Cwl:
    """Render ``graph`` as canonical CWL v1.2.

    The field-order tuples are part of the graph's emission surface, not an
    implicit dependency on dictionary insertion order.  Missing semantic data
    is an invalid graph for emission and fails close to its producer.
    """
    if not graph.cwl_version:
        raise ValueError('an emission graph must declare its CWL version')
    if not graph.lang_version:
        raise ValueError('an emission graph must declare its Sophios language version')
    if any(step.emission is None for step in graph.steps):
        raise ValueError('every step in an emission graph needs an emission descriptor')

    known: dict[str, Any] = {
        'steps': [_emit_step(step.emission) for step in graph.steps if step.emission is not None],
        'cwlVersion': graph.cwl_version,
        'class': 'Workflow',
        '$namespaces': {name: deepcopy(value) for name, value in graph.namespaces},
        '$schemas': deepcopy(list(graph.schemas)),
        'inputs': {port.name: _emit_port(port) for port in graph.workflow_inputs},
        versions.ANNOTATION_KEY: graph.lang_version,
        'outputs': {port.name: _emit_port(port) for port in graph.workflow_outputs},
        'requirements': {name: deepcopy(value) for name, value in graph.requirements},
    }
    known.update({name: deepcopy(value) for name, value in graph.passthrough})
    return {name: known[name] for name in graph.field_order if name in known}


def emit_job_inputs(graph: WorkflowGraph) -> Cwl:
    """Project the concrete job input document carried by ``graph``."""
    return {binding.name: deepcopy(binding.value) for binding in graph.job_bindings}


def _emit_step(step: StepEmission) -> dict[str, Any]:
    """Render one structured step descriptor in its declared canonical order.

    A binding arrives as an `EmittedValue`, and rendering it is the one
    decision this module makes. The check that used to stand here -- refusing a
    `wic_alias` that reached emission -- is gone with the state it guarded: the
    union has no member that can carry a Sophios word, so a phase cannot build
    one and mypy says so at the producer rather than the runner saying so a CI
    lane later.
    """
    known: dict[str, Any] = {
        'id': step.id,
        'in': {name: _emit_binding(value) for name, value in step.inputs},
        'run': deepcopy(step.run.target),
        'out': deepcopy(list(step.outputs)),
        'scatter': deepcopy(step.scatter),
        'scatterMethod': deepcopy(step.scatter_method),
        'when': deepcopy(step.when),
    }
    known.update({name: deepcopy(value) for name, value in step.passthrough})
    return {name: known[name] for name in step.field_order if name in known}


def _emit_binding(value: EmittedValue) -> str | dict[str, str]:
    """One `in:` entry, in the spelling its value asks for.

    The return type is written out rather than left as `Any` so that mypy
    checks this `match` covers the union. That check is what the runtime guard
    this replaced used to do: under `Any` a third member would fall off every
    arm, return `None`, and emit `in: {x: null}` -- the same silent, runner-only
    failure, arriving the same way.
    """
    match value:
        case Source(name=name, shorthand=True):
            return name
        case Source(name=name):
            return {'source': name}
        case Expression(text=text):
            return text


def _emit_port(port: WorkflowPort) -> Any:
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
        known['outputSource'] = deepcopy(port.output_source)
    known.update({name: deepcopy(value) for name, value in declaration.passthrough})
    return {name: known[name] for name in declaration.field_order if name in known}
