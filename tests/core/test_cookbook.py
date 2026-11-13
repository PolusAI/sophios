"""Every pair, a `.py` and a `.wic` of one stem, compiles to one CWL, job and list of notes from both surfaces, and the
bundle the `.py`'s objects write compiles to it too; each `expected.yaml` beside the pairs states what some of them
compile to, under the flags and the `compile()` arguments it names. The pairs are the cookbook's
(`docs/cookbook`), which the docs show, and tests/core/data/python_api_pairs; the docs show the files as they are."""
import re
import warnings
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

import sophios.compiler
from sophios import cli
from sophios.api.python.workflow import Inference, RunPolicy, Workflow
from sophios.lang.diagnostics import Diagnostic, SophiosError, lines
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.post_compile import inline_artifact_runs
from sophios.python_cwl_adapter import import_python_file
from sophios.runtime_inputs import output_target_inputs, runner_job_inputs
from sophios.wic_types import Json

from .cases import DATA, diagnostic, match
from .helpers import REPO_ROOT, shipped_tools
from .test_setup import home_with_shipped_tools, use_home
from .wic_reading import read_wic

COOKBOOK = REPO_ROOT / 'docs' / 'cookbook'
PYTHON_PAIRS = DATA / 'python_api_pairs'


#: What a stated compile may say.
_STATED = {'why', 'under', 'flags', 'settings', 'inference', 'policy', 'notes', 'reads', 'refused', 'compiled',
           'document'}


def _pairs() -> list[tuple[str, Callable[[], Workflow], Path, list[dict[str, Any]]]]:
    """Each pair: its name, what builds its objects, its `.wic`, and the compiles tests/core/data/pairs states of it:
    the default compile, then each it lists `under:` other flags and `compile()` arguments. A cookbook `.py` builds
    the objects with `workflow()`; tests/core/data/python_api_pairs/pairs.py holds one function per pair. A `.wic`
    with no `.py` is a workflow a pair calls."""
    paired = import_python_file('python_api_pairs', PYTHON_PAIRS / 'pairs.py')
    found = []
    for directory, builders in ((COOKBOOK, {path.stem: path for path in COOKBOOK.glob('*.py')}),
                                (PYTHON_PAIRS, {name: getattr(paired, name) for name in dir(paired)
                                                if (PYTHON_PAIRS / f'{name}.wic').is_file()})):
        stated = yaml.safe_load((DATA / 'pairs' / f'{directory.name}.yaml').read_text(encoding='utf-8'))
        for stem, build in sorted(builders.items()):
            if isinstance(build, Path):
                build = import_python_file(f'{directory.name}_{stem}', build).workflow
            default = stated.pop(stem, {})
            for each in (default, *default.get('under', [])):
                assert set(each) <= _STATED, f'{directory.name}.yaml: {stem} states {sorted(set(each) - _STATED)}'
            found.append((stem, build, directory / f'{stem}.wic', [default, *default.pop('under', [])]))
        assert not stated, f'{directory.name}.yaml states pairs that are not there: {sorted(stated)}'
    return found


@pytest.fixture(name='shipped_tools_home')
def _shipped_tools_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cookbook scripts name their tools under `Path.home() / 'wic' / 'cwl_adapters'`, as the docs tell a reader
    after `sophios --generate_config`; here that home holds this checkout's tools, so an empty home and CI pass
    alike."""
    use_home(monkeypatch, home_with_shipped_tools(tmp_path / 'home', REPO_ROOT))


def _compile_wic(root: Path, settings: sophios.compiler.CompileSettings) -> CompilationResult:
    """`root` compiled as the command line reads it, with the shipped tools and the `.wic` files beside it."""
    return sophios.compiler.compile_source(
        bundle_from_disk(root, {'global': {path.stem: path for path in root.parent.glob('*.wic')}}, shipped_tools()),
        settings, quiet=True)


def _bodies(diagnostics: Any) -> list[str]:
    """Each diagnostic as it is printed, but for the place it points at, which is the surface's own."""
    return [re.sub(r'^.+?:\d+:\d+: ', '', str(item)) for item in diagnostics]


def _refusal(error: SophiosError) -> list[dict[str, Any]]:
    """The diagnostics of `error` as cases state them, but for the place each points at."""
    return [{key: value for key, value in diagnostic(item).items() if key not in ('file', 'line', 'column',
                                                                                  'end_line', 'end_column')}
            for item in error.diagnostics]


def _through_the_file(root: Path, settings: sophios.compiler.CompileSettings) -> Any:
    """CWL, job and notes of `root`, compiled as the command line does, the CWL's outputs narrowed to the named ones
    as `compile()` returns them (docs/connecting-steps.md); or how it is refused."""
    try:
        result = _compile_wic(root, settings)
    except SophiosError as error:
        return _refusal(error)
    artifact = inline_artifact_runs(result.artifact)
    cwl = {**artifact.cwl}
    if named := read_wic(root.read_text(encoding='utf-8'), root.name).get('outputs'):
        cwl['outputs'] = {name: cwl['outputs'][name] for name in named}
    job = runner_job_inputs(artifact.job_inputs, output_target_inputs(sophios.compiler.resolved_view(result)))
    return cwl, job, _bodies(result.diagnostics)


def _reads(cwl: Json) -> dict[str, dict[str, Any]]:
    """What each compiled step reads, by its input."""
    return {step['id']: {port: source.get('source') if isinstance(source, dict) else source
                         for port, source in step['in'].items()} for step in cwl['steps']}


def _from_python(build: Callable[[], Workflow], stated: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> Any:
    """CWL, job and notes of the objects `build` makes, compiled with the stated `compile()` arguments, or how they
    are refused; the compile prints its notes on stderr as the command line does, and warns of nothing."""
    workflow = build()
    capsys.readouterr()
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        try:
            compiled = workflow.compile(inference=Inference(**stated.get('inference', {})),
                                        policy=RunPolicy(**stated.get('policy', {})))
        except SophiosError as error:
            return _refusal(error)
    assert capsys.readouterr().err.splitlines() == lines(compiled.diagnostics)
    return compiled.cwl_workflow, compiled.cwl_job_inputs, _bodies(compiled.diagnostics)


@pytest.mark.fast
@pytest.mark.usefixtures('shipped_tools_home')
def test_every_pair_compiles_alike_from_both_surfaces_and_as_stated(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each compile of a pair gives the `.wic`, compiled with the stated `flags` and `settings`, and the `.py`, with
    the stated `inference` and `policy`, one CWL, job and list of diagnostics, or one refusal. Each states what it
    gives: the `notes`, every diagnostic as printed but for its place (none unless stated); as it applies, what
    steps `reads`, how it is `refused`, the `compiled` document in part, or the whole `document` in a file. The
    bundle the objects write compiles to what they do."""
    defaults = cli.settings_from_args(cli.get_args(''))[0]
    for stem, build, wic, compiles in _pairs():
        for stated in compiles:
            from_python = _from_python(build, stated, capsys)
            settings = cli.settings_from_args(cli.get_args('', stated.get('flags', [])))[0]
            settings = replace(settings, inference=replace(settings.inference, **stated.get('settings', {})))
            assert _through_the_file(wic, settings) == from_python, (stem, stated)
            if isinstance(from_python, list):
                assert match(stated.get('refused', []), from_python), (stem, from_python)
                continue
            cwl, _job, notes = from_python
            assert notes == stated.get('notes', []), (stem, notes)
            assert match(stated.get('reads', {}), _reads(cwl)), (stem, _reads(cwl))
            assert match(stated.get('compiled', {}), cwl), (stem, cwl)
            if 'document' in stated:
                assert cwl == yaml.safe_load((DATA / 'pairs' / stated['document']).read_text(encoding='utf-8')), stem
        if 'refused' not in compiles[0]:
            assert _through_the_file(build().write_wic(tmp_path / stem), defaults) == _from_python(build, {}, capsys)


@pytest.mark.fast
@pytest.mark.usefixtures('shipped_tools_home')
def test_the_docs_show_the_files_and_the_notes_as_they_are() -> None:
    """A README block headed `# <name>` is that file, byte for byte; and `docs/edge-inference.md` quotes the note
    Python prints for the greeting with its edges left out as the compiler prints it."""
    sources = {'greeting.wic': COOKBOOK / 'greeting.wic', 'greeting.py': COOKBOOK / 'greeting.py',
               'count_words.py': REPO_ROOT / 'examples' / 'scripts' / 'count_words.py'}
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
    shown = {block.split('\n', 1)[0][2:]: block.split('\n', 1)[1]
             for block in re.findall(r'```(?:yaml|python)\n(# \S+\n.*?)```', readme, re.DOTALL)}
    assert shown == {name: path.read_text(encoding='utf-8') for name, path in sources.items()}

    module = import_python_file('cookbook_greeting_inferred', COOKBOOK / 'greeting_inferred.py')
    notes = module.workflow().compile().diagnostics
    (note,) = notes
    assert isinstance(note, Diagnostic) and note.span is not None
    row = f'greeting_inferred.py:{note.span.start_line}:{note.span.start_column}  {note.message}\n'
    page = (REPO_ROOT / 'docs' / 'edge-inference.md').read_text(encoding='utf-8')
    assert f'{lines(notes)[0]}\n  /home/you/sophios-tutorial/{row}' in page
