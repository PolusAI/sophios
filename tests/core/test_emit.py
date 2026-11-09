"""Canonical CWL emission from the workflow graph: byte-sensitive determinism, narrower than the compiler's contract
(UP_TO_EMBEDDING). Generated workflows inherit ast_strategies.workflows's exclusions; validation runs no CWL."""
import copy
from pathlib import Path
import tempfile
from typing import Any, Final

import pytest
from cwl_utils.parser import cwl_v1_2
from hypothesis import assume, example, given, strategies as st
from schema_salad.utils import yaml_no_ts  # pylint: disable=no-name-in-module

import sophios.post_compile
from sophios.input_output import dump_wic_yaml
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
from sophios.wic_types import StepId as LegacyStepId, Tools, Yaml

from . import ast_strategies as strat
from .cases import match
from .cwl_validation import validate_cwl
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic, compile_hermetic_cwl, network_refused, subworkflow_step
from .strategies import pinned
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt, registered


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
    the `run:` path, the file beside the root named by the root and the step, and nothing of the
    compiler's own: no format says `edam:`, so no EDAM namespace either.
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
    )
    names = Names.of(graph)
    document = surface(graph, names)
    assert emit(document, names) == {
        'steps': [{'id': 'write', 'in': {'message': {'source': 'message'}},
                   'run': 'handmade~write.cwl', 'out': ['file']}],
        'cwlVersion': CWL_VERSION,
        'class': 'Workflow',
        'inputs': {'message': {'type': 'string'}},
        'outputs': {'file': {'type': 'File',
                             'outputSource': 'write/file'}},
    }
    assert emit_job_inputs(document, names) == {'message': 'hello'}


def _file_locations(value: Any) -> list[str]:
    """Every `location` of a `File` object anywhere in a job value."""
    if isinstance(value, list):
        return [location for item in value for location in _file_locations(item)]
    if not isinstance(value, dict):
        return []
    if value.get('class') == 'File':
        return [value['location']]
    return [location for item in value.values() for location in _file_locations(item)]


def _secondary(location: str, pattern: Any) -> str:
    """The secondary file a `secondaryFiles` pattern names beside `location`."""
    text = str(pattern['pattern'] if isinstance(pattern, dict) else pattern)
    suffix = text.lstrip('^')
    for _ in range(len(text) - len(suffix)):
        location = location.rsplit('.', 1)[0]
    return location + suffix


def _job_files(declared: Yaml, job: Yaml) -> list[str]:
    """Every file a job names, each with the secondary files its input declares."""
    found: list[str] = []
    for name, value in job.items():
        patterns = declared.get(name, {}).get('secondaryFiles', [])
        for location in _file_locations(value):
            found.append(location)
            found.extend(_secondary(location, pattern)
                         for pattern in (patterns if isinstance(patterns, list) else [patterns]))
    return found


#: Stems whose emitted workflow cwltool rejects today, each with the reason,
#: and kept out of the validation property below. Every other property still
#: draws them. `test_a_schema_def_typed_input_validates` is a strict xfail, so
#: the exclusion cannot outlive the gap.
NOT_YET_VALID: Final[dict[str, str]] = {
    'schemed': ('its input names a SchemaDefRequirement type, and two references to it break: the promoted '
                'workflow input carries the bare name without the requirement that defines it, and the '
                "embedded tool's own bare reference no longer resolves to its SDR once the run is inlined"),
}


def _cwltool_validates(workflow: Yaml) -> bool:
    """`cwltool --validate` accepts the emitted workflow and its job inputs. Files named for `loadContents` or
    `secondaryFiles` are created empty, a required unset input is made optional, and `$schemas` is dropped (cwltool
    would fetch it; test_leak_boundary pins that it survives)."""
    info = compile_hermetic(workflow)
    inlined = sophios.post_compile.inline_artifact_runs(info.artifact).cwl
    job = info.artifact.job_inputs
    for name, declared in inlined['inputs'].items():
        if name not in job and 'default' not in declared:
            members = declared['type'] if isinstance(declared['type'], list) else [declared['type']]
            declared['type'] = members if 'null' in members else ['null', *members]
    inlined.pop('$schemas', None)
    with tempfile.TemporaryDirectory() as workdir, network_refused() as attempts:
        target, values = Path(workdir) / 'workflow.cwl', Path(workdir) / 'job.yml'
        target.write_text(dump_wic_yaml(inlined), encoding='utf-8')
        values.write_text(dump_wic_yaml(job), encoding='utf-8')
        for location in _job_files(inlined['inputs'], job):
            (Path(workdir) / location).touch()
        valid = validate_cwl(str(target), str(values)) == 0
    assert not attempts
    return bool(valid)


@pytest.mark.needs_cwltool
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows().filter(lambda w: not {s['id'] for s in w['steps']} & NOT_YET_VALID.keys()))
# A string input bound to a value YAML 1.2 would retype still validates as a string.
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '._'}}}]})
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '._5'}}}]})
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '1e3'}}}]})
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '1E3'}}}]})
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '0o17'}}}]})
@example({'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': '0o7'}}}]})
@ORACLE
def test_emit_validates_as_cwl_v1_2(workflow: Yaml) -> None:
    """CWL's external validator accepts each emitted workflow and its job inputs."""
    assert _cwltool_validates(workflow)


#: Strings YAML 1.1 reads as strings and a YAML 1.2 reader, cwltool's, reads as another type.
_STRINGS_CWLTOOL_RETYPES: Final[list[str]] = ['._', '._5', '1e3', '1E3', '0o17', '0o7']
#: Strings both readers agree to type, so the writer must already quote them.
_STRINGS_YAML_RETYPES: Final[list[str]] = [
    '1_000', 'yes', 'no', 'on', 'off', 'Yes', 'NO', 'True', '.5', '0x1F', '~', 'null', '1.', '.inf', '.nan',
    '+1', '1:30', '0b1', '2001-12-14', '2001-12-14t21:59:43.10-05:00', '', ' padded', '=', '<<']


@pytest.mark.fast
@given(st.lists(st.text() | st.text(' :#-?![]{}&*|>\'"%@`,.0123456789eExoOtTyYnN~+=<_')))
@example(_STRINGS_CWLTOOL_RETYPES + _STRINGS_YAML_RETYPES)
@ORACLE
def test_a_string_job_value_is_a_string_to_cwltool(texts: list[str]) -> None:
    """A job file keeps each string a string, as a value, in a list and as a key, when read the way cwltool reads it."""
    for document in ({f'x{i}': text for i, text in enumerate(texts)}, {'x': texts}, dict(zip(texts, texts))):
        assert yaml_no_ts().load(dump_wic_yaml(document)) == document, document


@pytest.mark.needs_cwltool
@pytest.mark.fast
@pytest.mark.xfail(strict=True, reason=NOT_YET_VALID['schemed'])
def test_a_schema_def_typed_input_validates() -> None:
    """A workflow whose step input is typed by a SchemaDefRequirement name is valid CWL."""
    assert _cwltool_validates({'steps': [
        {'id': 'schemed', 'in': {'coords': {'wic_inline_input': {'x': 1.0, 'y': 2.0}}}}]})


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_validator_rejects_the_independent_invalid_control(tmp_path: Path) -> None:
    """A validator unable to reject would make the validity claim vacuous."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    target = tmp_path / 'invalid.cwl'
    target.write_text('class: Workflow\nsteps: []\n', encoding='utf-8')
    with network_refused() as attempts:
        assert cwltool.main.main(['--validate', '--quiet', '--skip-schemas', str(target)]) == 1
    assert not attempts


@pytest.mark.fast
@pytest.mark.parametrize('scatter, expected', [
    ({}, 'File'),
    ({'in': {'name': {'wic_inline_input': ['a', 'b']}}, 'scatter': ['name']}, {'type': 'array', 'items': 'File'}),
], ids=['plain', 'scattered'])
def test_an_untyped_authored_output_takes_its_producers_type(scatter: Yaml, expected: object) -> None:
    """An output with an `outputSource:` and no `type:` was emitted as `type: None`; under scatter it takes the
    array type the scattered step's output has. A called workflow's output the author left untyped takes its
    producer's type too, and so does the caller's output that names it, whether the call or the caller wrote it."""
    compiled = compile_hermetic({'outputs': {'o': {'outputSource': 'mk_file/file'}},
                                 'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, **scatter}]})
    assert compiled.artifact.cwl['outputs']['o']['type'] == expected
    kid = {'inputs': {'name': {'type': 'string'}},
           'outputs': {'res': {'outputSource': 'mk_file/file'}},
           'steps': [{'id': 'mk_file', 'in': {'name': 'name'}}]}
    called = compile_hermetic({
        'outputs': {'o': {'outputSource': 'kid.wic/res'}},
        'steps': [{**subworkflow_step('kid.wic', kid),
                   'parentargs': {'in': {'name': {'wic_inline_input': 'a'}}, **scatter}}],
    }, 'out_f')
    outputs = called.artifact.cwl['outputs']
    lifted = [declaration['type'] for name, declaration in outputs.items() if name.endswith('~res')]
    assert [outputs['o']['type']] + lifted == [expected, expected]


@pytest.mark.fast
@pytest.mark.parametrize('output, said', [
    ({'label': 'no type here'}, ('has no `outputSource:` to take one from',)),
    ({'outputSource': 'mk_file/fiel'},
     ('`outputSource: mk_file/fiel` names no output of a step',  # codespell:ignore fiel
      "Did you mean 'mk_file/file'?")),
    ({'outputSource': 'kid/res'},
     ('`outputSource: kid/res` names no output of a step', "Did you mean 'kid.wic/res'?")),
    ({'outputSource': 'nothing/at_all'},
     ('names no output of a step', 'Check the step and output names.')),
    ({'outputSource': 'kid.wic/mk_file~file'},
     ('names no output of a step', "Did you mean 'mk_file/file'?")),
    ({'outputSource': 'oracle__step__1__mk_file/file'},
     ('`outputSource: oracle__step__1__mk_file/file` names no output of a step',)),
], ids=['no-source', 'misspelled-output', 'workflow-without-its-extension', 'nothing-close',
        'name-the-compiler-derives', 'old-generated-spelling-names-nothing'])
def test_an_untyped_output_with_nothing_to_take_a_type_from_is_wic036(
        output: Yaml, said: tuple[str, ...]) -> None:
    """The message says which half of the author's `outputSource:` to fix, not only to add a `type:`."""
    kid = {'outputs': {'res': {'type': 'File', 'outputSource': 'mk_file/file'}},
           'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]}
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'outputs': {'o': output},
                          'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}},
                                    subworkflow_step('kid.wic', kid)]})
    (diagnostic,) = caught.value.diagnostics
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
    assert listed['outputs']['o']['outputSource'] == 'mk_file/file'
    assert set(listed['requirements']) == {'InlineJavascriptRequirement', 'ScatterFeatureRequirement'}


def _tools_with_probe(inputs: Yaml, outputs: Yaml, namespaces: Yaml | None = None, *,
                      tools: Tools | None = None, stem: str = 'probe') -> Tools:
    """`tools` (the synthetic registry by default) plus a tool called `stem` declaring these ports and prefixes."""
    probe = clt(inputs, outputs)
    if namespaces:
        probe['$namespaces'] = namespaces
    return registered({stem: probe}, SYNTHETIC_TOOLS if tools is None else tools)


#: How a tool may declare `secondaryFiles`: each entry static or an expression, in its pattern or its `required`, and
#: the field bare, a mapping or a list. `_SECONDARY_CASES` are the declarations the properties below were written from.
_EXPRESSION_ENTRY: Final = {'pattern': '$(self.basename + ".bai")', 'required': False}
_SECONDARY: Final = st.sampled_from(['.idx', '$(self.basename + ".idx")', '${return self.basename + ".idx";}',
                                     {'pattern': '.crai', 'required': False}, _EXPRESSION_ENTRY,
                                     {'pattern': '.fai', 'required': '$(inputs.strict)'},
                                     {'pattern': '${return self.basename + ".bai";}', 'required': False}])
_SECONDARY_CASES: Final = ([_EXPRESSION_ENTRY], '$(self.basename + ".idx")', '${return self.basename + ".idx";}',
                           ['.idx', _EXPRESSION_ENTRY, {'pattern': '.crai', 'required': False},
                            {'pattern': '.fai', 'required': '$(inputs.strict)'},
                            {'pattern': '${return self.basename + ".bai";}', 'required': False}],
                           '.bai', {'pattern': '.bai', 'required': False}, ['.bai'], None)
_PORT_FIELDS: Final = {'secondaryFiles': st.none() | _SECONDARY | st.lists(_SECONDARY, max_size=4),
                       'streamable': st.booleans(), 'doc': st.sampled_from(['a doc', ['two', 'lines']]),
                       'label': st.just('a label'), 'format': st.just('http://example.org/format')}


def _promoted(port: Yaml, schema: Any) -> Yaml:
    """What the boundary may state of a tool port: each CWL v1.2 `schema` field but a binding, and of `secondaryFiles`
    the static entries as a list (a promoted expression fails in cwltool at run time, the bare form crashes it)."""
    kept = {key: value for key, value in port.items() if key in schema.attrs and key != 'inputBinding'}
    declared = kept.pop('secondaryFiles', None)
    static = [entry for entry in (declared if isinstance(declared, list) else [declared] * (declared is not None))
              if not any('$(' in str(text) or '${' in str(text)
                         for text in (entry.values() if isinstance(entry, dict) else [entry]))]
    return kept | ({'secondaryFiles': static} if static else {})


#: The control: a port with a static entry, promoted beside the drawn one.
_PLAIN: Final = {'type': 'File', 'secondaryFiles': ['.dict']}


def _boundary(inputs: Yaml, outputs: Yaml) -> Yaml:
    """The workflow `probe`, a tool with these ports and `plain`, compiles to; the tool's
    `InlineJavascriptRequirement`, which an expression needs, is not the workflow's."""
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=_tools_with_probe(
        {**inputs, 'plain': {**_PLAIN, 'inputBinding': {'position': 2}}},
        {**outputs, 'plain': {**_PLAIN, 'outputBinding': {'glob': 'p'}}}))
    assert 'InlineJavascriptRequirement' not in (compiled.get('requirements') or {})
    return compiled


@pytest.mark.fast
@given(st.fixed_dictionaries({'type': st.just('File'), 'inputBinding': st.just({'position': 1})}, optional={
    **_PORT_FIELDS, 'loadContents': st.booleans(),
    'loadListing': st.sampled_from(['no_listing', 'shallow_listing', 'deep_listing'])}))
@pinned({'type': 'File', 'secondaryFiles': ['.idx'], 'streamable': True, 'loadContents': True,
         'loadListing': 'shallow_listing', 'inputBinding': {'position': 1}},
        *({'type': 'File', 'secondaryFiles': declared, 'inputBinding': {'position': 1}}
          for declared in _SECONDARY_CASES))
@ORACLE
def test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state(port: Yaml) -> None:
    """A tool input promoted to the boundary keeps what `WorkflowInputParameter` allows, not its `inputBinding`, and
    of its `secondaryFiles` the static entries, as a list."""
    promoted = _boundary({'f': port}, {})['inputs']
    assert promoted == {'probe~f': _promoted(port, cwl_v1_2.WorkflowInputParameter), 'probe~plain': _PLAIN}


@pytest.mark.fast
@given(st.fixed_dictionaries({'type': st.just('File'), 'outputBinding': st.just({'glob': 'o.bam'})},
                             optional=_PORT_FIELDS))
@pinned({'type': 'File', 'secondaryFiles': ['.bai'], 'streamable': True, 'outputBinding': {'glob': 'o.bam'}},
        *({'type': 'File', 'secondaryFiles': declared, 'outputBinding': {'glob': 'o'}}
          for declared in _SECONDARY_CASES))
@ORACLE
def test_a_promoted_output_keeps_the_fields_a_workflow_output_may_state(port: Yaml) -> None:
    """A tool output promoted to the boundary keeps what `WorkflowOutputParameter` allows, not its `outputBinding`, and
    of its `secondaryFiles` the static entries, as a list."""
    promoted = _boundary({}, {'o': port})['outputs']
    assert promoted == {'probe~o': {**_promoted(port, cwl_v1_2.WorkflowOutputParameter), 'outputSource': 'probe/o'},
                        'probe~plain': {**_PLAIN, 'outputSource': 'probe/plain'}}


_REQUIREMENTS = st.sampled_from(['ResourceRequirement', 'InlineJavascriptRequirement', 'ScatterFeatureRequirement',
                                 'DockerRequirement'])
_BODIES = st.sampled_from([None, {'coresMin': 4}, {'expressionLib': ['function f(){}']}])


def _written(entries: dict[str, Any], listed: bool) -> Any:
    """`entries` as the document writes them: CWL's list form, or the mapping."""
    return [{'class': name, **(body or {})} for name, body in entries.items()] if listed else copy.deepcopy(entries)


@pytest.mark.fast
@given(st.dictionaries(_REQUIREMENTS, _BODIES, max_size=3), st.dictionaries(_REQUIREMENTS, _BODIES.filter(bool),
                                                                            max_size=2),
       st.booleans(), st.booleans(), st.booleans())
@example({'ResourceRequirement': {'coresMin': 4}, 'InlineJavascriptRequirement': {'expressionLib': ['function f(){}']}},
         {}, False, True, True)
@example({'ScatterFeatureRequirement': None, 'InlineJavascriptRequirement': None}, {}, False, True, True)
@example({'ResourceRequirement': {'coresMin': 1}}, {'DockerRequirement': {'coresMin': 4}}, True, False, False)
@example(None, {}, False, False, False).xfail(raises=AssertionError, reason='a bare requirements: reaches CWL as '
                                              'null, which cwltool refuses')
@example(None, {}, False, True, False).xfail(raises=AssertionError, reason='a bare requirements: drops the '
                                             'ScatterFeatureRequirement a scatter needs')
@ORACLE
def test_user_requirements_are_merged_into_not_copied(  # pylint: disable=too-many-positional-arguments
        authored: dict[str, Any] | None, hints: dict[str, Any], listed: bool, scatter: bool, when: bool) -> None:
    """`requirements` keeps what you wrote and gains what the workflow needs (language spec §1.1).

    The third compiler-owned key, and the one the reference used to list as an example of pure passthrough. Each
    class the workflow needs, `ScatterFeatureRequirement` for a scatter and `InlineJavascriptRequirement` for `when`,
    is added only when it is not already there, so a class the user already wrote keeps its body, and one written with
    no body, YAML null, which cwltool rejects, is an empty one. CWL's list form is read as the mapping, of
    `requirements:` and of `hints:` on the document and the step, and nothing to write is no key."""
    needs = {name for name, needed in (('ScatterFeatureRequirement', scatter), ('InlineJavascriptRequirement', when))
             if needed}
    assume(authored is None or all(body is not None or name in needs for name, body in authored.items()))
    step = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': ['a', 'b'] if scatter else 'a'}},
            **({'scatter': ['name']} if scatter else {}), **({'when': '$(true)'} if when else {})}
    hinted = {'hints': _written(hints, listed)} if hints else {}
    cwl = compile_hermetic({'requirements': None if authored is None else _written(authored, listed),
                            'steps': [{**step, **hinted}], **hinted}).artifact.cwl
    assert cwl.get('requirements', {}) == {**{name: body or {} for name, body in (authored or {}).items()},
                                           **{name: {} for name in needs - set(authored or {})}}
    assert not hints or cwl['hints'] == cwl['steps'][0]['hints'] == hints


@pytest.mark.fast
@pytest.mark.xfail(strict=True, raises=AssertionError, reason='a class with no body the workflow does not need '
                   'stays null, which cwltool refuses')
def test_a_requirement_written_with_no_body_is_an_empty_one() -> None:
    """The case `assume` rules out of the property above: nothing needs the class, so its null body is kept."""
    step = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}
    cwl = compile_hermetic({'requirements': {'ResourceRequirement': None}, 'steps': [step]}).artifact.cwl
    assert cwl['requirements'] == {'ResourceRequirement': {}}


_MYNS: Final = 'http://tool.example/'


def _tools_binding_myns(**uris: str) -> Tools:
    """One tool per stem, each taking a `File` whose `format:` is a `myns:` CURIE the tool binds to its URI."""
    tools = SYNTHETIC_TOOLS
    for stem, uri in uris.items():
        tools = _tools_with_probe({'f': {'type': 'File', 'format': 'myns:format_1'}}, {}, {'myns': uri},
                                  tools=tools, stem=stem)
    return tools


def _tools_prefix_alike() -> Tools:
    """Two tools agreeing on `myns`, and disagreeing on `edam`, which Emit binds itself."""
    tools = _tools_binding_myns(probe=_MYNS, other=_MYNS)
    for stem, uri in (('probe', 'http://edamontology.org/'), ('other', 'https://edamontology.org/')):
        tools[LegacyStepId(stem, SYNTHETIC_NS)].cwl['$namespaces']['edam'] = uri
    return tools


_OTHER_URI: Final = 'http://other.example/'
_MYNS_FORMAT: Final = {'format': 'myns:format_1'}


@pytest.mark.fast
@pytest.mark.parametrize('workflow, tools, expected', [
    ({'steps': [{'id': 'probe'}]}, _tools_binding_myns(probe=_MYNS),
     {'inputs': {'probe~f': _MYNS_FORMAT}, '$namespaces': {'myns': _MYNS}}),
    ({'steps': [{'id': 'probe'}, {'id': 'other'}, {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}}]},
     _tools_prefix_alike(),
     {'outputs': {'mk_file~file': {'format': 'edam:format_2330'}},
      '$namespaces': {'myns': _MYNS, 'edam': 'https://edamontology.org/'}}),
    ({'steps': [{'id': 'probe'}]},
     _tools_with_probe({}, {'o': {'type': 'File', **_MYNS_FORMAT, 'outputBinding': {'glob': 'o'}}}, {'myns': _MYNS}),
     {'outputs': {'probe~o': _MYNS_FORMAT}, '$namespaces': {'myns': _MYNS}}),
    ({'$namespaces': {'myns': _OTHER_URI}, 'inputs': {'x': {'type': 'File', **_MYNS_FORMAT}},
      'steps': [{'id': 'probe', 'in': {'f': 'x'}}]},
     _tools_binding_myns(probe=_MYNS), {'inputs': {'x': _MYNS_FORMAT}, '$namespaces': {'myns': _OTHER_URI}}),
], ids=['input', 'prefix-alike-and-edam', 'output', 'authored'])
def test_a_tools_namespace_prefix_is_declared_by_the_document_that_promotes_its_port(
        workflow: Yaml, tools: Tools, expected: Yaml) -> None:
    """A promoted port keeps its `myns:` CURIE, so the document that promotes it declares `myns` (the author's own
    binding wins over any tool's); tools agreeing on a prefix are no conflict, and `edam` is bound by Emit alone."""
    assert match(expected, compile_hermetic_cwl(workflow, tools=tools))


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
    child = next(artifact for artifact in info.artifact.children if artifact.namespace == ('child',))
    assert child.cwl['inputs']['probe~f']['format'] == 'myns:format_1'
    assert child.cwl['$namespaces']['myns'] == _MYNS


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
    assert compiled.get('$namespaces', {}) == declared
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
    expected = {'myns': _MYNS, **declared}
    assert beside['$namespaces'] == expected
    assert beneath['$namespaces'] == expected


@pytest.mark.fast
def test_a_cwl_record_emits_the_fields_it_carries() -> None:
    """Two sources merged into `sink.extras` (File[]), a default on `n`, and the
    requirements CWL demands for each. Between them the records carry every
    WorkflowStepInput field but `id`."""
    carried = {'label': 'both files', 'loadContents': True, 'loadListing': 'no_listing',
               'pickValue': 'all_non_null'}
    compiled = compile_hermetic_cwl({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'fa'}}]},
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'b'}}, 'out': [{'file': {'wic_anchor': 'fb'}}]},
        {'id': 'sink', 'in': {
            'file': {'wic_alias': 'fa'},
            'n': {'wic_raw_cwl': {'default': 3, 'valueFrom': '$(self + 1)'}},
            'extras': {'wic_raw_cwl': {'source': [{'wic_alias': 'fa'}, {'wic_alias': 'fb'}],
                                       'linkMerge': 'merge_flattened', **carried}}}}]})
    sink = compiled['steps'][2]
    assert sink['in']['extras'] == {'source': ['mk_file@1/file', 'mk_file@2/file'],
                                    'linkMerge': 'merge_flattened', **carried}
    assert sink['in']['n'] == {'default': 3, 'valueFrom': '$(self + 1)'}
    assert {'MultipleInputFeatureRequirement', 'StepInputExpressionRequirement',
            'InlineJavascriptRequirement'} <= set(compiled['requirements'])


@pytest.mark.fast
def test_a_record_with_one_source_and_link_merge_keeps_its_source_a_list() -> None:
    """CWL merges a list beside `linkMerge`; flattening `[x]` to `x` would change the value."""
    compiled = compile_hermetic_cwl({'inputs': {'f': 'File'}, 'steps': [
        {'id': 'sink', 'in': {'file': 'f', 'n': {'wic_inline_input': 1},
                              'extras': {'wic_raw_cwl': {'source': ['f'], 'linkMerge': 'merge_nested'}}}}]})
    assert compiled['steps'][0]['in']['extras'] == {'source': ['f'], 'linkMerge': 'merge_nested'}
    assert 'MultipleInputFeatureRequirement' in compiled['requirements']
    assert 'StepInputExpressionRequirement' not in compiled['requirements']


@pytest.mark.fast
def test_a_record_may_bind_an_input_the_process_does_not_declare_for_when(
        capsys: pytest.CaptureFixture[str]) -> None:
    """`go` is bound only so `when:` can read it; the step declares it, so no stderr line names it. Bound plainly,
    the same undeclared input is wic028."""
    workflow: Yaml = {'inputs': {'go': 'boolean'}, 'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}, 'go': {'wic_raw_cwl': {'source': 'go'}}},
         'when': '$(inputs.go)'}]}
    compiled = compile_hermetic_cwl(workflow)
    assert compiled['steps'][0]['in']['go'] == {'source': 'go'}
    assert compiled['steps'][0]['when'] == '$(inputs.go)'
    assert 'Warning!' not in capsys.readouterr().err
    workflow['steps'][0]['in']['go'] = 'go'
    with pytest.raises(SophiosError) as caught:
        compile_hermetic(workflow)
    assert SophiosErrorCode.UNDECLARED_PORT in {d.code for d in caught.value.diagnostics}


@pytest.mark.fast
def test_a_record_source_from_an_includer_is_discharged_like_a_bare_reference() -> None:
    """The child's record names the boundary input Link relays the parent's edge through."""
    child = {'steps': [{'id': 'sink', 'in': {'file': {'wic_raw_cwl': {'source': {'wic_alias': 'f'}}},
                                             'n': {'wic_inline_input': 1}}}]}
    compiled = compile_hermetic({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'f'}}]},
        subworkflow_step('child.wic', child)]})
    (edge,) = compiled.graph.linked_edges
    assert edge.sink.step.name == 'sink'
    workflow = next(child.cwl for child in compiled.artifact.children if child.cwl['class'] == 'Workflow')
    (inner,) = workflow['steps']
    assert inner['in']['file'] == {'source': 'sink~file'}


@pytest.mark.fast
@pytest.mark.parametrize('body', [{'source': 'w', 'nope': 1}, {'source': {'wic_inline_input': 3}}, {'source': 3}])
def test_a_desugared_record_under_wic_steps_is_checked_like_the_tagged_one(body: dict[str, Any]) -> None:
    """`{wic_raw_cwl: {...}}` contributed through `wic: steps:` is the record
    `!cwl {...}` spells, so a field it may not carry is refused, not dropped."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'inputs': {'w': 'File'}, 'steps': [{'id': 'count'}],
                          'wic': {'steps': {'(1, count)': {'in': {'file': {'wic_raw_cwl': body}}}}}})
    assert [d.code for d in caught.value.diagnostics] == [SophiosErrorCode.STEP_INPUT_RECORD]


@pytest.mark.fast
def test_a_desugared_record_under_wic_steps_binds_like_the_tagged_one() -> None:
    """The same record under `wic: steps:` reaches the emitted step as it is written."""
    record = {'source': 'w', 'loadContents': True}
    compiled = compile_hermetic_cwl({'inputs': {'w': 'File'}, 'steps': [{'id': 'count'}],
                                     'wic': {'steps': {'(1, count)': {'in': {'file': {'wic_raw_cwl': record}}}}}})
    assert compiled['steps'][0]['in']['file'] == record


@pytest.mark.fast
def test_emitted_inputs_list_authored_names_first_then_derived_in_step_order() -> None:
    """Authored inputs keep the order written and come before the inputs
    exposing a step's port, which follow the steps. `mk_file.name` is lifted by
    inference, after `mk_text`'s literal already exposed its own input."""
    compiled = compile_hermetic_cwl({
        'inputs': {'zeta': 'string', 'alpha': 'string'},
        'steps': [{'id': 'mk_file'},
                  {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'a'}}}]})
    assert list(compiled['inputs']) == ['zeta', 'alpha',
                                        'mk_file~name', 'mk_text~name']


@pytest.mark.fast
def test_a_workflows_schemas_are_what_its_author_wrote() -> None:
    """A document depends on the ontologies its author named, and on none of Sophios's choosing."""
    mine = 'https://example.org/ontologies/mine.owl'
    authored = compile_hermetic({'$schemas': [mine], 'steps': [{'id': 'mk_file'}]}).artifact.cwl
    assert authored['$schemas'] == [mine]
    assert '$schemas' not in compile_hermetic({'steps': [{'id': 'mk_file'}]}).artifact.cwl
