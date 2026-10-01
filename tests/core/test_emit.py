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
from hypothesis import given

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
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.lang.versions import ANNOTATION_KEY, ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic, subworkflow_step


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
    out_decl = PortDeclaration(PortType('File'))
    step = StepNode(
        step_id,
        inputs=(Port(input_id, in_decl.type, in_decl),),
        outputs=(Port(output_id, out_decl.type, out_decl),),
        run=ProcessRun('write.cwl', RegistryKey('global', 'write')),
    )
    graph = WorkflowGraph(
        namespace, (step,), name='handmade', lang_version='0.0.1', cwl_version=CWL_VERSION,
        workflow_inputs=(WorkflowPort(AuthoredName('message'), BoundaryDeclaration(in_decl)),),
        workflow_outputs=(
            WorkflowPort(AuthoredName('file'), BoundaryDeclaration(out_decl),
                         StepOutputRef(step_id, AuthoredName('file')), True),),
        job_bindings=(JobBinding(AuthoredName('message'), 'hello'),),
        input_mapping=((AuthoredName('message'), (input_id,)),),
        namespaces=((ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI),),
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
        '$schemas': ['https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl'],
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
@ORACLE
def test_emit_validates_as_cwl_v1_2(workflow: Yaml) -> None:
    """CWL's external validator accepts each emitted workflow and its job inputs.

    `cwltool --validate` type-checks every link, scatter included, and every
    job value against its input's declared type. It does not open the files a
    job names. A required input the job leaves unset is one the user supplies
    at run time, so it is made optional first: cwltool then still rejects a
    link that no member of the widened type fits.
    """
    import cwltool.main  # pylint: disable=import-outside-toplevel

    info = compile_hermetic(workflow)
    inlined = sophios.post_compile.inline_artifact_runs(info.artifact).cwl
    job = info.artifact.job_inputs
    for name, declared in inlined['inputs'].items():
        if name not in job and 'default' not in declared:
            members = declared['type'] if isinstance(declared['type'], list) else [declared['type']]
            declared['type'] = members if 'null' in members else ['null', *members]
    with tempfile.TemporaryDirectory() as workdir:
        target, values = Path(workdir) / 'workflow.cwl', Path(workdir) / 'job.yml'
        target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
        values.write_text(yaml.safe_dump(job, sort_keys=False), encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target), str(values)]) == 0


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_validator_rejects_the_independent_invalid_control(tmp_path: Path) -> None:
    """A validator unable to reject would make the validity claim vacuous."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    target = tmp_path / 'invalid.cwl'
    target.write_text('class: Workflow\nsteps: []\n', encoding='utf-8')
    assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 1


@pytest.mark.fast
def test_an_untyped_authored_output_takes_its_producers_type() -> None:
    """An output with an `outputSource:` and no `type:` was emitted as `type: None`."""
    compiled = compile_hermetic({'outputs': {'o': {'outputSource': 'mk_file/file'}},
                                 'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}]})
    assert compiled.artifact.cwl['outputs']['o']['type'] == 'File'


@pytest.mark.fast
def test_an_untyped_output_under_scatter_takes_the_array_type() -> None:
    """The type taken is the one the scattered step's output has, an array."""
    compiled = compile_hermetic({'outputs': {'o': {'outputSource': 'mk_file/file'}},
                                 'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a', 'b']}},
                                            'scatter': ['name']}]})
    assert compiled.artifact.cwl['outputs']['o']['type'] == {'type': 'array', 'items': 'File'}


@pytest.mark.fast
@pytest.mark.parametrize('call, expected', [
    ({}, 'File'),
    ({'in': {'name': {'wic_inline_input': ['a', 'b']}}, 'scatter': ['name']}, {'type': 'array', 'items': 'File'}),
], ids=['call', 'scattered-call'])
def test_an_untyped_output_of_a_called_workflow_is_typed_in_the_caller(
        call: Yaml, expected: object) -> None:
    """A called workflow's output the author left untyped takes its producer's type, and
    so does the caller's output that names it, whether the call or the caller wrote it."""
    kid = {'inputs': {'name': {'type': 'string'}},
           'outputs': {'res': {'outputSource': 'kid__step__1__mk_file/file'}},
           'steps': [{'id': 'mk_file', 'in': {'name': 'name'}}]}
    compiled = compile_hermetic({
        'outputs': {'o': {'outputSource': 'kid.wic/res'}},
        'steps': [{**subworkflow_step('kid.wic', kid),
                   'parentargs': {'in': {'name': {'wic_inline_input': 'a'}}, **call}}],
    }, 'out_f')
    outputs = compiled.artifact.cwl['outputs']
    lifted = [declaration['type'] for name, declaration in outputs.items() if name.endswith('___res')]
    assert [outputs['o']['type']] + lifted == [expected, expected]


@pytest.mark.fast
@pytest.mark.parametrize('output, said', [
    ({'label': 'no type here'}, ('has no `outputSource:` to take one from',)),
    ({'outputSource': 'mk_file/fiel'},
     ('`outputSource: mk_file/fiel` names no output of a step', "Did you mean 'mk_file/file'?")),
    ({'outputSource': 'kid/res'},
     ('`outputSource: kid/res` names no output of a step', "Did you mean 'kid.wic/res'?")),
    ({'outputSource': 'nothing/at_all'},
     ('names no output of a step', 'Check the step and output names.')),
    ({'outputSource': 'kid.wic/kid__step__1__mk_file___file'},
     ('names no output of a step', 'Check the step and output names.')),
], ids=['no-source', 'misspelled-output', 'workflow-without-its-extension', 'nothing-close',
        'name-the-compiler-derives'])
def test_an_untyped_output_with_nothing_to_take_a_type_from_is_wic036(
        output: Yaml, said: tuple[str, ...]) -> None:
    """The message says which half of the author's `outputSource:` to fix, not only to add a `type:`."""
    kid = {'outputs': {'res': {'type': 'File', 'outputSource': 'kid__step__1__mk_file/file'}},
           'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'outputs': {'o': output},
                          'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
                                    subworkflow_step('kid.wic', kid)]})
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code is SophiosErrorCode.UNTYPED_OUTPUT
    assert all(part in diagnostic.message for part in ("'o'", *said)), diagnostic.message
