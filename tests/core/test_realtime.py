"""Real-time analysis: a `cwl_subinterpreter` step declares an analysis Sophios runs beside the workflow."""
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

import sophios.main
import sophios.post_compile
from sophios import realtime, run_local
from sophios.api.python.workflow import Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.input_output import NoAliasDumper
from sophios.ir.realtime import Declaration
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.realtime import Analysis
from sophios.wic_types import StepId, Tool, Tools, Yaml

from .hermetic import compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

ADAPTER_PATH: Final = Path(__file__).resolve().parents[2] / 'cwl_adapters' / 'file_watchers' / 'cwl_subinterpreter.cwl'
TOOLS: Final[Tools] = {
    **SYNTHETIC_TOOLS,
    StepId('cwl_subinterpreter', SYNTHETIC_NS): Tool(str(ADAPTER_PATH),
                                                     yaml.safe_load(ADAPTER_PATH.read_text(encoding='utf-8'))),
}


def _ii(value: Any) -> Yaml:
    return {'wic_inline_input': value}


def _declaration_step(**overrides: Any) -> Yaml:
    """A well-formed declaration: count lines of `a.txt` whenever a `*.txt` changes."""
    bindings: Yaml = {'file_pattern': _ii('*.txt'), 'cwl_tool': _ii('count'), 'max_times': _ii('3'),
                      'config': _ii({'in': {'file': 'a.txt'}})}
    bindings.update(overrides)
    return {'id': 'cwl_subinterpreter', 'in': {key: value for key, value in bindings.items() if value is not None}}


def _step_ids(cwl: Yaml) -> list[str]:
    return [step['id'] for step in cwl['steps']]


def _declaration_line(document: Yaml) -> int:
    """The line the declaration step starts on in `document` as `hermetic.bundle` writes it."""
    text = yaml.dump(document, Dumper=NoAliasDumper, sort_keys=False, line_break='\n', indent=2)
    return next(number for number, line in enumerate(text.splitlines(), start=1)
                if line.strip() == '- id: cwl_subinterpreter')


@pytest.mark.fast
def test_a_declaration_is_not_emitted_and_is_returned_instead() -> None:
    """The declaration step leaves the CWL, and what it declares is in the result."""
    document = {'steps': [{'id': 'mk_file', 'in': {'name': _ii('a.txt')}}, _declaration_step()]}
    result = compile_hermetic(document, tools=TOOLS)

    assert _step_ids(result.artifact.cwl) == ['oracle__step__1__mk_file']
    assert result.artifact.job_inputs == {'oracle__step__1__mk_file___name': 'a.txt'}
    (declaration,) = result.realtime
    assert declaration == Declaration('oracle', 'count', '*.txt', 3, 60, {'in': {'file': 'a.txt'}},
                                      declaration.span)
    assert declaration.span is not None and declaration.span.start_line == _declaration_line(document)


@pytest.mark.fast
def test_a_declaration_in_a_subworkflow_is_taken_out_and_its_workflow_may_be_left_empty() -> None:
    """A subworkflow whose only step is a declaration is emitted with no steps; the declaration names it."""
    producer = {'id': 'mk_file', 'in': {'name': _ii('a.txt')}}
    analysis = subworkflow_step('watch.wic', {'steps': [_declaration_step(interval=_ii(5))]})
    result = compile_hermetic({'steps': [producer, analysis]}, tools=TOOLS)

    (child,) = [child for child in result.artifact.children if child.name == 'watch']
    assert child.cwl['steps'] == []
    assert [(d.document, d.interval) for d in result.realtime] == [('watch', 5)]


@pytest.mark.fast
def test_a_workflow_of_only_a_declaration_compiles_to_no_steps() -> None:
    """The at-least-one-step rule reads what the author wrote, so the workflow is valid and empty."""
    result = compile_hermetic({'steps': [_declaration_step()]}, tools=TOOLS)
    assert result.artifact.cwl['steps'] == []
    assert len(result.realtime) == 1


@pytest.mark.fast
@pytest.mark.parametrize(('port', 'overrides'), [
    ('file_pattern', {'file_pattern': {'wic_alias': 'made'}}),
    ('cwl_tool', {'cwl_tool': None}),
    ('cwl_tool', {'cwl_tool': _ii('')}),
    ('max_times', {'max_times': _ii('many')}),
    ('max_times', {'max_times': _ii(0)}),
    ('interval', {'interval': _ii(-1)}),
    ('config', {'config': _ii('in: a.txt')}),
])
def test_a_malformed_declaration_is_wic044_at_its_step(port: str, overrides: Yaml) -> None:
    """Each malformed input is reported at compile time, at the step and port, before anything runs."""
    producer = {'id': 'mk_file', 'in': {'name': _ii('a.txt')}, 'out': [{'file': {'wic_anchor': 'made'}}]}
    document = {'steps': [producer, _declaration_step(**overrides)]}
    with pytest.raises(SophiosError) as raised:
        compile_hermetic(document, tools=TOOLS)

    (diagnostic,) = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.REALTIME_DECLARATION
    assert diagnostic.span is not None and diagnostic.span.start_line == _declaration_line(document)
    assert diagnostic.locator is not None and diagnostic.locator.port == port


def _analyses(document: Yaml, yml_paths: dict[str, dict[str, Path]] | None = None) -> tuple[Analysis, ...]:
    """Compile `document` and then the analyses it declares, as the CLI does."""
    options, graph_settings = default_compilation_settings()
    return realtime.compile_analyses(compile_hermetic(document, tools=TOOLS).realtime, yml_paths or {}, TOOLS,
                                     options, graph_settings)


def _only_analysis(document: Yaml, yml_paths: dict[str, dict[str, Path]] | None = None) -> Analysis:
    """The one analysis `document` declares."""
    analyses = _analyses(document, yml_paths)
    assert len(analyses) == 1
    return analyses[0]


def _file_inputs(job: Yaml) -> list[str]:
    """The location of each job input that is a File, sorted."""
    return sorted(value['location'] for value in job.values()
                  if isinstance(value, dict) and value.get('class') == 'File')


@pytest.mark.fast
def test_a_tool_analysis_compiles_with_every_config_input_a_literal() -> None:
    """A plain file name and a nested `!ii` in `config: in:` are both literals of the analysis step.

    The shape of the corpus `npt_amber.wic` declaration: read as written, `a.txt` in an
    `in:` would be an edge reference and fail with wic011.
    """
    config = _ii({'in': {'file': 'a.txt', 'name': _ii('b.txt')}})
    analysis = _only_analysis({'steps': [_declaration_step(cwl_tool=_ii('xform'), config=config)]})

    assert analysis.name == 'oracle'
    assert _file_inputs(analysis.artifact.job_inputs) == ['a.txt']
    assert 'b.txt' in analysis.artifact.job_inputs.values()


@pytest.mark.fast
def test_a_wic_analysis_compiles_with_its_config_as_the_sidecar_of_its_steps(tmp_path: Path) -> None:
    """A `.wic` analysis is configured per step, as `wic: steps:`; the shape of `cwl_subinterpreter_protein.wic`."""
    (tmp_path / 'measure.wic').write_text(yaml.safe_dump({'steps': [{'id': 'count'}, {'id': 'count'}]}),
                                          encoding='utf-8')
    config = _ii({'(1, count)': {'in': {'file': 'a.txt'}}, '(2, count)': {'in': {'file': 'b.txt'}}})
    declaration = _declaration_step(cwl_tool=_ii('measure.wic'), config=config)
    analysis = _only_analysis({'steps': [declaration]}, {'global': {'measure': tmp_path / 'measure.wic'}})

    assert _file_inputs(analysis.artifact.job_inputs) == ['a.txt', 'b.txt']


@pytest.mark.fast
def test_two_declarations_in_one_workflow_get_distinct_names() -> None:
    """Each analysis keeps its files under its own name; a repeated document name gets a suffix."""
    analyses = _analyses({'steps': [_declaration_step(), _declaration_step()]})
    assert [analysis.name for analysis in analyses] == ['oracle', 'oracle_2']


@pytest.mark.fast
def test_an_analysis_that_does_not_compile_is_wic044_at_the_declaration() -> None:
    """An unknown analysis fails the compile at the declaration, before anything runs."""
    document = {'steps': [{'id': 'mk_file', 'in': {'name': _ii('a.txt')}}, _declaration_step(cwl_tool=_ii('nope'))]}
    with pytest.raises(SophiosError) as raised:
        _analyses(document)

    assert {diagnostic.code for diagnostic in raised.value.diagnostics} == {SophiosErrorCode.REALTIME_DECLARATION}
    assert all(diagnostic.span is not None and diagnostic.span.start_line == _declaration_line(document)
               for diagnostic in raised.value.diagnostics)
    assert "'nope'" in str(raised.value)


@pytest.mark.fast
def test_the_manifest_lists_each_analysis_and_goes_when_there_are_none(tmp_path: Path) -> None:
    """`write` writes each analysis and a manifest naming it, and returns its plan; with none, no manifest is left."""
    analysis = _only_analysis({'steps': [_declaration_step(interval=_ii('5'))]})
    (plan,) = realtime.write((analysis,), tmp_path, 'wf', tmp_path / 'project')

    (entry,) = json.loads(realtime.manifest_path(tmp_path, 'wf').read_text(encoding='utf-8'))
    assert {key: entry[key] for key in ('name', 'analysis', 'file_pattern', 'max_times', 'interval')} == {
        'name': 'oracle', 'analysis': 'count', 'file_pattern': '*.txt', 'max_times': 3, 'interval': 5}
    assert (tmp_path / entry['cwl']).is_file() and (tmp_path / entry['inputs']).is_file()
    assert entry['root_dir'] == str(tmp_path / 'project')
    assert (plan.name, plan.max_times, plan.interval) == ('oracle', 3, 5)
    assert (plan.cwl, plan.inputs) == (tmp_path / entry['cwl'], tmp_path / entry['inputs'])

    assert not realtime.write((), tmp_path, 'wf', tmp_path)
    assert not realtime.manifest_path(tmp_path, 'wf').exists()


# --------------------------------------------------------------------------
# The watcher, against a cachedir laid out as cwltool lays it out
# --------------------------------------------------------------------------

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
        root.mkdir(exist_ok=True)
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
        """Append `text` to the file `name` of job `key`."""
        path = self.root / key / name
        with path.open('a', encoding='utf-8') as stream:
            stream.write(text)

    def end(self, key: str, status: str = 'success') -> None:
        """End job `key` with `status`, as cwltool does."""
        (self.root / f'{key}.status').write_text(status, encoding='utf-8')


def _until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    """Wait for `condition`, failing after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.02)


def _jobs(cachedir: Path) -> list[Yaml]:
    """What each analysis run was given, in order."""
    runs = sorted((cachedir / 'realtime' / 'wf').glob('run-*/job.json'))
    return [json.loads(path.read_text(encoding='utf-8')) for path in runs]


def _watcher(tmp_path: Path, plan: realtime.Plan, command: realtime.Command | None = None) -> realtime.Watcher:
    """A started watcher of `tmp_path/cachedir` that polls fast."""
    watcher = realtime.Watcher(plan, tmp_path / 'cachedir', command or _command(), poll=0.02)
    watcher.start()
    return watcher


@pytest.mark.fast
def test_a_job_that_starts_long_after_the_watcher_is_still_analysed(tmp_path: Path) -> None:
    """Polling has no budget: `max_times` counts runs, not polls."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, max_times=2))
    time.sleep(0.5)                                            # 25 polls with nothing to see
    key = cache.start({'sub/out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()

    (job,) = _jobs(tmp_path / 'cachedir')
    assert job['log'] == {'class': 'File', 'location': str(cache.root / key / 'sub' / 'out.log'),
                          'format': 'edam:format_2330'}
    assert job['ref']['location'] == str(tmp_path / 'project' / 'ref.pdb')
    assert job['name'] == 'out.log'


@pytest.mark.fast
def test_a_later_workflow_run_numbers_its_analysis_runs_after_the_earlier_ones(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Two runs of the workflow share the cachedir; the second keeps the first's outputs and logs."""
    cache = _Cache(tmp_path / 'cachedir')
    plan = _plan(tmp_path, max_times=1)
    first = _watcher(tmp_path, plan)
    cache.end(cache.start({'out.log': 'first\n'}))
    first.finish()
    second = _watcher(tmp_path, plan)
    key = cache.start({'out.log': 'second\n'})
    cache.end(key)
    second.finish()

    first_job, second_job = _jobs(tmp_path / 'cachedir')
    assert first_job['log']['location'] == str(cache.root / f'{1:032x}' / 'out.log')
    assert second_job['log']['location'] == str(cache.root / key / 'out.log')
    assert sorted(path.name for path in (tmp_path / 'cachedir' / 'realtime' / 'wf').glob('run-*.log')) == [
        'run-001.log', 'run-002.log']
    assert 'run 2 (the workflow finished) finished' in capsys.readouterr().out


@pytest.mark.fast
def test_a_growing_file_triggers_once_per_interval_and_once_more_at_the_end(tmp_path: Path) -> None:
    """A file that keeps changing is analysed once per `interval`, and its last state at the end."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, interval=60))
    key = cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    for line in range(2, 6):
        cache.write(key, 'out.log', f'{line}\n')
        time.sleep(0.05)
    assert watcher.runs == 1
    watcher.finish()
    assert watcher.runs == 2


@pytest.mark.fast
def test_a_job_that_succeeds_triggers_at_once_and_its_run_is_the_last(capsys: pytest.CaptureFixture[str],
                                                                      tmp_path: Path) -> None:
    """The step finishing is the moment its outputs are complete: no `interval` delays that run."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, interval=60))
    key = cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    cache.write(key, 'out.log', '2\n')
    cache.end(key)
    _until(lambda: watcher.runs == 2)
    watcher.finish()

    assert watcher.runs == 2 and watcher.succeeded == 2
    assert 'the job that wrote out.log finished' in capsys.readouterr().out


@pytest.mark.fast
def test_changes_during_a_run_make_one_follow_up_run(tmp_path: Path) -> None:
    """Three changes while an analysis runs make one more run, not three."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path), _command(seconds=0.6))
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


@pytest.mark.fast
def test_runs_while_the_workflow_runs_leave_room_for_the_final_run(tmp_path: Path) -> None:
    """With `max_times` 3, two runs happen while the workflow runs, however much changes; the third is the end's."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, max_times=3))
    key = cache.start({'out.log': '1\n'})
    deadline = time.monotonic() + 2.0                          # changes all along, far more than three runs' worth
    while time.monotonic() < deadline:
        cache.write(key, 'out.log', 'more\n')
        time.sleep(0.05)
    assert watcher.runs == 2
    watcher.finish()
    assert watcher.runs == 3
    assert len(_jobs(tmp_path / 'cachedir')) == 3


@pytest.mark.fast
def test_a_job_from_an_earlier_run_is_not_a_trigger_but_its_files_are_used(tmp_path: Path) -> None:
    """A cache hit leaves its job alone, so it never triggers; this run reused it, so its files resolve."""
    cache = _Cache(tmp_path / 'cachedir')
    old = cache.start({'out.log': 'old\n', 'ref.pdb': 'ref'})
    cache.end(old)
    past = time.time() - 3600
    for path in (cache.root / old, cache.root / f'{old}.status'):
        os.utime(path, (past, past))
    watcher = _watcher(tmp_path, _plan(tmp_path, pattern='*.txt', reference=False))
    time.sleep(0.3)
    assert watcher.runs == 0
    new = cache.start({'out.txt': 'x', 'out.log': 'new\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()

    (job,) = _jobs(tmp_path / 'cachedir')
    assert job['log']['location'] == str(cache.root / new / 'out.log')
    assert job['ref']['location'] == str(cache.root / old / 'ref.pdb')


@pytest.mark.fast
def test_a_failed_job_means_no_final_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The outputs of a failed step are not a result to analyse; a line says why there is no final run."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, interval=60))
    key = cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    cache.write(key, 'out.log', '2\n')
    cache.end(key, 'permanentFail')
    watcher.finish()

    assert watcher.runs == 1
    assert 'no final run: the job that wrote out.log failed' in capsys.readouterr().out


@pytest.mark.fast
def test_a_failed_analysis_is_a_line(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """A failed run names its log; the summary counts it."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, interval=60), _command(exit_code=3))
    cache.start({'out.log': '1\n'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()

    out = capsys.readouterr().out
    assert 'run 1 (out.log changed) failed with exit code 3; log: ' in out
    assert '1 runs, 0 succeeded' in out


@pytest.mark.fast
def test_cancel_terminates_a_running_analysis(tmp_path: Path) -> None:
    """Ctrl-C does not wait for an analysis to finish."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path), _command(seconds=60))
    cache.start({'out.log': '1\n'})
    _until((cache.root / 'realtime' / 'wf' / 'run-001' / 'job.json').exists)
    began = time.monotonic()
    watcher.cancel()
    assert time.monotonic() - began < 10
    assert watcher.succeeded == 0


@pytest.mark.fast
def test_a_file_written_by_two_jobs_is_named_and_the_newest_used(capsys: pytest.CaptureFixture[str],
                                                                 tmp_path: Path) -> None:
    """Two steps of this run wrote `out.log`: the analysis gets the newer, and a line names both."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, pattern='*.trr', interval=60))
    first = cache.start({'out.log': 'first\n'})
    time.sleep(0.05)
    second = cache.start({'out.log': 'second\n', 'prod.trr': 'x'})
    _until(lambda: watcher.runs == 1)
    watcher.finish()

    (job,) = _jobs(tmp_path / 'cachedir')
    assert job['log']['location'] == str(cache.root / second / 'out.log')
    assert f'out.log was written by 2 jobs of this run ({first}, {second}); using the newest' in capsys.readouterr().out


@pytest.mark.fast
def test_a_file_found_nowhere_skips_the_run_with_one_line(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The corpus `cwl_subinterpreter_complex.wic` reads, as a literal, a file its own analysis writes."""
    cache = _Cache(tmp_path / 'cachedir')
    watcher = _watcher(tmp_path, _plan(tmp_path, reference=False))
    key = cache.start({'out.log': '1\n'})
    for line in range(2, 5):
        time.sleep(0.05)
        cache.write(key, 'out.log', f'{line}\n')
    watcher.finish()

    assert watcher.runs == 0
    out = capsys.readouterr().out
    assert out.count('skipped a run: no job of this run wrote ref.pdb') == 1
    assert 'declared at wf.wic:3:3' in out


# --------------------------------------------------------------------------
# End to end: the CLI runs the workflow with cwltool and the analysis beside it
# --------------------------------------------------------------------------

#: Writes `out.log` one line a second for six seconds: long enough to be watched while it grows.
SLOW_LOG: Final = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: [bash, -c, 'for i in 1 2 3 4 5 6; do echo $i >> out.log; sleep 1; done']
inputs: {}
outputs:
  log: {type: File, outputBinding: {glob: out.log}}
'''

#: Counts the lines of a log, or, with `false` as its command, fails.
COUNT_LINES: Final = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: [{command}]
inputs:
  log: {{type: File, inputBinding: {{position: 1}}}}
stdout: count.txt
outputs:
  count: {{type: stdout}}
'''

LIVE: Final = '''steps:
  - id: slow_log
  - id: cwl_subinterpreter
    in:
      file_pattern: !ii out.log
      cwl_tool: !ii {analysis}
      max_times: !ii '5'
      interval: !ii 1
      config: !ii
        in:
          log: out.log
'''


#: A producer and both analyses, `count_lines` and `fail_lines`, each watching its log.
LIVE_BOTH: Final = '''steps:
  - id: slow_log
  - id: cwl_subinterpreter
    in:
      file_pattern: !ii out.log
      cwl_tool: !ii count_lines
      max_times: !ii '5'
      interval: !ii 1
      config: !ii
        in:
          log: out.log
  - id: cwl_subinterpreter
    in:
      file_pattern: !ii out.log
      cwl_tool: !ii fail_lines
      max_times: !ii '5'
      interval: !ii 1
      config: !ii
        in:
          log: out.log
'''


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
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'slow_log.cwl').write_text(SLOW_LOG, encoding='utf-8')
    (tools / 'count_lines.cwl').write_text(COUNT_LINES.format(command='wc, -l'), encoding='utf-8')
    (tools / 'fail_lines.cwl').write_text(COUNT_LINES.format(command="'false'"), encoding='utf-8')
    (tmp_path / 'live.wic').write_text(LIVE_BOTH, encoding='utf-8')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'search_paths_cwl': {'global': [str(tools), str(ADAPTER_PATH.parent)]},
                                  'search_paths_wic': {'global': [str(tmp_path)]}}), encoding='utf-8')
    extracted: list[Path] = []
    monkeypatch.setattr(sophios.post_compile, 'verify_container_engine_config', lambda *_a, **_k: None)
    monkeypatch.setattr(sophios.post_compile, 'cwl_docker_extract', lambda _e, _p, path: extracted.append(Path(path)))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', 'live.wic', '--run_local', '--config_file', str(config)])

    sophios.main.main()                                        # returns: the workflow succeeded

    out = capfd.readouterr().out
    lines = out.splitlines()
    for name, analysis in (('live', 'count_lines'), ('live_2', 'fail_lines')):
        said = f'real-time analysis {name} ({analysis}): '
        runs = sorted((tmp_path / 'cachedir' / 'realtime' / name).glob('run-*.log'))
        assert len(runs) >= 2, out
        assert Path(f'autogenerated/realtime/{name}/{analysis}_only.cwl') in extracted
        if analysis == 'count_lines':
            last = sorted((tmp_path / 'cachedir' / 'realtime' / name).glob('run-*/count.txt'))[-1]
            assert last.read_text(encoding='utf-8').split()[0] == '6'
            assert f'{said}{len(runs)} runs, {len(runs)} succeeded' in lines
        else:
            assert any(line.startswith(said) and 'failed with exit code 1' in line for line in lines), out
            assert f'{said}{len(runs)} runs, 0 succeeded' in lines


@pytest.mark.fast
def test_an_analysis_tool_builds_its_image_from_the_dockerfile_beside_it(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    """A relative `dockerFile` `$include` names a file beside the tool, wherever the analysis CWL is written."""
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'slow_log.cwl').write_text(SLOW_LOG, encoding='utf-8')
    (tools / 'count_lines.cwl').write_text(COUNT_LINES.format(command='wc, -l') + '''hints:
  DockerRequirement:
    dockerPull: count_lines
    dockerFile: {$include: Dockerfile_count}
''', encoding='utf-8')
    (tmp_path / 'live.wic').write_text(LIVE.format(analysis='count_lines'), encoding='utf-8')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'search_paths_cwl': {'global': [str(tools), str(ADAPTER_PATH.parent)]},
                                  'search_paths_wic': {'global': [str(tmp_path)]}}), encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', 'live.wic', '--generate_cwl_workflow',
                                     '--config_file', str(config)])

    sophios.main.main()

    (tool,) = [yaml.safe_load(path.read_text(encoding='utf-8'))
               for path in Path('autogenerated/realtime/live').rglob('*.cwl')
               if 'Dockerfile_count' in path.read_text(encoding='utf-8')]
    assert tool['hints']['DockerRequirement']['dockerFile'] == {'$include': str(tools / 'Dockerfile_count')}


@pytest.mark.fast
def test_a_run_with_real_time_analysis_gives_cwltool_a_cachedir(monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str],
                                                                tmp_path: Path) -> None:
    """The watcher reads cwltool's job directories, so a run that has analyses caches, and says so."""
    monkeypatch.chdir(tmp_path)
    given: list[list[str]] = []

    def cwltool_main(args: list[str]) -> int:
        given.append(args)
        return 0
    monkeypatch.setattr(run_local.cwltool.main, 'main', cwltool_main)

    assert run_local.run_local({'container_engine': 'docker', 'cwl_runner': 'cwltool'}, False,
                               passthrough_args=[], workflow_name='wf', basepath='autogenerated',
                               realtime_plans=(_plan(tmp_path),)) == 0

    args = given[0]
    assert args[args.index('--cachedir') + 1] == str(tmp_path / 'cachedir')
    out = capsys.readouterr().out
    assert "Real-time analysis watches the runner's cache, so cwltool caches in cachedir/" in out
    assert 'real-time analysis wf (measure.wic): 0 runs, 0 succeeded' in out


@pytest.mark.fast
@pytest.mark.parametrize(('run_args', 'line'), [
    ({'cwl_runner': 'cwltool', 'generate_run_script': 'yes'},
     'Real-time analysis runs only with --run_local; run.sh runs the workflow without it.'),
    ({'cwl_runner': 'toil-cwl-runner'},
     'Real-time analysis needs cwltool; toil-cwl-runner runs the workflow without it.'),
])
def test_a_run_that_cannot_watch_says_so_and_runs_the_workflow(monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                                               run_args: dict[str, str], line: str) -> None:
    """A run script or another runner cannot run the analyses: the workflow runs, and a line says so."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_local.toil.cwl.cwltoil, 'main', lambda args: 0)

    assert run_local.run_local({'container_engine': 'docker', **run_args}, False,
                               passthrough_args=[], workflow_name='wf', basepath='autogenerated',
                               realtime_plans=(_plan(tmp_path),)) == 0

    captured = capsys.readouterr()
    assert line in captured.err
    assert line not in captured.out
    assert 'real-time analysis wf' not in captured.out + captured.err


@pytest.mark.fast
def test_the_python_api_declares_an_analysis_and_run_prepares_it(monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str],
                                                                 tmp_path: Path) -> None:
    """A `Step` built from the adapter is a declaration: never emitted, compiled and pulled by `run`."""
    adapters = ADAPTER_PATH.parents[1]
    touch = Step(clt_path=adapters / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    declaration = Step(clt_path=ADAPTER_PATH)
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    workflow = Workflow([touch, declaration], 'py_live')
    extracted: list[Path] = []
    monkeypatch.setattr(sophios.post_compile, 'verify_container_engine_config', lambda *_a, **_k: None)
    monkeypatch.setattr(sophios.post_compile, 'cwl_docker_extract', lambda _e, _p, path: extracted.append(Path(path)))
    monkeypatch.chdir(tmp_path)

    assert [step['id'] for step in workflow.compile().cwl_workflow['steps']] == ['py_live__step__1__touch']
    workflow.run(run_args_dict={'generate_run_script': 'yes'})

    (entry,) = json.loads(realtime.manifest_path(Path('autogenerated'), 'py_live').read_text(encoding='utf-8'))
    assert (entry['analysis'], entry['max_times']) == ('touch', 2)
    assert extracted == [Path('autogenerated/py_live.cwl'), Path('autogenerated/realtime/py_live/touch_only.cwl')]
    assert 'Real-time analysis runs only with --run_local' in capsys.readouterr().err


@pytest.mark.fast
def test_the_python_api_finds_a_wic_analysis_through_workflow_paths(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    """A `.wic` named by `cwl_tool` is found through `run(workflow_paths=...)`, as `from_wic` finds a nested one."""
    adapters = ADAPTER_PATH.parents[1]
    (tmp_path / 'measure.wic').write_text(yaml.safe_dump({'steps': [{'id': 'touch'}]}), encoding='utf-8')
    touch = Step(clt_path=adapters / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    declaration = Step(clt_path=ADAPTER_PATH)
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'measure.wic'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'(1, touch)': {'in': {'filename': 'again.txt'}}}
    workflow = Workflow([touch, declaration], 'py_wic')
    monkeypatch.setattr(sophios.post_compile, 'verify_container_engine_config', lambda *_a, **_k: None)
    monkeypatch.setattr(sophios.post_compile, 'cwl_docker_extract', lambda *_a: None)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SophiosError) as raised:
        workflow.run(run_args_dict={'generate_run_script': 'yes'})
    (diagnostic,) = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.REALTIME_DECLARATION
    assert 'measure.wic' in diagnostic.message and 'wic013' in diagnostic.message

    workflow.run(run_args_dict={'generate_run_script': 'yes'},
                 workflow_paths={'global': {'measure': tmp_path / 'measure.wic'}})

    (entry,) = json.loads(realtime.manifest_path(Path('autogenerated'), 'py_wic').read_text(encoding='utf-8'))
    assert entry['analysis'] == 'measure.wic'
    assert Path('autogenerated/realtime/py_wic/measure_only.cwl').is_file()
