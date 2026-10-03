"""Build Python API objects from a `.wic` file: `Workflow.from_wic`.

The file is read, resolved and compiled by the code `sophios --yaml` runs:
the file door collects every document and tool it reaches, and Resolve pairs
each step with its process, applies `wic: steps:` contributions and selects
nothing more. The objects are then built from that resolution, so a step holds
exactly the tool the file compile runs.

A construct the Python API cannot hold is refused with `api006`, all of them at
once. Nothing is guessed: no step is renamed, no edge is moved and nothing the
document says is dropped but `wic: graphviz` and `wic: inlineable`, which no
Python compile reads, and `cwlVersion`, which the compiler replaces.
"""

# pylint: disable=protected-access
# This module is a private adapter between a resolved document and the
# workflow objects, so reaching their internal state is intentional.

import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from sophios import compiler
from sophios.cli import default_compilation_settings
from sophios.ir.frontdoor import SourceBundle, bundle_from_disk
from sophios.ir.lower import output_step
from sophios.ir.resolve import ResolvedDocument, ResolvedStep, resolve
from sophios.lang import (CwlRecord, Diagnostic, Document, EdgeRef, InlineLiteral, InputValue, RawCwlRef,
                          Severity, SophiosError, SophiosErrorCode, SourceSpan, UnresolvedName, WicSidecar,
                          resolve_lang_version, to_json)
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import Tools

from ._ports import OutputParameter, _validate_namespace_name
from ._utils import authored_at, validate_python_identifier_name

if TYPE_CHECKING:
    from .workflow import Step, Workflow

#: Top-level keys a Python `Workflow` carries. `cwlVersion` is read and
#: replaced by the compiler, so it is accepted and dropped.
_DOCUMENT_KEYS: Final = frozenset({'inputs', 'outputs', 'cwlVersion'})

#: `wic:` keys that change nothing a Python workflow compiles or runs: they
#: draw the graph and allow `--cwl_inline_subworkflows`. Dropped, with a warning.
_DROPPED_WIC_KEYS: Final = ('graphviz', 'inlineable')

#: What a `wic: steps:` entry contributes to the step it names, already
#: applied by Resolve as the step's own `in:`/`out:`.
_CONTRIBUTED_WIC_KEYS: Final = frozenset({'in', 'out'})

#: `StepInput` field names, by the CWL spelling a `!cwl {...}` record writes.
_RECORD_FIELDS: Final = {'linkMerge': 'link_merge', 'pickValue': 'pick_value', 'valueFrom': 'value_from',
                         'loadContents': 'load_contents', 'loadListing': 'load_listing',
                         'label': 'label', 'default': 'default'}


@dataclass(slots=True)
class _Reading:
    """What reading the whole reach found: refusals, the `wic:` keys dropped per file, and
    the first call of each called document, with the body that call gives it."""

    refusals: list[Diagnostic] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    called: dict[str, tuple[Any, SourceSpan | None]] = field(default_factory=dict)

    def refuse(self, span: SourceSpan | None, message: str) -> None:
        """Record one construct the Python API cannot hold, once: a called document's `wic:` block
        is read as written and as merged with its caller's."""
        refusal = Diagnostic(Severity.ERROR, SophiosErrorCode.NO_PYTHON_SPELLING, message, span)
        if refusal not in self.refusals:
            self.refusals.append(refusal)

    def drop(self, span: SourceSpan | None, key: str) -> None:
        """Record a `wic:` key dropped from the file `span` names."""
        keys = self.dropped.setdefault(span.file if span is not None else '<wic>', [])
        if key not in keys:
            keys.append(key)


def workflow_from_wic(path: Path, tool_registry: Tools,
                      workflow_paths: Mapping[str, Mapping[str, Path]]) -> "Workflow":
    """Build the `Workflow` a `.wic` file describes.

    Args:
        path (Path): The root `.wic` file.
        tool_registry (Tools): The tools a step may name, as the CLI's registry holds them.
        workflow_paths (Mapping[str, Mapping[str, Path]]): The `.wic` files a step may
            call, `{namespace: {stem: path}}`.

    Raises:
        SophiosError: The file's own compile errors, or `api006` for every
            construct the Python API cannot hold.

    Returns:
        Workflow: The root workflow, its steps and nested workflows built.
    """
    bundle = bundle_from_disk(path, {namespace: dict(paths) for namespace, paths in workflow_paths.items()},
                              tool_registry)
    compiler_options, graph_settings = default_compilation_settings()
    compiler.compile_source(bundle, compiler_options, graph_settings,
                            relative_run_path=True, testing=False, graph_target=get_graph_reps(path.stem))
    assert bundle.parsed.document is not None  # the compile above raised otherwise
    version = resolve_lang_version(compiler_options.get('lang_version'), bundle.lang_version_pins)
    resolved = resolve(bundle.parsed.document, bundle.registry, name=bundle.name, lang_version=version)
    assert resolved.document is not None  # the compile above resolved it

    reading = _Reading()
    _read_document(resolved.document, bundle.parsed.document, bundle, reading)
    if reading.refusals:
        raise SophiosError(reading.refusals)
    for file, keys in reading.dropped.items():
        warnings.warn(f"{file}: wic: {', '.join(keys)} dropped: the Python API has no spelling for "
                      f"{'them' if len(keys) > 1 else 'it'}, and it changes no CWL a Workflow compiles or runs",
                      UserWarning, stacklevel=3)
    return _build_workflow(resolved.document)


# --------------------------------------------------------------------------
# Reading: every refusal in the whole reach, before anything is built
# --------------------------------------------------------------------------


def _read_document(document: ResolvedDocument, authored: Document, bundle: SourceBundle,
                   reading: _Reading) -> None:
    """Refuse what the Python API cannot hold in `document` and in every document it calls.

    `authored` is the document as written, before an implementation was selected.
    """
    source = document.source
    span = source.span
    _read_sidecar(authored.sidecar, span, reading)
    if source is not authored:
        _read_sidecar(source.sidecar, span, reading)
    for key, _value in source.passthrough:
        if key not in _DOCUMENT_KEYS:
            reading.refuse(span, f'{key}: a Python Workflow carries no top-level {key!r}; '
                           'only inputs:, outputs: and steps: have a Python spelling')
    declared = _read_ports(source, span, reading)
    _read_steps(document, declared, bundle, reading)
    _read_outputs(document, declared, reading)


def _read_sidecar(sidecar: WicSidecar | None, span: SourceSpan | None, reading: _Reading) -> None:
    """Refuse a document's `wic:` keys the API cannot say, and record the ones it drops."""
    if sidecar is None:
        return
    for key, value in sidecar.entries:
        if key in _DROPPED_WIC_KEYS:
            reading.drop(sidecar.span or span, key)
        elif key == 'namespace' and value == 'global':
            continue
        elif key == 'implementations':
            reading.refuse(sidecar.span or span,
                           'wic: implementations: a Python Workflow has one body; build the one you want '
                           'with from_wic on a file that holds only it')
        elif key == 'lang_version':
            reading.refuse(sidecar.span or span,
                           f'wic: lang_version: {value} pins the language version in a document; a Python '
                           f'workflow pins it per compile: pass compile(lang_version={str(value)!r})')
        else:
            reading.refuse(sidecar.span or span,
                           f'wic: {key}: {value!r} has no Python spelling; a Python workflow is in the '
                           'global namespace and carries no wic: metadata')


def _read_ports(source: Document, span: SourceSpan | None, reading: _Reading) -> set[str]:
    """Refuse a port declaration the API cannot hold; return the declared input names."""
    declared: set[str] = set()
    for kind, allowed in (('inputs', {'type'}), ('outputs', {'type', 'outputSource'})):
        if not isinstance(dict(source.passthrough).get(kind, {}), dict):
            reading.refuse(span, f'{kind}: is a list; a Python workflow declares its ports as a mapping')
            continue
        for name, spec in _ports(source, kind).items():
            _read_name(str(name), f'workflow {kind[:-1]}', span, reading)
            if kind == 'inputs':
                declared.add(str(name))
            extra = sorted(set(spec) - allowed) if isinstance(spec, dict) else []
            if extra:
                reading.refuse(span, f'{kind}: {name}: carries {", ".join(extra)}; a Python workflow port '
                               'carries only a type' + (' and its source' if kind == 'outputs' else ''))
            if kind == 'outputs' and not (isinstance(spec, dict) and 'outputSource' in spec):
                reading.refuse(span, f'outputs: {name}: has no outputSource; a Python workflow output is '
                               'bound to a step output or a workflow input')
    return declared


def _ports(source: Document, kind: str) -> dict[str, Any]:
    """A document's `inputs:` or `outputs:` mapping; none when it declares them otherwise."""
    ports = dict(source.passthrough).get(kind, {})
    return cast(dict[str, Any], ports) if isinstance(ports, dict) else {}


def _read_name(name: str, what: str, span: SourceSpan | None, reading: _Reading) -> None:
    """Refuse a port name no Python attribute can spell."""
    try:
        _validate_namespace_name(validate_python_identifier_name(name, context=what), context=what)
    except ValueError:
        reading.refuse(span, f'{what} {name!r}: no Python port can be called {name!r}; '
                       'rename it to a Python identifier')


def _read_steps(document: ResolvedDocument, declared: set[str], bundle: SourceBundle,
                reading: _Reading) -> None:
    """Refuse what each step says that a Python `Step` or nested `Workflow` cannot."""
    first: dict[str, int] = {}
    defined: set[str] = set()
    for index, resolved in enumerate(document.steps, start=1):
        step = resolved.source
        span = step.span
        if step.id in first:
            reading.refuse(span, f'steps {first[step.id]} and {index} are both called {step.id!r}; a Python '
                           'workflow names each step once: give one its own id: and run: <tool>')
        first.setdefault(step.id, index)
        for key, _value in step.passthrough:
            reading.refuse(span, f'step {step.id!r}: {key}: a Python Step carries no {key!r}')
        _read_step_sidecar(resolved, reading)
        _read_process(resolved, bundle, reading)
        for name, value in step.inputs:
            _read_name(name, f'step {step.id!r} input', span, reading)
            _read_value(step.id, name, value, declared, defined, span, reading)
        for output in step.outputs:
            if output.edge_def is not None:
                defined.add(output.edge_def.name)


def _read_step_sidecar(resolved: ResolvedStep, reading: _Reading) -> None:
    """Refuse a `wic: steps:` entry's keys that Resolve did not turn into the step's `in:`/`out:`.

    A call's entry is merged into the called document, which is read on its own.
    """
    if resolved.sidecar is None or resolved.process.child is not None:
        return
    for key, value in resolved.sidecar.entries:
        if key in _CONTRIBUTED_WIC_KEYS or (key == 'namespace' and value == 'global'):
            continue
        if key in _DROPPED_WIC_KEYS:
            reading.drop(resolved.sidecar.span or resolved.source.span, key)
            continue
        reading.refuse(resolved.sidecar.span or resolved.source.span,
                       f'wic: steps: {resolved.source.id}: {key}: {value!r} has no Python spelling'
                       + ('; write scatter: and scatterMethod: on the step itself'
                          if key in ('scatter', 'scatterMethod') else ''))


def _read_process(resolved: ResolvedStep, bundle: SourceBundle, reading: _Reading) -> None:
    """Refuse what a step's process and its `run:`/`scatter:`/`when:` say that the API cannot."""
    step, process = resolved.source, resolved.process
    span = step.span
    interpreted = dict(step.interpreted)
    run = interpreted.get('run')
    if process.generated:
        reading.refuse(span, f'step {step.id!r} is a python_script step; build its tool with '
                       'sophios.api.python.tool_builder and make it a Step')
        return
    if isinstance(run, str) and run.endswith('.wic'):
        reading.refuse(span, f'step {step.id!r}: run: {run} names a workflow; a nested Python Workflow is '
                       f'named after its file: write the step as id: {Path(run).name}')
    if process.child is not None:
        keys = [f'{key}:' for key in ('scatter', 'scatterMethod', 'when') if key in interpreted]
        if keys:
            reading.refuse(span, f'step {step.id!r} calls a workflow with {", ".join(keys)}; a nested Python '
                           'Workflow has no scatter_on or when: scatter or condition its steps instead')
        _read_call(process.child, span, reading)
        child_outputs = {port.name for port in process.outputs}
        for output in step.outputs:
            if output.name not in child_outputs:
                reading.refuse(output.span or span,
                               f'step {step.id!r} names output {output.name!r}, which {process.key.name}.wic '
                               "does not declare; declare it in that file's outputs:")
        authored = bundle.registry.workflow(process.key)
        assert authored is not None and authored.parsed.document is not None  # Resolve found it
        _read_document(process.child, authored.parsed.document, bundle, reading)
        return
    if 'scatter' in interpreted and 'scatterMethod' not in interpreted:
        reading.refuse(span, f'step {step.id!r} has scatter: and no scatterMethod:; a Python step always '
                       'writes its method: add scatterMethod: dotproduct')
    for port in process.inputs + process.outputs:
        _read_name(port.name, f'{step.id!r} port', span, reading)


def _read_call(child: ResolvedDocument, span: SourceSpan | None, reading: _Reading) -> None:
    """Refuse a call that gives a document another body than its first call gave it.

    A caller's `wic: steps:` contributions are applied to the called document, so two
    calls may build it apart; a nested Python `Workflow` is named after its file, and one
    name holds one workflow. The body compared is the document with its contributions
    applied, without its `wic:` block, which those contributions are written in.
    """
    body = to_json(replace(child.source, sidecar=None))
    first_body, first_span = reading.called.setdefault(child.name, (body, span))
    if body != first_body:
        reading.refuse(span, f'{child.name}.wic is called here and at {first_span} with different wic: steps: '
                       f'contributions; a nested Python Workflow is named after its file and holds one body: '
                       f'copy {child.name}.wic to a file of its own name for one of the calls')


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _read_value(step_id: str, name: str, value: InputValue, declared: set[str], defined: set[str],
                span: SourceSpan | None, reading: _Reading) -> None:
    """Refuse an `in:` value the API cannot bind: an edge from outside this document's
    earlier steps, or raw CWL that names no declared input."""
    where = value.span or span
    match value:
        case EdgeRef(name=edge) if edge not in defined:
            reading.refuse(where, f'step {step_id!r} input {name!r} reads !* {edge}, which no earlier step of '
                           'this document defines; a Python step binds only to earlier steps of its own '
                           'workflow: pass the value in through a workflow input')
        case RawCwlRef(expression=expression) if expression not in declared:
            reading.refuse(where, f'step {step_id!r} input {name!r} is !cwl {expression}; the Python API binds '
                           'sources as objects, never as CWL text: declare it in inputs: or bind a step '
                           'output')
        case CwlRecord(sources=sources):
            for source in sources:
                _read_value(step_id, name, source, declared, defined, span, reading)
        case _:
            pass


def _read_outputs(document: ResolvedDocument, declared: set[str], reading: _Reading) -> None:
    """Refuse an `outputSource` that names neither a step output nor a declared input."""
    for name, spec in _ports(document.source, 'outputs').items():
        raw = spec.get('outputSource') if isinstance(spec, dict) else None
        if raw is None:
            continue
        if _output_source(document, str(raw)) is None and raw not in declared:
            reading.refuse(document.source.span,
                           f'outputs: {name}: outputSource: {raw} names no step output and no declared input; '
                           'a Python workflow output is bound to one of them')


def _output_source(document: ResolvedDocument, raw: str) -> tuple[int, str] | None:
    """The step position and port an `outputSource` names, read as Lower reads it."""
    if '/' not in raw:
        return None
    step_text, port = raw.rsplit('/', 1)
    found = output_step(document.name, [step.source.id for step in document.steps], step_text)
    if found is None or not 1 <= found[0] <= len(document.steps):
        return None
    if port not in {output.name for output in document.steps[found[0] - 1].process.outputs}:
        return None
    return found[0], port


# --------------------------------------------------------------------------
# Building: objects, bottom-up, each at the `.wic` position it came from
# --------------------------------------------------------------------------


def _build_workflow(document: ResolvedDocument) -> "Workflow":
    """The `Workflow` for one resolved document; each call builds its own children."""
    from .workflow import Workflow  # pylint: disable=import-outside-toplevel

    processes = [_build_process(resolved) for resolved in document.steps]
    source = document.source
    with authored_at(source.span):
        workflow = Workflow(processes, document.name)
    for name, spec in _ports(source, 'inputs').items():
        with authored_at(source.span):
            getattr(workflow.inputs, name).as_type(spec.get('type') if isinstance(spec, dict) else spec)
    edges: dict[str, OutputParameter] = {}
    for resolved, process in zip(document.steps, processes):
        _bind_step(resolved, process, workflow, edges)
    for name, spec in _ports(source, 'outputs').items():
        with authored_at(source.span):
            if 'type' in spec:
                getattr(workflow.outputs, name).as_type(spec['type'])
            raw = str(spec['outputSource'])
            found = _output_source(document, raw)
            setattr(workflow.outputs, name, getattr(processes[found[0] - 1].outputs, found[1])
                    if found is not None else getattr(workflow.inputs, raw))
    return workflow


def _build_process(resolved: ResolvedStep) -> "Step | Workflow":
    """The `Step` holding the tool Resolve chose, or the nested `Workflow` a call names."""
    from .workflow import Step  # pylint: disable=import-outside-toplevel

    if resolved.process.child is not None:
        return _build_workflow(resolved.process.child)
    assert isinstance(resolved.process.declaration, dict)  # a tool's CWL; Resolve checked it
    with authored_at(resolved.source.span):
        return Step.from_cwl_document(resolved.process.declaration, process_name=resolved.source.id,
                                      run_path=resolved.process.run_path)


def _bind_step(resolved: ResolvedStep, process: "Step | Workflow", workflow: "Workflow",
               edges: dict[str, OutputParameter]) -> None:
    """Bind a step's `in:`, then its `scatter:` and `when:`, then record the edges its `out:` defines."""
    from .workflow import Step  # pylint: disable=import-outside-toplevel

    step = resolved.source
    for name, value in step.inputs:
        with authored_at(value.span or step.span):
            setattr(process.inputs, name, _python_value(value, workflow, edges))
    interpreted = dict(step.interpreted)
    if isinstance(process, Step):
        with authored_at(step.span):
            scatter = interpreted.get('scatter')
            if scatter is not None:
                ports = [scatter] if isinstance(scatter, str) else cast(list[Any], scatter)
                process.scatter_on(*(getattr(process.inputs, str(port)) for port in ports),
                                   method=str(interpreted['scatterMethod']))
            if 'when' in interpreted:
                process.when = str(interpreted['when'])
    for output in step.outputs:
        if output.edge_def is not None:
            edges[output.edge_def.name] = getattr(process.outputs, output.name)


def _python_value(value: InputValue, workflow: "Workflow", edges: dict[str, OutputParameter]) -> Any:
    """What a step input binds in Python for one `.wic` input value."""
    from .workflow import StepInput  # pylint: disable=import-outside-toplevel

    match value:
        case InlineLiteral(value=literal):
            return literal
        case EdgeRef(name=edge):
            return edges[edge]
        case UnresolvedName(name=name) | RawCwlRef(expression=name):
            return getattr(workflow.inputs, name)
        case CwlRecord(sources=sources, fields=fields):
            mapped = [_python_value(source, workflow, edges) for source in sources]
            options: dict[str, Any] = {_RECORD_FIELDS[key]: item for key, item in fields}
            return StepInput(source=mapped[0] if len(mapped) == 1 else mapped or None, **options)
