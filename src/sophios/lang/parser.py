"""Parse `.wic` source into a typed AST.

The parser composes YAML to a node tree rather than loading it to plain Python
objects, because composition preserves the source marks that make diagnostics
worth reading. Nothing here raises: a caller always receives a result carrying
whatever was parsed plus whatever went wrong.

See design_docs/core-refactor-design.md, Spec 1.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping, TypeAlias, final

import yaml

from ..utils_yaml import Key, Tag
from .diagnostics import Code, Diagnostics
from .nodes import (
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
from .spans import SourceSpan


@final
class Grammar:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The syntax layer's fixed vocabulary.

    A namespace rather than loose module constants: these describe one thing —
    what the language admits — and reading them together is how you check that.
    Every member is immutable and built once at import, so they are shared
    safely across threads and survive `fork` without coordination.
    """

    #: CWL keys on a step that Sophios reads *and acts upon*. Everything else
    #: on a step is passthrough, by definition. Enumerating this set is what
    #: makes the leak boundary a specification rather than an accident.
    INTERPRETED_STEP_KEYS: Final = frozenset({'scatter', 'scatterMethod', 'when', 'run'})

    #: `wic:` sidecar step keys have the surface form "(1, step_name)".
    WIC_STEP_KEY: Final = re.compile(r'^\(\s*(\d+)\s*,\s*(.+?)\s*\)$')

    #: The same rule as a string, for consumers that need to state it rather
    #: than apply it — the exported JSON Schema, principally.
    WIC_STEP_KEY_PATTERN: Final = WIC_STEP_KEY.pattern

    # NOTE: no SCALAR_TAGS table here. Scalar resolution is delegated to
    # PyYAML's SafeConstructor (see _resolved_scalar) — a hand-written table
    # diverged from the loader, which review caught.


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
        diagnostics.error(Code.INVALID_YAML, _yaml_error_message(exc), _yaml_error_span(filename, exc, whole))
        return ParseResult(None, diagnostics)

    if root is None:  # An empty document is well-formed and carries nothing.
        return ParseResult(Document(span=whole), diagnostics)

    if not isinstance(root, yaml.nodes.MappingNode):
        diagnostics.error(
            Code.NOT_A_MAPPING,
            f'a Sophios document must be a mapping, found {_kind(root)}',
            SourceSpan.of(filename, root),
        )
        return ParseResult(None, diagnostics)

    return ParseResult(_document(root, filename, diagnostics), diagnostics)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def _unique_entries(node: yaml.nodes.MappingNode, file: str, diags: Diagnostics,
                    what: str) -> list[tuple[str, yaml.nodes.Node]]:
    """Yield a mapping's entries with duplicate keys reported and dropped.

    Applied at every mapping boundary the language owns. YAML leaves repeated
    keys undefined; the loader silently keeps the last one, and a renderer's
    dict silently keeps one of them too — so the only honest treatment is a
    diagnostic at parse time. After this boundary, downstream code may rely on
    uniqueness, which is what makes every writer comprehension total.
    """
    seen: set[str] = set()
    kept: list[tuple[str, yaml.nodes.Node]] = []
    for key_node, value_node in node.value:
        key = _key_text(key_node, file, diags)
        if key in seen:
            diags.error(Code.DUPLICATE_KEY,
                        f'{what} {key!r} is defined more than once',
                        SourceSpan.of(file, key_node))
            continue
        seen.add(key)
        kept.append((key, value_node))
    return kept


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
    """Parse `steps:`, which may be a mapping or a sequence.

    Both surface forms are long-standing and remain supported; the AST is the
    same either way, so nothing downstream needs to care which was written.
    """
    match node:
        case yaml.nodes.MappingNode():
            entries = _unique_entries(node, file, diags, 'step')
            return tuple(_step(name, v, file, diags) for name, v in entries), True
        case yaml.nodes.SequenceNode():
            return tuple(_sequence_step(item, file, diags) for item in node.value), False
        case _:
            diags.error(
                Code.EXPECTED_MAPPING,
                f'steps: must be a mapping or a sequence, found {_kind(node)}',
                SourceSpan.of(file, node),
            )
            return (), False


def _sequence_step(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> Step:
    """Parse one entry of a sequence-form `steps:`.

    Two surface forms are in long-standing use and both remain supported:

        - id: touch        # explicit id, body alongside it
          in: {...}

        - touch:           # single-key mapping, the key is the id
            in: {...}
    """
    span = SourceSpan.of(file, node)
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(Code.EXPECTED_MAPPING, f'each step must be a mapping, found {_kind(node)}', span)
        return Step(id='', span=span)

    id_node = next((v for k, v in node.value if _key_text(k, file, diags) == 'id'), None)
    if id_node is not None:
        step_id = _name_text(id_node, file, diags)
        if not step_id:
            diags.error(Code.EMPTY_STEP_ID, 'id: must be a non-empty string', SourceSpan.of(file, id_node))
        body = [(k, v) for k, v in node.value if _key_text(k, file, diags) != 'id']
        return _step_body(step_id, body, span, file, diags)

    if len(node.value) == 1:
        key_node, value_node = node.value[0]
        return _step(_key_text(key_node, file, diags), value_node, file, diags)

    diags.error(
        Code.MISSING_STEP_ID,
        'a step in a sequence needs either an id: or a single name key',
        span,
    )
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
        diags.error(Code.EXPECTED_MAPPING, f'step {step_id!r} must be a mapping, found {_kind(node)}', span)
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
            diags.error(Code.DUPLICATE_KEY, f'step key {key!r} is defined more than once',
                        SourceSpan.of(file, key_node))
            continue
        seen.add(key)
        if key == 'id':
            # The step's identity always arrives from elsewhere by the time
            # this runs — the mapping key, the single name key, or an already
            # extracted id: — so an id: here is a second, contradictory
            # identity. Rendering would have to pick one silently; report it.
            diags.error(Code.DUPLICATE_KEY,
                        f'step {step_id!r} already has its identity; a second id: is contradictory',
                        SourceSpan.of(file, key_node))
            continue
        if key == 'in':
            inputs = _inputs(value_node, file, diags)
        elif key == 'out':
            outputs = _outputs(value_node, file, diags)
        elif key in Grammar.INTERPRETED_STEP_KEYS:
            interpreted.append((key, _opaque(value_node, file, diags)))
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
        diags.error(Code.EXPECTED_MAPPING, f'in: must be a mapping, found {_kind(node)}', SourceSpan.of(file, node))
        return ()

    seen: set[str] = set()
    bindings: list[tuple[str, InputValue]] = []
    for key_node, value_node in node.value:
        name = _key_text(key_node, file, diags)
        if name in seen:
            diags.error(
                Code.DUPLICATE_KEY,
                f'input {name!r} is bound more than once',
                SourceSpan.of(file, key_node),
            )
            continue
        seen.add(name)
        bindings.append((name, _input_value(value_node, file, diags)))
    return tuple(bindings)


def _input_value(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> InputValue:
    """Classify one step input into the closed `InputValue` union.

    Each construct has two equivalent surface forms and both must produce the
    same node (see the language reference, "Two surface forms"):

        tagged     filename: !ii empty.txt
        desugared  filename: {wic_inline_input: empty.txt}

    Humans write the tagged form; the Python API emits the desugared form,
    because a constructor that re-emitted its own tag would fire again on
    reload. An untagged scalar is an `UnresolvedName`, which resolution later
    binds to a workflow input or reports on.
    """
    span = SourceSpan.of(file, node)

    build = Forms.TAGGED.get(node.tag)
    if build is not None:
        return build(node, file, diags, span)

    _reject_unknown_tag(node, file, diags, span)

    desugared = _desugared_form(node, file, diags, span)
    if desugared is not None:
        return desugared

    if isinstance(node, yaml.nodes.ScalarNode):
        return UnresolvedName(node.value, span)
    # A bare mapping or sequence cannot name a workflow input, so it is only
    # meaningful as a literal.
    return InlineLiteral(_opaque(node, file, diags), span)


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
    build = Forms.DESUGARED.get(_key_text(key_node, file, diags))
    if build is None:
        return None
    return build(value_node, file, diags, span)


#: One builder: a YAML node and its context in, one input node out.
Builder: TypeAlias = Callable[[yaml.nodes.Node, str, Diagnostics, SourceSpan], InputValue]


@final
class Forms:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """Each construct's two spellings, and what each builds.

    Kept side by side and in one namespace because the language's claim is that
    the two spellings are equivalent (§6.1). Splitting them into separate
    module globals is how they drift apart — which is exactly the defect CE-02
    recorded. Both tables are read-only views.
    """

    #: Tagged spellings — what people write.
    TAGGED: Final[Mapping[str, Builder]] = MappingProxyType({
        Tag.INLINE_INPUT: lambda n, f, d, s: InlineLiteral(_literal(n, f, d), s, text=_literal_text(n)),
        Tag.ANCHOR: lambda n, f, d, s: EdgeDef(_name_text(n, f, d), s),
        Tag.ALIAS: lambda n, f, d, s: EdgeRef(_name_text(n, f, d), s),
        Tag.RAW_CWL: lambda n, f, d, s: RawCwlRef(_name_text(n, f, d), s),
    })

    #: Desugared spellings — what tooling emits.
    DESUGARED: Final[Mapping[str, Builder]] = MappingProxyType({
        Key.INLINE_INPUT: lambda n, f, d, s: InlineLiteral(_opaque(n, f, d), s),
        Key.ANCHOR: lambda n, f, d, s: EdgeDef(_name_text(n, f, d), s),
        Key.ALIAS: lambda n, f, d, s: EdgeRef(_name_text(n, f, d), s),
        Key.RAW_CWL: lambda n, f, d, s: RawCwlRef(_name_text(n, f, d), s),
    })

    #: The desugared construct keys. Derived from the table above so a
    #: construct added there needs no second edit anywhere else.
    DESUGARED_KEYS: Final = frozenset(DESUGARED)


def _outputs(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> tuple[OutputBinding, ...]:
    """Parse a step's `out:` sequence."""
    if not isinstance(node, yaml.nodes.SequenceNode):
        diags.error(Code.EXPECTED_SEQUENCE, f'out: must be a sequence, found {_kind(node)}', SourceSpan.of(file, node))
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
                    Code.EXPECTED_SCALAR,
                    f'out: entry {name!r} must bind an !& edge definition; its value is neither !& nor wic_anchor',
                    SourceSpan.of(file, value_node),
                )
            return OutputBinding(name, edge, span)
        case _:
            diags.error(
                Code.EXPECTED_SCALAR,
                'each out: entry must be a name or a single-key mapping',
                span,
            )
            return OutputBinding('', None, span)


#: The key a nested sidecar step wraps its child sidecar in, on the surface.
#: One constant read by both the parser (unwrap) and the renderer (re-wrap),
#: so the two cannot disagree about it — the PR #383 review found the parser
#: unwrapping and the renderer never re-wrapping, invisible to the round-trip
#: property precisely because the parser tolerates its own renderer's output.
SIDECAR_WRAPPER_KEY: Final = 'wic'


def _child_sidecar_node(node: yaml.nodes.Node) -> yaml.nodes.Node:
    """Unwrap the `wic:` key a nested sidecar step carries on the surface.

    `wic: steps: (1, outer):` holds `{wic: {steps: ...}}`, not a bare sidecar
    (see docs/advanced.md and reference §5). Without the unwrap, everything at
    depth two or more sat in `entries` as opaque content and `(1, inner)` was
    never normalised to a `StepKey` — falsifying the very docstring that says
    nobody downstream should ever parse that string again.
    """
    if isinstance(node, yaml.nodes.MappingNode) and len(node.value) == 1:
        key_node, value_node = node.value[0]
        if getattr(key_node, 'value', None) == SIDECAR_WRAPPER_KEY:
            assert isinstance(value_node, yaml.nodes.Node)  # untyped tuple from PyYAML
            return value_node
    return node


def _out_edge_def(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> EdgeDef | None:
    """Recognise an edge definition in either spelling, or None."""
    if node.tag == Tag.ANCHOR:
        return EdgeDef(_name_text(node, file, diags), SourceSpan.of(file, node))
    if isinstance(node, yaml.nodes.MappingNode) and len(node.value) == 1:
        key_node, value_node = node.value[0]
        if _key_text(key_node, file, diags) == Key.ANCHOR:
            return EdgeDef(_name_text(value_node, file, diags), SourceSpan.of(file, value_node))
    return None


def _sidecar(node: yaml.nodes.Node, file: str, diags: Diagnostics,
             _path: frozenset[int] = frozenset()) -> WicSidecar:
    """Parse a `wic:` block, normalising its `"(1, name)"` step keys."""
    span = SourceSpan.of(file, node)
    if id(node) in _path:
        diags.error(Code.RECURSIVE_ALIAS, 'alias cycle: a wic: block contains itself', span)
        return WicSidecar(span=span)
    if node.tag == 'tag:yaml.org,2002:null':
        # `wic:` with nothing under it is an empty sidecar, not an error.
        return WicSidecar(span=span)
    if not isinstance(node, yaml.nodes.MappingNode):
        diags.error(Code.EXPECTED_MAPPING, f'wic: must be a mapping, found {_kind(node)}', span)
        return WicSidecar(span=span)

    steps: list[tuple[StepKey, WicSidecar]] = []
    entries: list[tuple[str, OpaqueCwl]] = []

    for key, value_node in _unique_entries(node, file, diags, 'wic: entry'):
        if key != 'steps':
            entries.append((key, _opaque(value_node, file, diags)))
            continue
        if not isinstance(value_node, yaml.nodes.MappingNode):
            diags.error(
                Code.EXPECTED_MAPPING,
                f'wic: steps: must be a mapping, found {_kind(value_node)}',
                SourceSpan.of(file, value_node),
            )
            continue
        seen_steps: set[str] = set()
        for sub_key, sub_value in value_node.value:
            key_text = _key_text(sub_key, file, diags)
            if key_text in seen_steps:
                diags.error(Code.DUPLICATE_KEY,
                            f'wic: step key {key_text!r} is defined more than once',
                            SourceSpan.of(file, sub_key))
                continue
            seen_steps.add(key_text)
            parsed = _step_key(key_text)
            if parsed is None:
                diags.error(
                    Code.MALFORMED_WIC_STEP_KEY,
                    f'wic: step key {key_text!r} must have the form "(index, name)"',
                    SourceSpan.of(file, sub_key),
                )
                continue
            steps.append((parsed, _sidecar(_child_sidecar_node(sub_value), file, diags, _path | {id(node)})))

    return WicSidecar(steps=tuple(steps), entries=tuple(entries), span=span)


def _step_key(text: str) -> StepKey | None:
    """Normalise a `"(1, name)"` sidecar key, or None if it is malformed."""
    match = Grammar.WIC_STEP_KEY.match(text)
    if match is None:
        return None
    return StepKey(int(match.group(1)), match.group(2))


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------


def _literal_text(node: yaml.nodes.Node) -> str | None:
    """The source spelling of a tagged scalar literal, or None for collections.

    `value` is a pure function of this text (`yaml.safe_load`), so emitting the
    text verbatim reproduces the value exactly — rendering becomes
    transcription, and the lossy business of guessing a YAML spelling for a
    Python value is reserved for literals that never had one (API-built).
    """
    return str(node.value) if isinstance(node, yaml.nodes.ScalarNode) else None


def _literal(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> Any:
    """Materialise an `!ii` payload, which may be a scalar, mapping, or sequence.

    A custom tag suppresses YAML's own type resolution, so `!ii 5` arrives as
    the text "5". Re-resolving it here reproduces `inlineinput_constructor`
    exactly, which matters: the two must agree or the specification would
    quietly change what existing documents mean.
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


#: Passthrough nodes materialised per parse before the walk is cut off. YAML
#: aliases make exponential expansion writable in ten lines ("billion laughs");
#: a real workflow is nowhere near this, so the ceiling only stops attacks.
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
        diags.error(Code.RECURSIVE_ALIAS, 'alias cycle: a node contains itself', SourceSpan.of(file, node))
        return None
    spent[0] += 1
    if spent[0] > _EXPANSION_BUDGET:
        if spent[0] == _EXPANSION_BUDGET + 1:  # report once, not per node
            diags.error(Code.RECURSIVE_ALIAS,
                        f'alias expansion exceeds {_EXPANSION_BUDGET} nodes; refusing to materialise',
                        SourceSpan.of(file, node))
        return None
    path = _path | {id(node)}

    _reject_unknown_tag(node, file, diags)

    if node.tag in (Tag.ANCHOR, Tag.ALIAS, Tag.RAW_CWL) or (
            node.tag == Tag.INLINE_INPUT and isinstance(node, yaml.nodes.ScalarNode)):
        # Name-carrying tags route through the construct builder whatever the
        # node kind: `!& []` is a diagnostic — a name cannot be a collection,
        # and the loader rejects the same text. Falling through would strip
        # the tag silently. Scalar `!ii` routes the same way; collection `!ii`
        # is handled below, on the materialised content, because its builder
        # would call back into this walk on the very same node.
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
        # A collection literal: `!ii {a: 1}` in passthrough is the same
        # construct as in input position, wrapped around its materialised
        # content rather than re-dispatched into an infinite loop. `_literal`
        # passes _wrap_self=False because its caller already wraps.
        return InlineLiteral(content, SourceSpan.of(file, node))
    return content


def _resolved_scalar(node: yaml.nodes.ScalarNode) -> Any:
    """Convert a resolved scalar node to its Python value, exactly as the
    loader would.

    Delegated to PyYAML's own `SafeConstructor` rather than re-implemented:
    a hand-written table diverged from the loader on octals, hex, timestamps,
    `.inf`, and sexagesimals, which is precisely the "specification quietly
    changes what existing documents mean" this module exists to prevent. A
    fresh constructor per call — the class carries per-document state, and
    this layer is shared across threads.
    """
    try:
        return yaml.constructor.SafeConstructor().construct_object(node)
    except yaml.constructor.ConstructorError:
        # The resolver claimed a type the text cannot produce; the raw text
        # is the honest fallback.
        return node.value


def _name_text(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> str:
    """Return a scalar node's literal text, reporting anything non-scalar.

    Edge names and CWL references are identifiers, so the raw text is what is
    meant — resolving `false` to a bool, or `01` to an int, would rename them.
    This matches `anchor_constructor`, which uses `construct_scalar` — and,
    like it, a collection is an error: `!& []` names nothing, and the loader
    rejects the same text with a ConstructorError.
    """
    if not isinstance(node, yaml.nodes.ScalarNode):
        diags.error(Code.EXPECTED_SCALAR,
                    f'an edge or reference name must be a scalar, found {_kind(node)}',
                    SourceSpan.of(file, node))
        return ''
    return str(node.value)


def _key_text(node: yaml.nodes.Node, file: str, diags: Diagnostics) -> str:
    """Return a mapping key as text.

    YAML admits collection keys (`? [a, b]`); Sophios does not — every key in
    the language is a name. A non-scalar key is reported and stringified for
    recovery, rather than silently becoming the repr of a node list.

    A key can also carry a tag, and this is the position that used to forget
    it: `!: :` composed to a mapping whose key node was tagged, the tag was
    dropped by the stringify below, and the parser accepted a document the
    loader refuses — the one direction the language promises never to take.
    """
    _reject_unknown_tag(node, file, diags)
    if not isinstance(node, yaml.nodes.ScalarNode):
        diags.error(Code.EXPECTED_SCALAR,
                    f'mapping keys must be scalars, found {_kind(node)}',
                    SourceSpan.of(file, node))
    return str(node.value)


def _reject_unknown_tag(node: yaml.nodes.Node, file: str, diags: Diagnostics,
                        span: SourceSpan | None = None) -> None:
    """Report a tag the language does not own, wherever it appears.

    One home for the rule, deliberately. It used to be written out at each
    position that consumes a node — once for input values, once for the
    passthrough walk — and a third position, mapping keys, simply never got a
    copy. Restating a rule per position is how a position gets missed; now a
    new one has a single obvious thing to call.

    The rule itself: Sophios owns four tags, the loader rejects every other,
    and the specification must never be more permissive than the thing it
    specifies. The payload is kept, untagged, for recovery.
    """
    if node.tag.startswith('!') and node.tag not in Tag.ALL:
        diags.error(Code.UNKNOWN_TAG,
                    f'unknown tag {node.tag!r}; the Sophios tags are !ii, !&, !*, and !cwl',
                    span if span is not None else SourceSpan.of(file, node))


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
