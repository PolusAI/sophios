"""Real-time analysis declarations.

A step that runs the `cwl_subinterpreter` adapter is not a CWL step: it declares
an analysis that Sophios runs on the host, beside the workflow, each time the
files it watches change and once the workflow ends. It is taken out of the graph
before Link and Infer see it, so it is never emitted and inference never wires
it, and what it declares is checked here, at compile time.
"""
from dataclasses import dataclass, replace
from typing import Any, Final

from ..lang.diagnostics import Diagnostics, Locator, SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..lang.nodes import InlineLiteral, OpaqueCwl
from ..lang.spans import SourceSpan
from ..utils_yaml import Key
from .types import StepNode, WorkflowGraph

#: The registry name of the adapter whose steps are declarations.
ADAPTER: Final = 'cwl_subinterpreter'

#: Seconds between two runs triggered by a watched file changing, unless the declaration says otherwise.
DEFAULT_INTERVAL: Final = 60

_REQUIRED: Final = ('file_pattern', 'cwl_tool', 'max_times', 'config')


@dataclass(frozen=True, slots=True)
class Declaration:
    """One real-time analysis, as its declaration step wrote it.

    `config` is plain data: the `!ii` a value carried inside it is unwrapped,
    because everything in it is a literal.
    """

    #: The name of the workflow the declaration step sits in.
    document: str
    #: The tool or `.wic` to run, as `cwl_tool` names it.
    analysis: str
    file_pattern: str
    max_times: int
    interval: int
    #: A `.wic` analysis's `wic: steps:` mapping, or a tool step's own keys (`{in: ...}`).
    config: dict[str, Any]
    span: SourceSpan | None = None


def extract(graph: WorkflowGraph) -> tuple[WorkflowGraph, tuple[Declaration, ...]]:
    """`graph` without its declaration steps, at every depth, and what they declare.

    Raises:
        SophiosError: `wic044` for each declaration that is not well formed.
    """
    diagnostics = Diagnostics()
    graph, found = _extract(graph, diagnostics)
    if diagnostics.has_errors:
        raise SophiosError(diagnostics)
    return graph, found


def is_declaration(step: StepNode) -> bool:
    """Whether `step` runs the real-time analysis adapter, by its resolved identity."""
    return step.run is not None and step.run.process_id.name == ADAPTER


def _extract(graph: WorkflowGraph, diagnostics: Diagnostics) -> tuple[WorkflowGraph, tuple[Declaration, ...]]:
    found: list[Declaration] = []
    steps = []
    for step in graph.steps:
        if not is_declaration(step):
            steps.append(step)
            continue
        declaration = _declaration(graph.name, step, diagnostics)
        if declaration is not None:
            found.append(declaration)
    children = []
    for child in graph.children:
        extracted, nested = _extract(child, diagnostics)
        children.append(extracted)
        found.extend(nested)
    if len(steps) == len(graph.steps) and not found:
        return graph, ()
    return replace(graph, steps=tuple(steps), children=tuple(children)), tuple(found)


def _declaration(document: str, step: StepNode, diagnostics: Diagnostics) -> Declaration | None:
    """What `step` declares, or None after recording why it cannot be read."""
    before = len(diagnostics)

    def problem(port: str, message: str) -> None:
        diagnostics.error(SophiosErrorCode.REALTIME_DECLARATION,
                          f'real-time analysis step {step.id.name!r}: {message}', step.span,
                          Locator(step=step.id.name, index=step.id.index, port=port))

    values: dict[str, Any] = {}
    for binding in step.bindings:
        port = str(binding.sink.port)
        if isinstance(binding.value, InlineLiteral):
            values[port] = _plain(binding.value.value)
        else:
            problem(port, f'{port} must be a literal, written with !ii: Sophios reads it when it compiles.')
    bound = {str(binding.sink.port) for binding in step.bindings}
    for port in _REQUIRED:
        if port not in bound:
            problem(port, f'{port} is required.')
    if len(diagnostics) > before:
        return None

    for port in ('file_pattern', 'cwl_tool'):
        if not isinstance(values[port], str) or not values[port]:
            problem(port, f'{port} must be a non-empty string, not {values[port]!r}.')
    max_times = _positive_int(values['max_times'])
    if max_times is None:
        problem('max_times', f"max_times must be a positive integer such as '20', not {values['max_times']!r}.")
    interval = _positive_int(values.get('interval', DEFAULT_INTERVAL))
    if interval is None:
        problem('interval', f"interval must be a positive whole number of seconds, not {values['interval']!r}.")
    if not isinstance(values['config'], dict):
        problem('config', "config must be a mapping: the analysis's `wic: steps:` entries for a .wic, "
                f"or the step's own keys such as `in:` for a tool; not {values['config']!r}.")
    if len(diagnostics) > before or max_times is None or interval is None:
        return None
    return Declaration(document, values['cwl_tool'], values['file_pattern'], max_times, interval,
                       values['config'], step.span)


def _plain(value: OpaqueCwl) -> Any:
    """`value` with every `!ii` inside it, tagged or desugared, replaced by the value it carries."""
    match value:
        case InlineLiteral(value=inner):
            return _plain(inner)
        case dict() if set(value) == {Key.INLINE_INPUT}:
            return _plain(value[Key.INLINE_INPUT])
        case dict():
            return {key: _plain(item) for key, item in value.items()}
        case list():
            return [_plain(item) for item in value]
    return value


def _positive_int(value: Any) -> int | None:
    """`value` as a positive int, accepting the digit strings the corpus writes; None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None
