"""A tool that refers to files beside it is written packed, so it means the same wherever it is written. The project
is tests/core/data/packing, compiled and run where it is from a working directory of the test's own: what is packed
is checked on every leg, and the runs show that the written descriptions run."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from schema_salad.fetcher import DefaultFetcher

from sophios import run_local
from sophios.api.python.workflow import Step, Workflow
from sophios.compiler import CompileSettings, compile_source
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.plugins import get_tools_cwl
from sophios.wic_types import StepId, Tools

from .cases import DATA
from .helpers import ADAPTERS, EACH_RUNNER, no_preflight, run_cli, run_local_process, search_config

PROJECT: Final = DATA / 'packing'
TOOLS: Final = PROJECT / 'tools'
#: What `relcat` includes and prints.
SCRIPT: Final = (TOOLS / 's.sh').read_text(encoding='utf-8')
PRINTED: Final = 'from-include\n' + (TOOLS / 'data.txt').read_text(encoding='utf-8')


def _compile(wic: Path, tools: Tools | None = None) -> CompilationArtifact:
    return compile_source(bundle_from_disk(wic, {}, tools or {}), CompileSettings(),
                          quiet=True).artifact


def _tool(artifact: CompilationArtifact, step: str) -> dict[str, Any]:
    """The description the artifact carries for `step`'s tool."""
    child, = (child for child in artifact.children if child.namespace == (step,))
    return child.cwl


def _requirement(tool: dict[str, Any], section: str, name: str) -> Any:
    """A requirement or hint by its class, whether the section is written as a mapping or as a list."""
    entries = tool[section]
    if isinstance(entries, list):
        entries = {item['class']: {key: value for key, value in item.items() if key != 'class'} for item in entries}
    return entries[name]


def _entry(tool: dict[str, Any]) -> Any:
    """`relcat`'s script entry."""
    return _requirement(tool, 'requirements', 'InitialWorkDirRequirement')['listing'][0]['entry']


@pytest.mark.fast
# pylint: disable-next=too-many-locals,too-many-statements
def test_a_tool_that_refers_to_files_beside_it_is_written_packed_by_every_door(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step running `relcat` is described packed: its include its text, its default an absolute URI; the
    registry still holds the tool as read. The same inline body in two `.wic` files in two directories includes each
    one's own `s.sh`. Inference reads `make` as written, so its `edam:format_2330` output feeds `use`: no new root
    input. Only a tool a step runs is packed: one on the search path that cannot be costs nothing, until a step runs
    it, when cwltool's own message is the error, at the line of the step. Packing never fetches a tool's `$schemas`.
    The command line's tree and its one embedded document carry a Dockerfile itself, and the tree keeps the tool's own
    `v1.0`; Python's `compile()`, the document `run()` writes, and a real-time analysis's tool are packed alike."""
    registry = get_tools_cwl({'search_paths_cwl': {'global': [str(TOOLS)]}})
    artifact = _compile(PROJECT / 'relcat.wic', registry)
    assert [_entry(child.cwl) for child in artifact.children] == [SCRIPT] * 3
    written = artifact.children[0].cwl
    assert written['inputs']['extra']['default'] == {'class': 'File', 'location': (TOOLS / 'data.txt').as_uri()}
    assert written['cwlVersion'] == 'v1.2'
    assert _entry(registry[StepId('relcat', 'global')].cwl) == {'$include': 's.sh'}

    artifact = _compile(PROJECT / 'a' / 'w.wic')
    inner, = (child for child in artifact.children if child.namespace == ('inner',))
    included = _entry(_tool(artifact, 'say')), _entry(_tool(inner, 'say'))
    assert included == ('#!/bin/sh\necho a\n', '#!/bin/sh\necho b\n')

    artifact = _compile(PROJECT / 'make_use.wic')
    use, = (step for step in artifact.cwl['steps'] if step['id'] == 'use')
    assert use['in'] == {'file': 'make/out'} and not artifact.cwl['inputs']
    assert _tool(artifact, 'make')['inputs']['seed']['default']['location'] == (TOOLS / 'seed.txt').as_uri()

    broken = get_tools_cwl({'search_paths_cwl': {'global': [str(PROJECT / 'broken')]}})
    artifact = _compile(PROJECT / 'broken' / 'unused.wic', broken)
    assert [child.namespace for child in artifact.children] == [('echo',)]
    assert _tool(artifact, 'echo') == broken[StepId('echo', 'global')].cwl, \
        'nothing to resolve, so the tool is emitted as the loader read it'
    with pytest.raises(SophiosError) as raised:
        _compile(PROJECT / 'broken' / 'used.wic', broken)
    diagnostic, = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    assert diagnostic.locator is not None and (diagnostic.locator.step, diagnostic.locator.index) == ('broken', 2)
    assert diagnostic.span is not None and diagnostic.span.start_line == 3
    assert str(PROJECT / 'broken' / 'broken.cwl') in diagnostic.message
    # The OSError as schema-salad passes it on. On Windows the error's filename, spelled with backslashes,
    # differs from the URI's path, so schema-salad prefixes it with `Error reading <uri>: `.
    reason = diagnostic.message.splitlines()[-1].strip()
    assert "[Errno 2] No such file or directory: '" in reason and reason.endswith("missing.sh'")

    fetched: list[str] = []
    fetch_text = DefaultFetcher.fetch_text

    def spy(self: DefaultFetcher, url: str, content_types: list[str] | None = None) -> str:
        fetched.append(url)
        return fetch_text(self, url, content_types)
    monkeypatch.setattr(DefaultFetcher, 'fetch_text', spy)
    artifact = _compile(PROJECT / 'ontology.wic', get_tools_cwl({'search_paths_cwl': {'global': [str(TOOLS)]}}))
    assert _tool(artifact, 'ontology')['$schemas'] == ['http://schemas.invalid/ontology.owl']
    assert not [url for url in fetched if 'schemas.invalid' in url]

    monkeypatch.chdir(tmp_path)
    config = search_config(tmp_path / 'config.json', [TOOLS, ADAPTERS / 'file_watchers'], [PROJECT])
    for flags in ((), ('--cwl_inline_runtag',)):
        assert run_cli(monkeypatch, '--yaml', PROJECT / 'built.wic', '--config_file', config,
                       '--generate_cwl_workflow', '--basepath', 'out', *flags) is None
        root = yaml.safe_load(Path('out/built.cwl').read_text(encoding='utf-8'))
        tool = root['steps'][0]['run'] if flags else yaml.safe_load(Path('out/built~built.cwl').read_text(
            encoding='utf-8'))
        docker = _requirement(tool, 'hints', 'DockerRequirement')
        assert docker == {'dockerImageId': 'built_here', 'dockerFile': 'FROM alpine:3.20\nRUN echo built\n'}, flags
        assert flags or tool['cwlVersion'] == 'v1.0'

    monkeypatch.setattr(run_local, 'generate_run_script', lambda _cmdline: None)
    no_preflight(monkeypatch)
    relcat = TOOLS / 'relcat.cwl'
    compiled = Workflow([Step(clt_path=relcat)], 'py').compile().cwl_workflow
    Workflow([Step(clt_path=relcat)], 'py').run(basepath='ran', run_args_dict={'generate_run_script': 'yes'})
    ran = yaml.safe_load(Path('ran/py.cwl').read_text(encoding='utf-8'))
    assert run_cli(monkeypatch, '--yaml', PROJECT / 'live.wic', '--config_file', config,
                   '--generate_cwl_workflow') is None
    analysis, = Path('autogenerated/realtime/live').glob('*~relcat.cwl')
    for where, written in (('compile()', compiled['steps'][0]['run']), ('run()', ran['steps'][0]['run']),
                           ('analysis', yaml.safe_load(analysis.read_text(encoding='utf-8')))):
        assert _entry(written) == SCRIPT, where
        assert written['inputs']['extra']['default']['location'] == (TOOLS / 'data.txt').as_uri(), where


def _produced() -> list[str]:
    """What the run's output files hold, in order: cwltool and Toil each write them to an outdir of their own."""
    return sorted(path.read_text(encoding='utf-8') for path in Path('autogenerated').glob('outdir_*/**/*')
                  if path.is_file())


def _hashes(root: Path) -> dict[str, tuple[str, int]]:
    return {path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.needs_cwltool
def test_a_packed_project_runs_under_cwltool_and_changes_nothing_beside_it(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Compiling and running packed tools reads the tools, scripts and data where they are and writes none of them,
    nor anything beside them. `--partial_failure_enable` rewrites the packed description, its output optional and
    exit 1 a success, in the tree and embedded, and the tree runs. The conformance tool whose record type is
    `$import`ed from a file beside it is written runnable: the description the tree holds for its step runs, given the
    conformance job."""
    before = _hashes(PROJECT) | _hashes(DATA / 'cwl-v1.2')
    monkeypatch.chdir(tmp_path)
    run_local_process(PROJECT / 'relcat_and_config.wic', check=True)
    assert _produced() == ['config of A\n', PRINTED]

    run_local_process(PROJECT / 'one.wic', '--partial_failure_enable', '--basepath', 'tolerant', check=True)
    tree = yaml.safe_load(Path('tolerant/one~relcat.cwl').read_text(encoding='utf-8'))
    assert sorted(path.read_text(encoding='utf-8') for path in Path('tolerant').glob('outdir_*/**/*')
                  if path.is_file()) == [PRINTED]
    run_local_process(PROJECT / 'one.wic', '--partial_failure_enable', '--cwl_inline_runtag', '--basepath', 'embedded',
                      check=True)
    embedded = yaml.safe_load(Path('embedded/one.cwl').read_text(encoding='utf-8'))['steps'][0]['run']
    for written in (tree, embedded):
        assert _entry(written) == SCRIPT
        assert written['outputs']['out']['type'] == 'File?'
        assert written['successCodes'] == [0, 1]

    assert run_cli(monkeypatch, '--yaml', PROJECT / 'schemadef.wic', '--generate_cwl_workflow', '--basepath',
                   'schemadef') is None
    ran = subprocess.run([sys.executable, '-c', 'import sys, cwltool.main; sys.exit(cwltool.main.main())',
                          '--outdir', 'out', 'schemadef/schemadef~sd.cwl', str(PROJECT / 'schemadef.yml')],
                         capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)['out'] == '1970-01-01T00:00:00Z\n'
    assert _hashes(PROJECT) | _hashes(DATA / 'cwl-v1.2') == before


FORMS: Final = {'tree': (), 'embedded': ('--cwl_inline_runtag',)}


@pytest.mark.needs_cwltool
@EACH_RUNNER
@pytest.mark.parametrize('form', FORMS)
def test_a_packed_tool_runs_from_the_tree_and_embedded_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             runner: str, form: str) -> None:
    """Two steps run `relcat`, from the tree's `w~<step>.cwl` and from one embedded document: each prints its
    included script's line and its default's data."""
    monkeypatch.chdir(tmp_path)
    run_local_process(PROJECT / 'two.wic', '--cwl_runner', runner, *FORMS[form], check=True)
    assert _produced() == [PRINTED, PRINTED]


@pytest.mark.needs_cwltool
@EACH_RUNNER
def test_a_packed_tool_and_one_as_written_run_wired_by_inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 runner: str) -> None:
    """`make`, packed, feeds `use`, as written, through the edge inference made; `use` prints `make`'s seed."""
    monkeypatch.chdir(tmp_path)
    run_local_process(PROJECT / 'make_use.wic', '--cwl_runner', runner, check=True)
    assert _produced() == ['seed\n', 'seed\n']


@pytest.mark.needs_cwltool
@EACH_RUNNER
def test_one_basepath_runs_two_echo_cwl_and_two_config_txt_with_provenance(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch,
                                                                           runner: str) -> None:
    """Two tools both called `echo.cwl`, each with its own `config.txt` default, run from one base path with
    provenance; each step prints its own tool's file."""
    monkeypatch.chdir(tmp_path)
    run_local_process(PROJECT / 'pair.wic', '--cwl_runner', runner, check=True)
    assert _produced() == ['config of A\n', 'config of B\n']
    assert (Path('autogenerated') / 'provenance' / 'pair').is_dir()
