"""Every cookbook pair compiles to one CWL from both surfaces.

`docs/cookbook/<stem>.wic` and `docs/cookbook/<stem>.py` are the two spellings
`docs/cookbook.md` shows side by side. Each pair is compiled through both doors,
the file the CLI reads and `Workflow.compile()`, and the emitted CWL, the job
inputs and the codes of the notes the compile made must agree. A pair that
drifts apart is a cookbook entry that tells one surface's reader something
false.
"""
import re
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml

import sophios.compiler
from sophios.cli import default_compilation_settings
from sophios.ir.frontdoor import bundle_from_disk
from sophios.post_compile import inline_artifact_runs
from sophios.python_cwl_adapter import import_python_file
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Json

from .test_setup import load_test_registry

COOKBOOK = Path(__file__).resolve().parents[2] / 'docs' / 'cookbook'
#: A pair is a `.py` beside a `.wic` of the same stem. A `.wic` without a `.py`
#: is a subworkflow a pair calls; its Python lives in the caller's script.
PAIRS = sorted(path.stem for path in COOKBOOK.glob('*.py'))


def _codes(diagnostics: Iterable[object]) -> list[str]:
    """The `wic0NN` codes in rendered diagnostics, in order."""
    return [code for text in map(str, diagnostics) for code in re.findall(r'\[(wic\d{3})\]', text)]


def _as_compile_returns(cwl: Json, wic_path: Path) -> Json:
    """`cwl` with the outputs `Workflow.compile()` returns for the same workflow.

    A workflow that names outputs returns only those from `compile()`; the CLI
    also exposes every step output under its generated name. That is the one
    place the two doors differ, and `docs/cookbook.md` says so.
    """
    named = yaml.load(wic_path.read_text(encoding='utf-8'), Loader=wic_loader()).get('outputs')
    if not named:
        return cwl
    return {**cwl, 'outputs': {name: cwl['outputs'][name] for name in named}}


@pytest.mark.fast
@pytest.mark.parametrize('stem', PAIRS)
def test_both_surfaces_compile_to_the_same_cwl(stem: str) -> None:
    """The `.py` and the `.wic` of a pair emit one workflow, one job and one set of notes."""
    module = import_python_file(f'cookbook_{stem}', COOKBOOK / f'{stem}.py')
    python_side = module.workflow().compile()

    wic_path = COOKBOOK / f'{stem}.wic'
    workflows = {'global': {path.stem: path for path in COOKBOOK.glob('*.wic')}}
    bundle = bundle_from_disk(wic_path, workflows, load_test_registry().tools)
    result = sophios.compiler.compile_source(bundle, default_compilation_settings(),
                                             relative_run_path=True, testing=True)
    artifact = inline_artifact_runs(result.artifact)

    assert _as_compile_returns(normalize_artifact_cwl(artifact), wic_path) == python_side.cwl_workflow
    assert normalize_artifact_job_inputs(artifact, artifact.job_inputs) == python_side.cwl_job_inputs
    assert _codes(result.diagnostics) == _codes(python_side.diagnostics)


@pytest.mark.fast
def test_every_construct_has_a_pair() -> None:
    """The cookbook covers each construct, each with both spellings."""
    assert set(PAIRS) == {'inline_literal', 'workflow_input', 'explicit_edge', 'raw_cwl', 'record',
                          'scatter', 'when', 'nested', 'inference', 'named_output', 'renamed_step'}
    for stem in PAIRS:
        assert (COOKBOOK / f'{stem}.wic').is_file()


@pytest.mark.fast
def test_the_inference_pair_prints_its_note() -> None:
    """The inference entry shows a note; a pair that stopped making one would show nothing."""
    module = import_python_file('cookbook_inference', COOKBOOK / 'inference.py')
    assert _codes(module.workflow().compile().diagnostics) == ['wic043']


@pytest.mark.fast
def test_the_note_the_cookbook_quotes_is_the_one_the_compiler_prints() -> None:
    """`docs/cookbook.md` quotes the inference note; the quote must be the compiler's text."""
    module = import_python_file('cookbook_inference', COOKBOOK / 'inference.py')
    (note,) = map(str, module.workflow().compile().diagnostics)
    message = note.split(' note ', 1)[1]
    assert f'note {message}' in (COOKBOOK.parent / 'cookbook.md').read_text(encoding='utf-8')
