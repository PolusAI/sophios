"""`--cwl_inline_subworkflows` and `--cwl_inline_runtag`, judged on the inline tree of tests/core/data/inline: the flat
document it states, which calls dissolve and which stay and why, and what each step runs under each flag; a tool
embedded in a newer document keeps the defaults its own version gave it; and the flags on the command line."""
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import sophios.main
from sophios import post_compile
from sophios.compiler import CompileSettings, compile_source
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import Yaml

from .cases import DATA, match
from .helpers import stated
from .test_compiler import _tools
from .test_setup import workflow_paths

TREE = DATA / 'inline'


def _compiled(root: str) -> CompilationArtifact:
    """`root` of the inline tree compiled, its children the other `.wic` files beside it."""
    others = {'global': {path.stem: path for path in TREE.glob('*.wic') if path.name != root}}
    artifact = compile_source(bundle_from_disk(TREE / root, others, _tools(TREE)), CompileSettings(),
                              quiet=True).artifact
    assert artifact is not None
    return artifact


def _runs(form: CompilationArtifact) -> list[str]:
    """What each step of `form` runs, `T` a CommandLineTool and `W` a Workflow: embedded in its `run:`, or the child
    document its step names."""
    children = {child.namespace: child.cwl['class'] for child in form.children}
    classes = [step['run']['class'] if isinstance(step['run'], dict) else children[(step['id'],)]
               for step in form.cwl['steps']]
    return ['T' if kind == 'CommandLineTool' else 'W' for kind in classes]


@pytest.mark.fast
def test_the_inline_tree_flattens_to_the_document_it_states(capsys: pytest.CaptureFixture[str]) -> None:
    """Flattened, the tree is the document expected.yaml states, with the root's interface and job unchanged, and
    each kept call said once, with its reason; the flags apply shape first and do not depend on each other."""
    nested = _compiled('workflow.wic')
    expected = stated(TREE)
    flat = post_compile.flatten_subworkflows(nested)
    assert match(expected['said'], capsys.readouterr().err.splitlines())
    assert match(expected['flat'], flat.cwl)
    assert (list(flat.cwl['inputs']), list(flat.cwl['outputs']), flat.job_inputs) == \
        (list(nested.cwl['inputs']), list(nested.cwl['outputs']), nested.job_inputs)
    assert post_compile.apply_inline_options(nested, subworkflows=False, runtag=False) is nested
    forms = {'nested': nested, '--cwl_inline_subworkflows': flat,
             '--cwl_inline_runtag': post_compile.apply_inline_options(nested, subworkflows=False, runtag=True),
             'both': post_compile.apply_inline_options(nested, subworkflows=True, runtag=True)}
    assert {name: _runs(form) for name, form in forms.items()} == expected['runs']
    assert [step['id'] for step in forms['both'].cwl['steps']] == [step['id'] for step in flat.cwl['steps']]
    # A child whose requirements a script listed after the compile keeps its call: flattening reads the mapping only.
    listed = replace(nested, children=tuple(
        replace(child, cwl={**child.cwl, 'requirements': [{'class': 'EnvVarRequirement', 'envDef': {'A': 'b'}}]})
        if child.namespace == ('plain',) else child for child in nested.children))
    assert [step['id'] for step in post_compile.flatten_subworkflows(listed).cwl['steps']][1] == 'plain'
    assert match({'contains': [{'contains': ["step 'plain.wic'", 'requirements are written as a list']}]},
                 capsys.readouterr().err.splitlines())


@pytest.mark.fast
def test_an_embedded_tool_keeps_the_defaults_its_own_version_gave_it() -> None:
    """Embedded, a tool loses its `cwlVersion`, and nothing updates it as cwltool updates a file it loads: what its
    version implied is written out, what it declares itself is kept, and a version Sophios cannot read is refused.
    Each tool of tests/core/data/inline/embedded stands in for the one step of plain.wic."""
    artifact = _compiled('plain.wic')
    tools = TREE / 'embedded'
    for name, expected in stated(tools).items():
        tool = yaml.safe_load((tools / name).read_text(encoding='utf-8'))
        swapped = replace(artifact, children=(replace(artifact.children[0], cwl=tool),))
        if 'refused' in expected:
            with pytest.raises(SophiosError) as caught:
                post_compile.inline_artifact_runs(swapped)
            (diagnostic,) = caught.value.diagnostics
            assert diagnostic.code is SophiosErrorCode.UNSUPPORTED_CWL_VERSION, name
            assert expected['refused'] in diagnostic.message, name
            continue
        (step,) = post_compile.inline_artifact_runs(swapped).cwl['steps']
        assert 'cwlVersion' not in step['run'], name
        assert match(expected, step['run']), (name, step['run'])


def _cli(directory: Path, monkeypatch: pytest.MonkeyPatch, workflow: str, *flags: str) -> Path:
    """Run the CLI on a tutorial from a fresh `directory`; the directory it wrote."""
    directory.mkdir()
    monkeypatch.chdir(directory)
    source = next(paths[workflow] for paths in workflow_paths().values() if workflow in paths)
    monkeypatch.setattr(sys, 'argv', ['sophios', '--yaml', str(source), '--generate_cwl_workflow',
                                      '--quiet', *flags])
    sophios.main._main(*sophios.cli.parser.parse_known_args())  # pylint: disable=protected-access
    return directory / 'autogenerated'


def _document(written: Path, name: str) -> Yaml:
    """The root document the CLI wrote into `written`."""
    return cast(Yaml, yaml.safe_load((written / f'{name}.cwl').read_text(encoding='utf-8')))


@pytest.mark.fast
def test_the_inline_flags_on_the_command_line(  # pylint: disable=too-many-locals
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """End to end on the tutorials. multistep3 (touch, append_twice.wic calling append twice, cat) flattened: tools
    stay files, a dissolved call leaves no Workflow document, the root interface and job are the nested one's, and
    nothing is said; with --cwl_inline_runtag too, one document carrying every process. helloworld's echo.cwl is
    v1.0, and embedded it still reaches the network. test_rand_fail scatters over fail.wic, and the `when`
    --partial_failure_enable gives a call gates its inputs: both calls keep their place, and one line names each."""
    nested = _cli(tmp_path / 'nested', monkeypatch, 'multistep3')
    flat = _cli(tmp_path / 'flat', monkeypatch, 'multistep3', '--cwl_inline_subworkflows')
    document, default = _document(flat, 'multistep3'), _document(nested, 'multistep3')
    flat_ids = ['touch', 'append_twice~append@1', 'append_twice~append@2', 'cat']
    assert [step['id'] for step in document['steps']] == flat_ids
    assert [step['run'] for step in document['steps']] == [f'multistep3~{name}.cwl' for name in flat_ids]
    assert all((flat / step['run']).is_file() for step in document['steps'])
    assert not (flat / 'multistep3~append_twice.cwl').exists()
    assert 'SubworkflowFeatureRequirement' not in document.get('requirements', {})
    assert [document['steps'][2]['in']['file'], document['steps'][3]['in']['file']] == \
        [f'{flat_ids[1]}/file', f'{flat_ids[2]}/file']
    assert document['outputs']['append_twice~append@2~file']['outputSource'] == f'{flat_ids[2]}/file'
    assert (list(document['inputs']), list(document['outputs'])) == (list(default['inputs']), list(default['outputs']))
    assert (flat / 'multistep3_inputs.yml').read_text() == (nested / 'multistep3_inputs.yml').read_text()
    assert 'Warning!' not in capsys.readouterr().err

    both = _cli(tmp_path / 'both', monkeypatch, 'multistep3', '--cwl_inline_subworkflows', '--cwl_inline_runtag')
    assert sorted(path.name for path in both.iterdir()) == ['multistep3.cwl', 'multistep3_inputs.yml']
    assert [(step['id'], step['run']['class']) for step in _document(both, 'multistep3')['steps']] == \
        [(name, 'CommandLineTool') for name in flat_ids]
    (echo,) = _document(_cli(tmp_path / 'hello', monkeypatch, 'helloworld', '--cwl_inline_runtag'),
                        'helloworld')['steps']
    assert echo['run']['hints']['NetworkAccess'] == {'networkAccess': True}

    kept: dict[str, Any] = {}
    for name, workflow, step, flags in (('scattered', 'test_rand_fail', 'fail', ()),
                                        ('gated', 'multistep3', 'append_twice', ('--partial_failure_enable',))):
        capsys.readouterr()
        written = _cli(tmp_path / name, monkeypatch, workflow, '--cwl_inline_subworkflows', *flags)
        call = next(each for each in _document(written, workflow)['steps'] if each['id'] == step)
        assert (written / call['run']).is_file() and _document(written, workflow)['requirements'][
            'SubworkflowFeatureRequirement'] == {}
        kept[name] = [line for line in capsys.readouterr().err.splitlines() if line.startswith('Warning!')]
    assert match({'scattered': [{'contains': ["step 'fail.wic'", 'carries `scatter`']}],
                  'gated': [{'contains': ["step 'append_twice.wic'", 'carries `when`']}]}, kept)
