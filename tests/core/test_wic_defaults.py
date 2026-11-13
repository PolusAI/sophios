"""A `.wic` is read where it is and written elsewhere: a relative location in a `default:` names a file
beside the `.wic` that wrote it."""
from pathlib import Path
import shutil

import pytest

from sophios.compiler import CompileSettings, compile_source
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk

from .cases import DATA
from .synthetic_tools import SYNTHETIC_TOOLS


def _compile_wic(wic: Path) -> CompilationArtifact:
    return compile_source(bundle_from_disk(wic, {}, SYNTHETIC_TOOLS), CompileSettings(),
                          quiet=True).artifact


@pytest.mark.fast
def test_a_wic_file_default_is_written_absolute_against_the_wic(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/core/data/defaults, copied elsewhere and compiled from another directory: a relative `default:` of a
    workflow input, a called one's and a step input's names a file beside the `.wic` that wrote it, wherever Sophios
    runs and writes: a `location` as a URI, a `path` as a path; an absolute one is left as it is."""
    project = tmp_path / 'project'
    shutil.copytree(DATA / 'defaults', project)
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
