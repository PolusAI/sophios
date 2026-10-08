"""Parse `.wic` source into a typed AST.

The parser composes YAML to a node tree rather than loading it to plain
Python objects, because composition preserves the source marks that make
diagnostics worth reading. Nothing here raises: a caller always receives a
result carrying whatever was parsed plus whatever went wrong.
"""
import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping, TypeAlias, final

import yaml

from ..utils_yaml import Key, Tag
from .diagnostics import Diagnostics
from .error_codes import SophiosErrorCode
from .nodes import (
    RECORD_FIELDS,
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
    cwl_record,
)
from .cwl import CWL_VERSIONS
from .spans import SourceSpan
from .support import STEP_INPUT_RECORD_KEYS
from .values import Anything, AnyMapping, Flag, ListOf, OneOf, Record, Text, ValueShape

#: A `graphviz: style:` value: one or more styles, comma separated.
_GRAPHVIZ_STYLE: Final = ('((,\\s*)*(dashed|dotted|solid|invis|bold|tapered|filled|striped|wedged'
                          '|diagonals|rounded))+')
#: A `graphviz: ranksame:` entry: a `wic: steps:` key, `(index, name)`.
_RANKSAME_ENTRY: Final = '\\([0-9]+, [A-Za-z0-9_\\.]+\\)'
_NON_EMPTY: Final = Text(min_length=1)


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

    #: Every value key a `wic:` block admits (§5), with the shape its value
    #: takes. The block is Sophios's own metadata, not passthrough CWL, so it
    #: is closed: any other key is `wic033`, and a value of the wrong shape is
    #: `wic034`. The schema generator reads the same table.
    SIDECAR_VALUES: Final[Mapping[str, ValueShape]] = MappingProxyType({
        'graphviz': Record((('label', _NON_EMPTY),
                            ('style', Text(pattern=_GRAPHVIZ_STYLE)),
                            ('ranksame', ListOf(Text(pattern=_RANKSAME_ENTRY))))),
        'implementation': _NON_EMPTY,
        'implementations': AnyMapping(),
        'default_implementation': _NON_EMPTY,
        'version': _NON_EMPTY,
        'lang_version': _NON_EMPTY,
        'driver': OneOf(('slurm', 'argo')),
        'namespace': _NON_EMPTY,
        'inlineable': Flag(),
    })

    #: What a `wic: steps:` entry admits: a `wic:` block's values, plus what
    #: it says about the step it names -- the step keys it may override, and
    #: that step's `inference:` rules. Siblings of the entry's own `wic:`
    #: wrapper are folded into the one block it parses to.
    SIDECAR_STEP_VALUES: Final[Mapping[str, ValueShape]] = MappingProxyType({
        **SIDECAR_VALUES,
        'in': Anything(),
        'out': Anything(),
        'scatter': Anything(),
        'scatterMethod': OneOf(('dotproduct', 'flat_crossproduct', 'nested_crossproduct')),
        'inference': Anything(),
    })

    #: What an authored `cwlVersion:` may say: a version the substrate
    #: toolchain runs (§1). The compiler still writes its own; any other value
    #: is `wic035`, and the schema generator states the same enum.
    CWL_VERSION_VALUE: Final = OneOf(CWL_VERSIONS)

    #: Every key a `wic:` block admits: its values, and `steps:`, which is
    #: structure rather than a value.
    SIDECAR_KEYS: Final = frozenset(SIDECAR_VALUES) | {'steps'}

    #: Every key a `wic: steps:` entry admits.
    SIDECAR_STEP_KEYS: Final = frozenset(SIDECAR_STEP_VALUES) | {'steps'}

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

    @property
    def ok(self) -> bool:
        """Whether a document was produced with no errors."""
        return self.document is not None and not self.diagnostics.has_errors


def _every_node(root: yaml.nodes.Node) -> list[yaml.nodes.Node]:
    """Return every distinct node in the composed graph, once, tracked by identity."""
    seen: set[int] = set()
    order: list[yaml.nodes.Node] = []
    stack: list[yaml.nodes.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        order.append(node)
        if isinstance(node, yaml.nodes.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.nodes.MappingNode):
            for key_node, value_node in node.value:
                stack.append(key_node)
                stack.append(value_node)
    return order


#: What PyYAML's constructors raise on a node they cannot build: the wrong node
#: kind for the tag, or a scalar the tag's constructor cannot read.
_CONSTRUCTION_FAILURES: Final = (yaml.YAMLError, ValueError, KeyError, AttributeError, IndexError)


class _CoreConstructor(yaml.constructor.SafeConstructor):
    """`SafeConstructor` that leaves every `!`-tagged node unbuilt.

    Those tags are checked on their own, so a core node holding one is judged
    on the rest of its content.
    """


_CoreConstructor.add_multi_constructor('!', lambda _loader, _suffix, _node: None)


#: The tag each collection kind resolves to when none is written. Such a node's
#: own failure (a collection used as a key) is reported where the key is read,
#: so only a tag that disagrees with the node's kind is checked here.
_DEFAULT_TAGS: Final[dict[type[yaml.nodes.Node], str]] = {yaml.nodes.MappingNode: 'tag:yaml.org,2002:map',
                                                          yaml.nodes.SequenceNode: 'tag:yaml.org,2002:seq'}


_MERGE_TAG: Final = 'tag:yaml.org,2002:merge'


def _holds_merge_key(node: yaml.nodes.Node) -> bool:
    return isinstance(node, yaml.nodes.MappingNode) and any(key.tag == _MERGE_TAG for key, _ in node.value)


def _unconstructible(node: yaml.nodes.Node, merge_keys: set[int]) -> bool:
    """Whether the loader's `SafeConstructor` cannot build this node.

    A `!`-tagged node is built by its own tag's check, but a mapping holding a
    `<<` key is still checked for the merge the loader applies to it.
    """
    if node.tag == _MERGE_TAG:
        # No constructor of its own: the mapping holding `<<` consumes it, so only a key is safe.
        return id(node) not in merge_keys
    if node.tag.startswith('!'):
        if not _holds_merge_key(node):
            return False
        node = copy.copy(node)
        node.tag = _DEFAULT_TAGS[yaml.nodes.MappingNode]
    elif _DEFAULT_TAGS.get(type(node)) == node.tag and not _holds_merge_key(node):
        return False
    try:
        # Building a mapping flattens its merge keys in place; a copy keeps the tree intact.
        _CoreConstructor().construct_object(copy.deepcopy(node), deep=True)
    except _CONSTRUCTION_FAILURES:
        return True
    return False


def _tag_name(tag: str) -> str:
    """A core tag as its short name (`timestamp`), any other as written."""
    prefix = 'tag:yaml.org,2002:'
    return tag.removeprefix(prefix) if tag.startswith(prefix) and len(tag) > len(prefix) else repr(tag)


def _report_unknown_tags(root: yaml.nodes.Node, file: str, diags: Diagnostics) -> None:
    """Report a tag the language does not own, or cannot construct, wherever it appears.

    Applied to every node rather than called at each consuming position, so a
    position added later is covered by default. The payload is kept,
    untagged, for recovery. A node whose tag does not start with `!` is built
    with the loader's own constructor, so the parser agrees with the loader
    by construction: `!!foo`, `!<verbatim>`, `!!str [a]`, `!!int abc` are all
    rejected. A node is reported only when it is the innermost failure, not
    once more for each ancestor that holds it.
    """
    nodes = _every_node(root)
    merge_keys = {id(key) for node in nodes if isinstance(node, yaml.nodes.MappingNode)
                  for key, _ in node.value if key.tag == _MERGE_TAG}
    failing = {id(node) for node in nodes if _unconstructible(node, merge_keys)}
    for node in nodes:
        if node.tag.startswith('!') and node.tag not in Tag.ALL:
            diags.error(SophiosErrorCode.UNKNOWN_TAG,
                        f'unknown tag {node.tag!r}; the Sophios tags are !ii, !&, !*, and !cwl',
                        SourceSpan.of(file, node))
        if id(node) in failing and not any(
                id(inner) in failing and inner is not node for inner in _every_node(node)):
            message = ('YAML cannot merge into this mapping; `<<` takes a mapping or a list of mappings'
                       if node.tag.startswith('!')
                       else f'YAML cannot read this value as {_tag_name(node.tag)}')
            diags.error(SophiosErrorCode.UNKNOWN_TAG, message, SourceSpan.of(file, node))


def _in_reading_order(diags: Diagnostics) -> Diagnostics:
    """Sort diagnostics by position, so a reader works down the file once."""
    return Diagnostics(sorted(
        diags, key=lambda d: (d.span.start_line, d.span.start_column) if d.span else (0, 0)))


def parse(text: str, filename: str = '<string>') -> ParseResult:
    """Parse `.wic` source text into a `Document`.

    This function is total: it never raises, for any input. Malformed YAML,
    wrong node kinds, and unknown tags are all reported as diagnostics.
    """
    diagnostics = Diagnostics()
    whole = SourceSpan(filename, 1, 1, text.count('\n') + 1, 1)

    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        diagnostics.error(SophiosErrorCode.INVALID_YAML, _yaml_error_message(exc),
                          _yaml_error_span(filename, exc, whole))
        return ParseResult(None, diagnostics)

    if root is None:  # An empty document is well-formed and carries nothing.
        return ParseResult(Document(span=whole), diagnostics)

    _report_unknown_tags(root, filename, diagnostics)

    if not isinstance(root, yaml.nodes.MappingNode):
        diagnostics.error(
            SophiosErrorCode.NOT_A_MAPPING,
            f'a Sophios document must be a mapping, found {_kind(root)}',
            SourceSpan.of(filename, root),
        )
        return ParseResult(None, _in_reading_order(diagnostics))

    document = _document(root, filename, diagnostics)
    return ParseResult(document, _in_reading_order(diagnostics))


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def _unique_entries(node: yaml.nodes.MappingNode, file: str, diags: Diagnostics,
                    what: str) -> list[tuple[str, yaml.nodes.Node]]:
    """Return a mapping's entries with duplicate keys reported and dropped.

    Applied at every mapping boundary the language owns, so downstream code
    may rely on uniqueness after this point.
    """
    seen: set[str] = set()
    kept: list[tuple[str, yaml.nodes.Node]] = []
    for key_node, value_node in node.value:
        key = _key_text(key_node, file, diags)
        if key in seen:
            diags.error(SophiosErrorCode.DUPLICATE_KEY,
                        f'{what} {key!r} is defined more than once',
                        SourceSpan.of(file, key_node))
            continue
        seen.add(key)
        kept.append((key, value_node))
    return kept


def _listed(key: str, node: yaml.nodes.Node, file: str, diags: Diagnostics) -> OpaqueCwl:
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
    value = _opaque(node, file, diags)
    if not (isinstance(node, yaml.nodes.SequenceNode) and isinstance(value, list)):
        return value
    if any(isinstance(entry, dict) and entry.keys() & Grammar.IMPORT_KEYS for entry in value):
        return value
    named_by = Grammar.LISTED_BY[key]
    mapping: dict[str, OpaqueCwl] = {}
    for entry_node, entry in zip(node.value, value, strict=True):
        span = SourceSpan.of(file, entry_node)
        name = entry.get(named_by) if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not isinstance(name, str):
            diags.error(SophiosErrorCode.EXPECTED_MAPPING,
                        f'a list-form {key}: entry must be a mapping with a string {named_by}:', span)
            continue
        if named_by == 'id':
            written, name = name, name.rsplit('#', 1)[-1]
            if not name:
                diags.error(SophiosErrorCode.EXPECTED_MAPPING,
                            f'a list-form {key}: entry id: {written!r} names no port: '
                            'the name is the part after the last #, and it must not be empty', span)
                continue
        if name in mapping:
            diags.error(SophiosErrorCode.DUPLICATE_KEY, f'{key}: entry {name!r} is defined more than once', span)
            continue
        mapping[name] = {field: item for field, item in entry.items() if field != named_by}
    return mapping


def _document(root: yaml.nodes.MappingNode, file: str, diags: Diagnostics) -> Document:
    """Build a Document from the root mapping node."""
    steps: tuple[Step, ...] = ()
    steps_as_mapping = False
    sidecar: WicSidecar | None = None
    passthrough: list[tuple[str, OpaqueCwl]] = []

    for key, value_node in _unique_entries(root, file, diags, 'top-level key'):
        match key:
            case 'steps':
                steps, steps_as_mapping = _steps(value_node, file, diags)
            case 'wic':
                sidecar = _sidecar(value_node, file, diags)
            case 'cwlVersion':
                version = _opaque(value_node, file, diags)
                for message, at in Grammar.CWL_VERSION_VALUE.problems(version, value_node, key):
                    diags.error(SophiosErrorCode.UNSUPPORTED_CWL_VERSION, message, SourceSpan.of(file, at))
                passthrough.append((key, version))
            case key if key in Grammar.LISTED_BY:
                passthrough.append((key, _listed(key, value_node, file, diags)))
            case _:
                passthrough.append((key, _opaque(value_node, file, diags)))

    return Document(
        steps=steps,
        sidecar=sidecar,
        passthrough=tuple(passthrough),
        span=SourceSpan.of(file, root),
        steps_as_mapping=steps_as_mapping,
    )


def _steps(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> tuple[tuple[Step, ...], bool]:
    """Parse `steps:`, which may be a mapping or a sequence, into the same AST."""
    match node:
        case yaml.nodes.MappingNode():
            entries = _unique_entries(node, file, diags, 'step')
            return tuple(_step(name, v, file, diags) for name, v in entries), True
        case yaml.nodes.SequenceNode():
            return tuple(_sequence_step(item, file, diags) for item in node.value), False
        case _:
            diags.error(
                SophiosErrorCode.EXPECTED_MAPPING,
                f'steps: must be a mapping or a sequence, found {_kind(node)}',
                SourceSpan.of(file, node),
            )
            return (), False


def _sequence_step(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> Step:
    """Parse one entry of a sequence-form `steps:`; identity comes from an `id:` key.

    A single-key mapping (`- touch:`) is not a second sequence form: CWL lifts
    a key into `id` only when the field's value is a mapping, so in a
    sequence the key is never lifted and the step has no identity. Reported
    as `wic006` (reference §3.1).
    """
    span = SourceSpan.of(file, node)
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(SophiosErrorCode.EXPECTED_MAPPING, f'each step must be a mapping, found {_kind(node)}', span)
        return Step(id='', span=span)

    keyed = [(key_node, _key_text(key_node, file, diags), value_node) for key_node, value_node in node.value]
    id_entries = [(key_node, value_node) for key_node, key, value_node in keyed if key == 'id']
    if len(id_entries) > 1:
        # Identity must not be ambiguous; keeps the first id: and reports the rest.
        for key_node, _ in id_entries[1:]:
            diags.error(SophiosErrorCode.DUPLICATE_KEY, "step key 'id' is defined more than once",
                        SourceSpan.of(file, key_node))
    if id_entries:
        id_node = id_entries[0][1]
        step_id = _name_text(id_node, file, diags)
        if not step_id:
            diags.error(SophiosErrorCode.EMPTY_STEP_ID, 'id: must be a non-empty string', SourceSpan.of(file, id_node))
        body = [(key_node, value_node) for key_node, key, value_node in keyed if key != 'id']
        return _step_body(step_id, body, span, file, diags)

    if len(node.value) == 1 and isinstance(node.value[0][0], yaml.nodes.ScalarNode):
        # A single scalar key is the only case where a name can be offered
        # back in the message; a collection key has already earned wic005.
        _key_node, name, body_node = keyed[0]
        named = f"write '- id: {name}' if {name!r} is the step's name"
        forgotten = f"add the '- id:' line above if {name!r} is one of the step's own keys"
        first, second = (forgotten, named) if name in Grammar.STEP_KEYS else (named, forgotten)
        diags.error(
            SophiosErrorCode.MISSING_STEP_ID,
            f'a step in a sequence carries its name in an id: key — {first}; {second}. '
            f'Keying the whole steps: block by name is the other form (§3.1)',
            span,
        )
        # The reading decided above also decides which node holds the body:
        # under the forgotten-`id:` reading the entry itself is the body.
        if name in Grammar.STEP_KEYS:
            return _rejected_step('', list(node.value), span, file, diags)
        # Under the named reading, the key's value is the body, checked by
        # the mapping form's own code.
        _step(name, body_node, file, diags)  # diagnostics only; the step is discarded
        return Step(id='', span=span)

    diags.error(
        SophiosErrorCode.MISSING_STEP_ID,
        'a step in a sequence needs an id:',
        span,
    )
    return _rejected_step('', list(node.value), span, file, diags)


def _rejected_step(
    step_id: str,
    entries: list[tuple[yaml.nodes.Node, yaml.nodes.Node]],
    span: SourceSpan,
    file: str,
    diags: Diagnostics,
) -> Step:
    """Walk and report on the body of a sequence entry with no usable identity.

    The step itself is discarded, but its body is walked anyway so one pass
    reports everything it can see. The returned `Step` always carries no id.
    """
    _step_body(step_id, entries, span, file, diags)
    return Step(id='', span=span)


def _step(step_id: str, node: yaml.nodes.Node, file: str, diags: Diagnostics) -> Step:
    """Parse a step body, keyed by its id.

    A step may legitimately have no body at all (`some_step.wic:` with nothing
    under it), which YAML resolves to null.
    """
    span = SourceSpan.of(file, node)
    if node.tag == 'tag:yaml.org,2002:null':
        return Step(id=step_id, span=span)
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(SophiosErrorCode.EXPECTED_MAPPING, f'step {step_id!r} must be a mapping, found {_kind(node)}', span)
        return Step(id=step_id, span=span)
    return _step_body(step_id, list(node.value), span, file, diags)


def _step_body(
    step_id: str,
    entries: list[tuple[yaml.nodes.Node, yaml.nodes.Node]],
    span: SourceSpan,
    file: str,
    diags: Diagnostics,
) -> Step:
    """Split a step's keys into inputs, outputs, interpreted CWL, and passthrough."""
    inputs: tuple[tuple[str, InputValue], ...] = ()
    outputs: tuple[OutputBinding, ...] = ()
    interpreted: list[tuple[str, OpaqueCwl]] = []
    passthrough: list[tuple[str, OpaqueCwl]] = []

    seen: set[str] = set()
    for key_node, value_node in entries:
        key = _key_text(key_node, file, diags)
        if key in seen:
            diags.error(SophiosErrorCode.DUPLICATE_KEY, f'step key {key!r} is defined more than once',
                        SourceSpan.of(file, key_node))
            continue
        seen.add(key)
        if key == 'id':
            # The step's identity always arrives from elsewhere by the time
            # this runs — a mapping key, an already extracted id:, or the name
            # quoted in a rejected entry's own diagnostic — so an id: here is a
            # second, contradictory identity. Rendering would have to pick one
            # silently; report it.
            diags.error(SophiosErrorCode.DUPLICATE_KEY,
                        f'step {step_id!r} already has its identity; a second id: is contradictory',
                        SourceSpan.of(file, key_node))
            continue
        if key == 'in':
            inputs = _inputs(value_node, file, diags)
        elif key == 'out':
            outputs = _outputs(value_node, file, diags)
        elif key in Grammar.INTERPRETED_STEP_KEYS:
            interpreted.append((key, _opaque(value_node, file, diags)))
        elif key in Grammar.STEP_LISTED_KEYS:
            passthrough.append((key, _listed(key, value_node, file, diags)))
        else:
            passthrough.append((key, _opaque(value_node, file, diags)))

    return Step(
        id=step_id,
        inputs=inputs,
        outputs=outputs,
        interpreted=tuple(interpreted),
        passthrough=tuple(passthrough),
        span=span,
    )


def _inputs(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> tuple[tuple[str, InputValue], ...]:
    """Parse a step's `in:` mapping into typed input values.

    A repeated key is reported rather than silently resolved: binding the same
    input twice is ambiguous, and picking either one would hide a mistake.
    """
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(SophiosErrorCode.EXPECTED_MAPPING, f'in: must be a mapping, found {_kind(node)}',
                    SourceSpan.of(file, node))
        return ()

    seen: set[str] = set()
    bindings: list[tuple[str, InputValue]] = []
    for key_node, value_node in node.value:
        name = _key_text(key_node, file, diags)
        if name in seen:
            diags.error(
                SophiosErrorCode.DUPLICATE_KEY,
                f'input {name!r} is bound more than once',
                SourceSpan.of(file, key_node),
            )
            continue
        seen.add(name)
        bindings.append((name, _input_value(value_node, file, diags)))
    return tuple(bindings)


def _input_value(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> InputValue:
    """Classify one step input into the closed `InputValue` union.

    Each construct has two equivalent surface forms — tagged (`!ii empty.txt`)
    and desugared (`{wic_inline_input: empty.txt}`) — that must produce the
    same node. An untagged scalar is an `UnresolvedName`, resolved later
    against workflow inputs. `!&`/`wic_anchor` is checked separately: it is a
    known tag, just in the wrong position (§4.1.1), so it is diagnosed as
    `wic019` rather than `wic009 UNKNOWN_TAG`.
    """
    span = SourceSpan.of(file, node)

    if _is_edge_def(node):
        # A well-formed name is kept for recovery even though the construct
        # is rejected here.
        diags.error(
            SophiosErrorCode.MISPLACED_EDGE_DEF,
            "'!&' defines an edge, and an edge is defined where its value comes into being: "
            "a step's out: entry (§4.1.1). Use '!*' to consume an edge",
            span,
        )
        return UnresolvedName(_recovered_edge_name(node), span)

    build = Forms.TAGGED.get(node.tag)
    if build is not None:
        return build(node, file, diags, span)

    desugared = _desugared_form(node, file, diags, span)
    if desugared is not None:
        return desugared

    if isinstance(node, yaml.nodes.ScalarNode):
        return UnresolvedName(node.value, span)
    if isinstance(node, yaml.nodes.MappingNode):
        keys = {str(key.value) for key, _ in node.value if isinstance(key, yaml.nodes.ScalarNode)}
        if keys and keys <= STEP_INPUT_RECORD_KEYS:
            diags.error(
                SophiosErrorCode.STEP_INPUT_RECORD,
                'this mapping spells a CWL step input (' + ', '.join(sorted(keys)) + '), which '
                'Sophios does not read untagged; write `!cwl {source: ..., default: ...}` for a CWL '
                'step input, `!ii` for a literal of that shape',
                span)
    # A bare mapping or sequence cannot name a workflow input, so it is only
    # meaningful as a literal; a step-input record is kept as one for recovery.
    return InlineLiteral(_opaque(node, file, diags), span)


def _recovered_edge_name(node: yaml.nodes.Node) -> str:
    """Return the name a misplaced edge definition carried, without diagnosing it."""
    target = node
    if isinstance(node, yaml.nodes.MappingNode) and len(node.value) == 1:
        target = node.value[0][1]
    if not isinstance(target, yaml.nodes.ScalarNode):
        return ''
    text: str = target.value
    return text


def _is_edge_def(node: yaml.nodes.Node) -> bool:
    """Whether `node` spells an edge definition, in either surface form (§6.1)."""
    if node.tag == Tag.ANCHOR:
        return True
    return (isinstance(node, yaml.nodes.MappingNode)
            and len(node.value) == 1
            and getattr(node.value[0][0], 'value', None) == Key.ANCHOR)


def _is_desugared_record(node: yaml.nodes.Node) -> bool:
    """Whether `node` spells a step-input record as `{wic_raw_cwl: {...}}`."""
    return (isinstance(node, yaml.nodes.MappingNode)
            and len(node.value) == 1
            and getattr(node.value[0][0], 'value', None) == Key.RAW_CWL
            and isinstance(node.value[0][1], yaml.nodes.MappingNode))


def _desugared_form(
    node: yaml.nodes.Node,
    file: str,
    diags: Diagnostics,
    span: SourceSpan,
) -> InputValue | None:
    """Recognise the single-key mapping form of a wic construct, if present."""
    if not isinstance(node, yaml.nodes.MappingNode) or len(node.value) != 1:
        return None
    key_node, value_node = node.value[0]
    key = _key_text(key_node, file, diags)
    build = Forms.DESUGARED.get(key)
    if build is None:
        _report_misspelled_construct(key, key_node, file, diags)
        return None
    return build(value_node, file, diags, span)


#: `wic_` in *construct* position is Sophios-owned; not in name position,
#: where an input port or step may be called anything.
CONSTRUCT_PREFIX: Final = 'wic_'


def _report_misspelled_construct(key: str, key_node: yaml.nodes.Node,
                                 file: str, diags: Diagnostics) -> None:
    """Report a single-key mapping that reaches for a construct and misses.

    A desugared key shares its namespace with passthrough CWL, which is open
    by definition (§1), so a misspelled construct like `wic_inline_inpt`
    would otherwise vanish silently into the emitted document.
    """
    if not key.startswith(CONSTRUCT_PREFIX):
        return
    diags.error(
        SophiosErrorCode.RESERVED_KEY,
        f"{key!r} is not a Sophios construct, and a single-key mapping beginning 'wic_' is read as "
        f'one. The constructs are: {", ".join(sorted(Forms.DESUGARED_KEYS))}',
        SourceSpan.of(file, key_node))


def _merged_entries(node: yaml.nodes.MappingNode) -> list[tuple[yaml.nodes.Node, yaml.nodes.Node]]:
    """The entries of `node` with its `<<` merge keys applied, as the loader builds them.

    A merge the loader cannot flatten is reported by the tag check; its entries are read as written.
    """
    merged = copy.deepcopy(node)
    try:
        _CoreConstructor().flatten_mapping(merged)
    except _CONSTRUCTION_FAILURES:
        return list(node.value)
    return list(merged.value)


def _raw_cwl(node: yaml.nodes.Node, file: str, diags: Diagnostics, span: SourceSpan) -> InputValue:
    """`!cwl name` is a raw reference; `!cwl {source: ..., ...}` is a step-input record.

    The record's entries are materialised one by one: materialising the tagged
    node itself would route it back here.
    """
    if not isinstance(node, yaml.nodes.MappingNode):
        return RawCwlRef(_name_text(node, file, diags), span)
    body = {_key_text(key, file, diags): _opaque(value, file, diags) for key, value in _merged_entries(node)}
    record, bad = cwl_record(body, span)
    for key in bad:
        if key == 'source':
            message = '!cwl record: `source` names edges (!*) or workflow inputs, one or a list'
        elif key in RECORD_FIELDS:
            message = (f'!cwl record: {key!r} holds a Sophios construct; every field but source is CWL, '
                       'written out as is')
        else:
            message = (f'!cwl record: {key!r} is not a WorkflowStepInput field Sophios writes; the fields '
                       f'are source, {", ".join(sorted(RECORD_FIELDS))}')
        diags.error(SophiosErrorCode.STEP_INPUT_RECORD, message, span)
    if not bad and not record.sources and not {'default', 'valueFrom'} & dict(record.fields).keys():
        diags.error(SophiosErrorCode.STEP_INPUT_RECORD,
                    '!cwl record: with no source, default or valueFrom the step input receives no value; '
                    'leave it unbound for Sophios to connect, or give it a source or a default',
                    span)
    return record


#: One builder: a YAML node and its context in, one input node out.
Builder: TypeAlias = Callable[[yaml.nodes.Node, str, Diagnostics, SourceSpan], InputValue]


@final
class Forms:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """Each construct's two spellings (§6.1), and what each builds."""

    #: Tagged spellings — what people write. `!&` (`Tag.ANCHOR`) is
    #: deliberately absent: it is legal only on an `out:` entry, checked via
    #: `_out_edge_def` before this table is consulted (§4.1.1).
    TAGGED: Final[Mapping[str, Builder]] = MappingProxyType({
        Tag.INLINE_INPUT: lambda n, f, d, s: InlineLiteral(_literal(n, f, d), s, text=_literal_text(n)),
        Tag.ALIAS: lambda n, f, d, s: EdgeRef(_name_text(n, f, d), s),
        Tag.RAW_CWL: _raw_cwl,
    })

    #: Desugared spellings — what tooling emits. `wic_anchor` (`Key.ANCHOR`)
    #: is absent for the same reason as `Tag.ANCHOR` above.
    DESUGARED: Final[Mapping[str, Builder]] = MappingProxyType({
        Key.INLINE_INPUT: lambda n, f, d, s: InlineLiteral(_opaque(n, f, d), s),
        Key.ALIAS: lambda n, f, d, s: EdgeRef(_name_text(n, f, d), s),
        Key.RAW_CWL: _raw_cwl,
    })

    #: The desugared construct keys, derived so a construct added above
    #: needs no second edit.
    DESUGARED_KEYS: Final = frozenset(DESUGARED)


def _outputs(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> tuple[OutputBinding, ...]:
    """Parse a step's `out:` sequence."""
    if not isinstance(node, yaml.nodes.SequenceNode):
        diags.error(SophiosErrorCode.EXPECTED_SEQUENCE, f'out: must be a sequence, found {_kind(node)}',
                    SourceSpan.of(file, node))
        return ()
    return tuple(_output_binding(item, file, diags) for item in node.value)


def _output_binding(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> OutputBinding:
    """Parse one `out:` entry: a bare name, or a name bound to an edge definition."""
    span = SourceSpan.of(file, node)
    match node:
        case yaml.nodes.ScalarNode():
            return OutputBinding(node.value, None, span)
        case yaml.nodes.MappingNode() if len(node.value) == 1:
            key_node, value_node = node.value[0]
            name = _key_text(key_node, file, diags)
            edge = _out_edge_def(value_node, file, diags)
            if edge is None:
                # The value was not an edge definition in either spelling.
                # Report it rather than let it vanish: the AST promises to
                # preserve what it was given, and a silent drop is the one
                # thing a total parser must never do.
                diags.error(
                    SophiosErrorCode.EXPECTED_SCALAR,
                    f'out: entry {name!r} must bind an !& edge definition; its value is neither !& nor wic_anchor',
                    SourceSpan.of(file, value_node),
                )
            return OutputBinding(name, edge, span)
        case _:
            diags.error(
                SophiosErrorCode.EXPECTED_SCALAR,
                'each out: entry must be a name or a single-key mapping',
                span,
            )
            return OutputBinding('', None, span)


def _sidecar_out_entry(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> OpaqueCwl:
    """Parse a `wic:` sidecar step's `out:` entry exactly as a step's own `out:` (§4.1.1).

    Re-expressed as `OpaqueCwl` passthrough, in the desugared shape
    `render.py` emits for a stored edge def, since a bare `EdgeDef` is not a
    member of that closed union.
    """
    return [binding.name if binding.edge_def is None
            else {binding.name: {Key.ANCHOR: binding.edge_def.name}}
            for binding in _outputs(node, file, diags)]


#: The key a nested sidecar step wraps its child sidecar in, on the surface.
#: Read by both the parser (unwrap) and the renderer (re-wrap), so the two
#: cannot disagree about it.
SIDECAR_WRAPPER_KEY: Final = 'wic'


def _child_sidecar_node(node: yaml.nodes.Node) -> yaml.nodes.Node:
    """Unwrap the `wic:` key a nested sidecar step carries on the surface (§5).

    Also handles a merged sibling shape — `{wic: {namespace: ...}, out: [...]}`
    — folding siblings into the unwrapped mapping so nothing nested under the
    sibling `wic:` is stranded opaque and unread.
    """
    if not isinstance(node, yaml.nodes.MappingNode):
        return node
    wrapper: yaml.nodes.Node | None = None
    siblings: list[tuple[yaml.nodes.Node, yaml.nodes.Node]] = []
    for key_node, value_node in node.value:
        if getattr(key_node, 'value', None) == SIDECAR_WRAPPER_KEY:
            wrapper = value_node
        else:
            siblings.append((key_node, value_node))
    if wrapper is None:
        return node
    assert isinstance(wrapper, yaml.nodes.Node)  # untyped tuple from PyYAML
    if not siblings:
        return wrapper
    if isinstance(wrapper, yaml.nodes.ScalarNode) and wrapper.tag == 'tag:yaml.org,2002:null':
        # An empty wrapper beside its siblings is an empty mapping, as a bare
        # `{wic: }` already is; otherwise `wic` itself would reach the key check.
        return yaml.nodes.MappingNode('tag:yaml.org,2002:map', siblings,
                                      start_mark=node.start_mark, end_mark=node.end_mark)
    if not isinstance(wrapper, yaml.nodes.MappingNode):
        return node
    return yaml.nodes.MappingNode(wrapper.tag, [*wrapper.value, *siblings],
                                  start_mark=node.start_mark, end_mark=node.end_mark)


def _out_edge_def(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> EdgeDef | None:
    """Recognise an edge definition in either spelling, or None."""
    if node.tag == Tag.ANCHOR:
        return EdgeDef(_name_text(node, file, diags), SourceSpan.of(file, node))
    if isinstance(node, yaml.nodes.MappingNode) and len(node.value) == 1:
        key_node, value_node = node.value[0]
        if _key_text(key_node, file, diags) == Key.ANCHOR:
            return EdgeDef(_name_text(value_node, file, diags), SourceSpan.of(file, value_node))
    return None


def _sidecar_implementations(node: yaml.nodes.MappingNode, file: str,
                             diags: Diagnostics) -> list[tuple[str, Document]]:
    """Parse each `implementations:` body as the document it is, with spans intact."""
    parsed: list[tuple[str, Document]] = []
    for key_node, body in node.value:
        name = _key_text(key_node, file, diags)
        # An empty body defers to whatever supplies the implementation; recording
        # an empty document for it would shadow that supplier with nothing.
        if isinstance(body, yaml.nodes.MappingNode) and body.value:
            parsed.append((name, _document(body, file, diags)))
    return parsed


def _sidecar(node: yaml.nodes.Node, file: str, diags: Diagnostics,
             _path: frozenset[int] = frozenset(),
             values: Mapping[str, ValueShape] = Grammar.SIDECAR_VALUES) -> WicSidecar:
    """Parse a `wic:` block, normalising its `"(1, name)"` step keys.

    A key outside ``values`` and `steps:` is reported and dropped, since the
    block is closed (§5); a value is checked against its declared shape.
    """
    span = SourceSpan.of(file, node)
    if id(node) in _path:
        diags.error(SophiosErrorCode.RECURSIVE_ALIAS, 'alias cycle: a wic: block contains itself', span)
        return WicSidecar(span=span)
    if node.tag == 'tag:yaml.org,2002:null':
        # `wic:` with nothing under it is an empty sidecar, not an error.
        return WicSidecar(span=span)
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(SophiosErrorCode.EXPECTED_MAPPING, f'wic: must be a mapping, found {_kind(node)}', span)
        return WicSidecar(span=span)

    steps: list[tuple[StepKey, WicSidecar]] = []
    entries: list[tuple[str, OpaqueCwl]] = []
    implementations: list[tuple[str, Document]] = []

    for key, value_node in _admitted_entries(node, file, diags, frozenset(values) | {'steps'}):
        if key != 'steps':
            entries.append((key, _sidecar_value(key, value_node, values[key], file, diags)))
            if key == 'implementations' and isinstance(value_node, yaml.nodes.MappingNode):
                implementations.extend(
                    _sidecar_implementations(value_node, file, diags))
            continue
        if not isinstance(value_node, yaml.nodes.MappingNode):
            diags.error(
                SophiosErrorCode.EXPECTED_MAPPING,
                f'wic: steps: must be a mapping, found {_kind(value_node)}',
                SourceSpan.of(file, value_node),
            )
            continue
        seen_steps: set[str] = set()
        for sub_key, sub_value in value_node.value:
            key_text = _key_text(sub_key, file, diags)
            if key_text in seen_steps:
                diags.error(SophiosErrorCode.DUPLICATE_KEY,
                            f'wic: step key {key_text!r} is defined more than once',
                            SourceSpan.of(file, sub_key))
                continue
            seen_steps.add(key_text)
            parsed = _step_key(key_text)
            if parsed is None:
                diags.error(
                    SophiosErrorCode.MALFORMED_WIC_STEP_KEY,
                    f'wic: step key {key_text!r} must have the form "(index, name)" or be a step id',
                    SourceSpan.of(file, sub_key),
                )
                continue
            steps.append((parsed, _sidecar(_child_sidecar_node(sub_value), file, diags, _path | {id(node)},
                                           Grammar.SIDECAR_STEP_VALUES)))

    return WicSidecar(steps=tuple(steps), entries=tuple(entries),
                      implementations=tuple(implementations), span=span)


def _admitted_entries(node: yaml.nodes.MappingNode, file: str, diags: Diagnostics,
                      admitted: frozenset[str]) -> list[tuple[str, yaml.nodes.Node]]:
    """A `wic:` block's unique entries, each key it does not have reported
    at the key and dropped (`wic033`).

    A key that is not a scalar has been reported already (`wic005`), and is
    not reported twice.
    """
    for key_node, _ in node.value:
        if isinstance(key_node, yaml.nodes.ScalarNode) and str(key_node.value) not in admitted:
            diags.error(
                SophiosErrorCode.UNKNOWN_WIC_KEY,
                f'{key_node.value!r} is not a wic: key. The keys are: {", ".join(sorted(admitted))}',
                SourceSpan.of(file, key_node))
    return [(key, value) for key, value in _unique_entries(node, file, diags, 'wic: entry')
            if key in admitted]


def _sidecar_value(key: str, node: yaml.nodes.Node, shape: ValueShape,
                   file: str, diags: Diagnostics) -> OpaqueCwl:
    """Materialise one `wic:` value and report each way it misses its
    declared shape, at the node that misses (`wic034`).

    A value that already earned a diagnostic while being read is not checked
    again: one mistake, one diagnostic.
    """
    reported = len(diags)
    value = _sidecar_out_entry(node, file, diags) if key == 'out' else _opaque(node, file, diags)
    if len(diags) == reported:
        for message, at in shape.problems(value, node, f'wic: {key}'):
            diags.error(SophiosErrorCode.MALFORMED_WIC_VALUE, message, SourceSpan.of(file, at))
    return value


def _step_key(text: str) -> StepKey | None:
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


def _literal_text(node: yaml.nodes.Node) -> str | None:
    """Return the source spelling of a tagged scalar literal, or None for collections."""
    return str(node.value) if isinstance(node, yaml.nodes.ScalarNode) else None


def _literal(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> Any:
    """Materialise an `!ii` payload, which may be a scalar, mapping, or sequence.

    A custom tag suppresses YAML's own type resolution, so `!ii 5` arrives as
    the text "5"; re-resolving it here must reproduce `inlineinput_constructor`
    exactly.
    """
    if not isinstance(node, yaml.nodes.ScalarNode):
        return _opaque(node, file, diags, _wrap_self=False)
    if node.value == '':
        return ''
    try:
        return yaml.safe_load(node.value)
    except yaml.YAMLError:
        # Not a primitive; the literal text is the honest interpretation.
        return node.value


#: Passthrough nodes materialised per parse before the walk is cut off, to
#: stop a YAML alias "billion laughs" expansion attack.
_EXPANSION_BUDGET: Final = 100_000


def _opaque(node: yaml.nodes.Node, file: str, diags: Diagnostics,
            _path: frozenset[int] = frozenset(), _spent: list[int] | None = None,
            _wrap_self: bool = True) -> OpaqueCwl:
    """Materialise a node Sophios does not interpret, preserving it verbatim.

    Custom wic tags are still recognised inside otherwise-opaque content so
    that an edge reference buried in passthrough is not silently flattened to
    a plain string.
    """
    # Totality against adversarial aliases: a node already on the current
    # path is a cycle (compose() resolves an alias to the same object), and a
    # widening alias chain is cut off by the budget. Both are reported once.
    spent = _spent if _spent is not None else [0]
    if id(node) in _path:
        diags.error(SophiosErrorCode.RECURSIVE_ALIAS, 'alias cycle: a node contains itself', SourceSpan.of(file, node))
        return None
    spent[0] += 1
    if spent[0] > _EXPANSION_BUDGET:
        if spent[0] == _EXPANSION_BUDGET + 1:  # report once, not per node
            diags.error(SophiosErrorCode.RECURSIVE_ALIAS,
                        f'alias expansion exceeds {_EXPANSION_BUDGET} nodes; refusing to materialise',
                        SourceSpan.of(file, node))
        return None
    path = _path | {id(node)}

    if _is_edge_def(node):
        # `out:` never reaches here — `_out_edge_def` owns it — so this is
        # always the misplaced case, reported by `_input_value`.
        return _input_value(node, file, diags)

    if node.tag in (Tag.ANCHOR, Tag.ALIAS, Tag.RAW_CWL) or (
            node.tag == Tag.INLINE_INPUT and isinstance(node, yaml.nodes.ScalarNode)) or _is_desugared_record(node):
        # Name-carrying tags route through the construct builder regardless
        # of node kind, so the tag is never silently stripped. Collection
        # `!ii` is handled below instead, on the materialised content. A
        # record spelled `{wic_raw_cwl: {...}}` is `!cwl {...}` (§6.1), its
        # body checked the same wherever it is written.
        return _input_value(node, file, diags)

    content: OpaqueCwl
    match node:
        case yaml.nodes.ScalarNode() if node.tag == Tag.INLINE_INPUT:
            content = None  # pragma: no cover — routed above; keeps match total
        case yaml.nodes.ScalarNode():
            content = _resolved_scalar(node)
        case yaml.nodes.SequenceNode():
            content = [_opaque(item, file, diags, path, spent) for item in node.value]
        case yaml.nodes.MappingNode():
            content = {_key_text(k, file, diags): _opaque(v, file, diags, path, spent) for k, v in node.value}
        case _:  # pragma: no cover — compose() emits only the three kinds above
            content = node.value

    if node.tag == Tag.INLINE_INPUT and _wrap_self:
        # A collection `!ii {a: 1}` wraps its materialised content rather
        # than re-dispatching into an infinite loop.
        return InlineLiteral(content, SourceSpan.of(file, node))
    return content


def _resolved_scalar(node: yaml.nodes.ScalarNode) -> Any:
    """Convert a resolved scalar node to its Python value, exactly as the loader would.

    Delegated to PyYAML's own `SafeConstructor` rather than re-implemented,
    to avoid diverging from the loader on octals, hex, timestamps, `.inf`,
    and sexagesimals. A fresh constructor per call, since the class carries
    per-document state and this layer is shared across threads.
    """
    try:
        return yaml.constructor.SafeConstructor().construct_object(node)
    except _CONSTRUCTION_FAILURES:  # reported by _report_unknown_tags
        return node.value


def _name_text(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> str:
    """Return a scalar node's literal text, reporting anything non-scalar.

    Edge names and CWL references are identifiers, so the raw text is what
    is meant — resolving `false` to a bool, or `01` to an int, would rename
    them.
    """
    if not isinstance(node, yaml.nodes.ScalarNode):
        diags.error(SophiosErrorCode.EXPECTED_SCALAR,
                    f'an edge or reference name must be a scalar, found {_kind(node)}',
                    SourceSpan.of(file, node))
        return ''
    return str(node.value)


def _key_text(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> str:
    """Return a mapping key as text.

    YAML admits collection keys (`? [a, b]`); Sophios does not, since every
    key in the language is a name. A non-scalar key is reported and
    stringified for recovery.
    """
    if not isinstance(node, yaml.nodes.ScalarNode):
        diags.error(SophiosErrorCode.EXPECTED_SCALAR,
                    f'mapping keys must be scalars, found {_kind(node)}',
                    SourceSpan.of(file, node))
    return str(node.value)


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


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """Extract a single-line message from a PyYAML error."""
    problem = getattr(exc, 'problem', None)
    return str(problem) if problem else str(exc).splitlines()[0]


def _yaml_error_span(file: str, exc: yaml.YAMLError, fallback: SourceSpan) -> SourceSpan:
    """Locate a PyYAML error, falling back to the whole document."""
    mark = getattr(exc, 'problem_mark', None)
    if mark is None:
        return fallback
    return SourceSpan(file, mark.line + 1, mark.column + 1, mark.line + 1, mark.column + 1)
