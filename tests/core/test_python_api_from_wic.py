"""`Workflow.from_wic`: a `.wic` file as Python objects.

The unit tests build small documents beside each other in `tmp_path`, over the
synthetic tools, and say what each construct becomes, or why it is refused.
Then two properties over every workflow the corpus holds, and the in-repo table:

  * **From the file.** A `.wic` the file door compiles is either refused with
    `api006` and nothing else, or its objects compile to the CWL the file
    compiles to, with equal job inputs and diagnostic codes. Equal up to key
    order and CWL's shorthand for a port type: the Python API writes a step's
    `in:` in its tool's port order and a port's type in CWL's long form.
  * **Round trip.** The bundle `write_wic` writes for those objects compiles,
    through the file door, to exactly what the objects compile to, and reading
    that bundle back gives the same compilation again.
"""
# pylint: disable=redefined-outer-name  # `corpus_registry` is a pytest fixture
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

import sophios.compiler
import sophios.plugins
from sophios.api.python import _workflow_runtime
from sophios.api.python.workflow import SophiosError, SophiosErrorCode, Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.artifacts import CompilationResult
from sophios.lang import LANG_VERSION
from sophios.post_compile import inline_artifact_runs
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.utils_cwl import canonicalize_type
from sophios.utils_graphs import get_graph_reps
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Json, StepId, Tools

from .equivalence import Strength, equivalent
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS
from .test_examples import _is_includer_fragment, yml_paths_tuples_not_large
from .test_frontdoor import PYTHON_SCRIPT
# pylint: disable-next=unused-import  # `corpus_registry` is a pytest fixture
from .test_setup import CorpusRegistry, corpus_registry

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

WorkflowPaths = dict[str, dict[str, Path]]


def _canonical_ports(cwl: Any) -> Any:
    """`cwl` with every Workflow document's port declarations in CWL's long form: a bare `T` as
    `{type: T}`, and every type through `canonicalize_type` (`T?` and `T[]` are CWL's shorthand);
    and a step's `scatter: p` as `scatter: [p]`, CWL's shorthand for the one-port list."""
    match cwl:
        case {'class': 'Workflow'}:
            canonical = {key: _canonical_ports(value) for key, value in cwl.items()}
            for kind in ('inputs', 'outputs'):
                ports = cwl.get(kind)
                if isinstance(ports, dict):
                    canonical[kind] = {name: _long_form(spec) for name, spec in ports.items()}
            return canonical
        case {'run': _, 'scatter': str() as port}:
            return {**{key: _canonical_ports(value) for key, value in cwl.items()}, 'scatter': [port]}
        case dict():
            return {key: _canonical_ports(value) for key, value in cwl.items()}
        case list():
            return [_canonical_ports(item) for item in cwl]
        case _:
            return cwl


def _long_form(spec: Any) -> Any:
    declared = dict(spec) if isinstance(spec, dict) else {'type': spec}
    if 'type' in declared:
        declared['type'] = canonicalize_type(declared['type'])
    return declared


def _compile_file(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> CompilationResult:
    """`path` compiled as the CLI reads it, with the settings `Workflow.compile()` uses."""
    compiler_options, graph_settings, yaml_tag_paths = default_compilation_settings()
    return sophios.compiler.compile_source(
        bundle_from_disk(path, workflow_paths, tools), compiler_options, graph_settings, yaml_tag_paths,
        relative_run_path=True, testing=False, graph_target=get_graph_reps(path.stem))


def _file_door(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostic codes of `path` compiled as the CLI reads it, with the settings
    `Workflow.compile()` uses; outputs narrowed to the named ones as `compile()` returns them."""
    result = _compile_file(path, workflow_paths, tools)
    artifact = inline_artifact_runs(result.artifact)
    cwl = normalize_artifact_cwl(artifact)
    named = _named_outputs(path)
    if named and isinstance(cwl.get('outputs'), dict):
        cwl['outputs'] = {name: spec for name, spec in cwl['outputs'].items() if name in named}
    return (cwl, normalize_artifact_job_inputs(artifact, artifact.job_inputs),
            [diagnostic.code.value for diagnostic in result.diagnostics])


def _named_outputs(path: Path) -> list[str]:
    """The outputs `path` declares, which `Workflow.compile()` narrows its CWL to."""
    document = yaml.load(path.read_text(encoding='utf-8'), Loader=wic_loader())
    outputs = document.get('outputs') if isinstance(document, dict) else None
    return list(outputs) if isinstance(outputs, dict) else []


def _objects(workflow: Workflow) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostic codes of `workflow` compiled from its objects."""
    result = _workflow_runtime.compile_workflow_result(workflow)
    compiled = _workflow_runtime.compiled_workflow_from_result(workflow, result)
    return (compiled.cwl_workflow, compiled.cwl_job_inputs,
            [diagnostic.code.value for diagnostic in result.diagnostics])


# --------------------------------------------------------------------------
# What each construct becomes
# --------------------------------------------------------------------------

#: The synthetic tools, with `mk_file` also in a `gpu` plugin namespace.
TOOLS: Final[Tools] = {**SYNTHETIC_TOOLS, StepId('mk_file', 'gpu'): SYNTHETIC_TOOLS[StepId('mk_file', SYNTHETIC_NS)]}


def _documents(directory: Path, **documents: str) -> WorkflowPaths:
    """Write each document as `<name>.wic` in `directory`; the paths a step may call them by."""
    for name, text in documents.items():
        (directory / f'{name}.wic').write_text(text, encoding='utf-8')
    return {'global': {path.stem: path for path in directory.glob('*.wic')}}


def _from_wic(directory: Path, **documents: str) -> Workflow:
    """`Workflow.from_wic` of `root.wic`, written with `documents` into `directory`."""
    workflow_paths = _documents(directory, **documents)
    return Workflow.from_wic(directory / 'root.wic', tool_registry=TOOLS, workflow_paths=workflow_paths)


def _assert_compiles_alike(directory: Path, workflow: Workflow) -> None:
    """Property 1 for `root.wic` in `directory` and the objects read from it."""
    cwl, job_inputs, codes = _file_door(directory / 'root.wic', _documents(directory), TOOLS)
    python_cwl, python_job_inputs, python_codes = _objects(workflow)
    found = equivalent(_canonical_ports(cwl), _canonical_ports(python_cwl), Strength.UP_TO_ORDER)
    assert found is None, str(found)
    assert (python_job_inputs, python_codes) == (job_inputs, codes)


CHAIN: Final = ('inputs:\n  name: string\n'
                'steps:\n- id: mk_file\n  in:\n    name: name\n  out:\n  - file: !& made\n'
                '- id: xform\n  in:\n    file: !* made\n    name: !ii renamed.txt\n'
                'outputs:\n  result:\n    type: File\n    outputSource: xform/file\n')


@pytest.mark.fast
def test_each_step_becomes_a_step_and_each_binding_an_object(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare name is the workflow input, `!*` the producing step's output object and `!ii` a
    literal; the workflow output is the step output it names. The objects compile as the file
    does, and reading the file writes nothing."""
    monkeypatch.chdir(tmp_path)
    workflow = _from_wic(tmp_path, root=CHAIN)
    assert [path.name for path in tmp_path.iterdir()] == ['root.wic']
    mk_file, xform = workflow.steps
    assert isinstance(mk_file, Step) and isinstance(xform, Step)
    assert (workflow.process_name, mk_file.process_name, xform.process_name) == ('root', 'mk_file', 'xform')
    assert mk_file.inputs.name.linked and mk_file.inputs.name.value == 'name'
    (source,) = xform.inputs.file.source_outputs()
    assert source is mk_file.outputs.file
    assert not xform.inputs.name.linked and xform.inputs.name.value == 'renamed.txt'
    assert workflow.inputs[0].cwl_type() == 'string'
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_a_wic_call_becomes_a_nested_workflow(tmp_path: Path) -> None:
    """`child.wic` becomes a `Workflow` named `child`, built from its own file, its input bound by the caller."""
    workflow = _from_wic(tmp_path, root='steps:\n- id: child.wic\n  in:\n    n: !ii made.txt\n',
                         child='inputs:\n  n: string\nsteps:\n- id: mk_file\n  in:\n    name: n\n')
    (child,) = workflow.steps
    assert isinstance(child, Workflow) and child.process_name == 'child'
    assert [step.process_name for step in child.steps] == ['mk_file']
    assert child.inputs[0].value == 'made.txt'
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_an_input_left_to_inference_stays_unbound_and_compile_infers_it(tmp_path: Path) -> None:
    """`count` binds nothing; the objects leave its `file` unbound and the compile infers it as the file's does."""
    workflow = _from_wic(tmp_path, root='steps:\n- id: mk_file\n  in:\n    name: !ii a.txt\n- id: count\n')
    count = workflow.steps[1]
    assert isinstance(count, Step) and not count.inputs.file.is_bound()
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_an_inline_run_body_and_a_scatter_and_a_condition_are_kept(tmp_path: Path) -> None:
    """A step named apart from its inline tool holds that tool; `scatter`, its method and `when` carry over."""
    root = ('steps:\n- id: shout\n  run:\n    class: CommandLineTool\n    baseCommand: echo\n'
            '    inputs:\n      message:\n        type: string\n        inputBinding:\n          position: 1\n'
            '    outputs: {}\n'
            '  in:\n    message: !ii [a, b]\n  scatter: [message]\n  scatterMethod: dotproduct\n'
            '  when: $(inputs.message != "c")\n')
    workflow = _from_wic(tmp_path, root=root)
    (shout,) = workflow.steps
    assert isinstance(shout, Step) and shout.process_name == 'shout'
    assert shout.yaml['baseCommand'] == 'echo'
    assert [port.name for port in shout.scatter] == ['message'] and shout.scatterMethod == 'dotproduct'
    assert shout.when == '$(inputs.message != "c")'
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_a_scatter_over_an_array_workflow_input_is_kept(tmp_path: Path) -> None:
    """A `string[]` workflow input feeds the `string` port its step scatters over, as in the file."""
    root = ('inputs:\n  names: string[]\n'
            'steps:\n- id: mk_file\n  scatter: name\n  scatterMethod: dotproduct\n  in:\n    name: names\n')
    workflow = _from_wic(tmp_path, root=root)
    (mk_file,) = workflow.steps
    assert isinstance(mk_file, Step) and [port.name for port in mk_file.scatter] == ['name']
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_an_input_declared_with_an_empty_mapping_is_left_untyped(tmp_path: Path) -> None:
    """`name: {}` declares no type, as a null spec does: the call builds the workflow instead of raising."""
    root = 'inputs:\n  name: {}\nsteps:\n- id: mk_file\n  in:\n    name: name\n'
    workflow = _from_wic(tmp_path, root=root)
    assert [port.name for port in workflow.inputs] == ['name']


def _calling_child(name: str) -> str:
    """A document that calls `child.wic` and binds its `mk_file` step's `name` to `!ii {name}`."""
    return ('wic:\n  steps:\n    (1, child.wic):\n      wic:\n        steps:\n          (1, mk_file):\n'
            f'            in:\n              name: !ii {name}\nsteps:\n- id: child.wic\n')


@pytest.mark.fast
def test_one_file_called_twice_with_the_same_contributions_is_one_nested_workflow(tmp_path: Path) -> None:
    """`left` and `right` both call `child.wic` with the same `wic: steps:` body, so both calls build it alike."""
    workflow = _from_wic(tmp_path, root='steps:\n- id: left.wic\n- id: right.wic\n',
                         left=_calling_child('same.txt'), right=_calling_child('same.txt'),
                         child='steps:\n- id: mk_file\n')
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_a_record_becomes_a_step_input(tmp_path: Path) -> None:
    """`!cwl {source: [...], linkMerge: ...}` is a `StepInput` over the two output objects; its
    `linkMerge` reaches the CWL, as the file's does."""
    root = ('steps:\n- id: mk_file\n  in:\n    name: !ii a\n  out:\n  - file: !& f\n'
            '- id: xform\n  in:\n    file: !* f\n    name: !ii b\n  out:\n  - file: !& g\n'
            '- id: sink\n  in:\n    file: !* f\n    n: !ii 1\n'
            '    extras: !cwl {source: [!* f, !* g], linkMerge: merge_flattened}\n')
    workflow = _from_wic(tmp_path, root=root)
    mk_file, xform, sink = workflow.steps
    assert isinstance(sink, Step)
    assert sink.inputs.extras.record_sources() == (mk_file.outputs.file, xform.outputs.file)
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
@pytest.mark.parametrize('output_source', ['(2, xform)/file', 'root__step__2__xform/file'])
def test_positional_and_generated_output_sources_become_object_references(
        output_source: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A position or a generated step name is read as Lower reads it; the objects say `xform/file`,
    so the written bundle names the authored step and its compile warns about no spelling."""
    root = ('steps:\n- id: mk_file\n  in:\n    name: !ii a\n  out:\n  - file: !& f\n'
            '- id: xform\n  in:\n    file: !* f\n    name: !ii b\n'
            f'outputs:\n  o:\n    type: File\n    outputSource: {output_source}\n')
    workflow = _from_wic(tmp_path, root=root)
    written = workflow.write_wic(tmp_path / 'written')
    document = yaml.load(written.read_text(encoding='utf-8'), Loader=wic_loader())
    assert document['outputs']['o']['outputSource'] == 'xform/file'
    capsys.readouterr()
    workflow.compile()
    captured = capsys.readouterr()
    assert 'Warning!' not in captured.out + captured.err


@pytest.mark.fast
def test_a_note_from_a_loaded_workflow_names_the_wic_line(tmp_path: Path) -> None:
    """Objects carry the `.wic` line they were read from, so the compile of the objects names
    that line, as the compile of the file does: `count`, on line 8, inferred by recency."""
    root = 'steps:\n- id: mk_file\n  in:\n    name: !ii a\n- id: xform\n  in:\n    name: !ii b\n- id: count\n'
    workflow = _from_wic(tmp_path, root=root)
    (note,) = workflow.compile().diagnostics
    assert note.startswith('root.wic:8:3: note [wic043]'), note


# --------------------------------------------------------------------------
# What is refused, dropped, or raised as the file's own error
# --------------------------------------------------------------------------

ONE_STEP: Final = 'steps:\n- id: mk_file\n  in:\n    name: !ii a\n'

#: One document per construct the Python API cannot hold: the files, the file the
#: refusal names, and a fragment of what it says.
REFUSALS: Final[dict[str, tuple[dict[str, str], str, str]]] = {
    'repeated step id': ({'root': ONE_STEP + '- id: mk_file\n  in:\n    name: !ii b\n'},
                         'root', "steps 1 and 2 are both called 'mk_file'"),
    'edge from another document': (
        {'root': ONE_STEP + '  out:\n  - file: !& f\n- id: child.wic\n',
         'child': 'steps:\n- id: xform\n  in:\n    file: !* f\n    name: !ii b\n'},
        'child', 'reads !* f, which no earlier step of this document defines'),
    'top-level hints': ({'root': 'hints:\n  ResourceRequirement:\n    coresMin: 1\n' + ONE_STEP},
                        'root', "a Python Workflow carries no top-level 'hints'"),
    'input default': ({'root': 'inputs:\n  n:\n    type: string\n    default: a\n'
                               'steps:\n- id: mk_file\n  in:\n    name: n\n'},
                      'root', 'inputs: n: carries default'),
    'port ref-t': ({'root': 'inputs:\n  ref-t: string\nsteps:\n- id: mk_file\n  in:\n    name: ref-t\n'},
                   'root', "no Python port can be called 'ref-t'"),
    'step label': ({'root': 'steps:\n- id: mk_file\n  label: makes\n  in:\n    name: !ii a\n'},
                   'root', "a Python Step carries no 'label'"),
    'scatter without method': ({'root': 'steps:\n- id: mk_file\n  scatter: name\n  in:\n    name: !ii [a, b]\n'},
                               'root', 'add scatterMethod: dotproduct'),
    'scatter on a call': (
        {'root': 'steps:\n- id: child.wic\n  scatter: [n]\n  scatterMethod: dotproduct\n  in:\n    n: !ii [a, b]\n',
         'child': 'inputs:\n  n: string\nsteps:\n- id: mk_file\n  in:\n    name: n\n'},
        'root', 'calls a workflow with scatter:, scatterMethod:'),
    'run names a workflow': ({'root': 'steps:\n- id: sub\n  run: child.wic\n', 'child': ONE_STEP},
                             'root', 'write the step as id: child.wic'),
    'one file, two bodies': ({'root': 'steps:\n- id: left.wic\n- id: right.wic\n',
                              'left': _calling_child('from_left'), 'right': _calling_child('from_right'),
                              'child': 'steps:\n- id: mk_file\n'},
                             'right', 'child.wic is called here and at left.wic:'),
    'raw cwl source': ({'root': 'steps:\n- id: mk_file\n  in:\n    name: !cwl a/b\n'},
                       'root', 'never as CWL text'),
    'plugin namespace': ({'root': 'wic:\n  steps:\n    (1, mk_file):\n      wic:\n        namespace: gpu\n' + ONE_STEP},
                         'root', "namespace: 'gpu' has no Python spelling"),
    'implementations': ({'root': 'wic:\n  default_implementation: one\n  implementations:\n    one:\n'
                                 '      steps:\n      - id: mk_file\n        in:\n          name: !ii a\n'},
                        'root', 'a Python Workflow has one body'),
    'language version pin': ({'root': f'wic:\n  lang_version: "{LANG_VERSION}"\n' + ONE_STEP},
                             'root', f"compile(lang_version='{LANG_VERSION}')"),
}


@pytest.mark.fast
@pytest.mark.parametrize('construct', sorted(REFUSALS))
def test_each_construct_without_a_python_spelling_is_api006(construct: str, tmp_path: Path) -> None:
    """The document compiles; the Python API cannot hold it, and says where and what to write."""
    documents, file, fragment = REFUSALS[construct]
    with pytest.raises(SophiosError) as caught:
        _from_wic(tmp_path, **documents)
    (diagnostic,) = [item for item in caught.value.diagnostics if fragment in item.message]
    assert {item.code for item in caught.value.diagnostics} == {SophiosErrorCode.NO_PYTHON_SPELLING}
    assert diagnostic.span is not None and diagnostic.span.file == f'{file}.wic'


@pytest.mark.fast
def test_a_python_script_step_is_api006(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool generated from a script has no `Step`: the message says to build it with `tool_builder`.

    The script's generator imports `workflow_types` relative to the working directory, so the
    fixture supplies that sibling file, as `test_frontdoor` does.
    """
    types = tmp_path / 'sophios' / 'examples' / 'scripts'
    types.mkdir(parents=True)
    (types / 'workflow_types.py').write_text(
        "string = {'type': 'string', 'format': 'edam:format_2330'}\n", encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'my_script.py').write_text(PYTHON_SCRIPT, encoding='utf-8')
    monkeypatch.chdir(work)
    with pytest.raises(SophiosError) as caught:
        _from_wic(work, root='steps:\n- id: python_script\n  in:\n    script: !ii my_script.py\n'
                             '    name: !ii made.txt\n')
    assert [(item.code, 'tool_builder' in item.message) for item in caught.value.diagnostics] == [
        (SophiosErrorCode.NO_PYTHON_SPELLING, True)]


@pytest.mark.fast
def test_every_refusal_is_reported_at_once(tmp_path: Path) -> None:
    """Two constructs, two diagnostics, one raise."""
    with pytest.raises(SophiosError) as caught:
        _from_wic(tmp_path, root='hints:\n  ResourceRequirement:\n    coresMin: 1\n'
                                 + ONE_STEP + '- id: mk_file\n  in:\n    name: !ii b\n')
    assert len(caught.value.diagnostics) == 2


@pytest.mark.fast
def test_graphviz_and_inlineable_are_dropped_with_a_warning(tmp_path: Path) -> None:
    """Neither changes what a Python workflow compiles; the warning names the file and both keys."""
    root = 'wic:\n  graphviz:\n    label: drawn\n  inlineable: false\n' + ONE_STEP
    with pytest.warns(UserWarning, match='root.wic: wic: graphviz, inlineable dropped'):
        workflow = _from_wic(tmp_path, root=root)
    _assert_compiles_alike(tmp_path, workflow)


@pytest.mark.fast
def test_a_document_that_does_not_compile_raises_its_compile_error(tmp_path: Path) -> None:
    """The file's own error, the one `sophios --yaml` gives, and nothing built: `wic025`, not `api006`."""
    with pytest.raises(SophiosError) as caught:
        _from_wic(tmp_path, root='steps:\n- id: count\n  in:\n    file: !* nowhere\n')
    assert {item.code for item in caught.value.diagnostics} == {SophiosErrorCode.UNDEFINED_EDGE}


# --------------------------------------------------------------------------
# The two properties, over the corpus, and the in-repo table
# --------------------------------------------------------------------------


def _loaded(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> Workflow | None:
    """`Workflow.from_wic(path)`, or None when every construct it refused is `api006`."""
    try:
        return Workflow.from_wic(path, tool_registry=tools, workflow_paths=workflow_paths)
    except SophiosError as error:
        codes = {diagnostic.code for diagnostic in error.diagnostics}
        assert codes == {SophiosErrorCode.NO_PYTHON_SPELLING}, str(error)
        return None


@pytest.mark.fast
@pytest.mark.parametrize('yml_path_str, yml_path', yml_paths_tuples_not_large)
def test_a_wic_file_and_its_python_objects_compile_alike(yml_path_str: str, yml_path: Path,
                                                         corpus_registry: CorpusRegistry) -> None:
    """Property 1: refused with `api006` alone, or the same CWL, job inputs and diagnostic codes."""
    try:
        cwl, job_inputs, codes = _file_door(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise
    workflow = _loaded(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    if workflow is None:
        return
    python_cwl, python_job_inputs, python_codes = _objects(workflow)
    found = equivalent(_canonical_ports(cwl), _canonical_ports(python_cwl), Strength.UP_TO_ORDER)
    assert found is None, f'{yml_path_str}: the file and its Python objects compile apart\n{found}'
    assert python_job_inputs == job_inputs
    assert python_codes == codes


@pytest.mark.fast
@pytest.mark.parametrize('yml_path_str, yml_path', yml_paths_tuples_not_large)
def test_write_wic_of_a_loaded_workflow_compiles_identically(yml_path_str: str, yml_path: Path,
                                                             corpus_registry: CorpusRegistry,
                                                             tmp_path: Path) -> None:
    """Property 2: the bundle `write_wic` writes for the objects, through the file door, is the
    objects' own compilation, byte for byte; and reading that bundle back compiles the same."""
    try:
        _compile_file(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise
    workflow = _loaded(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    if workflow is None:
        return
    direct = workflow.compile()
    root = workflow.write_wic(tmp_path)
    written: WorkflowPaths = {'global': {path.stem: path for path in tmp_path.glob('*.wic')}}
    via_file = _workflow_runtime.compiled_workflow_from_result(
        workflow, _compile_file(root, written, corpus_registry.tools))
    assert equivalent(direct.cwl_workflow, via_file.cwl_workflow, Strength.IDENTICAL) is None
    assert equivalent(direct.cwl_job_inputs, via_file.cwl_job_inputs, Strength.IDENTICAL) is None

    reloaded = Workflow.from_wic(root, tool_registry=corpus_registry.tools, workflow_paths=written).compile()
    assert equivalent(direct.cwl_workflow, reloaded.cwl_workflow, Strength.IDENTICAL) is None
    assert equivalent(direct.cwl_job_inputs, reloaded.cwl_job_inputs, Strength.IDENTICAL) is None


#: The in-repo workflows the Python API cannot hold, each refused with `api006`:
#: six repeat a step id, `test_rand_fail` scatters a `.wic` call and reads an
#: edge across documents, and `secrets_echo` declares `$namespaces`, `hints` and
#: an input `default`.
REFUSED: Final = frozenset({'append_twice', 'multistep1', 'multistep2', 'multistep3', 'naming_conventions',
                            'naming_conventions_explicit', 'test_rand_fail', 'secrets_echo'})

#: The directories of `.wic` files shipped in this repository.
IN_REPO: Final = (REPO_ROOT / 'docs' / 'tutorials', REPO_ROOT / 'examples')


@lru_cache(maxsize=1)
def _in_repo_registry() -> tuple[WorkflowPaths, Tools]:
    """The `.wic` files this repository ships and the tools it ships, read from nothing else."""
    workflow_paths = {'global': {path.stem: path for directory in IN_REPO for path in directory.glob('*.wic')}}
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(REPO_ROOT / 'cwl_adapters')]}},
                                          quiet=True)
    return workflow_paths, tools


@pytest.mark.fast
def test_the_in_repo_workflows_that_python_cannot_hold_are_these() -> None:
    """Every `.wic` this repository ships compiles; those in `REFUSED` are `api006`, every other one builds."""
    workflow_paths, tools = _in_repo_registry()
    refused = {stem for stem, path in workflow_paths['global'].items()
               if _loaded(path, workflow_paths, tools) is None}
    assert refused == REFUSED
