"""What each backend gives a run: the runner's command line, and what it reads back from the runner."""
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import cwltool.argparser
import pytest
from hypothesis import example, given, strategies as st
from toil.cwl.cwltoil import get_options

from sophios import backends, preflight, run_setup
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.workflow import Step, Workflow
from sophios.backends import RunRequest
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.lang.diagnostics import Diagnostic, Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode

from .budgets import budget
from .nextflow.testkit import REPO_ROOT, runtime_workflow


def _argv(request: RunRequest) -> list[str]:
    return list(backends.backend(request.target, request.runner).launch(request).argv)


class _Frozen(backends.datetime):
    @classmethod
    def now(cls, tz: Any = None) -> '_Frozen':
        return cls(2026, 10, 9, 12, 2, 16)


#: What each CWL runner's own parser makes of its arguments.
_READ: dict[str, Callable[[list[str]], Any]] = {
    'cwltool': cwltool.argparser.arg_parser().parse_args, 'toil-cwl-runner': get_options}
#: Arguments each runner takes, with the option each sets as its parser reads it.
_PASSTHROUGH: dict[str, list[tuple[tuple[str, ...], str, Any]]] = {
    'cwltool': [(('--parallel',), 'parallel', True), (('--timestamps',), 'timestamps', True),
                (('--no-read-only',), 'no_read_only', True)],
    'toil-cwl-runner': [(('--maxCores', '2'), 'maxCores', 2), (('--retryCount', '3'), 'retryCount', 3),
                        (('--logLevel', 'DEBUG'), 'logLevel', 'DEBUG')]}
#: What a request leaves out, as RunRequest's docstring says.
_DEFAULTS: dict[str, Any] = {'outdir': '', 'cachedir': '', 'quiet': True, 'provenance': True,
                             'container_engine': 'docker', 'documents': None}
_PATHS = st.builds(lambda root, parts: os.path.join(root, *parts), st.sampled_from(['', '/tmp']),
                   st.lists(st.from_regex('[a-z_]{1,6}', fullmatch=True), min_size=1, max_size=2))
_REQUESTS = st.tuples(
    st.from_regex('[A-Za-z0-9_][A-Za-z0-9_.-]{0,11}', fullmatch=True), _PATHS,
    st.sampled_from(sorted(_READ)).flatmap(lambda runner: st.tuples(
        st.just(runner), st.lists(st.sampled_from(_PASSTHROUGH[runner]), unique=True))),
    st.fixed_dictionaries({}, optional={
        'outdir': st.one_of(st.just(''), _PATHS), 'cachedir': st.one_of(st.just(''), _PATHS), 'quiet': st.booleans(),
        'provenance': st.booleans(),
        'container_engine': st.sampled_from(['docker', 'singularity', 'podman', 'udocker']),
        'documents': st.lists(_PATHS, min_size=1, max_size=2).map(tuple)}))


@pytest.mark.fast
@budget(50)
@given(_REQUESTS)
@example(('wf', 'exec', ('cwltool', []), {'outdir': 'workflow_output'}))
@example(('wf', 'exec', ('toil-cwl-runner', []), {'outdir': 'workflow_output'}))
@example(('bbbc', 'base', ('cwltool', []), {}))
@example(('python_script_demo', 'base', ('cwltool', []), {}))
@example(('bbbc', 'base', ('toil-cwl-runner', []), {}))
@example(('python_script_demo', 'base', ('toil-cwl-runner', []), {}))
@example(('wf', 'base', ('cwltool', []), {'quiet': False, 'cachedir': 'cache'}))
def test_each_cwl_runner_reads_its_command_line_as_the_request_says(
        drawn: tuple[str, str, tuple[str, list[Any]], dict[str, Any]]) -> None:
    """As each runner's own parser reads it. The default outdir is named after the workflow, as provenance, the
    summary and the job store are, so two workflows started in the same second from one basepath never share it.
    `--quiet` is on for callers that say nothing and off when `quiet=False`, so `--debug` can be heard; Toil is never
    quiet. The passthrough arguments come before the documents."""
    name, basepath, (runner, passthrough), fields = drawn
    asked = _DEFAULTS | fields
    with mock.patch.object(backends, 'datetime', _Frozen):
        argv = _argv(RunRequest(name, Path(basepath), runner=runner, passthrough_args=tuple(
            argument for arguments, _, _ in passthrough for argument in arguments), **fields))
    read, base, cwl = _READ[runner](argv[1:]), os.path.realpath(basepath), runner == 'cwltool'
    assert argv.count('--outdir') == 1
    assert read.outdir == (os.path.realpath(asked['outdir']) if asked['outdir'] else
                           f"{base}/outdir_{'cwltool' if cwl else 'toil'}_{name}_2026_10_09_12.02.16")
    assert read.quiet is (cwl and asked['quiet'])
    assert (read.cachedir or None) == (os.path.abspath(asked['cachedir']) if cwl and asked['cachedir'] else None)
    assert read.provenance == (f'{base}/provenance/{name}' if asked['provenance'] else None)
    docker = read.user_space_docker_cmd or ('podman' if read.podman else 'docker')
    assert (read.singularity, 'singularity' if read.singularity else docker) == (
        asked['container_engine'] == 'singularity', asked['container_engine'])
    documents = [read.workflow, *read.job_order] if cwl else [read.cwltool, *read.cwljob]
    assert documents == list(asked['documents'] or [f'{base}/{name}.cwl', f'{base}/{name}_inputs.yml'])
    assert [getattr(read, option) for _, option, _ in passthrough] == [value for _, _, value in passthrough]
    assert ((read.write_summary,) if cwl else (read.jobStore, read.clean)) == (
        (f'{base}/output_{name}.json',) if cwl else (f'file:{base}/jobStore_{name}', 'always'))


@pytest.mark.fast
@pytest.mark.parametrize(('passthrough', 'provenance'), [
    ((), True), (('--batchSystem', 'slurm'), False), (('--batchSystem=slurm',), True)])
def test_toil_keeps_no_provenance_when_an_argument_is_slurm(tmp_path: Path, passthrough: tuple[str, ...],
                                                            provenance: bool) -> None:
    """Toil writes no provenance under Slurm: an argument that is exactly `slurm` drops the flag."""
    cmd = _argv(RunRequest('wf', tmp_path, runner='toil-cwl-runner', passthrough_args=passthrough))
    assert ('--provenance' in cmd) is provenance


@pytest.mark.fast
def test_the_runner_gets_this_processs_environment_with_the_users_on_top(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('SOPHIOS_TEST_KEPT', 'kept')
    monkeypatch.setenv('SOPHIOS_TEST_REPLACED', 'old')
    launch = backends.backend('cwl', 'cwltool').launch(
        RunRequest('wf', tmp_path, env={'SOPHIOS_TEST_REPLACED': 'new'}))
    assert launch.env['SOPHIOS_TEST_KEPT'] == 'kept' and launch.env['SOPHIOS_TEST_REPLACED'] == 'new'


@pytest.mark.fast
def test_an_unknown_runner_is_refused_naming_the_runners() -> None:
    with pytest.raises(ValueError, match="No backend runs cwl with 'cwl-runner'; the runners are cwltool"):
        backends.backend('cwl', 'cwl-runner')


#: Lines that end no run: any text with no `[` (cwltool's lines start with one) and no line break.
_TEXT = st.text(st.characters(exclude_characters='[\n\r'), max_size=20)


@st.composite
def _interleaved(draw: st.DrawFn, runs: list[list[tuple[str, Any]]], others: st.SearchStrategy[str],
                 dressed: st.SearchStrategy[str]) -> tuple[list[str], dict[str, Any]]:
    """The runs' lines interleaved, each in its order, among `others`, dressed; and what their tags say, in order."""
    queues = [list(run) for run in runs] + [[(line, None)] for line in draw(st.lists(others, max_size=4))]
    lines: list[str] = []
    said: dict[str, Any] = {}
    while any(queues):
        line, tag = draw(st.sampled_from([queue for queue in queues if queue])).pop(0)
        lines.append(draw(dressed).format(line))
        if tag is not None:
            said.setdefault(*tag)
    return lines, said


@st.composite
def _cwltool_logs(draw: st.DrawFn) -> tuple[list[str], dict[str, Any]]:
    """A cwltool log, and each job it says did not succeed with how it ended, as cwltool/job.py prints a job: its exit
    status or the signal that ended it when it did not succeed, then `completed <status>`; or neither line yet."""
    runs = []
    for job in draw(st.lists(st.text('ab_~@.:-0', min_size=1, max_size=6), unique=True, max_size=4)):
        ended = draw(st.one_of(st.none(), st.integers(1, 255), st.sampled_from([kind.name for kind in signal.Signals])))
        completed = draw(st.sampled_from([None, 'success', 'permanentFail', 'temporaryFail']))
        said = [] if ended is None else [f'[job {job}] exited with status: {ended}' if isinstance(ended, int)
                                         else f'[job {job}] was terminated by signal: {ended}']
        runs.append([(line, None) for line in said] + ([] if completed is None else [
            (f'[job {job}] completed {completed}', None if completed == 'success' else (job, ended))]))
    # Bare, after its level and a time, or in colour; among others, cwltool/workflow_job.py's that end no job.
    return draw(_interleaved(runs, st.one_of(_TEXT, st.sampled_from([
        '[step a] completed permanentFail', '[workflow wf] completed success',
        '[a] Iteration 1 completed permanentFail'])), st.sampled_from([
            '{}', 'INFO {}', 'WARNING {}', '[2026-10-22 10:00:00] WARNING {}',
            '\x1b[1;30mWARNING\x1b[0m \x1b[33m{}\x1b[0m'])))


@pytest.mark.fast
@budget(100)
@given(_cwltool_logs())
@example((['WARNING [job a] exited with status: 3', 'WARNING [job a] completed permanentFail',
           'INFO [job b] completed success',
           '\x1b[1;30mWARNING\x1b[0m \x1b[33m[job c] was terminated by signal: SIGKILL\x1b[0m',
           '\x1b[1;30mWARNING\x1b[0m \x1b[33m[job c] completed permanentFail\x1b[0m',
           '[2026-10-22 10:00:00] WARNING [job d] completed permanentFail'], {'a': 3, 'c': 'SIGKILL', 'd': None}))
def test_failed_steps_are_read_from_the_runners_log_lines(log: tuple[list[str], dict[str, Any]]) -> None:
    """A job is failed by its `completed` line, in that line's order, with the exit status or signal it ended with,
    else None; a job that succeeded, or has not completed, is not, and nor is a step's or a workflow's line."""
    lines, failed = log
    assert list(backends.failed_steps(lines).items()) == list(failed.items())


#: What Toil prints, as printed and read back: a line, never blank nor starting with `{`, or an object.
_PRINTED = st.one_of(st.text(st.characters(exclude_characters='\n\r'), min_size=1, max_size=20).filter(
    lambda line: line.strip(' \t') and not line.startswith('{')).map(lambda line: (line, None)), st.builds(
    lambda value, indent: (json.dumps(value, indent=indent), value), st.dictionaries(st.text(max_size=4), st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
        lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=4), inner, max_size=3),
        max_leaves=6), max_size=3), st.sampled_from([None, 4])))
_ECHOED = {'echo~stdout': {'class': 'File', 'path': '/out/stdout'}}


@pytest.mark.fast
@budget(100)
@given(st.lists(_PRINTED, max_size=4), st.sampled_from([0, 1, 2]))
@example([('no object here', None)], 0)
@example([('INFO a line before', None), (json.dumps(_ECHOED, indent=4), _ECHOED)], 0)
def test_toil_writes_the_output_object_it_printed_last_and_no_other(printed: list[tuple[str, Any]],
                                                                    exit_code: int) -> None:
    """Toil prints the output object and writes no summary, so it is written where cwltool's is: the object a run
    that succeeded printed last, and nothing for a run that failed or printed none last."""
    stdout = ''.join(f'{line}\n' for line, _ in printed)
    expected = printed[-1][1] if printed and exit_code == 0 else None
    with tempfile.TemporaryDirectory() as base:
        request = RunRequest('wf', Path(base), runner='toil-cwl-runner')
        outcome = backends.backend('cwl', 'toil-cwl-runner').outcome(request, exit_code, stdout, Path(base, 'no.log'))
        summary = Path(base).resolve() / 'output_wf.json'
        assert (outcome.outputs, outcome.summary) == (expected, None if expected is None else summary)
        assert (json.loads(summary.read_text(encoding='utf-8')) if summary.exists() else None) == expected


#: What Nextflow prints for a failed task of a scattered process, as the pinned version prints it.
_NEXTFLOW_FAILED_LOG = [
    "[PROCESS 72/fd70aa] failing:bad_step (2)",
    "[ERROR] ERROR ~ Error executing process > 'failing:bad_step (2)'",
    "Caused by:",
    "  Process `failing:bad_step (2)` terminated with an error exit status (7)",
    "[ERROR] failing:bad_step (2)",
    "exit: 7",
    "[ERROR] ERROR ~ Error executing process > 'failing:copy'",
    "  Missing output file(s) `copy.txt` expected by process `failing:copy`",
]


def _nextflow_request(tmp_path: Path, **fields: Any) -> RunRequest:
    """A Nextflow run of `wf` from `tmp_path`, whose names file `prepare` would have written."""
    (tmp_path / 'nextflow_wf').mkdir()
    (tmp_path / 'nextflow_wf' / 'nextflow_names.json').write_text(json.dumps({
        'processes': {'bad_step': 'bad-step', 'copy': 'child~copy'},
        'outputs': {'copy_result': 'copy~result', 'grid': 'grid', 'text': 'text', 'skipped': 'skipped'},
    }), encoding='utf-8')
    return RunRequest('wf', tmp_path, runner='nextflow', target='nextflow', **fields)


@pytest.mark.fast
def test_nextflow_reads_only_the_generated_configuration_from_the_workflows_own_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline, its parameters and its work directory, all in `nextflow_<workflow>/` under the base path, no
    ANSI log, then the passthrough arguments, with the environment a CWL runner gets and the v2 script parser."""
    monkeypatch.setenv('SOPHIOS_TEST_KEPT', 'kept')
    monkeypatch.setenv('NXF_SYNTAX_PARSER', 'v1')
    base = tmp_path.resolve() / 'nextflow_wf'
    request = RunRequest('wf', tmp_path, runner='nextflow', target='nextflow', env={'TOKEN': 'x'},
                         passthrough_args=('-process.executor', 'slurm', '-resume'))
    launch = backends.backend('nextflow', 'nextflow').launch(request)
    assert launch.argv == ('nextflow', '-C', f'{base}/nextflow.config', 'run', f'{base}/workflow.nf',
                           '-params-file', f'{base}/nextflow_params.json', '-work-dir', f'{base}/work',
                           '-ansi-log', 'false', '-process.executor', 'slurm', '-resume')
    assert (launch.cwd, launch.outdir) == (base, f'{base}/work')
    assert launch.env['SOPHIOS_TEST_KEPT'] == 'kept' and launch.env['TOKEN'] == 'x'
    assert launch.env['NXF_SYNTAX_PARSER'] == 'v2'


@st.composite
def _nextflow_logs(draw: st.DrawFn) -> tuple[list[str], dict[str, Any]]:
    """A Nextflow log, as the pinned version prints a failed task, and each process it says failed, in the order of
    its first line, with its exit status, or None for missing outputs."""
    runs = []
    for process in draw(st.lists(st.from_regex('[a-z_][a-z0-9_]{0,5}', fullmatch=True), unique=True, max_size=4)):
        workflow = ':'.join(draw(st.lists(st.from_regex('[a-z][a-z0-9_]{0,5}', fullmatch=True), min_size=1,
                                          max_size=3)))
        task = f"{workflow}:{process}{draw(st.sampled_from(['', ' (1)', ' (12)']))}"
        status = draw(st.one_of(st.none(), st.integers(1, 255)))
        said = ([f'  Missing output file(s) `out.txt` expected by process `{workflow}:{process}`'] if status is None
                else ['Caused by:', f'  Process `{task}` terminated with an error exit status ({status})',
                      f'[ERROR] {task}', f'exit: {status}'])
        runs.append([(f'[PROCESS 72/fd70aa] {task}', None),
                     (f"[ERROR] ERROR ~ Error executing process > '{task}'", (process, status)),
                     *[(line, None) for line in said]])
    return draw(_interleaved(runs, _TEXT, st.just('{}')))


@pytest.mark.fast
@budget(100)
@given(_nextflow_logs())
@example((_NEXTFLOW_FAILED_LOG, {'bad_step': 7, 'copy': None}))
def test_failed_nextflow_processes_are_read_from_its_log_lines(log: tuple[list[str], dict[str, Any]]) -> None:
    """A task names its process after the workflow and before a scattered task's index; a task that exited
    non-zero has its status, and one whose outputs were missing none."""
    lines, failed = log
    assert list(backends.nextflow_failed_steps(lines).items()) == list(failed.items())


@pytest.mark.fast
def test_a_nextflow_run_reports_the_outputs_its_pipeline_recorded_by_their_sophios_names(tmp_path: Path) -> None:
    """Each recorded path becomes a CWL object, the summary holds the output object, and only a run that
    succeeded has one."""
    file = tmp_path / 'nextflow_wf' / 'work' / 'ab' / 'copy.txt'
    cells = [[{'class': 'File', 'path': f'/w/{a}{b}/cell.txt'} for b in 'xy'] for a in 'ab']
    stdout = ''.join(f'{line}\n' for line in [
        '[PROCESS ab/cdef12] wf:copy',
        'sophios-output ' + json.dumps(['copy_result', {'class': 'File', 'path': str(file)}]),
        'sophios-output ' + json.dumps(['grid', cells]),
        'sophios-output ' + json.dumps(['text', 'a "line"\n']),
        'sophios-output ' + json.dumps(['skipped', None]),
    ])
    backend = backends.backend('nextflow', 'nextflow')
    outcome = backend.outcome(_nextflow_request(tmp_path), 0, stdout, tmp_path / 'absent.log')
    assert outcome.outputs == {
        'copy~result': {'class': 'File', 'location': file.as_uri(), 'path': str(file), 'basename': 'copy.txt'},
        'grid': [[{'class': 'File', 'location': f'file:///w/{a}{b}/cell.txt', 'path': f'/w/{a}{b}/cell.txt',
                   'basename': 'cell.txt'} for b in 'xy'] for a in 'ab'],
        'text': 'a "line"\n',
        'skipped': None,
    }
    assert outcome.summary == tmp_path.resolve() / 'output_wf.json'
    assert json.loads(outcome.summary.read_text(encoding='utf-8')) == outcome.outputs
    assert outcome.failed == {}


@pytest.mark.fast
def test_a_failed_nextflow_run_names_each_failed_step_and_no_outputs(tmp_path: Path) -> None:
    log = tmp_path / 'run_wf.log'
    log.write_text('\n'.join(_NEXTFLOW_FAILED_LOG) + '\n', encoding='utf-8')
    stdout = 'sophios-output ' + json.dumps(['grid', []]) + '\n'
    outcome = backends.backend('nextflow', 'nextflow').outcome(_nextflow_request(tmp_path), 1, stdout, log)
    assert (outcome.outputs, outcome.summary) == (None, None)
    assert outcome.failed == {'bad-step': 7, 'child~copy': None}
    assert not (tmp_path / 'output_wf.json').exists()


#: A tool that writes into an output-target Directory: an entry it may write, captured as its output.
_WRITE_INTO = '''cwlVersion: v1.2
class: CommandLineTool
baseCommand: touch
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.outDir)
      writable: true
inputs:
  outDir:
    type: Directory
  source:
    type: File
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
arguments:
- $(inputs.outDir.basename)/result.txt
'''


def _write_into(tmp_path: Path) -> Any:
    """The compilation of a one-step workflow `wf` whose step `write-into` writes into `made`, beside `source.txt`."""
    (tmp_path / 'write_into.cwl').write_text(_WRITE_INTO, encoding='utf-8')
    step = Step(clt_path=tmp_path / 'write_into.cwl', step_name='write-into')
    step.inputs.outDir = Path('made')
    step.inputs.source = Path('source.txt')
    return compile_workflow_result(Workflow([step], 'wf'))


#: What a Nextflow run asks of this machine.
_NEXTFLOW_SETTINGS = preflight.RunSettings('docker', '', runner='nextflow')


@pytest.mark.fast
@pytest.mark.parametrize('check', [False, True])
def test_nextflow_prepare_writes_the_pipeline_from_absolute_paths_and_makes_each_output_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: bool) -> None:
    """A relative path is read from the root directory; an output-target Directory that names nothing yet is
    made; and each Nextflow name maps back to its Sophios one. A check writes none of it under the base path,
    and makes no directory, as a CWL check does."""
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'source.txt').write_text('', encoding='utf-8')
    settings = run_setup.Settings(_NEXTFLOW_SETTINGS, root, check=check)
    prepared = backends.backend('nextflow', 'nextflow').prepare(_write_into(root), (), tmp_path / 'base', settings)
    if check:
        assert not (tmp_path / 'base').exists() and not (root / 'made').exists()
        return
    base = tmp_path / 'base' / 'nextflow_wf'
    assert prepared.documents == tuple(base / name for name in (
        'nextflow_workflow.json', 'workflow.nf', 'nextflow.config', 'nextflow_params.json'))
    params = json.loads((base / 'nextflow_params.json').read_text(encoding='utf-8'))
    assert params == {'write_into_outDir': {'class': 'Directory', 'path': str(root / 'made')},
                      'write_into_source': {'class': 'File', 'path': str(root / 'source.txt')}}
    assert (root / 'made').is_dir()
    assert json.loads((base / 'nextflow_names.json').read_text(encoding='utf-8')) == {
        'processes': {'write_into': 'write-into'}, 'outputs': {'write_into_outDir': 'write-into~outDir'}}


@pytest.mark.fast
def test_the_nextflow_check_reports_a_missing_input_and_not_an_output_directory_it_makes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As the CWL pre-flight does, before Nextflow starts; an output-target Directory is made, not looked for."""
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    settings = run_setup.Settings(_NEXTFLOW_SETTINGS, tmp_path, check=True)
    with pytest.raises(SophiosError) as raised:
        backends.backend('nextflow', 'nextflow').prepare(_write_into(tmp_path), (), tmp_path / 'base', settings)
    (missing,) = raised.value.diagnostics
    assert missing.code is SophiosErrorCode.MISSING_INPUT_FILE
    assert f"input 'write-into~source' (from the workflow, whose relative paths are read from {tmp_path}) names " \
        f"'{tmp_path / 'source.txt'}', which does not exist" in missing.message


@pytest.mark.fast
def test_a_nextflow_run_reads_its_own_names_after_another_workflow_is_prepared_in_its_base_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each workflow's names are its own file, as its summary is."""
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    (tmp_path / 'source.txt').write_text('', encoding='utf-8')
    settings = run_setup.Settings(_NEXTFLOW_SETTINGS, tmp_path)
    backend = backends.backend('nextflow', 'nextflow')
    backend.prepare(_write_into(tmp_path), (), tmp_path / 'base', settings)
    echo = Step(clt_path=REPO_ROOT / 'cwl_adapters' / 'echo.cwl')
    echo.inputs.message = 'hello'
    backend.prepare(compile_workflow_result(Workflow([echo], 'other')), (), tmp_path / 'base', settings)
    stdout = 'sophios-output ' + json.dumps(['write_into_outDir', 'made']) + '\n'
    request = RunRequest('wf', tmp_path / 'base', runner='nextflow', target='nextflow')
    assert backend.outcome(request, 0, stdout, tmp_path / 'absent.log').outputs == {'write-into~outDir': 'made'}


@pytest.mark.fast
def test_the_nextflow_check_asks_for_the_engine_only_when_a_process_has_a_container(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each process's image is read from the pipeline Nextflow runs, not from `nextflow_workflow.json`."""
    asked: list[list[str]] = []
    engine_down = Diagnostic(Severity.ERROR, SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE, 'docker is down.')

    def engine_problems(images: list[str], _settings: preflight.RunSettings) -> list[Diagnostic]:
        asked.append(images)
        return [engine_down]
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    monkeypatch.setattr(preflight, 'engine_problems', engine_problems)
    backend = backends.backend('nextflow', 'nextflow')
    host = runtime_workflow()
    contained = replace(host, processes=[replace(process, container='docker.io/bash:4.4')
                        for process in host.processes])
    backend.check(write_nextflow_artifacts(host, tmp_path / 'host'), (), _NEXTFLOW_SETTINGS)
    documents = write_nextflow_artifacts(contained, tmp_path / 'contained')
    documents[0].unlink()
    with pytest.raises(SophiosError) as raised:
        backend.check(documents, (), _NEXTFLOW_SETTINGS)
    assert asked == [['docker.io/bash:4.4', 'docker.io/bash:4.4']]
    assert list(raised.value.diagnostics) == [engine_down]


@pytest.mark.fast
@pytest.mark.parametrize(('platform', 'script', 'run_script', 'expected'), [
    (None, None, False, 'error [wic029] the run calls nextflow, which is neither in this Python environment nor on '
     'PATH: install Nextflow (https://www.nextflow.io/docs/latest/install.html).'),
    (None, 'printf "\\033[31mERROR: Cannot find Java or it\'s a wrong version\\033(B\\033[m\\n"; exit 1', False,
     "error [wic029] nextflow -version exited with status 1 (ERROR: Cannot find Java or it's a wrong version): "
     'Nextflow needs a Java it can run; install one and point JAVA_HOME at it.'),
    (None, 'echo "nextflow version 26.04.2"', False, None),
    (None, 'echo "nextflow version 26.04.2"', True, 'error [wic029] run.sh calls nextflow, which is not on PATH: '
     'install Nextflow (https://www.nextflow.io/docs/latest/install.html).'),
    ('Windows', 'echo "nextflow version 26.04.2"', False, 'error [wic029] nextflow cannot run here (Nextflow needs a '
     'POSIX system, which Windows is not): run Sophios inside WSL (https://learn.microsoft.com/windows/wsl/install).'),
], ids=['missing', 'no-java', 'starts', 'run-script-not-on-path', 'windows'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_nextflow_must_start_for_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str | None,
                                       script: str | None, run_script: bool, expected: str | None) -> None:
    """A run looks where `runner_program` looks; `run.sh` calls the `nextflow` on PATH; and, as for a CWL run,
    Sophios runs a runner on POSIX only, so a Nextflow that starts is no help on Windows."""
    program = tmp_path / 'nextflow'
    program.write_text(f'#!/bin/sh\n{script}\n', encoding='utf-8')
    program.chmod(0o755)
    monkeypatch.setattr(preflight, 'runner_program', lambda _name: None if script is None else str(program))
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', platform)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    settings = replace(_NEXTFLOW_SETTINGS, run_script=run_script)
    assert [str(problem) for problem in preflight.nextflow_problems(settings)] == ([expected] if expected else [])


@pytest.mark.fast
@pytest.mark.parametrize('runner', ['cwltool', 'nextflow'])
def test_the_engine_remedies_name_only_the_options_the_runs_target_takes(
        monkeypatch: pytest.MonkeyPatch, runner: str) -> None:
    """`--container_engine` and `--ignore_docker_processes` are a CWL run's: a Nextflow run is not told to pass
    them."""
    def absent(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError('docker')
    settings = preflight.RunSettings('docker', '', runner=runner)
    monkeypatch.setattr(preflight.sub, 'run', absent)
    (missing,) = preflight.engine_problems(['docker.io/bash:4.4'], settings)
    monkeypatch.setattr(preflight.sub, 'run', lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, b'', b''))
    monkeypatch.setattr(preflight, '_docker_processes', lambda: preflight.MAX_DOCKER_PROCESSES + 1)
    (busy,) = preflight.engine_problems(['docker.io/bash:4.4'], settings)
    cwl = runner != 'nextflow'
    assert str(missing).endswith('install docker, or name an installed engine with --container_engine.' if cwl
                                 else 'install docker.')
    assert str(busy).endswith('or pass --ignore_docker_processes.' if cwl else '(sudo pkill com.docker && sudo '
                              'pkill Docker).')
