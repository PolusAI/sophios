"""Properties of the exported JSON Schema (CR-101, T1.3).

Properties covered:
  P09  the exported schema accepts what the AST accepts, and rejects the
       structural mistakes the parser reports

WHAT P09 CAN AND CANNOT CLAIM. The schema is a deliberate over-approximation,
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
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings

from sophios.lang import DESUGARED_KEYS, INTERPRETED_STEP_KEYS, parse, to_json, wic_schema
from sophios.lang.parser import WIC_STEP_KEY_PATTERN

from .test_lang_parser import documents
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
    assert set(construct['properties']) == set(DESUGARED_KEYS)

    step_keys = set(SCHEMA['$defs']['stepBody']['properties'])
    assert INTERPRETED_STEP_KEYS <= step_keys

    steps = SCHEMA['$defs']['wicBlock']['properties']['steps']
    assert set(steps['patternProperties']) == {WIC_STEP_KEY_PATTERN}


@pytest.mark.fast
def test_schema_is_not_shared_between_callers() -> None:
    """Each export is a fresh object, so one caller's edits cannot leak."""
    first, second = wic_schema(), wic_schema()
    assert first == second
    first['$defs']['construct']['properties'].clear()
    assert wic_schema()['$defs']['construct']['properties']


# --------------------------------------------------------------------------
# P09, forward: everything the parser accepts, the schema accepts
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(documents())
@FAST
def test_p09_accepts_every_document_the_parser_accepts(source: str) -> None:
    """P09: a parsed document's JSON projection validates."""
    result = parse(source, 'gen.wic')
    assert result.document is not None
    assert _accepts(to_json(result.document)), list(VALIDATOR.iter_errors(to_json(result.document)))


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_p09_accepts_the_whole_corpus(path: Path) -> None:
    """P09: every real `.wic` in the repository validates against the schema."""
    result = parse(path.read_text(encoding='utf-8'), str(path))
    assert result.document is not None
    data = to_json(result.document)
    assert _accepts(data), list(VALIDATOR.iter_errors(data))


# --------------------------------------------------------------------------
# P09, reverse: the structural mistakes the parser reports, the schema rejects
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
def test_p09_rejects_what_the_parser_reports(label: str, source: str, projection: Any) -> None:
    """P09 reverse: parser and schema agree on the structural mistakes."""
    assert not parse(source, 'bad.wic').ok, f'parser accepted {label!r}'
    assert not _accepts(projection), f'schema accepted {label!r}'


# --------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize('key', sorted(DESUGARED_KEYS))
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
