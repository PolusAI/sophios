"""Properties of the exported JSON Schema.

The claim under test: the exported schema accepts what the AST accepts, and
rejects the structural mistakes the parser reports.

WHAT THE CLAIM CAN AND CANNOT SAY. The schema is a deliberate over-approximation,
for two reasons that come from the language rather than from this test:

  * JSON has no YAML tags, so the schema describes the desugared projection.
  * Passthrough CWL is open by definition (reference §1), so the schema cannot
    close any object that might carry it.

So "accepts exactly what the AST accepts" is checked as two separate claims:
agreement is exact on the structural shapes the parser itself diagnoses, and
one-directional (schema accepts everything the parser does) elsewhere. Stating
this here rather than asserting a stronger equivalence that is not true.

See design_docs/core-refactor-design.md, Spec 1.
"""
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings

from sophios.lang import Code, Forms, Grammar, parse, to_json, wic_schema
from sophios.lang.nodes import (
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    OutputBinding,
    RawCwlRef,
    Shape,
    Step,
    StepKey,
    UnresolvedName,
    WicSidecar,
    surface,
    surface_of,
)

from .strategies import documents
from .wic_corpus import CORPUS, corpus_id

FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)

SCHEMA = wic_schema()
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def _accepts(data: Any) -> bool:
    """Whether the exported schema accepts this document."""
    return VALIDATOR.is_valid(data)


# --------------------------------------------------------------------------
# The schema is well-formed and derived, not hand-written
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_schema_is_itself_valid() -> None:
    """The exported schema is a legal JSON Schema of the dialect it claims."""
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.fast
def test_schema_is_derived_from_the_parser() -> None:
    """Every construct and interpreted key comes from the parser's own tables.

    This is the acceptance criterion "derived from the AST, never
    hand-maintained": adding a construct to the parser must move the schema
    without anyone remembering to edit it.
    """
    construct = SCHEMA['$defs']['construct']
    assert set(construct['properties']) == set(Forms.DESUGARED_KEYS)

    step_keys = set(SCHEMA['$defs']['stepBody']['properties'])
    assert Grammar.INTERPRETED_STEP_KEYS <= step_keys

    steps = SCHEMA['$defs']['wicBlock']['properties']['steps']
    assert set(steps['patternProperties']) == {Grammar.WIC_STEP_KEY_PATTERN}


@pytest.mark.fast
def test_schema_is_not_shared_between_callers() -> None:
    """Each export is a fresh object, so one caller's edits cannot leak."""
    first, second = wic_schema(), wic_schema()
    assert first == second
    first['$defs']['construct']['properties'].clear()
    assert wic_schema()['$defs']['construct']['properties']


# --------------------------------------------------------------------------
# Forward: everything the parser accepts, the schema accepts
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(documents())
@FAST
def test_accepts_every_document_the_parser_accepts(source: str) -> None:
    """A parsed document's JSON projection validates — and IS JSON.

    Both halves matter, and only together are they the claim: `jsonschema`
    is duck-typed and will happily "validate" live Python objects that
    `json.dumps` refuses, which is precisely how a broken `to_json` stayed
    green under this property until review caught it.
    """
    result = parse(source, 'gen.wic')
    assert result.document is not None
    projection = to_json(result.document)
    json.dumps(projection)  # serialisability is the claim, not an assumption
    assert _accepts(projection), list(VALIDATOR.iter_errors(projection))


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_accepts_the_whole_corpus(path: Path) -> None:
    """Every real `.wic` in the repository validates against the schema."""
    result = parse(path.read_text(encoding='utf-8'), str(path))
    assert result.document is not None
    data = to_json(result.document)
    assert _accepts(data), list(VALIDATOR.iter_errors(data))


# --------------------------------------------------------------------------
# Reverse: the structural mistakes the parser reports, the schema rejects
# --------------------------------------------------------------------------
#
# Each case is a shape the parser emits a diagnostic for. Both halves are
# asserted together, so the pair cannot silently drift apart.

STRUCTURAL_VIOLATIONS: list[tuple[str, str, Any]] = [
    ('steps must be a mapping or sequence', 'steps: nope\n', {'steps': 'nope'}),
    ('step body must be a mapping or null', 'steps:\n  s: 3\n', {'steps': {'s': 3}}),
    ('in must be a mapping', 'steps:\n  s:\n    in: 3\n', {'steps': {'s': {'in': 3}}}),
    ('out must be a sequence', 'steps:\n  s:\n    out: 3\n', {'steps': {'s': {'out': 3}}}),
    ('wic must be a mapping', 'wic: 3\n', {'wic': 3}),
    ('wic steps must be a mapping', 'wic:\n  steps: 3\n', {'wic': {'steps': 3}}),
    ('wic step keys have the form (index, name)',
     'wic:\n  steps:\n    nope:\n      x: 1\n', {'wic': {'steps': {'nope': {'x': 1}}}}),
]


@pytest.mark.fast
@pytest.mark.parametrize('label,source,projection', STRUCTURAL_VIOLATIONS, ids=[c[0] for c in STRUCTURAL_VIOLATIONS])
def test_rejects_what_the_parser_reports(label: str, source: str, projection: Any) -> None:
    """Reverse direction: parser and schema agree on the structural mistakes."""
    assert not parse(source, 'bad.wic').ok, f'parser accepted {label!r}'
    assert not _accepts(projection), f'schema accepted {label!r}'


# --------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize('key', sorted(Forms.DESUGARED_KEYS))
def test_each_construct_validates(key: str) -> None:
    """Every desugared construct is accepted where an input value is expected."""
    assert _accepts({'steps': {'s': {'in': {'f': {key: 'x'}}}}})


@pytest.mark.fast
def test_passthrough_is_admitted_everywhere() -> None:
    """Unknown keys are passthrough CWL, not errors (§1)."""
    assert _accepts({
        '$namespaces': {'edam': 'http://edamontology.org/'},
        'steps': {'s': {'in': {'f': 'name'}, 'hints': [{'class': 'X'}]}},
    })


@pytest.mark.fast
def test_a_step_may_have_no_body() -> None:
    """`some_subworkflow.wic:` with nothing under it is well-formed (§3.1)."""
    assert _accepts({'steps': {'sub.wic': None}})
    assert _accepts({'wic': None})

# --------------------------------------------------------------------------
# The AST is the source of truth
# --------------------------------------------------------------------------
#
# The schema is generated from the `Surface` declarations in `nodes.py`. These
# check that the generation is total — that no field can be added, and no key
# invented, without one of these failing.


AST_NODES = [Document, Step, OutputBinding, WicSidecar, StepKey,
             InlineLiteral, EdgeDef, EdgeRef, RawCwlRef, UnresolvedName]


@pytest.mark.fast
@pytest.mark.parametrize('node_type', AST_NODES, ids=lambda n: n.__name__)
def test_every_ast_field_declares_its_surface(node_type: type) -> None:
    """Every field says how it is written, or `surface_of` raises.

    A field with no declaration is a hole in the specification: the AST would
    carry something the schema cannot describe and the reference does not
    mention.
    """
    for declared in fields(node_type):
        assert surface_of(node_type, declared.name) is not None


@pytest.mark.fast
def test_an_undeclared_field_is_rejected_loudly() -> None:
    """Adding a field without declaring its surface fails, rather than
    silently producing a schema that describes less than the AST holds."""

    @dataclass(frozen=True)
    class Drifted:
        declared: str = surface(Shape.IDENTITY, 'declared')
        forgotten: str = 'oops'

    assert surface_of(Drifted, 'declared').key == 'declared'
    with pytest.raises(TypeError, match='no surface declaration'):
        surface_of(Drifted, 'forgotten')


@pytest.mark.fast
def test_schema_keys_come_only_from_the_ast() -> None:
    """Every key the schema describes traces back to a declared field.

    The schema is not allowed to invent syntax. `id` is expected on the
    sequence form and absent from the mapping form, where the step's name is
    the mapping key rather than a key inside it.
    """
    declared = {surface_of(Step, f.name).key for f in fields(Step)} - {None}
    declared |= set(Grammar.INTERPRETED_STEP_KEYS)

    for form in ('sequenceStep', 'stepBody'):
        keys = set(SCHEMA['$defs'][form]['properties'])
        assert keys <= declared, f'{form} invented {keys - declared}'

    assert 'id' in SCHEMA['$defs']['sequenceStep']['properties']
    assert 'id' not in SCHEMA['$defs']['stepBody']['properties']


@pytest.mark.fast
@pytest.mark.parametrize('node_type,form', [(Step, 'sequenceStep'), (WicSidecar, 'wicBlock')],
                         ids=['Step', 'WicSidecar'])
def test_every_keyed_field_reaches_the_schema(node_type: type, form: str) -> None:
    """A field that occupies a surface key is described by the schema."""
    properties = SCHEMA['$defs'][form]['properties']
    for declared in fields(node_type):
        shape = surface_of(node_type, declared.name)
        if shape.key is not None and shape.shape is not Shape.INTERNAL:
            assert shape.key in properties, f'{node_type.__name__}.{declared.name} is not in the schema'


@pytest.mark.fast
def test_passthrough_declarations_open_their_objects() -> None:
    """A node declaring PASSTHROUGH produces an open object, and one without
    it does not. Openness is the AST's statement, not the generator's."""
    assert any(surface_of(Step, f.name).shape is Shape.PASSTHROUGH for f in fields(Step))
    assert SCHEMA['$defs']['sequenceStep']['additionalProperties'] is True
    assert SCHEMA['$defs']['construct']['additionalProperties'] is False


# --------------------------------------------------------------------------
# An export is the caller's to keep, and to edit
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_editing_an_export_cannot_affect_a_later_one() -> None:
    """Freshness is deep, not one level down.

    The generator builds from shared fragments, and those fragments are
    nested; a shallow copy of one hands out its interior. Reaching three
    levels into an export and writing there must not change what the next
    call produces — that is the whole of what "returned fresh each call"
    claims, and it is checked at the depth where the claim used to fail.
    """
    first = wic_schema()
    first['$defs']['stepBody']['properties']['in']['additionalProperties']['$ref'] = 'CORRUPTED'
    first['$defs']['outputEntry']['oneOf'][0]['type'] = 'CORRUPTED'

    second = wic_schema()
    assert second['$defs']['stepBody']['properties']['in']['additionalProperties']['$ref'] \
        == '#/$defs/inputValue'
    assert second['$defs']['outputEntry']['oneOf'][0]['type'] == 'string'


@pytest.mark.fast
def test_no_two_places_in_one_export_share_a_fragment() -> None:
    """Within a single export, equal fragments are still separate objects.

    `stepBody.in` and `sequenceStep.in` describe the same shape, so they were
    the same live object down inside; editing one edited both. Identity, not
    equality, is what a caller relies on when annotating a schema in place.
    """
    schema = wic_schema()
    step_body_in = schema['$defs']['stepBody']['properties']['in']
    sequence_in = schema['$defs']['sequenceStep']['properties']['in']

    assert step_body_in == sequence_in
    assert step_body_in is not sequence_in
    assert step_body_in['additionalProperties'] is not sequence_in['additionalProperties']


# --------------------------------------------------------------------------
# A field's constraints are the field's, not its shape's
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_an_empty_output_name_validates_because_the_parser_accepts_it() -> None:
    """`out: ['']` parses without a diagnostic, so it must validate.

    Both `Step.id` and `OutputBinding.name` are IDENTITY-shaped. A non-empty
    constraint written onto the shape reached both and made the schema
    stricter than the language for outputs — the forward direction's exact
    failure mode, invisible while the generator's names were all non-empty.
    """
    result = parse("steps:\n- id: s\n  out:\n  - ''\n", 'empty_out.wic')
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None
    assert _accepts(json.loads(json.dumps(to_json(result.document))))


@pytest.mark.fast
def test_an_empty_step_id_is_rejected_because_the_parser_reports_it() -> None:
    """The other direction, for the field that does carry the rule: an empty
    step id is `wic007`, so the schema must not accept it either."""
    result = parse("steps:\n- id: ''\n", 'empty_id.wic')
    assert any(d.code is Code.EMPTY_STEP_ID for d in result.diagnostics)
    assert not _accepts({'steps': [{'id': ''}]})


# --------------------------------------------------------------------------
# The `wic:` vocabulary has one home
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_the_validator_admits_exactly_the_declared_sidecar_keys() -> None:
    """`Grammar.SIDECAR_KEYS` and the validating schema name the same keys.

    They are separate places by necessity — the language layer owns the
    vocabulary, the validator owns each key's shape — and nothing linked them,
    so `lang_version` reached the reference, the resolver and the compiler
    while the validator still rejected it before compilation ever started.
    """
    from sophios.schemas.wic_schema import _wic_tag_schema

    validated = set(_wic_tag_schema(hypothesis=False)['properties'])
    declared = set(Grammar.SIDECAR_KEYS)

    assert validated == declared, (
        f'declared but not validated: {sorted(declared - validated)}; '
        f'validated but not declared: {sorted(validated - declared)}'
    )
