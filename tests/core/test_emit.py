"""Canonical CWL emission from the workflow graph.

The legacy finalizer is an oracle, never an input to Emit.  Generated
differentials demand byte-identical documents; separate graph-only tests make
that comparison non-vacuous and pin the phase boundary.

BLIND SPOTS: generated workflows inherit ``ast_strategies.workflows``'s
declared exclusions.  Validation does not execute CWL.  The static boundary
guard detects direct imports and calls; Python reflection could evade it, so
the planted-mutation test proves the detector against the ordinary breach.
"""
import ast
import copy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
import yaml
from hypothesis import HealthCheck, given, settings

import sophios.post_compile
from sophios.ir import (
    Direction,
    JobBinding,
    Namespace,
    Port,
    PortDeclaration,
    PortId,
    PortType,
    ProcessRun,
    StepEmission,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
    emit,
    emit_job_inputs,
)
from sophios.lang.cwl import CWL_VERSION
from sophios.lang.versions import ANNOTATION_KEY, ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .differential import assert_compilations_equivalent
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic
from .source_scan import REPO_ROOT


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_emit_is_identical_to_the_legacy_finalizer(workflow: Yaml) -> None:
    """The new terminal projection changes no byte of any workflow artifact."""
    old = compile_hermetic(copy.deepcopy(workflow), legacy_emission=True)
    new = compile_hermetic(copy.deepcopy(workflow))
    assert_compilations_equivalent(old, new, Strength.IDENTICAL)


@pytest.mark.fast
def test_differential_oracle_detects_a_changed_document() -> None:
    """A same-arm comparison or a disabled equivalence relation cannot pass."""
    workflow = {'steps': [{'id': 'mk_file',
                           'in': {'name': {'wic_inline_input': 'x'}}}]}
    old = compile_hermetic(copy.deepcopy(workflow), legacy_emission=True)
    changed = copy.deepcopy(old.rose.data.compiled_cwl)
    changed['class'] = 'CommandLineTool'
    assert equivalent(old.rose.data.compiled_cwl, changed, Strength.IDENTICAL) is not None


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_one_graph_emits_identically(workflow: Yaml) -> None:
    """A graph, not its mutable source dictionaries, determines every byte."""
    source = copy.deepcopy(workflow)
    graph = compile_hermetic(source).rose.data.emission_graph
    first = emit(graph)
    source.clear()
    second = emit(graph)
    assert equivalent(first, second, Strength.IDENTICAL) is None


@pytest.mark.fast
def test_a_hand_built_graph_emits_without_the_legacy_bridge() -> None:
    """The differential cannot be green merely because the bridge did all work."""
    namespace = Namespace()
    step_id = StepId(namespace, 1, 'write')
    input_id = PortId(step_id, Direction.INPUT, 'message')
    output_id = PortId(step_id, Direction.OUTPUT, 'file')
    in_decl = PortDeclaration(PortType('string'))
    out_decl = PortDeclaration(PortType('File'), field_order=('type', 'outputSource'))
    step = StepNode(
        step_id,
        inputs=(Port(input_id, in_decl.type, in_decl),),
        outputs=(Port(output_id, out_decl.type, out_decl),),
        emission=StepEmission(
            'write', (('message', {'source': 'message'}),),
            ProcessRun('write.cwl', 'global/write'), ('file',),
        ),
    )
    graph = WorkflowGraph(
        namespace, (step,), name='handmade', lang_version='0.0.1', cwl_version=CWL_VERSION,
        workflow_inputs=(WorkflowPort('message', in_decl),),
        workflow_outputs=(WorkflowPort('file', out_decl, 'write/file', True),),
        job_bindings=(JobBinding('message', 'hello'),),
        namespaces=((ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI),),
        field_order=('steps', 'cwlVersion', 'class', '$namespaces', 'inputs',
                     ANNOTATION_KEY, 'outputs'),
    )
    assert emit(graph) == {
        'steps': [{'id': 'write', 'in': {'message': {'source': 'message'}},
                   'run': 'write.cwl', 'out': ['file']}],
        'cwlVersion': CWL_VERSION,
        'class': 'Workflow',
        '$namespaces': {ANNOTATION_NAMESPACE: ANNOTATION_NAMESPACE_URI},
        'inputs': {'message': {'type': 'string'}},
        ANNOTATION_KEY: '0.0.1',
        'outputs': {'file': {'type': 'File', 'outputSource': 'write/file'}},
    }
    assert emit_job_inputs(graph) == {'message': 'hello'}


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
def test_hash_seed_does_not_change_emit() -> None:
    """Four interpreter hash seeds witness process-level determinism."""
    script = """
import json
from tests.core.hermetic import compile_hermetic
w = {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
print(json.dumps(compile_hermetic(w).rose.data.compiled_cwl, separators=(',', ':')))
"""
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(REPO_ROOT / 'src'), str(REPO_ROOT)))}
    results = []
    for seed in ('1', '2', '17', '101'):
        run = subprocess.run([sys.executable, '-c', script], cwd=REPO_ROOT,
                             env={**env, 'PYTHONHASHSEED': seed}, capture_output=True,
                             text=True, check=True)
        results.append(json.loads(run.stdout))
    assert all(result == results[0] for result in results[1:])


@pytest.mark.needs_cwltool
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_emit_validates_as_cwl_v1_2(workflow: Yaml) -> None:
    """CWL's external validator accepts each graph-derived artifact."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    info = compile_hermetic(workflow)
    inlined = sophios.post_compile.cwl_inline_runtag(info.rose).data.compiled_cwl
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / 'workflow.cwl'
        target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 0


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_validator_rejects_the_independent_invalid_control(tmp_path: Path) -> None:
    """A validator unable to reject would make the validity claim vacuous."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    target = tmp_path / 'invalid.cwl'
    target.write_text('class: Workflow\nsteps: []\n', encoding='utf-8')
    assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 1


def _emit_boundary(source: str) -> tuple[str, ...]:
    """Direct dependencies forbidden to a graph-only terminal projection."""
    tree = ast.parse(source)
    forbidden = {'compiler', 'plugins', 'config', 'pathlib', 'os'}
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            findings.extend(alias.name for alias in node.names
                            if alias.name.split('.')[0] in forbidden)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.lstrip('.').split('.')[0]
            if root in forbidden:
                findings.append(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in {'open', 'getattr', 'eval', 'exec'}:
            findings.append(node.func.id)
    return tuple(findings)


@pytest.mark.fast
def test_emit_has_only_graph_dependencies() -> None:
    """Emit cannot discover tools, read files, or consult compiler state."""
    path = REPO_ROOT / 'src' / 'sophios' / 'ir' / 'emit.py'
    assert not _emit_boundary(path.read_text(encoding='utf-8'))


@pytest.mark.fast
def test_boundary_guard_detects_a_planted_dependency() -> None:
    """The static half demonstrably fails for the breach it claims to catch."""
    source = inspect.cleandoc('''
        from pathlib import Path
        def emit(graph):
            return Path("registry.yml").read_text()
    ''')
    assert _emit_boundary(source)


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_emit_needs_no_state_beyond_the_graph(workflow: Yaml) -> None:
    """A graph remains sufficient after compiler policy and source are destroyed."""
    graph = compile_hermetic(copy.deepcopy(workflow)).rose.data.emission_graph
    expected = emit(graph)
    from sophios import compiler  # pylint: disable=import-outside-toplevel
    assert not hasattr(compiler, 'inference_rules')
    assert emit(graph) == expected
