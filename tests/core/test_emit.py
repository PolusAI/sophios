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
from typing import Any, Final

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
from sophios.wic_types import StepId as LegacyStepId, Tool, Tools, Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic, compile_hermetic_cwl, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt


@pytest.mark.skip_pypi_ci
@given(strat.workflows())
@ORACLE
def test_the_live_compiler_emits_only_from_its_graph(workflow: Yaml) -> None:
    """The public compiler's artifact is exactly its final graph projection."""
    result = compile_hermetic(copy.deepcopy(workflow))
    names = Names.of(result.graph)
    assert result.artifact.cwl == emit(surface(result.graph, names), names)


@pytest.mark.fast
@pytest.mark.parametrize('authored, emitted', [
    ([{'class': 'ResourceRequirement', 'coresMin': 1}], {'ResourceRequirement': {'coresMin': 1}}),
    (None, None),
], ids=['list-form', 'bare'])
def test_authored_requirements_are_emitted_as_the_mapping_form(authored: object, emitted: object) -> None:
    """CWL's list form of `requirements:` is emitted as the mapping the compiler writes.

    Pinned rather than generated: `ast_strategies.workflows` has no
    `requirements:` dimension. A bare `requirements:` has no entries to lift and
    reaches CWL as written.
    """
    workflow = {'requirements': authored,
                'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
    compiled = compile_hermetic(copy.deepcopy(workflow)).artifact.cwl
    assert compiled['requirements'] == emitted


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
    ({'outputSource': ['mk_file/file']}, ('is written as a list', 'Add `type:`.')),
    ({'outputSource': []}, ('is written as a list', 'Add `type:`.')),
    ({'outputSource': ['mk_file/file', 'mk_file/file']}, ('is written as a list', 'Add `type:`.')),
    ({'outputSource': 'nothing/at_all'},
     ('names no output of a step', 'Check the step and output names.')),
    ({'outputSource': 'kid.wic/kid__step__1__mk_file___file'},
     ('names no output of a step', 'Check the step and output names.')),
], ids=['no-source', 'misspelled-output', 'workflow-without-its-extension', 'source-list', 'empty-source-list',
        'two-sources', 'nothing-close', 'name-the-compiler-derives'])
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


@pytest.mark.fast
def test_list_form_outputs_and_requirements_compile_as_their_mapping_form() -> None:
    """A scattered step beside list-form `outputs:` and `requirements:` emits what the mapping spelling does."""
    def compiled(requirements: Any, outputs: Any) -> Yaml:
        return compile_hermetic_cwl({
            'requirements': requirements, 'outputs': outputs,
            'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a', 'b']}}, 'scatter': ['name']}]})

    listed = compiled([{'class': 'InlineJavascriptRequirement'}],
                      [{'id': 'o', 'type': 'File', 'outputSource': 'mk_file/file'}])
    mapped = compiled({'InlineJavascriptRequirement': {}},
                      {'o': {'type': 'File', 'outputSource': 'mk_file/file'}})
    assert listed == mapped
    assert listed['outputs']['o']['outputSource'] == 'oracle__step__1__mk_file/file'
    assert set(listed['requirements']) == {'InlineJavascriptRequirement', 'ScatterFeatureRequirement'}


def _tools_with_probe(inputs: Yaml, outputs: Yaml, namespaces: Yaml | None = None, *,
                      tools: Tools | None = None, stem: str = 'probe') -> Tools:
    """`tools` (the synthetic registry by default) plus a tool called `stem` declaring these ports and prefixes."""
    probe = clt(inputs, outputs)
    if namespaces:
        probe['$namespaces'] = namespaces
    registry = copy.deepcopy(SYNTHETIC_TOOLS if tools is None else tools)
    registry[LegacyStepId(stem, SYNTHETIC_NS)] = Tool(f'/synthetic/{stem}.cwl', probe)
    return registry


@pytest.mark.fast
def test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state() -> None:
    """A tool input promoted to the boundary keeps what `WorkflowInputParameter` allows, not its `inputBinding`."""
    tools = _tools_with_probe(
        {'f': {'type': 'File', 'secondaryFiles': ['.idx'], 'streamable': True, 'loadContents': True,
               'loadListing': 'shallow_listing', 'inputBinding': {'position': 1}}}, {})
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=tools)
    assert compiled['inputs'] == {'oracle__step__1__probe___f': {
        'type': 'File', 'secondaryFiles': ['.idx'], 'streamable': True, 'loadContents': True,
        'loadListing': 'shallow_listing'}}


_EXPRESSION_ENTRY: Final = {'pattern': '$(self.basename + ".bai")', 'required': False}
_REQUIRED_EXPRESSION_ENTRY: Final = {'pattern': '.fai', 'required': '$(inputs.strict)'}


@pytest.mark.fast
@pytest.mark.parametrize('declared, promoted', [
    ([_EXPRESSION_ENTRY], None),
    ('$(self.basename + ".idx")', None),
    ('${return self.basename + ".idx";}', None),
    (['.idx', _EXPRESSION_ENTRY, {'pattern': '.crai', 'required': False}, _REQUIRED_EXPRESSION_ENTRY,
      {'pattern': '${return self.basename + ".bai";}', 'required': False}],
     ['.idx', {'pattern': '.crai', 'required': False}]),
], ids=['only-expression', 'single-string-expression', 'single-string-script',
        'static-entries-beside-expressions'])
def test_a_promoted_port_leaves_a_secondary_files_expression_to_its_tool(declared: Any, promoted: Any) -> None:
    """The boundary states the static `secondaryFiles` entries and no expression.

    An expression is evaluated against its tool's `inputs` and `expressionLib`,
    which the workflow does not have, and the tool's `InlineJavascriptRequirement`
    is not the workflow's: a promoted expression fails in cwltool at run time.
    `plain` is the control: a port with a static entry, promoted beside it.
    """
    tools = _tools_with_probe(
        {'f': {'type': 'File', 'secondaryFiles': declared, 'inputBinding': {'position': 1}},
         'plain': {'type': 'File', 'secondaryFiles': ['.dict'], 'inputBinding': {'position': 2}}},
        {'o': {'type': 'File', 'secondaryFiles': declared, 'outputBinding': {'glob': 'o'}},
         'plain': {'type': 'File', 'secondaryFiles': ['.dict'], 'outputBinding': {'glob': 'p'}}})
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=tools)
    inputs, outputs = compiled['inputs'], compiled['outputs']
    assert inputs['oracle__step__1__probe___plain']['secondaryFiles'] == ['.dict']
    assert outputs['oracle__step__1__probe___plain']['secondaryFiles'] == ['.dict']
    assert inputs['oracle__step__1__probe___f'].get('secondaryFiles') == promoted
    assert outputs['oracle__step__1__probe___o'].get('secondaryFiles') == promoted
    assert 'InlineJavascriptRequirement' not in (compiled.get('requirements') or {})


@pytest.mark.fast
def test_a_promoted_output_keeps_the_fields_a_workflow_output_may_state() -> None:
    """A tool output promoted to the boundary keeps what `WorkflowOutputParameter` allows, not its `outputBinding`."""
    tools = _tools_with_probe(
        {}, {'o': {'type': 'File', 'secondaryFiles': ['.bai'], 'streamable': True,
                   'outputBinding': {'glob': 'o.bam'}}})
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=tools)
    assert compiled['outputs'] == {'oracle__step__1__probe___o': {
        'type': 'File', 'secondaryFiles': ['.bai'], 'streamable': True,
        'outputSource': 'oracle__step__1__probe/o'}}


_MYNS: Final = 'http://tool.example/'


def _tools_binding_myns(**uris: str) -> Tools:
    """One tool per stem, each taking a `File` whose `format:` is a `myns:` CURIE the tool binds to its URI."""
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    for stem, uri in uris.items():
        tools = _tools_with_probe({'f': {'type': 'File', 'format': 'myns:format_1'}}, {}, {'myns': uri},
                                  tools=tools, stem=stem)
    return tools


@pytest.mark.fast
def test_a_tools_namespace_prefix_is_declared_by_the_document_that_promotes_its_port() -> None:
    """The promoted port keeps its `myns:` CURIE, so the document that promotes it declares `myns`."""
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=_tools_binding_myns(probe=_MYNS))
    assert compiled['inputs']['oracle__step__1__probe___f']['format'] == 'myns:format_1'
    assert compiled['$namespaces'].get('myns') == _MYNS


@pytest.mark.fast
def test_a_prefix_declared_beneath_a_subworkflow_is_declared_by_the_root_that_promotes_it() -> None:
    """A port promoted through a subworkflow reaches the root with its CURIE, so the root declares the prefix."""
    compiled = compile_hermetic_cwl({'steps': [subworkflow_step('child.wic', {'steps': [{'id': 'probe'}]})]},
                                    tools=_tools_binding_myns(probe=_MYNS))
    [promoted] = compiled['inputs'].values()
    assert promoted['format'] == 'myns:format_1'
    assert compiled['$namespaces'].get('myns') == _MYNS


@pytest.mark.fast
def test_a_workflow_an_explicit_edge_crosses_declares_the_prefix_of_the_step_beneath_its_subworkflow() -> None:
    """An edge from the root into a grandchild promotes its port through the child, which keeps the CURIE.

    The step the port derives from sits two workflows down, so the child owns
    the port by the call that reaches it, and declares the prefix that call's
    workflow declares.
    """
    grandchild = {'steps': [{'id': 'probe', 'in': {'f': {'wic_alias': 'shared'}}}]}
    info = compile_hermetic({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'shared'}}]},
        subworkflow_step('child.wic', {'steps': [subworkflow_step('grand.wic', grandchild)]})]},
        tools=_tools_binding_myns(probe=_MYNS))
    child = next(artifact for artifact in info.artifact.children if artifact.name == 'child')
    assert child.cwl['inputs']['grand__step__1__probe___f']['format'] == 'myns:format_1'
    assert child.cwl['$namespaces']['myns'] == _MYNS


@pytest.mark.fast
def test_tools_binding_a_prefix_alike_declare_it_once_and_edam_is_left_to_emit() -> None:
    """Two tools agreeing on `myns` are no conflict; their disagreeing `edam` is not one either.

    Emit binds `edam` itself, to the canonical URI, so what a tool says for it
    cannot reach the document, though `mk_file` promotes an `edam:` format.
    """
    tools = _tools_binding_myns(probe=_MYNS, other=_MYNS)
    for stem, uri in (('probe', 'http://edamontology.org/'), ('other', 'https://edamontology.org/')):
        tools[LegacyStepId(stem, SYNTHETIC_NS)].cwl['$namespaces']['edam'] = uri
    compiled = compile_hermetic_cwl({'steps': [
        {'id': 'probe'}, {'id': 'other'}, {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]},
        tools=tools)
    assert compiled['outputs']['oracle__step__3__mk_file___file']['format'] == 'edam:format_2330'
    assert compiled['$namespaces']['myns'] == _MYNS
    assert compiled['$namespaces']['edam'] == 'https://edamontology.org/'


_OTHER_URI: Final = 'http://other.example/'


@pytest.mark.fast
@pytest.mark.parametrize('workflow, tools, first, second', [
    ({'$namespaces': {'myns': _OTHER_URI}, 'steps': [{'id': 'probe'}]},
     _tools_binding_myns(probe=_MYNS), (_OTHER_URI, 'this document'), (_MYNS, "step 'probe'")),
    ({'steps': [{'id': 'probe'}, {'id': 'other'}]},
     _tools_binding_myns(probe=_MYNS, other=_OTHER_URI), (_MYNS, "step 'probe'"), (_OTHER_URI, "step 'other'")),
    ({'steps': [subworkflow_step('child.wic', {'steps': [{'id': 'probe'}]}), {'id': 'other'}]},
     _tools_binding_myns(probe=_MYNS, other=_OTHER_URI), (_MYNS, "step 'child.wic'"), (_OTHER_URI, "step 'other'")),
    ({'steps': [subworkflow_step('child.wic', {'steps': [{'id': 'probe'}, {'id': 'other'}]})]},
     _tools_binding_myns(probe=_MYNS, other=_OTHER_URI), (_MYNS, "step 'probe'"), (_OTHER_URI, "step 'other'")),
], ids=['document-and-tool', 'tool-and-tool', 'subworkflow-and-tool', 'two-tools-in-a-subworkflow'])
def test_a_prefix_two_promoting_sources_bind_differently_is_reported(
        workflow: Yaml, tools: Tools, first: tuple[str, str], second: tuple[str, str]) -> None:
    """The promoted CURIE would resolve to whichever binding the document kept, so neither is kept.

    The sources are the document and the steps owning a promoted port that says
    `myns:`. The one diagnostic is `wic031`, and names the prefix and what each
    source binds it to.
    """
    with pytest.raises(SophiosError) as caught:
        compile_hermetic_cwl(workflow, tools=tools)
    [diagnostic] = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.DUPLICATE_DOCUMENT_NAME
    assert "'myns'" in diagnostic.message
    for uri, source in (first, second):
        assert repr(uri) in diagnostic.message and source in diagnostic.message


def _tools_clashing_on_schema_org(*, probe_port: Yaml, namespaces: Yaml) -> Tools:
    """`probe` takes `probe_port` and binds `namespaces`; `other` also binds `s` to a different URI."""
    tools = _tools_with_probe({'f': probe_port}, {}, namespaces)
    return _tools_with_probe({'g': {'type': 'File'}}, {}, {'s': 'https://schema.org/'}, tools=tools, stem='other')


@pytest.mark.fast
@pytest.mark.parametrize('probe_port, namespaces, declared', [
    ({'type': 'File'}, {'s': 'http://schema.org/'}, {}),
    ({'type': 'File', 'format': 'myns:format_1'}, {'s': 'http://schema.org/', 'myns': _MYNS}, {'myns': _MYNS}),
], ids=['no-prefix-used', 'another-prefix-used'])
def test_a_clash_on_a_prefix_no_promoted_format_uses_is_no_error(
        probe_port: Yaml, namespaces: Yaml, declared: Yaml, capsys: pytest.CaptureFixture[str]) -> None:
    """Two tools binding `s` differently conflict with nothing when no promoted port says `s:`.

    The document is the one its tools' agreement would give, and it declares
    only what a promoted format needs. Nothing is printed.
    """
    workflow = {'steps': [{'id': 'probe'}, {'id': 'other'}]}
    compiled = compile_hermetic_cwl(workflow, tools=_tools_clashing_on_schema_org(
        probe_port=probe_port, namespaces=namespaces))
    control = compile_hermetic_cwl(workflow, tools=_tools_with_probe(
        {'f': probe_port}, {}, {'myns': _MYNS}, tools=_tools_with_probe({'g': {'type': 'File'}}, {}, stem='other')))
    assert compiled == control
    assert compiled['$namespaces'] == {**declared, 'edam': 'https://edamontology.org/',
                                       ANNOTATION_NAMESPACE: ANNOTATION_NAMESPACE_URI}
    assert capsys.readouterr() == ('', '')


_ELSE_URI: Final = 'http://else.example/'


@pytest.mark.fast
@pytest.mark.parametrize('other_port, declared', [
    ({'type': 'File'}, {}),
    ({'type': 'File', 'format': 'elsens:format_2'}, {'elsens': _ELSE_URI}),
], ids=['no-format', 'a-format-with-another-prefix'])
def test_a_step_promoting_no_format_with_a_prefix_is_no_source_for_it_wherever_it_sits(
        other_port: Yaml, declared: Yaml) -> None:
    """Only the document and the steps owning a promoted `myns:` port are sources for `myns`.

    `other` binds `myns` elsewhere, but no CURIE it promotes says `myns:`, so
    its binding conflicts with nothing: beside `probe` or beneath a
    subworkflow, the root declares the same prefixes.
    """
    tools = _tools_with_probe({'g': other_port}, {}, {'myns': _OTHER_URI, 'elsens': _ELSE_URI},
                              tools=_tools_binding_myns(probe=_MYNS), stem='other')
    beside = compile_hermetic_cwl({'steps': [{'id': 'probe'}, {'id': 'other'}]}, tools=tools)
    beneath = compile_hermetic_cwl(
        {'steps': [{'id': 'probe'}, subworkflow_step('c.wic', {'steps': [{'id': 'other'}]})]}, tools=tools)
    expected = {'myns': _MYNS, **declared, 'edam': 'https://edamontology.org/',
                ANNOTATION_NAMESPACE: ANNOTATION_NAMESPACE_URI}
    assert beside['$namespaces'] == expected
    assert beneath['$namespaces'] == expected


@pytest.mark.fast
def test_a_prefix_only_a_promoted_output_format_uses_is_declared() -> None:
    """A promoted output keeps its `format:` CURIE as an input does, so the document declares its prefix."""
    tools = _tools_with_probe(
        {}, {'o': {'type': 'File', 'format': 'myns:format_1', 'outputBinding': {'glob': 'o'}}}, {'myns': _MYNS})
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=tools)
    assert compiled['outputs']['oracle__step__1__probe___o']['format'] == 'myns:format_1'
    assert compiled['$namespaces']['myns'] == _MYNS


@pytest.mark.fast
def test_a_port_the_author_wrote_keeps_its_own_prefix_whatever_a_tool_binds() -> None:
    """A `format:` the author wrote at the boundary uses the author's binding, which no tool's can contradict."""
    workflow = {'$namespaces': {'myns': _OTHER_URI},
                'inputs': {'x': {'type': 'File', 'format': 'myns:format_1'}},
                'steps': [{'id': 'probe', 'in': {'f': 'x'}}]}
    compiled = compile_hermetic_cwl(workflow, tools=_tools_binding_myns(probe=_MYNS))
    assert compiled['inputs']['x']['format'] == 'myns:format_1'
    assert compiled['$namespaces']['myns'] == _OTHER_URI
