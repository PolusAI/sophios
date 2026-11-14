"""The versioned, conservative boundary for user-authored references."""

import copy
from typing import Any, Final

import pytest
from cwltool.checker import check_types
from hypothesis import given
from hypothesis import strategies as st

from sophios.api.python.workflow import _python_api_types_match
from sophios.lang.compatibility import TypeRelation, reference_relation
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import StepId, Tool, Tools, Yaml

from .synthetic_tools import clt

from .budgets import budget
from .hermetic import ORACLE, compile_hermetic
from .reference_model import ReferenceExpectation, reference_expectation
from .strategies import cwl_types, pinned

_CASES: Final = (
    ('same atom', 'string', 'string', ReferenceExpectation.OVERLAPS),
    ('different atoms', 'string', 'File', ReferenceExpectation.DISJOINT),
    ('nullable shorthand', 'string', 'string?', ReferenceExpectation.OVERLAPS),
    ('union overlap', 'string', ['null', 'string'], ReferenceExpectation.OVERLAPS),
    ('union disjoint', ['int', 'boolean'], 'string', ReferenceExpectation.DISJOINT),
    # One disjoint member and one unknown member is not a disjoint union: the
    # narrowest true answer is unknown, and `any`-instead-of-`all` here is the
    # cheapest way to turn every optional `Any` into a false rejection.
    ('union of disjoint and unknown', ['null', 'Any'], 'File', ReferenceExpectation.UNKNOWN),
    ('array shorthand', 'File[]', {'type': 'array', 'items': 'File'},
     ReferenceExpectation.OVERLAPS),
    ('nested array shorthand', {'type': 'array', 'items': 'File[]'},
     {'type': 'array', 'items': {'type': 'array', 'items': 'File'}},
     ReferenceExpectation.OVERLAPS),
    ('array item overlap', {'type': 'array', 'items': ['null', 'File']},
     {'type': 'array', 'items': 'File'}, ReferenceExpectation.OVERLAPS),
    ('array item disjoint', {'type': 'array', 'items': 'string'},
     {'type': 'array', 'items': 'File'}, ReferenceExpectation.DISJOINT),
    ('scalar and array', 'string', 'string[]', ReferenceExpectation.DISJOINT),
    ('Any', 'Any', 'File[]', ReferenceExpectation.UNKNOWN),
    ('nested Any', {'type': 'array', 'items': 'Any'},
     {'type': 'array', 'items': 'string'}, ReferenceExpectation.UNKNOWN),
    ('record', {'type': 'record', 'fields': []}, 'string', ReferenceExpectation.UNKNOWN),
    ('enum', {'type': 'enum', 'symbols': ['a']}, 'string', ReferenceExpectation.UNKNOWN),
    ('named schema', 'example.org#Thing', 'string', ReferenceExpectation.UNKNOWN),
    ('missing cross-scope type', None, 'string', ReferenceExpectation.UNKNOWN),
    ('malformed array', {'type': 'array'}, 'string[]', ReferenceExpectation.UNKNOWN),
    # Against a scalar the kind alone would settle it, which is exactly the
    # temptation: the declaration is not valid CWL, so it is CWL validation's
    # to report and not ours to rename as a disjoint reference.
    ('malformed array against a scalar', {'type': 'array'}, 'string',
     ReferenceExpectation.UNKNOWN),
)


_ATOMS: Final = ('null', 'boolean', 'int', 'long', 'float', 'double', 'string', 'File', 'Directory', 'Any')
#: Declarations as a tool may write them: atoms, `Any`, unions and arrays, the `?` and `[]` shorthands, and what the
#: judge must survive without knowing: a record, an enum, a named schema, a malformed array, nothing.
_DECLARATIONS: Final = cwl_types(
    *_ATOMS, 'example.org#Thing', None, {'type': 'array'}, {'type': 'record', 'fields': []},
    {'type': 'enum', 'symbols': ['a']})


def _loaded(declared: Any) -> bool:
    """Whether `declared` is spelled as CWL holds it once loaded, the one form cwltool's checker reads."""
    if isinstance(declared, list):
        return all(map(_loaded, declared))
    if isinstance(declared, dict):
        return set(declared) == {'type', 'items'} and declared['type'] == 'array' and _loaded(declared['items'])
    return declared in _ATOMS


@pytest.mark.fast
@given(st.tuples(_DECLARATIONS, _DECLARATIONS, st.none()))
@pinned(*[(source, sink, expected) for _, source, sink, expected in _CASES],
        # cwltool's own verdicts, where Sophios claims to know
        ('string', 'string', ReferenceExpectation.OVERLAPS), ('string', 'File', ReferenceExpectation.DISJOINT),
        ({'type': 'array', 'items': 'string'}, {'type': 'array', 'items': 'string'}, ReferenceExpectation.OVERLAPS),
        ({'type': 'array', 'items': 'string'}, 'File[]', ReferenceExpectation.DISJOINT))
@budget(200)
def test_the_versioned_judge_matches_the_independent_model(
        case: tuple[Any, Any, ReferenceExpectation | None]) -> None:
    """Known incompatibility and uncertainty remain three distinct outcomes, as the independent model judges them,
    and as each pinned case says. Observing a passthrough declaration does not mutate it.

    cwltool checks the boundary from outside; production never calls it. Where it reads the declarations, it refuses
    what Sophios calls disjoint and takes what Sophios calls overlapping, but for values that meet only in null,
    which cwltool refuses and Sophios, which refuses only what is provably disjoint, lets through."""
    source, sink, expected = case
    before = copy.deepcopy((source, sink))
    found = reference_relation(source, sink)
    assert (source, sink) == before, 'observing a passthrough declaration mutated it'
    assert found.name == reference_expectation(source, sink).name == (expected or found).name
    if found is not TypeRelation.UNKNOWN and _loaded(source) and _loaded(sink):
        refused = check_types(copy.deepcopy(source), copy.deepcopy(sink), None, None, None) == 'exception'
        assert refused is (found is TypeRelation.DISJOINT) or refused and 'null' in repr((source, sink)), found
    assert reference_relation(_RECURSIVE, _STR_ARR) is TypeRelation.UNKNOWN


#: A cyclic Python value, which the total raw judge must call unknown rather than recurse on forever.
_RECURSIVE: Final[dict[str, Any]] = {'type': 'array'}
_RECURSIVE['items'] = _RECURSIVE


#: What a Python API port or a value bound to one may declare: the declarations above but nothing, and `stdout`.
_API_TYPES: Final = cwl_types(
    *_ATOMS, 'stdout', 'example.org#Thing', {'type': 'array'}, {'type': 'record', 'fields': []},
    {'type': 'enum', 'symbols': ['a']})


@pytest.mark.fast
@given(st.tuples(_API_TYPES, _API_TYPES, st.none()))
@pinned(
    # scatter_on() lifts the sink after the binding, so an array source against a scalar sink is not provably
    # disjoint at assignment time
    ('string', {'type': 'array', 'items': 'Any'}, True), ('string', {'type': 'array', 'items': 'string'}, True),
    ('string[]', 'Any', True), ({'type': 'array', 'items': 'string'}, {'type': 'array', 'items': 'Any'}, True),
    ('string', {'type': 'record', 'fields': []}, True), ('string', 'string', True),
    # no scatter arrangement rescues these
    ('File', 'string', False), ('File', {'type': 'array', 'items': 'string'}, False), ('string', 'File', False))
@budget(200)
def test_the_eager_api_check_defers_what_scatter_could_rescue(case: tuple[Any, Any, bool | None]) -> None:
    """The eager check has less information than the compiler and must say so.

    A `Workflow` has no resolved language version *and* no scatter yet: binding happens before `scatter_on()`, which
    lifts the sink to an array. The check refuses a value only when the independent model proves it disjoint from the
    sink as declared and from the sink lifted one level, and each pinned case is accepted or refused as it states."""
    sink, source, accepted = case
    found = _python_api_types_match(sink, source)
    assert found is not all(reference_expectation(source, declared) is ReferenceExpectation.DISJOINT
                            for declared in (sink, {'type': 'array', 'items': sink}))
    assert accepted is None or found is accepted


#: Each sink a reference may reach, as `typed_sink` declares it: atoms, a union, and arrays one and two deep.
_STR_ARR: Final[Yaml] = {'type': 'array', 'items': 'string'}
_STR_ARR2: Final[Yaml] = {'type': 'array', 'items': _STR_ARR}
_SINKS: Final = ('string', 'File', 'int', 'float', ['int', 'string'], _STR_ARR, _STR_ARR2)
_SOURCES: Final = ('string', 'File', 'int', 'float', 'Any', 'string[]', {'type': 'array', 'items': 'Any'}, _STR_ARR2)
#: How `typed_source` scatters: the inputs it scatters, and the method.
_SCATTERS: Final = (((), ''), (('seed',), 'dotproduct'), (('seed', 'seed2'), 'nested_crossproduct'),
                    (('seed', 'seed2'), 'flat_crossproduct'))


def _wrap(cwl_type: Any, layers: int) -> Any:
    for _ in range(layers):
        cwl_type = {'type': 'array', 'items': cwl_type}
    return cwl_type


def _typed_tools(source_type: Any, sink_type: Any) -> Tools:
    """`typed_source`, whose output is `source_type`, and `typed_sink`, whose input is `sink_type`."""
    return {
        StepId('typed_source', 'global'): Tool('/synthetic/typed_source.cwl', clt(
            {'seed': {'type': 'string'}, 'seed2': {'type': 'string', 'default': 'y'}},
            {'value': {'type': source_type}}, canonical=True)),
        StepId('typed_sink', 'global'): Tool('/synthetic/typed_sink.cwl', clt(
            {'value': {'type': sink_type}}, {}, canonical=True)),
    }


def _compiles(document: Yaml, tools: Tools) -> bool:
    """Whether `document` compiles; a refusal must be wic023 alone."""
    try:
        compile_hermetic(document, 'reference', tools=tools)
    except SophiosError as error:
        assert [d.code for d in error.diagnostics] == [SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE]
        return False
    return True


@pytest.mark.slow
@given(st.tuples(st.sampled_from(_SOURCES), st.sampled_from(_SINKS), st.sampled_from(_SCATTERS), st.integers(0, 2)))
@pinned(('string', 'string', _SCATTERS[0], 0), ('string', 'File', _SCATTERS[0], 0), ('Any', 'string', _SCATTERS[0], 1),
        ({'type': 'array', 'items': 'Any'}, 'string', _SCATTERS[0], 1), ('string', 'string', _SCATTERS[1], 1),
        ('File', 'string', _SCATTERS[1], 1), ('string', _STR_ARR2, _SCATTERS[2], 0),
        ('string', _STR_ARR, _SCATTERS[3], 0), ('string', _STR_ARR2, _SCATTERS[3], 0),
        (_STR_ARR2, 'string', _SCATTERS[0], 2))
@ORACLE
def test_a_reference_compiles_unless_the_model_proves_it_disjoint(case: tuple[Any, Any, Any, int]) -> None:
    """A workflow input of `source_type`, and `typed_source`'s output of that type by an edge, each feed
    `typed_sink`'s input, scattered `layers` times: each compiles exactly when the independent model does not prove
    the two disjoint, at the ranks CWL gives them. A scatter lifts the source one layer, or one per scattered input
    under nested_crossproduct, and the sink once per time `scatter:` names it, before the shared judgment; unknown
    and overlapping references compile, and only a disjoint one is wic023."""
    source_type, sink_type, (keys, method), layers = case
    sink: Yaml = {'id': 'typed_sink', 'in': {'value': 'wf_source'},
                  **({'scatter': ['value'] * layers} if layers else {})}
    lifted = _wrap(sink_type, layers)
    tools = _typed_tools(source_type, sink_type)
    assert _compiles({'inputs': {'wf_source': {'type': source_type}}, 'steps': [sink]}, tools) is (
        reference_expectation(source_type, lifted) is not ReferenceExpectation.DISJOINT)
    source: Yaml = {'id': 'typed_source', 'in': {'seed': {'wic_inline_input': ['x'] if keys else 'x'},
                                                 **({'seed2': {'wic_inline_input': ['y']}} if 'seed2' in keys else {})},
                    'out': [{'value': {'wic_anchor': 'edge'}}],
                    **({'scatter': list(keys), 'scatterMethod': method} if keys else {})}
    produced = _wrap(source_type, (len(keys) if method == 'nested_crossproduct' else 1) if keys else 0)
    assert _compiles({'steps': [source, {**sink, 'in': {'value': {'wic_alias': 'edge'}}}]}, tools) is (
        reference_expectation(produced, lifted) is not ReferenceExpectation.DISJOINT)
