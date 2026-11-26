"""The CWL of a resolved view: the one lowering of the CWL target.

The view holds what the author wrote and what the compiler decided; this module spells it as CWL v1.2, one document
per workflow and a job input document beside each. What only the document has -- the requirements its steps imply, the
EDAM namespace, a `when` written for the run policy, a step's `run:` file name and the order of keys -- is computed
here, once, and nothing else reads it. Opaque CWL is copied, never inspected.
"""
from copy import deepcopy

from .artifacts import CompilationArtifact, Inlining
from ..ir.names import NAMESPACE_SEPARATOR
from ..ir.namespaces import EDAM_NAMESPACE
from ..ir.policy import RunPolicy
from ..ir.resolved import (
    Entry, InputRef, OutputRef, OutputSource, Raw, ResolvedStep, ResolvedWorkflow, StepValue, Verbatim, spelling,
)
from ..lang import CWL_VERSION
from ..lang.nodes import Value
from ..lang.spans import SourceSpan
from ..lang.subset import written_text
from ..lang.tools import ToolNode
from ..lang.types import PortDeclaration
from ..lang.values import write_value
from ..wic_types import Cwl
from .lowering import process_of
from .policy import effective_when, tolerant_tool
from .spelling import written_type


def artifact_tree(view: ResolvedWorkflow) -> CompilationArtifact:
    """The CWL of `view`: one artifact per document, each tool step's process beside it, named by the root and the
    step that runs it (`<root>~<address>`)."""
    return _artifact(view, view.name, view.name, (), None)


def _artifact(workflow: ResolvedWorkflow, root: str, name: str, namespace: tuple[str, ...],
              span: SourceSpan | None) -> CompilationArtifact:
    """`workflow` as the document `name`, in the document tree of `root`, and what runs in its steps."""
    # Each step's `when`, written for the run policy.
    whens = [effective_when(_when(step), step.required_inputs, workflow.policy) for step in workflow.steps]
    return CompilationArtifact(
        namespace, name, f'{name}.cwl', _document(workflow, whens, root),
        write_value(workflow.values),
        tuple(_child(step, root, namespace, workflow.policy) for step in workflow.steps),
        Inlining(workflow.inlineable, span))


def _when(step: ResolvedStep) -> Value:
    """A step's `when` as the document writes it: the tree as the subset writes it, or the text the subset does not
    read, as written; None without one."""
    if step.condition is None:
        return None
    return written_text(step.condition.expr) if step.condition.expr is not None else dict(step.passthrough)['when']


def _child(step: ResolvedStep, root: str, namespace: tuple[str, ...], policy: RunPolicy) -> CompilationArtifact:
    """What `step` runs, as an artifact: a called workflow's document tree, or a tool's file."""
    match step.run:
        case ResolvedWorkflow() as called:
            return _artifact(called, root, _file(root, step), (*namespace, step.local), step.span)
        case ToolNode() as tool:
            return CompilationArtifact((step.local,), _file(root, step), tool.run_path,
                                       tolerant_tool(process_of(tool), policy), {})


def _file(root: str, step: ResolvedStep) -> str:
    """The stem of the file `step`'s process is written to, beside the root: `<root>~<address>`. No root name
    contains `~`, so the first one splits it off, and an address is unique in its root."""
    return f'{root}{NAMESPACE_SEPARATOR}{step.name}'


def _document(workflow: ResolvedWorkflow, whens: list[Value], root: str) -> Cwl:
    """The CWL `workflow` is written as: known keys in one fixed order, then the author's other keys in the order
    they were written."""
    document: dict[str, Value] = {
        'steps': [_step(step, when, root) for step, when in zip(workflow.steps, whens, strict=True)],
        'cwlVersion': CWL_VERSION,
        'class': 'Workflow',
    }
    namespaces = dict(workflow.namespaces)
    if _uses_edam(workflow):
        namespaces[EDAM_NAMESPACE[0]] = EDAM_NAMESPACE[1]
    if namespaces:
        document['$namespaces'] = deepcopy(namespaces)
    document.update((key, None) for key, _ in workflow.passthrough if key == '$schemas')  # its place, filled below
    document['inputs'] = {port.name: _port(port.declaration) for port in workflow.inputs}
    document['outputs'] = {port.name: _port(port.declaration, port.output_source) for port in workflow.outputs}
    if requirements := _requirements(workflow, whens):
        document['requirements'] = requirements
    # The `requirements:` mapping the author wrote is in the one written above.
    document.update({key: deepcopy(value) for key, value in workflow.passthrough
                     if key != 'requirements' or not isinstance(value, dict)})
    return document


def _step(step: ResolvedStep, when: Value, root: str) -> dict[str, Value]:
    """One step in canonical order. A call's ports are its workflow's boundary, in that workflow's order, so `out:`
    is the step's outputs whatever it runs."""
    known: dict[str, Value] = {
        'id': step.local,
        'in': {entry.name: _binding(entry.value) for entry in step.inputs},
        # The file the step's process is written to, beside every other document of the tree.
        'run': f'{_file(root, step)}.cwl',
        'out': list(step.outputs),
    }
    others = dict(step.passthrough)
    # A `scatterMethod` the scatter does not hold is the first of the step's other keys, and is written before `when`,
    # which the other keys hold too where the subset does not read it.
    method = step.scatter.method.value if step.scatter is not None and step.scatter.method is not None else \
        others.pop('scatterMethod', None)
    others.pop('when', None)
    if step.scatter is not None:
        known['scatter'] = list(step.scatter.inputs)
    if method is not None:
        known['scatterMethod'] = deepcopy(method)
    if when is not None:
        known['when'] = deepcopy(when)
    known.update({key: deepcopy(value) for key, value in others.items()})
    return known


def _binding(value: StepValue) -> str | dict[str, Value]:
    """One `in:` entry: the source alone as the bare string, which means what `{source: ...}` does, or a mapping."""
    match value:
        case Raw(text=text):
            return text
        case Entry(sources=sources, passthrough=passthrough, merge=merge, pick=pick, default=default,
                   has_default=has_default):
            known: dict[str, Value] = {}
            if sources:
                texts: list[Value] = [spelling(source) for source in sources]
                # One source stays a list beside `linkMerge`, which merges a list.
                known['source'] = texts if len(texts) > 1 or merge is not None else texts[0]
            if merge is not None:
                known['linkMerge'] = merge.value
            if pick is not None:
                known['pickValue'] = pick.value
            if has_default:
                known['default'] = write_value(default)
            known.update({key: deepcopy(field) for key, field in passthrough})
            return known['source'] if list(known) == ['source'] and isinstance(known['source'], str) else known


def _port(declaration: PortDeclaration, source: OutputSource = None) -> Value:
    """A workflow input or output, without assigning meaning to opaque fields."""
    if declaration.shorthand:
        return written_type(declaration.type, declaration.unread)
    known: dict[str, Value] = {'type': written_type(declaration.type, declaration.unread)}
    if declaration.has_format:
        known['format'] = deepcopy(declaration.format)
    if declaration.has_default:
        known['default'] = write_value(declaration.default)
    match source:
        case None:
            pass
        case InputRef() | OutputRef():
            known['outputSource'] = spelling(source)
        case Verbatim(value=value):
            known['outputSource'] = deepcopy(value)
    known.update({name: deepcopy(value) for name, value in declaration.passthrough})
    return known


def _requirements(workflow: ResolvedWorkflow, whens: list[Value]) -> dict[str, Value]:
    """The requirements the author wrote and the ones `workflow`'s calls, scatters, `when`s and step-input records
    imply, by class."""
    entries = [entry for step in workflow.steps for entry in step.inputs]
    value_from = [str(text) for entry in entries for key, text in entry.passthrough if key == 'valueFrom']
    requirements = {each.name: deepcopy(each.written) for each in workflow.needs if not each.hint}
    for requirement, needed in (
            ('SubworkflowFeatureRequirement', any(isinstance(step.run, ResolvedWorkflow) for step in workflow.steps)),
            ('ScatterFeatureRequirement',
             any(step.scatter is not None and step.scatter.inputs for step in workflow.steps)),
            ('InlineJavascriptRequirement',
             any(when is not None for when in whens) or any('$(' in text or '${' in text for text in value_from)),
            ('MultipleInputFeatureRequirement',
             any(len(entry.sources) > 1 or entry.merge is not None for entry in entries)),
            ('StepInputExpressionRequirement', bool(value_from))):
        # An authored body is kept; an authored `Class:` with no body is `{}`, as CWL needs.
        if needed and requirements.get(requirement) is None:
            requirements[requirement] = {}
    return dict(sorted(requirements.items()))


def _uses_edam(workflow: ResolvedWorkflow) -> bool:
    """Whether a `format:` of `workflow`'s inputs or outputs, or of a File in its job or a `default:`, is an `edam:`
    CURIE.

    The job and the defaults count because a runner reads their CURIEs against the workflow's `$namespaces`.
    """
    declarations: list[PortDeclaration] = [port.declaration for port in workflow.inputs]
    declarations += [port.declaration for port in workflow.outputs]
    formats = [declaration.format for declaration in declarations if declaration.has_format]
    formats += [found for declaration in declarations if declaration.has_default
                for found in _job_formats(write_value(declaration.default))]
    formats += [found for step in workflow.steps for entry in step.inputs for found in _job_formats(
        write_value(entry.default))]
    formats += [found for port in workflow.inputs if port.has_value for found in _job_formats(write_value(port.value))]
    return any(isinstance(text, str) and text.startswith(f'{EDAM_NAMESPACE[0]}:')
               for value in formats for text in (value if isinstance(value, list) else [value]))


def _job_formats(value: Value) -> list[Value]:
    """Every `format` a job value holds, at any depth: a File's, a File's in a list or a record."""
    match value:
        case dict():
            own = [value['format']] if 'format' in value else []
            return own + [found for item in value.values() for found in _job_formats(item)]
        case list():
            return [found for item in value for found in _job_formats(item)]
    return []
