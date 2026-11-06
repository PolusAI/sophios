"""Every cookbook pair compiles to one CWL from both surfaces.

`docs/cookbook/<stem>.wic` and `docs/cookbook/<stem>.py` are the two spellings
the documentation shows side by side. Each pair is compiled through both doors,
the file the CLI reads and `Workflow.compile()`, and the emitted CWL, the job
inputs and the codes of the notes the compile made must agree. A pair that
drifts apart is a cookbook entry that tells one surface's reader something
false.
"""
import re
import warnings
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pytest

import sophios.compiler
import sophios.plugins
from sophios.api.python.workflow import Inference
from sophios.lang import SophiosErrorCode
from sophios.lang.diagnostics import Severity, SophiosError, lines, report
from sophios.cli import default_compilation_settings
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.post_compile import inline_artifact_runs
from sophios.python_cwl_adapter import import_python_file
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.wic_types import CompilerOptions, Json, Tools

from .test_setup import home_with_shipped_tools, use_home
from .wic_reading import read_wic

REPO_ROOT = Path(__file__).resolve().parents[2]
COOKBOOK = REPO_ROOT / 'docs' / 'cookbook'
#: A pair is a `.py` beside a `.wic` of the same stem. A `.wic` without a `.py`
#: is a subworkflow a pair calls; its Python lives in the caller's script.
PAIRS = sorted(path.stem for path in COOKBOOK.glob('*.py'))
#: The pair that stops with an error, which `test_the_inferred_graph_stops_at_the_join` pins on both surfaces.
REFUSED = 'greeting_graph_inferred'


@pytest.fixture(scope='module', name='shipped_tools_home')
def _shipped_tools_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A home whose `wic/cwl_adapters` holds this checkout's tools."""
    return home_with_shipped_tools(tmp_path_factory.mktemp('home'), REPO_ROOT)


@pytest.fixture(autouse=True)
def _with_the_shipped_tools_at_home(monkeypatch: pytest.MonkeyPatch, shipped_tools_home: Path) -> None:
    """The scripts name their tools under `Path.home() / 'wic' / 'cwl_adapters'`, as the documentation
    tells a reader to run them after `sophios --generate_config`; here that home holds this checkout's
    tools, whatever the real home holds, so the test passes in an empty home and in CI."""
    use_home(monkeypatch, shipped_tools_home)


@lru_cache(maxsize=1)
def _shipped_tools() -> Tools:
    """The tools this checkout ships, by stem, as a `.wic` step finds them after
    `sophios --generate_config`: read from the repository, not from whatever the home holds."""
    return sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(REPO_ROOT / 'cwl_adapters')]}})


def _codes(diagnostics: Iterable[object]) -> list[str]:
    """The `wic0NN` codes in rendered diagnostics, in order."""
    return [code for text in map(str, diagnostics) for code in re.findall(r'\[(wic\d{3})\]', text)]


def _as_compile_returns(cwl: Json, wic_path: Path) -> Json:
    """`cwl` with the outputs `Workflow.compile()` returns for the same workflow.

    A workflow that names outputs returns only those from `compile()`; the CLI
    also exposes every step output under its generated name. That is the one
    place the two doors differ, and `docs/connecting-steps.md` says so.
    """
    named = read_wic(wic_path.read_text(encoding='utf-8'), wic_path.name).get('outputs')
    if not named:
        return cwl
    return {**cwl, 'outputs': {name: cwl['outputs'][name] for name in named}}


@pytest.mark.fast
@pytest.mark.parametrize('stem', [stem for stem in PAIRS if stem != REFUSED])
def test_both_surfaces_compile_to_the_same_cwl(stem: str) -> None:
    """The `.py` and the `.wic` of a pair emit one workflow, one job and one set of notes."""
    module = import_python_file(f'cookbook_{stem}', COOKBOOK / f'{stem}.py')
    python_side = module.workflow().compile()

    wic_path = COOKBOOK / f'{stem}.wic'
    workflows = {'global': {path.stem: path for path in COOKBOOK.glob('*.wic')}}
    bundle = bundle_from_disk(wic_path, workflows, _shipped_tools())
    result = sophios.compiler.compile_source(bundle, default_compilation_settings(),
                                             testing=True)
    artifact = inline_artifact_runs(result.artifact)

    assert _as_compile_returns(normalize_artifact_cwl(artifact), wic_path) == python_side.cwl_workflow
    assert normalize_artifact_job_inputs(artifact, artifact.job_inputs) == python_side.cwl_job_inputs
    assert _codes(result.diagnostics) == _codes(python_side.diagnostics)


def _compile_wic(stem: str) -> CompilationResult:
    """The `.wic` of a cookbook pair, compiled as the command line compiles it."""
    wic_path = COOKBOOK / f'{stem}.wic'
    workflows = {'global': {path.stem: path for path in COOKBOOK.glob('*.wic')}}
    bundle = bundle_from_disk(wic_path, workflows, _shipped_tools())
    return sophios.compiler.compile_source(bundle, default_compilation_settings(), testing=True)


@pytest.mark.fast
def test_the_inferred_graph_stops_at_the_join() -> None:
    """With every edge left out, `combine` would read the counts as its text and as its counts; both surfaces
    stop there with `wic055`, naming the step, the inputs and the output."""
    module = import_python_file(f'cookbook_{REFUSED}', COOKBOOK / f'{REFUSED}.py')
    with pytest.raises(SophiosError) as from_python:
        module.workflow().compile()
    with pytest.raises(SophiosError) as from_wic:
        _compile_wic(REFUSED)
    for caught in (from_python, from_wic):
        (error,) = [d for d in caught.value.diagnostics if d.severity is Severity.ERROR]
        assert error.code is SophiosErrorCode.INFERENCE_DUPLICATE
        assert error.message == ('combine.text and combine.counts would both read word_count.counts; '
                                 'write the edge each should read')


@pytest.mark.fast
def test_the_partial_graph_is_the_explicit_graph() -> None:
    """Two written edges, at the branch and at the join, leave inference the other three; the compiled
    workflow has every step reading what `greeting_graph.wic` writes out."""
    def reads(stem: str) -> dict[str, dict[str, str]]:
        steps = normalize_artifact_cwl(inline_artifact_runs(_compile_wic(stem).artifact))['steps']
        return {step['id']: {port: source['source'] if isinstance(source, dict) else source
                             for port, source in step['in'].items()} for step in steps}

    assert reads('greeting_graph_partial') == reads('greeting_graph')


@pytest.mark.fast
def test_the_inference_pair_prints_its_note() -> None:
    """The inference entry shows a note; a pair that stopped making one would show nothing."""
    module = import_python_file('cookbook_inference', COOKBOOK / 'inference.py')
    assert _codes(module.workflow().compile().diagnostics) == ['wic043']


@pytest.mark.fast
def test_both_surfaces_print_the_inference_note_alike(capsys: pytest.CaptureFixture[str]) -> None:
    """The Python compile prints its notes on stderr, as the command line does, and returns the same objects;
    nothing is a warning. The two surfaces print the same block, each row at its own file's line."""
    module = import_python_file('cookbook_inference', COOKBOOK / 'inference.py')
    capsys.readouterr()
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        compiled = module.workflow().compile()
    from_python = capsys.readouterr().err.splitlines()
    report(_compile_wic('inference').diagnostics)
    from_wic = capsys.readouterr().err.splitlines()

    (note,) = compiled.diagnostics
    (from_the_file,) = tuple(_compile_wic('inference').diagnostics)
    assert from_python == lines(compiled.diagnostics) and from_wic == lines([from_the_file])
    assert from_python[0] == from_wic[0] == ('note [wic043] 1 edge was guessed; draw the workflow to check it '
                                             '(--graphviz, or Workflow.draw() in Python)')
    assert note.message == from_the_file.message == 'cat.file <- echo.stdout (1 other fits)'


@pytest.mark.fast
def test_the_note_the_docs_quote_is_the_one_the_compiler_prints() -> None:
    """`docs/edge-inference.md` quotes the note of the greeting with its edges left out, as Python prints it;
    the quote must be the compiler's text."""
    module = import_python_file('cookbook_greeting_inferred', COOKBOOK / 'greeting_inferred.py')
    diagnostics = module.workflow().compile().diagnostics
    (note,) = diagnostics
    assert note.span is not None
    header = lines(diagnostics)[0]
    row = f'greeting_inferred.py:{note.span.start_line}:{note.span.start_column}  {note.message}\n'
    page = (REPO_ROOT / 'docs' / 'edge-inference.md').read_text(encoding='utf-8')
    assert f'{header}\n  /home/you/sophios-tutorial/{row}' in page


@pytest.mark.fast
def test_the_readme_shows_the_files_as_they_are() -> None:
    """A README block headed `# <name>` is that file, byte for byte, so the README cannot drift from the pair
    and the example the documentation runs."""
    sources = {'greeting.wic': COOKBOOK / 'greeting.wic', 'greeting.py': COOKBOOK / 'greeting.py',
               'count_words.py': REPO_ROOT / 'examples' / 'scripts' / 'count_words.py'}
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
    shown = {block.split('\n', 1)[0][2:]: block.split('\n', 1)[1]
             for block in re.findall(r'```(?:yaml|python)\n(# \S+\n.*?)```', readme, re.DOTALL)}
    assert shown == {name: path.read_text(encoding='utf-8') for name, path in sources.items()}


@pytest.mark.fast
@pytest.mark.parametrize(('stem', 'consumer', 'port', 'renamings'), [
    ('naming', 'report', 'input_summary', []),
    ('naming_renamed', 'abstract', 'input_abstract', [('abstract', 'summary')]),
])
def test_naming_conventions_give_the_summary_to_the_step_that_wants_it(
        stem: str, consumer: str, port: str, renamings: list[tuple[str, str]]) -> None:
    """By type alone the consumer takes `output_details`, the first of two files; with the naming conventions,
    and the renaming where the names differ, `docs/edge-inference.md` says it takes `output_summary`."""
    wic_path = COOKBOOK / f'{stem}.wic'
    workflows = {'global': {path.stem: path for path in COOKBOOK.glob('*.wic')}}

    def source(settings: CompilerOptions) -> str:
        bundle = bundle_from_disk(wic_path, workflows, _shipped_tools())
        result = sophios.compiler.compile_source(bundle, settings, testing=True)
        (step,) = [step for step in normalize_artifact_cwl(inline_artifact_runs(result.artifact))['steps']
                   if step['id'] == consumer]
        binding = step['in'][port]
        return str(binding['source'] if isinstance(binding, dict) else binding)

    without_conventions = default_compilation_settings()
    without_conventions['inference_use_naming_conventions'] = False
    assert source(without_conventions) == 'summarize/output_details'
    with_conventions = default_compilation_settings()
    with_conventions['renaming_conventions'] = renamings
    assert source(with_conventions) == 'summarize/output_summary'


@pytest.mark.fast
@pytest.mark.parametrize(('stem', 'consumer', 'port', 'inference'), [
    ('naming', 'report', 'input_summary', Inference()),
    ('naming_renamed', 'abstract', 'input_abstract', Inference(renamings=(('abstract', 'summary'),))),
])
def test_python_naming_conventions_give_the_summary_to_the_step_that_wants_it(
        stem: str, consumer: str, port: str, inference: Inference) -> None:
    """The Python half of the naming pairs: by type alone the consumer takes `output_details`; with the
    `Inference` docs/edge-inference.md shows, it takes `output_summary`, as the command line does."""
    module = import_python_file(f'cookbook_{stem}', COOKBOOK / f'{stem}.py')

    def source(compiled: Json) -> str:
        (step,) = [step for step in compiled['steps'] if step['id'] == consumer]
        binding = step['in'][port]
        return str(binding['source'] if isinstance(binding, dict) else binding)

    assert source(module.workflow().compile(
        inference=Inference(naming_conventions=False)).cwl_workflow) == 'summarize/output_details'
    with_conventions = module.workflow().compile(inference=inference).cwl_workflow
    assert source(with_conventions) == 'summarize/output_summary'
