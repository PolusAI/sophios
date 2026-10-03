"""Resolve a parsed document against an explicit immutable registry snapshot.

Resolution does no discovery, performs no filesystem access, and parses
nothing.  Parsed workflows and process definitions are values in
``RegistrySnapshot``; changing the environment cannot change the result of
resolving the same two values.  It prints one stderr line for each
``wic: steps:`` key that addresses no step; the key is ignored.
"""
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import sys
from typing import Any, Iterable, Mapping

from ..lang import (
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    Key,
    OutputBinding,
    ParseResult,
    RawCwlRef,
    SophiosErrorCode,
    SourceSpan,
    StepKey,
    UnresolvedName,
    WicSidecar,
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
    """One named workflow, parsed once by whoever read it, supplied as data.

    The parse diagnostics travel with the document: Resolve reports them
    only for a workflow a step actually calls.
    """

    key: RegistryKey
    parsed: ParseResult


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """All environment-dependent inputs to one resolution, copied and sorted."""

    tools: tuple[ToolDefinition, ...] = ()
    workflows: tuple[WorkflowSource, ...] = ()

    @classmethod
    def from_tools(cls, tools: Tools, *,
                   workflows: Mapping[tuple[str, str], ParseResult] | None = None) -> 'RegistrySnapshot':
        """Own a deterministic snapshot of the legacy public registry."""
        definitions = tuple(sorted((
            ToolDefinition(RegistryKey(step_id.plugin_ns, step_id.stem),
                           tool.run_path, deepcopy(tool.cwl))
            for step_id, tool in tools.items()
        ), key=lambda item: item.key))
        sources = tuple(sorted((
            WorkflowSource(RegistryKey(namespace, name), parsed)
            for (namespace, name), parsed in (workflows or {}).items()
        ), key=lambda item: item.key))
        return cls(definitions, sources)

    def tool(self, key: RegistryKey) -> ToolDefinition | None:
        """Look up a tool without exposing a mutable mapping."""
        return next((tool for tool in self.tools if tool.key == key), None)

    def workflow(self, key: RegistryKey) -> WorkflowSource | None:
        """Look up a parsed workflow without touching a path."""
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
    reported: set[str] = set()
    _report_stale_keys(document.sidecar, document, name, reported)
    version = resolve_lang_version(lang_version, _version_pins(document))
    resolved, diagnostics = _resolve_document(document, registry, name, version, (), reported)
    _copy_diagnostics(diagnostics, selection_diagnostics)
    return Resolved(resolved if not diagnostics.has_errors else None, diagnostics)


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _resolve_document(document: Document, registry: RegistrySnapshot, name: str,
                      version: str, trail: tuple[RegistryKey, ...], reported: set[str]) \
        -> tuple[ResolvedDocument, Diagnostics]:
    diagnostics = Diagnostics()
    document = _apply_parameters(document)
    steps: list[ResolvedStep] = []
    counts = Counter(step.id for step in document.steps)
    for index, step in enumerate(document.steps, start=1):
        sidecar = step_sidecar(document.sidecar, index, step.id, counts[step.id])
        process = _resolve_process(step, sidecar, registry, version, trail, diagnostics, reported)
        if process is not None:
            steps.append(ResolvedStep(step, process, sidecar))
    return ResolvedDocument(name, document, tuple(steps), version), diagnostics


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def _resolve_process(step: Step, sidecar: WicSidecar | None, registry: RegistrySnapshot,
                     version: str, trail: tuple[RegistryKey, ...],
                     diagnostics: Diagnostics, reported: set[str]) -> ResolvedProcess | None:
    sidecar_entries = dict(sidecar.entries) if sidecar is not None else {}
    namespace = str(sidecar_entries.get('namespace', 'global'))
    interpreted = dict(step.interpreted)
    run = interpreted.get('run')
    authored_name = _stem(run) if isinstance(run, str) else _stem(step.id)
    generated = step.id == 'python_script'
    name = generated_process_id(step) if generated else authored_name
    key = RegistryKey(namespace, name)

    # A tool and workflow may share a stem: an ordinary step prefers the tool,
    # falling back to a workflow only when no tool exists.
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
        parsed = workflow.parsed
        _copy_diagnostics(diagnostics, parsed.diagnostics)
        if parsed.document is None:
            return None
        inherited = _inherit_parameters(parsed.document, sidecar)
        # A called workflow selects its implementation exactly as a root one does.
        child_source, selection = _select_implementation(inherited, registry)
        _copy_diagnostics(diagnostics, selection)
        if child_source is None:
            return None
        if child_source is inherited:
            _report_stale_keys(parsed.document.sidecar, child_source, workflow_key.name, reported)
            _report_stale_keys(sidecar, child_source, workflow_key.name, reported)
        else:
            _report_stale_keys(child_source.sidecar, child_source, workflow_key.name, reported)
        child, child_diagnostics = _resolve_document(
            child_source, registry, workflow_key.name, version, trail + (workflow_key,), reported)
        _copy_diagnostics(diagnostics, child_diagnostics)
        interface = _workflow_interface(child_source, workflow_key, diagnostics)
        if interface is None:
            return None
        inputs, outputs = interface
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
        tool.run_path,
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
    # Bodies are written inline in this document, so selection is a lookup.
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
    _copy_diagnostics(diagnostics, source.parsed.diagnostics)
    return source.parsed.document, diagnostics


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
    return tuple(ResolvedPort(name, port_declaration(declaration, output=output))
                 for name, declaration in _named_entries(raw))


def _named_entries(raw: Any) -> tuple[tuple[str, Any], ...]:
    """CWL's map form (`{name: decl}`) or its `id`-keyed list form."""
    match raw:
        case dict():
            return tuple(raw.items())
        case list():
            return tuple((str(item['id']).rsplit('#', 1)[-1], {k: v for k, v in item.items() if k != 'id'})
                         for item in raw)
        case _:
            return ()


def _workflow_interface(document: Document, key: RegistryKey, diagnostics: Diagnostics) \
        -> tuple[tuple[ResolvedPort, ...], tuple[ResolvedPort, ...]] | None:
    """The ports a called workflow declares, or None, reported, when it declares them in a list.

    The parser reads CWL's list form of `inputs:` and `outputs:` as a mapping.
    It leaves a list as written only when it holds an `$import` or `$include`,
    which names ports in a file only cwltool reads, so no step could be checked
    against them.
    """
    passthrough = dict(document.passthrough)
    listed = [name for name in ('inputs', 'outputs') if isinstance(passthrough.get(name), list)]
    for name in listed:
        diagnostics.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                          f'workflow {key.namespace}/{key.name} lists its {name}: with an $import or $include, '
                          'whose ports only cwltool can read; write them as a mapping to call it',
                          document.span)
    if listed:
        return None
    return (_ports(passthrough.get('inputs', {}), output=False),
            _ports(passthrough.get('outputs', {}), output=True))


#: The two keys a `(N, name)` body contributes to the step it names; every
#: other key under it belongs to the child document's own `wic:` block.
_CONTRIBUTED_KEYS = ('in', 'out')

#: The span a contributed value carries when the sidecar it came from has
#: none. The parser gives every `wic:` block a span, so this stands only for a
#: sidecar built in memory, where there is no source position to name.
_CONTRIBUTION_SPAN = SourceSpan('<wic parameter passing>', 1, 1, 1, 1)


def _apply_parameters(document: Document) -> Document:
    """Contribute each `wic: steps: (N, name):` body to the step it names."""
    if document.sidecar is None:
        return document
    counts = Counter(step.id for step in document.steps)
    steps = tuple(_contributed_step(step, step_sidecar(document.sidecar, index, step.id, counts[step.id]))
                  for index, step in enumerate(document.steps, start=1))
    return document if steps == document.steps else replace(document, steps=steps)


def _contributed_step(step: Step, sidecar: WicSidecar | None) -> Step:
    """One step with its contributed `in:`/`out:` applied, the contributor
    winning; a `.wic` step receives nothing, since its body addresses steps
    inside the subworkflow. A contributed `out:` replaces the step's own entirely.
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

    Accepts either an already-typed tagged construct or its desugared mapping
    spelling; both surfaces mean one thing (§4.1).
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
    """Read one contributed `out:` entry -- a bare name, or a single-key
    mapping to a desugared edge definition -- back into an output binding.
    """
    match entry:
        case {**binding} if len(binding) == 1:
            name, anchor = next(iter(binding.items()))
            edge = anchor[Key.ANCHOR] if isinstance(anchor, dict) else None
            return OutputBinding(str(name), None if edge is None else EdgeDef(str(edge), span), span)
        case _:
            return OutputBinding(str(entry), None, span)


def _inherit_parameters(document: Document, sidecar: WicSidecar | None) -> Document:
    """A child document with the calling step's `wic:` body merged into its
    own, the contributor winning; `in:`/`out:` are excluded since they address
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
        key, inherited = _inherited_step(steps, key)
        steps[key] = child if inherited is None else _merged_sidecar(inherited, child)
    entries = dict(own.entries)
    for name, value in contributed.entries:
        entries[name] = _merged_value(entries.get(name), value)
    return WicSidecar(tuple(steps.items()), tuple(entries.items()),
                      implementations=own.implementations or contributed.implementations,
                      span=own.span)


def _inherited_step(steps: dict[StepKey, WicSidecar],
                    key: StepKey) -> tuple[StepKey, WicSidecar | None]:
    """The key `key` is merged under and the entry of `steps` it is merged over.
    A bare id and an `(index, id)` key of the same name address the same step, so a
    bare id merges into the one positional entry, and a positional key absorbs the bare one.
    """
    if key in steps:
        return key, steps[key]
    if key.index is None:
        positional = [other for other in steps if other.index is not None and other.name == key.name]
        return (positional[0], steps[positional[0]]) if len(positional) == 1 else (key, None)
    bare = StepKey(None, key.name)
    return key, steps.pop(bare, None)


def _merged_value(own: OpaqueCwl, contributed: OpaqueCwl) -> OpaqueCwl:
    """Mappings merge key by key; any other contributed value replaces."""
    if not isinstance(own, dict) or not isinstance(contributed, dict):
        return contributed
    merged = dict(own)
    for key, value in contributed.items():
        merged[key] = _merged_value(merged.get(key), value)
    return merged


def step_sidecar(sidecar: WicSidecar | None, index: int, name: str,
                 occurrences: int) -> WicSidecar | None:
    """The sidecar entry addressing step `index` called `name`: its `(index, name)` key,
    else its bare id when the id occurs once in the document. A positional key wins."""
    if sidecar is None:
        return None
    by_id = None
    for key, child in sidecar.steps:
        if key.index == index and key.name == name:
            return child
        if key.index is None and key.name == name and occurrences == 1:
            by_id = child
    return by_id


def _stale_key_reason(key: StepKey, ids: Sequence[str]) -> str | None:
    """Why `key` addresses no step among `ids`, the step ids of one document; None if it does."""
    if key.index is None:
        count = ids.count(key.name)
        if count == 1:
            return None
        if count == 0:
            return f'no step is called {key.name!r}'
        return f'{key.name!r} names {count} steps; write (index, {key.name})'
    if not 1 <= key.index <= len(ids):
        noun = 'step' if len(ids) == 1 else 'steps'
        return f'there is no step {key.index}; the document has {len(ids)} {noun}'
    actual = ids[key.index - 1]
    if actual == key.name:
        return None
    return f'step {key.index} is {actual!r}; write ({key.index}, {actual})'


def _report_stale_keys(sidecar: WicSidecar | None, document: Document, name: str,
                       reported: set[str]) -> None:
    """Print, once each, a line for every ``wic: steps:`` key of `sidecar` that addresses no
    step of `document`. The keys are ignored, as they always were."""
    if sidecar is None:
        return
    ids = [step.id for step in document.steps]
    for key, child in sidecar.steps:
        reason = _stale_key_reason(key, ids)
        if reason is None:
            continue
        span = child.span or sidecar.span or _CONTRIBUTION_SPAN
        line = (f'Warning! {span.file}: wic: steps: key {key} addresses no step of {name!r}: '
                f'{reason}. The key is ignored.')
        if line not in reported:
            reported.add(line)
            print(line, file=sys.stderr)


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
