"""Export a JSON Schema for Sophios, generated from the AST.

The AST is the source of truth: every key comes from a field's `Surface`
declaration in `nodes.py` and every construct from the parser's dispatch
tables, so nothing here can disagree with the parser about the shape of a
document. The schema describes the desugared projection (§6.1), since JSON
has no YAML tags, and is a deliberate over-approximation — an editor aid, not
a second implementation of the language.
"""
from collections.abc import Callable, Mapping
from dataclasses import fields
from types import MappingProxyType
from typing import Any, Final, final

from .nodes import Document, OutputBinding, Shape, Step, WicSidecar, surface_of
from .parser import SIDECAR_WRAPPER_KEY, Forms, Grammar
from .values import ValueShape


@final
class Json:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """What this module needs to speak JSON Schema, and nothing more."""

    #: Draft this schema targets. 2020-12 is what current editors consume.
    DIALECT: Final = 'https://json-schema.org/draft/2020-12/schema'

    #: Stable identifier, so an editor can bind it to `*.wic` by URI. A URN,
    #: deliberately, since nothing publishes a URL for this schema yet.
    SCHEMA_ID: Final = 'urn:sophios:schema:lang'


#: How each declared shape is expressed in JSON Schema — as builders, not as
#: fragments, so each call returns a structure nothing else holds a
#: reference to.
_SHAPE_SCHEMA: Final[Mapping[Shape, Callable[[], dict[str, Any]]]] = MappingProxyType({
    Shape.INPUT_BINDINGS: lambda: {
        'description': 'Input bindings. Each input may be bound only once (§4.2).',
        'type': 'object',
        'additionalProperties': {'$ref': '#/$defs/inputValue'},
    },
    Shape.OUTPUT_BINDINGS: lambda: {
        'type': 'array',
        'items': {'$ref': '#/$defs/outputEntry'},
    },
    Shape.STEPS: lambda: {'$ref': '#/$defs/steps'},
    Shape.SIDECAR: lambda: {'$ref': '#/$defs/wicBlock'},
    Shape.SIDECAR_STEPS: lambda: {
        'description': 'Per-step metadata, keyed "(index, name)" or by a unique step id.',
        'type': 'object',
        'patternProperties': {Grammar.WIC_STEP_KEY_PATTERN: {'$ref': '#/$defs/wicStepEntry'},
                              Grammar.WIC_STEP_ID_PATTERN: {'$ref': '#/$defs/wicStepEntry'}},
        'additionalProperties': False,
    },
    #: Structure only; a field's own constraints (e.g. non-empty step id)
    #: live with that field, not here, since IDENTITY is shared by fields
    #: with different constraints.
    Shape.IDENTITY: lambda: {'type': 'string'},
})


def wic_schema() -> dict[str, Any]:
    """Build the JSON Schema describing a desugared Sophios document.

    Returned fresh each call, with nothing aliased inside, so the caller may
    annotate or edit the result in place.
    """
    document = _object_schema(Document)
    document['properties']['cwlVersion'] = {
        **Grammar.CWL_VERSION_VALUE.json(),
        'description': 'Ignored in favour of the substrate version, but must be one the toolchain runs (§1).',
    }
    return {
        '$schema': Json.DIALECT,
        '$id': Json.SCHEMA_ID,
        'title': 'Sophios workflow',
        'description': 'Desugared projection of a Sophios document. '
                       'See docs/sophios_language_reference.md.',
        **document,
        '$defs': _defs(),
    }


def _object_schema(node_type: type, *, omit: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Turn one AST node into a JSON Schema object, field by field.

    Walks `dataclasses.fields` rather than a hand-written list, so a field
    without a `Surface` declaration raises rather than being silently omitted.
    """
    properties: dict[str, Any] = {}
    open_object = False

    for declared in fields(node_type):
        form = surface_of(node_type, declared.name)
        if declared.name in omit or form.shape is Shape.INTERNAL:
            continue
        match form.shape:
            case Shape.PASSTHROUGH:
                # Not a key: the licence for every key nobody else claimed.
                open_object = True
            case Shape.INTERPRETED:
                # A closed set of CWL keys, listed for editor completion but
                # left unconstrained — Sophios reads them, CWL owns their shapes.
                for key in sorted(Grammar.INTERPRETED_STEP_KEYS):
                    properties[key] = {'description': f'Interpreted by Sophios: {key} (§4.3).'}
            case Shape.SIDECAR_ENTRIES:
                # Closed, and each value's shape is the one the parser checks.
                properties.update(_values_schema(Grammar.SIDECAR_VALUES))
            case _ if form.key is not None:
                properties[form.key] = _SHAPE_SCHEMA[form.shape]()
            case _:
                # IDENTITY without a key is carried positionally, not as a key.
                continue

    return {'type': 'object', 'properties': properties, 'additionalProperties': open_object}


def _defs() -> dict[str, Any]:
    """The reusable shapes, each generated from the node it describes."""
    step_body = _step_body()
    return {
        'steps': {
            'description': 'A mapping keyed by step name, or a sequence of steps.',
            'oneOf': [
                {'type': 'object', 'additionalProperties': {'$ref': '#/$defs/stepBody'}},
                {'type': 'array', 'items': {'$ref': '#/$defs/sequenceStep'}},
            ],
        },
        'stepBody': step_body,
        'sequenceStep': _sequence_step(),
        'inputValue': _input_value(),
        'construct': _construct(),
        'outputEntry': _output_entry(),
        'wicBlock': _wic_block(),
        'wicStepBlock': _wic_step_block(),
        'wicStepEntry': _wic_step_entry(),
    }


def _step_body() -> dict[str, Any]:
    """A step keyed by name. Null is legal: a step may have no body (§3.1)."""
    # `id` is omitted: in this form the step's name is the mapping key.
    body = _object_schema(Step, omit=frozenset({'id'}))
    return {**body, 'type': ['object', 'null']}


def _sequence_step() -> dict[str, Any]:
    """A step written in a sequence, which carries its own non-empty `id:` (wic007)."""
    body = _object_schema(Step)
    identity = body['properties']['id']
    return {
        **body,
        'properties': {**body['properties'], 'id': {**identity, 'minLength': 1}},
        'required': ['id'],
        'description': 'A step written in a sequence, carrying its name in an id: key.',
    }


def _output_entry() -> dict[str, Any]:
    """One `out:` entry: a bare name, or a name bound to an edge."""
    identity = _SHAPE_SCHEMA[surface_of(OutputBinding, 'name').shape]
    return {
        'description': 'A bare output name, or a name bound to an edge definition.',
        'oneOf': [
            identity(),
            {'type': 'object', 'minProperties': 1, 'maxProperties': 1},
        ],
    }


def _values_schema(values: Mapping[str, ValueShape]) -> dict[str, Any]:
    """One property per declared `wic:` key, shaped as the parser checks it."""
    return {key: shape.json() for key, shape in sorted(values.items())}


def _wic_block() -> dict[str, Any]:
    """The `wic:` sidecar. Null is legal: a bare `wic:` is empty (§5)."""
    body = _object_schema(WicSidecar)
    return {
        **body,
        'description': 'Compiler metadata. Never emitted to CWL (§5).',
        'type': ['object', 'null'],
    }


def _wic_step_block() -> dict[str, Any]:
    """What a `wic: steps:` entry says about its step: a `wic:` block's
    keys, plus the ones only a step entry has."""
    body = _wic_block()
    return {
        **body,
        'description': 'Metadata for the step this entry names (§5).',
        'properties': {'steps': body['properties']['steps'],
                       **_values_schema(Grammar.SIDECAR_STEP_VALUES)},
    }


def _wic_step_entry() -> dict[str, Any]:
    """A `wic: steps:` entry: its keys directly, under a `wic:` wrapper, or both."""
    body = _wic_step_block()
    return {
        **body,
        'properties': {**body['properties'],
                       SIDECAR_WRAPPER_KEY: {'$ref': '#/$defs/wicStepBlock'}},
    }


def _input_value() -> dict[str, Any]:
    """One of the five input forms (§4.1).

    Unconstrained on purpose; `construct` is referenced only so editors can
    offer construct keys as completions.
    """
    return {
        'description': 'An inline literal, edge reference, raw CWL reference, '
                       'unresolved name, or step-input record (§4.1).',
        'anyOf': [{'$ref': '#/$defs/construct'}, {}],
    }


def _construct() -> dict[str, Any]:
    """A desugared Sophios construct valid in input position: a single-key mapping.

    Derived from the parser's dispatch table, so a construct added there
    appears here automatically.
    """
    return {
        'type': 'object',
        'minProperties': 1,
        'maxProperties': 1,
        'properties': {key: {} for key in sorted(Forms.DESUGARED_KEYS)},
        'additionalProperties': False,
    }
