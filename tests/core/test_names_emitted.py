"""Compiled CWL calls each step what its author wrote, and spells nothing of its own."""
import json
import re
from pathlib import Path

import pytest

from sophios import post_compile
from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
from sophios.input_output import write_artifacts_to_disk
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode

from .hermetic import compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_TOOLS


def _steps(cwl: dict) -> list[str]:
    return [step['id'] for step in cwl['steps']]


def _nested() -> CompilationArtifact:
    """`child.wic` (mk_file, then xform twice) called from a root that counts its file."""
    child = {'steps': [{'id': 'mk_file'}, {'id': 'xform'}, {'id': 'xform'}]}
    return compile_hermetic({'steps': [subworkflow_step('child.wic', child), {'id': 'count'}]}, 'root').artifact


@pytest.mark.fast
def test_a_step_is_called_by_its_id_and_a_call_without_its_wic() -> None:
    """No workflow prefix, no position, no `.wic`: `child.wic` runs as `child`, from `child.cwl`."""
    root = _nested()
    child, = (artifact for artifact in root.children if artifact.graph is not None)
    assert _steps(root.cwl) == ['child', 'count']
    assert root.cwl['steps'][0]['run'] == 'child.cwl'
    assert (child.namespace, child.name) == (('child',), 'child')


@pytest.mark.fast
def test_an_id_two_steps_have_carries_each_ones_position() -> None:
    """Every occurrence of a repeated id is numbered, the first too, so no name depends on what follows it."""
    child, = (artifact for artifact in _nested().children if artifact.graph is not None)
    assert _steps(child.cwl) == ['mk_file', 'xform_2', 'xform_3']


@pytest.mark.fast
def test_a_generated_name_joins_its_step_and_port_with_two_underscores() -> None:
    """A lifted input and a promoted output are `<step>__<port>`, nested once per call."""
    root = _nested().cwl
    assert 'child__mk_file__name' in root['inputs']
    assert root['outputs']['child__mk_file__file']['outputSource'] == 'child/mk_file__file'


@pytest.mark.fast
def test_a_step_named_like_an_input_takes_its_position_and_the_input_keeps_its_name() -> None:
    """Inputs, outputs and steps share one id space in CWL; the workflow's interface keeps what the author wrote."""
    cwl = compile_hermetic({'inputs': {'mk_file': {'type': 'string'}},
                            'steps': [{'id': 'mk_file', 'in': {'name': 'mk_file'}}]}).artifact.cwl
    assert _steps(cwl) == ['mk_file_1']
    assert list(cwl['inputs']) == ['mk_file']
    assert cwl['steps'][0]['in'] == {'name': 'mk_file'}


@pytest.mark.fast
def test_ids_that_differ_only_in_case_are_numbered() -> None:
    """`Fetch` and `fetch` would be one step directory on macOS and Windows."""
    cwl = compile_hermetic({'steps': [{'id': 'Fetch', 'run': 'mk_file'},
                                      {'id': 'fetch', 'run': 'mk_file'}]}).artifact.cwl
    assert _steps(cwl) == ['Fetch_1', 'fetch_2']


@pytest.mark.fast
def test_a_generated_name_the_author_already_used_takes_a_suffix() -> None:
    """The authored `xform__name` stays; the name lifted from step `xform`'s `name` becomes `xform__name_2`."""
    cwl = compile_hermetic({'inputs': {'xform__name': {'type': 'string'}},
                            'steps': [{'id': 'mk_file'}, {'id': 'xform'}]}).artifact.cwl
    assert list(cwl['inputs']) == ['xform__name', 'mk_file__name', 'xform__name_2']
    assert cwl['steps'][1]['in']['name'] == 'xform__name_2'


@pytest.mark.fast
def test_an_id_that_looks_generated_is_just_an_id() -> None:
    """A step its author called `root__step__1__mk_file` is that step, by that name, and nothing more."""
    cwl = compile_hermetic({'steps': [{'id': 'root__step__1__mk_file', 'run': 'mk_file'}],
                            'outputs': {'f': {'outputSource': 'root__step__1__mk_file/file'}}},
                           'root').artifact.cwl
    assert _steps(cwl) == ['root__step__1__mk_file']
    assert cwl['outputs']['f']['outputSource'] == 'root__step__1__mk_file/file'


@pytest.mark.fast
def test_the_old_generated_spelling_of_a_step_names_nothing() -> None:
    """`<workflow>__step__<n>__<id>` used to be read as step n; now it is an unknown name like any other."""
    with pytest.raises(SophiosError) as raised:
        compile_hermetic({'steps': [{'id': 'mk_file'}],
                          'outputs': {'f': {'outputSource': 'root__step__1__mk_file/file'}}}, 'root')
    assert [diagnostic.code for diagnostic in raised.value.diagnostics] == [SophiosErrorCode.UNTYPED_OUTPUT]


@pytest.mark.fast
def test_a_run_path_tool_is_written_under_its_own_file_name(tmp_path: Path) -> None:
    """`run: tools/t.cwl` is written as `t.cwl`, with no hash of where it came from."""
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools' / 't.cwl').write_text(
        'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: echo\ninputs: {a: string}\noutputs: {}\n',
        encoding='utf-8')
    (tmp_path / 'w.wic').write_text('steps:\n  - id: s\n    run: tools/t.cwl\n    in: {a: !ii x}\n', encoding='utf-8')
    result = compile_source(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS),
                            default_compilation_settings(), testing=True)
    write_artifacts_to_disk(result.artifact, tmp_path / 'out')
    step, = result.artifact.cwl['steps']
    assert step['run'] == 't.cwl'
    assert (tmp_path / 'out' / 't.cwl').is_file()


@pytest.mark.fast
def test_the_tree_is_one_directory_of_files_each_named_once(tmp_path: Path) -> None:
    """cwltool's `--provenance` copies every directory a workflow uses into one folder by its name, and
    stops at the second of a name; so the tree has no directories. A call and the step inside it that
    share a name are two files, and a tool two steps run is one."""
    child = {'steps': [{'id': 'mk_file'}, {'id': 'xform'}, {'id': 'xform'}]}
    artifact = compile_hermetic({'steps': [subworkflow_step('mk_file.wic', child)]}, 'root').artifact
    write_artifacts_to_disk(artifact, tmp_path)
    assert not [path for path in tmp_path.iterdir() if path.is_dir()]
    written = sorted(path.name for path in tmp_path.glob('*.cwl'))
    assert written == ['mk_file.cwl', 'mk_file_2.cwl', 'root.cwl', 'xform.cwl']
    called, = artifact.children
    assert artifact.cwl['steps'][0]['run'] == 'mk_file.cwl'
    assert [step['run'] for step in called.cwl['steps']] == ['mk_file_2.cwl', 'xform.cwl', 'xform.cwl']


@pytest.mark.fast
@pytest.mark.parametrize('form', ['nested', 'embedded', 'flat', 'flat_embedded'])
def test_no_form_of_the_compiled_cwl_spells_a_name_of_its_own(form: str) -> None:
    """Whichever way the tree is written, no id carries `__step__`, `.wic`, `___` or a hash."""
    nested = _nested()
    flat = post_compile.flatten_subworkflows(nested)
    artifact = {'nested': nested, 'embedded': post_compile.inline_artifact_runs(nested),
                'flat': flat, 'flat_embedded': post_compile.inline_artifact_runs(flat)}[form]

    def texts(node: CompilationArtifact) -> list[str]:
        return [json.dumps(node.cwl), json.dumps(node.job_inputs),
                *(text for child in node.children for text in texts(child))]
    found = [hit for text in texts(artifact) for hit in re.findall(r'__step__|\.wic|___|_[0-9a-f]{8}\b', text)]
    assert not found
    if form.startswith('flat'):
        assert _steps(artifact.cwl) == ['child__mk_file', 'child__xform_2', 'child__xform_3', 'count']
