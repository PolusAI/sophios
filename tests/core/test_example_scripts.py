"""What the shipped Python scripts make, not merely that they compile: the discovery lanes assert only that no exception
escaped. tests/core/data/shipped_scripts.yaml states it."""
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from sophios.python_cwl_adapter import import_python_file

from .cases import DATA, match
from .helpers import REPO_ROOT
from .test_setup import home_with_shipped_tools, use_home


@pytest.mark.fast
def test_each_shipped_script_makes_what_it_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """Each script's `compiled` workflow, `printed` request and built `tools`, as data/shipped_scripts.yaml states
    them. A script names the shipped tools under `Path.home() / 'wic' / 'cwl_adapters'`, as the docs run it."""
    use_home(monkeypatch, home_with_shipped_tools(tmp_path, REPO_ROOT))
    for script, stated in yaml.safe_load((DATA / 'shipped_scripts.yaml').read_text(encoding='utf-8')).items():
        path = REPO_ROOT / script
        module = import_python_file(path.stem, path.resolve())
        capsys.readouterr()
        made: Any = getattr(module, stated['call'])(*stated.get('args', [])) if 'call' in stated else None
        if 'compiled' in stated:
            compiled = made.compile().cwl_workflow
            assert match(stated['compiled'], compiled), (script, compiled)
        if 'printed' in stated:
            printed = capsys.readouterr().out  # the compiler's progress, then the request
            request, _ = json.JSONDecoder().raw_decode(printed, printed.index('{'))
            assert match(stated['printed'], request), (script, request)
        for build, tool in stated.get('tools', {}).items():
            document = getattr(module, build)().to_cwl_document()
            document.pop('doc')
            assert match(tool, document), (script, build, document)


@pytest.mark.fast
@pytest.mark.needs_cwltool
def test_the_integrated_ichnaea_script_compiles_its_hinted_tool(tmp_path: Path) -> None:
    """The hinted tool compiles: the script builds a tool with a GPU hint and a Docker requirement, which cwl_utils
    renders as lists and compile() once crashed on; imported from a copy, as `workflow()` writes a .cwl beside its
    own file. The script also writes the tool with `validate=True`, which runs cwltool in-process."""
    source = tmp_path / 'ichnaea_integrated.py'
    shutil.copyfile(REPO_ROOT / 'examples' / 'scripts' / source.name, source)
    module = import_python_file(source.stem, source)
    inputs = {'input_dir': '/data/in', 'output_dir': '/data/out', 'model_dir': '/data/models'}
    run = module.workflow(inputs, 'autoseg_workflow').compile().cwl_workflow['steps'][0]['run']
    assert any(entry.get('class') == 'DockerRequirement' for entry in run['requirements'])
    assert any(entry.get('class') == 'cwltool:CUDARequirement' for entry in run['hints'])
