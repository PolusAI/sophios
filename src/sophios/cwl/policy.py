"""What the run policy changes in the CWL a workflow is written as."""
from collections.abc import Sequence
from copy import deepcopy
import hashlib
import json

from ..ir.policy import RunPolicy
from ..lang.nodes import OpaqueCwl
from ..lang.subset import one_expression
from ..lang.types import optional, read_type
from ..wic_types import Cwl

#: A tool's own shorthand for an output that captures one of its streams.
_STREAMS = ('stdout', 'stderr')


def effective_when(authored: OpaqueCwl, needed: Sequence[str], policy: RunPolicy) -> OpaqueCwl:
    """The `when` a step is written with: the null checks of every input it requires, then its own.

    The checks come first, so the author's condition is evaluated only once every
    required input has arrived, and at most once: it is written once, verbatim.
    Without partial failure, or with no input to check, the step keeps the `when`
    its author wrote, or none. A `when` that is neither one `$(...)` nor one
    `${...}` is not a condition cwltool evaluates, and is written as it is.
    """
    if not policy.partial_failure or not needed:
        return authored
    checks = ' && '.join(f'inputs["{name}"] != null' for name in needed)
    if authored is None:
        return f'$({checks})'
    text = authored.strip() if isinstance(authored, str) else ''  # as cwltool reads an expression
    match one_expression(text):
        case '$(':
            return f'$({checks} && ({text[2:-1]}))'
        case '${':
            return f'${{if (!({checks})) {{ return false; }}{text[2:-1]}}}'
    return authored


def tolerant_tool(document: Cwl, policy: RunPolicy) -> Cwl:
    """`document` as a CommandLineTool runs under `policy`.

    Under partial failure its success codes are its own and the policy's, and
    each of its outputs accepts `null`, however its outputs are written: as a
    mapping or as the list a packed tool has, each as its type alone or as a
    mapping. A stream output becomes a `File?` globbing the stream's file: the
    one the tool names, or else one named by the sha1 of the tool as written,
    the scheme cwltool names an unnamed stream by, though not its digest.
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


def _type(entry: OpaqueCwl) -> OpaqueCwl:
    """The type an output declares, written alone or as its mapping's `type`."""
    return entry.get('type') if isinstance(entry, dict) else entry


def _captured(entry: OpaqueCwl) -> str | None:
    """The stream an output captures, if it says so as CWL lets it: without a binding of its own."""
    match _type(entry):
        case str() as declared if declared in _STREAMS and not (isinstance(entry, dict) and 'outputBinding' in entry):
            return declared
    return None


def _tolerant_output(entry: OpaqueCwl, tool: Cwl) -> OpaqueCwl:
    """One output of `tool`, as it accepts `null`. A stream output with a binding of its own is no CWL, and is
    left for cwltool to refuse."""
    declared = _type(entry)
    if (stream := _captured(entry)) is not None:
        fields = entry if isinstance(entry, dict) else {}
        return {**fields, 'type': 'File?', 'outputBinding': {'glob': tool[stream]}}
    if declared is None or declared in _STREAMS or optional(read_type(declared)):
        return entry
    match declared:
        case str():
            tolerant: OpaqueCwl = f'{declared}?'
        case list():
            tolerant = ['null', *declared]
        case _:
            tolerant = ['null', declared]
    return {**entry, 'type': tolerant} if isinstance(entry, dict) else tolerant
