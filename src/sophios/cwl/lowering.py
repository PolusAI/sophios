"""The CWL a tool is written as: the CWL target's spelling of a tool node.

A tool read from a `.cwl` is written back as it was read. A tool written in `.wic` is spelled from what its lines mean
(`lang.wic_tool.Plan`) beside the CWL its document carries: each line is written out as the CWL it needs in order to
mean what it says, and never changes CWL that is there. What a line writes is built with the standard's own classes,
so a field of CWL is a keyword Python checks.
"""
from copy import deepcopy
from typing import Any, Final

from cwl_utils.parser import cwl_v1_2 as _standard

from ..lang.cwl import CWL_VERSION, require
from ..lang.cwl_tool import write_word
from ..lang.nodes import ToolDocument
from ..lang.tools import Placement, ToolNode
from ..lang.wic_tool import SCRIPT_FILE, Line, read

_JAVASCRIPT: Final = _standard.InlineJavascriptRequirement.__name__
_STAGED: Final = _standard.InitialWorkDirRequirement.__name__


def process_of(tool: ToolNode) -> dict[str, Any]:
    """The CWL process a tool is written as: the document it was read from, or the spelling of the `.wic` it says."""
    if tool.document is not None:
        return command_line_tool(tool.document)
    return {key: deepcopy(value) for key, value in tool.passthrough}


def command_line_tool(tool: ToolDocument) -> dict[str, Any]:
    """The CWL CommandLineTool `tool` means.

    Raises:
        SophiosError: `wic058`, for every line the tool cannot mean.
    """
    plan = read(tool)
    head = {'class': _standard.CommandLineTool.__name__, 'cwlVersion': CWL_VERSION}
    # A key written with no value says nothing, as CWL reads it: it is no place already written.
    cwl: dict[str, Any] = head | {key: deepcopy(value) for key, value in tool.passthrough
                                  if key not in head and value is not None}
    ports: dict[str, dict[str, Any]] = {port.name: deepcopy(dict(port.fields)) for port in tool.inputs}
    for name, slot in plan.placed:
        ports[name]['inputBinding'] = (ports[name].get('inputBinding') or {}) | _binding(slot)
    if tool.docker is not None:
        _require(cwl, 'hints', _standard.DockerRequirement(dockerPull=tool.docker))
    if plan.resources is not None:
        _require(cwl, 'requirements', _standard.ResourceRequirement(
            coresMin=plan.resources.cores_min, ramMin=plan.resources.ram_min))
    if plan.script is not None:
        entry = require(cwl, 'requirements', _STAGED)
        assert entry is not None  # a tool the lines can mean has a listing
        entry.setdefault('listing', []).append(_standard.File(basename=SCRIPT_FILE, contents=plan.script.text).save())
    if plan.base:
        cwl['baseCommand'] = plan.base[0] if len(plan.base) == 1 else list(plan.base)
    if plan.arguments:
        cwl['arguments'] = [_binding(argument) for argument in plan.arguments] + cwl.get('arguments', [])
    cwl['inputs'] = ports
    cwl['outputs'] = {port.name: _output(port.fields, line) for port, line in zip(tool.outputs, plan.outputs)}
    if plan.javascript:
        require(cwl, 'requirements', _JAVASCRIPT)
    return cwl


def _require(cwl: dict[str, Any], section: str, added: _standard.Saveable) -> None:
    """Add the requirement `added` to the `section` of `cwl`, which writes none of its class."""
    fields: dict[str, Any] = added.save()
    entry = require(cwl, section, fields.pop('class'))
    assert entry is not None  # a tool the lines can mean has a section that holds one
    entry |= fields


def _binding(placement: Placement) -> dict[str, Any]:
    """A command line binding as CWL writes it: the fields the placement holds."""
    return _standard.CommandLineBinding(
        position=placement.position, prefix=placement.prefix, separate=placement.separate,
        itemSeparator=placement.item_separator,
        valueFrom=None if placement.value_from is None else write_word(placement.value_from)).save()


def _output(fields: tuple[tuple[str, Any], ...], line: Line | None) -> dict[str, Any]:
    """The output a port says by its line, as CWL writes it: `stdout` or `stderr`, or the files a pattern matches;
    what the port says beside the line is kept."""
    written = deepcopy(dict(fields))
    if line is None:
        return written
    if line.stream is not None:
        return {'type': line.stream.value} | written
    return {'type': 'Directory' if line.directory else 'File',
            'outputBinding': _standard.CommandOutputBinding(
                glob=None if line.glob is None else write_word(line.glob)).save()} | written
