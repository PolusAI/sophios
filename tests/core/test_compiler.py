"""The compiler, judged on example workflows in files: each example of tests/core/data/examples compiles to the CWL its
expected.yaml states (every platform) and runs under cwltool to the outputs it states (POSIX); each example of
tests/core/data/compiler compiles, or is refused, as its expected.yaml states."""
import contextlib
import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from sophios.compiler import CompileSettings, compile_source, cwl_notes, resolved_view
from sophios.input_output import write_artifacts_to_disk
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.resolved import NotLowered
from sophios.lang.diagnostics import SophiosError
from sophios.runtime_inputs import output_target_inputs
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_nf import NEXTFLOW
from sophios.wic_types import StepId, Tool, Tools, Yaml

from .cases import DATA, diagnostic, match
from .helpers import cwltool_outputs, example_workflow, examples, files_beside, observed, stated
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


@pytest.mark.fast
@pytest.mark.parametrize('folder', examples())
def test_an_example_compiles_to_the_cwl_it_states(folder: Path, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Each example's `compiled` states its CWL's requirements and, step by step in order, what each step reads from
    another step or a workflow input, how it scatters and when it runs: a compiler that reversed a list of sources
    would reverse it for every runner, so only a stated document catches it."""
    workflow = example_workflow(folder, tmp_path / 'example')
    monkeypatch.chdir(tmp_path / 'example')
    with contextlib.redirect_stdout(io.StringIO()):
        cwl = workflow.compile().cwl_workflow
    expected = stated(folder)['compiled']
    steps = {step['id']: step for step in cwl['steps']}
    assert list(steps) == list(expected['steps'])
    assert match(expected, {**cwl, 'steps': steps, 'requirements': cwl.get('requirements', {})})


@pytest.mark.needs_cwltool
@pytest.mark.serial
@pytest.mark.parametrize('folder', [example for example in examples()
                                    if stated(example.values[0]).get('reference') != 'none'])
def test_an_example_runs_under_cwltool_as_stated(folder: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """cwltool, the reference runner, gives each example exactly the outputs its expected.yaml states by class,
    basename and contents, for the CWL the workflow compiles to, and leaves the files beside the workflow as they
    were: the statement is CWL's answer, which the Nextflow leg (test_runtime.py) is held to as well."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    directory = tmp_path / 'example'
    workflow = example_workflow(folder, directory)
    monkeypatch.chdir(directory)
    before = files_beside(directory)
    outputs = cwltool_outputs(cwltool.main.main, workflow, directory)
    assert {name: observed(value) for name, value in outputs.items()} == stated(folder)['outputs']
    assert {name: found for name, found in files_beside(directory).items() if name in before} == before


def _tools(directory: Path) -> Tools:
    """The synthetic tools, each tool beside the example under its stem, and each in a folder beside it under that
    folder's name as its namespace, desugared as discovery desugars them."""
    return {**SYNTHETIC_TOOLS, **{
        StepId(path.stem, SYNTHETIC_NS if path.parent == directory else path.parent.name): Tool(
            str(path), desugar_into_canonical_normal_form(yaml.safe_load(path.read_text(encoding='utf-8'))))
        for path in [*directory.glob('*.cwl'), *directory.glob('*/*.cwl')]}}


def _written(directory: Path) -> dict[str, Any]:
    """Every file written in `directory`, read as YAML."""
    return {path.name: yaml.safe_load(path.read_text(encoding='utf-8'))
            for path in sorted(directory.iterdir(), key=lambda path: path.name)}


def _compiled_as_stated(folder: Path, root: str, expected: Yaml, out: Path,
                        capsys: pytest.CaptureFixture[str]) -> None:
    """The root `.wic` of the example `folder`, compiled as the command line reads it, checked against `expected`;
    what it writes goes into `out`."""
    others = {'global': {path.stem: path for path in folder.glob('*.wic') if path.name != root}}
    bundled = bundle_from_disk(folder / root, others, _tools(folder))
    options = replace(CompileSettings(), **expected.get('options', {}))
    if 'refused' in expected:
        with pytest.raises(SophiosError) as caught:
            compile_source(bundled, options, quiet=True)
        assert match(expected['refused'], [diagnostic(found) for found in caught.value.diagnostics]), root
        return
    result = compile_source(bundled, options, quiet=False)
    notes = [*result.diagnostics, *cwl_notes(resolved_view(result))]
    assert match(expected.get('notes', []), [diagnostic(found) for found in notes]), root
    assert match(expected.get('said', []), capsys.readouterr().err.splitlines()), root
    write_artifacts_to_disk(result.artifact, out, output_targets=output_target_inputs(resolved_view(result)))
    if 'nextflow' in expected:
        job = folder / 'job.yml'
        values = yaml.safe_load(job.read_text(encoding='utf-8')) if job.exists() else {}
        try:
            NEXTFLOW.lower(resolved_view(result, values={name: value for name, value in values.items()
                                                         if name in result.artifact.cwl['inputs']}))
            refusals = []
        except NotLowered as refused:
            refusals = [{'step': found.step, 'says': found.message} for found in refused.refusals]
        assert match(expected['nextflow'], refusals), root


@pytest.mark.fast
@pytest.mark.parametrize('folder', [pytest.param(path, id=path.name)
                                    for path in sorted((DATA / 'compiler').iterdir()) if path.is_dir()])
def test_a_compiler_example_compiles_as_it_states(folder: Path, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """Each root `.wic` of a directory of tests/core/data/compiler, compiled as the command line reads it (the tools
    and workflows beside it, and the synthetic tools) under the compile `options` it states, is `refused` with the
    diagnostics stated, by code, severity, step and port; or compiles with exactly the `notes` stated and the lines
    `said` on stderr, writes the files `written` states into one directory shared by every root, so a root that
    overwrote another's file fails it, as the `files` named for the root and no others; and, where `nextflow` is
    stated, is refused by the Nextflow target, given the values the directory's `job.yml` holds for its inputs, as it
    states, by step and subject."""
    out = tmp_path / 'out'
    out.mkdir()
    roots: dict[str, Yaml] = stated(folder)
    for root, expected in roots.items():
        _compiled_as_stated(folder, root, expected, out, capsys)
    written = _written(out)
    for root, expected in roots.items():
        stem = Path(root).stem
        mine = [name for name in written if name.split('~')[0] in (f'{stem}.cwl', stem, f'{stem}_inputs.yml')]
        assert 'files' not in expected or mine == expected['files'], root
        assert match(expected.get('written', {}), written), root
