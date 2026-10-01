"""Registry resolution and the direct typed front end.

The generated checks compare the typed front door with the live typed compiler.
Their input filter is an independent model of the scalar
coercions; it does not call Resolve, Lower, or a production semantic judge.

BLIND SPOTS: generated workflows use the synthetic flat-tool vocabulary.
Nested workflow source, implementation precedence, generated identities, and
raw CWL each have pinned examples because they are registry shapes rather than
choices in that generator.
"""
import builtins
import copy
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from hypothesis import given

from sophios.ir import (
    RegistryKey,
    RegistrySnapshot,
    ToolDefinition,
    WorkflowSource,
    front_end,
    generated_process_id,
    resolve,
)
from sophios.ir.lower import lower
from sophios.lang import (EdgeRef, InlineLiteral, RawCwlRef, SourceSpan, Step,
                          UnresolvedName, parse)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import StepId as LegacyStepId, Yaml

from . import ast_strategies as strat
from .budgets import budget
from .hermetic import ORACLE, bundle, compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, inputs_of, outputs_of

REPO_ROOT = Path(__file__).resolve().parents[2]


def _typed(workflow: Yaml):  # type: ignore[no-untyped-def]
    model = bundle(workflow, 'oracle', SYNTHETIC_TOOLS)
    result = front_end(model.parsed, model.registry, name='oracle')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.graph is not None, list(result.diagnostics)
    return result


def _assert_processes_match_the_registry(document: Any) -> None:
    """Every non-generated process equals the tool the registry holds for it."""
    for step in document.steps:
        process = step.process
        if process.child is not None:
            _assert_processes_match_the_registry(process.child)
            continue
        if process.generated:
            continue
        # The name the step authored, not the key the resolver returned: a
        # resolver that answers every lookup with one tool reports that tool's
        # key too, so comparing its own answer to itself proves nothing.
        stem = step.source.id
        assert process.key.name == stem
        assert process.run_path == SYNTHETIC_TOOLS[LegacyStepId(stem, SYNTHETIC_NS)].run_path
        assert tuple(port.name for port in process.inputs) == tuple(inputs_of(stem))
        assert tuple(port.name for port in process.outputs) == tuple(outputs_of(stem))


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_every_resolved_process_is_the_one_the_registry_holds(workflow: Yaml) -> None:
    """Resolution is compared where the bridged differential cannot see it.

    Run path and both interfaces, per step and recursively through child
    workflows, against an independent model of the same registry -- not
    against the compiled bytes, which the source round-trip already fixes
    whatever Resolve returned.
    """
    model = bundle(copy.deepcopy(workflow), 'oracle', SYNTHETIC_TOOLS)
    parsed = model.parsed
    assert parsed.document is not None
    resolved = resolve(parsed.document,
                       model.registry,
                       name='oracle')
    assert resolved.document is not None, list(resolved.diagnostics)
    _assert_processes_match_the_registry(resolved.document)


@pytest.mark.fast
def test_resolved_interfaces_match_an_independent_registry_model() -> None:
    """A resolver returning empty interfaces cannot satisfy the differential by accident."""
    parsed = parse('steps:\n- id: join\n', 'probe.wic')
    assert parsed.document is not None
    result = resolve(parsed.document, RegistrySnapshot.from_tools(SYNTHETIC_TOOLS))
    assert result.document is not None
    process = result.document.steps[0].process
    assert tuple(port.name for port in process.inputs) == tuple(inputs_of('join'))
    assert tuple(port.name for port in process.outputs) == tuple(outputs_of('join'))


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_resolution_depends_only_on_the_registry(workflow: Yaml) -> None:
    """Filesystem and ambient environment are unavailable during resolution."""
    model = bundle(workflow, 'oracle', SYNTHETIC_TOOLS)
    parsed = model.parsed
    assert parsed.document is not None
    snapshot = model.registry

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError('resolution attempted filesystem access')

    with patch.object(builtins, 'open', forbidden), patch.object(Path, 'read_text', forbidden):
        result = resolve(parsed.document, snapshot, name='oracle')
    assert result.document is not None, list(result.diagnostics)


@pytest.mark.fast
def test_snapshot_owns_tool_definitions() -> None:
    """Mutating the caller's registry after snapshotting cannot change resolution."""
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    snapshot = RegistrySnapshot.from_tools(tools)
    tools[LegacyStepId('mk_file', SYNTHETIC_NS)].cwl['inputs'].clear()
    parsed = parse('steps:\n- id: mk_file\n', 'probe.wic')
    assert parsed.document is not None
    result = resolve(parsed.document, snapshot)
    assert result.document is not None
    assert tuple(port.name for port in result.document.steps[0].process.inputs) == ('name',)


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_registry_order_cannot_change_resolution(workflow: Yaml) -> None:
    """Lookup has no first-match semantics over registry iteration order."""
    model = bundle(workflow, 'oracle', SYNTHETIC_TOOLS)
    parsed = model.parsed
    assert parsed.document is not None
    canonical = model.registry
    reversed_snapshot = RegistrySnapshot(tuple(reversed(canonical.tools)),
                                         tuple(reversed(canonical.workflows)))
    left = resolve(parsed.document, canonical, name='oracle')
    right = resolve(parsed.document, reversed_snapshot, name='oracle')
    assert left.document == right.document
    assert list(left.diagnostics) == list(right.diagnostics)


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@budget(200)
def test_parse_resolve_lower_are_directly_typed(workflow: Yaml) -> None:
    """Each phase consumes the preceding phase's value, not a rendered adapter."""
    model = bundle(workflow, 'oracle', SYNTHETIC_TOOLS)
    parsed = model.parsed
    assert parsed.document is not None
    resolved = resolve(parsed.document,
                       model.registry,
                       name='oracle')
    assert resolved.document is not None, list(resolved.diagnostics)
    lowered = lower(resolved.document)
    assert lowered.graph is not None, list(lowered.diagnostics)
    assert len(lowered.graph.steps) == len(resolved.document.steps)


@pytest.mark.fast
def test_nested_workflow_source_is_resolved_from_the_snapshot() -> None:
    """A child source is registry content, not a path Resolve may open."""
    child = 'steps:\n- id: mk_file\n  in:\n    name: !ii child.txt\n'
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): parse(child, 'child.wic')})
    result = front_end(parse('steps:\n- id: child.wic\n', 'root.wic'), registry, name='root')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.graph is not None and len(result.graph.children) == 1
    assert result.resolved.document.steps[0].process.child is not None


@pytest.mark.fast
@pytest.mark.parametrize('key', ['inputs', 'outputs'])
@pytest.mark.parametrize('directive', ['$import', '$include'])
def test_a_called_workflow_listing_its_ports_with_an_import_is_reported(key: str, directive: str) -> None:
    """The ports are in a file only cwltool reads, so no step can be checked against them.

    Calling such a workflow is reported at the call, not met with a `KeyError`.
    """
    child = {key: [{directive: 'shared.yml'}],
             'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [subworkflow_step('child.wic', child)]})
    [diagnostic] = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    assert f'workflow global/child lists its {key}:' in diagnostic.message


@pytest.mark.fast
def test_step_sidecar_namespace_selects_the_process() -> None:
    """The closest sidecar namespace wins over the default global namespace."""
    global_tool = SYNTHETIC_TOOLS[LegacyStepId('mk_file', SYNTHETIC_NS)]
    alt = ToolDefinition(RegistryKey('alt', 'mk_file'), '/alt/mk_file.cwl',
                         copy.deepcopy(global_tool.cwl))
    registry = RegistrySnapshot(RegistrySnapshot.from_tools(SYNTHETIC_TOOLS).tools + (alt,))
    source = '''
wic:
  steps:
    (1, mk_file):
      wic:
        namespace: alt
steps:
- id: mk_file
'''
    result = front_end(parse(source, 'workflow.wic'), registry)
    assert result.resolved is not None and result.resolved.document is not None
    assert result.resolved.document.steps[0].process.run_path == '/alt/mk_file.cwl'


@pytest.mark.fast
def test_explicit_implementation_overrides_the_default() -> None:
    """Implementation selection is deterministic and uses the explicit choice first."""
    registry = RegistrySnapshot(
        RegistrySnapshot.from_tools(SYNTHETIC_TOOLS).tools,
        (WorkflowSource(RegistryKey('global', 'fast'), parse('steps:\n- id: mk_file\n')),
         WorkflowSource(RegistryKey('global', 'safe'), parse('steps:\n- id: mk_text\n'))),
    )
    source = '''
wic:
  implementations: {fast: {}, safe: {}}
  default_implementation: safe
  implementation: fast
'''
    result = front_end(parse(source, 'choice.wic'), registry, name='choice')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.resolved.document.steps[0].source.id == 'mk_file'


@pytest.mark.fast
def test_generated_process_identity_is_content_deterministic() -> None:
    """Generated identities neither use uuid nor depend on source spans."""
    span = SourceSpan('probe.wic', 1, 1, 1, 1)
    one = Step('python_script', inputs=(('script', InlineLiteral('a.py', span)),))
    same = Step('python_script', inputs=(('script', InlineLiteral('a.py', span)),))
    other = Step('python_script', inputs=(('script', InlineLiteral('b.py', span)),))
    assert generated_process_id(one) == generated_process_id(same)
    assert generated_process_id(one) != generated_process_id(other)


@pytest.mark.fast
def test_raw_cwl_reference_needs_no_global_escape_hatch() -> None:
    """The local tag reaches CWL unchanged while ordinary bare names stay governed."""
    workflow = {'inputs': {'wf_name': {'type': 'string'}},
                'steps': [{'id': 'mk_file', 'in': {'name': {'wic_raw_cwl': 'wf_name'}}}]}
    compiled = compile_hermetic(workflow).artifact.cwl
    assert compiled['steps'][0]['in']['name'] == 'wf_name'


@pytest.mark.fast
def test_collecting_test_setup_neither_discovers_plugins_nor_writes_files(tmp_path: Path) -> None:
    """The corpus registry is an execution fixture, not an import side effect."""
    script = '''
import sophios.plugins
def forbidden(*args, **kwargs):
    raise AssertionError("collection performed environment discovery")
sophios.plugins.get_tools_cwl = forbidden
import core.test_setup
'''
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(
        (str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')))}
    run = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert not (tmp_path / 'autogenerated').exists()


# Parameter passing: a document contributes to a step of a subworkflow it calls
# by nesting `wic:` blocks, keyed `(index, name)` at every level. The corpus
# uses three body shapes -- a bare `wic:` that only recurses, an `in:`, and an
# `out:` carrying an edge definition -- and `mm-workflows`' `basic.wic` places
# the only definition of one of its edges this way, so a contribution that goes
# astray deletes an edge rather than reporting anything. One pin per shape,
# plus the two rules that decide whether a contribution lands at all.


def _resolved(source: str, **workflows: str) -> Any:
    """Resolve `source` against the synthetic tools plus the named child sources."""
    parsed = parse(source, 'root.wic')
    assert parsed.document is not None, list(parsed.diagnostics)
    registry = RegistrySnapshot.from_tools(
        SYNTHETIC_TOOLS,
        workflows={(SYNTHETIC_NS, name): parse(child, f'{name}.wic')
                   for name, child in workflows.items()})
    result = resolve(parsed.document, registry, name='root')
    assert result.document is not None, list(result.diagnostics)
    return result.document


def _descend(document: Any, depth: int) -> Any:
    """The document reached by entering each first step's subworkflow `depth` times."""
    for _ in range(depth):
        child = document.steps[0].process.child
        assert child is not None
        document = child
    return document


@pytest.mark.fast
def test_contributed_input_reaches_a_step_of_a_called_subworkflow() -> None:
    """An `in:` written at a distance binds that step's input in the child."""
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (1, mk_file):
            in:
              name: !ii contributed.txt
steps:
- id: child.wic
''', child='steps:\n- id: mk_file\n')
    bound = _descend(document, 1).steps[0].source.input('name')
    assert isinstance(bound, InlineLiteral) and bound.value == 'contributed.txt'


@pytest.mark.fast
def test_contributed_output_defines_an_edge_on_a_nested_step() -> None:
    """`out:` carries `!&` to a step two subworkflows down, as `basic.wic` does.

    The definition exists nowhere else: writing it in the child would define it
    twice when that child is called from a second root, which is the whole
    reason the corpus places it from above.
    """
    document = _resolved('''
wic:
  steps:
    (1, min.wic):
      wic:
        steps:
          (1, cg.wic):
            wic:
              steps:
                (1, mk_file):
                  out:
                  - file: !& min.tpr
steps:
- id: min.wic
''', min='steps:\n- id: cg.wic\n', cg='steps:\n- id: mk_file\n  in:\n    name: !ii min.tpr\n')
    outputs = _descend(document, 2).steps[0].source.outputs
    assert [(binding.name, binding.edge_def and binding.edge_def.name)
            for binding in outputs] == [('file', 'min.tpr')]


@pytest.mark.fast
def test_a_bare_wic_body_only_recurses_until_it_reaches_a_payload() -> None:
    """Three nested `wic:` bodies carry nothing themselves; the fourth binds."""
    document = _resolved('''
wic:
  steps:
    (1, a.wic):
      wic:
        steps:
          (1, b.wic):
            wic:
              steps:
                (1, c.wic):
                  wic:
                    steps:
                      (1, mk_file):
                        in:
                          name: !* deep.txt
steps:
- id: a.wic
''', a='steps:\n- id: b.wic\n', b='steps:\n- id: c.wic\n', c='steps:\n- id: mk_file\n')
    bound = _descend(document, 3).steps[0].source.input('name')
    assert isinstance(bound, EdgeRef) and bound.name == 'deep.txt'


@pytest.mark.fast
def test_a_contributed_value_wins_over_the_child_s_own() -> None:
    """Overriding is the point of parameter passing: the contributor wins."""
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (1, mk_file):
            in:
              name: !ii from_the_parent.txt
steps:
- id: child.wic
''', child='steps:\n- id: mk_file\n  in:\n    name: !ii its_own.txt\n')
    bound = _descend(document, 1).steps[0].source.input('name')
    assert isinstance(bound, InlineLiteral) and bound.value == 'from_the_parent.txt'


@pytest.mark.fast
def test_a_contribution_needs_both_the_index_and_the_name_to_agree() -> None:
    """`(N, name)` addresses one occurrence, so half a match is no match."""
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (2, mk_file):
            in:
              name: !ii wrong_index.txt
          (1, mk_text):
            in:
              name: !ii wrong_name.txt
steps:
- id: child.wic
''', child='steps:\n- id: mk_file\n  in:\n    name: !ii its_own.txt\n')
    bound = _descend(document, 1).steps[0].source.input('name')
    assert isinstance(bound, InlineLiteral) and bound.value == 'its_own.txt'


@pytest.mark.fast
def test_a_contribution_to_a_wic_step_addresses_the_subworkflow_not_the_call() -> None:
    """A `.wic` step's body names steps inside it; nothing lands on the call site."""
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      in:
        name: !ii never_bound_here.txt
      wic:
        steps:
          (1, mk_file):
            in:
              name: !ii contributed.txt
steps:
- id: child.wic
''', child='steps:\n- id: mk_file\n')
    assert document.steps[0].source.inputs == ()
    bound = _descend(document, 1).steps[0].source.input('name')
    assert isinstance(bound, InlineLiteral) and bound.value == 'contributed.txt'


_CONTRIBUTED_TEMPLATE = '''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (1, mk_file):
            in:
              name: {spelling}
steps:
- id: child.wic
'''


def _contributed_construct(spelling: str) -> tuple[str, Any]:
    """The construct and payload a contributed `name:` carries, spans aside."""
    document = _resolved(_CONTRIBUTED_TEMPLATE.format(spelling=spelling),
                         child='steps:\n- id: mk_file\n')
    value = _descend(document, 1).steps[0].source.input('name')
    payload = {InlineLiteral: 'value', EdgeRef: 'name',
               RawCwlRef: 'expression', UnresolvedName: 'name'}[type(value)]
    return type(value).__name__, getattr(value, payload)


@pytest.mark.fast
@pytest.mark.parametrize('tagged, desugared, construct', [
    ('!ii shared.txt', '{wic_inline_input: shared.txt}', ('InlineLiteral', 'shared.txt')),
    ('!* shared.txt', '{wic_alias: shared.txt}', ('EdgeRef', 'shared.txt')),
    ('!cwl shared.txt', '{wic_raw_cwl: shared.txt}', ('RawCwlRef', 'shared.txt')),
    ('shared.txt', 'shared.txt', ('UnresolvedName', 'shared.txt')),
    ('{a: 1}', '{a: 1}', ('InlineLiteral', {'a': 1})),
])
def test_a_contributed_value_means_the_same_in_both_surfaces(
        tagged: str, desugared: str, construct: tuple[str, Any]) -> None:
    """A contribution is opaque content, where each construct has two spellings.

    The tagged one is what a human writes and the desugared one is what the
    Python API and `render` emit, so a contribution that only understood tags
    would apply to a hand-written document and vanish from a round-tripped one.
    """
    assert _contributed_construct(tagged) == construct
    assert _contributed_construct(desugared) == construct


@pytest.mark.fast
def test_a_contributed_out_replaces_the_step_s_own_sequence() -> None:
    """`out:` is a sequence, and a sequence the contributor supplies replaces.

    Merging the two instead would keep an edge definition the contributor
    deliberately removed, and define it twice wherever the contributor put its
    own back.
    """
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (1, mk_file):
            out:
            - file
steps:
- id: child.wic
''', child='steps:\n- id: mk_file\n  out:\n  - file: !& its_own_edge\n')
    outputs = _descend(document, 1).steps[0].source.outputs
    assert [(binding.name, binding.edge_def) for binding in outputs] == [('file', None)]


@pytest.mark.fast
def test_a_contribution_merges_with_the_child_s_own_body_for_the_same_step() -> None:
    """Both bodies address `(1, xform)`; neither replaces the other wholesale.

    A subworkflow that already parameterises one of its own steps is the
    ordinary case -- `cg.wic` does -- so a contribution that overwrote the
    whole `(N, name)` body would delete whatever the child had written there,
    and one that overwrote the whole `in:` would delete the bindings it does
    not itself mention.
    """
    document = _resolved('''
wic:
  steps:
    (1, child.wic):
      wic:
        steps:
          (1, xform):
            in:
              name: !ii contributed.txt
steps:
- id: child.wic
''', child='''
wic:
  steps:
    (1, xform):
      in:
        file: !* upstream
      out:
      - file: !& its_own_edge
steps:
- id: xform
''')
    step = _descend(document, 1).steps[0].source
    upstream, contributed = step.input('file'), step.input('name')
    assert isinstance(upstream, EdgeRef) and upstream.name == 'upstream'
    assert isinstance(contributed, InlineLiteral) and contributed.value == 'contributed.txt'
    assert [(binding.name, binding.edge_def and binding.edge_def.name)
            for binding in step.outputs] == [('file', 'its_own_edge')]
