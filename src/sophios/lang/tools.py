"""A tool as the middle end and every target but the CWL one hold it: a node of Sophios's own, with no CWL in it.

`ToolNode` is a tool's interface (its ports, whether it is a workflow) and its body. The body, what the tool runs and
needs, is read once by the front door's readers (`lang.cwl_tool`, `lang.wic_tool`) where they read the tool; what the
node cannot say stays in `passthrough`, which only the CWL target reads. Nothing here imports the front door or a
target.
"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .needs import FEATURES, Inherited
from .nodes import OpaqueCwl, ToolDocument
from .types import PortDeclaration, Type


class Stream(StrEnum):
    """The stream of a command that an output is."""

    STDOUT = 'stdout'
    STDERR = 'stderr'


class BodyKind(StrEnum):
    """Whether a tool runs a command line, or is something else a target refuses as a whole."""

    COMMAND = 'command'
    OTHER = 'other'


class NeedKind(StrEnum):
    """What a requirement or hint of a tool asks for, of the closed set Sophios's targets read; any other is OTHER."""

    CONTAINER = 'container'
    ENVIRONMENT = 'environment'
    STAGING = 'staging'
    RESOURCES = 'resources'
    ACCELERATOR = 'accelerator'
    NETWORK = 'network'
    SHELL = 'shell'
    OTHER = 'other'


class Part(StrEnum):
    """What of an input a reference to it names: its value, its path, or its base name."""

    VALUE = 'value'
    PATH = 'path'
    BASENAME = 'basename'


@dataclass(frozen=True, slots=True)
class Reference:
    """A reference to the input `name` of the tool, inside a word."""

    name: str
    part: Part = Part.VALUE


@dataclass(frozen=True, slots=True)
class Word:
    """A word of a command line or a path: literal text and references to inputs, in order. A literal part is there
    wherever the text begins, ends or runs on between references, empty or not."""

    parts: tuple[str | Reference, ...]

    @classmethod
    def literal(cls, text: str) -> 'Word':
        """The word that is `text` and nothing else."""
        return cls((text,))

    @property
    def names(self) -> tuple[str, ...]:
        """The inputs it refers to, in order of first mention."""
        return tuple(dict.fromkeys(part.name for part in self.parts if isinstance(part, Reference)))

    @property
    def spelled(self) -> str:
        """Its text with each reference as the `$(inputs.name)` the language writes it, for a message to quote."""
        suffixes = {Part.VALUE: '', Part.PATH: '.path', Part.BASENAME: '.basename'}
        return ''.join(part if isinstance(part, str) else f'$(inputs.{part.name}{suffixes[part.part]})'
                       for part in self.parts)

    @property
    def text(self) -> str | None:
        """Its text when it refers to no input, else None."""
        texts = [part for part in self.parts if isinstance(part, str)]
        return ''.join(texts) if len(texts) == len(self.parts) else None


class Collect(StrEnum):
    """What a command's output file is made into: the first file its pattern matches, or the text of it."""

    FIRST = 'first'
    TEXT = 'text'


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a value lands on a command line: an input's binding, or an argument's. Each value is as written, never
    coerced; `unread` names the written fields it holds no value of. A `value_from` no word holds is `unread_value`,
    the text as written, which no target interprets."""

    position: int | str | None = None
    prefix: str | None = None
    separate: bool | None = None
    item_separator: str | None = None
    shell_quote: bool | None = None
    value_from: Word | None = None
    unread: tuple[str, ...] = ()
    unread_value: str | None = None


#: A command line word: bare text, or text and where it lands.
Arg = str | Placement


@dataclass(frozen=True, slots=True)
class Capture:
    """How an output is collected: the stream of the command it is, or by `glob`, and what is made of the file. A glob
    no word holds (a list, an empty text, an expression) is `unread_glob`, and an `output_eval` that is neither of
    `Collect`'s is `unread_eval`, each as written, which no target interprets."""

    stream: Stream | None = None
    glob: Word | None = None
    load_contents: bool | None = None
    output_eval: Collect | None = None
    unread: tuple[str, ...] = ()
    unread_glob: str | list[str] | None = None
    unread_eval: str | None = None


@dataclass(frozen=True, slots=True)
class ToolInput:
    """An input of the body: its name as the tool names it, its type, its default (None for none) and its binding;
    `schemas` holds the written fields of each array schema the type is made of."""

    name: str
    type: Type
    default: OpaqueCwl = None
    placement: Placement | None = None
    unread: tuple[str, ...] = ()
    schemas: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """An output of the body; `capture` is None for one that says no way to collect it."""

    name: str
    type: Type
    capture: Capture | None = None
    unread: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Container:
    """The image a tool runs in: `pull` or else `image_id`, as written."""

    pull: str | None
    image_id: str | None


@dataclass(frozen=True, slots=True)
class Variable:
    """One environment variable of a tool; `value` is text, which may name an input."""

    name: str
    value: str
    unread: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Variables:
    """The environment a tool sets."""

    entries: tuple[Variable, ...]


@dataclass(frozen=True, slots=True)
class Dirent:
    """A staged entry that is written out: what is staged, under which name, and whether the task may write to it."""

    entry: str
    entryname: str | None = None
    writable: bool | None = None
    unread: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Generated:
    """A staged file made of literal text: its name and its text, whatever the inputs."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class Other:
    """A staged entry of a kind no node holds: what it is computed from stays with the CWL target."""


#: One entry of what a tool stages in its working directory: a bare reference to an input, a `Dirent`, a file made of
#: literal text, or anything else.
Staged = str | Dirent | Generated | Other


@dataclass(frozen=True, slots=True)
class Staging:
    """What a tool stages before its command runs; `entries` is None where the listing is not a list."""

    entries: tuple[Staged, ...] | None


@dataclass(frozen=True, slots=True)
class Resources:
    """What a tool asks of the machine, as written (an int, a string that may be an expression, or None)."""

    cores_min: OpaqueCwl = None
    cores_max: OpaqueCwl = None
    ram_min: OpaqueCwl = None
    ram_max: OpaqueCwl = None


@dataclass(frozen=True, slots=True)
class Accelerator:
    """A tool's use of an accelerator, with the vendor's own fields as written."""

    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Network:
    """A tool's use of the network: whether it may, as written."""

    allowed: OpaqueCwl


@dataclass(frozen=True, slots=True)
class Need:
    """One requirement or hint of a tool, in the order it was written. `place` is where it is written (its key in a
    mapping, its index in a list) and `name` what it is called there, which a refusal prints; `understood` is False
    for a hint no class of the standard reads. `unread` names the written fields no value of `body` holds."""

    place: str
    name: str
    hint: bool
    kind: NeedKind
    understood: bool = True
    unread: tuple[str, ...] = ()
    body: Container | Variables | Staging | Resources | Accelerator | Network | None = None


@dataclass(frozen=True, slots=True)
class ToolBody:  # pylint: disable=too-many-instance-attributes
    """What a tool runs, and what it needs to run it, in the order the tool wrote them."""

    kind: BodyKind
    #: What the tool says it is, for a refusal to print.
    written_kind: str
    unread: tuple[str, ...]
    base: str | tuple[str, ...] | None
    arguments: tuple[Arg, ...]
    stdin: str | None
    stdout: str | None
    stderr: str | None
    inputs: tuple[ToolInput, ...]
    #: Empty for a body that is not a command line.
    outputs: tuple[ToolOutput, ...]
    #: Requirements in the order written, then hints in the order written.
    needs: tuple[Need, ...]
    #: The (place, name) of each need an enclosing step or workflow gave: `requirements.<name>` or `hints.<name>`.
    inherited: tuple[tuple[str, str], ...] = ()

    def need(self, kind: NeedKind) -> Need | None:
        """The first requirement of `kind`, else the first hint of it."""
        found = [each for each in self.needs if each.kind is kind and each.understood]
        return next((each for each in found if not each.hint), found[0] if found else None)

    def extension(self, name: str) -> Accelerator | None:
        """The vendor's `name` requirement, else its hint, as an accelerator."""
        return next((each.body for each in self.needs
                     if each.name == name and isinstance(each.body, Accelerator)), None)


@dataclass(frozen=True, slots=True)
class ToolPort:
    """A port of the tool's interface, read without reading its body."""

    name: str
    declaration: PortDeclaration


@dataclass(frozen=True)
class ToolNode:  # pylint: disable=too-many-instance-attributes
    """A tool: where it is, its interface, and its body.

    `document` is the tool as `.wic` says it, when it is written there; `passthrough` is the document of a tool
    written in CWL as it was read, opaque: which of the two a tool has is how the CWL target writes it. Neither is
    read by the middle end or by any other target, which read the body, that the front door read once where it read the
    tool. A tool the standard's loader refuses has no body, and holds the loader's refusal for a target that needs the
    body to raise.
    """

    run_path: str
    inputs: tuple[ToolPort, ...]
    outputs: tuple[ToolPort, ...]
    #: Whether it is a workflow, which a step cannot run as a tool.
    workflow: bool
    namespaces: tuple[tuple[str, OpaqueCwl], ...]
    document: ToolDocument | None
    passthrough: tuple[tuple[str, OpaqueCwl], ...]
    own: ToolBody | None
    refused: Exception | None = None

    def body(self, inherited: tuple[Inherited, ...] = ()) -> ToolBody:
        """The tool's body, with each requirement and hint in `inherited` (outermost first) added unless the tool
        names one of that class that wins over it: a requirement of the tool wins over either, its hint over a hint.

        Raises:
            SophiosError: `api004`, when the standard's loader refused the tool or an inherited body (or what else
                stopped the front door reading it).
        """
        if self.own is None:
            assert self.refused is not None  # a tool is read, or refused
            raise self.refused
        requirements = [need for need in self.own.needs if not need.hint]
        hints = [need for need in self.own.needs if need.hint]
        places: list[tuple[str, str]] = []
        for each in inherited:
            named = {need.name for need in (*requirements, *(hints if each.hint else ())) if need.understood}
            if each.name in FEATURES or each.name in named:
                continue
            if each.need is None:
                assert each.refused is not None  # an inherited body is read, or refused
                raise each.refused
            (hints if each.hint else requirements).append(each.need)
            places.append((f'{"hints" if each.hint else "requirements"}.{each.name}', each.name))
        return replace(self.own, needs=(*requirements, *hints), inherited=tuple(places)) if places else self.own
