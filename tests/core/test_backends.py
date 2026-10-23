"""What each backend gives a run: the runner's command line, and what it reads back from the runner."""
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sophios import backends, preflight, run_setup
from sophios.api.python._workflow_runtime import compile_workflow_result
from sophios.api.python.workflow import Step, Workflow
from sophios.backends import RunRequest
from sophios.input_output_nf import write_nextflow_artifacts
from sophios.lang.diagnostics import Diagnostic, Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode

from .nextflow.testkit import REPO_ROOT, runtime_workflow


def _argv(request: RunRequest) -> list[str]:
    return list(backends.backend(request.target, request.runner).launch(request).argv)


@pytest.mark.fast
@pytest.mark.parametrize('runner', ['cwltool', 'toil-cwl-runner'])
def test_a_given_outdir_is_used_once_and_clean_follows_the_runner(tmp_path: Path, runner: str) -> None:
    outdir = tmp_path / 'workflow_output'
    cmd = _argv(RunRequest('wf', tmp_path / 'exec', runner=runner, outdir=str(outdir)))
    assert cmd.count('--outdir') == 1
    assert cmd[cmd.index('--outdir') + 1] == str(outdir.resolve())
    if runner == 'toil-cwl-runner':
        assert cmd[cmd.index('--clean') + 1] == 'always'
    else:
        assert '--clean' not in cmd


@pytest.mark.fast
@pytest.mark.parametrize(('runner', 'name'), [('cwltool', 'cwltool'), ('toil-cwl-runner', 'toil')])
def test_two_workflows_started_in_the_same_second_get_their_own_outdirs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str, name: str) -> None:
    """The default output directory is named after the workflow, as provenance, the summary and the
    job store are, so two workflows started in the same second from one basepath never share it."""
    class Frozen(backends.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> 'Frozen':
            return cls(2026, 10, 9, 12, 2, 16)

    monkeypatch.setattr(backends, 'datetime', Frozen)
    base = tmp_path.resolve()
    outdirs = []
    for workflow in ('bbbc', 'python_script_demo'):
        cmd = _argv(RunRequest(workflow, base, runner=runner))
        outdirs.append(cmd[cmd.index('--outdir') + 1])
    assert outdirs == [f'{base}/outdir_{name}_bbbc_2026_10_09_12.02.16',
                       f'{base}/outdir_{name}_python_script_demo_2026_10_09_12.02.16']


@pytest.mark.fast
def test_cwltool_is_quiet_unless_told_otherwise_and_toil_never_is(tmp_path: Path) -> None:
    """`--quiet` is on for callers that say nothing and off when `quiet=False`, so `--debug` can be heard."""
    assert '--quiet' in _argv(RunRequest('wf', tmp_path))
    assert '--quiet' not in _argv(RunRequest('wf', tmp_path, quiet=False))
    assert '--quiet' not in _argv(RunRequest('wf', tmp_path, runner='toil-cwl-runner'))


@pytest.mark.fast
@pytest.mark.parametrize(('cachedir', 'given'), [('', None), ('cache', 'cache')])
def test_cwltool_gets_the_cachedir_it_was_given_made_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                              cachedir: str, given: str | None) -> None:
    monkeypatch.chdir(tmp_path)
    cmd = _argv(RunRequest('wf', tmp_path, cachedir=cachedir))
    if given is None:
        assert '--cachedir' not in cmd
    else:
        assert cmd[cmd.index('--cachedir') + 1] == str(tmp_path / given)


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


@pytest.mark.fast
def test_failed_steps_are_read_from_the_runners_log_lines() -> None:
    lines = ['WARNING [job a] exited with status: 3', 'WARNING [job a] completed permanentFail',
             'INFO [job b] completed success',
             '\x1b[1;30mWARNING\x1b[0m \x1b[33m[job c] was terminated by signal: SIGKILL\x1b[0m',
             '\x1b[1;30mWARNING\x1b[0m \x1b[33m[job c] completed permanentFail\x1b[0m',
             '[2026-10-22 10:00:00] WARNING [job d] completed permanentFail']
    assert backends.failed_steps(lines) == {'a': 3, 'c': 'SIGKILL', 'd': None}


@pytest.mark.fast
def test_toil_writes_no_summary_when_it_printed_no_output_object(tmp_path: Path) -> None:
    request = RunRequest('wf', tmp_path, runner='toil-cwl-runner')
    backend = backends.backend('cwl', 'toil-cwl-runner')
    outcome = backend.outcome(request, 0, 'no object here\n', tmp_path / 'absent.log')
    assert outcome.outputs is None and outcome.summary is None
    assert not (tmp_path / 'output_wf.json').exists()


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


@pytest.mark.fast
def test_failed_nextflow_processes_are_read_from_its_log_lines() -> None:
    """A task names its process after the workflow and before a scattered task's index; a task that exited
    non-zero has its status, and one whose outputs were missing none."""
    assert backends.nextflow_failed_steps(_NEXTFLOW_FAILED_LOG) == {'bad_step': 7, 'copy': None}


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


@pytest.mark.fast
def test_real_time_analysis_does_not_run_beside_nextflow(tmp_path: Path) -> None:
    request = RunRequest('wf', tmp_path, runner='nextflow', target='nextflow')
    assert backends.backend('nextflow', 'nextflow').realtime_command(request) == (
        'Real-time analysis needs cwltool; nextflow runs the workflow without it.')


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
    made, except by a check; and each Nextflow name maps back to its Sophios one."""
    monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'source.txt').write_text('', encoding='utf-8')
    settings = run_setup.Settings(_NEXTFLOW_SETTINGS, root, check=check)
    prepared = backends.backend('nextflow', 'nextflow').prepare(_write_into(root), (), tmp_path / 'base', settings)
    base = tmp_path / 'base' / 'nextflow_wf'
    assert prepared.documents == tuple(base / name for name in (
        'nextflow_workflow.json', 'workflow.nf', 'nextflow.config', 'nextflow_params.json'))
    params = json.loads((base / 'nextflow_params.json').read_text(encoding='utf-8'))
    assert params == {'write_into_outDir': {'class': 'Directory', 'path': str(root / 'made')},
                      'write_into_source': {'class': 'File', 'path': str(root / 'source.txt')}}
    assert (root / 'made').is_dir() is not check
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


#: The two ways Nextflow may be missing: from where a run looks for it, and from PATH, where run.sh does.
_NO_NEXTFLOW = ('error [wic029] the run calls nextflow, which is neither in this Python environment nor on PATH: '
                'install Nextflow (https://www.nextflow.io/docs/latest/install.html).')
_NO_NEXTFLOW_ON_PATH = ('error [wic029] run.sh calls nextflow, which is not on PATH: '
                        'install Nextflow (https://www.nextflow.io/docs/latest/install.html).')


@pytest.mark.fast
@pytest.mark.parametrize(('script', 'run_script', 'expected'), [
    (None, False, _NO_NEXTFLOW),
    ('printf "\\033[31mERROR: Cannot find Java or it\'s a wrong version\\033(B\\033[m\\n"; exit 1', False,
     "error [wic029] nextflow -version exited with status 1 (ERROR: Cannot find Java or it's a wrong version): "
     'Nextflow needs a Java it can run; install one and point JAVA_HOME at it.'),
    ('echo "nextflow version 26.04.2"', False, None),
    ('echo "nextflow version 26.04.2"', True, _NO_NEXTFLOW_ON_PATH),
], ids=['missing', 'no-java', 'starts', 'run-script-not-on-path'])
def test_nextflow_must_start_for_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       script: str | None, run_script: bool, expected: str | None) -> None:
    """A run looks where `runner_program` looks; `run.sh` calls the `nextflow` on PATH."""
    program = tmp_path / 'nextflow'
    program.write_text(f'#!/bin/sh\n{script}\n', encoding='utf-8')
    program.chmod(0o755)
    monkeypatch.setattr(preflight, 'runner_program', lambda _name: None if script is None else str(program))
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', None)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    settings = replace(_NEXTFLOW_SETTINGS, run_script=run_script)
    assert [str(problem) for problem in preflight.nextflow_problems(settings)] == ([expected] if expected else [])


@pytest.mark.fast
def test_a_nextflow_run_on_windows_is_sent_to_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    """As a CWL run is: Sophios runs a runner on POSIX only."""
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', 'Windows')
    monkeypatch.setattr(preflight, 'runner_program', lambda _name: sys.executable)
    assert [str(problem) for problem in preflight.nextflow_problems(_NEXTFLOW_SETTINGS)] == [
        'error [wic029] nextflow cannot run here (Nextflow needs a POSIX system, which Windows is not): run Sophios '
        'inside WSL (https://learn.microsoft.com/windows/wsl/install).']


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
