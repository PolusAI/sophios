"""The typed compiler boundary over the phase pipeline."""
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from itertools import chain
from pathlib import Path


from .config import SHIPPED_RENAMINGS
from .cwl import tools
from .cwl.io import dump_wic_yaml

from .ir.complete import complete
from .ir.artifacts import CompilationResult
from .ir.infer import InferencePolicy, InsertionCatalog, infer
from .ir.link import link
from .ir.namespaces import declare_namespaces
from .ir.frontdoor import SourceBundle
from .ir.pipeline import front_end
from .ir.realtime import extract
from .ir.resolve import RegistrySnapshot, ToolCheck, ToolDefinition, version_pins
from .ir.names import NAMESPACE_SEPARATOR, Names
from .ir.policy import RunPolicy
from .ir.resolved import ResolvedStep, ResolvedWorkflow, resolve
from .ir.types import RegistryKey, StepId, StepNode, WorkflowGraph
from .lang import versions
from .lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from .lang.nodes import CwlRecord, ToolDocument, UnresolvedName
from .lang.cwl import boolean_problem
from .lang.parser import step_key
from .lang.error_codes import SophiosErrorCode
from .wic_types import Cwl, Tool


@dataclass(frozen=True, slots=True)
class CompileSettings:
    """What a compilation is given besides its source: how it infers, how the workflow runs, the language version
    (None infers it), and whether a step input may name what is not a workflow input. The defaults are the command
    line's."""

    inference: InferencePolicy = InferencePolicy(renaming_conventions=SHIPPED_RENAMINGS)
    policy: RunPolicy = RunPolicy()
    lang_version: str | None = None
    allow_raw_cwl: bool = False


def compile_source(bundle: SourceBundle,
                   settings: CompileSettings) -> CompilationResult:
    """Compile a parsed root workflow and every workflow it reaches.

    The one door. A bundle read from files carries the spans of the text its
    author wrote, so a diagnostic names a position the reader can open; one
    the Python API built carries the span of the Python line that made each node.
    """
    selected_version = versions.resolve(settings.lang_version, bundle.lang_version_pins)
    front = front_end(bundle.parsed, bundle.registry, name=bundle.name, check=_tool_check())
    if front.graph is None:
        raise SophiosError(front.diagnostics)
    selected_version = _tools_version(selected_version, settings, bundle, front.graph)
    if not front.graph.steps:
        raise SophiosError.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                                 'workflows must define at least one step')
    authored, realtime = extract(front.graph)
    _check_unresolved_names(authored, settings.allow_raw_cwl)

    prepared = complete(authored)
    linked = link(prepared)
    if linked.graph is None:
        raise SophiosError(linked.diagnostics)
    inferred = infer(linked.graph, settings.inference,
                     InsertionCatalog.from_registry(bundle.registry) if settings.inference.insert_steps_automatically
                     else InsertionCatalog())
    if inferred.graph is None:
        raise SophiosError(inferred.diagnostics)
    graph = declare_namespaces(inferred.graph, bundle.registry)
    names = Names.of(graph)
    diagnostics = Diagnostics([
        *front.diagnostics, *linked.diagnostics, *inferred.diagnostics,
        *dict.fromkeys(_authored_spelling_notes(authored)),
        *dict.fromkeys(_when_notes(graph, names)),
    ])
    step_tools = dict(_tools(graph, _Descriptions(bundle.registry)))
    return CompilationResult(graph, step_tools, selected_version, diagnostics, realtime, policy=settings.policy)


def _tools_version(selected: str, settings: CompileSettings, bundle: SourceBundle, graph: WorkflowGraph) -> str:
    """The version the compile runs under once the tools its steps run are read: the ones written in `.wic` may pin a
    version, and are syntax in one."""
    written = list(_written(graph, bundle.registry))
    if not written:
        return selected
    return versions.resolve(
        settings.lang_version, (*bundle.lang_version_pins, *chain.from_iterable(map(version_pins, written))),
        needs=[(versions.TOOLS_SINCE, tool.span) for tool in written])


def _written(graph: WorkflowGraph, registry: RegistrySnapshot) -> Iterator[ToolDocument]:
    """The tool of each tool step at any depth that is written in `.wic`."""
    for step in graph.steps:
        called = graph.child(step.id)
        if called is not None:
            yield from _written(called, registry)
        elif (definition := registry.tool(step.run)) is not None and definition.source.document is not None:
            yield definition.source.document


def _tool_check() -> ToolCheck:
    """What a compile asks of each tool a step runs: cwltool's validator, once per tool however many steps run it."""
    refusals: dict[RegistryKey, str | None] = {}

    def check(tool: ToolDefinition) -> str | None:
        if tool.key not in refusals:
            try:
                tools.validate(tool.run_path, None if tool.from_file else tool.cwl)
                refusals[tool.key] = None
            except tools.InvalidTool as refused:
                refusals[tool.key] = str(refused)
        return refusals[tool.key]

    return check


def resolved_view(result: CompilationResult) -> ResolvedWorkflow:
    """The resolved view of `result`'s graph, with `result.policy`, for a target to lower;
    `bind` gives it a run's values and kept outputs.

    Each tool step is paired with the tool the compile found for it, `result.tools`: the view is built from
    the graph and its tools, never from an emitted document.
    """
    return resolve(result.graph, result.tools, policy=result.policy)


def cwl_notes(view: ResolvedWorkflow) -> tuple[Diagnostic, ...]:
    """The notes the CWL target makes of a compilation, read through its `view`; it never refuses one and never
    changes its CWL: `wic056` at each step whose authored `when` cannot give `true` or `false`, which cwltool stops
    the run at. The view's values and kept outputs change none of them."""
    return tuple(
        Diagnostic(Severity.NOTE, SophiosErrorCode.WHEN_NOT_BOOLEAN,
                   f'when {problem}: cwltool stops the run when it gives anything but true or false',
                   step.span, Locator(step=step.name))
        for step, problem in non_boolean_whens(view))


def non_boolean_whens(workflow: ResolvedWorkflow) -> tuple[tuple[ResolvedStep, str], ...]:
    """Each step of `workflow` at any depth, a call among them, in graph order, whose `when` cannot give `true` or
    `false`, with why (`boolean_problem`): the one check of a `when` every target shares."""
    found: list[tuple[ResolvedStep, str]] = []
    for step in workflow.steps:
        if step.condition is not None and (
                problem := boolean_problem(step.when_text, step.condition.type)) is not None:
            found.append((step, problem))
        if isinstance(step.run, ResolvedWorkflow):
            found.extend(non_boolean_whens(step.run))
    return tuple(found)


def _tools(graph: WorkflowGraph, descriptions: '_Descriptions') -> Iterator[tuple[StepId, Tool]]:
    """Every tool step at any depth, with the tool it runs."""
    for step in graph.steps:
        called = graph.child(step.id)
        if called is None:
            tool = descriptions.tool(step)
            yield step.id, Tool(tool.run_path, descriptions.of(step, tool))
        else:
            yield from _tools(called, descriptions)


def _authored_spelling_notes(graph: WorkflowGraph) -> list[Diagnostic]:
    """A `wic069` note for each `outputSource` that names a step by an id more than one step
    has: it means the first.

    Such a spelling still compiles, so nothing here fails the compile; each note says what
    to write instead.
    """
    notes: list[Diagnostic] = []
    spans = [step.span for step in graph.steps if step.span is not None]
    if not spans:
        return [note for child in graph.children for note in _authored_spelling_notes(child)]
    sources = {port.name: port for port in graph.workflow_outputs}
    ids = [step.id.name for step in graph.steps]
    for name, source in graph.output_mapping:
        port = sources[name]
        written = str(port.output_source).rsplit('/', 1)[0]
        key = step_key(written)
        if key is not None and key.index is not None or ids.count(source.step.name) == 1:
            continue
        position = [step.id for step in graph.steps].index(source.step) + 1
        notes.append(Diagnostic(
            Severity.NOTE, SophiosErrorCode.AMBIGUOUS_OUTPUT_SOURCE,
            f"output {str(name)!r} has outputSource '{port.output_source}', but "
            f'{ids.count(source.step.name)} steps have the id {written!r} and it means the first. '
            f"Write '({position}, {source.step.name})/{source.port}' to say so.",
            port.span or graph.span, Locator(port=str(name))))
    for child in graph.children:
        notes.extend(_authored_spelling_notes(child))
    return notes


def _when_notes(graph: WorkflowGraph, names: Names) -> list[Diagnostic]:
    """A `wic056` note for each `when:` that reads an input its step does not declare: it has
    no value there, and cwltool stops the run at a `when:` that gives anything but true or false.

    Read from the finished graph, where a call declares every name its child exposes, as
    `names` spells it. A dotted read of a made name (`inputs.touch~filename`) reads
    `inputs.touch`; the note names the bracket form.
    """
    notes: list[Diagnostic] = []
    if not any(step.span is not None for step in graph.steps):
        return [note for child in graph.children for note in _when_notes(child, names)]
    for step in graph.steps:
        declared = [names.port(port.id.port) for port in step.inputs]
        for read in step.guard.reads if step.guard is not None else ():
            if read not in declared:
                made = [name for name in declared if name.startswith(f'{read}{NAMESPACE_SEPARATOR}')]
                hint = f" Write {' or '.join(f'inputs[{name!r}]' for name in made)} to read one it declares." \
                    if made else ''
                notes.append(Diagnostic(
                    Severity.NOTE, SophiosErrorCode.WHEN_NOT_BOOLEAN,
                    f'when reads inputs.{read}, which its process does not declare; it has no value there, and '
                    f'cwltool stops the run when it gives anything but true or false.{hint}',
                    step.span, Locator(step=step.id.name)))
    for child in graph.children:
        notes.extend(_when_notes(child, names))
    return notes


def _check_unresolved_names(graph: WorkflowGraph, allow_raw_cwl: bool,
                            names: Names | None = None) -> None:
    # Authored text is recognized by comparing it with what each declared
    # input is spelled as -- an author may write a derived name -- never by
    # taking the text apart.
    names = names or Names.of(graph)
    declared = {names.port(port.name) for port in graph.workflow_inputs}
    for step in graph.steps:
        for binding in step.bindings:
            value = binding.value
            for reference in value.sources if isinstance(value, CwlRecord) else (value,):
                if not isinstance(reference, UnresolvedName) or reference.name in declared or allow_raw_cwl:
                    continue
                name = reference.name
                raise SophiosError.error(
                    SophiosErrorCode.UNRESOLVED_INPUT,
                    f'Did you forget to use !ii before {name}?',
                    span=getattr(value, 'span', None),
                    locator=Locator(step=step.id.name, index=step.id.index, port=names.port(binding.sink.port)))
    for child in graph.children:
        _check_unresolved_names(child, allow_raw_cwl, names)


class _Descriptions:
    """The tool each step runs, for one compile.

    As read, unless it refers to a file by a path relative to where it was
    written: then packed by cwltool there, once however many steps run it, so
    it means the same wherever it is written. Inference and linking have
    already read every tool as written.
    """

    def __init__(self, registry: RegistrySnapshot) -> None:
        self._registry = registry
        self._packed: dict[RegistryKey, Cwl] = {}

    def tool(self, step: StepNode) -> ToolDefinition:
        """The registry's definition of the tool `step` runs."""
        assert step.run is not None
        key = step.run
        definition = self._registry.tool(key)
        if definition is None:
            raise SophiosError.error(
                SophiosErrorCode.SUBWORKFLOW_INVALID,
                f'process {key.namespace}/{key.name} disappeared after resolution')
        return definition

    def of(self, step: StepNode, tool: ToolDefinition) -> Cwl:
        """The description written for `step`, which runs `tool`.

        Raises:
            SophiosError: `wic013` at the step, with cwltool's message, when cwltool cannot pack the tool.
        """
        if not tools.has_relative_reference(tool.cwl):
            return deepcopy(tool.cwl)
        if tool.key not in self._packed:
            try:
                self._packed[tool.key] = tools.packed(dump_wic_yaml(tool.cwl),
                                                      Path(tool.run_path).absolute().as_uri())
            except tools.PackError as error:
                raise SophiosError.error(
                    SophiosErrorCode.SUBWORKFLOW_INVALID,
                    f'step {step.id.name!r} runs {tool.run_path}, which refers to files relative to itself, '
                    f'and cwltool cannot read them:\n{error}',
                    span=step.span, locator=Locator(step=step.id.name, index=step.id.index)) from error
        return deepcopy(self._packed[tool.key])
