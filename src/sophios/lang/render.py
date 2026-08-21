"""Write a Sophios AST back out, in either of the YAML surface's two spellings.

Rendering is the inverse of parsing, and having both is what makes the syntax
layer checkable — the exactness of that claim is enforced by the round-trip
property in `tests/core/test_lang_render.py`, which is the claim's single home.

Design, after the PR #383 review:

*Transcription over reconstruction.* A literal parsed from tagged YAML carries
its source text, and rendering emits that text verbatim — `value` was computed
from it, so the round-trip is exact by construction. Only literals that never
had a spelling (API-built, desugared-form) are serialised, via PyYAML's own
emitter, and where the tagged form structurally cannot express a value (the
string '0': the composer strips quotes before `!ii` payloads are re-resolved)
the desugared spelling is used instead of a lossy tag.

*Totality over the closed union.* `OpaqueCwl` is a closed recursive type, and
`plain` matches it exhaustively — a construct nested inside a collection is
re-spelled, never handed raw to the dumper. Collections under `!ii` render
tagged in every position, block-style, so a passthrough construct survives the
round-trip as itself.

    render(document)   ->  text,  tagged spelling      (`!ii x`)
    to_json(document)  ->  data,  desugared spelling, JSON-serialisable

See docs/sophios_language_reference.md.
"""
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal, final

import yaml

from ..utils_yaml import Key, Tag
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
    UnresolvedName,
    WicSidecar,
)
from .parser import SIDECAR_WRAPPER_KEY


@final
class _Emit:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """Everything the emitter needs to decide how to write a value.

    Grouped so the two rules that govern output style sit together, and
    immutable so they can be read from any thread without coordination.
    """

    #: Every tag the tagged spelling emits.
    WIC_TAGS: Final = Tag.ALL

    #: Text needing no quoting after a tag: no leading YAML indicator, no
    #: whitespace, and nothing that would start a comment or end the scalar.
    PLAIN_SAFE: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\Z')


@dataclass(frozen=True, slots=True)
class _Tagged:
    """A value carrying a wic tag: `!tag payload`, scalar or collection."""

    tag: str
    value: Any


# --------------------------------------------------------------------------
# The structural walk, shared by both spellings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Writer:
    """Turns an AST into plain data, in one of the two surface spellings.

    `mode='tagged'` is what people write and what `render` emits;
    `mode='json'` is the desugared, JSON-serialisable projection `to_json`
    returns — dates become ISO-8601 text there, because JSON has no date.
    """

    mode: Literal['tagged', 'json']

    def document(self, document: Document) -> dict[str, Any]:
        """Key order follows the reference: `wic:`, then `steps:`, then the rest."""
        body: dict[str, Any] = {}

        if document.sidecar is not None:
            body['wic'] = self.sidecar(document.sidecar)

        if document.steps or document.steps_as_mapping:
            # Emitted in the surface form it was parsed from, so a round-trip
            # does not silently restyle a file.
            body['steps'] = (
                {step.id: self.step(step) for step in document.steps}
                if document.steps_as_mapping
                # id first for readability, and re-assigned after the spread so a
                # stray passthrough 'id' can never win — dict displays keep the
                # first position but take the last value.
                else [{'id': step.id, **self.step(step), 'id': step.id}  # pylint: disable=duplicate-key
                      for step in document.steps]
            )

        for key, value in document.passthrough:
            body[key] = self.plain(value)

        return body

    def step(self, step: Step) -> dict[str, Any]:
        """One step's keys, minus its id."""
        out: dict[str, Any] = {}
        if step.inputs:
            out['in'] = {name: self.input_value(value) for name, value in step.inputs}
        if step.outputs:
            out['out'] = [self.output(binding) for binding in step.outputs]
        for key, value in (*step.interpreted, *step.passthrough):
            out[key] = self.plain(value)
        return out

    def output(self, binding: OutputBinding) -> Any:
        """One `out:` entry: a bare name, or a name bound to an edge."""
        if binding.edge_def is None:
            return binding.name
        return {binding.name: self.input_value(binding.edge_def)}

    def sidecar(self, sidecar: WicSidecar) -> Any:
        """A `wic:` block, restoring its `(index, name)` step keys.

        Children are re-wrapped in the same key the parser unwraps
        (`SIDECAR_WRAPPER_KEY`): every downstream consumer reads through that
        wrapper explicitly, so dropping it is a semantic edit, not a
        simplification. An empty block renders `{}`, never `None` — consumers
        defend against a *missing* key with `.get(k, {})`, and a key present
        with `None` sails past that defence into an AttributeError.
        """
        out: dict[str, Any] = {key: self.plain(value) for key, value in sidecar.entries}
        if sidecar.steps:
            out['steps'] = {str(key): {SIDECAR_WRAPPER_KEY: self.sidecar(child)}
                            for key, child in sidecar.steps}
        return out

    def input_value(self, value: InputValue) -> Any:
        """Spell one input construct in this writer's mode."""
        match value:
            case InlineLiteral():
                return self._literal(value)
            case EdgeDef(name=name):
                return _Tagged(Tag.ANCHOR, name) if self.mode == 'tagged' else {Key.ANCHOR: name}
            case EdgeRef(name=name):
                return _Tagged(Tag.ALIAS, name) if self.mode == 'tagged' else {Key.ALIAS: name}
            case RawCwlRef(expression=expression):
                return _Tagged(Tag.RAW_CWL, expression) if self.mode == 'tagged' else {Key.RAW_CWL: expression}
            case UnresolvedName(name=name):
                return name

    def _literal(self, literal: InlineLiteral) -> Any:
        """Spell an `!ii` literal.

        Parsed literals are transcribed from their source text — exact by
        construction, since the value was computed from that text. Literals
        with no text are serialised: collections render tagged block-style
        with their payload walked (so nested constructs are re-spelled, never
        handed raw to the dumper), and scalars go through PyYAML's emitter
        with a desugared fallback where the tagged form cannot express the
        value at all.
        """
        if self.mode != 'tagged':
            return {Key.INLINE_INPUT: self.plain(literal.value)}

        if literal.text is not None:
            return _Tagged(Tag.INLINE_INPUT, literal.text)

        if isinstance(literal.value, (list, dict)):
            return _Tagged(Tag.INLINE_INPUT, self.plain(literal.value))

        if isinstance(literal.value, (InlineLiteral, EdgeDef, EdgeRef, RawCwlRef, UnresolvedName)):
            # A construct as the direct payload has no tagged spelling — two
            # tags cannot share a node — so the desugared form carries it.
            return {Key.INLINE_INPUT: self.plain(literal.value)}

        spelled = _spell_scalar(literal.value)
        if spelled is not None:
            return _Tagged(Tag.INLINE_INPUT, spelled)
        # The tagged form has no spelling for this value (e.g. the string
        # '0' — the composer strips quotes before the payload is re-resolved),
        # so the desugared spelling carries it instead of a lossy tag.
        return {Key.INLINE_INPUT: self.plain(literal.value)}

    def plain(self, value: OpaqueCwl) -> Any:
        """Passthrough content, exhaustively over the closed `OpaqueCwl` union.

        Every member is handled by name; there is no silent default in which a
        forgotten node kind reaches the dumper as a live dataclass.
        """
        match value:
            case InlineLiteral() | EdgeDef() | EdgeRef() | RawCwlRef() | UnresolvedName():
                return self.input_value(value)
            case dict():
                return {k: self.plain(v) for k, v in value.items()}
            case list():
                return [self.plain(v) for v in value]
            case datetime() | date() if self.mode == 'json':
                return value.isoformat()  # JSON has no date type
            case None | bool() | int() | float() | str() | date() | datetime():
                return value


# --------------------------------------------------------------------------
# Scalar spelling for literals that never had a source text
# --------------------------------------------------------------------------


def _spell_scalar(value: Any) -> str | None:
    """A tagged spelling for `value`, or None when no faithful one exists.

    PyYAML's own emitter chooses the text, so the spelling is always one its
    own resolver accepts (`.inf`, `1.0e+300`, dates). Fidelity is then checked
    against the parser's actual pipeline: a tagged payload has its quotes
    resolved by the composer *before* `yaml.safe_load` re-types the content,
    which is exactly why quoted spellings cannot protect a string like '0' —
    if the simulated round-trip does not reproduce the value, there is no
    tagged spelling, and the caller must desugar.
    """
    candidate = yaml.safe_dump(value, default_flow_style=True).partition('\n')[0].strip()
    node = yaml.compose(candidate, Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.nodes.ScalarNode):
        return None
    reparsed = yaml.safe_load(node.value) if node.value != '' else ''
    if isinstance(value, float) and isinstance(reparsed, float) and math.isnan(value) and math.isnan(reparsed):
        return candidate
    if reparsed == value and type(reparsed) is type(value):
        return candidate
    return None


# --------------------------------------------------------------------------
# YAML emission
# --------------------------------------------------------------------------


def _represent_tagged(dumper: yaml.SafeDumper, data: _Tagged) -> yaml.nodes.Node:
    """Emit a `_Tagged` as `!tag payload`, whatever shape the payload has."""
    match data.value:
        case dict():
            return dumper.represent_mapping(data.tag, data.value)
        case list():
            return dumper.represent_sequence(data.tag, data.value)
        case _:
            text = str(data.value)
            style = '' if _Emit.PLAIN_SAFE.match(text) else None
            return dumper.represent_scalar(data.tag, text, style=style)


class _WicDumper(yaml.SafeDumper):
    """A dumper that knows the wic tags and does not fold long lines."""

    def ignore_aliases(self, data: Any) -> bool:
        """Never emit YAML anchors; wic has its own edge syntax."""
        return True

    def choose_scalar_style(self) -> str:
        """Allow plain scalars after a wic tag.

        PyYAML only permits plain style when a scalar's tag is implicit, so an
        explicit tag forces quotes and every value comes out as `!ii 'x'`.
        YAML itself has no such rule, so plain style is restored for the wic
        tags whenever the value analyses as safe to write bare.
        """
        event = self.event
        if isinstance(event, yaml.events.ScalarEvent) and event.tag in _Emit.WIC_TAGS and event.style == '':
            if self.analysis is None:
                self.analysis = self.analyze_scalar(event.value)
            if self.analysis.allow_block_plain:
                return ''
        return super().choose_scalar_style()


_WicDumper.add_representer(_Tagged, _represent_tagged)


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def render(document: Document) -> str:
    """Render a document to `.wic` source text, in the tagged spelling."""
    body = _Writer('tagged').document(document)
    if not body:
        return ''
    return yaml.dump(body, Dumper=_WicDumper, sort_keys=False, default_flow_style=False, width=10_000)


def to_json(document: Document) -> dict[str, Any]:
    """Project a document into JSON-serialisable data, desugared.

    This is what a consumer without YAML tags sees, and what the exported JSON
    Schema describes. `json.dumps` of the result always succeeds — that claim
    is a property, not a comment (see `test_lang_schema.py`).
    """
    return _Writer('json').document(document)
