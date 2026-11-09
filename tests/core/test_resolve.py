"""Registry resolution and the direct typed front end, compared with the live compiler under an independent model of
scalar coercions. Blind spot: synthetic flat tools; nested sources, precedence, identities and raw CWL are examples."""
import builtins
import copy
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

import pytest
from hypothesis import example, given

from sophios.ir import (
    RegistryKey,
    RegistrySnapshot,
    ToolDefinition,
    WorkflowSource,
    front_end,
    generated_process_id,
    resolve,
)
from sophios.lang import (EdgeRef, InlineLiteral, RawCwlRef, SourceSpan, Step,
                          UnresolvedName, parse)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import StepId as LegacyStepId, Yaml

from . import ast_strategies as strat
from .cases import cases, match
from .hermetic import ORACLE, bundle, compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, inputs_of, outputs_of


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
@example({'steps': [{'id': 'join'}]})  # a resolver returning empty interfaces cannot satisfy this by accident
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


_PAYLOAD: Final = {InlineLiteral: 'value', EdgeRef: 'name', RawCwlRef: 'expression', UnresolvedName: 'name'}


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('contributions', 'root', 'children', 'expect', 'stderr'))
def test_contribution_case(case: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    """A document contributes to a step of a subworkflow it calls by nesting `wic:` blocks keyed `(index, name)`,
    and a contribution gone astray deletes an edge rather than reporting anything.

    `root` and its `children` resolve; each `expect` entry names the step at `step` (default 0) of the document
    `depth` calls below the root, and what it binds: `inputs` as `[construct, payload]` and `outputs` as
    `[name, edge defined]`. `stderr` is what is printed.
    """
    document = _resolved(case['root'], **case.get('children', {}))
    for expected in case['expect']:
        step = _descend(document, expected.get('depth', 0)).steps[expected.get('step', 0)].source
        found = {**expected, 'outputs': [[out.name, out.edge_def and out.edge_def.name] for out in step.outputs],
                 'inputs': {name: [type(value).__name__, getattr(value, _PAYLOAD[type(value)])]
                            for name, value in step.inputs}}
        assert match(expected, found), (case['id'], found)
    if 'stderr' in case:
        assert match(case['stderr'], capsys.readouterr().err.splitlines())
