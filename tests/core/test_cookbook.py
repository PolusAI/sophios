"""Every cookbook pair (`docs/cookbook/<stem>.wic` and `.py`) compiles to one CWL, job inputs and note codes from both
surfaces, or a docs page tells one surface's reader something false."""
import re
import warnings
from collections.abc import Iterable
from pathlib import Path

import pytest

import sophios.compiler
from sophios.api.python.workflow import Inference
from sophios.lang import SophiosErrorCode
from sophios.lang.diagnostics import Severity, SophiosError, lines, report
from sophios.cli import default_compilation_settings
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.post_compile import inline_artifact_runs
from sophios.python_cwl_adapter import import_python_file
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.wic_types import CompilerOptions, Json

from .helpers import REPO_ROOT, shipped_tools
from .test_setup import home_with_shipped_tools, use_home
from .wic_reading import read_wic

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
    """The scripts name their tools under `Path.home() / 'wic' / 'cwl_adapters'`, as the docs tell a reader after
    `sophios --generate_config`; here that home holds this checkout's tools, so an empty home and CI pass alike."""
    use_home(monkeypatch, shipped_tools_home)


def _codes(diagnostics: Iterable[object]) -> list[str]:
    """The `wic0NN` codes in rendered diagnostics, in order."""
    return [code for text in map(str, diagnostics) for code in re.findall(r'\[(wic\d{3})\]', text)]


def _as_compile_returns(cwl: Json, wic_path: Path) -> Json:
    """`cwl` with the outputs `Workflow.compile()` returns: only the named ones, where the CLI also exposes every step
    output (docs/connecting-steps.md)."""
    named = read_wic(wic_path.read_text(encoding='utf-8'), wic_path.name).get('outputs')
    if not named:
        return cwl
    return {**cwl, 'outputs': {name: cwl['outputs'][name] for name in named}}


def _bodies(diagnostics: Iterable[object]) -> list[str]:
    """Each diagnostic as it is printed, but for the place it points at."""
    return [re.sub(r'^.+?:\d+:\d+: ', '', str(diagnostic)) for diagnostic in diagnostics]


def _compile_wic(root: Path, settings: CompilerOptions | None = None) -> CompilationResult:
    """`root` compiled as the CLI reads it, with the shipped tools and the `.wic` files beside it, a `run:` path read
    from beside the document."""
    return sophios.compiler.compile_source(
        bundle_from_disk(root, {'global': {path.stem: path for path in root.parent.glob('*.wic')}}, shipped_tools()),
        settings or default_compilation_settings(), testing=True)


def _through_the_file(root: Path) -> tuple[Json, Json, list[str]]:
    """CWL (outputs narrowed as `compile()` returns them), job and diagnostics of `root`, compiled by `_compile_wic`."""
    result = _compile_wic(root)
    artifact = inline_artifact_runs(result.artifact)
    return (_as_compile_returns(normalize_artifact_cwl(artifact), root),
            normalize_artifact_job_inputs(artifact, artifact.job_inputs), _bodies(result.diagnostics))


@pytest.mark.fast
@pytest.mark.parametrize('stem', [stem for stem in PAIRS if stem != REFUSED])
def test_both_surfaces_compile_to_the_same_cwl(stem: str) -> None:
    """The `.py` and the `.wic` of a pair emit one workflow, one job and one set of notes."""
    module = import_python_file(f'cookbook_{stem}', COOKBOOK / f'{stem}.py')
    python_side = module.workflow().compile()
    cwl, job, notes = _through_the_file(COOKBOOK / f'{stem}.wic')
    assert (cwl, job) == (python_side.cwl_workflow, python_side.cwl_job_inputs)
    assert _codes(notes) == _codes(python_side.diagnostics)


@pytest.mark.fast
def test_the_inferred_graph_stops_at_the_join() -> None:
    """With every edge left out, `combine` would read the counts as its text and as its counts; both surfaces
    stop there with `wic055`, naming the step, the inputs and the output."""
    module = import_python_file(f'cookbook_{REFUSED}', COOKBOOK / f'{REFUSED}.py')
    with pytest.raises(SophiosError) as from_python:
        module.workflow().compile()
    with pytest.raises(SophiosError) as from_wic:
        _compile_wic(COOKBOOK / f'{REFUSED}.wic')
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
        return {step['id']: {port: source['source'] if isinstance(source, dict) else source
                             for port, source in step['in'].items()}
                for step in _through_the_file(COOKBOOK / f'{stem}.wic')[0]['steps']}

    assert reads('greeting_graph_partial') == reads('greeting_graph')


@pytest.mark.fast
def test_both_surfaces_print_the_inference_note_alike(capsys: pytest.CaptureFixture[str]) -> None:
    """The inference entry shows a note; a pair that stopped making one would show nothing. The Python compile prints
    its notes on stderr, as the command line does, and returns the same objects; nothing is a warning. The two
    surfaces print the same block, each row at its own file's line."""
    module = import_python_file('cookbook_inference', COOKBOOK / 'inference.py')
    capsys.readouterr()
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        compiled = module.workflow().compile()
    from_python = capsys.readouterr().err.splitlines()
    report(_compile_wic(COOKBOOK / 'inference.wic').diagnostics)
    from_wic = capsys.readouterr().err.splitlines()

    (note,) = compiled.diagnostics
    (from_the_file,) = tuple(_compile_wic(COOKBOOK / 'inference.wic').diagnostics)
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
    ('naming', 'report', 'input_summary', None),
    ('naming_renamed', 'abstract', 'input_abstract', (('abstract', 'summary'),)),
])
def test_naming_conventions_give_the_summary_to_the_step_that_wants_it(
        stem: str, consumer: str, port: str, renamings: tuple[tuple[str, str], ...] | None) -> None:
    """With the naming conventions off the consumer takes `output_details`, the first of two files; by default, with the
    renaming where names differ, docs/edge-inference.md says it takes `output_summary`, from the `.wic` and `.py`."""
    module = import_python_file(f'cookbook_{stem}', COOKBOOK / f'{stem}.py')

    def source(compiled: Json) -> str:
        (step,) = [step for step in compiled['steps'] if step['id'] == consumer]
        binding = step['in'][port]
        return str(binding['source'] if isinstance(binding, dict) else binding)

    def from_the_file(settings: CompilerOptions) -> str:
        if renamings is not None:
            settings['renaming_conventions'] = list(renamings)
        return source(normalize_artifact_cwl(inline_artifact_runs(_compile_wic(COOKBOOK / f'{stem}.wic',
                                                                               settings).artifact)))

    off = default_compilation_settings()
    off['inference_use_naming_conventions'] = False
    assert from_the_file(off) == source(module.workflow().compile(
        inference=Inference(naming_conventions=False)).cwl_workflow) == 'summarize/output_details'
    assert from_the_file(default_compilation_settings()) == source(module.workflow().compile(
        inference=Inference(renamings=renamings)).cwl_workflow) == 'summarize/output_summary'
