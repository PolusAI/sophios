"""Resolve a parsed document against an explicit immutable registry snapshot.

Resolution does no discovery and performs no filesystem access.  Workflow
sources and process definitions are values in ``RegistrySnapshot``; changing
the environment cannot change the result of resolving the same two values.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from ..lang import (
    Document,
    EdgeRef,
    InlineLiteral,
    RawCwlRef,
    SophiosErrorCode,
    UnresolvedName,
    WicSidecar,
    parse,
    resolve_lang_version,
)
from ..lang.diagnostics import Diagnostic, Diagnostics
from ..lang.nodes import InputValue, OpaqueCwl, Step
from ..wic_types import Cwl, Tools
from .declarations import port_declaration
from .types import PortDeclaration


@dataclass(frozen=True, slots=True, order=True)
class RegistryKey:
    """A process name in one plugin namespace."""

    namespace: str
    name: str


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
        child, child_diagnostics = _resolve_document(
            parsed.document, registry, workflow_key.name, version, trail + (workflow_key,))
        _copy_diagnostics(diagnostics, child_diagnostics)
        inputs, outputs = _workflow_interface(parsed.document)
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
