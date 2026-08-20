"""Export a JSON Schema for `.wic`, derived from the language definition.

The schema is generated from the same constants the parser dispatches on — the
desugared construct keys, the interpreted step keys, the `wic:` step-key
pattern — so it cannot drift from the parser by being edited separately. There
is no hand-maintained schema document anywhere in the tree.

**What the schema can and cannot say.** Two limits are structural, not
oversights, and both follow from the language's own design:

*JSON has no YAML tags.* A validator sees a document after loading, so `!ii x`
is invisible to it. The schema therefore describes the **desugared** projection
(§6.1) — the shape `to_json` produces and the shape a YAML language server sees
once wic tags are resolved.

*Passthrough is open by definition.* The reference (§1) says anything outside
the interpreted set is copied through untouched, so the schema cannot close any
object that may carry passthrough CWL. It constrains the shapes the parser
itself diagnoses — what `steps`, `in`, `out`, and `wic` must *be* — and admits
anything else.

The result is a deliberate over-approximation: everything the parser accepts
validates, and the structural mistakes the parser reports are rejected. It is
an editor aid, not a second implementation of the language.

See docs/wic_language_reference.md.
"""
from typing import Any, Final

from .parser import DESUGARED_KEYS, INTERPRETED_STEP_KEYS, WIC_STEP_KEY_PATTERN

#: Draft this schema targets. 2020-12 is what current editors consume.
DIALECT: Final = 'https://json-schema.org/draft/2020-12/schema'

#: Stable identifier, so an editor can bind it to `*.wic` by URI.
SCHEMA_ID: Final = 'https://raw.githubusercontent.com/PolusAI/sophios/master/wic.schema.json'


def wic_schema() -> dict[str, Any]:
    """Build the JSON Schema describing a desugared `.wic` document.

    Returned fresh each call: the result is mutable, and a shared instance a
    caller edited would corrupt every later export.
    """
    return {
        '$schema': DIALECT,
        '$id': SCHEMA_ID,
        'title': 'Sophios .wic workflow',
        'description': 'Desugared projection of a .wic document. See docs/wic_language_reference.md.',
        'type': 'object',
        'properties': {
            'wic': _wic_block(),
            'steps': {'$ref': '#/$defs/steps'},
        },
        # Open: every unlisted key is passthrough CWL by definition (§1).
        'additionalProperties': True,
        '$defs': _defs(),
    }


def _defs() -> dict[str, Any]:
    """The reusable shapes, one per construct the parser diagnoses."""
    return {
        'steps': {
            'description': 'A mapping keyed by step name, or a sequence of steps.',
            'oneOf': [
                {'type': 'object', 'additionalProperties': {'$ref': '#/$defs/stepBody'}},
                {'type': 'array', 'items': {'$ref': '#/$defs/sequenceStep'}},
            ],
        },
        'stepBody': _step_body(),
        'sequenceStep': {
            'description': "A step written in a sequence: either it carries id:, "
                           "or it is a single-key mapping naming the step.",
            'type': 'object',
            'properties': {'id': {'type': 'string', 'minLength': 1}},
            'additionalProperties': True,
        },
        'inputValue': _input_value(),
        'construct': _construct(),
        'outputEntry': {
            'description': 'A bare output name, or a name bound to an edge definition.',
            'oneOf': [
                {'type': 'string'},
                {'type': 'object', 'minProperties': 1, 'maxProperties': 1},
            ],
        },
        'wicBlock': _wic_block(),
    }


def _step_body() -> dict[str, Any]:
    """A step's keys. Null is legal: a step may have no body at all (§3.1)."""
    return {
        'type': ['object', 'null'],
        'properties': {
            'in': {
                'description': 'Input bindings. Each input may be bound only once (§4.2).',
                'type': 'object',
                'additionalProperties': {'$ref': '#/$defs/inputValue'},
            },
            'out': {
                'type': 'array',
                'items': {'$ref': '#/$defs/outputEntry'},
            },
            # Listed for editor completion, not constrained: Sophios reads
            # these but CWL owns their shapes.
            **{key: {'description': f'Interpreted by Sophios: {key} (§4.3).'}
               for key in sorted(INTERPRETED_STEP_KEYS)},
        },
        'additionalProperties': True,
    }


def _input_value() -> dict[str, Any]:
    """One of the five input forms (§4.1).

    Unconstrained on purpose. A mapping whose single key is a construct key is
    a construct; any other value is an inline literal or an unresolved name,
    and the parser accepts all of them. `construct` is referenced so editors
    can offer the four keys as completions.
    """
    return {
        'description': 'An inline literal, edge definition, edge reference, '
                       'raw CWL reference, or unresolved name (§4.1).',
        'anyOf': [{'$ref': '#/$defs/construct'}, {}],
    }


def _construct() -> dict[str, Any]:
    """A desugared wic construct: a single-key mapping.

    Derived from the parser's dispatch table, so a construct added there
    appears here without anyone remembering to update a schema.
    """
    return {
        'type': 'object',
        'minProperties': 1,
        'maxProperties': 1,
        'properties': {key: {} for key in sorted(DESUGARED_KEYS)},
        'additionalProperties': False,
    }


def _wic_block() -> dict[str, Any]:
    """The `wic:` sidecar. Null is legal: a bare `wic:` is an empty block (§5)."""
    return {
        'description': 'Compiler metadata. Never emitted to CWL (§5).',
        'type': ['object', 'null'],
        'properties': {
            'steps': {
                'description': 'Per-step metadata, keyed "(index, name)".',
                'type': 'object',
                'patternProperties': {WIC_STEP_KEY_PATTERN: {'$ref': '#/$defs/wicBlock'}},
                'additionalProperties': False,
            },
        },
        'additionalProperties': True,
    }
