"""Compiled CWL calls each step what its author wrote, and spells nothing of its own."""
import json
import re
from pathlib import Path
from typing import Any, Final, cast

import pytest
import yaml
from hypothesis import example, given, strategies as st

from sophios import post_compile
from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
from sophios.input_output import write_artifacts_to_disk
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .budgets import budget
from .hermetic import compile_hermetic, subworkflow_step
from .strategies import pinned
from .synthetic_tools import SYNTHETIC_TOOLS, clt, inputs_of, registered


def _steps(cwl: dict) -> list[str]:
    return [step['id'] for step in cwl['steps']]


#: `child.wic` (mk_file, then xform twice) called from a root that counts its file.
_NESTED: Final[Yaml] = {'steps': [subworkflow_step('child.wic', {'steps': [{'id': 'mk_file'}, {'id': 'xform'},
                                                                           {'id': 'xform'}]}), {'id': 'count'}]}


def _nested() -> CompilationArtifact:
    """`_NESTED`, compiled."""
    return compile_hermetic(_NESTED, 'root').artifact


@pytest.mark.fast
def test_a_step_is_called_by_its_id_and_a_call_without_its_wic() -> None:
    """No workflow prefix, no position, no `.wic`: `child.wic` runs as `child`, from `root~child.cwl`. A lifted input
    and a promoted output are `<step>~<port>`, nested once per call."""
    root = _nested()
    child, = (artifact for artifact in root.children if artifact.graph is not None)
    assert _steps(root.cwl) == ['child', 'count']
    assert root.cwl['steps'][0]['run'] == 'root~child.cwl'
    assert (child.namespace, child.name) == (('child',), 'root~child')
    assert 'child~mk_file~name' in root.cwl['inputs']
    assert root.cwl['outputs']['child~mk_file~file']['outputSource'] == 'child/mk_file~file'


@pytest.mark.fast
@given(st.tuples(st.lists(st.tuples(st.sampled_from(['mk_file', 'MK_File', 'xform', 'Xform', 'a', 'A']),
                                    st.sampled_from(['mk_file', 'xform', 'count'])), min_size=1, max_size=4),
                 st.lists(st.sampled_from(['mk_file', 'Xform', 'a']), max_size=2, unique=True)))
@example(([('mk_file', 'mk_file'), ('xform', 'xform'), ('xform', 'xform')], []))
@example(([('mk_file', 'mk_file')], ['mk_file']))
@example(([('Fetch', 'mk_file'), ('fetch', 'mk_file')], []))
@budget(100)
def test_a_step_is_numbered_exactly_when_another_id_has_its_name(
        case: tuple[list[tuple[str, str]], list[str]]) -> None:
    """docs/compiling-to-cwl.md: a step keeps its id, but every occurrence of a repeated id carries its position after
    an `@`, the first too, so no name depends on what follows it; so does a step named like a workflow input, since
    CWL keeps the three in one id space; and ids that differ only in case count as one id (`Fetch` and `fetch` would be
    one step directory on macOS and Windows). An input keeps the name its author wrote, and a made one is
    `<step>~<port>`."""
    steps, inputs = case
    taken = [step_id.casefold() for step_id, _ in steps]
    names = [f'{step_id}@{index}' if taken.count(step_id.casefold()) > 1 or step_id.casefold() in map(
        str.casefold, inputs) else step_id for index, (step_id, _) in enumerate(steps, start=1)]
    bound = {'in': {'name': inputs[0]}} if inputs else {}
    cwl = compile_hermetic({'inputs': {name: {'type': 'string'} for name in inputs},
                            'steps': [{'id': step_id, 'run': run, **(bound if run == 'mk_file' else {})}
                                      for step_id, run in steps]}).artifact.cwl
    assert _steps(cwl) == names
    assert list(cwl['inputs'])[:len(inputs)] == inputs
    assert set(cwl['inputs']) - set(inputs) <= {f'{name}~{port}' for name, (_, run) in zip(names, steps)
                                                for port in inputs_of(run)}
    assert all(step['in'] == bound['in'] for step, (_, run) in zip(cwl['steps'], steps) if bound and run == 'mk_file')


def _clashes() -> CompilationArtifact:
    """`greeting`, with every clash a document's names can have: repeated calls and steps, case, an authored input
    spelled like a generated name, an `a~b` port, and an inner call that `flatten_subworkflows` dissolves."""
    tools = registered({'probe': clt({'a~b': {'type': 'string', 'inputBinding': {'position': 1}}}, {})})
    child = {'inputs': {'file': {'type': 'File'}}, 'outputs': {'file': {'outputSource': 'xform/file'}},
             'steps': [subworkflow_step('inner.wic', {'steps': [{'id': 'mk_file'}]}),
                       {'id': 'xform', 'in': {'file': 'file'}}]}
    root = {'inputs': {'echo': {'type': 'string'}, 'mk_file__name': {'type': 'string'}},
            'steps': [subworkflow_step('child.wic', child), subworkflow_step('child.wic', child),
                      {'id': 'child', 'run': 'count'},
                      {'id': 'echo', 'run': 'mk_file', 'in': {'name': 'echo'}}, {'id': 'mk_file'},
                      {'id': 'Touch', 'run': 'mk_file'}, {'id': 'touch', 'run': 'mk_file'},
                      {'id': 'xform'}, {'id': 'xform'}, {'id': 'probe'}]}
    return compile_hermetic(root, 'greeting', tools=tools).artifact


def _documents(artifact: CompilationArtifact) -> list[CompilationArtifact]:
    return [artifact, *(document for child in artifact.children if child.graph is not None
                        for document in _documents(child))]


@pytest.mark.fast
def test_no_two_identities_share_a_text() -> None:
    """Steps are as written or `<id>@<n>`, a made name is `<step>~<port>` whatever else the document
    holds, and in every document, nested or flattened, no step shares a text with another id."""
    nested = _clashes()
    spelled = [(list(document.cwl['inputs']), list(document.cwl['outputs']), _steps(document.cwl))
               for document in _documents(nested)]
    assert spelled[0] == (
        ['echo', 'mk_file__name', 'child@1~file', 'child@1~inner~mk_file~name', 'child@1~xform~name',
         'child@2~inner~mk_file~name', 'child@2~xform~name', 'mk_file~name',
         'Touch@6~name', 'touch@7~name', 'xform@8~name', 'xform@9~name', 'probe~a~b'],
        ['child@1~file', 'child@1~inner~mk_file~file', 'child@1~xform~file',
         'child@2~file', 'child@2~inner~mk_file~file', 'child@2~xform~file', 'child@3~n',
         'echo@4~file', 'mk_file~file', 'Touch@6~file', 'touch@7~file', 'xform@8~file', 'xform@9~file'],
        ['child@1', 'child@2', 'child@3', 'echo@4', 'mk_file', 'Touch@6', 'touch@7', 'xform@8', 'xform@9',
         'probe'])
    assert spelled[1:] == 2 * [(['file', 'inner~mk_file~name', 'xform~name'],
                                ['file', 'inner~mk_file~file', 'xform~file'], ['inner', 'xform']),
                               (['mk_file~name'], ['mk_file~file'], ['mk_file'])]
    flat = post_compile.flatten_subworkflows(nested)
    assert _steps(flat.cwl) == ['child@1~inner~mk_file', 'child@1~xform', 'child@2~inner~mk_file',
                                'child@2~xform', 'child@3', 'echo@4', 'mk_file', 'Touch@6', 'touch@7',
                                'xform@8', 'xform@9', 'probe']
    for document in (*_documents(nested), flat):
        ports = {name.casefold() for name in (*document.cwl['inputs'], *document.cwl['outputs'])}
        steps = [step.casefold() for step in _steps(document.cwl)]
        assert len(set(steps)) == len(steps) and not ports & set(steps)
        for side in ('inputs', 'outputs'):
            assert len({name.casefold() for name in document.cwl[side]}) == len(document.cwl[side])


@pytest.mark.fast
def test_an_id_that_looks_generated_is_just_an_id() -> None:
    """A step its author called `root__step__1__mk_file` is that step, by that name, and nothing more."""
    cwl = compile_hermetic({'steps': [{'id': 'root__step__1__mk_file', 'run': 'mk_file'}],
                            'outputs': {'f': {'outputSource': 'root__step__1__mk_file/file'}}},
                           'root').artifact.cwl
    assert _steps(cwl) == ['root__step__1__mk_file']
    assert cwl['outputs']['f']['outputSource'] == 'root__step__1__mk_file/file'


def _tool(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: {command}\n'
                    'inputs: {a: string}\noutputs: {}\n', encoding='utf-8')


def _compile_file(wic: Path) -> CompilationArtifact:
    return compile_source(bundle_from_disk(wic, {}, SYNTHETIC_TOOLS), default_compilation_settings(),
                          quiet=True).artifact


def _runs(written: Path) -> list[str]:
    return [step['run'] for step in yaml.safe_load(written.read_text(encoding='utf-8'))['steps']]


def _command(written: Path) -> str:
    return cast(str, yaml.safe_load(written.read_text(encoding='utf-8'))['baseCommand'])


@pytest.mark.fast
def test_every_document_is_beside_the_root_named_by_its_address_from_the_root(tmp_path: Path) -> None:
    """One directory and no folder: the root, its job, and `<root>~<address>.cwl` for each step at every
    depth; each `run:` names a file beside the document that holds it. A runner reads no other job."""
    inner = {'steps': [{'id': 'xform'}]}
    child = {'steps': [{'id': 'mk_file'}, subworkflow_step('inner.wic', inner)]}
    artifact = compile_hermetic({'steps': [subworkflow_step('child.wic', child), {'id': 'count'}]}, 'root').artifact
    write_artifacts_to_disk(artifact, tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        'root.cwl', 'root_inputs.yml', 'root~child.cwl', 'root~child~inner.cwl', 'root~child~inner~xform.cwl',
        'root~child~mk_file.cwl', 'root~count.cwl']
    assert _runs(tmp_path / 'root.cwl') == ['root~child.cwl', 'root~count.cwl']
    assert _runs(tmp_path / 'root~child.cwl') == ['root~child~mk_file.cwl', 'root~child~inner.cwl']
    assert _runs(tmp_path / 'root~child~inner.cwl') == ['root~child~inner~xform.cwl']


@pytest.mark.fast
def test_a_tool_three_steps_run_is_written_three_times(tmp_path: Path) -> None:
    """A file per step, not per tool: each step's file is its own, the same text three times."""
    artifact = compile_hermetic({'steps': [{'id': 'mk_file'}, {'id': 'xform'}, {'id': 'xform'}, {'id': 'xform'}]},
                                'root').artifact
    write_artifacts_to_disk(artifact, tmp_path)
    files = ['root~xform@2.cwl', 'root~xform@3.cwl', 'root~xform@4.cwl']
    assert _runs(tmp_path / 'root.cwl') == ['root~mk_file.cwl', *files]
    assert len({(tmp_path / name).read_text(encoding='utf-8') for name in files}) == 1


@pytest.mark.fast
def test_two_different_tools_with_one_file_name_are_two_files(tmp_path: Path) -> None:
    """`run: a/echo.cwl` and `run: b/echo.cwl` are named by the steps that run them, never by the tools."""
    _tool(tmp_path / 'a' / 'echo.cwl', 'echo')
    _tool(tmp_path / 'b' / 'echo.cwl', 'rm')
    (tmp_path / 'greeting.wic').write_text('steps:\n  - id: hello\n    run: a/echo.cwl\n    in: {a: !ii x}\n'
                                           '  - id: shout\n    run: b/echo.cwl\n    in: {a: !ii y}\n',
                                           encoding='utf-8')
    out = tmp_path / 'out'
    write_artifacts_to_disk(_compile_file(tmp_path / 'greeting.wic'), out)
    assert _runs(out / 'greeting.cwl') == ['greeting~hello.cwl', 'greeting~shout.cwl']
    assert (_command(out / 'greeting~hello.cwl'), _command(out / 'greeting~shout.cwl')) == ('echo', 'rm')


@pytest.mark.fast
def test_two_roots_share_one_basepath_without_overwriting_each_other(tmp_path: Path) -> None:
    """Every file starts with its root's name, so a second root written beside the first changes none of it."""
    _tool(tmp_path / 'a' / 'echo.cwl', 'echo')
    _tool(tmp_path / 'b' / 'echo.cwl', 'rm')
    for root, tool in (('greeting', 'a'), ('other', 'b')):
        (tmp_path / f'{root}.wic').write_text(f'steps:\n  - id: hello\n    run: {tool}/echo.cwl\n'
                                              '    in: {a: !ii x}\n', encoding='utf-8')
    out = tmp_path / 'out'
    write_artifacts_to_disk(_compile_file(tmp_path / 'greeting.wic'), out)
    first = {path.name: path.read_text(encoding='utf-8') for path in out.iterdir()}
    write_artifacts_to_disk(_compile_file(tmp_path / 'other.wic'), out)
    assert {name: (out / name).read_text(encoding='utf-8') for name in first} == first
    assert sorted(path.name for path in out.iterdir()) == [
        'greeting.cwl', 'greeting_inputs.yml', 'greeting~hello.cwl', 'other.cwl', 'other_inputs.yml', 'other~hello.cwl']
    assert (_command(out / 'greeting~hello.cwl'), _command(out / 'other~hello.cwl')) == ('echo', 'rm')


@pytest.mark.fast
def test_a_step_named_con_writes_an_ordinary_file(tmp_path: Path) -> None:
    """A step's name is a suffix of its file's, so `con`, `aux` and `nul` are no Windows device names there."""
    artifact = compile_hermetic({'steps': [{'id': name, 'run': 'mk_file'} for name in ('con', 'aux', 'nul')]},
                                'greeting').artifact
    write_artifacts_to_disk(artifact, tmp_path)
    assert _runs(tmp_path / 'greeting.cwl') == ['greeting~con.cwl', 'greeting~aux.cwl', 'greeting~nul.cwl']
    assert all((tmp_path / name).is_file() for name in _runs(tmp_path / 'greeting.cwl'))


def _addresses(node: CompilationArtifact) -> list[str]:
    """Each step that runs a tool, beneath `node` at any depth, by its address: the calls above it and its name."""
    calls = {child.namespace[-1]: child for child in node.children if child.graph is not None}
    return [address for name in _steps(node.cwl)
            for address in ([f'{name}~{inner}' for inner in _addresses(calls[name])] if name in calls else [name])]


@pytest.mark.fast
@given(st.tuples(strat.workflows().filter(lambda workflow: '___' not in json.dumps(workflow)),
                 st.sampled_from(['nested', 'embedded', 'flat', 'flat_embedded'])))
@pinned(*((_NESTED, form) for form in ('nested', 'embedded', 'flat', 'flat_embedded')))
@budget(50)
def test_no_form_of_the_compiled_cwl_spells_a_name_of_its_own(case: tuple[Yaml, str]) -> None:
    """Whichever way the tree is written, no id carries `__step__`, `.wic`, `___` or a hash, and nothing in it is
    Sophios's own: no `sophios` key, or prefix; a literal is the author's, and `___` the one a drawn literal can spell.
    Flattened, a step of a call, one deep as drawn, is named by its address from the root."""
    workflow, form = case
    nested = compile_hermetic(workflow, 'root').artifact
    flat = post_compile.flatten_subworkflows(nested) if form.startswith('flat') else nested
    artifact = post_compile.inline_artifact_runs(flat) if form.endswith('embedded') else flat

    def documents(node: CompilationArtifact) -> list[Any]:
        return [node.cwl, node.job_inputs, *(document for child in node.children for document in documents(child))]
    found = [hit for document in documents(artifact)
             for hit in re.findall(r'__step__|\.wic|___|_[0-9a-f]{8}\b|"sophios[^"]*":', json.dumps(document))]
    assert not found, found
    assert not form.startswith('flat') or _steps(artifact.cwl) == _addresses(nested)
