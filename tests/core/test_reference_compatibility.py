"""The versioned, conservative boundary for user-authored references."""

import ast
import copy
from functools import partial
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from sophios.api.python.workflow import _python_api_types_match
from sophios.inlineing import get_inlineable_subworkflows
from sophios.lang.compatibility import TypeRelation, reference_relation
from sophios.lang.diagnostics import SophiosErrorCode, SophiosError
from sophios.lang.versions import KNOWN_VERSIONS
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_yaml import wic_loader
from sophios.wic_types import StepId, Tool, Tools, Yaml, YamlTree

from .synthetic_tools import clt

from .hermetic import ORACLE, compile_hermetic
from .reference_model import ReferenceExpectation, reference_expectation

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

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


@pytest.mark.fast
@pytest.mark.parametrize('why, source, sink, expected', _CASES, ids=[case[0] for case in _CASES])
def test_the_versioned_judge_matches_the_independent_model(
        why: str, source: Any, sink: Any, expected: ReferenceExpectation) -> None:
    """Known incompatibility and uncertainty remain three distinct outcomes."""
    before = copy.deepcopy((source, sink))
    found = reference_relation(source, sink, lang_version='0.0.1')
    assert found.name == expected.name, why
    assert (source, sink) == before, 'observing a passthrough declaration mutated it'
    assert reference_expectation(source, sink) is expected


@pytest.mark.fast
def test_a_recursive_or_malformed_declaration_is_unknown_not_a_crash() -> None:
    """A cyclic Python value cannot make the total raw judge recurse forever."""
    recursive: dict[str, Any] = {'type': 'array'}
    recursive['items'] = recursive
    assert reference_relation(
        recursive, {'type': 'array', 'items': 'string'}, lang_version='0.0.1') \
        is TypeRelation.UNKNOWN


@pytest.mark.fast
def test_every_language_version_owns_a_judgment() -> None:
    """Dispatch is exhaustive over versions and rejects an impossible caller value."""
    assert all(reference_relation('string', 'string', lang_version=version)
               is TypeRelation.OVERLAPS for version in KNOWN_VERSIONS)
    with pytest.raises(ValueError, match='No reference judgment'):
        reference_relation('string', 'string', lang_version='not-a-version')


def _subworkflow_tree(parentargs: Yaml, wic: Yaml | None = None) -> YamlTree:
    """A root workflow with one subworkflow call, as the AST reader leaves it."""
    tree: Yaml = {'steps': [{'id': 'child.wic',
                             'subtree': {'inputs': {'x': {'type': 'string'}},
                                         'steps': [{'id': 'sink', 'in': {'value': 'x'}}]},
                             'parentargs': parentargs}]}
    if wic is not None:
        tree['wic'] = wic
    return YamlTree(StepId('root', 'global'), tree)


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'parentargs', 'wic', 'offered'), [
    ('scatter at the call site', {'in': {'x': 'actual'}, 'scatter': ['sink___value']}, None, False),
    ('scatter in wic: steps: metadata', {'in': {'x': 'actual'}},
     {'steps': {'(1, child.wic)': {'scatter': ['sink___value']}}}, False),
    ('metadata without scatter', {}, {'steps': {'(1, child.wic)': {'in': {'x': 1}}}}, True),
    ('no scatter anywhere', {'in': {'x': 'actual'}}, None, True),
])
def test_a_scattered_subworkflow_is_not_offered_to_the_ast_inliner(
        claim: str, parentargs: Yaml, wic: Yaml | None, offered: bool) -> None:
    """Inlining cannot erase scatter and manufacture a disjoint reference.

    Scatter reaches a step from two places and the inliner runs before they are
    merged: `parentargs` is the call site as written under `steps:`, while
    `wic: steps:` metadata is merged onto it in `compile_workflow_once`, which
    is later. Reading only the first offers a subworkflow whose scatter is
    spelled in the metadata, and inlining it erases that scatter.
    """
    result = get_inlineable_subworkflows(
        _subworkflow_tree(parentargs, wic), implementation=False, namespaces_init=[])
    assert bool(result) is offered, claim


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'sink', 'source', 'accepted'), [
    # scatter_on() lifts the sink after the binding, so an array source against a
    # scalar sink is not provably disjoint at assignment time.
    ('Any[] source, scalar sink', 'string', {'type': 'array', 'items': 'Any'}, True),
    ('typed array source, scalar sink', 'string', {'type': 'array', 'items': 'string'}, True),
    # no scatter arrangement rescues these
    ('scalar source, incompatible scalar sink', 'File', 'string', False),
    ('array source, incompatible scalar sink', 'File', {'type': 'array', 'items': 'string'}, False),
    ('matching scalars', 'string', 'string', True),
])
def test_the_eager_api_check_defers_what_scatter_could_rescue(
        claim: str, sink: Any, source: Any, accepted: bool) -> None:
    """The eager check has less information than the compiler and must say so.

    A `Workflow` has no resolved language version *and* no scatter yet: binding
    happens before `scatter_on()`, which lifts the sink to an array. An `Any[]`
    output bound to a scalar input and scattered afterwards compiles, so
    rejecting it at assignment preempts the judgment this check exists not to
    preempt. Disjointness is proven here only when it holds for the sink as
    declared and for the sink lifted one level.
    """
    assert _python_api_types_match(sink, source) is accepted, claim


@pytest.mark.slow
@pytest.mark.parametrize('source, sink, expected', [
    ('string', 'string', TypeRelation.OVERLAPS),
    ('string', 'File', TypeRelation.DISJOINT),
    ({'type': 'array', 'items': 'string'}, {'type': 'array', 'items': 'string'},
     TypeRelation.OVERLAPS),
    ({'type': 'array', 'items': 'string'}, 'File[]', TypeRelation.DISJOINT),
])
def test_cwltool_agrees_where_sophios_claims_knowledge(
        source: Any, sink: Any, expected: TypeRelation) -> None:
    """cwltool checks the boundary from outside; production never calls it."""
    from cwltool.checker import check_types  # pylint: disable=import-outside-toplevel

    result = check_types(copy.deepcopy(source), copy.deepcopy(sink), None, None, None)
    if expected is TypeRelation.DISJOINT:
        assert result == 'exception'
    else:
        assert result in {'pass', 'warning'}


@pytest.mark.fast
def test_production_and_generators_do_not_import_the_forbidden_oracles() -> None:
    """Production cannot delegate to cwltool; generators cannot ask production."""
    files_and_forbidden = (
        (REPO_ROOT / 'src/sophios/lang/compatibility.py', {'cwltool'}),
        (REPO_ROOT / 'tests/core/reference_model.py',
         {'sophios.lang.compatibility', 'sophios.inference'}),
        (REPO_ROOT / 'tests/core/ast_strategies.py',
         {'sophios.lang.compatibility', 'sophios.inference'}),
    )
    for path, forbidden in files_and_forbidden:
        tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
        imported = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        breaches = sorted(module for module in imported
                          if any(module == owner or module.startswith(f'{owner}.')
                                 for owner in forbidden))
        assert not breaches, f'{path.name} imports its forbidden oracle: {breaches}'


_ENDPOINTS: Final = (
    ('mk_file', 'name', 'string'),
    ('count', 'file', 'File'),
    ('scale', 'n', 'int'),
    ('scale', 'factor', 'float'),
    ('poly', 'value', ['int', 'string']),
)
_SOURCE_TYPES: Final = (
    'string', 'File', 'int', 'float', 'Any', 'string[]',
    {'type': 'array', 'items': 'Any'},
)


@pytest.mark.slow
@given(st.sampled_from(_ENDPOINTS), st.sampled_from(_SOURCE_TYPES), st.booleans())
@ORACLE
def test_workflow_input_references_follow_the_independent_outcome(
        endpoint: tuple[str, str, Any], source_type: Any, scattered: bool) -> None:
    """Unknown/overlapping references compile; only disjoint ones are wic023."""
    stem, argument, declared_sink = endpoint
    sink_type = {'type': 'array', 'items': declared_sink} if scattered else declared_sink
    expected = reference_expectation(source_type, sink_type)
    step: Yaml = {'id': stem, 'in': {argument: 'wf_source'}}
    if scattered:
        step['scatter'] = [argument]
    document: Yaml = {'inputs': {'wf_source': {'type': source_type}}, 'steps': [step]}

    if expected is ReferenceExpectation.DISJOINT:
        with pytest.raises(SophiosError) as caught:
            compile_hermetic(document, 'reference')
        assert [diagnostic.code for diagnostic in caught.value.diagnostics] == [
            SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE]
    else:
        compile_hermetic(document, 'reference')


#: `clt` in canonical normal form — the shape the inference and
#: explicit-edge paths read.
_clt = partial(clt, canonical=True)


def _edge_tools(source_type: Any, sink_type: Any) -> Tools:
    """Return two tools exposing the requested raw endpoint declarations."""
    return {
        StepId('typed_source', 'global'): Tool(
            '/synthetic/typed_source.cwl',
            _clt({'seed': {'type': 'string'},
                  'seed2': {'type': 'string', 'default': 'y'}},
                 {'value': {'type': source_type}})),
        StepId('typed_sink', 'global'): Tool(
            '/synthetic/typed_sink.cwl',
            _clt({'value': {'type': sink_type}}, {})),
    }


#: A scatter as the test models it: the entries, and the method. Kept apart from
#: the production helpers so the expectations below are a restatement of CWL's
#: rule rather than a second call into the code under test.
_STR_ARR: Final[Yaml] = {'type': 'array', 'items': 'string'}
_STR_ARR2: Final[Yaml] = {'type': 'array', 'items': _STR_ARR}


def _model_output_rank(keys: tuple[str, ...], method: str) -> int:
    """Array layers a scatter adds to every output of the step carrying it."""
    if not keys:
        return 0
    return len(keys) if method == 'nested_crossproduct' else 1


def _model_wrap(cwl_type: Any, layers: int) -> Any:
    for _ in range(layers):
        cwl_type = {'type': 'array', 'items': cwl_type}
    return cwl_type


@pytest.mark.fast
@pytest.mark.parametrize('source_type, sink_type, source_scatter, sink_scatter', [
    ('string', 'string', ((), ''), ()),
    ('string', 'File', ((), ''), ()),
    ('Any', 'string', ((), ''), ('value',)),
    ({'type': 'array', 'items': 'Any'}, 'string', ((), ''), ('value',)),
    ('string', 'string', (('seed',), 'dotproduct'), ('value',)),
    ('File', 'string', (('seed',), 'dotproduct'), ('value',)),
    # nested_crossproduct nests one level per scattered input, so a two-input
    # scatter over a `string` output is `string[][]` and this sink is satisfied.
    ('string', _STR_ARR2, (('seed', 'seed2'), 'nested_crossproduct'), ()),
    # flat_crossproduct produces one flat array however many inputs it scatters.
    ('string', _STR_ARR, (('seed', 'seed2'), 'flat_crossproduct'), ()),
    # ... so against the same sink it is one layer short, and provably disjoint.
    ('string', _STR_ARR2, (('seed', 'seed2'), 'flat_crossproduct'), ()),
    # An input listed twice is scattered twice.
    (_STR_ARR2, 'string', ((), ''), ('value', 'value')),
])
def test_explicit_edges_follow_the_same_judgment(
        source_type: Any, sink_type: Any,
        source_scatter: tuple[tuple[str, ...], str], sink_scatter: tuple[str, ...]) -> None:
    """Both endpoint scatter lifts occur before the shared judgment, at the rank
    CWL gives them rather than one layer whenever any scatter is present."""
    source_keys, source_method = source_scatter
    effective_source = _model_wrap(source_type, _model_output_rank(source_keys, source_method))
    effective_sink = _model_wrap(sink_type, sink_scatter.count('value'))
    expected = reference_expectation(effective_source, effective_sink)
    source: Yaml = {
        'id': 'typed_source',
        'in': {'seed': {'wic_inline_input': ['x'] if source_keys else 'x'}},
        'out': [{'value': {'wic_anchor': 'edge'}}],
    }
    sink: Yaml = {'id': 'typed_sink', 'in': {'value': {'wic_alias': 'edge'}}}
    if source_keys:
        source['scatter'] = list(source_keys)
        source['scatterMethod'] = source_method
        if 'seed2' in source_keys:
            source['in']['seed2'] = {'wic_inline_input': ['y']}
    if sink_scatter:
        sink['scatter'] = list(sink_scatter)

    if expected is ReferenceExpectation.DISJOINT:
        with pytest.raises(SophiosError) as caught:
            compile_hermetic({'steps': [source, sink]}, 'edges',
                             tools=_edge_tools(source_type, sink_type))
        assert caught.value.diagnostics[0].code is SophiosErrorCode.INCOMPATIBLE_INPUT_REFERENCE
    else:
        compile_hermetic({'steps': [source, sink]}, 'edges',
                         tools=_edge_tools(source_type, sink_type))


@pytest.mark.fast
def test_echo_multi_scatter_keeps_any_at_the_cwl_boundary() -> None:
    """The exact tutorial that exposed the old matcher's Any false positive."""
    path = REPO_ROOT / 'docs/tutorials/echo_multi_scatter.wic'
    document = desugar_into_canonical_normal_form(
        yaml.load(path.read_text(encoding='utf-8'), Loader=wic_loader()))
    tools: Tools = {
        StepId('array_indices', 'global'): Tool(
            '/synthetic/array_indices.cwl',
            _clt(
                {'input_array': {'type': 'string[]'}, 'input_indices': {'type': 'int[]'}},
                {'output_array': {'type': 'Any'}},
            )),
        StepId('echo_3', 'global'): Tool(
            '/synthetic/echo_3.cwl',
            _clt({'message1': {'type': 'string'}, 'message2': {'type': 'string'},
                  'message3': {'type': 'string'}}, {})),
    }
    compile_hermetic(document, 'echo_multi_scatter', tools=tools)
