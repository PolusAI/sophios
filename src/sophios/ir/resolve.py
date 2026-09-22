"""Resolve a parsed document against an explicit immutable registry snapshot.

Resolution does no discovery and performs no filesystem access.  Workflow
sources and process definitions are values in ``RegistrySnapshot``; changing
the environment cannot change the result of resolving the same two values.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from ..lang import (
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    Key,
    OutputBinding,
    RawCwlRef,
    SophiosErrorCode,
    SourceSpan,
    UnresolvedName,
    WicSidecar,
    parse,
    resolve_lang_version,
)
from ..lang.diagnostics import Diagnostic, Diagnostics
from ..lang.nodes import InputValue, OpaqueCwl, Step
from ..wic_types import Cwl, Tools
from .declarations import port_declaration
from .types import PortDeclaration, RegistryKey


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """An owned snapshot of one runnable process definition."""

    key: RegistryKey
    run_path: str
    cwl: Cwl


@dataclass(frozen=True, slots=True)
class WorkflowSource:
    """One named workflow source supplied to Resolve as data."""

    key: RegistryKey
    source: str


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """All environment-dependent inputs to one resolution, copied and sorted."""

    tools: tuple[ToolDefinition, ...] = ()
    workflows: tuple[WorkflowSource, ...] = ()

    @classmethod
    def from_tools(cls, tools: Tools, *,
                   workflows: Mapping[tuple[str, str], str] | None = None) -> 'RegistrySnapshot':
        """Own a deterministic snapshot of the legacy public registry."""
        definitions = tuple(sorted((
            ToolDefinition(RegistryKey(step_id.plugin_ns, step_id.stem),
                           tool.run_path, deepcopy(tool.cwl))
            for step_id, tool in tools.items()
        ), key=lambda item: item.key))
        sources = tuple(sorted((
            WorkflowSource(RegistryKey(namespace, name), source)
            for (namespace, name), source in (workflows or {}).items()
        ), key=lambda item: item.key))
        return cls(definitions, sources)

    def tool(self, key: RegistryKey) -> ToolDefinition | None:
        """Look up a tool without exposing a mutable mapping."""
        return next((tool for tool in self.tools if tool.key == key), None)

    def workflow(self, key: RegistryKey) -> WorkflowSource | None:
        """Look up workflow source content without touching a path."""
        return next((workflow for workflow in self.workflows if workflow.key == key), None)


@dataclass(frozen=True, slots=True)
class ResolvedPort:
    """A named process port with its complete declaration."""

    name: str
    declaration: PortDeclaration


@dataclass(frozen=True, slots=True)
class ResolvedProcess:
    """The process a step names, including a recursively resolved workflow."""

    key: RegistryKey
    run_path: OpaqueCwl
    inputs: tuple[ResolvedPort, ...]
    outputs: tuple[ResolvedPort, ...]
    declaration: OpaqueCwl
    child: 'ResolvedDocument | None' = None
    generated: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedStep:
    """One authored occurrence paired with the process it invokes."""

    source: Step
    process: ResolvedProcess
    sidecar: WicSidecar | None = None


@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    """A document after every process identity and interface is known."""

    name: str
    source: Document
    steps: tuple[ResolvedStep, ...]
    lang_version: str


@dataclass(frozen=True, slots=True)
class Resolved:
    """Resolution result: a document when successful and all diagnostics."""

    document: ResolvedDocument | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        """Whether resolution produced a document without errors."""
        return self.document is not None and not self.diagnostics.has_errors


def resolve(document: Document, registry: RegistrySnapshot, *, name: str = 'workflow',
            lang_version: str | None = None) -> Resolved:
    """Resolve ``document`` solely from ``registry`` and explicit arguments."""
    selected, selection_diagnostics = _select_implementation(document, registry)
    if selected is None:
        return Resolved(None, selection_diagnostics)
    document = selected
    version = resolve_lang_version(lang_version, _version_pins(document))
    resolved, diagnostics = _resolve_document(document, registry, name, version, ())
    _copy_diagnostics(diagnostics, selection_diagnostics)
    return Resolved(resolved if not diagnostics.has_errors else None, diagnostics)


def _resolve_document(document: Document, registry: RegistrySnapshot, name: str,
                      version: str, trail: tuple[RegistryKey, ...]) \
        -> tuple[ResolvedDocument, Diagnostics]:
    diagnostics = Diagnostics()
    document = _apply_parameters(document)
    steps: list[ResolvedStep] = []
    for index, step in enumerate(document.steps, start=1):
        sidecar = _step_sidecar(document.sidecar, index, step.id)
        process = _resolve_process(step, sidecar, registry, version, trail, diagnostics)
        if process is not None:
            steps.append(ResolvedStep(step, process, sidecar))
    return ResolvedDocument(name, document, tuple(steps), version), diagnostics


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def _resolve_process(step: Step, sidecar: WicSidecar | None, registry: RegistrySnapshot,
                     version: str, trail: tuple[RegistryKey, ...],
                     diagnostics: Diagnostics) -> ResolvedProcess | None:
    sidecar_entries = dict(sidecar.entries) if sidecar is not None else {}
    namespace = str(sidecar_entries.get('namespace', 'global'))
    interpreted = dict(step.interpreted)
    run = interpreted.get('run')
    authored_name = _stem(run) if isinstance(run, str) else _stem(step.id)
    generated = step.id == 'python_script'
    name = generated_process_id(step) if generated else authored_name
    key = RegistryKey(namespace, name)

    # A tool and a workflow may intentionally share a stem.  The authored
    # ``.wic`` spelling selects the workflow; an ordinary step selects the
    # tool when one exists and falls back to a workflow only when it does not.
    # Looking up workflows first makes an attached ``fail.wic`` recursively
    # resolve the ``fail`` tool inside itself as the workflow again.
    explicit_workflow = step.id.endswith('.wic') \
        or (isinstance(run, str) and run.endswith('.wic'))
    tool = registry.tool(key)
    if tool is None and run is not None and isinstance(run, str):
        tool = registry.tool(RegistryKey(namespace, _stem(run)))
    workflow = registry.workflow(key) if explicit_workflow or tool is None else None
    if workflow is None and explicit_workflow:
        workflow = registry.workflow(RegistryKey(namespace, _stem(step.id)))
    if workflow is not None:
        workflow_key = workflow.key
        if workflow_key in trail:
            diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                              f'workflow cycle reaches {workflow_key.namespace}/{workflow_key.name}',
                              step.span)
            return None
        parsed = parse(workflow.source, f'{workflow_key.name}.wic')
        _copy_diagnostics(diagnostics, parsed.diagnostics)
        if parsed.document is None:
            return None
        child_source = _inherit_parameters(parsed.document, sidecar)
        child, child_diagnostics = _resolve_document(
            child_source, registry, workflow_key.name, version, trail + (workflow_key,))
        _copy_diagnostics(diagnostics, child_diagnostics)
        inputs, outputs = _workflow_interface(child_source)
        return ResolvedProcess(workflow_key, f'{workflow_key.name}.cwl', inputs, outputs,
                               {'class': 'Workflow'}, child)

    if tool is None:
        diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                          f'process {namespace}/{name} is absent from the supplied registry',
                          step.span)
        return None
    cwl = tool.cwl
    if not isinstance(cwl, dict):
        diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                          f'process {namespace}/{name} is not a CWL mapping', step.span)
        return None
    return ResolvedProcess(
        tool.key,
        deepcopy(run) if run is not None and not isinstance(run, str) else tool.run_path,
        _ports(cwl.get('inputs', {}), output=False),
        _ports(cwl.get('outputs', {}), output=True),
        deepcopy(cwl),
        generated=generated,
    )


def generated_process_id(step: Step) -> str:
    """A stable identity for a generated Python tool, independent of process state."""
    payload = [(name, _input_identity(value)) for name, value in step.inputs]
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                               default=str).encode('utf-8')).hexdigest()[:16]
    return f'python_script_{digest}'


def _select_implementation(document: Document,
                           registry: RegistrySnapshot) -> tuple[Document | None, Diagnostics]:
    diagnostics = Diagnostics()
    if document.sidecar is None:
        return document, diagnostics
    entries = dict(document.sidecar.entries)
    if 'implementations' not in entries:
        return document, diagnostics
    chosen = entries.get('implementation', entries.get('default_implementation'))
    if not isinstance(chosen, str) or not chosen:
        diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                          'a workflow with implementations needs an implementation selection',
                          document.sidecar.span)
        return None, diagnostics
    # Parsed in place by the parser: the bodies are written inline in this
    # document, so the selection is a lookup, and the spans inside the chosen
    # one already point at the file the reader wrote.
    selected = dict(document.sidecar.implementations).get(chosen)
    if selected is not None:
        return selected, diagnostics
    namespace = str(entries.get('namespace', 'global'))
    source = registry.workflow(RegistryKey(namespace, chosen))
    if source is None:
        diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                          f'implementation {namespace}/{chosen} is absent from the supplied registry',
                          document.sidecar.span)
        return None, diagnostics
    parsed = parse(source.source, f'{chosen}.wic')
    _copy_diagnostics(diagnostics, parsed.diagnostics)
    return parsed.document, diagnostics


def _input_identity(value: InputValue) -> Any:
    match value:
        case InlineLiteral(value=literal):
            return ('literal', literal)
        case EdgeRef(name=name):
            return ('edge', name)
        case RawCwlRef(expression=expression):
            return ('cwl', expression)
        case UnresolvedName(name=name):
            return ('name', name)


def _ports(raw: Any, *, output: bool) -> tuple[ResolvedPort, ...]:
    if not isinstance(raw, dict):
        return ()
    return tuple(ResolvedPort(str(name), port_declaration(declaration, output=output))
                 for name, declaration in raw.items())


def _workflow_interface(document: Document) -> tuple[tuple[ResolvedPort, ...],
                                                     tuple[ResolvedPort, ...]]:
    passthrough = dict(document.passthrough)
    return (_ports(passthrough.get('inputs', {}), output=False),
            _ports(passthrough.get('outputs', {}), output=True))


#: The two keys a `(N, name)` body contributes to the step it names. Every
#: other key under it belongs to the child document's own `wic:` block --
#: `namespace`, `graphviz`, `steps` -- which is why `merge_yml_trees` deletes
#: `wic` from the step arguments and merges it into the child instead.
_CONTRIBUTED_KEYS = ('in', 'out')

#: The span a contributed value carries when the sidecar it came from has
#: none. The parser gives every `wic:` block a span, so this stands only for a
#: sidecar built in memory, where there is no source position to name.
_CONTRIBUTION_SPAN = SourceSpan('<wic parameter passing>', 1, 1, 1, 1)


def _apply_parameters(document: Document) -> Document:
    """Contribute each `wic: steps: (N, name):` body to the step it names.

    Parameter passing (`ast.py::merge_yml_trees`): a document reaches a step
    of a subworkflow by nesting `wic:` blocks, and the innermost body's `in:`
    and `out:` land on that step -- which is how `basic.wic` places the sole
    definition of an edge on a step of a subworkflow called from two roots.
    Every ancestor's contribution is merged into a document's own sidecar
    before it is resolved, so applying the sidecar a document carries applies
    the whole chain, at whatever depth each contribution entered it.
    """
    if document.sidecar is None:
        return document
    steps = tuple(_contributed_step(step, _step_sidecar(document.sidecar, index, step.id))
                  for index, step in enumerate(document.steps, start=1))
    return document if steps == document.steps else replace(document, steps=steps)


def _contributed_step(step: Step, sidecar: WicSidecar | None) -> Step:
    """One step with its contributed `in:`/`out:` applied, the contributor winning.

    A `.wic` step receives nothing: its body names steps *inside* the
    subworkflow and is passed down, exactly as `merge_yml_trees` applies step
    arguments only off the subworkflow keys. `out:` is a sequence, so a
    contributed one replaces the step's own entirely, as `TYPESAFE_REPLACE`
    does for every non-mapping.
    """
    if sidecar is None or step.id.endswith('.wic'):
        return step
    entries = dict(sidecar.entries)
    span = sidecar.span or _CONTRIBUTION_SPAN
    inputs = step.inputs
    contributed_in = entries.get('in')
    if isinstance(contributed_in, dict):
        bound = dict(step.inputs)
        for name, value in contributed_in.items():
            bound[name] = _contributed_input(value, span)
        inputs = tuple(bound.items())
    contributed_out = entries.get('out')
    outputs = tuple(_contributed_output(entry, span) for entry in contributed_out) \
        if isinstance(contributed_out, list) else step.outputs
    if (inputs, outputs) == (step.inputs, step.outputs):
        return step
    return replace(step, inputs=inputs, outputs=outputs)


def _contributed_input(value: OpaqueCwl, span: SourceSpan) -> InputValue:
    """Read one contributed `in:` value back into the closed input union.

    A sidecar entry is opaque content, so a tagged construct arrives already
    typed while the desugared spelling of the same construct arrives as the
    mapping `render` emits for it. Both surfaces mean one thing (§4.1).
    """
    match value:
        case InlineLiteral() | EdgeRef() | RawCwlRef() | UnresolvedName():
            return value
        case {Key.INLINE_INPUT: literal}:
            return InlineLiteral(literal, span)
        case {Key.ALIAS: name}:
            return EdgeRef(str(name), span)
        case {Key.RAW_CWL: expression}:
            return RawCwlRef(str(expression), span)
        case dict() | list():
            return InlineLiteral(value, span)
        case _:
            return UnresolvedName(str(value), span)


def _contributed_output(entry: OpaqueCwl, span: SourceSpan) -> OutputBinding:
    """Read one contributed `out:` entry back into an output binding.

    The shape is the one the parser stores for a sidecar `out:`: a bare name,
    or a single-key mapping of that name to a desugared edge definition.
    """
    match entry:
        case {**binding} if len(binding) == 1:
            name, anchor = next(iter(binding.items()))
            edge = anchor[Key.ANCHOR] if isinstance(anchor, dict) else None
            return OutputBinding(str(name), None if edge is None else EdgeDef(str(edge), span), span)
        case _:
            return OutputBinding(str(entry), None, span)


def _inherit_parameters(document: Document, sidecar: WicSidecar | None) -> Document:
    """A child document with the calling step's `wic:` body merged into its own.

    The contributor wins wherever both name the same key, which is what
    `TYPESAFE_REPLACE` buys `merge_yml_trees`: a parent overrides the value a
    subworkflow chose for itself. `in:` and `out:` are excluded because they
    are siblings of the `wic:` wrapper rather than part of it -- they address
    the calling step, not the document it calls.
    """
    if sidecar is None:
        return document
    entries = tuple((key, value) for key, value in sidecar.entries
                    if key not in _CONTRIBUTED_KEYS)
    if not sidecar.steps and not entries:
        return document
    return replace(document, sidecar=_merged_sidecar(
        document.sidecar, WicSidecar(sidecar.steps, entries,
                                     implementations=sidecar.implementations,
                                     span=sidecar.span)))


def _merged_sidecar(own: WicSidecar | None, contributed: WicSidecar) -> WicSidecar:
    """`own` with `contributed` merged over it, key by key and step by step."""
    if own is None:
        return contributed
    steps = dict(own.steps)
    for key, child in contributed.steps:
        inherited = steps.get(key)
        steps[key] = child if inherited is None else _merged_sidecar(inherited, child)
    entries = dict(own.entries)
    for name, value in contributed.entries:
        entries[name] = _merged_value(entries.get(name), value)
    return WicSidecar(tuple(steps.items()), tuple(entries.items()),
                      implementations=own.implementations or contributed.implementations,
                      span=own.span)


def _merged_value(own: OpaqueCwl, contributed: OpaqueCwl) -> OpaqueCwl:
    """Mappings merge key by key; any other contributed value replaces."""
    if not isinstance(own, dict) or not isinstance(contributed, dict):
        return contributed
    merged = dict(own)
    for key, value in contributed.items():
        merged[key] = _merged_value(merged.get(key), value)
    return merged


def _step_sidecar(sidecar: WicSidecar | None, index: int, name: str) -> WicSidecar | None:
    if sidecar is None:
        return None
    return next((child for key, child in sidecar.steps
                 if key.index == index and key.name == name), None)


def _version_pins(document: Document) -> tuple[str, ...]:
    if document.sidecar is None:
        return ()
    value = dict(document.sidecar.entries).get('lang_version')
    return () if value is None else (str(value),)


def _stem(value: str) -> str:
    leaf = value.replace('\\', '/').rsplit('/', 1)[-1]
    return leaf[:-4] if leaf.endswith('.wic') else leaf[:-4] if leaf.endswith('.cwl') else leaf


def _copy_diagnostics(target: Diagnostics, source: Iterable[Diagnostic]) -> None:
    for diagnostic in source:
        target._append(diagnostic)  # pylint: disable=protected-access
