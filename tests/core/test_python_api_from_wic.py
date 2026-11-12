"""`Workflow.from_wic`: a `.wic` file as Python objects. Examples say what a document becomes or why it is refused;
properties: a file the file door compiles is refused with `api006` alone or compiles to equal output, and `write_wic`
round-trips."""
# pylint: disable=redefined-outer-name  # `corpus_registry` is a pytest fixture
import tempfile
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Final, cast

import pytest
import yaml
from hypothesis import example, given, strategies as st

import sophios.compiler
from sophios.api.python import _workflow_runtime
from sophios.api.python.workflow import SophiosError, SophiosErrorCode, Workflow
from sophios.cli import default_compilation_settings
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.artifacts import CompilationResult
from sophios.post_compile import inline_artifact_runs
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.utils_cwl import canonicalize_type
from sophios.lang import Document, render
from sophios.wic_types import Json, StepId, Tool, Tools

from .ast_strategies import SUBWORKFLOW_BODIES, compilable_documents
from .budgets import budget
from .cases import case_dirs, diagnostic, match
from .equivalence import Strength, equivalent
from .helpers import ADAPTERS, REPO_ROOT, shipped_tools
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt
from .test_examples import _is_includer_fragment, yml_paths_tuples_not_large
from .test_frontdoor import PYTHON_SCRIPT
# pylint: disable-next=unused-import  # `corpus_registry` is a pytest fixture
from .test_setup import CorpusRegistry, corpus_registry
from .wic_reading import read_wic


WorkflowPaths = dict[str, dict[str, Path]]


def _canonical_ports(cwl: Any) -> Any:
    """`cwl` with every Workflow document's port declarations in CWL's long form: a bare `T` as
    `{type: T}`, and every type through `canonicalize_type` (`T?` and `T[]` are CWL's shorthand)."""
    match cwl:
        case {'class': 'Workflow'}:
            canonical = {key: _canonical_ports(value) for key, value in cwl.items()}
            for kind in ('inputs', 'outputs'):
                ports = cwl.get(kind)
                if isinstance(ports, dict):
                    canonical[kind] = {name: _long_form(spec) for name, spec in ports.items()}
            return canonical
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
    return sophios.compiler.compile_source(
        bundle_from_disk(path, workflow_paths, tools), default_compilation_settings(),
        testing=False)


def _file_door(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostics of `path` compiled as the CLI reads it, with the settings
    `Workflow.compile()` uses; outputs narrowed to the named ones as `compile()` returns them."""
    result = _compile_file(path, workflow_paths, tools)
    artifact = inline_artifact_runs(result.artifact)
    cwl = normalize_artifact_cwl(artifact)
    named = _named_outputs(path)
    if named and isinstance(cwl.get('outputs'), dict):
        cwl['outputs'] = {name: spec for name, spec in cwl['outputs'].items() if name in named}
    return (cwl, normalize_artifact_job_inputs(artifact, artifact.job_inputs), list(map(str, result.diagnostics)))


def _named_outputs(path: Path) -> list[str]:
    """The outputs `path` declares, which `Workflow.compile()` narrows its CWL to."""
    outputs = read_wic(path.read_text(encoding='utf-8'), path.name).get('outputs')
    return list(outputs) if isinstance(outputs, dict) else []


def _objects(workflow: Workflow) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostics of `workflow` compiled from its objects."""
    result = _workflow_runtime.compile_workflow_result(workflow)
    compiled = _workflow_runtime.compiled_workflow_from_result(workflow, result)
    return compiled.cwl_workflow, compiled.cwl_job_inputs, list(map(str, result.diagnostics))


#: The synthetic tools, with `mk_file` also in a `gpu` plugin namespace, and `pair_sink`, whose `energy_x` and
#: `energy_y` read `pair_src`'s `edr_x` and `edr_y` only through the shipped renamings.
TOOLS: Final[Tools] = {
    **SYNTHETIC_TOOLS, StepId('mk_file', 'gpu'): SYNTHETIC_TOOLS[StepId('mk_file', SYNTHETIC_NS)],
    StepId('pair_src', SYNTHETIC_NS): Tool('/synthetic/pair_src.cwl', clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'edr_x': {'type': 'File', 'outputBinding': {'glob': '$(inputs.name)'}},
         'edr_y': {'type': 'File', 'outputBinding': {'glob': '$(inputs.name)'}}}, canonical=True)),
    StepId('pair_sink', SYNTHETIC_NS): Tool('/synthetic/pair_sink.cwl', clt(
        {'energy_x': {'type': 'File', 'inputBinding': {'position': 1}},
         'energy_y': {'type': 'File', 'inputBinding': {'position': 2}}},
        {'out': {'type': 'File', 'outputBinding': {'glob': 'out.txt'}}}, canonical=True))}


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


def _calling_child(name: str) -> str:
    """A document that calls `child.wic` and binds its `mk_file` step's `name` to `!ii {name}`."""
    return ('wic:\n  steps:\n    (1, child.wic):\n      wic:\n        steps:\n          (1, mk_file):\n'
            f'            in:\n              name: !ii {name}\nsteps:\n- id: child.wic\n')


def _held_today(document: Document) -> bool:
    """Not yet, though the file compiles: a `schemed` step, loaded as `api004`, and a scatter over an unbound port,
    raised as a bare ValueError rather than `api006`; each is a strict xfail example below, so this expires."""
    return not any(step.id == 'schemed'
                   or not set(cast(list[str], dict(step.interpreted).get('scatter') or [])) <= dict(step.inputs).keys()
                   for step in document.steps)


def _files(document: Document) -> tuple[str, dict[str, str]]:
    """`document` without the passthrough Python has no spelling for, and each workflow it calls, as files."""
    held = replace(document, steps=tuple(replace(step, passthrough=()) for step in document.steps),
                   passthrough=tuple(entry for entry in document.passthrough if entry[0] == 'inputs'))
    return render(held), {step.id: yaml.safe_dump(SUBWORKFLOW_BODIES[step.id.removesuffix('.wic').rstrip('0123456789')])
                          for step in document.steps if step.id.endswith('.wic')}


def _outcome(compile_it: Callable[[], tuple[Json, Json, list[str]]]) -> Any:
    """What `compile_it` gives, its CWL's ports in long form, or the codes it is refused with."""
    try:
        cwl, job_inputs, diagnostics = compile_it()
    except SophiosError as error:
        return {diagnostic.code for diagnostic in error.diagnostics}
    return _canonical_ports(cwl), job_inputs, diagnostics


@pytest.mark.fast
@given(compilable_documents().filter(_held_today).map(_files), st.just(False))
# A bare name is the workflow input, `!*` the producing step's output object and `!ii` a literal; the workflow output
# is the step output it names.
@example((CHAIN, {}), True)
# `child.wic` becomes a nested `Workflow` named `child`, built from its own file, its input bound by the caller.
@example(('steps:\n- id: child.wic\n  in:\n    n: !ii made.txt\n',
          {'child.wic': 'inputs:\n  n: string\nsteps:\n- id: mk_file\n  in:\n    name: n\n'}), True)
# `count` binds nothing; the objects leave its `file` unbound and the compile infers it as the file's does, naming
# the `.wic` line the objects were read from, line 8, in its note.
@example(('steps:\n- id: mk_file\n  in:\n    name: !ii a\n- id: xform\n  in:\n    name: !ii b\n- id: count\n', {}),
         True)
@example(('steps:\n- id: mk_file\n  in:\n    name: !ii a.txt\n- id: count\n', {}), True)
# A step named apart from its inline tool holds that tool; `scatter`, its method and `when` carry over.
@example(('steps:\n- id: shout\n  run:\n    class: CommandLineTool\n    baseCommand: echo\n'
          '    inputs:\n      message:\n        type: string\n        inputBinding:\n          position: 1\n'
          '    outputs: {}\n  in:\n    message: !ii [a, b]\n  scatter: [message]\n  scatterMethod: dotproduct\n'
          '  when: $(inputs.message != "c")\n', {}), True)
# A `string[]` workflow input feeds the `string` port its step scatters over, as in the file.
@example(('inputs:\n  names: string[]\n'
          'steps:\n- id: mk_file\n  scatter: name\n  scatterMethod: dotproduct\n  in:\n    name: names\n', {}), True)
# The file's own compile pairs `energy_x` with `edr_x` and `energy_y` with `edr_y` through the shipped renamings;
# loading it for Python must not refuse it (`wic055`) by starting from none.
@example(('steps:\n- id: pair_src\n  in:\n    name: !ii a.txt\n- id: pair_sink\n', {}), True)
# `left` and `right` both call `child.wic` with the same `wic: steps:` body, so both calls build it alike.
@example(('steps:\n- id: left.wic\n- id: right.wic\n', {'left.wic': _calling_child('same.txt'),
          'right.wic': _calling_child('same.txt'), 'child.wic': 'steps:\n- id: mk_file\n'}), True)
# `!cwl {source: [...], linkMerge: ...}` is a `StepInput` over the two output objects; its `linkMerge` reaches the CWL.
@example(('steps:\n- id: mk_file\n  in:\n    name: !ii a\n  out:\n  - file: !& f\n'
          '- id: xform\n  in:\n    file: !* f\n    name: !ii b\n  out:\n  - file: !& g\n'
          '- id: sink\n  in:\n    file: !* f\n    n: !ii 1\n'
          '    extras: !cwl {source: [!* f, !* g], linkMerge: merge_flattened}\n', {}), True)
# Held today: a scatter over an unbound port raises a bare ValueError, and a `schemed` step is refused as `api004`.
@example(('steps:\n- id: captured\n  scatter: [name]\n  scatterMethod: dotproduct\n', {}), False).xfail(
    raises=ValueError)
@example(('steps:\n- id: schemed\n', {}), False).xfail(raises=AssertionError)
@budget(25)
def test_a_wic_file_and_the_objects_read_from_it_compile_alike(drawn: tuple[str, dict[str, str]],
                                                               must_build: bool) -> None:
    """Property 1 over generated documents, whose reading writes nothing: `api006` alone, or the file's own refusal,
    or the same CWL, job inputs and diagnostics, at the `.wic` lines the objects were read from. A pinned document
    must build."""
    root, called = drawn
    with tempfile.TemporaryDirectory() as name, pytest.MonkeyPatch.context() as monkeypatch:
        directory = Path(name)
        monkeypatch.chdir(directory)
        for file, text in {'root.wic': root, **called}.items():
            (directory / file).write_text(text, encoding='utf-8')
        workflow_paths = {'global': {path.stem: path for path in directory.glob('*.wic')}}
        file_door = _outcome(lambda: _file_door(directory / 'root.wic', workflow_paths, TOOLS))
        try:
            workflow = Workflow.from_wic(directory / 'root.wic', tool_registry=TOOLS, workflow_paths=workflow_paths)
        except SophiosError as error:
            assert not must_build and (isinstance(file_door, set) or {d.code for d in error.diagnostics} == {
                SophiosErrorCode.NO_PYTHON_SPELLING}), str(error)
            return
        assert sorted(path.name for path in directory.iterdir()) == sorted({'root.wic', *called})
        assert _outcome(lambda: _objects(workflow)) == file_door


@pytest.mark.fast
def test_an_input_declared_with_an_empty_mapping_is_left_untyped(tmp_path: Path) -> None:
    """`name: {}` declares no type, as a null spec does: the call builds the workflow instead of raising."""
    root = 'inputs:\n  name: {}\nsteps:\n- id: mk_file\n  in:\n    name: name\n'
    workflow = _from_wic(tmp_path, root=root)
    assert [port.name for port in workflow.inputs] == ['name']


def _output_source_root(output_source: str) -> str:
    return ('steps:\n- id: mk_file\n  in:\n    name: !ii a\n  out:\n  - file: !& f\n'
            '- id: xform\n  in:\n    file: !* f\n    name: !ii b\n'
            f'outputs:\n  o:\n    type: File\n    outputSource: {output_source}\n')


@pytest.mark.fast
def test_a_positional_output_source_becomes_an_object_reference(tmp_path: Path,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    """A position is read as Lower reads it; the objects say `xform/file`,
    so the written bundle names the authored step and its compile warns about no spelling."""
    workflow = _from_wic(tmp_path, root=_output_source_root('(2, xform)/file'))
    written = workflow.write_wic(tmp_path / 'written')
    document = read_wic(written.read_text(encoding='utf-8'))
    assert document['outputs']['o']['outputSource'] == 'xform/file'
    capsys.readouterr()
    workflow.compile()
    captured = capsys.readouterr()
    assert 'Warning!' not in captured.out + captured.err


@pytest.mark.fast
def test_a_real_time_declaration_written_back_stays_a_declaration(tmp_path: Path) -> None:
    """`write_wic` names the adapter as the registry does, so the bundle declares the same analysis
    the objects do, `max_times: !ii '20'` included, and emits no step for it."""
    adapter = ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl'
    tools = {**TOOLS, StepId(adapter.stem, SYNTHETIC_NS): Tool(str(adapter), yaml.safe_load(adapter.read_text()))}
    root = ("steps:\n- id: mk_file\n  in:\n    name: !ii a.txt\n"
            "- id: cwl_subinterpreter\n  in:\n    file_pattern: !ii '*.txt'\n    cwl_tool: !ii count\n"
            "    max_times: !ii '20'\n    config: !ii {in: {file: a.txt}}\n")
    workflow = Workflow.from_wic(tmp_path / 'root.wic', tool_registry=tools,
                                 workflow_paths=_documents(tmp_path, root=root))
    direct = _workflow_runtime.compile_workflow_result(workflow, tool_registry=tools)

    bundle = tmp_path / 'bundle'
    written_root = workflow.write_wic(bundle)
    written: WorkflowPaths = {'global': {path.stem: path for path in bundle.glob('*.wic')}}
    via_file = _compile_file(written_root, written, tools)

    (declared,) = direct.realtime
    assert declared.max_times == 20
    assert [replace(item, span=None) for item in via_file.realtime] == [replace(declared, span=None)]
    direct_cwl = _workflow_runtime.compiled_workflow_from_result(workflow, direct).cwl_workflow
    via_file_cwl = _workflow_runtime.compiled_workflow_from_result(workflow, via_file).cwl_workflow
    assert [step['id'] for step in direct_cwl['steps']] == ['mk_file']
    assert equivalent(direct_cwl, via_file_cwl, Strength.IDENTICAL) is None


ONE_STEP: Final = 'steps:\n- id: mk_file\n  in:\n    name: !ii a\n'


@pytest.mark.fast
@pytest.mark.parametrize('case, expected', case_dirs('from_wic_refusals', 'diagnostics'))
def test_a_document_python_cannot_hold_is_refused_whole(case: Path, expected: dict[str, Any]) -> None:
    """A document that compiles but holds what the Python API cannot is `api006`, once per construct, each where it
    is written and saying what to write, in one raise; a document that does not compile raises its own error."""
    with pytest.raises(SophiosError) as caught:
        Workflow.from_wic(case / 'root.wic', tool_registry=TOOLS,
                          workflow_paths={'global': {path.stem: path for path in case.glob('*.wic')}})
    assert match(expected['diagnostics'], [diagnostic(found) for found in caught.value.diagnostics]), caught.value


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
def test_graphviz_and_inlineable_are_dropped_with_a_warning(tmp_path: Path) -> None:
    """Neither changes what a Python workflow compiles; the warning names the file and both keys."""
    root = 'wic:\n  graphviz:\n    label: drawn\n  inlineable: false\n' + ONE_STEP
    with pytest.warns(UserWarning, match='root.wic: wic: graphviz, inlineable dropped'):
        workflow = _from_wic(tmp_path, root=root)
    _assert_compiles_alike(tmp_path, workflow)


def _loaded(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> Workflow | None:
    """`Workflow.from_wic(path)`, or None when every construct it refused is `api006`."""
    try:
        return Workflow.from_wic(path, tool_registry=tools, workflow_paths=workflow_paths)
    except SophiosError as error:
        codes = {diagnostic.code for diagnostic in error.diagnostics}
        assert codes == {SophiosErrorCode.NO_PYTHON_SPELLING}, str(error)
        return None


@pytest.mark.cwl_corpus
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


@pytest.mark.cwl_corpus
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
#: four repeat a step id, `test_rand_fail` scatters a `.wic` call and reads an
#: edge across documents, and `secrets_echo` declares `$namespaces`, `hints` and
#: an input `default`.
REFUSED: Final = frozenset({'append_twice', 'multistep1', 'multistep2', 'multistep3', 'test_rand_fail',
                            'secrets_echo'})

#: The directories of `.wic` files shipped in this repository.
IN_REPO: Final = (REPO_ROOT / 'docs' / 'tutorials', REPO_ROOT / 'examples')


@lru_cache(maxsize=1)
def _in_repo_registry() -> tuple[WorkflowPaths, Tools]:
    """The `.wic` files this repository ships and the tools it ships, read from nothing else."""
    workflow_paths = {'global': {path.stem: path for directory in IN_REPO for path in directory.glob('*.wic')}}
    return workflow_paths, shipped_tools()


@pytest.mark.fast
def test_the_in_repo_workflows_that_python_cannot_hold_are_these() -> None:
    """Every `.wic` this repository ships compiles; those in `REFUSED` are `api006`, every other one builds."""
    workflow_paths, tools = _in_repo_registry()
    refused = {stem for stem, path in workflow_paths['global'].items()
               if _loaded(path, workflow_paths, tools) is None}
    assert refused == REFUSED
