"""Parse `.wic` source into a typed AST.

The parser composes YAML to a node tree rather than loading it to plain
Python objects, because composition preserves the source marks that make
diagnostics worth reading. Nothing here raises: a caller always receives a
result carrying whatever was parsed plus whatever went wrong.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import chain
from types import MappingProxyType
from typing import Any, Final, Iterator, Mapping, NoReturn, final, get_args

import yaml
from jsonschema import Draft202012Validator

from ..utils_yaml import KEY_OF_TAG, Key, Tag
from .diagnostics import Diagnostics
from .error_codes import SophiosErrorCode
from .nodes import (
    CwlRecord,
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    InputValue,
    OpaqueCwl,
    OutputBinding,
    RawCwlRef,
    Step,
    StepKey,
    UnresolvedName,
    WicSidecar,
)
from .cwl import CWL_VERSIONS, SCATTER_METHODS
from .spans import SourceSpan
from .support import STEP_INPUT_RECORD_KEYS

#: A `graphviz: style:` value: one or more styles, comma separated.
_GRAPHVIZ_STYLE: Final = ('((,\\s*)*(dashed|dotted|solid|invis|bold|tapered|filled|striped|wedged'
                          '|diagonals|rounded))+')
#: A `graphviz: ranksame:` entry: a `wic: steps:` key, `(index, name)`.
_RANKSAME_ENTRY: Final = '\\([0-9]+, [A-Za-z0-9_\\.]+\\)'
_NON_EMPTY: Final = {'type': 'string', 'minLength': 1}


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
    SIDECAR_VALUES: Final[Mapping[str, Mapping[str, Any]]] = MappingProxyType({
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
    SIDECAR_STEP_VALUES: Final[Mapping[str, Mapping[str, Any]]] = MappingProxyType({
        **SIDECAR_VALUES,
        'in': {},
        'out': {},
        'scatter': {},
        'scatterMethod': {'enum': sorted(SCATTER_METHODS)},
        'inference': {},
    })

    #: `wic:` sidecar step keys have the surface form "(1, step_name)".
    WIC_STEP_KEY: Final = re.compile(r'^\(\s*(\d+)\s*,\s*(.+?)\s*\)$')

    #: The same rule as a string, for consumers that need to state it rather
    #: than apply it — the exported JSON Schema, principally.
    WIC_STEP_KEY_PATTERN: Final = WIC_STEP_KEY.pattern

    #: The other spelling: a bare step id, for a step whose id is unique.
    WIC_STEP_ID: Final = re.compile(r'^[A-Za-z0-9_.-]+$')
    WIC_STEP_ID_PATTERN: Final = WIC_STEP_ID.pattern

    # No SCALAR_TAGS table here: scalar resolution is delegated to PyYAML's
    # SafeConstructor (see _resolved_scalar) to avoid diverging from it.


@dataclass(frozen=True, slots=True)
class ParseResult:
    """The outcome of parsing one document.

    `document` is None only when nothing could be recovered — malformed YAML,
    or a root that is not a mapping. Otherwise it is present even alongside
    errors, so a caller can report several problems at once.
    """

    document: Document | None
    diagnostics: Diagnostics


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

    def construct_object(self, node: yaml.nodes.Node, deep: bool = False) -> Any:
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
    """Parse `.wic` source text into a `Document`.

    This function is total: it never raises, for any input. Malformed YAML,
    wrong node kinds, and unknown tags are all reported as diagnostics, in
    reading order, so a reader works down the file once.
    """
    reader = _Reader(filename)
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
    document = None
    if isinstance(root, yaml.nodes.MappingNode):
        document = reader.document(root)
    else:
        reader.error(SophiosErrorCode.NOT_A_MAPPING, f'a Sophios document must be a mapping, found {_kind(root)}', root)
    # What the loader cannot build is reported unless the parser has reported an error at that place: both can
    # trip over one node -- a collection used as a key or as a name, a misplaced `!&` -- and one mistake earns one
    # diagnostic. A root that is no mapping was not read, which is no reason to keep quiet about its content.
    if failure and (document is None or all(_start(found.span) != _start(failure[1]) for found in reader.diags)):
        reader.diags.error(SophiosErrorCode.UNKNOWN_TAG, *failure)
    return ParseResult(document, Diagnostics(sorted(reader.diags, key=lambda found: _start(found.span))))


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Reader:
    """One reading of one file: where its nodes are written, and what has been reported about them."""

    file: str
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

    def _listed(self, key: str, node: yaml.nodes.Node) -> OpaqueCwl:
        """Materialise `inputs`, `outputs`, `requirements` or `hints`, reading CWL's list form as its mapping form.

        CWL writes each as a mapping or as a list of entries named by `id:` (ports)
        or `class:` (requirements). The compiler acts on the mapping, so the list is
        lifted here. A port's `id:` may be written as a fragment (`#name`); the key
        is the name after the `#`. An entry with no name, a fragment with nothing
        after the `#`, or a name already taken cannot be a mapping entry and is
        reported. A list holding an `$import` or `$include`
        entry cannot be keyed, since that entry is named by a file only cwltool
        reads, so it is returned as written for cwltool to resolve.
        """
        value = self._opaque(node)
        if not isinstance(node, yaml.nodes.SequenceNode) or not isinstance(value, list) or any(
                isinstance(entry, dict) and entry.keys() & Grammar.IMPORT_KEYS for entry in value):
            return value
        named_by = Grammar.LISTED_BY[key]
        mapping: dict[str, OpaqueCwl] = {}
        for entry_node, entry in zip(node.value, value, strict=True):
            name = entry.get(named_by) if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(name, str):
                self.error(SophiosErrorCode.EXPECTED_MAPPING,
                           f'a list-form {key}: entry must be a mapping with a string {named_by}:', entry_node)
                continue
            written, name = name, name.rsplit('#', 1)[-1] if named_by == 'id' else name
            if named_by == 'id' and not name:
                self.error(SophiosErrorCode.EXPECTED_MAPPING,
                           f'a list-form {key}: entry id: {written!r} names no port: '
                           'the name is the part after the last #, and it must not be empty', entry_node)
            elif name in mapping:
                self.error(SophiosErrorCode.DUPLICATE_KEY,
                           f'{key}: entry {name!r} is defined more than once', entry_node)
            else:
                mapping[name] = {field: item for field, item in entry.items() if field != named_by}
        return mapping

    def document(self, root: yaml.nodes.MappingNode) -> Document:
        """Build a Document from the root mapping node."""
        steps: tuple[Step, ...] = ()
        as_mapping = False
        sidecar: WicSidecar | None = None
        passthrough: list[tuple[str, OpaqueCwl]] = []
        port_spans: list[tuple[str, str, SourceSpan]] = []
        for _, key, value_node in self._entries(root.value, 'top-level key'):
            if key == 'steps':
                steps, as_mapping = self._steps(value_node), isinstance(value_node, yaml.nodes.MappingNode)
            elif key == 'wic':
                sidecar = self._sidecar(value_node)
            else:
                value = self._listed(key, value_node) if key in Grammar.LISTED_BY else self._opaque(value_node)
                passthrough.append((key, value))
                if key == 'cwlVersion' and value not in CWL_VERSIONS:
                    # The compiler writes its own; an authored one must be one the toolchain runs (language spec §1).
                    self.error(SophiosErrorCode.UNSUPPORTED_CWL_VERSION,
                               f'cwlVersion must be one of {", ".join(CWL_VERSIONS)}, found {_found(value)}',
                               value_node)
                if key in ('inputs', 'outputs') and isinstance(value_node, yaml.nodes.MappingNode):
                    port_spans.extend((key, name.value, self.span(name)) for name, _ in value_node.value
                                      if isinstance(name, yaml.nodes.ScalarNode))
        return Document(steps, sidecar, tuple(passthrough), self.span(root), as_mapping, tuple(port_spans))

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
        interpreted: list[tuple[str, OpaqueCwl]] = []
        passthrough: list[tuple[str, OpaqueCwl]] = []
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
            elif key in Grammar.STEP_LISTED_KEYS:
                passthrough.append((key, self._listed(key, value_node)))
            else:
                passthrough.append((key, self._opaque(value_node)))
        return Step(step_id, inputs, outputs, tuple(interpreted), tuple(passthrough), span)

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
            case Key.RAW_CWL, payload:
                return self._raw_cwl(payload, span, frozenset(), [0])
            case Key.INLINE_INPUT, payload:
                # A literal is the first node its own reading spends.
                return self._literal(node, frozenset(), [1]) if payload is node else InlineLiteral(
                    self._opaque(payload), span)
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
        return InlineLiteral(self._opaque(node), span)

    def _raw_cwl(self, node: yaml.nodes.Node, span: SourceSpan, path: frozenset[int], spent: list[int]) -> InputValue:
        """`!cwl name` is a raw reference; `!cwl {source: ..., ...}` is a step-input record, written at `span`.

        The record's `source` names edges (`!*`, or `{wic_alias: name}`) and workflow inputs (bare names), one or a
        list; every other field is CWL, copied out. Its entries are materialised on the path that reached the record,
        so a record holding itself is an alias cycle.
        """
        if not isinstance(node, yaml.nodes.MappingNode):
            return RawCwlRef(self._name_text(node), span)
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
        return CwlRecord(tuple(sources), tuple(fields.items()), span)

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

    def _sidecar(self, node: yaml.nodes.Node, values: Mapping[str, Mapping[str, Any]] = Grammar.SIDECAR_VALUES,
                 path: frozenset[int] = frozenset()) -> WicSidecar:
        """Parse a `wic:` block, normalising its `"(1, name)"` step keys.

        A key outside ``values`` and `steps:` is reported and dropped, since the
        block is closed (.wic reference §7); a value is checked against its schema.
        """
        steps: list[tuple[StepKey, WicSidecar]] = []
        entries: list[tuple[str, OpaqueCwl]] = []
        implementations: list[tuple[str, Document]] = []
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
                    if key == 'implementations' and isinstance(value_node, yaml.nodes.MappingNode):
                        # Each body is the document it is, with its spans. An empty one defers to whatever supplies
                        # the implementation: an empty document for it would shadow that supplier with nothing.
                        implementations = [(self._key_text(name), self.document(body))
                                           for name, body in value_node.value
                                           if isinstance(body, yaml.nodes.MappingNode) and body.value]
        return WicSidecar(tuple(steps), tuple(entries), tuple(implementations), self.span(node))

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

    def _sidecar_value(self, key: str, node: yaml.nodes.Node, schema: Mapping[str, Any]) -> OpaqueCwl:
        """Materialise one `wic:` value and check it against its schema (`wic034`).

        A value that already earned a diagnostic while being read is not checked
        again: one mistake, one diagnostic.
        """
        reported = len(self.diags)
        value: OpaqueCwl
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
            return InlineLiteral(self._read(untagged, path | {id(node)}, spent), span)
        value: Any = node.value
        if node.style is not None:
            return InlineLiteral(value, span)
        try:
            value = yaml.safe_load(value) if value else value
        except yaml.YAMLError:
            pass  # not a primitive: its text is the honest reading
        except _UNREADABLE:
            read = yaml.compose(value, Loader=yaml.SafeLoader)
            assert read is not None  # a non-empty scalar composes to a node
            self.error(SophiosErrorCode.UNKNOWN_TAG, f'YAML cannot read this value as {_tag_name(read.tag)}', node)
        return InlineLiteral(value, span, None if '\n' in node.value else node.value)

    def _opaque(self, node: yaml.nodes.Node, path: frozenset[int] = frozenset(),
                spent: list[int] | None = None) -> OpaqueCwl:
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

    def _read(self, node: yaml.nodes.Node, path: frozenset[int], spent: list[int]) -> OpaqueCwl:
        """Read a node `_opaque` has entered, or the untagged copy of one."""
        match _construct(node, True), node:
            case (Key.RAW_CWL, yaml.nodes.MappingNode() as body), _:
                return self._raw_cwl(body, self.span(node), path, spent)
            case (Key.INLINE_INPUT, payload), _ if payload is node:
                return self._literal(node, path, spent)
            case (key, payload), _ if payload is node or key == Key.ANCHOR:
                return self._input_value(node)  # a name, or a misplaced edge definition, reported there
            case _, yaml.nodes.SequenceNode():
                return [self._opaque(item, path, spent) for item in node.value]
            case _, yaml.nodes.MappingNode():
                return {self._key_text(key): self._opaque(value, path, spent) for key, value in node.value}
        scalar: OpaqueCwl = _resolved_scalar(node)
        return scalar

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
            record = passthrough and key == Key.RAW_CWL != tagged and isinstance(payload, yaml.nodes.MappingNode)
            if key == Key.ANCHOR or record or key in Key.ALL and tagged is None:
                return key, payload
    return (tagged, node) if tagged else None


def _holds_construct(value: OpaqueCwl) -> bool:
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


def _refusals(schema: Mapping[str, Any], value: OpaqueCwl, node: yaml.nodes.Node,
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
                else:
                    yield f'{where} has no key {name!r}; its keys are: {", ".join(sorted(declared))}', key_node
        case {'items': entry}, list(), yaml.nodes.SequenceNode():
            for index, (item, item_node) in enumerate(zip(value, node.value)):
                yield from _refusals(entry, item, item_node, f'{where}[{index}]')
        case _:
            for error in Draft202012Validator(schema).iter_errors(value):
                yield f'{where} {_REFUSED[error.validator](error.validator_value, value)}', node


_KINDS: Final[Mapping[str, str]] = MappingProxyType({
    'string': 'a string', 'boolean': 'true or false', 'object': 'a mapping', 'array': 'a list'})
#: What each schema keyword of `Grammar.SIDECAR_VALUES` refuses, in the words of a `.wic` author.
_REFUSED: Final[Mapping[str, Callable[[Any, OpaqueCwl], str]]] = MappingProxyType({
    'type': lambda kind, value: f'must be {_KINDS[kind]}, found {_found(value)}',
    'enum': lambda choices, value: f'must be one of {", ".join(choices)}, found {_found(value)}',
    'minLength': lambda _least, _value: 'must not be empty',
    'pattern': lambda pattern, value: f'must match {pattern}, found {value!r}',
})


def _found(value: OpaqueCwl) -> str:
    """What a value is, for a message: a scalar as YAML read it, anything else by its kind."""
    match value:
        case dict():
            return 'a mapping'
        case list():
            return 'a list'
        case None | bool() | int() | float() | str():
            return repr(value)
    return f'a {type(value).__name__}'


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


def _resolved_scalar(node: yaml.nodes.Node) -> Any:
    """Convert a resolved scalar node to its Python value, exactly as `yaml.SafeLoader` would.

    Delegated to PyYAML's own `SafeConstructor` rather than re-implemented,
    to avoid diverging from YAML on octals, hex, timestamps, `.inf`,
    and sexagesimals. A fresh constructor per call, since the class carries
    per-document state and this layer is shared across threads.
    """
    try:
        return yaml.constructor.SafeConstructor().construct_object(node)
    except (yaml.YAMLError, *_UNREADABLE):  # reported by `_load`
        return node.value


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
