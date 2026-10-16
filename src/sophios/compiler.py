"""The typed compiler boundary over the phase pipeline."""
import re
import sys
from copy import deepcopy
from typing import Final

from .ir.complete import complete
from .ir.artifacts import CompilationArtifact, CompilationResult
from .ir.emit import emit, emit_job_inputs, surface
from .ir.infer import InferencePolicy, InsertionCatalog, infer
from .ir.link import link
from .ir.namespaces import declare_namespaces
from .ir.frontdoor import SourceBundle
from .ir.pipeline import front_end
from .ir.realtime import extract
from .ir.resolve import RegistrySnapshot
from .ir.names import Names
from .ir.types import WorkflowGraph
from .lang import versions
from .lang.diagnostics import Locator, SophiosError
from .lang.nodes import CwlRecord, UnresolvedName
from .lang.parser import Grammar
from .lang.error_codes import SophiosErrorCode
from .wic_types import CompilerOptions


# pylint: disable-next=too-many-locals
def compile_source(bundle: SourceBundle,
                   compiler_options: CompilerOptions,
                   *,
                   testing: bool) -> CompilationResult:
    """Compile a parsed root workflow and every workflow it reaches.

    The one door. A bundle read from files carries the spans of the text its
    author wrote, so a diagnostic names a position the reader can open; one
    the Python API built carries the span of the Python line that made each node.
    """
    if not testing:
        print(' starting compilation of', bundle.name)
    selected_version = versions.resolve(
        compiler_options.get('lang_version'), bundle.lang_version_pins)
    front = front_end(bundle.parsed, bundle.registry, name=bundle.name,
                      lang_version=selected_version)
    if front.graph is None or front.resolved is None or front.resolved.document is None:
        raise SophiosError(front.diagnostics)
    if not front.graph.steps:
        raise SophiosError.error(SophiosErrorCode.SUBWORKFLOW_INVALID,
                                 'workflows must define at least one step')
    authored, realtime = extract(front.graph)
    _check_unresolved_names(authored, compiler_options['allow_raw_cwl'])
    for note in dict.fromkeys(_authored_spelling_notes(authored)):
        print(note, file=sys.stderr)

    prepared = complete(authored)
    linked = link(prepared)
    if linked.graph is None:
        raise SophiosError(linked.diagnostics)
    policy = InferencePolicy(
        disabled=compiler_options['inference_disable'],
        use_naming_conventions=compiler_options['inference_use_naming_conventions'],
        renaming_conventions=tuple(compiler_options.get('renaming_conventions', ())),
        insert_steps_automatically=compiler_options['insert_steps_automatically'],
        format_rules=tuple(compiler_options.get('inference_rules', {}).items()),
        strict=compiler_options['inference_strict'],
    )
    inferred = infer(linked.graph, policy, InsertionCatalog.from_registry(bundle.registry))
    if inferred.graph is None:
        raise SophiosError(inferred.diagnostics)
    graph = declare_namespaces(complete(inferred.graph), bundle.registry)
    artifact = _artifact_tree(graph, graph.name, Names.of(graph), bundle.registry,
                              partial_failure=compiler_options['partial_failure_enable'])
    if not testing:
        print('finishing compilation of', bundle.name)
    return CompilationResult(graph, artifact, inferred.diagnostics, realtime)


#: `inputs.name` and `inputs["name"]` inside a `when:` expression.
_EXPRESSION_INPUT: Final = re.compile(r'inputs(?:\.([A-Za-z_]\w*)|\[\s*["\']([^"\']+)["\']\s*\])')


def _expression_inputs(expression: object) -> tuple[str, ...]:
    """Every step input a CWL expression reads, in order of first mention."""
    if not isinstance(expression, str):
        return ()
    return tuple(dict.fromkeys(a or b for a, b in _EXPRESSION_INPUT.findall(expression)))


def _authored_spelling_notes(graph: WorkflowGraph) -> list[str]:
    """One plain line for each place a document addresses a step or an input ambiguously.

    Such a spelling still compiles, so nothing here fails the compile; each line says what
    to write instead. Two places are checked: an `outputSource` that names a step by an id
    more than one step has (it means the first), and a `when:` that reads an input its step
    does not declare (CWL evaluates it as null).
    A document the Python API built names the script that built it.
    """
    notes: list[str] = []
    spans = [step.span for step in graph.steps if step.span is not None]
    if not spans:
        return [note for child in graph.children for note in _authored_spelling_notes(child)]
    file = spans[0].file
    sources = {port.name: port.output_source for port in graph.workflow_outputs}
    ids = [step.id.name for step in graph.steps]
    for name, source in graph.output_mapping:
        written = str(sources[name]).rsplit('/', 1)[0]
        if Grammar.WIC_STEP_KEY.match(written) or ids.count(source.step.name) == 1:
            continue
        position = [step.id for step in graph.steps].index(source.step) + 1
        notes.append(f"Warning! {file}: output {str(name)!r} has outputSource '{sources[name]}', but "
                     f'{ids.count(source.step.name)} steps have the id {written!r} and it means the first. '
                     f"Write '({position}, {source.step.name})/{source.port}' to say so.")
    for step in graph.steps:
        declared = {str(port.id.port) for port in step.inputs}
        for read in _expression_inputs(dict(step.interpreted).get('when')):
            if read not in declared:
                notes.append(f"Warning! {file}: step {step.id.name!r} reads inputs.{read} in `when:`, which "
                             'its process does not declare; CWL evaluates it as null.')
    for child in graph.children:
        notes.extend(_authored_spelling_notes(child))
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
                    'If you want to compile the workflow anyway, use --allow_raw_cwl',
                    span=getattr(value, 'span', None),
                    locator=Locator(step=step.id.name, index=step.id.index, port=names.port(binding.sink.port)))
    for child in graph.children:
        _check_unresolved_names(child, allow_raw_cwl, names)


def _artifact_tree(graph: WorkflowGraph, name: str, names: Names, registry: RegistrySnapshot, *,
                   partial_failure: bool) -> CompilationArtifact:
    """One artifact per emitted document, each surfaced exactly once, a child
    named as the `run:` its parent emits, so the file written is the one it runs."""
    document = surface(graph, names, partial_failure=partial_failure)
    children: list[CompilationArtifact] = []
    for step in document.steps:
        assert step.run is not None
        child = step.run.child
        if child is not None:
            children.append(_artifact_tree(child, names.file(step.id), names, registry,
                                           partial_failure=partial_failure))
            continue
        key = step.run.process_id
        definition = registry.tool(key)
        if definition is None:
            raise SophiosError.error(
                SophiosErrorCode.SUBWORKFLOW_INVALID,
                f'process {key.namespace}/{key.name} disappeared after resolution')
        # Named by the `run:` the parent emits, so the file written is the one it runs.
        children.append(CompilationArtifact(
            (names.step(step.id),), names.file(step.id),
            definition.run_path, deepcopy(definition.cwl), {}, None,
        ))

    return CompilationArtifact(
        tuple(names.step(part) for part in graph.namespace.parts),
        name,
        f'{name}.cwl',
        emit(document, names),
        emit_job_inputs(document, names),
        graph,
        tuple(children),
    )
