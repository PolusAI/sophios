"""Registry resolution and the direct typed front end.

The generated differential is byte-exact after the temporary post-Lower
handoff.  Its input filter is an independent model of the legacy scalar
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
from hypothesis import HealthCheck, given, settings

from sophios.ir import (
    RegistryKey,
    RegistrySnapshot,
    ToolDefinition,
    WorkflowSource,
    front_end,
    generated_process_id,
    legacy_after_lower,
    resolve,
)
from sophios.ir.lower import lower
from sophios.lang import InlineLiteral, SourceSpan, Step, parse
from sophios.wic_types import StepId as LegacyStepId, Yaml

from . import ast_strategies as strat
from .differential import assert_compilations_equivalent
from .equivalence import Strength
from .hermetic import ORACLE, compile_hermetic
from .source_scan import REPO_ROOT
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, inputs_of, outputs_of


def _scalar_literals_fit(workflow: Yaml) -> bool:
    """Independent model of the only generated well-formed compile rejection."""
    for step in workflow.get('steps', []):
        if not isinstance(step, dict) or not isinstance(step.get('id'), str):
            continue
        declared = inputs_of(step['id']) if LegacyStepId(step['id'], SYNTHETIC_NS) \
            in SYNTHETIC_TOOLS else {}
        for name, value in step.get('in', {}).items():
            if not isinstance(value, dict) or 'wic_inline_input' not in value:
                continue
            target = declared.get(name, {}).get('type')
            if target not in ('int', 'float'):
                continue
            try:
                (int if target == 'int' else float)(value['wic_inline_input'])
            except (TypeError, ValueError):
                return False
    return True


def _source_model(workflow: Yaml) -> tuple[str, dict[tuple[str, str], str]]:
    """Undo only the legacy loader's subtree attachment for test input source.

    This is a test-side model: real authored source names a ``.wic`` child and
    the registry supplies that child's source; ``ast_strategies.to_yml`` has
    already attached it in the shape the old compiler consumes.
    """
    sources: dict[tuple[str, str], str] = {}

    def detach(document: Yaml) -> Yaml:
        copied = copy.deepcopy(document)
        detached: list[Yaml] = []
        for step in copied.get('steps', []):
            if not isinstance(step, dict) or 'subtree' not in step:
                detached.append(step)
                continue
            child = detach(step['subtree'])
            child_name = str(step['id']).removesuffix('.wic')
            sources[(SYNTHETIC_NS, child_name)] = yaml.safe_dump(child, sort_keys=False)
            detached.append({'id': step['id'], **step.get('parentargs', {})})
        copied['steps'] = detached
        return copied

    root = detach(workflow)
    return yaml.safe_dump(root, sort_keys=False), sources


def _typed(workflow: Yaml):  # type: ignore[no-untyped-def]
    source, workflows = _source_model(workflow)
    result = front_end(source, RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows),
                       name='oracle')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.graph is not None, list(result.diagnostics)
    return result


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_resolution_is_identical_to_legacy_lookup(workflow: Yaml) -> None:
    """Typed registry resolution changes no final artifact bytes."""
    typed = _typed(copy.deepcopy(workflow))
    bridged = legacy_after_lower(typed.resolved.document, typed.graph)
    old = compile_hermetic(copy.deepcopy(workflow))
    new = compile_hermetic(bridged)
    assert_compilations_equivalent(old, new, Strength.IDENTICAL)


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
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_resolution_depends_only_on_the_registry(workflow: Yaml) -> None:
    """Filesystem and ambient environment are unavailable during resolution."""
    source, workflows = _source_model(workflow)
    parsed = parse(source, 'oracle.wic')
    assert parsed.document is not None
    snapshot = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows)

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
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_registry_order_cannot_change_resolution(workflow: Yaml) -> None:
    """Lookup has no first-match semantics over registry iteration order."""
    source, workflows = _source_model(workflow)
    parsed = parse(source, 'oracle.wic')
    assert parsed.document is not None
    canonical = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows)
    reversed_snapshot = RegistrySnapshot(tuple(reversed(canonical.tools)),
                                         tuple(reversed(canonical.workflows)))
    left = resolve(parsed.document, canonical, name='oracle')
    right = resolve(parsed.document, reversed_snapshot, name='oracle')
    assert left.document == right.document
    assert list(left.diagnostics) == list(right.diagnostics)


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_parse_front_door_is_identical_to_the_legacy_input(workflow: Yaml) -> None:
    """Source enters Parse and reaches legacy Link/Infer only after typed Lower."""
    if not _scalar_literals_fit(workflow):
        return
    typed = _typed(copy.deepcopy(workflow))
    new = compile_hermetic(legacy_after_lower(typed.resolved.document, typed.graph))
    old = compile_hermetic(copy.deepcopy(workflow))
    assert_compilations_equivalent(old, new, Strength.IDENTICAL)


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_parse_resolve_lower_are_directly_typed(workflow: Yaml) -> None:
    """Each phase consumes the preceding phase's value, not a rendered adapter."""
    source, workflows = _source_model(workflow)
    parsed = parse(source, 'oracle.wic')
    assert parsed.document is not None
    resolved = resolve(parsed.document,
                       RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows),
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
        SYNTHETIC_TOOLS, workflows={(SYNTHETIC_NS, 'child'): child})
    result = front_end('steps:\n- id: child.wic\n', registry, name='root')
    assert result.resolved is not None and result.resolved.document is not None
    assert result.graph is not None and len(result.graph.children) == 1
    assert result.resolved.document.steps[0].process.child is not None


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
    result = front_end(source, registry)
    assert result.resolved is not None and result.resolved.document is not None
    assert result.resolved.document.steps[0].process.run_path == '/alt/mk_file.cwl'


@pytest.mark.fast
def test_explicit_implementation_overrides_the_default() -> None:
    """Implementation selection is deterministic and uses the explicit choice first."""
    registry = RegistrySnapshot(
        RegistrySnapshot.from_tools(SYNTHETIC_TOOLS).tools,
        (WorkflowSource(RegistryKey('global', 'fast'), 'steps:\n- id: mk_file\n'),
         WorkflowSource(RegistryKey('global', 'safe'), 'steps:\n- id: mk_text\n')),
    )
    source = '''
wic:
  implementations: {fast: {}, safe: {}}
  default_implementation: safe
  implementation: fast
'''
    result = front_end(source, registry, name='choice')
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
    compiled = compile_hermetic(workflow).rose.data.compiled_cwl
    assert compiled['steps'][0]['in']['name'] == 'wf_name'


@pytest.mark.fast
def test_collecting_test_setup_neither_discovers_plugins_nor_writes_schemas(tmp_path: Path) -> None:
    """The corpus registry is an execution fixture, not an import side effect."""
    script = '''
import sophios.plugins
import sophios.schemas.wic_schema
def forbidden(*args, **kwargs):
    raise AssertionError("collection performed environment discovery")
sophios.plugins.get_tools_cwl = forbidden
sophios.schemas.wic_schema.get_validator = forbidden
import core.test_setup
'''
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(
        (str(REPO_ROOT / 'src'), str(REPO_ROOT / 'tests')))}
    run = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert not (tmp_path / 'autogenerated').exists()
