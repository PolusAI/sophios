"""Canonical CWL emission, judged on drawn workflows by the laws every compile keeps and on documents stated by hand.
Generated workflows inherit ast_strategies.workflows's exclusions; the hand-stated anchors are what a law cannot see:
the order of a record's sources, the fields it carries, a graph built without the compiler."""
import builtins
import copy
import json
import re
from pathlib import Path
import tempfile
from typing import Any, Final
from unittest.mock import patch

import pytest
from cwl_utils.parser import cwl_v1_2
from hypothesis import assume, example, given, strategies as st
from schema_salad.utils import yaml_no_ts  # pylint: disable=no-name-in-module

from sophios import compute, post_compile
from sophios.input_output import dump_wic_yaml
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.complete import coerce_job_value, complete
from sophios.ir.declarations import port_declaration
from sophios.ir.emit import emit, emit_job_inputs, surface
from sophios.ir.infer import infer
from sophios.ir.link import link
from sophios.ir.names import Names
from sophios.ir.pipeline import front_end
from sophios.ir.resolve import RegistrySnapshot, resolve
from sophios.ir.types import (AuthoredName, BoundaryDeclaration, Direction, EdgeOrigin, JobBinding, Namespace, Port,
                              PortDeclaration, PortId, PortType, RegistryKey, StepId, StepNode, StepOutputRef,
                              WorkflowGraph, WorkflowPort)
from sophios.lang.cwl import CWL_VERSION
from sophios.lang.diagnostics import SophiosError
from sophios.wic_types import StepId as LegacyStepId, Yaml

from . import ast_strategies as strat
from .cwl_validation import validate_cwl
from .equivalence import Strength, equivalent, flatten_model
from .hermetic import (COVERAGE, ORACLE, bundle, compile_hermetic, compile_hermetic_cwl, network_refused,
                       subworkflow_step)
from .reference_model import inference_choice
from .strategies import cwl_types, pinned
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt, inputs_of, outputs_of, registered


def _render(graph: WorkflowGraph) -> Any:
    """`graph` emitted as the compiler emits its root document."""
    names = Names.of(graph)
    return emit(surface(graph, names), names)


def _graphs(graph: WorkflowGraph) -> tuple[WorkflowGraph, ...]:
    return (graph,) + tuple(item for child in graph.children for item in _graphs(child))


def _resolves_from_the_registry_alone(document: Any) -> None:
    """Every non-generated process is the tool the registry holds under the name its step authored, run path and both
    interfaces, recursively through child workflows: not the key the resolver returned, which a resolver answering
    every lookup with one tool would report too."""
    for step in document.steps:
        process = step.process
        if process.child is not None:
            _resolves_from_the_registry_alone(process.child)
        elif not process.generated:
            stem = step.source.id
            assert process.key.name == stem
            assert process.run_path == SYNTHETIC_TOOLS[LegacyStepId(stem, SYNTHETIC_NS)].run_path
            assert tuple(port.name for port in process.inputs) == tuple(inputs_of(stem))
            assert tuple(port.name for port in process.outputs) == tuple(outputs_of(stem))


def _documents(node: CompilationArtifact) -> list[Any]:
    return [node.cwl, node.job_inputs, *(document for child in node.children for document in _documents(child))]


def _addresses(node: CompilationArtifact) -> list[str]:
    """Each step that runs a tool, beneath `node` at any depth, by its address: the calls above it and its name."""
    calls = {child.namespace[-1]: child for child in node.children if child.graph is not None}
    return [address for step in node.cwl['steps'] for name in [step['id']]
            for address in ([f'{name}~{inner}' for inner in _addresses(calls[name])] if name in calls else [name])]


#: `child.wic` (mk_file, then xform twice) called from a root that counts its file.
_NESTED: Final[Yaml] = {'steps': [subworkflow_step('child.wic', {'steps': [{'id': 'mk_file'}, {'id': 'xform'},
                                                                           {'id': 'xform'}]}), {'id': 'count'}]}


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(lambda workflow: '___' not in json.dumps(workflow)))
@example({'steps': [{'id': 'join'}]})  # a resolver returning empty interfaces cannot satisfy this by accident
@example(_NESTED)
@ORACLE
def test_a_drawn_workflow_compiles_by_every_law(workflow: Yaml) -> None:  # pylint: disable=too-many-locals
    """The laws one compile keeps, each over the same drawn workflow:

    - resolution reads the registry and nothing else, the filesystem refused, and its order changes nothing;
    - every edge inference adds is the independent model's choice (reference_model.inference_choice);
    - the phases composed directly emit what the live compiler does, up to embedding;
    - a graph, not its mutable source, determines every byte: the source is emptied between two emissions;
    - every compiled workflow makes a request the compute service's schema takes (nothing is sent);
    - no form of the compiled CWL, nested, embedded, flat or both, spells a name of its own: no `__step__`, `.wic`,
      `___`, hash or `sophios` key; flattened, a step of a call is named by its address from the root;
    - flattening dissolves every call to the DAG the independent model (equivalence.flatten_model) does, and keeps
      the root's interface and job.
    """
    model = bundle(copy.deepcopy(workflow), 'oracle', SYNTHETIC_TOOLS)
    assert model.parsed.document is not None

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError('resolution attempted filesystem access')
    with patch.object(builtins, 'open', forbidden), patch.object(Path, 'read_text', forbidden):
        resolved = resolve(model.parsed.document, model.registry, name='oracle')
    assert resolved.document is not None, list(resolved.diagnostics)
    _resolves_from_the_registry_alone(resolved.document)
    backwards = RegistrySnapshot(tuple(reversed(model.registry.tools)), tuple(reversed(model.registry.workflows)))
    assert resolve(model.parsed.document, backwards, name='oracle').document == resolved.document

    front = front_end(model.parsed, model.registry, name='oracle')
    assert front.graph is not None, list(front.diagnostics)
    linked = link(complete(front.graph))
    assert linked.graph is not None, list(linked.diagnostics)
    inferred = infer(linked.graph)
    assert inferred.graph is not None, list(inferred.diagnostics)
    for graph in _graphs(inferred.graph):
        original = next(item for item in _graphs(linked.graph) if item.namespace == graph.namespace)
        for edge in (edge for edge in graph.linked_edges if edge.origin is EdgeOrigin.INFERRED):
            position = next(index for index, step in enumerate(original.steps) if step.id == edge.sink.step)
            sink = next(port for port in original.steps[position].inputs if port.id.port == edge.sink.port)
            assert inference_choice(original.steps, position, sink) == edge.source

    source = copy.deepcopy(workflow)
    compiled = compile_hermetic(source)
    nested = compiled.artifact
    assert equivalent(_render(inferred.graph), nested.cwl, Strength.UP_TO_EMBEDDING) is None
    names = Names.of(compiled.graph)
    document = surface(compiled.graph, names)
    first = emit(document, names)
    source.clear()
    assert equivalent(first, emit(document, names), Strength.IDENTICAL) is None

    request = compute.request_for('oracle', nested.cwl, nested.job_inputs)
    assert (request['workflow'], request['inputs']) == (nested.cwl, nested.job_inputs)
    assert json.loads(json.dumps(request)) == request

    flat = post_compile.flatten_subworkflows(nested)
    for form in (nested, flat, post_compile.inline_artifact_runs(nested), post_compile.inline_artifact_runs(flat)):
        found = [hit for each in _documents(form)
                 for hit in re.findall(r'__step__|\.wic|___|_[0-9a-f]{8}\b|"sophios[^"]*":', json.dumps(each))]
        assert not found, found
    assert [step['id'] for step in flat.cwl['steps']] == _addresses(nested)
    assert equivalent(flatten_model(nested), flatten_model(flat), Strength.UP_TO_RENAMING) is None
    assert all(child.cwl['class'] != 'Workflow' for child in flat.children)
    assert (list(flat.cwl['inputs']), list(flat.cwl['outputs']), flat.job_inputs) == \
        (list(nested.cwl['inputs']), list(nested.cwl['outputs']), nested.job_inputs)


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
        run=RegistryKey('global', 'write'),
    )
    graph = WorkflowGraph(
        namespace, (step,), name='handmade', lang_version='0.0.1',
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
#: draws them. Each is an xfail example of it, so the exclusion cannot outlive the gap.
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
    inlined = post_compile.inline_artifact_runs(info.artifact).cwl
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
@example({'steps': [{'id': 'schemed', 'in': {'coords': {'wic_inline_input': {'x': 1.0, 'y': 2.0}}}}]}).xfail(
    raises=AssertionError, reason=NOT_YET_VALID['schemed'])
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


_TYPES = cwl_types('null', 'string', 'int', 'float', 'boolean', 'File', 'Directory')
_OBJECTS = st.fixed_dictionaries(
    {'class': st.sampled_from(['File', 'Directory']), 'location': st.text(max_size=2)},
    optional={'format': st.just('edam:format_2')})
_VALUES = st.recursive(
    # NaN is excluded: `nan != nan`, so no value holding it equals itself.
    st.one_of(st.text(max_size=2), st.integers(), st.booleans(), st.floats(allow_nan=False),
              st.sampled_from([float('inf'), float('-inf')]), _OBJECTS),
    lambda inner: st.lists(inner, max_size=2), max_leaves=4)


@pytest.mark.skip_pypi_ci
@given(_TYPES, st.sampled_from([{}, {'format': 'edam:format_1'}]), _VALUES)
@COVERAGE
def test_a_job_value_is_its_own_normal_form(raw: Any, fmt: Yaml, value: Any) -> None:
    """Coercing a coerced job value changes nothing, which is what lets a
    child's job value be lifted a level and coerced against the outer port."""
    declaration = port_declaration({'type': raw, **fmt})
    name = AuthoredName('x')
    try:
        once = coerce_job_value(name, declaration, value)
    except SophiosError:
        return
    assert coerce_job_value(name, declaration, once) == once


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
    compiled = compile_hermetic_cwl({'steps': [{'id': 'probe'}]}, tools=registered({'probe': clt(
        {**inputs, 'plain': {**_PLAIN, 'inputBinding': {'position': 2}}},
        {**outputs, 'plain': {**_PLAIN, 'outputBinding': {'glob': 'p'}}})}))
    assert 'InlineJavascriptRequirement' not in (compiled.get('requirements') or {})
    return compiled


_INPUTS: Final = st.fixed_dictionaries({'type': st.just('File'), 'inputBinding': st.just({'position': 1})}, optional={
    **_PORT_FIELDS, 'loadContents': st.booleans(),
    'loadListing': st.sampled_from(['no_listing', 'shallow_listing', 'deep_listing'])})
_OUTPUTS: Final = st.fixed_dictionaries({'type': st.just('File'), 'outputBinding': st.just({'glob': 'o.bam'})},
                                        optional=_PORT_FIELDS)


@pytest.mark.fast
@given(st.tuples(_INPUTS, _OUTPUTS))
@pinned(({'type': 'File', 'secondaryFiles': ['.idx'], 'streamable': True, 'loadContents': True,
          'loadListing': 'shallow_listing', 'inputBinding': {'position': 1}},
         {'type': 'File', 'secondaryFiles': ['.bai'], 'streamable': True, 'outputBinding': {'glob': 'o.bam'}}),
        *(({'type': 'File', 'secondaryFiles': declared, 'inputBinding': {'position': 1}},
           {'type': 'File', 'secondaryFiles': declared, 'outputBinding': {'glob': 'o'}})
          for declared in _SECONDARY_CASES))
@ORACLE
def test_a_promoted_port_keeps_the_fields_a_workflow_port_may_state(ports: tuple[Yaml, Yaml]) -> None:
    """A tool port promoted to the boundary keeps what `WorkflowInputParameter` or `WorkflowOutputParameter` allows,
    not its binding, and of its `secondaryFiles` the static entries, as a list."""
    port_in, port_out = ports
    compiled = _boundary({'f': port_in}, {'o': port_out})
    assert compiled['inputs'] == {'probe~f': _promoted(port_in, cwl_v1_2.WorkflowInputParameter),
                                  'probe~plain': _PLAIN}
    assert compiled['outputs'] == {
        'probe~o': {**_promoted(port_out, cwl_v1_2.WorkflowOutputParameter), 'outputSource': 'probe/o'},
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
