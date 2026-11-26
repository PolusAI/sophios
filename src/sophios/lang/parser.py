"""Parse `.wic` source into a typed AST.

The parser composes YAML to a node tree rather than loading it to plain
Python objects, because composition preserves the source marks that make
diagnostics worth reading. Nothing here raises: a caller always receives a
result carrying whatever was parsed plus whatever went wrong.
"""
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import chain
from types import MappingProxyType
from typing import Any, Final, Iterator, Mapping, NoReturn, TypedDict, cast, final, get_args

import yaml
from jsonschema import Draft202012Validator

from ..utils_yaml import KEY_OF_TAG, Key, Tag
from .diagnostics import Diagnostics
from .error_codes import SophiosErrorCode
from .nodes import (
    Document,
    EdgeDef,
    EdgeRef,
    GraphvizEntry,
    InlineLiteral,
    InputRecord,
    InputValue,
    OutputBinding,
    PassthroughRef,
    Port,
    Step,
    StepKey,
    ToolDocument,
    UnresolvedName,
    Value,
    WicSidecar,
    Word,
)
from . import wic_tool
from .cwl import CWL_VERSIONS, SIZE
from .spans import SourceSpan
from .subset import EXPRESSION
from .support import STEP_INPUT_RECORD_KEYS
from .types import SCATTER_METHODS
from .values import read_value

#: A `graphviz: style:` value: one or more styles, comma separated.
_GRAPHVIZ_STYLE: Final = ('((,\\s*)*(dashed|dotted|solid|invis|bold|tapered|filled|striped|wedged'
                          '|diagonals|rounded))+')
#: A `graphviz: ranksame:` entry: a `wic: steps:` key, `(index, name)`.
_RANKSAME_ENTRY: Final = '\\([0-9]+, [A-Za-z0-9_\\.]+\\)'

#: The JSON Schema keywords Sophios writes: the exported schema, and the `wic:` value schemas the parser checks.
JsonSchema = TypedDict('JsonSchema', {
    '$schema': str, '$id': str, '$ref': str, '$defs': 'dict[str, JsonSchema]',
    'title': str, 'description': str, 'type': 'str | list[str]', 'enum': list[str], 'pattern': str,
    'minLength': int, 'exclusiveMinimum': int, 'minProperties': int, 'maxProperties': int, 'required': list[str],
    'properties': 'dict[str, JsonSchema]', 'patternProperties': 'dict[str, JsonSchema]',
    'additionalProperties': 'bool | JsonSchema', 'items': 'JsonSchema',
    'oneOf': 'list[JsonSchema]', 'anyOf': 'list[JsonSchema]', 'not': 'JsonSchema',
}, total=False)

_NON_EMPTY: Final[JsonSchema] = {'type': 'string', 'minLength': 1}
#: Where a value ends: nothing follows it. `$` stops before the line break a block scalar ends in.
_END: Final = '(?![\\s\\S])'
#: What a tool needs of a resource: a positive number, or a CWL expression.
_AMOUNT: Final[JsonSchema] = {'type': ['number', 'string'], 'exclusiveMinimum': 0,
                              'pattern': f'^\\s*{EXPRESSION.pattern}',
                              'description': 'a positive number or a CWL expression'}


#: A port of a tool, as its `inputs:` and as its `outputs:` write one under its name.
_PORTS: Final[Mapping[str, JsonSchema]] = MappingProxyType({
    'inputs': {'type': ['string', 'array', 'object'], 'properties': {'inputBinding': {'type': 'object'}},
               'description': 'a type or a CWL port (in a flow mapping a comma starts another input: write the '
                              'port on a line of its own, or quote it)'},
    'outputs': {'type': ['string', 'object'], 'properties': {'from': {'type': 'string'}},
                'description': 'a line (`stdout`, `stderr` or a pattern such as `{name}.txt`) or a CWL port'}})
#: The two sections of them: a mapping of each name to its port, or CWL's list of ports, each a mapping named by its
#: `id:`, whose name is what follows the last `#`; and no value is no port.
_SECTIONS: Final[Mapping[str, JsonSchema]] = MappingProxyType({key: {
    'type': ['object', 'array', 'null'], 'additionalProperties': port,
    'description': 'a mapping of names to ports, or a list of ports each named by its `id:`', 'items': {
        **port, 'type': 'object', 'required': ['id'],
        'properties': {**port['properties'], 'id': {'type': 'string', 'pattern': '[^#]$'}}}}
    for key, port in _PORTS.items()})


@final
class Grammar:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The syntax layer's fixed vocabulary.

    A namespace rather than loose module constants, since these describe one
    thing — what the language admits. Every member is immutable and built
    once at import.
    """

    #: CWL keys on a step that Sophios reads and acts upon; everything else
    #: on a step is passthrough, by definition, bar `STEP_LISTED_KEYS`, whose
    #: list form is read as a mapping.
    INTERPRETED_STEP_KEYS: Final = frozenset({'scatter', 'scatterMethod', 'when', 'run'})

    #: CWL keys whose entries are named, written either as a mapping or as a
    #: list. Each maps to the key that names a list entry: `id` for a port,
    #: `class` for a requirement. The parser reads both spellings as the mapping.
    LISTED_BY: Final[Mapping[str, str]] = MappingProxyType({
        'inputs': 'id', 'outputs': 'id', 'requirements': 'class', 'hints': 'class'})

    #: The ones a step carries; a step's ports are `in:` and `out:`.
    STEP_LISTED_KEYS: Final = frozenset({'requirements', 'hints'})

    #: The keys of a list entry that names no `id:` or `class:` until cwltool has
    #: read the file it points at. A list holding one is left as written.
    IMPORT_KEYS: Final = frozenset({'$import', '$include'})

    #: Every key a step carries in its own right. A key here cannot also be a
    #: step's name in a sequence entry, which is how `wic006` tells a
    #: forgotten `id:` from a step called `run`.
    STEP_KEYS: Final = frozenset({'id', 'in', 'out'}) | INTERPRETED_STEP_KEYS

    #: The desugared keys an input may be spelled with (language spec §2). `wic_anchor` is absent: an edge is
    #: defined on an `out:` entry only (.wic reference §3.6).
    DESUGARED_KEYS: Final = Key.ALL - {Key.ANCHOR}

    #: Every value key a `wic:` block admits (.wic reference §7), with the JSON Schema of its
    #: value. The block is Sophios's own metadata, not passthrough CWL, so it
    #: is closed: any other key is `wic033`, and a value its schema refuses is
    #: `wic034`. The exported schema states the same table.
    SIDECAR_VALUES: Final[Mapping[str, JsonSchema]] = MappingProxyType({
        'graphviz': {'type': 'object',
                     'properties': {'label': _NON_EMPTY,
                                    'style': {'type': 'string', 'pattern': _GRAPHVIZ_STYLE},
                                    'ranksame': {'type': 'array',
                                                 'items': {'type': 'string', 'pattern': _RANKSAME_ENTRY}}},
                     'additionalProperties': False},
        'implementation': _NON_EMPTY,
        'implementations': {'type': 'object'},
        'default_implementation': _NON_EMPTY,
        'version': _NON_EMPTY,
        'lang_version': _NON_EMPTY,
        'driver': {'enum': ['slurm', 'argo']},
        'namespace': _NON_EMPTY,
        'inlineable': {'type': 'boolean'},
    })

    #: What a `wic: steps:` entry admits: a `wic:` block's values, plus what
    #: it says about the step it names -- the step keys it may override, and
    #: that step's `inference:` rules. Siblings of the entry's own `wic:`
    #: wrapper are folded into the one block it parses to.
    SIDECAR_STEP_VALUES: Final[Mapping[str, JsonSchema]] = MappingProxyType({
        **SIDECAR_VALUES,
        'in': {},
        'out': {},
        'scatter': {},
        'scatterMethod': {'enum': sorted(SCATTER_METHODS)},
        'inference': {},
    })

    #: The keys that say what a tool runs. One of them makes a document, or a step's inline `run:` body, a tool, as
    #: `steps` makes a document a workflow.
    TOOL_RUNS: Final = ('command', 'script')

    #: Every key of its own a tool has (.wic reference §2.4), with the JSON Schema of its value; any other key of a
    #: tool is CWL, copied. The parser checks each value (`wic059`, in the words of its `description` when it has
    #: one) and the exported schema states the same table.
    TOOL_VALUES: Final[Mapping[str, JsonSchema]] = MappingProxyType({
        'command': {'type': ['string', 'array'], 'items': {'type': 'string'},
                    'description': 'a command line as you would type it, or the list of its words'},
        'script': {'type': 'string'},
        **_SECTIONS,
        'docker': {'type': 'string', 'pattern': f'^[^\\s,]+{_END}', 'description': 'one image, such as python:3.12'},
        'needs': {'type': 'object', 'additionalProperties': False, 'properties': {'cores': _AMOUNT, 'ram': {
            **_AMOUNT, 'pattern': f'^{SIZE.pattern}{_END}|{_AMOUNT["pattern"]}',
            'description': 'a positive number of mebibytes, a size such as 512M, 64G or 1T, or a CWL expression'}}},
    })

    #: A word of a command line as typed (.wic reference §2.4): text, 'quoted' or "quoted" text and `{name}`
    #: placeholders, up to a space; or a quote that never closes.
    LINE_WORD: Final = re.compile(r"""(?:'[^']*'|"[^"]*"|\{[^{}]*\}|[^\s'"])+|\S""")
    #: What a line holds beside its text: `{{` and `}}`, the braces they are; `{name}`, an input; a brace alone.
    LINE_PLACE: Final = re.compile(r'\{\{|\}\}|\{([^{}]*)\}|[{}]')
    #: What a word of a typed line holds only between quotes: a space, what only a shell reads, a quote; or nothing.
    NEEDS_A_QUOTE: Final = re.compile(r"""[\s|&;<>()`$*?\[\]\\'"]|^$""")

    #: `wic:` sidecar step keys have the surface form "(1, step_name)".
    WIC_STEP_KEY: Final = re.compile(r'^\(\s*(\d+)\s*,\s*(.+?)\s*\)$')

    #: The other spelling: a bare step id, for a step whose id is unique.
    WIC_STEP_ID: Final = re.compile(r'^[A-Za-z0-9_.-]+$')

    # No SCALAR_TAGS table here: scalar resolution is delegated to PyYAML's
    # SafeConstructor (see _resolved_scalar) to avoid diverging from it.


@dataclass(frozen=True, slots=True)
class ParseResult:
    """The outcome of parsing one document.

    `document` is the workflow the text is, and `tool` the tool it is when it
    says `command:` or `script:`: one of the two. Both are None only when
    nothing could be recovered — malformed YAML, or a root that is not a
    mapping. Otherwise one is present even alongside errors, so a caller can
    report several problems at once.
    """

    document: Document | None
    diagnostics: Diagnostics
    tool: ToolDocument | None = None


def _every_node(root: yaml.nodes.Node) -> list[yaml.nodes.Node]:
    """Return every distinct node in the composed graph, once, tracked by identity."""
    seen: dict[int, yaml.nodes.Node] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if id(node) not in seen:
            seen[id(node)] = node
            if isinstance(node, yaml.nodes.SequenceNode):
                stack.extend(node.value)
            elif isinstance(node, yaml.nodes.MappingNode):
                stack.extend(chain.from_iterable(node.value))
    return list(seen.values())


#: What the loader's scalar constructors raise, with no mark, on a value they
#: cannot read: `!!int abc`, `!!bool x`, an empty `!!float`, `2020-13-45`.
_UNREADABLE: Final = (ValueError, KeyError, AttributeError, IndexError)

_MERGE_TAG: Final = 'tag:yaml.org,2002:merge'


class _Unbuildable(yaml.constructor.ConstructorError):
    """A node the loader cannot build, already worded for `wic009`."""


class _Loader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # SafeLoader's own depth
    """`yaml.SafeLoader`, run over an already composed tree.

    The constructors are YAML's own, so whatever YAML builds or rejects, this
    does too. A `!` tag builds nothing, since the parser reads each one
    itself; the content of a tagged collection is still YAML, and is built.
    A failure YAML raises with no mark is raised at the node that failed.
    """

    yaml_constructors = dict(yaml.SafeLoader.yaml_constructors)
    yaml_multi_constructors = dict(yaml.SafeLoader.yaml_multi_constructors)

    def construct_object(self, node: yaml.nodes.Node, deep: bool = False) -> object:
        try:
            return super().construct_object(node, deep)
        except _UNREADABLE as exc:
            raise _Unbuildable(None, None, f'YAML cannot read this value as {_tag_name(node.tag)}',
                               node.start_mark) from exc

    def construct_undefined(self, node: yaml.nodes.Node) -> NoReturn:
        problem = ('`!!merge` belongs on a `<<` key; YAML cannot construct it as a value'
                   if node.tag == _MERGE_TAG else f'unknown tag {node.tag!r}; YAML cannot construct it')
        raise _Unbuildable(None, None, problem, node.start_mark)

    def construct_sophios_tag(self, _suffix: str, node: yaml.nodes.Node) -> None:
        """Build the content of a `!` tagged collection as YAML, and nothing for the tag."""
        match node:
            case yaml.nodes.MappingNode():
                self.construct_mapping(node, deep=True)
            case yaml.nodes.SequenceNode():
                self.construct_sequence(node, deep=True)


_Loader.yaml_constructors[None] = _Loader.construct_undefined  # what a tag with no constructor gets
_Loader.add_multi_constructor('!', _Loader.construct_sophios_tag)


def _tag_name(tag: str) -> str:
    """A core tag as its short name (`timestamp`), any other as written."""
    prefix = 'tag:yaml.org,2002:'
    return tag.removeprefix(prefix) if tag.startswith(prefix) and len(tag) > len(prefix) else repr(tag)


def _load(root: yaml.nodes.Node, nodes: list[yaml.nodes.Node], whole: SourceSpan) -> tuple[str, SourceSpan] | None:
    """Build the composed tree with the loader, and return the first thing it cannot build, and where.

    Only the first failure is known, since the loader stops there. Building a
    mapping applies its `<<` merges to the tree in place, and the tree is left
    so, for the parser to read the entries the loader reads; a mapping the
    loader did not reach before it stopped is merged the same way after.
    """
    merging = [(node, written) for node in nodes if isinstance(node, yaml.nodes.MappingNode)
               and (written := sum(key.tag != _MERGE_TAG for key, _ in node.value)) < len(node.value)]
    loader = _Loader('')
    failure = None
    try:
        loader.construct_document(root)
    except yaml.MarkedYAMLError as exc:
        problem = exc.problem if isinstance(exc, _Unbuildable) else f'YAML cannot load this: {exc.problem}'
        failure = str(problem), _error_span(exc, whole)
    for node, written in merging:
        try:
            loader.flatten_mapping(node)  # nothing left to merge, unless the loader stopped first
        except yaml.YAMLError:
            continue  # a merge the loader cannot apply; the mapping is read as it is left
        _drop_overridden(node, written)
    return failure


def _drop_overridden(node: yaml.nodes.MappingNode, written: int) -> None:
    """Keep each key once, in the place and with the entry the loader's dict gives it.

    The loader has put the merged entries first and the mapping's own
    `written` entries after them. A dict keeps a key where it first comes and
    takes its last value, so a merged key stays in its merged place and holds
    the mapping's own entry when the mapping overrides it. A key the parser
    then finds twice is one the author wrote twice.
    """
    merged = {str(key.value): (key, value) for key, value in node.value[:len(node.value) - written]}
    own, overriding = [], set()
    for key, value in node.value[len(node.value) - written:]:
        if (name := str(key.value)) in merged and name not in overriding:
            merged[name] = key, value
            overriding.add(name)
        else:
            own.append((key, value))
    node.value = [*merged.values(), *own]


def _start(span: SourceSpan | None) -> tuple[int, int]:
    """Where a span starts; every diagnostic of the parser has one."""
    return (span.start_line, span.start_column) if span else (0, 0)


def parse(text: str, filename: str = '<string>') -> ParseResult:
    """Parse `.wic` source text into a `Document`, or into a `ToolDocument` when it says `command:` or `script:`.

    This function is total: it never raises, for any input. Malformed YAML,
    wrong node kinds, and unknown tags are all reported as diagnostics, in
    reading order, so a reader works down the file once.
    """
    reader = _Reader(filename, text)
    whole = SourceSpan(filename, 1, 1, text.count('\n') + 1, 1)
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        reader.diags.error(SophiosErrorCode.INVALID_YAML, str(getattr(exc, 'problem', None) or exc).splitlines()[0],
                           _error_span(exc, whole))
        return ParseResult(None, reader.diags)
    if root is None:  # An empty document is well-formed and carries nothing.
        return ParseResult(Document(span=whole), reader.diags)

    # A tag the language does not own is reported on every node, not at each position that reads one, so a
    # position added later is covered by default. The payload is kept, untagged, for recovery.
    nodes = _every_node(root)
    for node in nodes:
        if node.tag.startswith('!') and node.tag not in Tag.ALL:
            reader.error(SophiosErrorCode.UNKNOWN_TAG,
                         f'unknown tag {node.tag!r}; the Sophios tags are !ii, !&, !*, and !cwl', node)
    failure = _load(root, nodes, reader.span(root))
    document = tool = None
    if isinstance(root, yaml.nodes.MappingNode):
        if reader.runs(root):
            tool = reader.tool(root)
        else:
            document = reader.document(root)
    else:
        reader.error(SophiosErrorCode.NOT_A_MAPPING, f'a Sophios document must be a mapping, found {_kind(root)}', root)
    # What the loader cannot build is reported unless the parser has reported an error at that place: both can
    # trip over one node -- a collection used as a key or as a name, a misplaced `!&` -- and one mistake earns one
    # diagnostic. A root that is no mapping was not read, which is no reason to keep quiet about its content.
    if failure and ((document or tool) is None
                    or all(_start(found.span) != _start(failure[1]) for found in reader.diags)):
        reader.diags.error(SophiosErrorCode.UNKNOWN_TAG, *failure)
    return ParseResult(document, Diagnostics(sorted(reader.diags, key=lambda found: _start(found.span))), tool)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Reader:
    """One reading of one file: where its nodes are written, and what has been reported about them."""

    file: str
    text: str
    diags: Diagnostics = field(default_factory=Diagnostics)

    def span(self, node: yaml.nodes.Node, start: yaml.nodes.Node | None = None) -> SourceSpan:
        """Where `node` is written, from where `start` is, when given."""
        return SourceSpan.from_marks(self.file, (start or node).start_mark, node.end_mark)

    def error(self, code: SophiosErrorCode, message: str, node: yaml.nodes.Node) -> None:
        """Report an error at `node`."""
        self.diags.error(code, message, self.span(node))

    def _entries(self, pairs: list[tuple[yaml.nodes.Node, yaml.nodes.Node]],
                 what: str) -> list[tuple[yaml.nodes.Node, str, yaml.nodes.Node]]:
        """A mapping's entries, each as its key's node, its key and its value's node; a key written twice is reported
        and read once. Every mapping the language owns is read through here, so what follows relies on unique keys."""
        seen: set[str] = set()
        kept = []
        for key_node, value_node in pairs:
            key = self._key_text(key_node)
            if key in seen:
                self.error(SophiosErrorCode.DUPLICATE_KEY, f'{what} {key!r} is defined more than once', key_node)
            else:
                seen.add(key)
                kept.append((key_node, key, value_node))
        return kept

    def _listed(self, key: str, node: yaml.nodes.Node,
                named: list[tuple[str, yaml.nodes.Node, Value]] | None = None,
                malformed: SophiosErrorCode = SophiosErrorCode.EXPECTED_MAPPING) -> Value:
        """Materialise `inputs`, `outputs`, `requirements` or `hints`, reading CWL's list form as its mapping form.

        CWL writes each as a mapping or as a list of entries named by `id:` (ports)
        or `class:` (requirements). The compiler acts on the mapping, so the list is
        lifted here. A port's `id:` may be written as a fragment (`#name`); the key
        is the name after the `#`. An entry with no name, a fragment with nothing
        after the `#`, or a name already taken cannot be a mapping entry and is
        reported. A list holding an `$import` or `$include`
        entry cannot be keyed, since that entry is named by a file only cwltool
        reads, so it is returned as written for cwltool to resolve. `named`, when
        given, gets every entry that has a name, as the name, the entry's node
        and its other fields: one whose name is taken too. An entry that cannot be read is reported as `malformed`.
        """
        value = self._opaque(node)
        if not isinstance(node, yaml.nodes.SequenceNode) or not isinstance(value, list) or any(
                isinstance(entry, dict) and entry.keys() & Grammar.IMPORT_KEYS for entry in value):
            return value
        named_by = Grammar.LISTED_BY[key]
        mapping: dict[str, Value] = {}
        for entry_node, entry in zip(node.value, value, strict=True):
            name = entry.get(named_by) if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(name, str):
                self.error(malformed, f'a list-form {key}: entry must be a mapping with a string {named_by}:',
                           entry_node)
                continue
            written, name = name, name.rsplit('#', 1)[-1] if named_by == 'id' else name
            body = {field: item for field, item in entry.items() if field != named_by}
            if named_by == 'id' and not name:
                self.error(malformed, f'a list-form {key}: entry id: {written!r} names no port: '
                           'the name is the part after the last #, and it must not be empty', entry_node)
                continue
            if named is not None:
                named.append((name, entry_node, body))
            if name in mapping:
                self.error(SophiosErrorCode.DUPLICATE_KEY,
                           f'{key}: entry {name!r} is defined more than once', entry_node)
            else:
                mapping[name] = body
        return mapping

    def document(self, root: yaml.nodes.MappingNode) -> Document:
        """Build a Document from the root mapping node."""
        steps: tuple[Step, ...] = ()
        as_mapping = False
        sidecar: WicSidecar | None = None
        passthrough: list[tuple[str, Value]] = []
        port_spans: list[tuple[str, str, SourceSpan]] = []
        for _, key, value_node in self._entries(root.value, 'top-level key'):
            if key == 'steps':
                steps, as_mapping = self._steps(value_node), isinstance(value_node, yaml.nodes.MappingNode)
            elif key == 'wic':
                sidecar = self._sidecar(value_node)
            else:
                value = self._passed(key, value_node)
                passthrough.append((key, value))
                if key in ('inputs', 'outputs') and isinstance(value_node, yaml.nodes.MappingNode):
                    port_spans.extend((key, name.value, self.span(name)) for name, _ in value_node.value
                                      if isinstance(name, yaml.nodes.ScalarNode))
        return Document(steps, sidecar, tuple(passthrough), self.span(root), as_mapping, tuple(port_spans))

    def _passed(self, key: str, node: yaml.nodes.Node) -> Value:
        """A top-level key the language does not read: CWL, kept as written, its list forms keyed."""
        value = self._listed(key, node) if key in Grammar.LISTED_BY else self._opaque(node)
        if key == 'cwlVersion' and value not in CWL_VERSIONS:
            # The compiler writes its own; an authored one must be one the toolchain runs (language spec §1).
            self.error(SophiosErrorCode.UNSUPPORTED_CWL_VERSION,
                       f'cwlVersion must be one of {", ".join(CWL_VERSIONS)}, found {_found(value)}', node)
        return value

    def runs(self, node: yaml.nodes.Node) -> list[yaml.nodes.Node]:
        """The keys of `node` that say what a tool runs: a mapping with one is a tool."""
        return [key for key, _ in node.value if isinstance(key, yaml.nodes.ScalarNode)
                and key.value in Grammar.TOOL_RUNS] if isinstance(node, yaml.nodes.MappingNode) else []

    def tool(self, root: yaml.nodes.Node) -> ToolDocument:
        """Build a ToolDocument from a mapping that says `command:` or `script:`: a document, or a step's inline
        `run:` body. Each of its own keys is checked against its shape (`wic059`) and read into what it says, each
        word and each port with its place; every other key is CWL. A key that is refused is read as unsaid, so one
        mistake earns one diagnostic. What the tool cannot mean (`wic058`) is reported in the same reading, unless
        its `inputs:` could not be read.
        """
        nodes = {key: (at, value) for at, key, value in self._entries(root.value, 'tool key')}
        defining = [key for key in nodes if key in ('steps', *Grammar.TOOL_RUNS)]
        for key in defining[1:]:
            self._refuse(f'`{key}:` beside `{defining[0]}:`: a document has one defining key. `steps:` makes it a '
                         'workflow, `command:` or `script:` a tool; a workflow runs a tool from a step '
                         '(`run: tool.wic`)', self.span(nodes[key][0]))
        reported = len(self.diags)
        # A placeholder is an input, so `inputs:` is read first, wherever it is written.
        said: dict[str, Any] = {'inputs': self._ports('inputs', nodes['inputs'][1]) if 'inputs' in nodes else ()}
        declared = {port.name: port for port in said['inputs']} if len(self.diags) == reported else None
        passthrough: list[tuple[str, Value]] = []
        for key, (_, node) in nodes.items():
            reported = len(self.diags)
            if key == 'wic':
                said['sidecar'] = self._sidecar(node)
            elif key == 'outputs':
                said[key] = self._ports(key, node, declared)
            elif key in Grammar.TOOL_VALUES and key != 'inputs':
                value = self._opaque(node)
                if len(self.diags) == reported and self._shaped(Grammar.TOOL_VALUES[key], value, node, key) \
                        and key not in defining[1:]:
                    said[key] = self._words(node, declared) if key == 'command' else tuple(
                        value.items()) if isinstance(value, dict) else value
            elif key not in ('inputs', 'steps', *defining[1:]):
                value = self._passed(key, node)
                self._plain(value, node)
                passthrough.append((key, value))
        tool = ToolDocument(**said, passthrough=tuple(passthrough), span=self.span(root),
                            keys=tuple((key, self.span(at)) for key, (at, _) in nodes.items()))
        if declared is not None:
            wic_tool.read(tool, self.diags)
        return tool

    def _ports(self, key: str, node: yaml.nodes.Node, declared: Mapping[str, Port] | None = None) -> tuple[Port, ...]:
        """The `inputs:` or `outputs:` of a tool as its ports, each where it is named: a mapping of names, or CWL's
        list of ports, each named by its `id:`. Each is checked against the port `Grammar.TOOL_VALUES` gives the
        section; one that is not of its shape is reported and left out, and one named twice is held to its shape as
        the first is. An output that says a line, alone or as its `from:`, has it read where it is written, a
        placeholder of it one of the inputs `declared`."""
        port = cast(JsonSchema, Grammar.TOOL_VALUES[key]['additionalProperties'])
        entries: list[tuple[str, yaml.nodes.Node, Value]] = []
        if isinstance(node, yaml.nodes.MappingNode):
            named = [(name, at, body, self._opaque(body)) for at, name, body in self._entries(node.value, key[:-1])]
        elif isinstance(node, yaml.nodes.SequenceNode):
            listed = self._listed(key, node, entries, SophiosErrorCode.MALFORMED_TOOL_VALUE)
            if listed is not None and not isinstance(listed, dict):
                self.error(SophiosErrorCode.MALFORMED_TOOL_VALUE,
                           f'{key} names ports only cwltool reads (`$import`): write them as a mapping of names, or '
                           'as a list of ports each named by its `id:`', node)
            named = [(name, item, item, body) for name, item, body in entries]
        else:
            # No value is no port, and any other is refused.
            self._shaped(Grammar.TOOL_VALUES[key], self._opaque(node), node, key)
            return ()
        ports: dict[str, Port] = {}
        for name, at, written, body in named:
            if not self._shaped(port, body, written, f'{key}: {name}'):
                continue
            self._plain(body, written)
            fields = dict(body) if isinstance(body, dict) else {'type' if key == 'inputs' else 'from': body}
            line = fields.pop('from', None) if key == 'outputs' else None
            if isinstance(body, dict) and isinstance(written, yaml.nodes.MappingNode):
                written = next((value for said, value in written.value if said.value == 'from'), written)
            ports[name] = Port(name, tuple(fields.items()), None if line is None else tuple(
                self._parts(str(line), written, [0], declared, 'an output line')), self.span(at))
        return tuple(ports.values())

    # pylint: disable-next=too-many-locals
    def _words(self, node: yaml.nodes.Node, declared: Mapping[str, Port] | None) -> tuple[Word, ...]:
        """The words of `command:`. A line as typed splits at spaces, and a word of it is text, `'quoted text'` or
        `{name}`; a list is the words themselves, and no quote or shell character is read in one. A value is a word
        of its own, so none ever reaches a shell. A word that is refused is read on as the text it was written as,
        so one mistake earns one diagnostic."""
        typed = isinstance(node, yaml.nodes.ScalarNode)
        cursor = [0]
        words: list[Word] = []
        for item, written in [(node, word) for word in Grammar.LINE_WORD.findall(node.value)] if typed else [
                (item, str(item.value)) for item in node.value]:
            reported, inside = len(self.diags), [*cursor]
            where = self._within(item, written, cursor)
            quoted = bool(typed and re.fullmatch(r"""'[^']*'|"[^"]*\"""", written))
            parts = self._parts(written[1:-1] if quoted else written, item, inside, declared, '`command:`')
            names = [part for part in parts if part.input is not None]
            text = ''.join(part.text for part in parts)
            if names and (quoted or len(parts) > 1):
                self._refuse(f'{written}: a value is a word of its own, never joined to text or put in quotes; for '
                             '`--out=VALUE` write the input with `inputBinding: {prefix: --out=, separate: false}`, '
                             'and for a value inside a script write `script:`', where)
            elif typed and not (quoted or names) and (found := Grammar.NEEDS_A_QUOTE.search(text)):
                self._refuse(f"{written}: a quote covers a whole word, as in 'two words'" if found[0] in '\'"' else
                             f'`{found[0]}` in `command:` needs a shell, and no shell reads a command line: quote '
                             'the word to pass it as text, or write `script:`', where)
            words.append(Word(written, span=where) if len(self.diags) > reported
                         else names[0] if names else Word(text, None, quoted, where))
        return tuple(words)

    def _parts(self, text: str, node: yaml.nodes.Node, cursor: list[int], declared: Mapping[str, Port] | None,
               line: str) -> list[Word]:
        """`text` of a line as its runs of text and its `{name}` placeholders, each the input of that name among
        `declared`, with its own place. `{{` and `}}` are the braces they are. A brace alone, and a name that is no
        input, are reported and read as the text they are; with no inputs read, no name is looked up."""
        parts: list[Word] = []
        run, last = '', 0
        for found in Grammar.LINE_PLACE.finditer(text):
            run, last = run + text[last:found.start()], found.end()
            where = self._within(node, found[0], cursor)
            name = found[1]
            if name is not None and declared is not None and name in declared:
                parts += [Word(run)] * bool(run) + [Word(input=declared[name], span=where)]
                run = ''
            elif name is None:
                run += found[0][0]
                if len(found[0]) == 1:
                    self._refuse(f'`{found[0]}` alone opens or closes no placeholder: write `{{name}}` for the '
                                 f'input `name`, and `{found[0] * 2}` for a brace that is text', where)
            else:
                run += found[0]
                if declared is not None:
                    self._refuse(f'{found[0]} names no input of this tool (its inputs: '
                                 f'{", ".join(declared) or "none"}); in {line}, a brace that is text is written '
                                 'twice, `{{`', where)
        return parts + [Word(run + text[last:])] * bool(run + text[last:])

    def _plain(self, value: Value, node: yaml.nodes.Node) -> None:
        """Report a Sophios construct in `value`, read from `node`: a tool takes none."""
        if _holds_construct(value):
            self._refuse('a tool takes no edge and no workflow input, so a Sophios construct (`!ii`, `!*`, `!cwl`) '
                         'says nothing in one: write the value CWL takes', self.span(node))

    def _shaped(self, schema: JsonSchema, value: Value, node: yaml.nodes.Node, where: str) -> bool:
        """Whether `value`, one of a tool's own read from `node`, is of the shape `schema` gives it. Each way it is
        not is reported (`wic059`), at the node that holds the fault."""
        refused = list(_refusals(schema, value, node, where))
        for message, at in refused:
            self.error(SophiosErrorCode.MALFORMED_TOOL_VALUE, message, at)
        return not refused

    def _within(self, node: yaml.nodes.Node, text: str, cursor: list[int]) -> SourceSpan:
        """Where `text`, a piece of the scalar `node`, is written: its next place in the scalar's source from
        `cursor` on, which moves past it. Only a scalar YAML reads as it is written, on one line, between quotes or
        without, has such places, and there the next is the right one. In any other (a block, a folded line, an
        escape, a quote written twice) a piece found by its text could be another piece, so each stands at the
        scalar."""
        start, end = node.start_mark.index, node.end_mark.index
        written = self.text[start:end]
        found = self.text.find(text, max(cursor[0], start), end) if text and node.value in (
            written, written[1:-1]) else -1
        if found < 0:
            return self.span(node)
        cursor[0] = found + len(text)
        line = node.start_mark.line + 1
        column = found - self.text.rfind('\n', 0, found)
        return SourceSpan(self.file, line, column, line, column + len(text))

    def _refuse(self, message: str, span: SourceSpan | None) -> None:
        """Report what a line of a tool cannot say."""
        self.diags.error(SophiosErrorCode.TOOL_DOCUMENT, message, span)

    def _steps(self, node: yaml.nodes.Node) -> tuple[Step, ...]:
        """Parse `steps:`, which may be a mapping or a sequence, into the same AST."""
        match node:
            case yaml.nodes.MappingNode():
                return tuple(self._step(name, value, key) for key, name, value in self._entries(node.value, 'step'))
            case yaml.nodes.SequenceNode():
                return tuple(map(self._sequence_step, node.value))
        self.error(SophiosErrorCode.EXPECTED_MAPPING,
                   f'steps: must be a mapping or a sequence, found {_kind(node)}', node)
        return ()

    def _sequence_step(self, node: yaml.nodes.Node) -> Step:
        """Parse one entry of a sequence-form `steps:`; identity comes from an `id:` key.

        A single-key mapping (`- touch:`) is not a second sequence form: CWL lifts
        a key into `id` only when the field's value is a mapping, so in a
        sequence the key is never lifted and the step has no identity. Reported
        as `wic006` (.wic reference §2.1). An entry with no identity is still read,
        so one pass reports everything it can see, and gives a step with no id.
        """
        span = self.span(node)
        if not isinstance(node, yaml.nodes.MappingNode):
            self.error(SophiosErrorCode.EXPECTED_MAPPING, f'each step must be a mapping, found {_kind(node)}', node)
        elif named := [entry for entry in node.value if self._key_text(entry[0]) == 'id']:
            step_id = self._name_text(named[0][1])
            if not step_id:
                self.error(SophiosErrorCode.EMPTY_STEP_ID, 'id: must be a non-empty string', named[0][1])
            return self._step_body(step_id, node.value, span, named[0][0])
        else:
            forgot_id = True  # the entry is the body of a step whose `- id:` line is missing
            match node.value:
                # Only a single scalar key can be offered back as a name; a collection key has earned wic005.
                case [(yaml.nodes.ScalarNode(value=name), body)]:
                    forgot_id = name in Grammar.STEP_KEYS
                    names = f"write '- id: {name}' if {name!r} is the step's name"
                    forgot = f"add the '- id:' line above if {name!r} is one of the step's own keys"
                    first, second = (forgot, names) if forgot_id else (names, forgot)
                    self.error(SophiosErrorCode.MISSING_STEP_ID,
                               f'a step in a sequence carries its name in an id: key — {first}; {second}. '
                               f'Keying the whole steps: block by name is the other form (.wic reference §2.1)', node)
                    if not forgot_id:
                        self._step(name, body)  # the key names the step and its value is the body, as in a mapping
                case _:
                    self.error(SophiosErrorCode.MISSING_STEP_ID, 'a step in a sequence needs an id:', node)
            if forgot_id:
                self._step_body('', node.value, span)
        return Step('', span=span)

    def _step(self, step_id: str, node: yaml.nodes.Node, key_node: yaml.nodes.Node | None = None) -> Step:
        """Parse a step body, keyed by its id; the step's span starts at `key_node`, the id, when given.

        A step may legitimately have no body at all (`some_step.wic:` with nothing
        under it), which YAML resolves to null.
        """
        span = self.span(node, key_node)
        if node.tag != 'tag:yaml.org,2002:null':
            if isinstance(node, yaml.nodes.MappingNode):
                return self._step_body(step_id, node.value, span)
            self.diags.error(SophiosErrorCode.EXPECTED_MAPPING,
                             f'step {step_id!r} must be a mapping, found {_kind(node)}', span)
        return Step(step_id, span=span)

    def _step_body(self, step_id: str, entries: list[tuple[yaml.nodes.Node, yaml.nodes.Node]],
                   span: SourceSpan, named_at: yaml.nodes.Node | None = None) -> Step:
        """Split a step's keys into inputs, outputs, interpreted CWL, and passthrough."""
        inputs: tuple[tuple[str, InputValue], ...] = ()
        outputs: tuple[OutputBinding, ...] = ()
        interpreted: list[tuple[str, Value]] = []
        passthrough: list[tuple[str, Value]] = []
        tool: ToolDocument | None = None
        for key_node, key, value_node in self._entries(entries, 'step key'):
            if key == 'id':
                # The step has its identity from its key in `steps:`, or from the `id:` of its entry at `named_at`:
                # any other is a second, contradictory one, and rendering would have to pick between them.
                if key_node is not named_at:
                    self.error(SophiosErrorCode.DUPLICATE_KEY,
                               f'step {step_id!r} already has its identity; a second id: is contradictory', key_node)
            elif key == 'in':
                inputs = self._inputs(value_node)
            elif key == 'out':
                outputs = self._outputs(value_node)
            elif key in Grammar.INTERPRETED_STEP_KEYS:
                interpreted.append((key, self._opaque(value_node)))
                if key == 'run' and self.runs(value_node):  # an inline body that says what runs is a tool
                    tool = self.tool(value_node)
            elif key in Grammar.STEP_LISTED_KEYS:
                passthrough.append((key, self._listed(key, value_node)))
            else:
                passthrough.append((key, self._opaque(value_node)))
        return Step(step_id, inputs, outputs, tuple(interpreted), tuple(passthrough), span, tool)

    def _inputs(self, node: yaml.nodes.Node) -> tuple[tuple[str, InputValue], ...]:
        """Parse a step's `in:` mapping into typed input values.

        A repeated key is reported rather than silently resolved: binding the same
        input twice is ambiguous, and picking either one would hide a mistake.
        """
        if not isinstance(node, yaml.nodes.MappingNode):
            self.error(SophiosErrorCode.EXPECTED_MAPPING, f'in: must be a mapping, found {_kind(node)}', node)
            return ()
        return tuple((name, self._input_value(value)) for _, name, value in self._entries(node.value, 'input'))

    def _input_value(self, node: yaml.nodes.Node) -> InputValue:
        """Read one step input (.wic reference §3.1): a construct in either spelling; a bare name, which resolves
        later to a workflow input; or a mapping or a sequence, which can name nothing and so is a literal."""
        span = self.span(node)
        found = _construct(node)
        if found and found[1] is not node and found[0] != Key.ANCHOR:
            self._key_text(node.value[0][0])  # a key that spells a construct is a name like any other
        match found:
            case Key.ANCHOR, payload:
                # A construct the language has, in the wrong place, so wic019 and not wic009; its name is kept.
                self.error(SophiosErrorCode.MISPLACED_EDGE_DEF,
                           "'!&' defines an edge, and an edge is defined where its value comes into being: "
                           "a step's out: entry (.wic reference §3.6). Use '!*' to consume an edge", node)
                return UnresolvedName(payload.value if isinstance(payload, yaml.nodes.ScalarNode) else '', span)
            case Key.ALIAS, payload:
                return EdgeRef(self._name_text(payload), span)
            case Key.PASSTHROUGH, payload:
                return self._raw_cwl(payload, span, frozenset(), [0])
            case Key.INLINE_INPUT, payload:
                # A literal is the first node its own reading spends.
                return self._literal(node, frozenset(), [1]) if payload is node else InlineLiteral(
                    read_value(self._opaque(payload)), span)
        if isinstance(node, yaml.nodes.ScalarNode):
            return UnresolvedName(node.value, span)
        if isinstance(node, yaml.nodes.MappingNode):
            match node.value:
                case [(yaml.nodes.ScalarNode(value=key) as key_node, _)] if key.startswith('wic_'):
                    # A desugared key shares its namespace with passthrough CWL, which is open (language spec §1), so
                    # a misspelled construct such as `wic_inline_inpt` would otherwise ride into the emitted document.
                    # A name may carry the prefix: only a single-key mapping where a value is expected is read so.
                    self.error(SophiosErrorCode.RESERVED_KEY,
                               f"{key!r} is not a Sophios construct, and a single-key mapping beginning 'wic_' is read "
                               f'as one. The constructs are: {", ".join(sorted(Grammar.DESUGARED_KEYS))}', key_node)
            keys = {str(key.value) for key, _ in node.value if isinstance(key, yaml.nodes.ScalarNode)}
            if keys and keys <= STEP_INPUT_RECORD_KEYS:
                # Kept as a literal for recovery.
                self.error(SophiosErrorCode.STEP_INPUT_RECORD,
                           'this mapping spells a CWL step input (' + ', '.join(sorted(keys)) + '), which '
                           'Sophios does not read untagged; write `!cwl {source: ..., default: ...}` for a CWL '
                           'step input, `!ii` for a literal of that shape', node)
        return InlineLiteral(read_value(self._opaque(node)), span)

    def _raw_cwl(self, node: yaml.nodes.Node, span: SourceSpan, path: frozenset[int], spent: list[int]) -> InputValue:
        """`!cwl name` is a raw reference; `!cwl {source: ..., ...}` is a step-input record, written at `span`.

        The record's `source` names edges (`!*`, or `{wic_alias: name}`) and workflow inputs (bare names), one or a
        list; every other field is CWL, copied out. Its entries are materialised on the path that reached the record,
        so a record holding itself is an alias cycle.
        """
        if not isinstance(node, yaml.nodes.MappingNode):
            return PassthroughRef(self._name_text(node), span)
        fields = {self._key_text(key): self._opaque(value, path | {id(node)}, spent) for key, value in node.value}
        written = fields.pop('source', [])
        sources: list[EdgeRef | UnresolvedName] = []
        faults: list[str] = []
        for entry in written if isinstance(written, list) else [written]:
            match entry:
                case EdgeRef() | UnresolvedName():
                    sources.append(entry)
                case {Key.ALIAS: str() as name, **rest} if not rest:
                    sources.append(EdgeRef(name, span))
                case str() as name:
                    sources.append(UnresolvedName(name, span))
                case _:
                    faults.append('`source` names edges (!*) or workflow inputs, one or a list')
        for key in [key for key in fields if key not in STEP_INPUT_RECORD_KEYS]:
            faults.append(f'{key!r} is not a WorkflowStepInput field Sophios writes; the fields are '
                          + ', '.join(sorted(STEP_INPUT_RECORD_KEYS)))
            del fields[key]
        for key in [key for key, value in fields.items() if _holds_construct(value)]:
            faults.append(f'{key!r} holds a Sophios construct; every field but source is CWL, written out as is')
            del fields[key]
        if not faults and not sources and not {'default', 'valueFrom'} & fields.keys():
            faults.append('with no source, default or valueFrom the step input receives no value; '
                          'leave it unbound for Sophios to connect, or give it a source or a default')
        for fault in faults:
            self.diags.error(SophiosErrorCode.STEP_INPUT_RECORD, f'!cwl record: {fault}', span)
        return InputRecord(tuple(sources), tuple((key, read_value(item) if key == 'default' else item)
                                                 for key, item in fields.items()), span)

    def _outputs(self, node: yaml.nodes.Node) -> tuple[OutputBinding, ...]:
        """Parse a step's `out:` sequence."""
        if not isinstance(node, yaml.nodes.SequenceNode):
            self.error(SophiosErrorCode.EXPECTED_SEQUENCE, f'out: must be a sequence, found {_kind(node)}', node)
            return ()
        return tuple(map(self._output_binding, node.value))

    def _output_binding(self, node: yaml.nodes.Node) -> OutputBinding:
        """Parse one `out:` entry: a bare name, or a name bound to an edge definition."""
        span = self.span(node)
        match node:
            case yaml.nodes.ScalarNode():
                return OutputBinding(node.value, None, span)
            case yaml.nodes.MappingNode(value=[(key_node, value_node)]):
                name = self._key_text(key_node)
                match value_node:
                    case yaml.nodes.MappingNode(value=[(inner, _)]) if value_node.tag != Tag.ANCHOR:
                        self._key_text(inner)  # a key that spells a construct is a name like any other
                match _construct(value_node):
                    case Key.ANCHOR, payload:
                        return OutputBinding(name, EdgeDef(self._name_text(payload), self.span(payload)), span)
                # Reported, not dropped: the AST keeps what it was given.
                self.error(SophiosErrorCode.EXPECTED_SCALAR,
                           f'out: entry {name!r} must bind an !& edge definition; its value is neither !& nor '
                           'wic_anchor', value_node)
                return OutputBinding(name, None, span)
        self.error(SophiosErrorCode.EXPECTED_SCALAR, 'each out: entry must be a name or a single-key mapping', node)
        return OutputBinding('', None, span)

    def _sidecar(self, node: yaml.nodes.Node, values: Mapping[str, JsonSchema] = Grammar.SIDECAR_VALUES,
                 path: frozenset[int] = frozenset()) -> WicSidecar:
        """Parse a `wic:` block, normalising its `"(1, name)"` step keys.

        A key outside ``values`` and `steps:` is reported and dropped, since the
        block is closed (.wic reference §7); a value is checked against its schema.
        """
        steps: list[tuple[StepKey, WicSidecar]] = []
        entries: list[tuple[str, Value]] = []
        implementations: list[tuple[str, Document]] = []
        graphviz = GraphvizEntry()
        if id(node) in path:
            self.error(SophiosErrorCode.RECURSIVE_ALIAS, 'alias cycle: a wic: block contains itself', node)
        elif node.tag == 'tag:yaml.org,2002:null':
            pass  # `wic:` with nothing under it is an empty block
        elif not isinstance(node, yaml.nodes.MappingNode):
            self.error(SophiosErrorCode.EXPECTED_MAPPING, f'wic: must be a mapping, found {_kind(node)}', node)
        else:
            admitted = {*values, 'steps'}
            for key_node, _ in node.value:
                # Every key outside the block is reported, a repeated one too; any but a scalar has earned wic005.
                if isinstance(key_node, yaml.nodes.ScalarNode) and key_node.value not in admitted:
                    self.error(SophiosErrorCode.UNKNOWN_WIC_KEY,
                               f'{key_node.value!r} is not a wic: key. The keys are: {", ".join(sorted(admitted))}',
                               key_node)
            for _, key, value_node in self._entries(node.value, 'wic: entry'):
                if key == 'steps':
                    steps = self._sidecar_steps(value_node, path | {id(node)})
                elif key in values:
                    entries.append((key, self._sidecar_value(key, value_node, values[key])))
                    if key == 'graphviz':
                        graphviz = _graphviz(entries[-1][1])
                    if key == 'implementations' and isinstance(value_node, yaml.nodes.MappingNode):
                        # Each body is the document it is, with its spans. An empty one defers to whatever supplies
                        # the implementation: an empty document for it would shadow that supplier with nothing.
                        implementations = [(self._key_text(name), self.document(body))
                                           for name, body in value_node.value
                                           if isinstance(body, yaml.nodes.MappingNode) and body.value]
                        for runs in (found for _, body in value_node.value for found in self.runs(body)[:1]):
                            self.error(SophiosErrorCode.TOOL_DOCUMENT,
                                       f'`{runs.value}:` makes this implementation a tool, and an implementation is a '
                                       'workflow: a step of it runs the tool (`run: tool.wic`)', runs)
        return WicSidecar(tuple(steps), tuple(entries), tuple(implementations), graphviz, self.span(node))

    def _sidecar_steps(self, node: yaml.nodes.Node, path: frozenset[int]) -> list[tuple[StepKey, WicSidecar]]:
        """Parse `wic: steps:`, each entry what the block says about the step its key names."""
        steps: list[tuple[StepKey, WicSidecar]] = []
        if not isinstance(node, yaml.nodes.MappingNode):
            self.error(SophiosErrorCode.EXPECTED_MAPPING, f'wic: steps: must be a mapping, found {_kind(node)}', node)
            return steps
        for key_node, key, value_node in self._entries(node.value, 'wic: step key'):
            if (named := step_key(key)) is None:
                self.error(SophiosErrorCode.MALFORMED_WIC_STEP_KEY,
                           f'wic: step key {key!r} must have the form "(index, name)" or be a step id', key_node)
            else:
                steps.append((named, self._sidecar(_unwrapped(value_node), Grammar.SIDECAR_STEP_VALUES, path)))
        return steps

    def _sidecar_value(self, key: str, node: yaml.nodes.Node, schema: JsonSchema) -> Value:
        """Materialise one `wic:` value and check it against its schema (`wic034`).

        A value that already earned a diagnostic while being read is not checked
        again: one mistake, one diagnostic.
        """
        reported = len(self.diags)
        value: Value
        if key == 'out':
            # Read as a step's own `out:` is (.wic reference §3.6), and kept in the desugared spelling the writer
            # gives an edge definition, which is no passthrough value itself.
            value = [binding.name if binding.edge_def is None else {binding.name: {Key.ANCHOR: binding.edge_def.name}}
                     for binding in self._outputs(node)]
        else:
            value = self._opaque(node)
        if len(self.diags) == reported:
            for message, at in _refusals(schema, value, node, f'wic: {key}'):
                self.error(SophiosErrorCode.MALFORMED_WIC_VALUE, message, at)
        return value

    def _literal(self, node: yaml.nodes.Node, path: frozenset[int], spent: list[int]) -> InlineLiteral:
        """`!ii payload`, the payload a scalar, a mapping or a sequence.

        A tag switches off YAML's own typing, so `!ii 5` arrives as the text "5". A scalar is read as YAML reads the
        same scalar untagged (language spec §2): a plain one by what it says, so `!ii 5` is the int 5, and a plain
        value YAML cannot read (`!ii 2020-13-45`) is reported as the untagged one is; a quoted or block one is its
        text, so `!ii '5'` is "5". A collection is read as the same collection, untagged.

        A plain scalar on one line also keeps its spelling, as it says more than its value: `!ii 0777` is the int 511,
        and would otherwise be written back as `!ii 511`. A plain scalar spanning lines was folded, so it has none.
        """
        span = self.span(node)
        if not isinstance(node, yaml.nodes.ScalarNode):
            untagged = type(node)('', node.value, node.start_mark, node.end_mark)
            return InlineLiteral(read_value(self._read(untagged, path | {id(node)}, spent)), span)
        text: str = node.value
        if node.style is not None:
            return InlineLiteral(text, span)
        value: Value = text
        try:
            value = yaml.safe_load(text) if text else text
        except yaml.YAMLError:
            pass  # not a primitive: its text is the honest reading
        except _UNREADABLE:
            read = yaml.compose(text, Loader=yaml.SafeLoader)
            assert read is not None  # a non-empty scalar composes to a node
            self.error(SophiosErrorCode.UNKNOWN_TAG, f'YAML cannot read this value as {_tag_name(read.tag)}', node)
        return InlineLiteral(read_value(value), span, None if '\n' in text else text)

    def _opaque(self, node: yaml.nodes.Node, path: frozenset[int] = frozenset(),
                spent: list[int] | None = None) -> Value:
        """Materialise a node Sophios does not interpret, as YAML reads it.

        A construct inside it is still read as one, so an edge reference buried in passthrough is not flattened to a
        string. Of the desugared spellings only an edge definition and a record are: any other mapping keyed
        `wic_...` is passthrough here.

        An alias that leads back into the node it is in is a cycle (compose() resolves an alias to the same object),
        and a chain of aliases that widens past the budget a bomb: each is reported once and read as nothing.
        """
        spent = [0] if spent is None else spent
        if id(node) in path:
            self.error(SophiosErrorCode.RECURSIVE_ALIAS, 'alias cycle: a node contains itself', node)
            return None
        spent[0] += 1
        if spent[0] > _EXPANSION_BUDGET:
            if spent[0] == _EXPANSION_BUDGET + 1:  # report once, not per node
                self.error(SophiosErrorCode.RECURSIVE_ALIAS,
                           f'alias expansion exceeds {_EXPANSION_BUDGET} nodes; refusing to materialise', node)
            return None
        return self._read(node, path | {id(node)}, spent)

    def _read(self, node: yaml.nodes.Node, path: frozenset[int], spent: list[int]) -> Value:
        """Read a node `_opaque` has entered, or the untagged copy of one."""
        match _construct(node, True), node:
            case (Key.PASSTHROUGH, yaml.nodes.MappingNode() as body), _:
                return self._raw_cwl(body, self.span(node), path, spent)
            case (Key.INLINE_INPUT, payload), _ if payload is node:
                return self._literal(node, path, spent)
            case (key, payload), _ if payload is node or key == Key.ANCHOR:
                return self._input_value(node)  # a name, or a misplaced edge definition, reported there
            case _, yaml.nodes.SequenceNode():
                return [self._opaque(item, path, spent) for item in node.value]
            case _, yaml.nodes.MappingNode():
                return {self._key_text(key): self._opaque(value, path, spent) for key, value in node.value}
        return _resolved_scalar(node)

    def _name_text(self, node: yaml.nodes.Node) -> str:
        """Return a scalar node's literal text, reporting anything non-scalar.

        Edge names and CWL references are identifiers, so the raw text is what
        is meant — resolving `false` to a bool, or `01` to an int, would rename
        them.
        """
        if not isinstance(node, yaml.nodes.ScalarNode):
            self.error(SophiosErrorCode.EXPECTED_SCALAR,
                       f'an edge or reference name must be a scalar, found {_kind(node)}', node)
            return ''
        return str(node.value)

    def _key_text(self, node: yaml.nodes.Node) -> str:
        """Return a mapping key as text.

        YAML admits collection keys (`? [a, b]`) and tagged ones (`!ii a: b`);
        Sophios does not, since every key in the language is a name. A non-scalar
        key, or one carrying a Sophios tag, is reported and stringified for
        recovery.
        """
        if not isinstance(node, yaml.nodes.ScalarNode):
            self.error(SophiosErrorCode.EXPECTED_SCALAR,
                       f'mapping keys must be scalars, found {_kind(node)}', node)
        elif node.tag in Tag.ALL:
            self.error(SophiosErrorCode.EXPECTED_SCALAR,
                       f'a mapping key is a name and takes no tag; {node.tag} belongs on the value, '
                       f'as in `{node.value}: {node.tag} <value>`', node)
        return str(node.value)


def _construct(node: yaml.nodes.Node, passthrough: bool = False) -> tuple[str, yaml.nodes.Node] | None:
    """The construct `node` spells (language spec §2), as its desugared key and the node holding its payload.

    `!tag payload` and `{key: payload}` are one construct. A tag is read before a key, and an edge definition
    before either, so `!ii {wic_anchor: e}` is the misplaced definition it is anywhere. In passthrough a record
    spelled by key is read before any tag but its own.
    """
    tagged = KEY_OF_TAG.get(node.tag)
    match node:
        case yaml.nodes.MappingNode(value=[(yaml.nodes.ScalarNode(value=key), payload)]) if tagged != Key.ANCHOR:
            record = passthrough and key == Key.PASSTHROUGH != tagged and isinstance(payload, yaml.nodes.MappingNode)
            if key == Key.ANCHOR or record or key in Key.ALL and tagged is None:
                return key, payload
    return (tagged, node) if tagged else None


def _holds_construct(value: Value) -> bool:
    """Whether a Sophios construct sits anywhere inside `value`."""
    match value:
        case dict():
            return any(map(_holds_construct, value.values()))
        case list():
            return any(map(_holds_construct, value))
    return isinstance(value, get_args(InputValue))


#: The key a `wic: steps:` entry may wrap its block in. The parser unwraps it, the renderer wraps in it again and
#: the schema names it, so the three cannot disagree about it.
SIDECAR_WRAPPER_KEY: Final = 'wic'


def _unwrapped(node: yaml.nodes.Node) -> yaml.nodes.Node:
    """A `wic: steps:` entry without the `wic:` key it may wrap its block in (.wic reference §7).

    The wrapper's siblings are folded into the block it wraps, so that `{wic: {namespace: ...}, out: [...]}` is read
    whole. An empty wrapper beside siblings is an empty block, as a bare `{wic: }` is.
    """
    if not isinstance(node, yaml.nodes.MappingNode):
        return node
    wrapped = [value for key, value in node.value if key.value == SIDECAR_WRAPPER_KEY]
    siblings = [entry for entry in node.value if entry[0].value != SIDECAR_WRAPPER_KEY]
    if not wrapped or not siblings:
        return wrapped[-1] if wrapped else node
    match wrapped[-1]:
        case yaml.nodes.MappingNode(tag=tag, value=entries):
            siblings = [*entries, *siblings]
        case yaml.nodes.ScalarNode(tag='tag:yaml.org,2002:null'):
            tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
        case _:
            return node
    return yaml.nodes.MappingNode(tag, siblings, node.start_mark, node.end_mark)


def _refusals(schema: JsonSchema, value: Value, node: yaml.nodes.Node,
              where: str) -> Iterator[tuple[str, yaml.nodes.Node]]:
    """Each way `schema` refuses `value`, which was read from `node`, with the node that holds the fault.

    What a schema says of a collection's entries is checked entry by entry, beside the nodes they are written in,
    so a scalar is no collection whatever its tag says; the rest is jsonschema's to check, and is reported as the
    keyword that refuses and what it refuses.
    """
    match schema, value, node:
        case {'properties': declared}, dict(), yaml.nodes.MappingNode():
            for key_node, value_node in node.value:
                name = str(key_node.value)
                if name in declared:
                    yield from _refusals(declared[name], value[name], value_node, f'{where}: {name}')
                elif schema.get('additionalProperties') is False:
                    yield f'{where} has no key {name!r}; its keys are: {", ".join(sorted(declared))}', key_node
        case {'items': entry}, list(), yaml.nodes.SequenceNode():
            for index, (item, item_node) in enumerate(zip(value, node.value)):
                yield from _refusals(entry, item, item_node, f'{where}[{index}]')
        case _:
            # JSON has no number that is not finite, so no keyword refuses one: it is checked as the text it is.
            unfinite = 'number' in schema.get('type', ()) and isinstance(value, float) and not math.isfinite(value)
            for error in Draft202012Validator(schema).iter_errors(str(value) if unfinite else value):
                # A value whose schema describes it is told so in those words, whichever keyword refuses it.
                yield (f'{where} must be {schema["description"]}, found {_found(value)}' if 'description' in schema
                       else f'{where} {_REFUSED[error.validator](error.validator_value, value)}'), node


_KINDS: Final[Mapping[str, str]] = MappingProxyType({
    'string': 'a string', 'boolean': 'true or false', 'object': 'a mapping', 'array': 'a list'})
#: What each schema keyword of `Grammar.SIDECAR_VALUES` refuses, in the words of a `.wic` author.
_REFUSED: Final[Mapping[str, Callable[[Any, Value], str]]] = MappingProxyType({
    'type': lambda kind, value: f'must be {_KINDS[kind]}, found {_found(value)}',
    'enum': lambda choices, value: f'must be one of {", ".join(choices)}, found {_found(value)}',
    'minLength': lambda _least, _value: 'must not be empty',
    'pattern': lambda pattern, value: f'must match {pattern}, found {value!r}',
})


def _found(value: Value) -> str:
    """What a value is, for a message: a scalar as YAML read it, anything else by its kind."""
    match value:
        case dict():
            return 'a mapping'
        case list():
            return 'a list'
        case None | bool() | int() | float() | str():
            return repr(value)
    return 'a Sophios construct' if isinstance(value, get_args(InputValue)) else f'a {type(value).__name__}'


def _graphviz(value: Value) -> GraphvizEntry:
    """The `graphviz:` value as an entry. A value its schema refused (`wic034`) was reported, and reads as far as it
    is well formed."""
    if not isinstance(value, dict):
        return GraphvizEntry()
    label, style, same = value.get('label'), value.get('style'), value.get('ranksame')
    keys = None if not isinstance(same, list) else tuple(
        key for item in same if isinstance(item, str) and (key := step_key(item)) is not None)
    return GraphvizEntry(label if isinstance(label, str) else None, style if isinstance(style, str) else None, keys)


def step_key(text: str) -> StepKey | None:
    """Normalise a `"(1, name)"` or bare `"name"` sidecar key, or None if it is malformed."""
    match = Grammar.WIC_STEP_KEY.match(text)
    if match is not None:
        return StepKey(int(match.group(1)), match.group(2))
    if Grammar.WIC_STEP_ID.match(text):
        return StepKey(None, text)
    return None


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------


#: Passthrough nodes materialised per parse before the walk is cut off, to
#: stop a YAML alias "billion laughs" expansion attack.
_EXPANSION_BUDGET: Final = 100_000


def _resolved_scalar(node: yaml.nodes.Node) -> Value:
    """Convert a resolved scalar node to its Python value, exactly as `yaml.SafeLoader` would.

    Delegated to PyYAML's own `SafeConstructor` rather than re-implemented,
    to avoid diverging from YAML on octals, hex, timestamps, `.inf`,
    and sexagesimals. A fresh constructor per call, since the class carries
    per-document state and this layer is shared across threads.
    """
    text: str = node.value
    try:
        scalar: Value = yaml.constructor.SafeConstructor().construct_object(node)
        return scalar
    except (yaml.YAMLError, *_UNREADABLE):  # reported by `_load`
        return text


def _kind(node: yaml.nodes.Node) -> str:
    """Describe a node's kind for a diagnostic message."""
    match node:
        case yaml.nodes.ScalarNode() if node.tag == 'tag:yaml.org,2002:null':
            return 'nothing'
        case yaml.nodes.ScalarNode():
            return 'a scalar'
        case yaml.nodes.SequenceNode():
            return 'a sequence'
        case yaml.nodes.MappingNode():
            return 'a mapping'
        case _:
            return 'an unsupported node'


def _error_span(exc: yaml.YAMLError, whole: SourceSpan) -> SourceSpan:
    """Where PyYAML places an error, or else `whole`, the text it read."""
    mark = getattr(exc, 'problem_mark', None)
    return SourceSpan.from_marks(whole.file, mark, mark) if mark else whole
