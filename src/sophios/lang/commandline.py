"""The CWL CommandLineTool a tool means: the one lowering of a `ToolDocument`, whether the parser read it from a
`.wic` or Python built it from objects.

A line (`command:`, `script:`, `docker:`, `needs:`, a port line, an output line) is written out as the CWL it needs in
order to mean what it says, and never changes CWL that is there: a place a line writes that the author wrote too is
refused. `OWNED` is the table of those places, and one walk over what the author wrote checks every one. Every other
key is CWL's own, copied. What a line writes is built with the standard's own classes, so a field of CWL is a keyword
Python checks.
"""
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
import posixpath
import re
from typing import Any, Final

from cwl_utils.parser import cwl_v1_2 as _standard

from .cwl import (ATOMIC_TYPES, CWL_VERSION, EXPRESSION, SIZE, canonical_type, declare_javascript, declared,
                  input_reference, literal, require, required)
from .diagnostics import Diagnostics, SophiosError
from .error_codes import SophiosErrorCode
from .nodes import OpaqueCwl, Port, ToolDocument, Word
from .spans import SourceSpan

#: The file a `script:` is written to in the working directory: no glob matches a dot file.
SCRIPT_FILE: Final = '.script.sh'
_WRITTEN: Final = 'that place is already written, and a line never changes CWL that is there'


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


def command_line_tool(tool: ToolDocument, refused: Diagnostics | None = None) -> dict[str, Any]:
    """The CWL CommandLineTool `tool` means.

    Every line it cannot mean is reported, `wic058` at the word, the port or the key that says it, and the lowering
    goes on to the next: into `refused` when given, which the parser does for a tool it reads.

    Raises:
        SophiosError: those refusals, when `refused` is not given.
    """
    lowering = _Lowering(tool, Diagnostics() if refused is None else refused)
    lowering.lower()
    if refused is None and lowering.refused:
        raise SophiosError(lowering.refused)
    return lowering.cwl


def _one(written: OpaqueCwl) -> tuple[OpaqueCwl, bool, bool]:
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
class _Lowering:
    """One tool on its way to CWL: what is written so far, and what was refused."""

    tool: ToolDocument
    refused: Diagnostics
    cwl: dict[str, Any] = field(default_factory=dict)
    #: Each input as the CWL port being written, by its name.
    ports: dict[str, dict[str, Any]] = field(default_factory=dict)

    def refuse(self, message: str, span: SourceSpan | None) -> None:
        """Report a line the tool cannot mean, where it is said: at `span`, or else where the tool is."""
        self.refused.error(SophiosErrorCode.TOOL_DOCUMENT, message, span or self.tool.span)

    def lower(self) -> None:
        """Write each line of the tool out, beside the CWL it copies."""
        tool, cwl = self.tool, self.cwl
        head = {'class': _standard.CommandLineTool.__name__, 'cwlVersion': CWL_VERSION}
        # A key written with no value says nothing, as CWL reads it: it is no place already written.
        cwl |= head | {key: deepcopy(value) for key, value in tool.passthrough
                       if key not in head and value is not None}
        for place, shape in (('arguments', list), ('requirements', dict | list), ('hints', dict | list)):
            if not isinstance(cwl.get(place, []), shape):
                self.refuse(f'`{place}:` is where a line of this tool adds what it writes, and it is not the '
                            f'{"list" if shape is list else "mapping or list of requirements"} CWL writes there',
                            tool.at(place))
                del cwl[place]
        self.ports = {port.name: deepcopy(dict(port.fields)) for port in tool.inputs}
        for line in ('script', 'command'):
            if getattr(tool, line) is not None:
                self._owned(line)
                break
        if tool.docker is not None:
            self._requires('docker', 'hints', _standard.DockerRequirement(dockerPull=tool.docker))
        if needs := dict(tool.needs):
            size = SIZE.fullmatch(str(needs.get('ram')))
            self._requires('needs', 'requirements', _standard.ResourceRequirement(
                coresMin=needs.get('cores'),
                ramMin=int(size[1]) * 1024 ** 'MGT'.index(size[2]) if size else needs.get('ram')))
        if tool.script is not None:
            self._script(tool.script)
        elif tool.command is not None:
            self._command(tool.command)
        cwl['inputs'] = self.ports
        cwl['outputs'] = {port.name: self._output(port) for port in tool.outputs}
        declare_javascript(cwl)

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

    def _requires(self, line: str, section: str, added: _standard.Saveable) -> None:
        """Add the requirement the key `line` writes to the tool's `section`. One the tool declares, as a requirement
        or as a hint, is that place already written."""
        fields: dict[str, Any] = added.save()
        name = fields.pop('class')
        if declared(self.cwl, name):
            self.refuse(f'`{line}:` writes the {name} of this tool, and one is written beside it: {_WRITTEN}',
                        self.tool.at(line))
        elif (entry := require(self.cwl, section, name)) is not None:
            entry |= fields

    def _command(self, words: tuple[Word, ...]) -> None:
        """A command line as typed. The line says where: its words of text before the first input or flag are the
        `baseCommand` (when it starts with a program), `-x {n}` is the input `n` with its flag, `{n}` is the input
        `n` in its place, and any other word is text in its place. The CWL beside it says how: an input's own
        `inputBinding:` (a prefix, `separate`...) keeps its fields, and the line adds the `position`."""
        cwl = self.cwl
        if not words or not (words[0].text or words[0].input):
            self.refuse('a tool says what runs: `command:` has no words, so there is nothing to run',
                        self.tool.at('command'))
            return
        # A line that starts with a flag or an input has no program: the image's entry point runs it.
        program = words[0].input is None and not words[0].text.startswith('-')
        base: list[str] = []
        arguments: list[Any] = []
        position, flag = 0, None
        for word, following in zip(words, (*words[1:], Word())):
            if word.input is None:
                if following.input is not None and not word.quoted and word.text.startswith('-') \
                        and word.text.strip('-'):
                    flag = word  # an unquoted dash word directly before a value is that value's flag
                elif program and not position:
                    base.append(word.text)
                else:
                    position += 1
                    arguments.append(_standard.CommandLineBinding(position=position, valueFrom=literal(
                        word.text) if EXPRESSION.search(word.text) else word.text).save())
                continue
            position += 1
            said, flag = flag, None
            name, port = word.input.name, self.ports[word.input.name]
            binding = port.get('inputBinding', {})
            refusal = (
                # Its place is said by an earlier word of the line, or by the author's own binding.
                'has its place already, from an earlier word of `command:` or from a `position:` in its '
                '`inputBinding:`: an input has one place, and the line says it once' if 'position' in binding else
                f'has the flag `{said.text}` before it, and its `inputBinding:` says a `prefix:` too: {_WRITTEN}'
                if said and 'prefix' in binding else
                'is a boolean, which writes its flag and nothing else: write the flag before it'
                if not (said or binding) and _one(word.input.type)[0] == 'boolean' else None)
            if refusal:
                self.refuse(f'{{{name}}} {refusal}', word.span)
            else:
                port['inputBinding'] = binding | _standard.CommandLineBinding(
                    position=position, prefix=said and said.text).save()
        if base:
            cwl['baseCommand'] = base[0] if len(base) == 1 else base
        if arguments:
            cwl['arguments'] = arguments + cwl.get('arguments', [])

    def _script(self, text: str) -> None:
        """A shell script as written, in the working directory, run by bash with each input in the environment
        variable of its name: an array is its items between spaces, an input with no value is unset, a true boolean
        is set and empty. No value is ever part of the script's text, and the whole command line is the script's:
        `env name=value ... bash .script.sh`, so no word of the author's can come before the script."""
        tool, cwl = self.tool, self.cwl
        for port in tool.inputs:
            if not re.fullmatch(r'[A-Za-z_]\w*', port.name, re.ASCII) or port.name == port.name.upper():
                self.refuse(f'input {port.name!r} of a `script:` tool is the shell variable of its name, so the name '
                            'is letters, digits and `_` with a small letter in it: a name in capitals is the '
                            "shell's own (PATH, BASH_ENV)", port.span)
            self.ports[port.name]['inputBinding'] = _standard.CommandLineBinding(
                position=0, prefix=f'{port.name}=', separate=False,
                itemSeparator=' ' if _one(port.type)[1] else None).save()
        staged = _standard.InitialWorkDirRequirement.__name__
        if 'hints' in dict(declared(cwl, staged)):
            self.refuse('`script:` adds its file to an InitialWorkDirRequirement, and one under `hints:` would be '
                        'overridden by it: write yours under `requirements:`', tool.at('hints'))
        entry = require(cwl, 'requirements', staged)
        listing = None if entry is None else entry.setdefault('listing', [])
        if isinstance(listing, list):
            listing.append(_standard.File(basename=SCRIPT_FILE, contents=text).save())
        else:
            self.refuse('`script:` adds its file to the `listing:` of the InitialWorkDirRequirement, and the one '
                        'beside it has no list to add it to', tool.at('requirements'))
        cwl |= {'baseCommand': 'env', 'arguments': [
            _standard.CommandLineBinding(position=place, valueFrom=word).save()
            for place, word in enumerate(('bash', SCRIPT_FILE), start=1)]}

    def _output(self, port: Port) -> dict[str, Any]:
        """The output a port says by its line: `stdout` or `stderr`, the stream as CWL captures it; else the file a
        pattern of text and inputs matches, a Directory when the pattern is one Directory input's own name. What the
        port says beside the line is kept, and a port that says no line is CWL, copied."""
        fields = deepcopy(dict(port.fields))
        if port.line is None:
            return fields
        text = ''.join(part.text for part in port.line)
        named = [part.input for part in port.line if part.input is not None]
        stream = not named and text in ('stdout', 'stderr')
        if 'outputBinding' in fields or stream and 'type' in fields:
            self.refuse(f'this line writes the `{"outputBinding" if "outputBinding" in fields else "type"}:` of its '
                        f'output, and one is written beside it: {_WRITTEN}', port.span)
        elif stream:
            return {'type': text} | fields
        elif not named and (not text.strip() or not isinstance(kind := _one(text)[0], str) or kind in ATOMIC_TYPES):
            self.refuse(f'{text!r} is {"a type" if text.strip() else "empty"}, and an output line says where the '
                        'output is: `stdout`, `stderr` or a pattern such as `{name}.txt`; an output with a type of '
                        'its own is a CWL port (`type:`, `outputBinding:`)', port.span)
        else:
            glob = ''
            for part in port.line:
                if part.input is None:
                    # cwltool unescapes a glob that holds an expression, which one that names an input does.
                    glob += literal(part.text) if named or EXPRESSION.search(text) else part.text
                    continue
                kind, array, optional = _one(part.input.type)
                reference = input_reference(part.input.name, '.basename' if kind in ('File', 'Directory') else '')
                if array or optional or isinstance(kind, list) or reference is None:
                    self.refuse(f'{{{part.input.name}}} has no one name to give this output: it is an array, may be '
                                'null or of several types, or has a quote in its name. An output that depends on it '
                                'is a CWL port (`type:`, `outputBinding:`)', part.span)
                glob += reference or ''
            directory = len(port.line) == 1 and bool(named) and _one(named[0].type)[0] == 'Directory'
            return {'type': 'Directory' if directory else 'File',
                    'outputBinding': _standard.CommandOutputBinding(glob=glob).save()} | fields
        return fields
