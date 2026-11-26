"""The CWL reader at the front door: a CWL process document becomes the neutral tool node.

Everything a target needs the meaning of is read here, once, by the standard's own loader: the interface at once, and
the body (what runs, what it needs) when a target asks. A field the node holds no value of is named in an `unread`,
for a target that cannot run it to refuse by name; the document itself stays, opaque, in the node's `passthrough`, which
only the CWL target reads. This module and `lang.cwl` are the only ones that read a tool's CWL.
"""
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import replace
import re
from typing import Any, Final

from cwl_utils.parser import cwl_v1_2 as standard

from .cwl import (
    Process, Written, separate_read_as_false, canonical, fields_set, inherited as loaded_inherited, input_reference,
    literal, load_process, ports_named,
)
from .cwl_fields import OUTPUT_EVAL_REFUSALS, invalid_section, refused_class, unread
from .diagnostics import SophiosError
from .needs import FEATURES, Inherited
from .nodes import Value
from .ports import named_entries, port_declaration
from .subset import EXPRESSION
from .tools import (
    Accelerator, BodyKind, Capture, Collect, Computed, Container, Dirent, Generated, Need, NeedKind, Network,
    Other, Part, Placement, Reference, Resources, Staged, Staging, Stream, Text, ToolBody, ToolInput, ToolNode,
    ToolOutput, ToolPort, Variable, Variables, Word,
)
from .unread import UnreadField
from .types import read_type
from .values import read_value

#: The vendor's accelerator class, which no field of the standard says.
CUDA: Final = 'cwltool:CUDARequirement'

_KINDS: Final = {
    'DockerRequirement': NeedKind.CONTAINER, 'EnvVarRequirement': NeedKind.ENVIRONMENT,
    'InitialWorkDirRequirement': NeedKind.STAGING, 'ResourceRequirement': NeedKind.RESOURCES,
    'NetworkAccess': NeedKind.NETWORK, 'ShellCommandRequirement': NeedKind.SHELL,
}
_STREAMS: Final = {stream.value: stream for stream in Stream}


_INPUT_EXPRESSION: Final = re.compile(
    r'\$\(\s*inputs\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<suffix>path|basename))?\s*\)')
#: One token of a string CWL interpolates: an escape it rewrites (a backslash before `$(`, `${` or a backslash), or a
#: reference.
_INTERPOLATION: Final = re.compile(rf'\\(?P<escaped>\$[({{]|\\)|{_INPUT_EXPRESSION.pattern}')
_PARTS: Final = {None: Part.VALUE, 'path': Part.PATH, 'basename': Part.BASENAME}
#: What an output file is made into, by the text that says it.
_COLLECTS: Final = {'$(self[0])': Collect.FIRST, '$(self[0].contents)': Collect.TEXT}


def read_word(text: Value) -> Word | None:
    """The word a CWL string says: literal text, and references to inputs (`$(inputs.x)`, `$(inputs.x.path)` and
    `$(inputs.x.basename)`); None for any other text, which holds no word: one with a NUL byte, which no program
    takes, and one with an expression CWL would evaluate. cwltool interpolates only text that holds an expression,
    stripping it and unescaping what the text escapes."""
    if not isinstance(text, str) or '\x00' in text:
        return None
    written, interpolated = text, EXPRESSION.search(text)
    if interpolated:
        text = text.strip()
    parts: list[str | Reference] = []
    offset = 0
    for match in _INTERPOLATION.finditer(text) if interpolated else ():
        parts.append(text[offset:match.start()])
        parts.append(match['escaped'] or Reference(match['name'], _PARTS[match['suffix']]))
        offset = match.end()
    parts.append(text[offset:])
    # A `$` not followed by `(` or `{` is plain text, as in CWL. A space stands in for each token, so the text on its
    # two sides cannot join into an expression's start: `{$$(inputs.x){$` is `{$`, a reference and `{$`.
    if interpolated and EXPRESSION.search(_INTERPOLATION.sub(' ', text)):
        return None
    return Word(tuple(parts), written)


_SUFFIXES: Final = {Part.VALUE: '', Part.PATH: '.path', Part.BASENAME: '.basename'}


def write_word(word: Word | Computed) -> str:
    """`word` as CWL writes it: literal text escaped as cwltool unescapes it where the text holds an expression or
    the word a reference, a reference as `$(inputs.name)` and its suffix; a `Computed` as it was written."""
    if isinstance(word, Computed):
        return word.text
    text = ''.join(part for part in word.parts if isinstance(part, str))
    escaped = bool(EXPRESSION.search(text)) or word.text is None
    return ''.join((literal(part) if escaped else part) if isinstance(part, str)
                   else input_reference(part.name, _SUFFIXES[part.part]) or '' for part in word.parts)


def _references(text: str) -> tuple[Reference, ...]:
    """The inputs `text` refers to, wherever in it, in the order it does."""
    return tuple(Reference(found['name'], _PARTS[found['suffix']])
                 for found in _INTERPOLATION.finditer(text) if found['name'])


def read_text(text: Value) -> Text:
    """The word `text` says, else the text as written: an expression a target would have to evaluate, or text no
    target can take as it is."""
    return read_word(text) or Computed(str(text), bool(isinstance(text, str) and EXPRESSION.search(text)),
                                       _references(str(text)))


def _amount(value: Any) -> Any:
    """A number a requirement asks for as it is written; text is not a number, and is held as it was written."""
    return Computed(value, bool(EXPRESSION.search(value)), _references(value)) if isinstance(value, str) else value


def _position(value: Any) -> Any:
    """A command line position as written: a whole number, the text of one as that number, or any other text as
    written."""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return Computed(value, bool(EXPRESSION.search(value)), _references(value))
    return value


def _unread(value: Any, owner: str) -> tuple[UnreadField, ...]:
    """The fields `value` writes that earn a refusal, those of a CWL `owner` that no node of Sophios holds."""
    return unread(owner, fields_set(value))


def _placement(binding: Any) -> Placement:
    """A binding, a `CommandLineBinding` or the `InputBinding` of a process that is no tool, which writes only its
    `loadContents`."""
    written = getattr(binding, 'valueFrom', None)
    return Placement(
        _position(getattr(binding, 'position', None)), getattr(binding, 'prefix', None),
        getattr(binding, 'separate', None), getattr(binding, 'itemSeparator', None),
        getattr(binding, 'shellQuote', None), None if written is None else read_text(written),
        _unread(binding, 'CommandLineBinding'))


def _members(written: Any) -> list[Any]:
    """The members of a union, or the one type."""
    return written if isinstance(written, list) else [written]


def _input(name: str, port: Any) -> ToolInput:
    binding = getattr(port, 'inputBinding', None)
    return ToolInput(
        name, read_type(canonical(port.type_)), read_value(port.default),
        None if binding is None else _placement(binding),
        _unread(port, 'CommandInputParameter'),
        tuple(_unread(member, 'CommandInputArraySchema') for member in _members(port.type_)
              if isinstance(member, (standard.CommandInputArraySchema, standard.CommandOutputArraySchema))),
        'stdin' in _members(canonical(port.type_)))


def _output(name: str, port: Any) -> ToolOutput:
    written = canonical(port.type_)
    stream = _STREAMS.get(written) if isinstance(written, str) else None
    binding = getattr(port, 'outputBinding', None)
    capture = None
    if binding is not None or stream is not None:
        glob, collect = getattr(binding, 'glob', None), getattr(binding, 'outputEval', None)
        text = read_text(glob) if isinstance(glob, str) and glob else None
        collected = _COLLECTS.get(collect.strip()) if isinstance(collect, str) else None
        capture = Capture(stream, text, getattr(binding, 'loadContents', None), collected,
                          () if binding is None else _unread(binding, 'CommandOutputBinding'),
                          glob if text is None else None, collect if collected is None else None,
                          OUTPUT_EVAL_REFUSALS.get(collect.strip()) if isinstance(collect, str) else None)
    return ToolOutput(name, read_type(written), capture, _unread(port, 'CommandOutputParameter'))


def _staged(item: Any) -> Staged:
    match item:
        case str():
            return read_text(item)
        case standard.Dirent():
            return Dirent(read_text(item.entry), None if item.entryname is None else read_text(item.entryname),
                          item.writable, _unread(item, 'Dirent'))
        case standard.File() if (isinstance(item.basename, str) and isinstance(item.contents, str)
                                 and set(fields_set(item)) <= {'class', 'basename', 'contents'}):
            return Generated(item.basename, item.contents)
    return Other()


def _body_of(each: Any) -> Any:
    """What a requirement of the standard holds, by its class."""
    match each:
        case standard.DockerRequirement():
            return Container(each.dockerPull, each.dockerImageId)
        case standard.EnvVarRequirement():
            return Variables(tuple(
                Variable(entry.envName, read_text(entry.envValue), _unread(entry, 'EnvironmentDef'))
                for entry in each.envDef))
        case standard.InitialWorkDirRequirement():
            return Staging(tuple(map(_staged, each.listing)) if isinstance(each.listing, list) else None)
        case standard.ResourceRequirement():
            return Resources(*map(_amount, (each.coresMin, each.coresMax, each.ramMin, each.ramMax)))
        case standard.NetworkAccess():
            return Network(each.networkAccess)

    return None


def _need(place: str, hint: bool, each: Any) -> Need:
    """The need one requirement or hint of the loader is, `place` being where it is written."""
    if not isinstance(each, standard.ProcessRequirement):
        return Need(place, str(each.get('class') if isinstance(each, dict) else each), hint, NeedKind.OTHER, False)
    name = type(each).__name__
    return Need(place, name, hint, _KINDS.get(name, NeedKind.OTHER), True, _unread(each, name), _body_of(each),
                refused_class(name))


def _accelerator(fields: Mapping[str, Any]) -> Accelerator:
    """The accelerator the vendor's fields say."""
    return Accelerator(_amount(fields.get('cudaDeviceCountMin')), _amount(fields.get('cudaDeviceCountMax')),
                       fields.get('cudaVersionMin'), fields.get('cudaComputeCapability'),
                       tuple((name, fields[name]) for name in ('cudaVersionMin', 'cudaComputeCapability')
                             if name in fields))


def _vendor(place: str, hint: bool, name: str, fields: Mapping[str, Any]) -> Need:
    """The need a vendor's requirement or hint is: no class of the standard holds it."""
    return Need(place, name, hint, NeedKind.ACCELERATOR if name == CUDA else NeedKind.OTHER, True,
                unread(name, fields), _accelerator(fields) if name == CUDA else None, refused_class(name))


def _needs(process: Process, written: Written) -> tuple[Need, ...]:
    """Each requirement and hint of a process in the order written, requirements first: the loader's, with the
    vendor's among them where they were written."""
    found: list[Need] = []
    for section in ('requirements', 'hints'):
        typed = iter(getattr(process, section) or ())
        for place, extension in written[section].items():
            if extension:  # the vendor's: no class of the standard holds it
                found.append(_vendor(place, section == 'hints', *extension))
            else:
                found.append(_need(place, section == 'hints', next(typed)))
    return tuple(found)


def command(process: Process, written: Written) -> ToolBody:
    """The body a loaded process says. The ports it declares are its own, and a tool written in `.wic` that carries
    its CWL beside its lines loads it as one of no ports, which its lines then give."""
    inputs = tuple(_input(name, port) for name, port in ports_named(process.inputs).items())
    needs = _needs(process, written)
    if not isinstance(process, standard.CommandLineTool):
        return ToolBody(BodyKind.OTHER, process.class_, (), None, (), None, None, None, inputs, (), needs)
    outputs = tuple(_output(name, port) for name, port in ports_named(process.outputs).items())
    stdin, stdout, stderr = (None if stream is None else read_text(stream)
                             for stream in (process.stdin, process.stdout, process.stderr))
    base = tuple(process.baseCommand) if isinstance(process.baseCommand, list) else process.baseCommand
    arguments = tuple(_placement(argument) if isinstance(argument, standard.CommandLineBinding) else read_text(argument)
                      for argument in process.arguments or ())
    return ToolBody(BodyKind.COMMAND, process.class_, _unread(process, 'CommandLineTool'), base, arguments,
                    stdin, stdout, stderr, inputs, outputs, needs)


def laid(read: Placement | None, slot: Placement) -> Placement:
    """The binding the CWL beside the lines writes, with the place a line gives over it: the `position`, and what else
    the line says of it (a `prefix`, `separate`, an `item_separator`). As for any binding, a field the node holds but
    no target lowers (`itemSeparator`) is named `unread`."""
    given = {name: value for name in ('position', 'prefix', 'separate', 'item_separator')
             if (value := getattr(slot, name)) is not None}
    placement = replace(read or Placement(), **given)
    if slot.item_separator is not None and not any(item.name == 'itemSeparator' for item in placement.unread):
        placement = replace(placement, unread=(*placement.unread, *unread('CommandLineBinding', ('itemSeparator',))))
    return placement


def read_input(process: Process, name: str, fields: Mapping[str, Any]) -> ToolInput:
    """The input `name` that writes `fields`, as the loader of `process` reads it."""
    binding = fields.get('inputBinding')
    written = {**fields, 'inputBinding': separate_read_as_false(binding)} if binding is not None else dict(fields)
    return _input(name, standard.CommandInputParameter.fromDoc(
        {'id': name, **written}, str(process.id), process.loadingOptions))


def read_output(process: Process, name: str, fields: Mapping[str, Any]) -> ToolOutput:
    """The output `name` that writes `fields`, as the loader of `process` reads it."""
    return _output(name, standard.CommandOutputParameter.fromDoc(
        {'id': name, **fields}, str(process.id), process.loadingOptions))


def read_inherited(name: str, written: Value, hint: bool) -> tuple[Need | None, Exception | None]:
    """The need a step's or a workflow's requirement or hint `name` is, read once where it is written; a body the
    standard's loader refuses is held as its refusal, with the loader's message, for a tool that inherits it to raise.
    """
    fields = written if isinstance(written, dict) else {}
    if ':' in name:
        return _vendor(name, hint, name, fields), None
    try:
        return _need(name, hint, loaded_inherited(name, fields)), None
    except SophiosError as refusal:
        return None, refusal


def split(passthrough: 'tuple[tuple[str, Value], ...]', owner: str
          ) -> tuple[tuple[Inherited, ...], tuple[UnreadField, ...]]:
    """The requirements then the hints a step or a workflow (`owner`) writes as a mapping, in the order written; and
    the refusal of every other key, a `requirements` or `hints` of another shape among them."""
    found: list[Inherited] = []
    other: list[str] = []
    for key, value in passthrough:
        if key in ('requirements', 'hints') and isinstance(value, dict):
            found.extend(Inherited(str(name), body, key == 'hints', *read_inherited(str(name), body, key == 'hints'),
                                   unread=unread(str(name), body) if str(name) in FEATURES and isinstance(body, dict)
                                   else ()) for name, body in value.items())
        else:
            other.append(key)
    return (tuple(sorted(found, key=lambda each: each.hint)),
            (*unread(owner, other), *(invalid_section(owner, section) for section in ('requirements', 'hints')
                                      if section in other)))


def beside(keys: Iterable[tuple[str, Value]], run_path: str) -> tuple[Process, Written]:
    """The CWL a `.wic` tool carries beside its lines (its requirements, hints, arguments, streams and the rest of what
    CommandLineTool says), read as a process of no ports, which its lines give.

    Raises:
        SophiosError: `api004` with the loader's own message, for a key the standard's loader refuses.
    """
    head: dict[str, Value] = {'class': standard.CommandLineTool.__name__, 'inputs': [], 'outputs': []}
    return load_process(head | {key: value for key, value in keys if key not in head and value is not None},
                        run_path)


def tool_port(name: str, declaration: Value, *, output: bool) -> ToolPort:
    """The port `name` that writes `declaration`, a type or CWL's port of fields."""
    return ToolPort(name, port_declaration(declaration, output=output))


def ports(raw: Value, *, output: bool) -> tuple[ToolPort, ...]:
    """The ports a process declares under its `inputs:` or its `outputs:`, in CWL's map or list form."""
    return tuple(tool_port(name, declaration, output=output) for name, declaration in named_entries(raw))


def namespaces(raw: Value) -> tuple[tuple[str, Value], ...]:
    """The prefixes a process declares in its `$namespaces`."""
    return tuple((str(prefix), uri) for prefix, uri in raw.items()) if isinstance(raw, dict) else ()


def held(body: Callable[[], ToolBody]) -> tuple[ToolBody | None, Exception | None]:
    """The body `body` reads, or what stopped the read, the standard's loader's refusal (`api004`) among it: a compile
    that never needs the body is not stopped by it, as it was not when the body was read on asking."""
    try:
        return body(), None
    except Exception as refusal:  # pylint: disable=broad-exception-caught
        return None, refusal


def node(cwl: dict[str, Any], run_path: str, load: Callable[[], tuple[Process, Written]]) -> ToolNode:
    """The tool the CWL process `cwl` says, whose body is read through `load` (the standard's loader, once, shared by
    every reader of the tool) and a body the standard's loader refuses held as its refusal, with the loader's message,
    for a target that needs the body to raise; held as it was read, for the CWL target to write back."""
    return ToolNode(
        run_path, ports(cwl.get('inputs', {}), output=False), ports(cwl.get('outputs', {}), output=True),
        cwl.get('class') == 'Workflow', namespaces(cwl.get('$namespaces')), None,
        tuple((key, deepcopy(value)) for key, value in cwl.items()), *held(lambda: command(*load())))
