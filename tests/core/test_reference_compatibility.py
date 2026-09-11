"""The versioned, conservative boundary for user-authored references."""

import ast
import copy
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from sophios.lang.compatibility import TypeRelation, reference_relation
from sophios.lang.cwl import CWL_VERSION
from sophios.lang.diagnostics import Code, SophiosError
from sophios.lang.versions import KNOWN_VERSIONS
from sophios.inlineing import get_inlineable_subworkflows
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Cwl, StepId, Tool, Tools, Yaml, YamlTree

from .hermetic import ORACLE, compile_hermetic
from .reference_model import ReferenceExpectation, reference_expectation

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

_CASES: Final = (
    ('same atom', 'string', 'string', ReferenceExpectation.OVERLAPS),
    ('different atoms', 'string', 'File', ReferenceExpectation.DISJOINT),
    ('nullable shorthand', 'string', 'string?', ReferenceExpectation.OVERLAPS),
    ('union overlap', 'string', ['null', 'string'], ReferenceExpectation.OVERLAPS),
    ('union disjoint', ['int', 'boolean'], 'string', ReferenceExpectation.DISJOINT),
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


@pytest.mark.fast
def test_scattered_subworkflow_is_not_offered_to_the_ast_inliner() -> None:
    """Inlining cannot erase scatter and manufacture a disjoint reference."""
    tree = YamlTree(StepId('root', 'global'), {
        'steps': [{
            'id': 'child.wic',
            'subtree': {'steps': [{'id': 'sink'}]},
            'parentargs': {'scatter': ['sink___value']},
        }],
    })

    assert get_inlineable_subworkflows(tree, {}, False, []) == []


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
            Code.INCOMPATIBLE_INPUT_REFERENCE]
    else:
        compile_hermetic(document, 'reference')


def _clt(inputs: dict[str, Cwl], outputs: dict[str, Cwl]) -> Cwl:
    """Build a minimal synthetic tool for explicit-edge integration cases."""
    return desugar_into_canonical_normal_form({
        'cwlVersion': CWL_VERSION,
        'class': 'CommandLineTool',
        'baseCommand': 'true',
        'inputs': inputs,
        'outputs': outputs,
    })


def _edge_tools(source_type: Any, sink_type: Any) -> Tools:
    """Return two tools exposing the requested raw endpoint declarations."""
    return {
        StepId('typed_source', 'global'): Tool(
            '/synthetic/typed_source.cwl',
            _clt({'seed': {'type': 'string'}}, {'value': {'type': source_type}})),
        StepId('typed_sink', 'global'): Tool(
            '/synthetic/typed_sink.cwl',
            _clt({'value': {'type': sink_type}}, {})),
    }


@pytest.mark.fast
@pytest.mark.parametrize('source_type, sink_type, source_scatter, sink_scatter', [
    ('string', 'string', False, False),
    ('string', 'File', False, False),
    ('Any', 'string', False, True),
    ({'type': 'array', 'items': 'Any'}, 'string', False, True),
    ('string', 'string', True, True),
    ('File', 'string', True, True),
])
def test_explicit_edges_follow_the_same_judgment(
        source_type: Any, sink_type: Any, source_scatter: bool, sink_scatter: bool) -> None:
    """Both endpoint scatter lifts occur before the shared judgment."""
    effective_source = {'type': 'array', 'items': source_type} if source_scatter else source_type
    effective_sink = {'type': 'array', 'items': sink_type} if sink_scatter else sink_type
    expected = reference_expectation(effective_source, effective_sink)
    source: Yaml = {
        'id': 'typed_source',
        'in': {'seed': {'wic_inline_input': ['x'] if source_scatter else 'x'}},
        'out': [{'value': {'wic_anchor': 'edge'}}],
    }
    sink: Yaml = {'id': 'typed_sink', 'in': {'value': {'wic_alias': 'edge'}}}
    if source_scatter:
        source['scatter'] = ['seed']
    if sink_scatter:
        sink['scatter'] = ['value']

    if expected is ReferenceExpectation.DISJOINT:
        with pytest.raises(SophiosError) as caught:
            compile_hermetic({'steps': [source, sink]}, 'edges',
                             tools=_edge_tools(source_type, sink_type))
        assert caught.value.diagnostics[0].code is Code.INCOMPATIBLE_INPUT_REFERENCE
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
