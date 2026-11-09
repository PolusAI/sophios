"""`--quiet` and `run(run_args_dict={'quiet': ...})` hide cwltool's own log and nothing else: the compiled CWL is
unchanged, each tool's messages print as the run goes, and a failed step is named with how it ended."""
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Final, Iterator, cast

import pytest
import yaml

from sophios import run_local
from sophios.api.python import _workflow_runtime as python_runtime
from sophios.api.python.workflow import Step, Workflow

from .helpers import EACH_RUNNER, no_preflight, run_cli, run_local_process, search_config, write_tree

#: Prints a line on each stream, and fails when told to.
SAY: Final = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: [sh, -c]
arguments: ['echo "$0 out"; echo "$0 err" >&2; exit $1']
inputs:
  word: {type: string, default: say, inputBinding: {position: 1}}
  status: {type: int, default: 0, inputBinding: {position: 2}}
outputs: {}
'''
#: Names its own stdout.
OWN: Final = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
stdout: own.txt
inputs: {}
outputs: {out: {type: File, outputBinding: {glob: own.txt}}}
'''
PAIR: Final = '''steps:
- id: say
- id: local
  run: local/local.cwl
'''
#: A search-path tool (`say`, `own`), a tool named by a `run:` path, an inline body and a call.
WORKFLOW: Final = '''steps:
- id: say
- id: local
  run: local/local.cwl
- id: inline
  run:
    cwlVersion: v1.2
    class: CommandLineTool
    baseCommand: echo
    inputs: {}
    outputs: {}
- id: pair
  run: pair.wic
- id: own
'''
ADDRESSES: Final = ('say', 'local', 'inline', 'pair~say', 'pair~local', 'own')


@pytest.fixture(name='project')
def _project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`w.wic` and the tools it runs, with a config that searches `tools/`. Nothing is pulled."""
    root = tmp_path / 'project'
    write_tree(root, {'tools/say.cwl': SAY, 'tools/own.cwl': OWN, 'local/local.cwl': SAY,
                      'pair.wic': PAIR, 'w.wic': WORKFLOW})
    search_config(root / 'config.json', [root / 'tools'], [root])
    no_preflight(monkeypatch)
    monkeypatch.chdir(root)
    return root


def _sophios(monkeypatch: pytest.MonkeyPatch, *flags: str) -> list[str]:
    """`sophios --yaml w.wic <flags>`; the runner commands it would run."""
    commands: list[str] = []
    monkeypatch.setattr(run_local, 'generate_run_script', commands.append)
    assert run_cli(monkeypatch, '--yaml', 'w.wic', '--config_file', 'config.json', *flags) is None
    return commands


def _tools(document: dict[str, Any], address: str = '') -> Iterator[tuple[str, dict[str, Any]]]:
    """Each embedded tool of `document`, with the address of the step that runs it."""
    for step in document['steps']:
        step_address = f'{address}~{step["id"]}' if address else step['id']
        if step['run']['class'] == 'Workflow':
            yield from _tools(step['run'], step_address)
        else:
            yield step_address, step['run']


def _read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding='utf-8')))


def _tree(basepath: Path) -> dict[str, Any]:
    """Each tool Sophios wrote under `basepath`, by its step's address."""
    return {address: _read(basepath / f'w~{address}.cwl') for address in ADDRESSES}


def _python_workflow(project: Path) -> Workflow:
    say = Step(clt_path=project / 'tools' / 'say.cwl')
    own = Step(clt_path=project / 'tools' / 'own.cwl')
    inline = Step.from_cwl_document(yaml.safe_load(SAY), process_name='inline')
    return Workflow([say, own, inline], 'w')


@pytest.mark.fast
def test_quiet_does_not_change_the_compiled_cwl(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A search-path tool, one named by a `run:` path, an inline body and the tools of a call are written as
    they are, compiled or run, quiet or not: no stream is named that the tool does not name itself."""
    _sophios(monkeypatch, '--generate_cwl_workflow', '--basepath', 'plain')
    _sophios(monkeypatch, '--generate_cwl_workflow', '--quiet', '--basepath', 'compiled')
    _sophios(monkeypatch, '--generate_run_script', '--quiet', '--basepath', 'run')
    plain = _tree(project / 'plain')
    assert _tree(project / 'compiled') == plain
    assert _tree(project / 'run') == plain
    assert {address: tool.get('stdout') for address, tool in plain.items()} == {
        'say': None, 'local': None, 'inline': None, 'pair~say': None, 'pair~local': None, 'own': 'own.txt'}
    assert not any('stderr' in tool for tool in plain.values())


@pytest.mark.fast
def test_a_quiet_python_run_writes_what_compile_returns(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run()` is quiet by default, and the document it writes is the one `compile()` returns."""
    workflow = _python_workflow(project)
    monkeypatch.setattr(python_runtime.run_local, 'generate_run_script', lambda cmdline: None)
    workflow.run(basepath='py', run_args_dict={'generate_run_script': 'yes'})
    written = _read(project / 'py' / 'w.cwl')
    assert written == workflow.compile().cwl_workflow
    assert {address: tool.get('stdout') for address, tool in _tools(written)} == {
        'say': None, 'own': 'own.txt', 'inline': None}


@pytest.mark.fast
@EACH_RUNNER
@pytest.mark.parametrize('quiet', [True, False])
def test_only_a_quiet_cwltool_run_is_given_quiet(project: Path, monkeypatch: pytest.MonkeyPatch,
                                                 runner: str, quiet: bool) -> None:
    """From the command line and from `run()`: cwltool gets `--quiet` only when the run is quiet, Toil never,
    and neither runner is told to keep a tool's streams anywhere."""
    flags = ['--quiet'] if quiet else []
    cli = _sophios(monkeypatch, '--generate_run_script', '--cwl_runner', runner, *flags)[0]
    commands: list[str] = []
    monkeypatch.setattr(python_runtime.run_local, 'generate_run_script', commands.append)
    _python_workflow(project).run(basepath='py', run_args_dict={'generate_run_script': 'yes', 'cwl_runner': runner,
                                                                'quiet': 'yes' if quiet else 'no'})
    for cmdline in (cli, commands[0]):
        words = shlex.split(cmdline)
        assert ('--quiet' in words) is (quiet and runner == 'cwltool')
        assert '--log-dir' not in words


@pytest.mark.needs_cwltool
def test_a_failing_step_is_named_with_its_status_below_its_own_messages(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    """A quiet run still prints what the step wrote, and the summary names the step and how it ended."""
    write_tree(tmp_path, {'say.cwl': SAY,
                          'fail.wic': 'steps:\n- id: say\n  run: say.cwl\n  in: {word: !ii boom, status: !ii 3}\n'})
    monkeypatch.chdir(tmp_path)

    ran = run_local_process(tmp_path / 'fail.wic', '--quiet', capture_output=True)

    assert ran.returncode != 0
    assert 'boom err\n' in ran.stderr
    assert ran.stderr.endswith(
        "Failure! step 'say' exited with status 3. Its own messages are above, under its name.\n")


@pytest.mark.needs_cwltool
def test_a_quiet_run_keeps_a_file_the_tool_names_as_its_stdout(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    """A tool that writes `system.pdb` itself and also names it as its empty stdout gets the file it wrote."""
    write = "import sys; open(sys.argv[1], 'w').write('ATOM 1\\\\n')"
    write_tree(tmp_path, {'probe.cwl': f'''cwlVersion: v1.2
class: CommandLineTool
baseCommand: [{json.dumps(sys.executable)}, -c, "{write}"]
inputs: {{output_pdb_path: {{type: string, default: system.pdb, inputBinding: {{position: 1}}}}}}
outputs: {{output_pdb_path: {{type: File, outputBinding: {{glob: $(inputs.output_pdb_path)}}}}}}
stdout: $(inputs.output_pdb_path)
''', 'probe.wic': 'steps:\n- id: probe\n  run: probe.cwl\n'})
    monkeypatch.chdir(tmp_path)

    ran = run_local_process(tmp_path / 'probe.wic', '--quiet', capture_output=True)

    assert ran.returncode == 0, ran.stdout + ran.stderr
    (output,) = (tmp_path / 'autogenerated').glob('outdir_*/**/system.pdb')
    assert output.read_text(encoding='utf-8') == 'ATOM 1\n'
