"""A `.wic` is read where it is and written elsewhere: a relative location in a `default:` names a file
beside the `.wic` that wrote it."""
from pathlib import Path

import pytest

from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk

from .synthetic_tools import SYNTHETIC_TOOLS

pytestmark = pytest.mark.package


def _compile_wic(wic: Path) -> CompilationArtifact:
    return compile_source(bundle_from_disk(wic, {}, SYNTHETIC_TOOLS), default_compilation_settings(),
                          testing=True).artifact


@pytest.mark.fast
def test_a_wic_file_default_is_written_absolute_against_the_wic(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative `default:` of a workflow input, a called one's and a step input's names a file beside the `.wic`
    that wrote it, wherever Sophios runs and writes: a `location` as a URI, a `path` as a path."""
    project = tmp_path / 'project'
    (project / 'sub').mkdir(parents=True)
    (project / 'sub' / 'child.wic').write_text(
        'inputs:\n  cfg: {type: File, default: {class: File, location: other.txt}}\n'
        'steps:\n- id: xform\n  in: {file: cfg, name: !ii out.txt}\n', encoding='utf-8')
    (project / 'w.wic').write_text(
        'inputs:\n  cfg: {type: File, default: {class: File, location: data/config.txt}}\n'
        '  ref: {type: File, default: {class: File, path: data/ref.txt}}\n'
        '  far: {type: File, default: {class: File, location: "file:///elsewhere/far.txt"}}\n'
        'steps:\n- id: xform\n  in: {file: cfg, name: !ii a.txt}\n- id: again\n  run: xform\n'
        '  in: {file: ref, name: !ii b.txt}\n- id: third\n  run: xform\n  in: {file: far, name: !ii c.txt}\n'
        '- id: call\n  run: sub/child.wic\n- id: fourth\n  run: xform\n'
        '  in: {file: !cwl {default: {class: File, location: data/step.txt}}, name: !ii d.txt}\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    artifact = _compile_wic(project / 'w.wic')
    inputs = artifact.cwl['inputs']
    assert inputs['cfg']['default'] == {'class': 'File', 'location': (project / 'data' / 'config.txt').as_uri()}
    assert inputs['ref']['default'] == {'class': 'File', 'path': str(project / 'data' / 'ref.txt')}
    assert inputs['far']['default'] == {'class': 'File', 'location': 'file:///elsewhere/far.txt'}
    (child,) = (document for document in artifact.children if document.graph is not None)
    assert child.cwl['inputs']['cfg']['default']['location'] == (project / 'sub' / 'other.txt').as_uri()
    assert artifact.cwl['steps'][-1]['in']['file'] == {
        'default': {'class': 'File', 'location': (project / 'data' / 'step.txt').as_uri()}}
