"""A tool that refers to files beside it is written packed, so it means the same wherever it is written."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from schema_salad.fetcher import DefaultFetcher

from sophios import run_local, utils_cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.frontdoor import bundle_from_disk
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import StepId, Tool, Tools

from .helpers import ADAPTERS, EACH_RUNNER, no_preflight, run_cli, run_local_process, search_config, write_tree

#: Its script by `$include`, its data by a relative default: both beside it.
RELCAT: Final = '''cwlVersion: v1.2
class: CommandLineTool
requirements:
  InitialWorkDirRequirement:
    listing:
    - entryname: s.sh
      entry:
        $include: s.sh
baseCommand: [sh, s.sh]
inputs:
  extra:
    type: File
    default: {class: File, location: data.txt}
    inputBinding: {position: 1}
outputs:
  out: {type: stdout}
stdout: out.txt
'''
SCRIPT: Final = 'echo from-include; cat "$1"\n'
DATA: Final = 'data beside the tool\n'
#: What `relcat` prints.
PRINTED: Final = 'from-include\n' + DATA
PLAIN: Final = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs: {message: {type: string, default: hi, inputBinding: {position: 1}}}
outputs: {out: {type: stdout}}
stdout: out.txt
'''
#: A relative default, and an output whose format inference reads as written: `edam:format_2330`.
MAKE: Final = '''cwlVersion: v1.2
class: CommandLineTool
$namespaces: {edam: https://edamontology.org/}
baseCommand: cat
stdout: made.txt
inputs:
  seed: {type: File, default: {class: File, location: seed.txt}, inputBinding: {position: 1}}
outputs:
  out: {type: File, format: edam:format_2330, outputBinding: {glob: made.txt}}
'''
USE: Final = '''cwlVersion: v1.2
class: CommandLineTool
$namespaces: {edam: https://edamontology.org/}
baseCommand: cat
stdout: used.txt
inputs:
  file: {type: File, format: edam:format_2330, inputBinding: {position: 1}}
outputs:
  out: {type: File, outputBinding: {glob: used.txt}}
'''
MAKE_USE_PROJECT: Final = {
    'tools/seed.txt': 'seed\n', 'tools/make.cwl': MAKE, 'tools/use.cwl': USE,
    'w.wic': 'steps:\n- id: make\n  run: tools/make.cwl\n- id: use\n  run: tools/use.cwl\n'}
#: A step `say` whose inline tool includes the `s.sh` beside the `.wic` that holds it.
INLINE_SAY: Final = '''- id: say
  run:
    class: CommandLineTool
    requirements:
      InitialWorkDirRequirement:
        listing: [{entryname: s.sh, entry: {$include: s.sh}}]
    baseCommand: [sh, s.sh]
    inputs: {}
    outputs: {}
'''
#: Like `extract_pdbbind_refined.cwl`: its image is built from a Dockerfile beside it.
DOCKERFILE_TOOL: Final = '''cwlVersion: v1.0
class: CommandLineTool
hints:
  DockerRequirement:
    dockerImageId: built_here
    dockerFile:
      $include: Dockerfile_built
baseCommand: echo
inputs: {}
outputs: {}
'''
DOCKERFILE: Final = 'FROM alpine:3.20\nRUN echo built\n'
#: Its `$include` names nothing.
BROKEN: Final = '''cwlVersion: v1.2
class: CommandLineTool
requirements:
  InitialWorkDirRequirement:
    listing: [{entryname: s.sh, entry: {$include: missing.sh}}]
baseCommand: [sh, s.sh]
inputs: {}
outputs: {}
'''


def _relcat(directory: Path, script: str = SCRIPT) -> Path:
    """`relcat.cwl`, its script and its data, in `directory`."""
    return write_tree(directory, {'s.sh': script, 'data.txt': DATA, 'relcat.cwl': RELCAT}) / 'relcat.cwl'


def _registry(*tools: Path) -> Tools:
    """Each file as a search-path tool, read as the loader reads it."""
    return {StepId(tool.stem, 'global'): Tool(str(tool), yaml.safe_load(tool.read_text(encoding='utf-8')))
            for tool in tools}


def _compile(wic: Path, tools: Tools | None = None) -> CompilationArtifact:
    return compile_source(bundle_from_disk(wic, {}, tools or {}), default_compilation_settings(),
                          testing=True).artifact


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
def test_a_tool_with_an_include_and_a_relative_default_is_emitted_packed(tmp_path: Path) -> None:
    """The include is its text and the default an absolute URI; the registry still holds the tool as read."""
    tool = _relcat(tmp_path / 'tools')
    write_tree(tmp_path, {'w.wic': 'steps:\n- id: relcat\n'})
    registry = _registry(tool)

    written = _tool(_compile(tmp_path / 'w.wic', registry), 'relcat')

    assert _entry(written) == SCRIPT
    assert written['inputs']['extra']['default'] == {
        'class': 'File', 'location': (tmp_path / 'tools/data.txt').as_uri()}
    assert written['cwlVersion'] == 'v1.2'
    assert _entry(registry[StepId('relcat', 'global')].cwl) == {'$include': 's.sh'}


@pytest.mark.fast
def test_every_step_running_a_tool_is_described_packed(tmp_path: Path) -> None:
    """However many steps run a tool, every step's description is packed."""
    tool = _relcat(tmp_path / 'tools')
    write_tree(tmp_path, {'w.wic': 'steps:\n- id: relcat\n- id: relcat\n- id: relcat\n'})

    artifact = _compile(tmp_path / 'w.wic', _registry(tool))

    assert [_entry(child.cwl) for child in artifact.children] == [SCRIPT] * 3


@pytest.mark.fast
def test_one_inline_body_in_two_directories_reads_each_ones_files(tmp_path: Path) -> None:
    """The same body, written in two `.wic` files in two directories, includes each directory's `s.sh`."""
    write_tree(tmp_path, {'a/s.sh': 'echo a\n', 'b/s.sh': 'echo b\n', 'b/inner.wic': 'steps:\n' + INLINE_SAY,
                          'a/w.wic': 'steps:\n' + INLINE_SAY + '- id: inner\n  run: ../b/inner.wic\n'})

    artifact = _compile(tmp_path / 'a' / 'w.wic')

    inner, = (child for child in artifact.children if child.namespace == ('inner',))
    assert (_entry(_tool(artifact, 'say')), _entry(_tool(inner, 'say'))) == ('echo a\n', 'echo b\n')


def _dockerfile_project(tmp_path: Path) -> Path:
    """A project whose search path holds `built.cwl` and the Dockerfile it builds its image from."""
    write_tree(tmp_path, {'tools/built.cwl': DOCKERFILE_TOOL, 'tools/Dockerfile_built': DOCKERFILE,
                          'w.wic': 'steps:\n- id: built\n'})
    search_config(tmp_path / 'config.json', [tmp_path / 'tools'], [tmp_path])
    return tmp_path


@pytest.mark.fast
def test_a_dockerfile_include_is_packed_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The written tool carries the Dockerfile itself, in the tree and embedded, and keeps its own `v1.0`."""
    monkeypatch.chdir(_dockerfile_project(tmp_path))
    for flags in ((), ('--cwl_inline_runtag',)):
        assert run_cli(monkeypatch, '--yaml', 'w.wic', '--config_file', 'config.json', '--generate_cwl_workflow',
                       '--basepath', 'out', *flags) is None
        root = yaml.safe_load(Path('out/w.cwl').read_text(encoding='utf-8'))
        tool = root['steps'][0]['run'] if flags else yaml.safe_load(Path('out/w~built.cwl').read_text(encoding='utf-8'))
        docker = _requirement(tool, 'hints', 'DockerRequirement')
        assert docker == {'dockerImageId': 'built_here', 'dockerFile': DOCKERFILE}, flags
        assert flags or tool['cwlVersion'] == 'v1.0'


def _analysis_project(tmp_path: Path) -> Path:
    """`relcat` on the search path, run by `w.wic` and declared as a real-time analysis by `live.wic`."""
    tool = _relcat(tmp_path / 'tools')
    write_tree(tmp_path, {'tools/slow_log.cwl': '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: [touch, out.log]
inputs: {}
outputs: {log: {type: File, outputBinding: {glob: out.log}}}
''', 'w.wic': 'steps:\n- id: relcat\n', 'live.wic': '''steps:
  - id: slow_log
  - id: cwl_subinterpreter
    in:
      file_pattern: !ii out.log
      cwl_tool: !ii relcat
      max_times: !ii '1'
      config: !ii {in: {}}
'''})
    search_config(tmp_path / 'config.json', [tool.parent, ADAPTERS / 'file_watchers'], [tmp_path])
    return tool


@pytest.mark.fast
def test_compile_run_and_an_analysis_all_carry_the_packed_text(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Python `compile()`, the document `run()` writes, and a real-time analysis's tool are packed alike."""
    tool = _analysis_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_local, 'generate_run_script', lambda _cmdline: None)
    no_preflight(monkeypatch)

    compiled = Workflow([Step(clt_path=tool)], 'py').compile().cwl_workflow
    Workflow([Step(clt_path=tool)], 'py').run(basepath='ran', run_args_dict={'generate_run_script': 'yes'})
    ran = yaml.safe_load(Path('ran/py.cwl').read_text(encoding='utf-8'))
    assert run_cli(monkeypatch, '--yaml', 'live.wic', '--config_file', 'config.json', '--generate_cwl_workflow') is None
    analysis, = Path('autogenerated/realtime/live').glob('*~relcat.cwl')

    for where, written in (('compile()', compiled['steps'][0]['run']), ('run()', ran['steps'][0]['run']),
                           ('analysis', yaml.safe_load(analysis.read_text(encoding='utf-8')))):
        assert _entry(written) == SCRIPT, where
        assert written['inputs']['extra']['default']['location'] == (tool.parent / 'data.txt').as_uri(), where


@pytest.mark.fast
def test_inference_wires_a_packed_tool_as_it_wires_one_as_written(tmp_path: Path) -> None:
    """Inference reads `make` as written, so its `edam:format_2330` output feeds `use`'s input: no new root
    input. The description written for `make` is packed."""
    write_tree(tmp_path, MAKE_USE_PROJECT)

    artifact = _compile(tmp_path / 'w.wic')

    use, = (step for step in artifact.cwl['steps'] if step['id'] == 'use')
    assert use['in'] == {'file': 'make/out'}
    assert not artifact.cwl['inputs']
    seed = tmp_path / 'tools' / 'seed.txt'
    assert _tool(artifact, 'make')['inputs']['seed']['default']['location'] == seed.as_uri()


@pytest.mark.fast
def test_a_broken_tool_nobody_runs_stops_no_compile(tmp_path: Path) -> None:
    """Only a tool a step runs is packed: one on the search path that cannot be packed costs nothing."""
    write_tree(tmp_path, {'broken.cwl': BROKEN, 'echo.cwl': PLAIN, 'w.wic': 'steps:\n- id: echo\n'})

    registry = _registry(tmp_path / 'broken.cwl', tmp_path / 'echo.cwl')

    artifact = _compile(tmp_path / 'w.wic', registry)

    assert [child.namespace for child in artifact.children] == [('echo',)]
    assert _tool(artifact, 'echo') == registry[StepId('echo', 'global')].cwl, \
        'nothing to resolve, so the tool is emitted as the loader read it'


@pytest.mark.fast
def test_a_step_running_a_tool_with_a_missing_include_reports_cwltools_error_at_the_step(tmp_path: Path) -> None:
    """cwltool's own message, at the line of the step that runs the tool."""
    write_tree(tmp_path, {'broken.cwl': BROKEN, 'echo.cwl': PLAIN, 'w.wic': 'steps:\n- id: echo\n- id: broken\n'})

    with pytest.raises(SophiosError) as raised:
        _compile(tmp_path / 'w.wic', _registry(tmp_path / 'broken.cwl', tmp_path / 'echo.cwl'))

    diagnostic, = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    assert diagnostic.locator is not None and (diagnostic.locator.step, diagnostic.locator.index) == ('broken', 2)
    assert diagnostic.span is not None and diagnostic.span.start_line == 3
    assert str(tmp_path / 'broken.cwl') in diagnostic.message
    # The OSError as schema-salad passes it on. On Windows the error's filename, spelled with backslashes,
    # differs from the URI's path, so schema-salad prefixes it with `Error reading <uri>: `.
    reason = diagnostic.message.splitlines()[-1].strip()
    assert "[Errno 2] No such file or directory: '" in reason and reason.endswith("missing.sh'")


def _produced() -> list[str]:
    """What the run's output files hold, in order: cwltool and Toil each write them to an outdir of their own."""
    return sorted(path.read_text(encoding='utf-8') for path in Path('autogenerated').glob('outdir_*/**/*')
                  if path.is_file())


FORMS: Final = {'tree': (), 'embedded': ('--cwl_inline_runtag',)}


@pytest.mark.needs_cwltool
@EACH_RUNNER
@pytest.mark.parametrize('form', FORMS)
def test_a_packed_tool_runs_from_the_tree_and_embedded_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             runner: str, form: str) -> None:
    """Two steps run `relcat`, from the tree's `w~<step>.cwl` and from one embedded document: each prints its
    included script's line and its default's data."""
    _relcat(tmp_path / 'tools')
    write_tree(tmp_path, {'w.wic': 'steps:\n- id: one\n  run: tools/relcat.cwl\n- id: two\n  run: tools/relcat.cwl\n'})
    monkeypatch.chdir(tmp_path)

    run_local_process(tmp_path / 'w.wic', '--cwl_runner', runner, *FORMS[form], check=True)

    assert _produced() == [PRINTED, PRINTED]


SCHEMADEF: Final = Path(__file__).resolve().parent / 'data' / 'cwl-v1.2' / 'tests'


@pytest.mark.needs_cwltool
def test_a_schemadef_tool_with_an_import_is_written_runnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The conformance tool whose record type is `$import`ed from a file beside it: the description the tree
    holds for its step runs, given the conformance job."""
    tool = SCHEMADEF / 'schemadef_types_with_import-tool.cwl'
    job = json.loads((SCHEMADEF / 'schemadef_types_with_import-job.json').read_text(encoding='utf-8'))
    value = job['readgroups_bam']
    value['bam'] = {'class': 'File', 'location': (SCHEMADEF / value['bam']['location']).as_uri()}
    write_tree(tmp_path, {'job.yml': yaml.safe_dump({'message': value}),
                          'w.wic': yaml.safe_dump({'steps': [{'id': 'sd', 'run': os.path.relpath(tool, tmp_path)}]})})
    monkeypatch.chdir(tmp_path)
    assert run_cli(monkeypatch, '--yaml', 'w.wic', '--generate_cwl_workflow') is None

    ran = subprocess.run([sys.executable, '-c', 'import sys, cwltool.main; sys.exit(cwltool.main.main())',
                          '--outdir', 'out', 'autogenerated/w~sd.cwl', 'job.yml'], capture_output=True, text=True,
                         check=False)

    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)['out'] == '1970-01-01T00:00:00Z\n'


@pytest.mark.needs_cwltool
def test_a_packed_tool_keeps_the_partial_failure_rewrite_written_and_embedded(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """`--partial_failure_enable` rewrites the packed description: its output optional, exit 1 a success, in the
    tree and embedded; the tree runs."""
    _relcat(tmp_path / 'tools')
    write_tree(tmp_path, {'w.wic': 'steps:\n- id: relcat\n  run: tools/relcat.cwl\n'})
    monkeypatch.chdir(tmp_path)

    run_local_process(tmp_path / 'w.wic', '--partial_failure_enable', check=True)
    tree = yaml.safe_load(Path('autogenerated/w~relcat.cwl').read_text(encoding='utf-8'))
    assert _produced() == [PRINTED]
    run_local_process(tmp_path / 'w.wic', '--partial_failure_enable', '--cwl_inline_runtag', check=True)
    embedded = yaml.safe_load(Path('autogenerated/w.cwl').read_text(encoding='utf-8'))['steps'][0]['run']

    for written in (tree, embedded):
        assert _entry(written) == SCRIPT
        assert written['outputs']['out']['type'] == 'File?'
        assert written['successCodes'] == [0, 1]


@pytest.mark.needs_cwltool
@EACH_RUNNER
def test_a_packed_tool_and_one_as_written_run_wired_by_inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 runner: str) -> None:
    """`make`, packed, feeds `use`, as written, through the edge inference made; `use` prints `make`'s seed."""
    write_tree(tmp_path, MAKE_USE_PROJECT)
    monkeypatch.chdir(tmp_path)

    run_local_process(tmp_path / 'w.wic', '--cwl_runner', runner, check=True)

    assert _produced() == ['seed\n', 'seed\n']


def _cat_config(directory: Path, config: str) -> Path:
    """A tool that prints its `config.txt` default, beside it in `directory`."""
    return write_tree(directory, {'config.txt': config, 'echo.cwl': '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: cat
stdout: out.txt
inputs:
  config: {type: File, default: {class: File, location: config.txt}, inputBinding: {position: 1}}
outputs:
  out: {type: stdout}
'''}) / 'echo.cwl'


@pytest.mark.needs_cwltool
@EACH_RUNNER
def test_one_basepath_runs_two_echo_cwl_and_two_config_txt_with_provenance(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch,
                                                                           runner: str) -> None:
    """Two tools both called `echo.cwl`, each with its own `config.txt` default, run from one base path with
    provenance; each step prints its own tool's file."""
    _cat_config(tmp_path / 'dataA', 'config of A\n')
    _cat_config(tmp_path / 'dataB', 'config of B\n')
    write_tree(tmp_path, {'w.wic': 'steps:\n- id: a\n  run: dataA/echo.cwl\n- id: b\n  run: dataB/echo.cwl\n'})
    monkeypatch.chdir(tmp_path)

    run_local_process(tmp_path / 'w.wic', '--cwl_runner', runner, check=True)

    assert _produced() == ['config of A\n', 'config of B\n']
    assert (Path('autogenerated') / 'provenance' / 'w').is_dir()


def _hashes(root: Path) -> dict[str, tuple[str, int]]:
    return {path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.needs_cwltool
def test_nothing_outside_the_basepath_is_written_or_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compiling and running packed tools reads the tools, scripts and data where they are and writes none of
    them, nor anything beside them."""
    project = tmp_path / 'project'
    _relcat(project / 'tools')
    _cat_config(project / 'dataA', 'config of A\n')
    write_tree(project, {'w.wic': 'steps:\n- id: relcat\n  run: tools/relcat.cwl\n- id: a\n  run: dataA/echo.cwl\n'})
    before = _hashes(project)
    (tmp_path / 'work').mkdir()
    monkeypatch.chdir(tmp_path / 'work')

    run_local_process(project / 'w.wic', check=True)

    assert _hashes(project) == before
    assert _produced() == ['config of A\n', PRINTED]


@pytest.mark.fast
def test_packing_does_not_fetch_a_tools_schemas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`$schemas` stay as written: packing a tool never goes to the network for an ontology."""
    ontology = 'http://schemas.invalid/ontology.owl'
    fetched: list[str] = []
    fetch_text = DefaultFetcher.fetch_text

    def spy(self: DefaultFetcher, url: str, content_types: list[str] | None = None) -> str:
        fetched.append(url)
        return fetch_text(self, url, content_types)

    monkeypatch.setattr(DefaultFetcher, 'fetch_text', spy)
    tool = yaml.safe_load(PLAIN)
    tool['$schemas'] = [ontology]

    packed = utils_cwl.packed(yaml.safe_dump(tool), (tmp_path / 'echo.cwl').as_uri())

    assert ontology not in fetched
    assert packed['$schemas'] == [ontology]
