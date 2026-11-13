"""`Workflow.from_wic`: a `.wic` file as Python objects. A document of every construct Python spells, and documents of
what it cannot hold, say what each becomes (tests/core/data/from_wic); over generated documents and the corpus, a file
the file door compiles is refused with `api006` alone or compiles to equal output, and `write_wic` round-trips."""
# pylint: disable=redefined-outer-name  # `corpus_registry` is a pytest fixture
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Final, cast

import pytest
import yaml
from hypothesis import given

import sophios.compiler
from sophios.api.python import _workflow_runtime
from sophios.api.python.workflow import SophiosError, SophiosErrorCode, Workflow
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.artifacts import CompilationResult
from sophios.post_compile import inline_artifact_runs
from sophios.runtime_inputs import output_target_inputs, runner_job_inputs
from sophios.utils_cwl import canonicalize_type
from sophios.lang import Document, render
from sophios.wic_types import Json, StepId, Tool, Tools

from .ast_strategies import SUBWORKFLOW_BODIES, documents
from .budgets import budget
from .cases import DATA, case_dirs, diagnostic, match
from .equivalence import Strength, equivalent
from .helpers import ADAPTERS, REPO_ROOT, shipped_tools
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS, clt
from .test_examples import _is_includer_fragment, yml_paths_tuples_not_large
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
        bundle_from_disk(path, workflow_paths, tools), sophios.compiler.CompileSettings(),
        quiet=False)


def _file_door(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostics of `path` compiled as the CLI reads it, with the settings
    `Workflow.compile()` uses; outputs narrowed to the named ones as `compile()` returns them."""
    result = _compile_file(path, workflow_paths, tools)
    artifact = inline_artifact_runs(result.artifact)
    cwl = {**artifact.cwl}
    named = _named_outputs(path)
    if named and isinstance(cwl.get('outputs'), dict):
        cwl['outputs'] = {name: spec for name, spec in cwl['outputs'].items() if name in named}
    job = runner_job_inputs(artifact.job_inputs, output_target_inputs(sophios.compiler.resolved_view(result)))
    return (cwl, job, list(map(str, result.diagnostics)))


def _named_outputs(path: Path) -> list[str]:
    """The outputs `path` declares, which `Workflow.compile()` narrows its CWL to."""
    outputs = read_wic(path.read_text(encoding='utf-8'), path.name).get('outputs')
    return list(outputs) if isinstance(outputs, dict) else []


def _objects(workflow: Workflow) -> tuple[Json, Json, list[str]]:
    """CWL, job inputs and diagnostics of `workflow` compiled from its objects."""
    result = _workflow_runtime.compile_workflow_result(workflow)
    compiled = _workflow_runtime.compiled_workflow_from_result(workflow, result)
    return compiled.cwl_workflow, compiled.cwl_job_inputs, list(map(str, result.diagnostics))


#: The real-time analysis declaration's adapter.
_WATCHER: Final = ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl'

#: The synthetic tools, with `mk_file` also in a `gpu` plugin namespace; `pair_sink`, whose `energy_x` and `energy_y`
#: read `pair_src`'s `edr_x` and `edr_y` only through the shipped renamings; and the declaration's adapter.
TOOLS: Final[Tools] = {
    StepId(_WATCHER.stem, SYNTHETIC_NS): Tool(str(_WATCHER), yaml.safe_load(_WATCHER.read_text(encoding='utf-8'))),
    **SYNTHETIC_TOOLS, StepId('mk_file', 'gpu'): SYNTHETIC_TOOLS[StepId('mk_file', SYNTHETIC_NS)],
    StepId('pair_src', SYNTHETIC_NS): Tool('/synthetic/pair_src.cwl', clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'edr_x': {'type': 'File', 'outputBinding': {'glob': '$(inputs.name)'}},
         'edr_y': {'type': 'File', 'outputBinding': {'glob': '$(inputs.name)'}}}, canonical=True)),
    StepId('pair_sink', SYNTHETIC_NS): Tool('/synthetic/pair_sink.cwl', clt(
        {'energy_x': {'type': 'File', 'inputBinding': {'position': 1}},
         'energy_y': {'type': 'File', 'inputBinding': {'position': 2}}},
        {'out': {'type': 'File', 'outputBinding': {'glob': 'out.txt'}}}, canonical=True))}


def _held_today(document: Document) -> bool:
    """Not yet, though the file compiles: a `schemed` step, loaded as `api004`, and a scatter over an unbound port,
    raised as a bare ValueError rather than `api006`; tests/core/data/from_wic/refused/held_* state each, so this
    expires."""
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
@pytest.mark.filterwarnings('ignore:.*graphviz dropped:UserWarning')  # every_construct states it
@given(documents().filter(_held_today).map(_files))  # pylint: disable=no-member
@budget(25)
def test_a_wic_file_and_the_objects_read_from_it_compile_alike(drawn: tuple[str, dict[str, str]]) -> None:
    """Over generated documents, whose reading writes nothing: `api006` alone, or the file's own refusal, or the same
    CWL, job inputs and diagnostics, at the `.wic` lines the objects were read from."""
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
            assert isinstance(file_door, set) or {d.code for d in error.diagnostics} == {
                SophiosErrorCode.NO_PYTHON_SPELLING}, str(error)
            return
        assert sorted(path.name for path in directory.iterdir()) == sorted({'root.wic', *called})
        assert _outcome(lambda: _objects(workflow)) == file_door


def _loaded(path: Path, workflow_paths: WorkflowPaths, tools: Tools) -> Workflow | None:
    """`Workflow.from_wic(path)`, or None when every construct it refused is `api006`."""
    try:
        return Workflow.from_wic(path, tool_registry=tools, workflow_paths=workflow_paths)
    except SophiosError as error:
        codes = {diagnostic.code for diagnostic in error.diagnostics}
        assert codes == {SophiosErrorCode.NO_PYTHON_SPELLING}, str(error)
        return None


def _compiles_alike_and_writes_back(path: Path, workflow_paths: WorkflowPaths, tools: Tools, workflow: Workflow,
                                    directory: Path) -> Path:
    """The objects read from `path` compile as the file does, up to order and CWL's shorthand, with the same job inputs
    and diagnostics; the bundle `write_wic` writes for them in `directory`, through the file door, is the objects' own
    compilation, byte for byte, and so is that bundle read back. Returns the bundle's root."""
    from_file, from_objects = _file_door(path, workflow_paths, tools), _objects(workflow)
    found = equivalent(_canonical_ports(from_file[0]), _canonical_ports(from_objects[0]), Strength.UP_TO_ORDER)
    assert found is None, f'{path.name}: the file and its Python objects compile apart\n{found}'
    assert from_objects[1:] == from_file[1:]
    direct = workflow.compile()
    root = workflow.write_wic(directory)
    written: WorkflowPaths = {'global': {each.stem: each for each in directory.glob('*.wic')}}
    via_file = _workflow_runtime.compiled_workflow_from_result(workflow, _compile_file(root, written, tools))
    reloaded = Workflow.from_wic(root, tool_registry=tools, workflow_paths=written).compile()
    for other in (via_file, reloaded):
        assert equivalent(direct.cwl_workflow, other.cwl_workflow, Strength.IDENTICAL) is None
        assert equivalent(direct.cwl_job_inputs, other.cwl_job_inputs, Strength.IDENTICAL) is None
    return root


@pytest.mark.cwl_corpus
@pytest.mark.fast
@pytest.mark.parametrize('yml_path_str, yml_path', yml_paths_tuples_not_large)
def test_a_wic_file_and_its_python_objects_compile_alike(yml_path_str: str, yml_path: Path, tmp_path: Path,
                                                         corpus_registry: CorpusRegistry) -> None:
    """Over the corpus: refused with `api006` alone, or the objects compile as the file does and write a bundle that
    compiles to them (`_compiles_alike_and_writes_back`)."""
    try:
        _compile_file(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise
    workflow = _loaded(Path(yml_path), corpus_registry.workflows, corpus_registry.tools)
    if workflow is not None:
        _compiles_alike_and_writes_back(Path(yml_path), corpus_registry.workflows, corpus_registry.tools, workflow,
                                        tmp_path)


@pytest.mark.fast
def test_a_document_of_every_construct_python_spells_is_read_as_stated(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    """tests/core/data/from_wic/built/every_construct/root.wic is read into objects, warning of what is dropped, and
    they compile as the file does and write back (`_compiles_alike_and_writes_back`); expected.yaml states the
    inputs, the compiled steps, the declared analysis and what the bundle writes. Nothing is written beside it."""
    case = DATA / 'from_wic' / 'built' / 'every_construct'
    stated = yaml.safe_load((case / 'expected.yaml').read_text(encoding='utf-8'))
    workflow_paths = {'global': {path.stem: path for path in case.glob('*.wic')}}
    monkeypatch.chdir(tmp_path)
    with pytest.warns(UserWarning) as warned:
        workflow = Workflow.from_wic(case / 'root.wic', tool_registry=TOOLS, workflow_paths=workflow_paths)
    assert [str(warning.message) for warning in warned if 'dropped' in str(warning.message)] == [
        f'{text}: the Python API has no spelling for them, and it changes no CWL a Workflow compiles or runs'
        for text in stated['warns']]
    assert [port.name for port in workflow.inputs] == stated['inputs']
    root = _compiles_alike_and_writes_back(case / 'root.wic', workflow_paths, TOOLS, workflow, tmp_path / 'bundle')
    assert match(stated['written'], read_wic(root.read_text(encoding='utf-8')))
    result = _workflow_runtime.compile_workflow_result(workflow)
    assert [step['id'] for step in result.artifact.cwl['steps']] == stated['steps']
    assert [{'analysis': item.analysis, 'max_times': item.max_times} for item in result.realtime] == stated['realtime']
    assert not list(tmp_path.glob('*.wic'))


#: The in-repo workflows the Python API cannot hold, each refused with `api006`: four repeat a step id,
#: `test_rand_fail` scatters a `.wic` call and reads an edge across documents, and `secrets_echo` declares
#: `$namespaces`, `hints` and an input `default`.
REFUSED: Final = frozenset({'append_twice', 'multistep1', 'multistep2', 'multistep3', 'test_rand_fail',
                            'secrets_echo'})

#: The directories of `.wic` files shipped in this repository.
IN_REPO: Final = (REPO_ROOT / 'docs' / 'tutorials', REPO_ROOT / 'examples')


@pytest.mark.fast
@pytest.mark.parametrize('case, expected', case_dirs('from_wic/refused', 'project', 'refused', 'raises'))
def test_a_document_python_cannot_hold_is_refused_whole(case: Path, expected: dict[str, Any],
                                                        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A document that compiles but holds what the Python API cannot is `api006`, once per construct, each saying
    what to write instead, in one raise; a document that does not compile raises its own error. expected.yaml states
    each diagnostic, or the exception a document held today raises. The in-repo workflows refused are `REFUSED`, and
    every other one builds."""
    root = case / 'root.wic'
    if 'project' in expected:  # run in place, from a copy
        shutil.copytree(DATA / Path(expected['project']).parts[0], tmp_path / 'project')
        root = tmp_path / 'project' / Path(*Path(expected['project']).parts[1:])
        monkeypatch.chdir(root.parent)
    with pytest.raises(SophiosError if 'refused' in expected else ValueError) as caught:
        Workflow.from_wic(root, tool_registry=TOOLS,
                          workflow_paths={'global': {path.stem: path for path in root.parent.glob('*.wic')}})
    if 'refused' in expected:
        refused = cast(SophiosError, caught.value)
        assert match(expected['refused'], [diagnostic(found) for found in refused.diagnostics]), refused
    if case.name == 'every_construct':
        workflow_paths = {'global': {path.stem: path for directory in IN_REPO for path in directory.glob('*.wic')}}
        assert {stem for stem, path in workflow_paths['global'].items()
                if _loaded(path, workflow_paths, shipped_tools()) is None} == REFUSED
