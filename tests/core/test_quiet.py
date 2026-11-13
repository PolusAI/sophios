"""`--quiet` and `run(run_args_dict={'quiet': ...})` hide cwltool's own log and nothing else: the compiled CWL is
unchanged, each tool's messages print as the run goes, and a failed step is named with how it ended. The project is
tests/core/data/quiet."""
import shlex
import shutil
from pathlib import Path
from typing import Any, Final, Iterator, cast

import pytest
import yaml

from sophios import run_local
from sophios.api.python.workflow import Step, Workflow

from .cases import DATA
from .helpers import EACH_RUNNER, no_preflight, run_cli, run_local_process, search_config

pytestmark = pytest.mark.package

ADDRESSES: Final = ('say', 'local', 'inline', 'pair~say', 'pair~local', 'own')


@pytest.fixture(name='project')
def _project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A copy of the project, with a config that searches `tools/`. Nothing is pulled."""
    root = tmp_path / 'project'
    shutil.copytree(DATA / 'quiet', root)
    search_config(root / 'config.json', [root / 'tools'], [root])
    no_preflight(monkeypatch)
    monkeypatch.chdir(root)
    return root


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


@pytest.mark.fast
@EACH_RUNNER
def test_quiet_changes_only_what_cwltool_is_told(project: Path, monkeypatch: pytest.MonkeyPatch, runner: str) -> None:
    """A search-path tool, one named by a `run:` path, an inline body and the tools of a call are written as they are,
    compiled or run, quiet or not: no stream is named that the tool does not name itself. `run()` is quiet by default,
    and the document it writes is the one `compile()` returns. From the command line and from `run()`, cwltool gets
    `--quiet` only when the run is quiet, Toil never, and neither runner is told to keep a tool's streams anywhere."""
    commands: list[str] = []
    monkeypatch.setattr(run_local, 'generate_run_script', commands.append)
    say, own = Step(clt_path=project / 'tools' / 'say.cwl'), Step(clt_path=project / 'tools' / 'own.cwl')
    inline = Step.from_cwl_document(_read(project / 'local' / 'local.cwl'), process_name='inline')
    for quiet in (True, False):
        flags = ['--quiet'] if quiet else []
        for mode, base in (('--generate_cwl_workflow', 'compiled'), ('--generate_run_script', 'run')):
            assert run_cli(monkeypatch, '--yaml', 'w.wic', '--config_file', 'config.json', mode, *flags,
                           '--cwl_runner', runner, '--basepath', f'{base}_{quiet}') is None
        workflow = Workflow([say, own, inline], 'w')
        workflow.run(basepath=f'py_{quiet}', run_args_dict={'generate_run_script': 'yes', 'cwl_runner': runner,
                                                            'quiet': 'yes' if quiet else 'no'})
        assert _read(project / f'py_{quiet}' / 'w.cwl') == workflow.compile().cwl_workflow
        assert {address: tool.get('stdout') for address, tool in _tools(_read(project / f'py_{quiet}' / 'w.cwl'))} \
            == {'say': None, 'own': 'own.txt', 'inline': None}
    trees = {base: {address: _read(project / base / f'w~{address}.cwl') for address in ADDRESSES}
             for base in ('compiled_True', 'compiled_False', 'run_True', 'run_False')}
    assert all(tree == trees['compiled_False'] for tree in trees.values())
    assert {address: tool.get('stdout') for address, tool in trees['compiled_False'].items()} == {
        'say': None, 'local': None, 'inline': None, 'pair~say': None, 'pair~local': None, 'own': 'own.txt'}
    assert not any('stderr' in tool for tool in trees['compiled_False'].values())
    assert len(commands) == 4
    for cmdline, quiet in zip(commands, (True, True, False, False)):
        words = shlex.split(cmdline)
        assert ('--quiet' in words) is (quiet and runner == 'cwltool') and '--log-dir' not in words


@pytest.mark.needs_cwltool
def test_a_quiet_run_prints_each_steps_messages_and_keeps_the_files_it_names(monkeypatch: pytest.MonkeyPatch,
                                                                             tmp_path: Path) -> None:
    """A quiet run still prints what the step wrote, and the summary names a failed step and how it ended. A tool that
    writes `system.pdb` itself and also names it as its empty stdout gets the file it wrote."""
    monkeypatch.chdir(tmp_path)
    ran = run_local_process(DATA / 'quiet' / 'fail.wic', '--quiet', capture_output=True)
    assert ran.returncode != 0
    assert 'boom err\n' in ran.stderr
    assert ran.stderr.endswith(
        "Failure! step 'say' exited with status 3. Its own messages are above, under its name.\n")

    ran = run_local_process(DATA / 'quiet' / 'probe.wic', '--quiet', '--basepath', 'probe', capture_output=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    (output,) = Path('probe').glob('outdir_*/**/system.pdb')
    assert output.read_text(encoding='utf-8') == 'ATOM 1\n'
