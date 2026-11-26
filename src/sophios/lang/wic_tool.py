"""The .wic reader at the front door: a tool written in `.wic` becomes the neutral tool node.

A tool's lines (`command:`, `script:`, `docker:`, `needs:`, its port and output lines) are read here, into a `Plan`
of what each means: which words are the program and which are arguments, where an input lands on the command line,
the file a script is, what a tool asks of the machine, and what an output line collects. The node's body is that plan
laid over the CWL the document carries beside its lines, which the CWL reader reads (`lang.cwl_tool`): a line's meaning
is held here, and a CWL field's there. Nothing is made of CWL on the way, and the CWL target spells the same plan
when it writes the tool.

A line is added to what the author wrote and never changes it: a place a line writes that the author wrote too is
refused (`wic058`), where the parser reads the tool. `OWNED` is the table of those places, and one walk over what the
author wrote checks every one.
"""
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from functools import cache
import posixpath
import re
from typing import Any, Final

from . import cwl_tool
from .cwl import ATOMIC_TYPES, SIZE, Process, Written, canonical_type, declared, input_reference
from .diagnostics import Diagnostics, SophiosError
from .error_codes import SophiosErrorCode
from .nodes import Port, ToolDocument, Value, Word
from .spans import SourceSpan
from .subset import EXPRESSION, strings
from .tools import (
    Capture, Container, Generated, Need, NeedKind, Part, Placement, Reference, Resources, Staging, Stream, ToolBody,
    ToolInput, ToolNode, ToolOutput,
)
from .tools import Word as Phrase
from .types import Atom, required, spell

#: The file a `script:` is written to in the working directory: no glob matches a dot file.
SCRIPT_FILE: Final = '.script.sh'
_WRITTEN: Final = 'that place is already written, and a line never changes CWL that is there'
_HEAD: Final = ('class', 'cwlVersion')
_STAGED: Final = 'InitialWorkDirRequirement'
_JAVASCRIPT: Final = 'InlineJavascriptRequirement'


@dataclass(frozen=True, slots=True)
class Place:
    """A place of the CWL that a line writes, so the author may not: the key `key`, at any depth under the key `under`
    of the tool (`under` itself, when they are one), holding anything, or the name `holds` (as a path, `./x` is `x`).
    `says` is the refusal, from what the line writes to what is written beside it."""

    under: str
    key: str
    says: str
    holds: str | None = None


#: What each defining line writes of the CWL, which is then not the author's to write. `command:` writes the
#: `baseCommand` and, for an input it places, a `position` (and a `prefix` for its flag), which only the words that
#: place an input can say. `script:` writes the whole command line, `env name=value ... bash .script.sh`, so every
#: place that would put a word on it is its own, at any depth: `arguments`, `baseCommand`, an `inputBinding` of an
#: input, its type, an array's items or a record's field, a record (no variable holds one), and the file it writes.
OWNED: Final = {
    'command': (
        Place('baseCommand', 'baseCommand', 'writes the `baseCommand` of this tool, and one is written beside it'),),
    'script': (
        *(Place(key, key, f'writes the whole command line of this tool, and `{key}:` is written beside it')
          for key in ('baseCommand', 'arguments')),
        Place('inputs', 'inputBinding', 'binds each input to the variable of its name, and an `inputBinding:` is '
              'written in an input, or in its type, items or fields'),
        Place('inputs', 'type', 'passes each input as the variable of its name, and a record has none: a record '
                                'input is written', 'record'),
        *(Place('requirements', key, f'writes the file `{SCRIPT_FILE}` of its working directory, and a listing entry '
                'of that name is written beside it', SCRIPT_FILE) for key in ('entryname', 'basename'))),
}


@dataclass(frozen=True, slots=True)
class Line:
    """What an output line collects: the stream of the command, or the files its pattern matches, which are a
    Directory when the pattern is one Directory input's own name. `glob` is the pattern, a word."""

    stream: Stream | None = None
    glob: Phrase | None = None
    directory: bool = False


@dataclass(frozen=True, slots=True)
class Plan:  # pylint: disable=too-many-instance-attributes
    """What the lines of a tool mean."""

    #: The words that are the program.
    base: tuple[str, ...]
    #: The words of the command line that are neither the program nor an input, placed as they come.
    arguments: tuple[Placement, ...]
    #: Where each input the lines place lands: its `position`, and a `prefix` for its flag.
    placed: tuple[tuple[str, Placement], ...]
    #: The file a `script:` is written to.
    script: Generated | None
    resources: Resources | None
    #: One per output of the document: None for one that says no line, which is CWL as it is written.
    outputs: tuple[Line | None, ...]
    #: Whether the tool holds an expression and declares InlineJavascriptRequirement nowhere: the tool then does.
    javascript: bool


def read(tool: ToolDocument, refused: Diagnostics | None = None) -> Plan:
    """What the lines of `tool` mean.

    Every line it cannot mean is reported, `wic058` at the word, the port or the key that says it, and the reading
    goes on to the next: into `refused` when given, which the parser does for a tool it reads.

    Raises:
        SophiosError: those refusals, when `refused` is not given.
    """
    reading = _Reading(tool, Diagnostics() if refused is None else refused)
    plan = reading.read()
    if refused is None and reading.refused:
        raise SophiosError(reading.refused)
    return plan


def _one(written: Value) -> tuple[Value, bool, bool]:
    """A port's type as what one value of it is, without `?` and `[]`; whether it is an array; whether it may be
    null."""
    one, optional = required(canonical_type(written))
    array = isinstance(one, dict) and one.get('type') == 'array'
    return (one.get('items') if isinstance(one, dict) and array else one), array, optional


def _keys(value: Any, schemas: dict[str, Any], typed: bool = False, seen: frozenset[str] = frozenset()
          ) -> Iterator[tuple[Any, Any]]:
    """Each key written in `value`, at any depth, with the value it holds: CWL's own structure, so what a `default:`
    holds is data and has none, and a type that names one of the tool's `schemas` is what that says."""
    match value:
        case dict():
            for key, held in value.items():
                yield key, held
                if key != 'default':
                    yield from _keys(held, schemas, key in ('type', 'items'), seen)
        case list():
            for item in value:
                yield from _keys(item, schemas, typed, seen)
        case str() if typed and (name := value.rstrip('?[]')) in schemas and name not in seen:
            yield from _keys(schemas[name], schemas, False, seen | {name})


@dataclass(slots=True)
class _Reading:  # pylint: disable=too-many-instance-attributes
    """One tool on its way to a plan: what it means so far, and what was refused."""

    tool: ToolDocument
    refused: Diagnostics
    #: What the author wrote beside the lines, as CWL writes it, by key.
    cwl: dict[str, Any] = field(default_factory=dict)
    base: list[str] = field(default_factory=list)
    arguments: list[Placement] = field(default_factory=list)
    placed: dict[str, Placement] = field(default_factory=dict)
    script: Generated | None = None
    resources: Resources | None = None

    def refuse(self, message: str, span: SourceSpan | None) -> None:
        """Report a line the tool cannot mean, where it is said: at `span`, or else where the tool is."""
        self.refused.error(SophiosErrorCode.TOOL_DOCUMENT, message, span or self.tool.span)

    def read(self) -> Plan:
        """Read each line of the tool, beside the CWL it carries."""
        tool = self.tool
        # A key written with no value says nothing, as CWL reads it: it is no place already written.
        self.cwl = {key: value for key, value in tool.passthrough if key not in _HEAD and value is not None}
        for place, shape in (('arguments', list), ('requirements', dict | list), ('hints', dict | list)):
            if not isinstance(self.cwl.get(place, []), shape):
                self.refuse(f'`{place}:` is where a line of this tool adds what it writes, and it is not the '
                            f'{"list" if shape is list else "mapping or list of requirements"} CWL writes there',
                            tool.at(place))
                del self.cwl[place]
        for line in ('script', 'command'):
            if getattr(tool, line) is not None:
                self._owned(line)
                break
        if tool.docker is not None and declared(self.cwl, 'DockerRequirement'):
            self.refuse(f'`docker:` writes the DockerRequirement of this tool, and one is written beside it: '
                        f'{_WRITTEN}', tool.at('docker'))
        if needs := dict(tool.needs):
            if declared(self.cwl, 'ResourceRequirement'):
                self.refuse(f'`needs:` writes the ResourceRequirement of this tool, and one is written beside it: '
                            f'{_WRITTEN}', tool.at('needs'))
            size = SIZE.fullmatch(str(needs.get('ram')))
            self.resources = Resources(needs.get('cores'), None,
                                       int(size[1]) * 1024 ** 'MGT'.index(size[2]) if size else needs.get('ram'), None)
        if tool.script is not None:
            self._script(tool.script)
        elif tool.command is not None:
            self._command(tool.command)
        outputs = tuple(self._output(port) for port in tool.outputs)
        return Plan(tuple(self.base), tuple(self.arguments), tuple(self.placed.items()), self.script, self.resources,
                    outputs, self._javascript(outputs))

    def _javascript(self, outputs: tuple[Line | None, ...]) -> bool:
        """Whether a string anywhere in the tool, the author's or a line's, holds an expression, and the author
        declared InlineJavascriptRequirement nowhere, as a requirement or a hint."""
        tool = self.tool
        written = [self.cwl, [dict(port.fields) for port in (*tool.inputs, *tool.outputs)], tool.docker, tool.script,
                   *self.base, *(cwl_tool.write_word(argument.value_from)
                                 for argument in self.arguments if argument.value_from),
                   *(slot.prefix for slot in self.placed.values()),
                   *(cwl_tool.write_word(line.glob) for line in outputs if line and line.glob),
                   self.resources and [self.resources.cores_min, self.resources.ram_min]]
        return any(map(EXPRESSION.search, strings(written))) and not declared(self.cwl, _JAVASCRIPT)

    def _owned(self, line: str) -> None:
        """Refuse each place `line` writes that the author wrote too, wherever in the CWL beside it: one walk over
        everything written, for every place of `OWNED`. A key with no value, or an empty list, says nothing."""
        refused: set[SourceSpan | None] = set()  # one mistake at one place earns one diagnostic
        schemas = {str(entry.get('name')).rpartition('#')[2]: entry
                   for _, each in declared(self.cwl, 'SchemaDefRequirement')
                   for entry in each.get('types') or () if isinstance(entry, dict)}
        for place in OWNED[line]:
            for span, written in self._written(place.under):
                if span not in refused and any(
                        key == place.key and held is not None and held != [] and (
                            place.holds is None or isinstance(held, str) and posixpath.normpath(held) == place.holds)
                        for key, held in _keys(written, schemas)):
                    refused.add(span)
                    self.refuse(f'`{line}:` {place.says}: {_WRITTEN}', span)

    def _written(self, under: str) -> Iterator[tuple[SourceSpan | None, dict[str, Any]]]:
        """What the author wrote under the key `under`, each with where: a key of the CWL beside the line, or the
        port of each input, written by its name."""
        if under == 'inputs':
            yield from ((port.span, {under: dict(port.fields)}) for port in self.tool.inputs)
        elif under in self.cwl:
            yield self.tool.at(under), {under: self.cwl[under]}

    def _command(self, words: tuple[Word, ...]) -> None:
        """A command line as typed. The line says where: its words of text before the first input or flag are the
        program (when it starts with one), `-x {n}` is the input `n` with its flag, `{n}` is the input `n` in its
        place, and any other word is text in its place. The CWL beside it says how: an input's own `inputBinding:`
        (a prefix, `separate`...) keeps its fields, and the line adds the `position`."""
        if not words or not (words[0].text or words[0].input):
            self.refuse('a tool says what runs: `command:` has no words, so there is nothing to run',
                        self.tool.at('command'))
            return
        # A line that starts with a flag or an input has no program: the image's entry point runs it.
        program = words[0].input is None and not words[0].text.startswith('-')
        position, flag = 0, None
        for word, following in zip(words, (*words[1:], Word())):
            if word.input is None:
                if following.input is not None and not word.quoted and word.text.startswith('-') \
                        and word.text.strip('-'):
                    flag = word  # an unquoted dash word directly before a value is that value's flag
                elif program and not position:
                    self.base.append(word.text)
                else:
                    position += 1
                    self.arguments.append(Placement(position=position, value_from=Phrase.literal(word.text)))
                continue
            position += 1
            said, flag = flag, None
            name = word.input.name
            binding: Any = dict(word.input.fields).get('inputBinding') or {}
            refusal = (
                # Its place is said by an earlier word of the line, or by the author's own binding.
                'has its place already, from an earlier word of `command:` or from a `position:` in its '
                '`inputBinding:`: an input has one place, and the line says it once'
                if name in self.placed or 'position' in binding else
                f'has the flag `{said.text}` before it, and its `inputBinding:` says a `prefix:` too: {_WRITTEN}'
                if said and 'prefix' in binding else
                'is a boolean, which writes its flag and nothing else: write the flag before it'
                if not (said or binding) and _one(word.input.type)[0] == 'boolean' else None)
            if refusal:
                self.refuse(f'{{{name}}} {refusal}', word.span)
            else:
                self.placed[name] = Placement(position=position, prefix=said.text if said else None)

    def _script(self, text: str) -> None:
        """A shell script as written, in the working directory, run by bash with each input in the environment
        variable of its name: an array is its items between spaces, an input with no value is unset, a true boolean
        is set and empty. No value is ever part of the script's text, and the whole command line is the script's:
        `env name=value ... bash .script.sh`, so no word of the author's can come before the script."""
        tool = self.tool
        for port in tool.inputs:
            if not re.fullmatch(r'[A-Za-z_]\w*', port.name, re.ASCII) or port.name == port.name.upper():
                self.refuse(f'input {port.name!r} of a `script:` tool is the shell variable of its name, so the name '
                            'is letters, digits and `_` with a small letter in it: a name in capitals is the '
                            "shell's own (PATH, BASH_ENV)", port.span)
            self.placed[port.name] = Placement(position=0, prefix=f'{port.name}=', separate=False,
                                               item_separator=' ' if _one(port.type)[1] else None)
        if any(section == 'hints' for section, _ in declared(self.cwl, _STAGED)):
            self.refuse('`script:` adds its file to an InitialWorkDirRequirement, and one under `hints:` would be '
                        'overridden by it: write yours under `requirements:`', tool.at('hints'))
        entry = next((item for section, item in declared(self.cwl, _STAGED) if section == 'requirements'), {})
        if not isinstance(entry.get('listing', []), list):
            self.refuse('`script:` adds its file to the `listing:` of the InitialWorkDirRequirement, and the one '
                        'beside it has no list to add it to', tool.at('requirements'))
        self.script = Generated(SCRIPT_FILE, text)
        self.base = ['env']
        self.arguments = [Placement(position=place, value_from=Phrase.literal(word))
                          for place, word in enumerate(('bash', SCRIPT_FILE), start=1)]

    def _output(self, port: Port) -> Line | None:
        """The output a port says by its line: `stdout` or `stderr`, the stream of the command; else the file a
        pattern of text and inputs matches, a Directory when the pattern is one Directory input's own name. A port
        that says no line is CWL, as it is written."""
        if port.line is None:
            return None
        fields = dict(port.fields)
        text = ''.join(part.text for part in port.line)
        named = [part.input for part in port.line if part.input is not None]
        stream = not named and text in ('stdout', 'stderr')
        if 'outputBinding' in fields or stream and 'type' in fields:
            self.refuse(f'this line writes the `{"outputBinding" if "outputBinding" in fields else "type"}:` of its '
                        f'output, and one is written beside it: {_WRITTEN}', port.span)
        elif stream:
            return Line(Stream(text))
        elif not named and (not text.strip() or not isinstance(kind := _one(text)[0], str) or kind in ATOMIC_TYPES):
            self.refuse(f'{text!r} is {"a type" if text.strip() else "empty"}, and an output line says where the '
                        'output is: `stdout`, `stderr` or a pattern such as `{name}.txt`; an output with a type of '
                        'its own is a CWL port (`type:`, `outputBinding:`)', port.span)
        else:
            glob: list[str | Reference] = []
            for part in port.line:
                if part.input is None:
                    glob.append(part.text)
                    continue
                kind, array, optional = _one(part.input.type)
                reference = input_reference(part.input.name, '.basename' if kind in ('File', 'Directory') else '')
                if array or optional or isinstance(kind, list) or reference is None:
                    self.refuse(f'{{{part.input.name}}} has no one name to give this output: it is an array, may be '
                                'null or of several types, or has a quote in its name. An output that depends on it '
                                'is a CWL port (`type:`, `outputBinding:`)', part.span)
                    continue
                glob.append(Reference(part.input.name, Part.BASENAME if kind in ('File', 'Directory') else Part.VALUE))
            return Line(glob=Phrase(tuple(glob)), directory=len(port.line) == 1 and bool(named)
                        and _one(named[0].type)[0] == 'Directory')
        return None


def _written_port(port: Port, line: Line | None) -> dict[str, Any]:
    """The fields an output port writes of the CWL: its own, with the type its line says unless it writes one."""
    if line is None:
        return dict(port.fields)
    return {'type': spell(Atom.DIRECTORY if line.directory else Atom.FILE)} | dict(port.fields)


def _merged(found: ToolInput, slot: Placement | None) -> ToolInput:
    """An input as the CWL beside the lines says it, with where the line places it over its own binding."""
    return found if slot is None else replace(found, placement=cwl_tool.laid(found.placement, slot))


def _place(document: ToolDocument, section: str, name: str, found: list[Need]) -> str:
    """Where a requirement a line adds is written: its class in a mapping, its index in a list."""
    return name if isinstance(dict(document.passthrough).get(section), dict) else str(len(found))


def _needs(document: ToolDocument, plan: Plan, own: tuple[Need, ...]) -> tuple[Need, ...]:
    """What the CWL beside the lines asks for, with what the lines ask for added to it, as they are added to the CWL
    in each section: requirements first, then hints."""
    requirements = [need for need in own if not need.hint]
    hints = [need for need in own if need.hint]
    if plan.resources is not None:
        requirements.append(Need(_place(document, 'requirements', 'ResourceRequirement', requirements),
                                 'ResourceRequirement', False, NeedKind.RESOURCES, True, (), plan.resources))
    if plan.script is not None:
        for index, need in enumerate(requirements):
            if need.name == _STAGED:
                assert isinstance(need.body, Staging)
                requirements[index] = replace(need, body=Staging((*(need.body.entries or ()), plan.script)))
                break
        else:
            requirements.append(Need(_place(document, 'requirements', _STAGED, requirements), _STAGED, False,
                                     NeedKind.STAGING, True, (), Staging((plan.script,))))
    if plan.javascript:
        requirements.append(Need(_place(document, 'requirements', _JAVASCRIPT, requirements), _JAVASCRIPT, False,
                                 NeedKind.OTHER))
    if document.docker is not None:
        hints.append(Need(_place(document, 'hints', 'DockerRequirement', hints), 'DockerRequirement', True,
                          NeedKind.CONTAINER, True, (), Container(document.docker, None)))
    return (*requirements, *hints)


def _output(process: Process, port: Port, line: Line | None) -> ToolOutput:
    """An output as the CWL beside the lines says it, with what its line collects."""
    found = cwl_tool.read_output(process, port.name, _written_port(port, line))
    return found if line is None else replace(found, capture=Capture(line.stream, line.glob))


def _body(document: ToolDocument, plan: Plan, process: Process, written: Written) -> ToolBody:
    """The body of the tool: its lines over the CWL beside them, which `process` is."""
    own = cwl_tool.command(process, written)
    slots = dict(plan.placed)
    return replace(
        own, base=plan.base[0] if len(plan.base) == 1 else plan.base or None,
        arguments=(*plan.arguments, *own.arguments),
        inputs=tuple(_merged(cwl_tool.read_input(process, port.name, dict(port.fields)), slots.get(port.name))
                     for port in document.inputs),
        outputs=tuple(_output(process, port, line) for port, line in zip(document.outputs, plan.outputs)),
        needs=_needs(document, plan, own.needs))


def node(document: ToolDocument, run_path: str) -> ToolNode:
    """The tool `document` says, whose body is read when a target asks."""
    plan = read(document, Diagnostics())  # a tool the parse accepted

    @cache
    def load() -> tuple[Process, Written]:
        return cwl_tool.beside(document.passthrough, run_path)

    return ToolNode(
        run_path,
        tuple(cwl_tool.tool_port(port.name, dict(port.fields), output=False) for port in document.inputs),
        tuple(cwl_tool.tool_port(port.name, _written_port(port, line), output=True)
              for port, line in zip(document.outputs, plan.outputs)),
        False, cwl_tool.namespaces(dict(document.passthrough).get('$namespaces')), document, (),
        *cwl_tool.held(lambda: _body(document, plan, *load())))
