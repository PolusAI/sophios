"""Write a `.wic` AST back out, in either of the language's two spellings.

Rendering is the inverse of parsing, and having both is what makes the syntax
layer checkable: `parse(render(document))` must reproduce the document it
started from, or one of the two is wrong about the language.

The language reference (§6.1) says every wic construct has a *tagged* spelling
and a *desugared* one, and that they mean the same thing. That claim is load
bearing, so it is expressed here as one structural walk parameterised by which
spelling to use, rather than as two walks that have to be kept in agreement:

    render(document)   ->  text,  tagged spelling      (`!ii x`)
    to_json(document)  ->  data,  desugared spelling   (`{wic_inline_input: x}`)

`render` emits the tagged form because that is what the reference tells people
to write. `to_json` produces the desugared form because that is the projection
a JSON-shaped consumer sees — JSON has no notion of a YAML tag.

See docs/wic_language_reference.md.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

import yaml

from ..utils_yaml import (
    KEY_ALIAS,
    KEY_ANCHOR,
    KEY_INLINE_INPUT,
    TAG_ALIAS,
    TAG_ANCHOR,
    TAG_INLINE_INPUT,
)
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
from .parser import KEY_RAW_CWL, TAG_RAW_CWL

#: How one input value is spelled. The two implementations below are the
#: tagged and desugared surface forms of the same construct.
Spelling: TypeAlias = Callable[[InputValue], Any]

#: Every tag the tagged spelling emits.
_WIC_TAGS: Final = frozenset({TAG_INLINE_INPUT, TAG_ANCHOR, TAG_ALIAS, TAG_RAW_CWL})

#: Text needing no quoting after a tag: no leading YAML indicator, no
#: whitespace, and nothing that would start a comment or end the scalar.
_PLAIN_SAFE: Final = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\Z')


class _Tagged:
    """A value carrying a wic tag, so the YAML dumper emits `!tag value`."""

    __slots__ = ('tag', 'value')

    def __init__(self, tag: str, value: str) -> None:
        self.tag = tag
        self.value = value


# --------------------------------------------------------------------------
# The two spellings
# --------------------------------------------------------------------------


def _tagged(value: InputValue) -> Any:
    """Spell an input value with its YAML tag, as people write it."""
    match value:
        case InlineLiteral(value=literal) if _is_scalar(literal):
            return _Tagged(TAG_INLINE_INPUT, _scalar_text(literal))
        case InlineLiteral(value=literal):
            # A tag cannot carry a collection on one line, so the single case
            # the tagged spelling cannot express falls back to the other one.
            return {KEY_INLINE_INPUT: literal}
        case EdgeDef(name=name):
            return _Tagged(TAG_ANCHOR, name)
        case EdgeRef(name=name):
            return _Tagged(TAG_ALIAS, name)
        case RawCwlRef(expression=expression):
            return _Tagged(TAG_RAW_CWL, expression)
        case UnresolvedName(name=name):
            return name


def _desugared(value: InputValue) -> Any:
    """Spell an input value as a single-key mapping, as tooling emits it."""
    match value:
        case InlineLiteral(value=literal):
            return {KEY_INLINE_INPUT: literal}
        case EdgeDef(name=name):
            return {KEY_ANCHOR: name}
        case EdgeRef(name=name):
            return {KEY_ALIAS: name}
        case RawCwlRef(expression=expression):
            return {KEY_RAW_CWL: expression}
        case UnresolvedName(name=name):
            return name


# --------------------------------------------------------------------------
# The structural walk, shared by both spellings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Writer:
    """Turns an AST into plain data, spelling input values one chosen way."""

    spell: Spelling

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
                else [{'id': step.id, **self.step(step)} for step in document.steps]
            )

        for key, value in document.passthrough:
            body[key] = self.plain(value)

        return body

    def step(self, step: Step) -> dict[str, Any]:
        """One step's keys, minus its id."""
        out: dict[str, Any] = {}
        if step.inputs:
            out['in'] = {name: self.spell(value) for name, value in step.inputs}
        if step.outputs:
            out['out'] = [self.output(binding) for binding in step.outputs]
        for key, value in (*step.interpreted, *step.passthrough):
            out[key] = self.plain(value)
        return out

    def output(self, binding: OutputBinding) -> Any:
        """One `out:` entry: a bare name, or a name bound to an edge."""
        if binding.edge_def is None:
            return binding.name
        return {binding.name: self.spell(binding.edge_def)}

    def sidecar(self, sidecar: WicSidecar) -> Any:
        """A `wic:` block, restoring its `(index, name)` step keys."""
        out: dict[str, Any] = {key: self.plain(value) for key, value in sidecar.entries}
        if sidecar.steps:
            out['steps'] = {str(key): self.sidecar(child) for key, child in sidecar.steps}
        return out or None

    def plain(self, value: OpaqueCwl) -> Any:
        """Passthrough content, re-spelling any wic value nested inside it."""
        match value:
            case InlineLiteral() | EdgeDef() | EdgeRef() | RawCwlRef() | UnresolvedName():
                return self.spell(value)
            case dict():
                return {k: self.plain(v) for k, v in value.items()}
            case list():
                return [self.plain(v) for v in value]
            case _:
                return value


# --------------------------------------------------------------------------
# YAML emission
# --------------------------------------------------------------------------


def _represent_tagged(dumper: yaml.SafeDumper, data: _Tagged) -> yaml.nodes.Node:
    """Emit a `_Tagged` as `!tag value`."""
    style = '' if _PLAIN_SAFE.match(data.value) else None
    return dumper.represent_scalar(data.tag, data.value, style=style)


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
        if isinstance(event, yaml.events.ScalarEvent) and event.tag in _WIC_TAGS and event.style == '':
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
    body = _Writer(_tagged).document(document)
    if not body:
        return ''
    return yaml.dump(body, Dumper=_WicDumper, sort_keys=False, default_flow_style=False, width=10_000)


def to_json(document: Document) -> dict[str, Any]:
    """Project a document into plain JSON-shaped data, desugared.

    This is what a consumer without YAML tags sees, and what the exported JSON
    Schema describes.
    """
    return _Writer(_desugared).document(document)


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------


def _is_scalar(value: Any) -> bool:
    """Whether a value can be written as a tagged scalar."""
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_text(value: Any) -> str:
    """Render a scalar as the text an `!ii` tag should carry.

    A custom tag suppresses YAML's type resolution on the way back in, so the
    text has to be one that `yaml.safe_load` maps to this same value.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return 'null'
    return str(value)
