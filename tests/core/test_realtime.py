"""Real-time analysis: a `cwl_subinterpreter` step declares an analysis Sophios runs beside the workflow. The analyses
of tests/core/data/realtime compile, are written and run as stated there; the watcher is driven over a stand-in for
cwltool's cachedir and a stand-in analysis."""
import json
import os
import shutil
import string
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from itertools import chain
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import example, given, strategies as st

from sophios import realtime, run_local
from sophios.backends import RunRequest
from sophios.input_output import NoAliasDumper
from sophios.ir.realtime import Declaration
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import StepId, Tool, Tools, Yaml

from .budgets import budget
from .cases import DATA, match, printed
from .fake_runner import POSIX_ONLY, install
from .helpers import ADAPTERS, live_workflow, no_preflight, run_cli, search_config
from .hermetic import compile_hermetic
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

ADAPTER_PATH: Final = ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl'
TOOLS: Final[Tools] = {
    **SYNTHETIC_TOOLS,
    StepId('cwl_subinterpreter', SYNTHETIC_NS): Tool(str(ADAPTER_PATH),
                                                     yaml.safe_load(ADAPTER_PATH.read_text(encoding='utf-8'))),
}


def _ii(value: Any) -> Yaml:
    return {'wic_inline_input': value}


def _declaration_lines(document: Yaml) -> list[int]:
    """The line each declaration step starts on in `document` as `hermetic.bundle` writes it."""
    text = yaml.dump(document, Dumper=NoAliasDumper, sort_keys=False, line_break='\n', indent=2)
    return [number for number, line in enumerate(text.splitlines(), start=1)
            if line.strip() == '- id: cwl_subinterpreter']


_UNREADABLE, _REFUSED = object(), object()
_POSITIVE = st.integers(1, 10**6).flatmap(lambda n: st.sampled_from([n, str(n), f' {n} ']).map(lambda v: (v, n)))
#: Not positive whole numbers. Digits other than ASCII ones are left out: `int()` raises on '²' (reported).
_NOT_POSITIVE = (st.integers(max_value=0) | st.booleans() | st.floats(allow_nan=False) | st.sampled_from(['0', '00'])
                 | st.text(string.ascii_letters + ' -.+', max_size=3))
#: Per port, values `ir/realtime.py` documents it takes, with what the declaration holds, and values it refuses.
_PORTS: Final = {
    'file_pattern': (st.text(min_size=1).map(lambda text: (text, text)), st.just('') | st.integers() | st.none()),
    'cwl_tool': (st.text(min_size=1).map(lambda text: (text, text)), st.just('') | st.lists(st.text(), max_size=1)),
    'max_times': (_POSITIVE, _NOT_POSITIVE),
    'interval': (_POSITIVE, _NOT_POSITIVE),
    'config': (st.dictionaries(st.text(min_size=1), st.text(), max_size=2).map(lambda config: (config, config)),
               st.text() | st.lists(st.text(), max_size=1) | st.integers()),
}


@st.composite
def _declaration(draw: st.DrawFn) -> dict[str, tuple[Yaml | None, Any]]:
    """Each port's binding, None when not written, and what the declaration holds; at most two written wrong."""
    wrong = draw(st.sets(st.sampled_from(sorted(_PORTS)), max_size=2))
    return {port: draw(st.one_of(st.just((None, 60 if port == 'interval' else _UNREADABLE)),
                                 st.just(({'wic_alias': 'made'}, _UNREADABLE)),
                                 refused.map(lambda value: (_ii(value), _REFUSED)))
                       if port in wrong else taken.map(lambda pair: (_ii(pair[0]), pair[1])))
            for port, (taken, refused) in _PORTS.items()}


def _held(**overrides: tuple[Yaml | None, Any]) -> dict[str, tuple[Yaml | None, Any]]:
    return {'file_pattern': (_ii('*.txt'), '*.txt'), 'cwl_tool': (_ii('count'), 'count'), 'max_times': (_ii('3'), 3),
            'interval': (None, 60), 'config': (_ii({'in': {'file': 'a.txt'}}), {'in': {'file': 'a.txt'}}), **overrides}


_PRODUCER: Final = {'id': 'mk_file', 'in': {'name': _ii('a.txt')}, 'out': [{'file': {'wic_anchor': 'made'}}]}


@pytest.mark.fast
@given(st.lists(_declaration(), min_size=1, max_size=2))
@example([_held()])
@example([_held(file_pattern=({'wic_alias': 'made'}, _UNREADABLE))])
@example([_held(cwl_tool=(None, _UNREADABLE))])
@example([_held(cwl_tool=(_ii(''), _REFUSED))])
@example([_held(max_times=(_ii('many'), _REFUSED))])
@example([_held(max_times=(_ii(0), _REFUSED))])
@example([_held(max_times=(_ii(True), _REFUSED))])  # `true` is no count, though Python's bool is an int
@example([_held(interval=(_ii(-1), _REFUSED))])
@example([_held(config=(_ii('in: a.txt'), _REFUSED))])
@budget(30)
def test_a_declaration_is_returned_instead_of_emitted_or_is_wic044_at_each_port(
        declarations: list[dict[str, tuple[Yaml | None, Any]]]) -> None:
    """Declarations leave the CWL as it is without them and are returned as written, at their lines; one written
    wrong is `wic044` at each port not written or not a literal or, when all are literals, refusing its value."""
    document = {'steps': [_PRODUCER, *({'id': 'cwl_subinterpreter', 'in': {
        port: binding for port, (binding, _) in declaration.items() if binding is not None}}
        for declaration in declarations)]}
    lines = _declaration_lines(document)
    wrong = [[port for port, (_, held) in declaration.items() if held is _UNREADABLE]
             or [port for port, (_, held) in declaration.items() if held is _REFUSED] for declaration in declarations]
    if any(wrong):
        with pytest.raises(SophiosError) as raised:
            compile_hermetic(document, tools=TOOLS)
        assert {diagnostic.code for diagnostic in raised.value.diagnostics} == {SophiosErrorCode.REALTIME_DECLARATION}
        assert sorted((d.span.start_line if d.span else None, d.locator.port if d.locator else None)
                      for d in raised.value.diagnostics) == sorted(
            (line, port) for line, ports in zip(lines, wrong) for port in ports)
        return
    result, alone = compile_hermetic(document, tools=TOOLS), compile_hermetic({'steps': [_PRODUCER]}, tools=TOOLS)
    assert (result.artifact.cwl, result.artifact.job_inputs, list(map(str, result.diagnostics))) == (
        alone.artifact.cwl, alone.artifact.job_inputs, list(map(str, alone.diagnostics)))
    assert [(declaration.span.start_line if declaration.span else None, replace(declaration, span=None))
            for declaration in result.realtime] == [(line, Declaration('oracle', *(
                held[port][1] for port in ('cwl_tool', 'file_pattern', 'max_times', 'interval', 'config'))))
        for line, held in zip(lines, declarations)]


REALTIME: Final = DATA / 'realtime'


def _project(tmp_path: Path) -> Path:
    """A copy of tests/core/data/realtime, and a config that finds its tools, its workflows and the adapter."""
    root = tmp_path / 'project'
    shutil.copytree(REALTIME, root, ignore=shutil.ignore_patterns('expected.yaml'))
    search_config(root / 'config.json', [root / 'tools', ADAPTER_PATH.parent], [root])
    return root


@pytest.mark.fast
# pylint: disable-next=too-many-locals
def test_declared_analyses_are_taken_out_compiled_and_written_on_their_own_from_either_door(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The analyses of declared.wic, as tests/core/data/realtime/expected.yaml states them: a tool's `config: in:`
    values are literals of the analysis, a .wic analysis is configured per step, a subworkflow whose only step is a
    declaration is emitted with no steps, and a relative `dockerFile` `$include` is the file beside the tool, wherever
    the analysis is written. With no analyses, no manifest is left. In Python, a `Step` built from the adapter is a
    declaration: never emitted, compiled and pulled by `run`, the analysis written as the workflow is, one document
    with every process embedded, and its job; a `.wic` named by `cwl_tool` is found through `run(workflow_paths=...)`,
    as `from_wic` finds a nested one, and without them is wic044 naming the wic013 it met."""
    root = _project(tmp_path)
    expected = yaml.safe_load((REALTIME / 'expected.yaml').read_text(encoding='utf-8').replace('{tmp}', str(root)))
    monkeypatch.chdir(root)
    assert run_cli(monkeypatch, '--yaml', 'declared.wic', '--config_file', 'config.json',
                   '--generate_cwl_workflow') is None
    written = root / 'autogenerated'
    for directory, names in expected['files'].items():
        assert sorted(path.name for path in (written / directory).iterdir()) == names, directory
    for name, holds in expected['written'].items():
        assert match(holds, yaml.safe_load((written / name).read_text(encoding='utf-8'))), name
    assert not realtime.write((), written, 'declared', root)
    assert not realtime.manifest_path(written, 'declared').exists()
    capsys.readouterr()
    assert run_cli(monkeypatch, '--yaml', 'refused.wic', '--config_file', 'config.json', '--generate_cwl_workflow',
                   '--diagnostics', 'json') == 1
    assert match(expected['refused'], printed(capsys.readouterr().err))

    prepared = no_preflight(monkeypatch)
    (tmp_path / 'python').mkdir()
    monkeypatch.chdir(tmp_path / 'python')
    workflow = live_workflow('py_live')
    assert [step['id'] for step in workflow.compile().cwl_workflow['steps']] == ['touch']
    workflow.run(run_args_dict={'generate_run_script': 'yes'})

    (entry,) = json.loads(realtime.manifest_path(Path('autogenerated'), 'py_live').read_text(encoding='utf-8'))
    assert (entry['analysis'], entry['max_times']) == ('touch', 2)
    assert list(chain.from_iterable(prepared)) == [Path('autogenerated/py_live.cwl'),
                                                   Path('autogenerated/realtime/py_live/touch_only.cwl')]
    assert 'Real-time analysis runs only with --run_local' in capsys.readouterr().err
    written = Path('autogenerated/realtime/py_live')
    assert sorted(path.name for path in written.iterdir()) == ['touch_only.cwl', 'touch_only_inputs.yml']
    (step,) = yaml.safe_load((written / 'touch_only.cwl').read_text(encoding='utf-8'))['steps']
    assert step['run']['class'] == 'CommandLineTool'

    workflow = live_workflow('py_wic', 'touched.wic', {'(1, touch)': {'in': {'filename': 'again.txt'}}})
    with pytest.raises(SophiosError) as raised:
        workflow.run(run_args_dict={'generate_run_script': 'yes'})
    (diagnostic,) = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.REALTIME_DECLARATION
    assert 'touched.wic' in diagnostic.message and 'wic013' in diagnostic.message
    workflow.run(run_args_dict={'generate_run_script': 'yes'},
                 workflow_paths={'global': {'touched': REALTIME / 'touched.wic'}})
    (entry,) = json.loads(realtime.manifest_path(Path('autogenerated'), 'py_wic').read_text(encoding='utf-8'))
    assert entry['analysis'] == 'touched.wic'
    assert Path('autogenerated/realtime/py_wic/touched_only.cwl').is_file()


#: An analysis stand-in: copies its job file into its output directory, so a test can read what it was given.
RECORD: Final = ('import os, shutil, sys, time; os.makedirs(sys.argv[2], exist_ok=True); '
                 'shutil.copy(sys.argv[1], os.path.join(sys.argv[2], "job.json")); '
                 'time.sleep(float(sys.argv[3])); sys.exit(int(sys.argv[4]))')


def _command(seconds: float = 0.0, exit_code: int = 0) -> realtime.Command:
    """A stand-in analysis that records its job, takes `seconds` and exits with `exit_code`."""
    def command(_cwl: Path, job: Path, outdir: Path, _cachedir: Path) -> list[str]:
        return [sys.executable, '-c', RECORD, str(job), str(outdir), str(seconds), str(exit_code)]
    return command


def _plan(tmp_path: Path, *, max_times: int = 5, interval: float = 0.0, pattern: str = '*.log',
          reference: bool = True) -> realtime.Plan:
    """A plan whose analysis reads `out.log` and `ref.pdb`, and takes a string; `ref.pdb` is in the project."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    inputs = tmp_path / 'analysis_inputs.yml'
    inputs.write_text(yaml.safe_dump({'log': {'class': 'File', 'location': 'out.log', 'format': 'edam:format_2330'},
                                      'ref': {'class': 'File', 'location': 'ref.pdb'},
                                      'name': 'out.log'}), encoding='utf-8')
    (tmp_path / 'project').mkdir()
    if reference:
        (tmp_path / 'project' / 'ref.pdb').write_text('ref', encoding='utf-8')
    return realtime.Plan('wf', 'measure.wic', pattern, max_times, interval, tmp_path / 'analysis.cwl', inputs,
                         tmp_path / 'project', 'wf.wic:3:3')


class _Cache:
    """A cwltool cachedir: `start` makes a job's directory and empty status, `end` writes its status."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def start(self, files: dict[str, str]) -> str:
        """Start a job that has written `files` (paths relative to its directory) so far; return its key."""
        self.count += 1
        key = f'{self.count:032x}'
        (self.root / key / 'sub').mkdir(parents=True)
        (self.root / f'{key}.status').touch()
        for name, text in files.items():
            self.write(key, name, text)
        return key

    def write(self, key: str, name: str, text: str) -> None:
        """Append `text` to the file `name` of job `key` in one step, so the watcher's thread never scans between the
        file appearing and its text."""
        path = self.root / key / name
        partial = path.with_name(path.name + '.partial')
        before = path.read_text(encoding='utf-8') if path.exists() else ''
        partial.write_text(before + text, encoding='utf-8')
        os.replace(partial, path)

    def end(self, key: str, status: str = 'success') -> None:
        """End job `key` with `status`, as cwltool does."""
        (self.root / f'{key}.status').write_text(status, encoding='utf-8')


def _until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    """Wait for `condition`, failing after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.02)


def _jobs(cache: _Cache) -> list[Yaml]:
    """What each analysis run was given, in order."""
    runs = sorted((cache.root / 'realtime' / 'wf').glob('run-*/job.json'))
    return [json.loads(path.read_text(encoding='utf-8')) for path in runs]


def _started(plan: realtime.Plan, cache: _Cache, command: realtime.Command | None = None) -> realtime.Watcher:
    """A started watcher of cache that polls fast."""
    watcher = realtime.Watcher(plan, cache.root, command or _command(), poll=0.02)
    watcher.start()
    return watcher


def _watch(tmp_path: Path, command: realtime.Command | None = None, **plan: Any) -> tuple[_Cache, realtime.Watcher]:
    """A cwltool cachedir and a started watcher of it that polls fast, for `_plan(tmp_path, **plan)`."""
    cache = _Cache(tmp_path / 'cachedir')
    return cache, _started(_plan(tmp_path, **plan), cache, command)


@pytest.mark.fast
# pylint: disable-next=too-many-statements
def test_an_analysis_runs_while_its_job_writes_and_once_more_at_its_end_unless_it_failed(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Polling has no budget: `max_times` counts runs, not polls, so a job that starts long after the watcher is still
    analysed. A file that keeps changing is analysed once per `interval`, and its last state at the end; the step
    finishing is the moment its outputs are complete, so no `interval` delays that run. Changes while an analysis runs
    make one follow-up run, not one each, and runs while the workflow runs leave room for the final one. The outputs of
    a failed step are no result to analyse, so there is no final run, and a line says why; a failed analysis is a line
    naming its log, counted in the summary; and Ctrl-C does not wait for an analysis to finish."""
    cache, watcher = _watch(tmp_path / 'late', max_times=2)
    time.sleep(0.5)                                            # 25 polls with nothing to see
    key = cache.start({'sub/out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()
    (job,) = _jobs(cache)
    assert job['log'] == {'class': 'File', 'location': str(cache.root / key / 'sub' / 'out.log'),
                          'format': 'edam:format_2330'}
    assert job['ref']['location'] == str(tmp_path / 'late' / 'project' / 'ref.pdb') and job['name'] == 'out.log'

    cache, watcher = _watch(tmp_path / 'growing', interval=60)
    key = cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    for line in range(2, 6):
        cache.write(key, 'out.log', f'{line}\n')
        time.sleep(0.05)
    assert watcher.runs == 1
    cache.end(key)
    _until(lambda: watcher.runs == 2)
    watcher.finish()
    assert watcher.runs == watcher.succeeded == 2
    assert 'the job that wrote out.log finished' in capsys.readouterr().out

    cache, watcher = _watch(tmp_path / 'busy', _command(seconds=0.6))
    key = cache.start({'out.log': '1\n'})
    _until((cache.root / 'realtime' / 'wf' / 'run-001.log').exists)
    for line in range(2, 5):
        cache.write(key, 'out.log', f'{line}\n')
        time.sleep(0.05)
    _until(lambda: watcher.runs == 2)
    time.sleep(1.0)
    assert watcher.runs == 2
    watcher.finish()
    assert watcher.runs == 2

    cache, watcher = _watch(tmp_path / 'room', max_times=3)
    key = cache.start({'out.log': '1\n'})
    deadline = time.monotonic() + 2.0                          # changes all along, far more than three runs' worth
    while time.monotonic() < deadline:
        cache.write(key, 'out.log', 'more\n')
        time.sleep(0.05)
    assert watcher.runs == 2
    watcher.finish()
    assert watcher.runs == 3 and len(_jobs(cache)) == 3

    cache, watcher = _watch(tmp_path / 'failed', interval=60)
    key = cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    cache.write(key, 'out.log', '2\n')
    cache.end(key, 'permanentFail')
    watcher.finish()
    assert watcher.runs == 1
    assert 'no final run: the job that wrote out.log failed' in capsys.readouterr().out

    cache, watcher = _watch(tmp_path / 'failing', _command(exit_code=3), interval=60)
    cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()
    out = capsys.readouterr().out
    assert 'run 1 (out.log changed) failed with exit code 3; log: ' in out and '1 runs, 0 succeeded' in out

    cache, watcher = _watch(tmp_path / 'cancelled', _command(seconds=60))
    cache.start({'out.log': '1\n'})
    _until((cache.root / 'realtime' / 'wf' / 'run-001' / 'job.json').exists)
    began = time.monotonic()
    watcher.cancel()
    assert time.monotonic() - began < 10 and watcher.succeeded == 0


@pytest.mark.fast
# pylint: disable-next=too-many-locals
def test_an_analysis_reads_the_newest_file_this_run_wrote(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Two steps of this run wrote `out.log`: the analysis gets the newer, and a line names both. A cache hit leaves
    its job alone, so it never triggers, but this run reused it, so its files resolve. A file no job of this run wrote,
    as the corpus `cwl_subinterpreter_complex.wic` reads one its own analysis writes, skips the run with one line. A
    later run of the workflow shares the cachedir, keeps the earlier one's outputs and logs, and numbers its own runs
    after them."""
    cache, watcher = _watch(tmp_path / 'two', pattern='*.trr', interval=60)
    first = cache.start({'out.log': 'first\n'})
    time.sleep(0.05)
    second = cache.start({'out.log': 'second\n', 'prod.trr': 'x'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()
    (job,) = _jobs(cache)
    assert job['log']['location'] == str(cache.root / second / 'out.log')
    assert f'out.log was written by 2 jobs of this run ({first}, {second}); using the newest' in capsys.readouterr().out

    cache = _Cache(tmp_path / 'hit' / 'cachedir')
    old = cache.start({'out.log': 'old\n', 'ref.pdb': 'ref'})
    cache.end(old)
    past = time.time() - 3600
    for path in (cache.root / old, cache.root / f'{old}.status'):
        os.utime(path, (past, past))
    watcher = _started(_plan(tmp_path / 'hit', pattern='*.txt', reference=False), cache)
    time.sleep(0.3)
    assert watcher.runs == 0
    new = cache.start({'out.txt': 'x', 'out.log': 'new\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()
    (job,) = _jobs(cache)
    assert job['log']['location'] == str(cache.root / new / 'out.log')
    assert job['ref']['location'] == str(cache.root / old / 'ref.pdb')

    cache, watcher = _watch(tmp_path / 'nowhere', reference=False)
    key = cache.start({'out.log': '1\n'})
    for line in range(2, 5):
        time.sleep(0.05)
        cache.write(key, 'out.log', f'{line}\n')
    watcher.finish()
    assert watcher.runs == 0
    out = capsys.readouterr().out
    assert out.count('skipped a run: no job of this run wrote ref.pdb') == 1 and 'declared at wf.wic:3:3' in out

    cache = _Cache(tmp_path / 'again' / 'cachedir')
    plan = _plan(tmp_path / 'again', max_times=1)
    earlier = _started(plan, cache)
    cache.end(cache.start({'out.log': 'first\n'}))
    earlier.finish()
    later = _started(plan, cache)
    key = cache.start({'out.log': 'second\n'})
    cache.end(key)
    later.finish()
    first_job, second_job = _jobs(cache)
    assert first_job['log']['location'] == str(cache.root / f'{1:032x}' / 'out.log')
    assert second_job['log']['location'] == str(cache.root / key / 'out.log')
    assert sorted(path.name for path in (cache.root / 'realtime' / 'wf').glob('run-*.log')) == [
        'run-001.log', 'run-002.log']
    assert 'run 2 (the workflow finished) finished' in capsys.readouterr().out


@pytest.mark.needs_cwltool
def test_the_analysis_runs_beside_the_workflow_and_never_changes_its_outcome(
        monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """`--run_local` runs each analysis while the producer writes and once it is done; the run succeeds either way.

    One workflow declares two analyses: `count_lines`, which succeeds, and `fail_lines`,
    which fails. Each is named `live` and `live_2`, and each claim is read from the
    messages that analysis prefixes. At least two runs means one started while the
    workflow ran: the end adds at most one. Each analysis's CWL is handed to image
    extraction with the workflow's.
    """
    root = _project(tmp_path)
    prepared = no_preflight(monkeypatch)
    monkeypatch.chdir(root)

    assert run_cli(monkeypatch, '--yaml', 'live.wic', '--run_local', '--config_file', 'config.json') is None

    out = capfd.readouterr().out
    lines = out.splitlines()
    for name, analysis in (('live', 'count_lines'), ('live_2', 'fail_lines')):
        said = f'real-time analysis {name} ({analysis}): '
        runs = sorted((root / 'cachedir' / 'realtime' / name).glob('run-*.log'))
        assert len(runs) >= 2, out
        assert Path(f'autogenerated/realtime/{name}/{analysis}_only.cwl') in chain.from_iterable(prepared)
        if analysis == 'count_lines':
            last = sorted((root / 'cachedir' / 'realtime' / name).glob('run-*/count.txt'))[-1]
            assert last.read_text(encoding='utf-8').split()[0] == '6'
            assert f'{said}{len(runs)} runs, {len(runs)} succeeded' in lines
        else:
            assert any(line.startswith(said) and 'failed with exit code 1' in line for line in lines), out
            assert f'{said}{len(runs)} runs, 0 succeeded' in lines


@POSIX_ONLY
@pytest.mark.fast
@pytest.mark.parametrize(('request_fields', 'line'), [
    ({}, "Real-time analysis watches the runner's cache, so cwltool caches in cachedir/"),
    ({'generate_run_script': True},
     'Real-time analysis runs only with --run_local; run.sh runs the workflow without it.'),
    ({'runner': 'toil-cwl-runner'},
     'Real-time analysis needs cwltool; toil-cwl-runner runs the workflow without it.'),
], ids=['cwltool', 'run-script', 'toil'])
def test_a_run_watches_its_analyses_only_where_cwltool_runs_and_says_so(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
        request_fields: dict[str, Any], line: str) -> None:
    """The watcher reads cwltool's job directories, so a run that has analyses caches, and says so. A run script or
    another runner cannot run the analyses: the workflow runs, and a line on stderr says so."""
    monkeypatch.chdir(tmp_path)
    fake = install(monkeypatch, tmp_path)

    assert run_local.run(RunRequest('wf', Path('autogenerated'), realtime_plans=(_plan(tmp_path),),
                                    **request_fields)) == 0

    captured = capsys.readouterr()
    watched = not request_fields
    assert line in captured.out if watched else line in captured.err and line not in captured.out
    assert ('real-time analysis wf (measure.wic): 0 runs, 0 succeeded' in captured.out) is watched
    if watched:
        args = fake.record()['argv']
        assert args[args.index('--cachedir') + 1] == str(tmp_path / 'cachedir')
