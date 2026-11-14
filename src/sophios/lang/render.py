"""Write a Sophios AST back out, in either of the YAML surface's two spellings.

Rendering is the inverse of parsing. A literal parsed from tagged YAML
carries its source text and is emitted verbatim, so the round-trip is exact
by construction; only literals that never had a spelling are serialised.
`OpaqueCwl` is closed and matched exhaustively, so a construct nested in a
collection is re-spelled rather than handed raw to the dumper.

    render(document)   ->  text,  tagged spelling      (`!ii x`)
    to_json(document)  ->  data,  desugared spelling, JSON-serialisable
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, get_args

import yaml

from ..utils_yaml import KEY_OF_TAG, Key, Tag
from .nodes import (
    CwlRecord,
    Document,
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


@dataclass(frozen=True, slots=True)
class _Tagged:
    """A value carrying a wic tag: `!tag payload`, scalar or collection.

    A scalar payload is written plain where YAML allows it, unless `quoted`:
    under `!ii` the quotes say the payload is text (language spec §2).
    """

    tag: str
    value: Any
    quoted: bool = False


# --------------------------------------------------------------------------
# The structural walk, shared by both spellings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Writer:
    """Turns an AST into plain data, in one of the two surface spellings.

    Tagged is what people write and what `render` emits; otherwise it is the
    desugared, JSON-serialisable projection `to_json` returns.
    """

    tagged: bool

    def document(self, document: Document) -> dict[str, Any]:
        """Key order follows the .wic reference: `wic:`, then `steps:`, then the rest."""
        body: dict[str, Any] = {}

        if document.sidecar is not None:
            body['wic'] = self.sidecar(document.sidecar)

        if document.steps or document.steps_as_mapping:
            # Emitted in the surface form it was parsed from, so a round-trip
            # does not silently restyle a file.
            body['steps'] = (
                {step.id: self.step(step) for step in document.steps}
                if document.steps_as_mapping
                # 'id' re-assigned after the spread so a stray passthrough
                # 'id' can never win — dict displays keep first position but
                # take the last value.
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
        """One `out:` entry: a bare name, or a name bound to an edge, defined only here (.wic reference §3.6)."""
        if binding.edge_def is None:
            return binding.name
        return {binding.name: self.construct(Tag.ANCHOR, binding.edge_def.name)}

    def construct(self, tag: str, payload: Any) -> Any:
        """Spell one construct: `!tag payload`, or `{key: payload}` desugared."""
        return _Tagged(tag, payload) if self.tagged else {KEY_OF_TAG[tag]: payload}

    def sidecar(self, sidecar: WicSidecar) -> Any:
        """A `wic:` block, restoring its `(index, name)` step keys.

        Children are re-wrapped in `SIDECAR_WRAPPER_KEY`, matching the
        parser's unwrap. An empty block renders `{}`, never `None`, since
        consumers defend against a missing key with `.get(k, {})`.
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
            case EdgeRef(name=name):
                return self.construct(Tag.ALIAS, name)
            case RawCwlRef(expression=expression):
                return self.construct(Tag.RAW_CWL, expression)
            case UnresolvedName(name=name):
                return name
            case CwlRecord(sources=sources, fields=fields):
                body: dict[str, Any] = {}
                if sources:
                    spelled = [self.input_value(source) for source in sources]
                    # One source stays a list beside `linkMerge`, which CWL reads as a merge of a list.
                    body['source'] = spelled[0] if len(spelled) == 1 and 'linkMerge' not in dict(fields) \
                        else spelled
                body.update({key: self.plain(value) for key, value in fields})
                return self.construct(Tag.RAW_CWL, body)

    def _literal(self, literal: InlineLiteral) -> Any:
        """Spell an `!ii` literal: transcribed from source text when parsed, else serialised."""
        if self.tagged and literal.text is not None:
            return _Tagged(Tag.INLINE_INPUT, literal.text)
        if not self.tagged or isinstance(literal.value, (list, dict)):
            return self.construct(Tag.INLINE_INPUT, self.plain(literal.value))
        if isinstance(literal.value, get_args(InputValue)):
            # A construct as the direct payload has no tagged spelling — two
            # tags cannot share a node — so the desugared form carries it.
            return {Key.INLINE_INPUT: self.plain(literal.value)}
        return _spell_scalar(literal.value)

    def plain(self, value: OpaqueCwl) -> Any:
        """Spell passthrough content, exhaustively over the closed `OpaqueCwl` union."""
        match value:
            case InlineLiteral() | EdgeRef() | RawCwlRef() | UnresolvedName() | CwlRecord():
                return self.input_value(value)
            case dict():
                return {k: self.plain(v) for k, v in value.items()}
            case list():
                return [self.plain(v) for v in value]
            case datetime() | date() if not self.tagged:
                return value.isoformat()  # JSON has no date type
            case None | bool() | int() | float() | str() | date() | datetime():
                return value


# --------------------------------------------------------------------------
# Scalar spelling for literals that never had a source text
# --------------------------------------------------------------------------


def _spell_scalar(value: Any) -> _Tagged:
    """Return the tagged spelling that reads back as the scalar `value`.

    The parser reads an `!ii` scalar as YAML reads the same scalar untagged
    (language spec §2), so a string is written plain when YAML resolves that
    text as a string, and quoted otherwise: `!ii '0'` is the text 0. Any
    other scalar is written as `yaml.safe_dump` spells it, plain.
    """
    if isinstance(value, str):
        resolved = yaml.resolver.Resolver().resolve(yaml.nodes.ScalarNode, value, (True, False))
        return _Tagged(Tag.INLINE_INPUT, value, quoted=resolved != 'tag:yaml.org,2002:str')
    return _Tagged(Tag.INLINE_INPUT, yaml.safe_dump(value, default_flow_style=True).partition('\n')[0].strip())


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
            # Plain is asked for, and `_WicDumper` grants it where YAML's own
            # analysis of the text allows; a quoted payload gets the quote
            # style the text needs.
            return dumper.represent_scalar(data.tag, str(data.value), style=None if data.quoted else '')


class _WicDumper(yaml.SafeDumper):
    """A dumper that knows the wic tags and does not fold long lines."""

    def ignore_aliases(self, data: Any) -> bool:
        """Never emit YAML anchors; wic has its own edge syntax."""
        return True

    def choose_scalar_style(self) -> str:
        """Allow plain scalars after a wic tag.

        PyYAML only permits plain style when a scalar's tag is implicit, so
        an explicit tag would otherwise force quotes on every value.
        """
        event = self.event
        if isinstance(event, yaml.events.ScalarEvent) and event.tag in Tag.ALL and event.style == '':
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
    body = _Writer(tagged=True).document(document)
    if not body:
        return ''
    return yaml.dump(body, Dumper=_WicDumper, sort_keys=False, default_flow_style=False, width=10_000)


def to_json(document: Document) -> dict[str, Any]:
    """Project a document into JSON-serialisable data, desugared.

    This is what a consumer without YAML tags sees, and what the exported
    JSON Schema describes. `json.dumps` of the result always succeeds.
    """
    return _Writer(tagged=False).document(document)
