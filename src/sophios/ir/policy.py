"""How a compiled workflow runs: the run policy, and the CWL it is lowered to.

Partial failure is compile input. A job that exits with one of the policy's
success codes counts as a success, and leaves `null` where it wrote nothing; a
step runs only once every input it requires has arrived. Emit lowers the policy
once per document, a step's condition through `effective_when` and a tool through
`tolerant_tool`, and nothing rewrites the CWL afterwards.
"""
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cwl_utils.errors import SubstitutionError
from cwl_utils.expression import scanner

from ..lang.nodes import OpaqueCwl
from ..wic_types import Cwl
from .declarations import required
from .names import Names
from .types import PortType, StepNode

#: A tool's own shorthand for an output that captures one of its streams.
_STREAMS = ('stdout', 'stderr')


@dataclass(frozen=True, slots=True)
class RunPolicy:
    """Whether a job may fail partially, and the exit codes that then count as success."""

    partial_failure: bool = False
    #: Sorted, without repeats, 0 always among them.
    success_codes: tuple[int, ...] = (0, 1)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'success_codes', tuple(sorted({0, *self.success_codes})))


def effective_when(step: StepNode, names: Names, policy: RunPolicy) -> OpaqueCwl:
    """The `when` `step` is written with: the null checks of every input it requires, then its own.

    The checks come first, so the author's condition is evaluated only once every
    required input has arrived, and at most once: it is written once, verbatim.
    Without partial failure, or with no input to check, the step keeps the `when`
    its author wrote, or none. A `when` that is neither one `$(...)` nor one
    `${...}` is not a condition cwltool evaluates, and is written as it is.
    """
    authored = dict(step.interpreted).get('when')
    needed = [names.port(port.id.port) for port in step.inputs if required(port.declaration)]
    if not policy.partial_failure or not needed:
        return authored
    checks = ' && '.join(f'inputs["{name}"] != null' for name in needed)
    if authored is None:
        return f'$({checks})'
    text = authored.strip() if isinstance(authored, str) else ''  # as cwltool reads an expression
    if _one_expression(text, '$('):
        return f'$({checks} && ({text[2:-1]}))'
    if _one_expression(text, '${'):
        return f'${{if (!({checks})) {{ return false; }}{text[2:-1]}}}'
    return authored


def _one_expression(text: str, opening: str) -> bool:
    """Whether `text` is one CWL expression, opened by `opening`, from end to end, as cwltool scans it."""
    try:
        return text.startswith(opening) and scanner(text) == (0, len(text))
    except SubstitutionError:
        return False


def tolerant_tool(document: Cwl, policy: RunPolicy) -> Cwl:
    """`document` as a CommandLineTool runs under `policy`.

    Under partial failure its success codes are its own and the policy's, and
    each of its outputs accepts `null`, however its outputs are written: as a
    mapping or as the list a packed tool has, each as its type alone or as a
    mapping. A stream output becomes a `File?` globbing the stream's file: the
    one the tool names, or else one named by the sha1 of the tool as written,
    the scheme cwltool names an unnamed stream by, though not its digest.
    Without partial failure, or for any other class, the document is as it was.
    """
    if not policy.partial_failure or document.get('class') != 'CommandLineTool':
        return document
    tool = deepcopy(document)
    outputs = tool.get('outputs', {})
    entries = outputs if isinstance(outputs, list) else list(outputs.values())
    for stream in _STREAMS:
        if stream not in tool and any(_captured(entry) == stream for entry in entries):
            tool[stream] = hashlib.sha1(json.dumps(tool, sort_keys=True, default=str).encode('utf-8'),
                                        usedforsecurity=False).hexdigest()
    if isinstance(outputs, list):
        tool['outputs'] = [_tolerant_output(entry, tool) for entry in outputs]
    elif 'outputs' in tool:
        tool['outputs'] = {name: _tolerant_output(entry, tool) for name, entry in outputs.items()}
    own = tool.get('successCodes')
    tool['successCodes'] = sorted({*(own if isinstance(own, list) else []), *policy.success_codes})
    return tool


def _type(entry: Any) -> Any:
    """The type an output declares, written alone or as its mapping's `type`."""
    return entry.get('type') if isinstance(entry, dict) else entry


def _captured(entry: Any) -> Any:
    """The stream an output captures, if it says so as CWL lets it: without a binding of its own."""
    declared = _type(entry)
    return declared if declared in _STREAMS and not (isinstance(entry, dict) and 'outputBinding' in entry) else None


def _tolerant_output(entry: Any, tool: Cwl) -> Any:
    """One output of `tool`, as it accepts `null`. A stream output with a binding of its own is no CWL, and is
    left for cwltool to refuse."""
    declared = _type(entry)
    if (stream := _captured(entry)) is not None:
        fields = entry if isinstance(entry, dict) else {}
        return {**fields, 'type': 'File?', 'outputBinding': {'glob': tool[stream]}}
    if declared is None or declared in _STREAMS or PortType(declared).optional:
        return entry
    match declared:
        case str():
            tolerant: Any = f'{declared}?'
        case list():
            tolerant = ['null', *declared]
        case _:
            tolerant = ['null', declared]
    return {**entry, 'type': tolerant} if isinstance(entry, dict) else tolerant
