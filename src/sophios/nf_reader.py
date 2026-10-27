"""Read a pipeline Sophios wrote back into the model it was written from: the renderer's inverse.

The reader admits exactly the text the renderer writes (design §4.2). From
the three files Nextflow reads, ``workflow.nf``, ``nextflow_params.json``
and ``nextflow.config``, it reads each fact the text states once, through
the idiom table the renderer fills (:class:`sophios.input_output_nf.Idioms`),
builds the model, and renders it again: the files must be that rendering,
so each form the text shows only through others is checked, never read.
Anything else stops the read with a coded diagnostic at its line, and
nothing is returned.

Layout is free: indentation, the length of each run of spaces and tabs,
blank lines, comments, the order of the process blocks, and LF or CR LF
line endings, except inside a string literal, a script or stub body, or a
helper, which are content, as a line break is. ``nextflow_params.json`` is
read as JSON, so its whitespace and the order of its keys are free.
"""

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import re
from typing import Any, NoReturn

from .input_output_nf import (
    NEXTFLOW_CONFIG,
    NEXTFLOW_PARAMS,
    NEXTFLOW_SCRIPT,
    NF_CUDA_CARRIERS,
    NF_DIRECTORY_CARRIER,
    NF_ENTRYPOINT_READ,
    NF_ENTRYPOINT_WORDS,
    NF_HELPER_FUNCTIONS,
    NF_STUB_LINES,
    Idioms,
    nextflow_params,
    render_nextflow,
    render_nextflow_config,
)
from .ir.complete import coerce_job_value
from .ir.types import AuthoredName, PortDeclaration, PortType
from .lang.diagnostics import SophiosError
from .lang.error_codes import SophiosErrorCode
from .lang.spans import SourceSpan
from .nf_expr import GROOVY_IDENTIFIER, GROOVY_LITERAL, groovy_unescape, read_groovy
from .nf_types import (
    NF_ENTRY_STAGING_PREFIX,
    PARAM_TYPES,
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfConnection,
    NfEntry,
    NfEnvVar,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfOptionalBinding,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfShellLiteral,
    NfSourceConnection,
    NfStepInputConnection,
    NfTemplate,
    NfTemplateSegment,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
    param_channel,
)

_Code = SophiosErrorCode
_SOURCE = re.compile(rf"{GROOVY_IDENTIFIER.pattern}(?:\.out\.{GROOVY_IDENTIFIER.pattern})?")
_STREAMS = {"<": "stdin", ">": "stdout", "2>": "stderr"}
_CLOSERS = {"(": ")", "[": "]", "{": "}", "'": "'", '"': '"'}
#: A JSON string, whose brackets nest nothing, or a bracket.
_JSON_BRACKET = re.compile(r'"(?:[^"\\]|\\.)*"|[][{}]')


@dataclass(frozen=True, slots=True)
class _Line:
    """One line without its layout, where it was read, and whether it is content kept as written."""

    file: str
    number: int
    text: str
    verbatim: bool = False

    @property
    def span(self) -> SourceSpan:
        """Where the line is."""
        return SourceSpan(self.file, self.number, 1, self.number, 1)

    def stop(self, code: SophiosErrorCode, what: str) -> NoReturn:
        """Stop the read here: this line is ``what``."""
        shown = self.text.strip().split("\n", 1)[0][:160]
        raise SophiosError.error(code, f"{what}: {shown}", span=self.span)


def _skip(text: str, at: int) -> int:
    """The index after the string or bracketed group that opens at ``text[at]``, however deep what it holds nests."""
    openers, at = [text[at]], at + 1
    while openers:
        if at >= len(text):
            raise ValueError(f"a {openers[-1]} that does not close")
        quoted = openers[-1] in "'\""
        if text[at] == _CLOSERS[openers[-1]]:
            openers.pop()
        elif quoted and text[at] == "\\":
            at += 1
        elif openers[-1] == '"' and text.startswith("${", at):
            openers.append("{")
            at += 1
        elif not quoted and text[at] in _CLOSERS:
            openers.append(text[at])
        at += 1
    return at


def _split(text: str, separator: str = ", ") -> list[str]:
    """``text`` split at each ``separator`` outside strings and brackets."""
    parts, start, at = [], 0, 0
    while at < len(text):
        if text.startswith(separator, at):
            parts.append(text[start:at])
            start = at = at + len(separator)
        else:
            at = _skip(text, at) if text[at] in "'\"([{" else at + 1
    return [*parts, text[start:]]


def _code(line: str, comment: bool) -> tuple[str, bool]:
    """``line`` without comments, indentation, and all but one space of each run of spaces and tabs.

    ``comment`` is whether a block comment is open before the line; the
    second value is whether one is open after it.
    """
    text, at = "", 0
    while at < len(line):
        if comment:
            end = line.find("*/", at)
            at, comment = (len(line), True) if end < 0 else (end + 2, False)
            text += "" if text.endswith(" ") else " "
        elif line.startswith("//", at):
            break
        elif line.startswith("/*", at):
            at, comment = at + 2, True
        elif line[at] in " \t":
            text, at = text + ("" if text.endswith(" ") else " "), at + 1
        elif line[at] in "'\"":
            end = _skip(line, at)
            text, at = text + line[at:end], end
        else:
            text, at = text + line[at], at + 1
    return text.strip(" "), comment


def _lines(file: str, text: str) -> list[_Line]:
    """The lines of ``text`` without layout; a script or stub body and each helper are kept as written."""
    raw = text.split("\n")
    helpers = {helper.split("\n", 1)[0]: helper for helper in NF_HELPER_FUNCTIONS}
    lines: list[_Line] = []
    index, comment, body = 0, False, False
    while index < len(raw):
        line = _Line(file, index + 1, raw[index], verbatim=True)
        start = len(line.text) - len(line.text.lstrip(" \t"))
        if body and line.text.startswith('"""', start):
            # The quotes that close a script or stub end its string; what follows them on their line is code.
            lines.append(_Line(file, line.number, line.text[:start + 3], verbatim=True))
            line, body = _Line(file, line.number, line.text[start + 3:]), False
        if body:
            lines.append(line)
        elif not comment and line.verbatim and line.text.startswith("def ", start):
            helper = helpers.get(line.text[start:], "")
            if "\n".join([line.text[start:], *raw[index + 1:index + helper.count("\n") + 1]]) != helper:
                line.stop(_Code.IMPORT_EDITED_HELPER, "a def that is not a helper Sophios writes, byte for byte")
            lines.append(_Line(file, line.number, helper, verbatim=True))
            index += helper.count("\n")
        elif lines and lines[-1].text in ("script:", "stub:") and _opens(line.text, comment):
            # What precedes the quotes that open a script or stub is layout; what follows them is in its string.
            lines.append(_Line(file, line.number, '"""', verbatim=True))
            body = True
        else:
            try:
                code, comment = _code(line.text, comment)
            except ValueError as exc:
                line.stop(_Code.IMPORT_FOREIGN_STATEMENT, f"text Sophios does not write, with {exc}")
            if code:
                lines.append(_Line(file, line.number, code))
        index += 1
    return lines


def _opens(line: str, comment: bool) -> bool:
    """Whether ``line`` ends in the quotes that open a script or stub, with nothing but layout before them."""
    try:
        return line.endswith('"""') and _code(line[:-3], comment) == ("", False)
    except ValueError:
        return False


def _depth(line: _Line) -> int:
    """How many more braces ``line`` opens than it closes, outside strings; its strings close, as ``_code`` read."""
    depth, at = 0, 0
    while not line.verbatim and at < len(line.text):
        if line.text[at] in "'\"":
            at = _skip(line.text, at)
            continue
        depth += {"{": 1, "}": -1}.get(line.text[at], 0)
        at += 1
    return depth


def _top(lines: list[_Line]) -> tuple[list[_Line], dict[str, list[_Line]], list[list[_Line]]]:
    """The top-level lines but the process blocks, the process blocks by name, and the workflow blocks.

    The process blocks lie together between the helpers and the workflows, in any order.
    """
    others: list[_Line] = []
    processes: dict[str, list[_Line]] = {}
    workflows: list[list[_Line]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.text == "nextflow.enable.dsl=2" or line.verbatim:
            if processes or workflows:
                line.stop(_Code.IMPORT_MISMATCH, "a line Sophios writes before the processes")
            others.append(line)
            index += 1
            continue
        process = Idioms.PROCESS.match(line.text)
        if process is None and Idioms.WORKFLOW.match(line.text) is None and line.text != "workflow {":
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a top-level statement Sophios does not write")
        end, depth = index, _depth(line)
        while depth > 0 and end + 1 < len(lines):
            end += 1
            depth += _depth(lines[end])
        if depth:
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a block that does not close")
        block = lines[index:end + 1]
        if process is None:
            workflows.append(block)
            others.extend(block)
        elif workflows:
            line.stop(_Code.IMPORT_MISMATCH, "a process Sophios writes before the workflows")
        elif processes.setdefault(process["name"], block) is not block:
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a second process of this name")
        index = end + 1
    return others, processes, workflows


def _literal(text: str) -> str:
    if GROOVY_LITERAL.fullmatch(text) is None:
        raise ValueError(f"{text} is not a string")
    return groovy_unescape(text[1:-1])


def _strings(text: str) -> str | tuple[str, ...]:
    """A string, or a list of strings."""
    if not text.startswith("["):
        return _literal(text)
    if not text.endswith("]"):
        raise ValueError(f"{text} is not a list")
    return tuple(_literal(item) for item in _split(text[1:-1])) if text != "[]" else ()


def _named(text: str) -> dict[str, str]:
    """Named arguments, ``name: value`` each."""
    named = {}
    for part in _split(text):
        if (found := Idioms.OPTION.match(part)) is None:
            raise ValueError(f"{part} is not a named argument")
        named[found["name"]] = found["value"]
    return named


def _options(text: str) -> dict[str, str]:
    """The named arguments a declaration has after its value, each after a comma."""
    if text and not text.startswith(", "):
        raise ValueError(f"{text} is not named arguments")
    return _named(text[2:]) if text else {}


def _pieces(text: str) -> list[tuple[bool, str]]:
    """GString content as its runs of text and its interpolations, each True; escapes stay in the text."""
    pieces: list[tuple[bool, str]] = []
    at = start = 0
    while at < len(text):
        if text.startswith("${", at):
            if start < at:
                pieces.append((False, text[start:at]))
            end = _skip(text, at + 1)
            pieces.append((True, text[at:end]))
            at = start = end
        else:
            at += 2 if text[at] == "\\" else 1
    return [*pieces, (False, text[start:])] if start < len(text) else pieces


def _reference(value: str) -> NfInputReference | NfBasenameReference:
    """The reference whose Groovy value is ``value``."""
    if found := Idioms.STAGED_NAME.match(value):
        return NfBasenameReference(found["name"])
    if found := Idioms.ENTRY_NAME.match(value) or Idioms.FLOAT_TEXT.match(value):
        return NfInputReference(found["name"])
    if GROOVY_IDENTIFIER.fullmatch(value):
        return NfInputReference(value)
    raise ValueError(f"{value} is not a reference Sophios writes")


def _segment(text: str) -> NfTemplateSegment:
    if text.startswith("'"):
        return NfLiteral(_literal(text))
    found = Idioms.STRING.match(text)
    return _reference(text if found is None else found["value"])


def _template(token: str) -> NfTemplate:
    if (found := Idioms.TEMPLATE.match(token)) is None:
        raise ValueError(f"{token} is not a quoted value")
    return NfTemplate(tuple(_segment(part) for part in _split(found["expression"], " + ")))


def _token(token: str) -> NfCommandToken:
    """The command token an interpolation renders."""
    if found := Idioms.FLAG.match(token):
        return NfFlag(found["name"], _literal(found["prefix"]))
    if found := Idioms.ARRAY.match(token):
        prefixed = Idioms.PREFIXED_ITEMS.match(found["joined"])
        return NfArrayBinding(found["name"], None if prefixed is None else _literal(prefixed["prefix"]))
    if found := Idioms.OPTIONAL.match(token):
        if separate := Idioms.SEPARATE.match(found["words"]):
            return NfOptionalBinding(found["name"], _literal(separate["prefix"]))
        if joined := Idioms.JOINED.match(found["words"]):
            return NfOptionalBinding(found["name"], _literal(joined["prefix"]), separate=False)
        return NfOptionalBinding(found["name"])
    return _template(token)


def _tokens(pieces: list[tuple[bool, str]]) -> list[NfCommandToken]:
    """A command line's tokens: one space between two, and raw text a shell literal.

    Raw text before the first interpolation or after the last is a literal
    and its space; between two, it is one space, or a literal between two.
    """
    tokens: list[NfCommandToken] = []
    for index, (interpolation, text) in enumerate(pieces):
        if interpolation:
            tokens.append(_token(text))
        elif not 0 < index < len(pieces) - 1 or len(text) > 1:
            literal = text[1 if index else 0:len(text) - (index < len(pieces) - 1)]
            tokens.append(NfShellLiteral(groovy_unescape(literal)))
    return tokens


def _streams(pieces: list[tuple[bool, str]]) -> dict[str, NfTemplate]:
    """The redirects ``pieces`` hold: each operator between spaces, then its target."""
    streams = {}
    for index in range(0, len(pieces), 2):
        operator = pieces[index][1].strip()
        if pieces[index][0] or operator not in _STREAMS or index + 1 == len(pieces):
            raise ValueError(f"{pieces[index][1]} is not a redirect")
        streams[_STREAMS[operator]] = _template(pieces[index + 1][1])
    return streams


def _command(body: list[_Line], head: _Line) -> tuple[NfCommand, dict[str, NfEntry]]:
    """The command a script body runs, and the entries it makes first."""
    texts = [line.text.removeprefix("    ") for line in body]
    entries: dict[str, NfEntry] = {}
    env: list[NfEnvVar] = []
    index = 0
    try:
        for index, text in enumerate(texts):
            if found := Idioms.ENTRY.match(text):
                name = None if Idioms.ENTRY_NAME.match(found["entry"]) else _literal(found["entry"])
                entries[found["name"]] = NfEntry(name, found["operator"] == "cp -RL")
            elif found := Idioms.EXPORT.match(text):
                env.append(NfEnvVar(found["name"], _template(found["value"])))
            elif not (text.startswith("if [ -e ") or text == NF_ENTRYPOINT_READ):
                break
        rest = texts[index:]
        parenthesized = len(rest) == 3 and rest[0] == "(" and rest[2].startswith(")")
        if not parenthesized and len(rest) != 1:
            raise ValueError("a script that is not a command Sophios writes")
        index += parenthesized  # the command's line, after its "("
        pieces = _pieces(texts[index].removeprefix(f"{NF_ENTRYPOINT_WORDS} "))
        # A command on one line holds no shell literal: its first operator between spaces starts its redirects.
        split = len(pieces) if parenthesized else next(
            (at for at, (interpolation, text) in enumerate(pieces)
             if not interpolation and text.strip() in _STREAMS and text.startswith(" ")), len(pieces))
        # An empty line is the one shell literal with no text.
        tokens = _tokens(pieces[:split]) or [NfShellLiteral("")]
        index += parenthesized  # the ")" line, which holds the redirects
        streams = _streams(_pieces(texts[index][1:]) if parenthesized else pieces[split:])
        return NfCommand(tokens, streams.get("stdin"), streams.get("stdout"), streams.get("stderr"), env), entries
    except ValueError as exc:
        (body[index] if body else head).stop(_Code.IMPORT_FOREIGN_SCRIPT, str(exc))


def _glob(text: str, literal: bool) -> NfTemplate:
    """An output's glob; a pattern glob escapes its braces."""
    if text.startswith("'"):
        segments: list[NfTemplateSegment] = [NfLiteral(_literal(text))]
    else:
        segments = [_reference(found["value"]) if (found := Idioms.INTERPOLATION.match(piece))
                    else NfLiteral(groovy_unescape(piece)) for _, piece in _pieces(text[1:-1])]
    return NfTemplate(tuple(
        NfLiteral(segment.value.replace("\\{", "{").replace("\\}", "}"))
        if isinstance(segment, NfLiteral) and not literal else segment
        for segment in segments
    ))


@dataclass(frozen=True, slots=True)
class _Process:
    """What a process block states. An input's path kind and array mark are its source's, so they come later."""

    head: _Line
    inputs: tuple[tuple[str, str, NfEntry | None], ...]
    scattered: tuple[str, ...]
    outputs: tuple[NfPort, ...]
    command: NfCommand
    container: str | None
    resources: NfResources


def _section(lines: list[_Line], index: int) -> int:
    """The index after the script or stub whose triple quote opens at ``lines[index]``."""
    end = index + 1
    while end < len(lines) and lines[end].verbatim and lines[end].text.strip() != '"""':
        end += 1
    if not lines[index].verbatim or end == len(lines) or not lines[end].verbatim:
        lines[index - 1].stop(_Code.IMPORT_FOREIGN_SCRIPT, "a script or stub that is not one triple-quoted string")
    return end + 1


def _input(found: dict[str, str]) -> tuple[str, str, bool]:
    stage = _options(found["options"]).get("stageAs")
    return found["name"], found["qualifier"], stage == f"'{NF_ENTRY_STAGING_PREFIX}{found['name']}/*'"


def _output(text: str, directories: str | tuple[str, ...]) -> NfPort:
    """The port an output declaration declares; ``directories`` names the outputs that are Directories."""
    element = Idioms.TUPLE_OUTPUT.match(text)
    if found := element or Idioms.OUTPUT.match(text):
        emit = found["emit"]
        if capture := Idioms.TEXT_CAPTURE.match(found["declaration"]):
            return NfPort(emit, "val", emit, NfTemplate((NfLiteral(_literal(capture["literal"])),)), capture="text")
        if path := (Idioms.PATH_ELEMENT if element else Idioms.PATH_OUTPUT).match(found["declaration"]):
            options = _options(path["options"])
            literal = options.get("glob") == "false"
            return NfPort(emit, "path", emit, _glob(path["glob"], literal),
                          "directory" if emit in directories else None,
                          capture="single" if literal and "arity" in options else None,
                          optional=options.get("optional") == "true" or options.get("arity") == "'0..*'")
    raise ValueError("an output Sophios does not write")


def _process(block: list[_Line]) -> _Process:  # pylint: disable=too-many-branches
    """Read a process block's directives, declarations and script, each value at the line that holds it."""
    head, section = block[0], "directive"
    container: str | None = None
    resources = NfResources()
    directories: str | tuple[str, ...] = ()
    inputs: list[tuple[str, str, bool]] = []
    scattered: list[str] = []
    outputs: list[NfPort] = []
    script: list[_Line] = []
    index = 1
    while index < len(block) - 1:
        line = block[index]
        index += 1
        try:
            if line.text in ("input:", "output:"):
                section = line.text[:-1]
            elif line.text in ("script:", "stub:"):
                start, index = index, _section(block, index)
                body = block[start + 1:index - 1]
                if line.text == "script:":
                    script = body
                elif [item.text for item in body] != [f"    {item}" for item in NF_STUB_LINES[2:-1]]:
                    line.stop(_Code.IMPORT_FOREIGN_SCRIPT, "a stub other than the one Sophios writes")
            elif section == "directive":
                if found := Idioms.CONTAINER.match(line.text):
                    if not (container := _literal(found["literal"])).strip():
                        raise ValueError("a container with no image")
                elif found := Idioms.CPUS.match(line.text):
                    resources = replace(resources, cpus=int(found["count"]))
                elif found := Idioms.ACCELERATOR.match(line.text):
                    resources = replace(resources, accelerator=int(found["count"]))
                elif found := Idioms.MEMORY.match(line.text):
                    memory = found["memory"]
                    resources = replace(resources, memory_mb=(float if "." in memory else int)(memory))
                elif found := Idioms.EXT.match(line.text):
                    ext: dict[str, Any] = {name: _strings(value) for name, value in _named(found["carriers"]).items()}
                    directories = ext.get(NF_DIRECTORY_CARRIER, ())
                    resources = replace(resources, **{field: ext[key] for key, field in NF_CUDA_CARRIERS if key in ext})
                elif not line.text.startswith(("containerOptions ", "beforeScript ")):
                    line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a process directive Sophios does not write")
            elif section == "input":
                if found := Idioms.TUPLE_INPUT.match(line.text):
                    elements = [Idioms.ELEMENT.match(element) for element in _split(found["elements"])]
                    if None in elements:
                        raise ValueError("an input Sophios does not write")
                    inputs.extend(_input(element) for element in elements if element)
                    scattered.extend(element["name"] for element in elements if element)
                elif found := Idioms.INPUT.match(line.text):
                    inputs.append(_input(found))
                else:
                    raise ValueError("an input Sophios does not write")
            else:
                outputs.append(_output(line.text, directories))
        except ValueError as exc:
            code = _Code.IMPORT_FOREIGN_STATEMENT if section == "directive" else _Code.IMPORT_FOREIGN_DECLARATION
            line.stop(code, str(exc))
    command, entries = _command(script, head)
    return _Process(
        head, tuple((name, qualifier, entries.get(name) if entry else None) for name, qualifier, entry in inputs),
        tuple(scattered), tuple(outputs), command, container, resources,
    )


def _chain(text: str) -> list[str]:
    """The channels ``a.combine(b).combine(c)`` joins, each without the box around it."""
    channels: list[str] = []
    while found := Idioms.COMBINE.match(text):
        text, channels = found["channels"], [found["channel"], *channels]
    return [boxed["source"] if (boxed := Idioms.BOXED.match(channel)) else channel for channel in [text, *channels]]


class _Channels:
    """The channels the workflow block names: the parameters, and each process output."""

    def __init__(self, parameters: dict[str, str], processes: dict[str, _Process]) -> None:
        self.parameters, self.processes = parameters, processes
        self.sources: dict[str, tuple[str | None, str]] = {
            idiom.fill(process=name, emit=port.name): (name, port.name)
            for name, process in processes.items() for port in process.outputs
            for idiom in (Idioms.PROCESS_OUTPUT, Idioms.CONDITIONAL_OUTPUT)
        }
        self.sources.update({name: (None, name) for name in parameters})

    def edge(self, text: str, process: str, port: str, adapter: str | None = None) -> NfSourceConnection:
        """The connection a channel makes into ``process.port``; a gathered one gathers, unless it is scattered."""
        if found := Idioms.GATHERED.match(text):
            text, adapter = found["source"], adapter or "gather"
        if text not in self.sources:
            raise ValueError(f"{text} is not a channel Sophios writes")
        source, name = self.sources[text]
        if source is None:
            return NfWorkflowInputConnection(name, process, port, adapter)
        return NfProcessConnection(source, name, process, port, adapter)

    def incoming(self, text: str, process: str, port: str) -> NfSourceConnection | NfStepInputConnection:
        """The connection a call argument makes into ``process.port``."""
        if found := Idioms.STEP_INPUT.match(text):
            pick = found["pick_value"]
            return NfStepInputConnection(
                tuple(self.edge(channel, process, port) for channel in _chain(found["channels"])),
                _literal(found["link_merge"]), None if pick == "null" else _literal(pick),
            )
        return self.edge(text.removesuffix(".first()"), process, port)


@dataclass(slots=True)
class _Main:
    """What the statements of ``main:`` say of each process, which they name, each channel with the line it is on."""

    calls: dict[str, tuple[_Line, list[str]]]
    scatters: dict[str, list[Any]]
    merges: dict[str, list[tuple[_Line, str]]]
    branches: dict[str, tuple[_Line, tuple[_Line, str], str]]


def _main(lines: list[_Line]) -> _Main:
    """The holes of each statement of ``main:``, which must be one the renderer writes."""
    main = _Main({}, {}, {}, {})
    index = 0
    while index < len(lines):
        line = lines[index]
        try:
            index = _statement(main, lines, index)
        except ValueError as exc:
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, f"a statement Sophios does not write, with {exc}")
    return main


def _statement(main: _Main, lines: list[_Line], index: int) -> int:
    """Read the statement that starts at ``lines[index]`` into ``main``; the index after it."""
    line = lines[index]
    if found := Idioms.SCATTER.match(line.text):
        index = next((end for end in range(index, len(lines)) if lines[end].text == "}"), len(lines) - 1)
        method = "dotproduct" if lines[index - 1].text.startswith("(0..<") else "flat_crossproduct"
        main.scatters[found["process"]] = [line, found["sources"], method]
    elif (found := Idioms.SHAPE.match(line.text)) and found["process"] in main.scatters:
        main.scatters[found["process"]][2] = "nested_crossproduct"
    elif found := Idioms.MERGE.match(line.text):
        main.merges[found["process"]] = [(line, found["first"]), (line, found["second"])]
        while index + 1 < len(lines) and (more := Idioms.MERGE_NEXT.match(lines[index + 1].text)):
            main.merges[found["process"]].append((lines[index + 1], more["argument"]))
            index += 1
    elif (found := Idioms.BRANCH.match(line.text)) and index + 3 < len(lines) and (
            run := Idioms.RUN.match(lines[index + 1].text)):
        main.branches[found["process"]] = (lines[index + 1], (line, found["combined"]), run["predicate"])
        index += 3
    elif found := Idioms.CALL.match(line.text):
        main.calls[found["process"]] = (line, _split(found["arguments"]) if found["arguments"] else [])
    elif not Idioms.MIX.match(line.text):
        line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a statement Sophios does not write")
    return index + 1


def _port(name: str, qualifier: str, entry: NfEntry | None, connection: Any, channels: "_Channels") -> NfPort:
    """An input port, whose path kind and array mark are its source's."""
    first = connection.sources[0] if isinstance(connection, NfStepInputConnection) else connection
    if isinstance(first, NfWorkflowInputConnection):
        channel = param_channel(channels.parameters[first.from_port])
        kind, array = channel.path_kind, channel.carries_list and first.adapter is None
    else:
        outputs = channels.processes[first.from_process].outputs
        kind = next(port.path_kind for port in outputs if port.name == first.from_port)
        array = first.adapter == "gather"
    if isinstance(connection, NfStepInputConnection):
        array = connection.gives_list
    return NfPort(name, qualifier, path_kind=kind if qualifier == "path" else None, is_array=array, entry=entry)


def _process_model(
    name: str, process: _Process, main: _Main, channels: _Channels
) -> tuple[NfProcess, list[NfConnection]]:
    """A process, with the connections into it the workflow block makes, each read at the line it is on."""
    line, texts = main.calls[name]
    branch = main.branches.get(name)
    arguments = [(line, text) for text in texts]
    incoming: dict[str, Any] = {}
    at = line
    try:
        if name in main.scatters:
            at, sources, method = main.scatters[name]
            for port, source in zip(process.scattered, _chain(sources), strict=True):
                incoming[port] = channels.edge(source, name, port, method)
            arguments = [(branch[1][0], channel) for channel in _chain(branch[1][1])[1:]] if branch else arguments[1:]
        elif branch:
            arguments = main.merges.get(name, [branch[1]])
        others = [port for port, _, _ in process.inputs if port not in process.scattered]
        for port, (at, argument) in zip(others, arguments, strict=True):
            incoming[port] = channels.incoming(argument, name, port)
        names = [port for port, _, _ in process.inputs]
        variables = {f"__w{index}": port for index, port in enumerate(names[:len(process.scattered) or None])}
        variables |= {f"__b{index}": port for index, port in enumerate(others)} if process.scattered else {}
    except ValueError as exc:
        at.stop(_Code.IMPORT_FOREIGN_STATEMENT, f"channels Sophios does not write ({exc})")
    try:
        inputs = [_port(port, qualifier, entry, incoming[port], channels) for port, qualifier, entry in process.inputs]
        model = NfProcess(name, inputs, process.outputs, process.command, process.container, process.resources)
    except (ValueError, TypeError, KeyError) as exc:
        process.head.stop(_Code.IMPORT_MISMATCH, f"a process Sophios does not write ({exc})")
    if branch is None:
        return model, list(incoming.values())
    try:
        # The process holds without its condition, so what it refuses with one is the condition, on its run: line,
        # as it is a condition nested deeper than Python recurses, which no compile writes.
        return replace(model, condition=read_groovy(branch[2], variables)), list(incoming.values())
    except (ValueError, RecursionError) as exc:
        branch[0].stop(_Code.IMPORT_FOREIGN_STATEMENT, f"a condition Sophios does not write ({exc})")


def _workflow(block: list[_Line], processes: dict[str, _Process], parameters: dict[str, str],
              params: dict[str, Any]) -> ExecutableNextflowWorkflow:
    """The model the named workflow block and the process blocks describe."""
    sections: dict[str, list[_Line]] = {"take": [], "main": [], "emit": []}
    section = "take"
    for line in block[1:-1]:
        if line.text in ("take:", "main:", "emit:"):
            section = line.text[:-1]
        else:
            sections[section].append(line)
    main, channels = _main(sections["main"]), _Channels(parameters, processes)
    built: list[NfProcess] = []
    connections: list[NfConnection] = []
    for name, (line, _) in main.calls.items():
        if name not in processes:
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a call of no process the pipeline declares")
        process, incoming = _process_model(name, processes[name], main, channels)
        built.append(process)
        connections.extend(incoming)
    for line in sections["emit"]:
        found = Idioms.EMIT.match(line.text)
        source = _SOURCE.match(found["expression"]) if found else None
        process_name, port = channels.sources.get(source.group() if source else "", (None, ""))
        if found is None or process_name is None:
            line.stop(_Code.IMPORT_FOREIGN_STATEMENT, "a workflow output Sophios does not write")
        connections.append(NfWorkflowOutputConnection(process_name, port, found["output"]))
    try:
        return ExecutableNextflowWorkflow(block[0].text.split(" ")[1], built, connections, params, parameters)
    except (ValueError, TypeError) as exc:
        block[0].stop(_Code.IMPORT_MISMATCH, f"a workflow Sophios does not write ({exc})")


def _float(value: Any) -> Any:
    """A float parameter's value: its floats are their Python text."""
    if isinstance(value, list):
        return [_float(item) for item in value]
    if isinstance(value, str):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{value} is not a finite float")
        return number
    return value


def _written(name: str, declared: str, value: Any) -> bool:
    """Whether ``value`` is what a compile writes for a parameter of type ``declared``: a job value its coercion
    keeps as it is, and ``[]`` for an absent optional other than a File or Directory, which has no lowering.
    Compared as JSON, where an int is not the float it equals."""
    absent = declared.endswith("?") and value == [] and declared not in ("File?", "Directory?")
    try:
        kept = coerce_job_value(AuthoredName(name), PortDeclaration(PortType(declared)), None if absent else value)
    except SophiosError:
        return False
    return json.dumps([] if kept is None else kept, sort_keys=True) == json.dumps(value, sort_keys=True)


def _read(path: Path, code: SophiosErrorCode) -> str:
    """A file's text: CR LF ends a line, as Groovy reads it, and a lone CR does not. A byte that is not UTF-8,
    which Sophios never writes, stops the read with ``code`` at its line."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        number = data.count(b"\n", 0, exc.start) + 1
        line = data.split(b"\n")[number - 1].decode("utf-8", "replace")
        _Line(str(path), number, line).stop(code, f"text that is not UTF-8 ({exc.reason})")


def _key(file: str, text: str, key: str | None) -> _Line:
    """The line of the params file that holds ``key`` as a key, not as a value, or its first line."""
    lines = text.split("\n")
    number = next((number for number, line in enumerate(lines, 1)
                   if key is not None and re.search(rf'"{re.escape(key)}"\s*:', line)), 1)
    return _Line(file, number, lines[number - 1])


def _params(file: str, text: str, parameters: dict[str, str]) -> dict[str, Any]:
    """The parameter values the params file holds, one for each parameter the entry block checks, as a compile
    writes it for the parameter's type."""
    depth = 0
    for found in _JSON_BRACKET.finditer(text):
        depth += {"[": 1, "{": 1, "]": -1, "}": -1}.get(found.group(), 0)
        if depth > 3:  # the parameters, a list of values, a File or Directory: Sophios nests nothing deeper
            number = text.count("\n", 0, found.start()) + 1
            line = _Line(file, number, text.split("\n")[number - 1])
            line.stop(_Code.IMPORT_PARAMS, "JSON nested deeper than Sophios writes")
    try:
        values = json.loads(text)
    except json.JSONDecodeError as exc:
        _Line(file, exc.lineno, text.split("\n")[exc.lineno - 1]).stop(_Code.IMPORT_PARAMS, f"not JSON, {exc.msg}")
    except ValueError as exc:  # an integer with more digits than Python converts
        _key(file, text, None).stop(_Code.IMPORT_PARAMS, f"not JSON Sophios writes, {exc}")
    if not isinstance(values, dict):
        _key(file, text, None).stop(_Code.IMPORT_PARAMS, "not a JSON object")
    if extra := sorted(values.keys() - parameters.keys()):
        _key(file, text, extra[0]).stop(_Code.IMPORT_PARAMS, f"{extra[0]!r}, a parameter the pipeline does not check")
    if missing := sorted(parameters.keys() - values.keys()):
        _key(file, text, None).stop(_Code.IMPORT_PARAMS, f"no value for {missing[0]!r}, which the pipeline checks")
    read = {}
    for name, value in values.items():
        try:
            read[name] = _float(value) if parameters[name].removesuffix("?").removesuffix("[]") == "float" else value
        except ValueError as exc:
            _key(file, text, name).stop(_Code.IMPORT_PARAMS, f"{exc}, which a float parameter takes")
        if not _written(name, parameters[name], read[name]):
            _key(file, text, name).stop(_Code.IMPORT_PARAMS,
                                        f"not a value Sophios writes for a parameter of type {parameters[name]!r}")
    return read


def _same(given: list[_Line], expected: list[_Line], code: SophiosErrorCode, head: _Line) -> None:
    """Stop at the first line of ``given`` that is not the rendering's."""
    for line, rendered in zip(given, expected):
        if line.text != rendered.text:
            line.stop(code, f"text Sophios does not write here; it writes {rendered.text.split(chr(10))[0]!r}")
    if len(given) > len(expected):
        given[len(expected)].stop(code, "text Sophios does not write here")
    if len(given) < len(expected):
        (given[-1] if given else head).stop(code, f"Sophios writes {expected[len(given)].text!r} after this")


def read_nextflow(directory: str | Path) -> tuple[ExecutableNextflowWorkflow, dict[str, SourceSpan]]:
    """Read the pipeline Sophios wrote into ``directory`` back into the model it was written from.

    The reader is the renderer's inverse (design §4.2): from the three files
    Nextflow reads it returns the model they were written from, and that
    model renders the same files, up to layout. It reads nothing else:
    not ``nextflow_workflow.json``, no tool registry, no network, and no
    path a parameter names. Beside the model, it gives the line each
    process block and the named workflow block begins at, by name.

    Args:
        directory (str | Path): The directory holding ``workflow.nf``,
            ``nextflow_params.json`` and ``nextflow.config``.

    Raises:
        SophiosError: The files are not what Sophios writes, with a code
            from wic047 to wic053 at the first line that is not.
        OSError: A file cannot be read.

    Returns:
        tuple[ExecutableNextflowWorkflow, dict[str, SourceSpan]]: The model the files were written from, and where
            each of its processes and its workflow begins.
    """
    folder = Path(directory)
    script_file = str(folder / NEXTFLOW_SCRIPT)
    script = _lines(script_file, _read(folder / NEXTFLOW_SCRIPT, _Code.IMPORT_FOREIGN_STATEMENT))
    others, blocks, workflows = _top(script)
    start = script[0] if script else _Line(script_file, 1, "")
    if len(workflows) != 2 or workflows[1][0].text != "workflow {" or workflows[0][0].text == "workflow {":
        start.stop(_Code.IMPORT_FOREIGN_STATEMENT, "not one named workflow and then the entry workflow")
    checks = [(line, found["name"], _literal(found["type"]))
              for line in workflows[1] for found in Idioms.CHECKED.find(line.text)]
    if unknown := [line for line, _, declared in checks if declared not in PARAM_TYPES]:
        unknown[0].stop(_Code.IMPORT_FOREIGN_STATEMENT, "a parameter type Sophios does not write")
    parameters = {name: declared for _, name, declared in checks}
    params_file = str(folder / NEXTFLOW_PARAMS)
    params_text = _read(folder / NEXTFLOW_PARAMS, _Code.IMPORT_PARAMS)
    params = _params(params_file, params_text, parameters)
    try:  # a condition its run: line reads that is too deep to build into the workflow or render, past that line
        model = _workflow(workflows[0], {name: _process(block) for name, block in blocks.items()}, parameters, params)
        rendered, written = render_nextflow(model), nextflow_params(model)
    except RecursionError as exc:
        workflows[0][0].stop(_Code.IMPORT_FOREIGN_STATEMENT, f"a condition Sophios does not write ({exc})")
    rendered_others, rendered_blocks, _ = _top(_lines(script_file, rendered))
    _same(others, rendered_others, _Code.IMPORT_MISMATCH, start)
    for name, block in blocks.items():
        _same(block, rendered_blocks.get(name, []), _Code.IMPORT_MISMATCH, block[0])
    given = json.loads(params_text)
    for name in sorted(given):
        if json.dumps(written[name], sort_keys=True) != json.dumps(given[name], sort_keys=True):
            _key(params_file, params_text, name).stop(_Code.IMPORT_PARAMS, f"not the value Sophios writes for {name!r}")
    config_file = str(folder / NEXTFLOW_CONFIG)
    config = _lines(config_file, _read(folder / NEXTFLOW_CONFIG, _Code.IMPORT_CONFIG))
    _same(config, _lines(config_file, render_nextflow_config(model)), _Code.IMPORT_CONFIG,
          _Line(config_file, 1, ""))
    return model, {name: block[0].span for name, block in blocks.items()} | {model.name: workflows[0][0].span}
