"""Canonical CWL emission from the workflow graph.

Emit is an exact projection of one graph, so its local determinism checks are
byte-sensitive. That is deliberately narrower than the compiler migration's
end-to-end compatibility contract, which is behavioral equivalence at
``UP_TO_EMBEDDING``. Separate graph-only tests make the projection claim
non-vacuous and pin the phase boundary.

BLIND SPOTS: generated workflows inherit ``ast_strategies.workflows``'s
declared exclusions.  Validation does not execute CWL.
"""
import copy
from pathlib import Path
import tempfile

import pytest
import yaml
from hypothesis import HealthCheck, given, settings

import sophios.post_compile
from sophios.ir import (
    BoundaryDeclaration,
    Direction,
    JobBinding,
    Namespace,
    Port,
    PortDeclaration,
    PortId,
    PortType,
    ProcessRun,
    RegistryKey,
    Source,
    StepEmission,
    StepId,
    StepNode,
    WorkflowGraph,
    WorkflowPort,
    emit,
    emit_job_inputs,
    surface,
)
from sophios.ir.names import Names
from sophios.ir.types import AuthoredName, StepOutputRef
from sophios.lang.cwl import CWL_VERSION
from sophios.lang.versions import ANNOTATION_KEY, ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_the_live_compiler_emits_only_from_its_graph(workflow: Yaml) -> None:
    """The public compiler's artifact is exactly its final graph projection."""
    result = compile_hermetic(copy.deepcopy(workflow))
    names = Names.of(result.graph)
    assert result.artifact.cwl == emit(surface(result.graph, names), names)


@pytest.mark.fast
@pytest.mark.parametrize('authored', [
    [{'class': 'ResourceRequirement', 'coresMin': 1}],
    None,
], ids=['list-form', 'bare'])
def test_a_requirements_shape_sophios_does_not_model_survives_emission(authored: object) -> None:
    """`requirements:` in a shape the compiler never writes reaches CWL unchanged.

    Pinned rather than generated: `ast_strategies.workflows` has no
    `requirements:` dimension, so no generated property reaches either
    spelling. Both are valid CWL and the compiler only ever builds the
    mapping form, so nothing else would notice a phase that assumed it.
    """
    workflow = {'requirements': authored,
                'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
    compiled = compile_hermetic(copy.deepcopy(workflow)).artifact.cwl
    assert compiled['requirements'] == authored


@pytest.mark.fast
def test_differential_oracle_detects_a_changed_document() -> None:
    """A same-arm comparison or a disabled equivalence relation cannot pass."""
    workflow = {'steps': [{'id': 'mk_file',
                           'in': {'name': {'wic_inline_input': 'x'}}}]}
    result = compile_hermetic(copy.deepcopy(workflow))
    changed = copy.deepcopy(result.artifact.cwl)
    changed['class'] = 'CommandLineTool'
    assert equivalent(result.artifact.cwl, changed, Strength.IDENTICAL) is not None


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_one_graph_emits_identically(workflow: Yaml) -> None:
    """A graph, not its mutable source dictionaries, determines every byte.

    Emit needs nothing beyond it: the source is emptied between the two
    projections and neither byte moves.
    """
    source = copy.deepcopy(workflow)
    graph = compile_hermetic(source).graph
    names = Names.of(graph)
    document = surface(graph, names)
    first = emit(document, names)
    source.clear()
    second = emit(document, names)
    assert equivalent(first, second, Strength.IDENTICAL) is None


@pytest.mark.fast
def test_a_hand_built_graph_emits_without_a_compiler_adapter() -> None:
    """The graph projection cannot be green only through compiler construction.

    Hand-built and surfaced, so this states the document the compiler ships:
    the `run:` path written under the step directory and EDAM declared.
    """
    namespace = Namespace()
    step_id = StepId(namespace, 1, 'write')
    input_id = PortId(step_id, Direction.INPUT, AuthoredName('message'))
    output_id = PortId(step_id, Direction.OUTPUT, AuthoredName('file'))
    in_decl = PortDeclaration(PortType('string'))
    out_decl = PortDeclaration(PortType('File'), field_order=('type', 'outputSource'))
    step = StepNode(
        step_id,
        inputs=(Port(input_id, in_decl.type, in_decl),),
        outputs=(Port(output_id, out_decl.type, out_decl),),
        emission=StepEmission(
            ((AuthoredName('message'), Source(AuthoredName('message'))),),
            ProcessRun('write.cwl', RegistryKey('global', 'write')), (AuthoredName('file'),),
        ),
    )
    graph = WorkflowGraph(
        namespace, (step,), name='handmade', lang_version='0.0.1', cwl_version=CWL_VERSION,
        workflow_inputs=(WorkflowPort(AuthoredName('message'), BoundaryDeclaration(in_decl)),),
        workflow_outputs=(
            WorkflowPort(AuthoredName('file'), BoundaryDeclaration(out_decl),
                         StepOutputRef(step_id, AuthoredName('file')), True),),
        job_bindings=(JobBinding(AuthoredName('message'), 'hello'),),
        namespaces=((ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI),),
        field_order=('steps', 'cwlVersion', 'class', '$namespaces', 'inputs',
                     ANNOTATION_KEY, 'outputs'),
    )
    names = Names.of(graph)
    document = surface(graph, names)
    assert emit(document, names) == {
        'steps': [{'id': 'handmade__step__1__write', 'in': {'message': {'source': 'message'}},
                   'run': 'handmade__step__1__write/write.cwl', 'out': ['file']}],
        'cwlVersion': CWL_VERSION,
        'class': 'Workflow',
        '$namespaces': {'edam': 'https://edamontology.org/',
                        ANNOTATION_NAMESPACE: ANNOTATION_NAMESPACE_URI},
        'inputs': {'message': {'type': 'string'}},
        ANNOTATION_KEY: '0.0.1',
        'outputs': {'file': {'type': 'File',
                             'outputSource': 'handmade__step__1__write/file'}},
    }
    assert emit_job_inputs(document, names) == {'message': 'hello'}


@pytest.mark.needs_cwltool
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_emit_validates_as_cwl_v1_2(workflow: Yaml) -> None:
    """CWL's external validator accepts each graph-derived artifact."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    info = compile_hermetic(workflow)
    inlined = sophios.post_compile.inline_artifact_runs(info.artifact).cwl
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
