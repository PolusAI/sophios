"""`sophios.compute` against the slurm driver (vjaganat90/compute, vasu_revamp; Node 26+) under fake Slurm and Toil.
A local lane (`test_ci_coverage.LOCAL_ONLY`): `SOPHIOS_SLURM_DRIVER=<checkout>/packages/slurm-driver pytest <file>`;
with `SOPHIOS_NEXTFLOW_ENV=<a conda env holding nextflow>` it runs Nextflow runs too."""
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from sophios import compute
from sophios.compute import RunConfig
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.run_status import RunState, RunStatus
from sophios.python_cwl_adapter import import_python_file
from sophios.wic_types import TargetName

pytestmark = [pytest.mark.skip_pypi_ci, pytest.mark.skipif(
    'SOPHIOS_SLURM_DRIVER' not in os.environ,
    reason='set SOPHIOS_SLURM_DRIVER to the packages/slurm-driver directory of a vjaganat90/compute checkout, on '
    'its branch vasu_revamp'),
    pytest.mark.skipif(sys.platform == 'win32', reason='the compute client needs macOS, Linux or WSL')]


@dataclass(frozen=True)
class Cluster:
    """A driver running under fake Slurm: its URL, unix:// and its socket, and the directories a test reads and
    steers."""

    url: str
    driver: Path
    slurm: Path
    shared: Path
    runs: Path

    def directory(self, run: compute.Run) -> Path:
        """`run`'s own folder, under the account that submitted it: this test's."""
        return self.runs / getpass.getuser() / run.id

    def says(self, job: str | None, line: str) -> None:
        """Make Slurm say `line`, `<name>|<state>|<exit code>|<start>|<end>|<time limit>`, of `job`."""
        (self.slurm / f'{job}.sacct').write_text(line + '\n', encoding='utf-8')

    def exit_of(self, run: compute.Run) -> str:
        """The exit status of `run`'s job script, once it has ended: the fake `sbatch` writes it to `<job>.exit`."""
        ended = self.slurm / f'{run.slurm_job}.exit'
        for _ in range(1800):
            if ended.exists() and ended.read_text('utf-8').strip():
                return ended.read_text('utf-8').strip()
            time.sleep(0.1)
        raise AssertionError(f'the job script of {run.id} did not end')

    def leader(self, run: compute.Run, state: str) -> None:
        """Make Slurm say `run`'s leader is `state`; an end only once its job script has ended, since the driver cancels
        what a run left queued once it records the end, cutting the script before it wrote what the test reads."""
        status = '0' if state in ('PENDING', 'RUNNING') else self.exit_of(run)
        self.says(run.slurm_job, f'{run.id}|{state}|{status}:0|2026-10-26T10:00:00|2026-10-26T10:05:00|1-00:00:00')


def _get(cluster: Cluster, path: str) -> requests.Response:
    """`GET path` of the driver, over its socket, as Sophios makes every call."""
    return compute._Client(cluster.url).call('GET', path)  # pylint: disable=protected-access


@contextmanager
def _serving(root: Path, toil_env: Path) -> Iterator[Cluster]:
    """The driver of `SOPHIOS_SLURM_DRIVER` over the fakes copied into `root` and the Toil env `toil_env`, on a socket
    in a directory of its own (a Unix socket path must be short); its log is `root/driver.log`."""
    driver = Path(os.environ['SOPHIOS_SLURM_DRIVER']).resolve()
    node = shutil.which('node')
    assert node is not None, 'the slurm driver needs Node 26 or later on PATH'
    shutil.copytree(driver / 'test' / 'fake', root, dirs_exist_ok=True)
    for name in ('data', 'shared', 'images', 'home'):
        (root / name).mkdir()
    sockets = Path(tempfile.mkdtemp())
    env = {'HOME': str(root / 'home'), 'PATH': f'{root / "slurm"}:/usr/bin:/bin', 'DATA_DIR': str(root / 'data'),
           'SHARED_ROOTS': str(root / 'shared'), 'TOIL_ENV': str(toil_env), 'IMAGE_CACHE': str(root / 'images'),
           'LEADER_PARTITION': 'normal', 'LEADER_TIME': '1-00:00:00', 'LEADER_MEMORY': '8G', 'LEADER_CPUS': '2',
           'SOCKET_PATH': str(sockets / 'api.sock')}
    log = root / 'driver.log'
    with log.open('w', encoding='utf-8') as stderr, \
            subprocess.Popen([node, 'src/main.ts'], cwd=driver, env=env, stderr=stderr) as process:
        try:
            for _ in range(300):
                if 'listening on' in log.read_text('utf-8') or process.poll() is not None:
                    break
                time.sleep(0.1)
            assert process.poll() is None, log.read_text('utf-8')
            yield Cluster(f'unix://{sockets / "api.sock"}', driver, root / 'slurm', root / 'shared',
                          root / 'data' / 'runs')
        finally:
            process.terminate()
            process.wait()
            shutil.rmtree(sockets)


@pytest.fixture(scope='module', name='cluster')
def _cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver under fake Slurm and the driver's fake Toil, until the module ends."""
    root = tmp_path_factory.mktemp('cluster')
    with _serving(root, root / 'toil') as cluster:
        yield cluster


@pytest.fixture(scope='module', name='toil_cluster')
def _toil_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver under fake Slurm with real Toil from this Python's own env; each job script runs on this machine
    under Toil's single_machine batch system, as the driver's real-Toil lane runs it, with no containers."""
    toil_env = Path(sys.prefix)
    assert (toil_env / 'bin' / 'toil-cwl-runner').exists(), f'{toil_env} has no toil-cwl-runner, which Sophios needs'
    root = tmp_path_factory.mktemp('toil_cluster')
    with _serving(root, toil_env) as cluster:
        (cluster.slurm / 'sbatch.then').write_text(
            "sed -e 's|--batchSystem slurm|--batchSystem single_machine|' -e 's|--singularity|--no-container|' "
            '"$script" > "$script.local" && mv "$script.local" "$script"\n', encoding='utf-8')
        yield cluster


#: A stand-in for Apptainer's `singularity`, which this machine has not, each call a line of `calls` beside it: `pull`
#: makes the image file Nextflow names, and `exec` runs the command it is given on this machine, with no image, so
#: with no runscript.
_SINGULARITY = """#!/bin/bash
echo "$*" >> "$(dirname "$0")/calls"
case $1 in
pull) echo image > "$3" ;;
exec) while [ $# -gt 0 ] && [[ $1 != *.img ]]; do shift; done; shift; exec "$@" ;;
*) exit 2 ;;
esac
"""


@pytest.fixture(scope='module', name='nextflow_cluster')
def _nextflow_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver under fake Slurm with real Nextflow, from the conda env SOPHIOS_NEXTFLOW_ENV names, in its fake Toil
    env, with its Java, as TOIL_ENV holds them on the cluster: as the driver's own Nextflow lane runs it. The leader
    runs Nextflow on this machine, and so does each task job Nextflow submits; a container's task runs through the
    stand-in for `singularity`, on the PATH of the account's login environment."""
    nextflow = os.environ.get('SOPHIOS_NEXTFLOW_ENV')
    if nextflow is None:
        pytest.skip('set SOPHIOS_NEXTFLOW_ENV to a conda env whose bin/ holds nextflow, and lib/jvm its Java')
    root = tmp_path_factory.mktemp('nextflow_cluster')
    with _serving(root, root / 'toil') as cluster:
        (root / 'toil' / 'bin' / 'nextflow').unlink()
        (root / 'toil' / 'bin' / 'nextflow').symlink_to(Path(nextflow) / 'bin' / 'nextflow')
        (root / 'toil' / 'lib').symlink_to(Path(nextflow) / 'lib')
        (root / 'apptainer').mkdir()
        (root / 'apptainer' / 'singularity').write_text(_SINGULARITY, encoding='utf-8')
        (root / 'apptainer' / 'singularity').chmod(0o755)
        # Slurm's commands, which the Nextflow head runs, the stand-in, and macOS's md5, which Nextflow's launcher runs.
        (cluster.slurm / 'login.env').write_text(
            f'PATH={cluster.slurm}:{root / "apptainer"}:/usr/bin:/bin:/usr/sbin:/sbin\n', encoding='utf-8')
        # Nextflow asks Slurm about its task jobs every half second, not every few.
        (cluster.slurm / 'sbatch.then').write_text(
            'echo "executor { pollInterval = \'500ms\'; queueStatInterval = \'1s\' }" '
            '>> "$(dirname "$script")/service.config"\n', encoding='utf-8')
        yield cluster


def _reader() -> CommandLineTool:
    inputs = Inputs(file=Input(cwl.file, position=1))
    return CommandLineTool('reader', inputs, Outputs()).base_command('cat')


def _workflow(cluster: Cluster, location: str | None = None, name: str = 'svc') -> Workflow:
    """A step that reads `location`, by default a file on the shared filesystem, by its absolute path."""
    (cluster.shared / 'data').mkdir(exist_ok=True)
    (cluster.shared / 'data' / 'a.txt').write_text('a\n', encoding='utf-8')
    step = Step(_reader(), step_name='show')
    step.inputs.file = location or str(cluster.shared / 'data' / 'a.txt')
    return Workflow([step], name)


def test_the_client_runs_on_the_contract_and_the_schema_the_driver_serves(cluster: Cluster) -> None:
    """Sophios's copy of the schema is the driver's, and so is the major version of the contract it speaks."""
    served = _get(cluster, '/run-request.schema.json').json()
    assert served == compute.SCHEMA == json.loads((cluster.driver / 'run-request.schema.json').read_text('utf-8'))
    contract = _get(cluster, '/openapi.json').json()
    assert contract['info']['version'].split('.')[0] == str(compute.CONTRACT)


def test_a_run_is_submitted_followed_and_reattached_and_its_answers_are_the_contracts(
        cluster: Cluster, capsys: pytest.CaptureFixture[str]) -> None:
    """201 with its id printed; the status until COMPLETED with the output object; the same run from a new
    Service; every answer valid against the contract the driver serves."""
    service = compute.Service(cluster.url)
    run = service.submit(_workflow(cluster))
    assert run.id.startswith('svc-') and run.slurm_job
    assert f'Submitted run {run.id}' in capsys.readouterr().out
    cluster.leader(run, 'COMPLETED')
    status = run.wait()
    assert (status.state, status.reason, run.outputs()) == (RunState.COMPLETED, None, {'answer': 42})
    assert status.started_at is not None and status.started_at.tzinfo is not None
    assert f'{run.id}: COMPLETED' in capsys.readouterr().out
    assert compute.Service(cluster.url).attach(run.id).status().state is RunState.COMPLETED
    assert 'fake toil-cwl-runner' in run.logs()

    contract = _get(cluster, '/openapi.json').json()
    registry = Registry().with_resources([
        ('openapi.json', Resource.from_contents(contract, default_specification=DRAFT202012)),
        ('run-request.schema.json', Resource.from_contents(compute.SCHEMA))])

    def valid(answer: requests.Response, schema: str) -> None:
        validator = Draft202012Validator({'$ref': f'openapi.json#/components/schemas/{schema}'}, registry=registry)
        assert not list(validator.iter_errors(answer.json())), answer.text

    valid(_get(cluster, f'/runs/{run.id}'), 'Run')
    valid(_get(cluster, '/runs/nope'), 'Errors')


def test_a_request_the_driver_refuses_names_each_input_and_says_to_send_basepath(cluster: Cluster) -> None:
    """Every problem at once, each at its authored input, in the driver's words: a relative path sent without
    basepath is told to send it, and the outputs folder relative to it."""
    service = compute.Service(cluster.url)
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster, location='data/missing.txt'),
                       RunConfig('results').basepath(str(cluster.shared)))
    (problem,) = refused.value.messages
    assert problem.startswith('input show~file: data/missing.txt') and 'does not exist' in problem
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster, location='data/a.txt'))
    assert refused.value.messages == (
        'input show~file: data/a.txt is relative: send basepath, the cluster directory it is relative to, and '
        'workflowOutDir, the outputs folder relative to basepath',)
    with pytest.raises(compute.RequestRefused, match='there is no run nope'):
        service.attach('nope')


def test_a_path_written_for_a_shell_is_sent_as_written_and_the_driver_refuses_it(cluster: Cluster) -> None:
    """Sophios walks no paths: the driver, the one authority on them, refuses `~` at the authored input, naming the
    shared roots, the only paths it reads."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.Service(cluster.url).submit(_workflow(cluster, location='~/Desktop/raw'))
    assert refused.value.messages == (
        'input show~file: ~/Desktop/raw starts with "~", which a shell expands on the machine that wrote it; no '
        f'shell reads the request, and the driver reads only paths under its shared roots, {cluster.shared}: send '
        'the path as it is on the cluster',)


def test_a_run_that_fails_is_a_status_from_wait_naming_why_and_its_log_says_so(cluster: Cluster) -> None:
    """`wait` returns the FAILED run's status, raising nothing, with the service's reason; `leader.log` has why."""
    jobs = cluster.slurm / 'jobs'
    after = 1000 + (len(jobs.read_text('utf-8').splitlines()) if jobs.exists() else 0)
    failing = cluster.slurm.parent / 'toil' / 'bin' / 'toil-cwl-runner.fail'
    failing.write_text('', encoding='utf-8')
    try:
        run = compute.Service(cluster.url).submit(_workflow(cluster))
        cluster.leader(run, 'FAILED')
    finally:
        failing.unlink()
    status = run.wait()
    assert (status.state, status.reason, run.slurm_job) == (
        RunState.FAILED, f"the workflow failed; see the run's log, GET /runs/{run.id}/logs/leader.log", str(after))
    assert 'the workflow failed' in run.logs()


def test_a_failed_step_is_named_in_the_status_and_the_logs_stay_the_leaders(cluster: Cluster) -> None:
    """The step's name as the author wrote it, with its exit code in the reason; `logs` is `leader.log`, Toil's log
    of the whole run, not the step's."""
    run = compute.Service(cluster.url).submit(_workflow(cluster))
    directory = cluster.directory(run)
    processes = json.loads((directory / 'run.json').read_text('utf-8'))['processes']
    (process,) = (key for key, path in processes.items() if path == 'show')
    unit = f'workflow.json.show.{process}'
    (directory / 'logs' / 'batch' / 'toil_abc.1.4001.out.log').write_text('', encoding='utf-8')
    (directory / 'logs' / 'jobs' / f'failed_CWLJob_{unit}_000.log').write_text('reading\nboom\n', encoding='utf-8')
    cluster.says('4001', f'toil_job_1_{unit}|FAILED|1:0|2026-10-26T10:01:00|2026-10-26T10:02:00|1-00:00:00')
    cluster.leader(run, 'FAILED')
    status = run.wait()
    assert [(step.name, step.state, step.exit_status) for step in status.steps] == [('show', RunState.FAILED, None)]
    assert status.reason == 'step show failed (exit code 1:0); see its logs'
    leader = run.logs()
    assert 'fake toil-cwl-runner' in leader and 'boom' not in leader


def test_a_run_whose_leader_slurm_never_starts_fails_naming_why_and_has_no_log(cluster: Cluster) -> None:
    """Slurm makes `leader.log` as it starts the leader: a leader it will never start is the run's FAILED end, with
    Slurm's reason, and `logs` is '', as the examples print it when a run did not complete."""
    # Slurm keeps the job waiting for good, and its script writes no leader.log.
    (cluster.slurm / 'sbatch.then').write_text(
        'id=$((1000 + $(cat "$here/jobs" 2> /dev/null | wc -l)))\n'
        'echo PartitionTimeLimit > "$here/$id.squeue"\n'
        "sed -e 's|^#SBATCH --output=.*|#SBATCH --output=/dev/null|' \"$script\" > \"$script.x\" "
        '&& mv "$script.x" "$script"\n', encoding='utf-8')
    try:
        run = compute.Service(cluster.url).submit(_workflow(cluster))
        status = run.wait()
    finally:
        (cluster.slurm / 'sbatch.then').unlink()
    assert status.state is RunState.FAILED and str(status.reason).startswith(
        'Slurm will never start the leader (PartitionTimeLimit)')
    assert run.logs() == ''


def test_cancel_says_whether_the_run_is_cancelled_as_future_cancel_does(cluster: Cluster) -> None:
    """True once the run is CANCELLED by request, and again when sent again; a run that had ended COMPLETED stays
    so, and cancel is False."""
    run = compute.Service(cluster.url).submit(_workflow(cluster))
    cluster.leader(run, 'RUNNING')
    assert run.cancel() is True
    status = run.status()
    assert (status.state, status.reason) == (RunState.CANCELLED, 'cancelled by request')
    assert run.cancel() is True

    done = compute.Service(cluster.url).submit(_workflow(cluster))
    cluster.leader(done, 'COMPLETED')
    assert done.wait().state is RunState.COMPLETED
    assert done.cancel() is False and done.status().state is RunState.COMPLETED


def test_waiting_ends_at_its_timeout_and_the_run_goes_on(cluster: Cluster, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """A QUEUED run not ended within the timeout raises, and is still there."""
    monkeypatch.setattr(compute, 'POLL', 0.05)
    run = compute.Service(cluster.url).submit(_workflow(cluster))
    with pytest.raises(TimeoutError, match=f'run {run.id} has not ended within 0.2 s; it goes on'):
        run.wait(timeout=0.2)
    assert f'{run.id}: QUEUED' in capsys.readouterr().out
    assert run.status().state is RunState.QUEUED


def test_a_busy_service_is_asked_again_every_5_s_and_then_given_up_on(
        cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """503 while Slurm cannot be asked: the call is made again every 5 s, until PATIENCE runs out."""
    run = compute.Service(cluster.url).submit(_workflow(cluster))
    failing = cluster.slurm / 'sacct.fail'
    failing.write_text('sacct: error: Problem talking to the database\n', encoding='utf-8')
    try:
        monkeypatch.setattr(compute, 'PATIENCE', 1.0)
        with pytest.raises(compute.ServiceUnavailable, match='answered 503: sacct failed: sacct: error: Problem'):
            run.status()
        monkeypatch.setattr(compute, 'PATIENCE', 60.0)
        started = time.monotonic()
        subprocess.Popen(['sh', '-c', f'sleep 1 && rm {failing}'])  # pylint: disable=consider-using-with
        assert run.status().state is RunState.QUEUED
        assert time.monotonic() - started >= 4  # once again, after 5 s
    finally:
        failing.unlink(missing_ok=True)


#: A tool that writes into the folder its input `out` names and returns that folder: an output folder, to the service.
_WRITER = """cwlVersion: v1.2
class: CommandLineTool
requirements:
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.out)
        writable: true
inputs:
  out: Directory
outputs:
  out:
    type: Directory
    outputBinding:
      glob: $(inputs.out.basename)
baseCommand: [sh, -c, 'echo segmented > "$0/result.txt"']
arguments: [$(inputs.out.basename)]
"""


def _writes(directory: Path, out: str) -> Workflow:
    tool = directory / 'writer.cwl'
    tool.write_text(_WRITER, encoding='utf-8')
    step = Step(clt_path=tool, step_name='write')
    step.inputs.out = out
    return Workflow([step], 'writes')


def _ended(cluster: Cluster, run: compute.Run) -> RunStatus:
    """Once real Toil has run the job to its end, the run's status: Slurm says COMPLETED for exit 0, else FAILED."""
    cluster.leader(run, 'COMPLETED' if cluster.exit_of(run) == '0' else 'FAILED')
    return run.wait()


def test_workflow_out_dir_files_an_output_folder_given_as_a_name_under_that_name_and_takes_no_second_run(
        toil_cluster: Cluster, tmp_path: Path) -> None:
    """With real Toil: an output folder sent as just a name is made in the run, never read against basepath, and
    lands in <the RunConfig's folder, relative to basepath>/<name>; that folder is created when missing. Once it holds
    the outputs, the same RunConfig is refused for a new run, which would replace them."""
    results = toil_cluster.shared / 'results'
    config = RunConfig('results').basepath(str(toil_cluster.shared))
    service = compute.Service(toil_cluster.url)
    run = service.submit(_writes(tmp_path, 'segmentation'), config)
    status = _ended(toil_cluster, run)
    assert status.state is RunState.COMPLETED, run.logs()
    assert (results / 'segmentation' / 'result.txt').read_text('utf-8') == 'segmented\n'
    assert not (toil_cluster.shared / 'segmentation').exists()
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_writes(tmp_path, 'segmentation'), config)
    assert refused.value.messages == (
        f'workflowOutDir: results (against {toil_cluster.shared}: {results}) is not empty, and what a run writes '
        'there replaces same-named files and merges into same-named folders; name a new or an empty folder',)


def test_without_a_folder_the_outputs_land_in_the_runs_own_folder_under_the_submitters_account(
        toil_cluster: Cluster, tmp_path: Path) -> None:
    """With real Toil: `RunConfig()` names no folder, so the output folder given as a name lands in the run's own,
    `runs/<account>/<id>/out`, which `outputs()` names."""
    run = compute.Service(toil_cluster.url).submit(_writes(tmp_path, 'segmentation'), RunConfig())
    status = _ended(toil_cluster, run)
    assert status.state is RunState.COMPLETED, run.logs()
    folder = toil_cluster.directory(run) / 'out' / 'segmentation'
    assert (folder / 'result.txt').read_text('utf-8') == 'segmented\n'
    outputs = run.outputs()
    assert outputs is not None and outputs['write~out']['path'] == str(folder)


def test_an_absolute_output_folder_is_written_there_in_place_without_a_copy_or_a_warning(
        toil_cluster: Cluster, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With real Toil, and no RunConfig: the folder, created by the service, holds the result, and `outputs()` names
    it there, under its output's id; nothing is copied into the run's own folder, and nothing is warned of."""
    folder = toil_cluster.shared / 'in_place'
    run = compute.Service(toil_cluster.url).submit(_writes(tmp_path, str(folder)))
    assert 'WARNING' not in capsys.readouterr().err
    status = _ended(toil_cluster, run)
    assert status.state is RunState.COMPLETED, run.logs()
    assert (folder / 'result.txt').read_text('utf-8') == 'segmented\n'
    assert run.outputs() == {'write~out': {'class': 'Directory', 'location': folder.as_uri(), 'path': str(folder),
                                           'basename': 'in_place'}}
    assert not list((toil_cluster.directory(run) / 'out').rglob('result.txt'))


def test_one_run_config_sent_again_while_its_first_run_goes_on_is_refused_naming_that_run(cluster: Cluster) -> None:
    """Toil fills the folder only once a run has succeeded, so it is empty while the run goes on: the driver refuses
    a second run there all the same, at workflowOutDir, and takes it once the first has ended, here cancelled, which
    left the folder empty."""
    config = RunConfig(str(cluster.shared / 'busy'))
    service = compute.Service(cluster.url)
    first = service.submit(_workflow(cluster), config)
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster), config)
    assert refused.value.messages == (
        f'workflowOutDir: {cluster.shared / "busy"} is in use by run {first.id}, which is still going; name another '
        'folder',)
    cluster.leader(first, 'RUNNING')
    assert first.cancel() is True
    assert service.submit(_workflow(cluster), config).id != first.id


@pytest.mark.parametrize('script', ['ichnaea_compact', 'ichnaea_integrated'])
def test_the_ichnaea_examples_run_on_the_service(cluster: Cluster, script: str, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """Each example's `main()`, its folder on the cluster (BASEPATH, or ROOT) and SERVICE_URL pointed at this service,
    submits a request the service accepts and follows the run to its end, then prints its outputs (the fake Toil's);
    Slurm says COMPLETED once the job script has ended."""
    monkeypatch.setattr(compute, 'POLL', 0.2)
    for name in ('input_directory', 'model_directory'):
        (cluster.shared / name).mkdir(exist_ok=True)
    (cluster.shared / 'model_directory' / 'sam2.1_hiera_small.pt').write_text('weights\n', encoding='utf-8')
    source = tmp_path / f'{script}.py'
    shutil.copyfile(Path(__file__).resolve().parents[2] / 'examples' / 'scripts' / f'{script}.py', source)
    example = import_python_file(script, source)
    monkeypatch.setattr(example, 'BASEPATH' if hasattr(example, 'BASEPATH') else 'ROOT', str(cluster.shared))
    monkeypatch.setattr(example, 'SERVICE_URL', cluster.url)
    monkeypatch.chdir(tmp_path)
    # The job ends by writing Slurm's word on it: the id the fake sbatch is about to give, COMPLETED.
    (cluster.slurm / 'sbatch.then').write_text(
        'id=$((1000 + $(cat "$here/jobs" 2> /dev/null | wc -l)))\n'
        'echo "printf \'%s\\n\' \'x|COMPLETED|0:0|2026-10-26T10:00:00|2026-10-26T10:05:00|1-00:00:00\' '
        '> \'$here/$id.sacct\'" >> "$script"\n', encoding='utf-8')
    try:
        assert example.main() == 0
    finally:
        (cluster.slurm / 'sbatch.then').unlink()
    printed = capsys.readouterr()
    assert printed.out.endswith(" COMPLETED: {'answer': 42}\n")
    if script == 'ichnaea_integrated':
        assert (cluster.shared / 'output_directory3').is_dir()
        assert 'WARNING' not in printed.err


def _segments(root: str, scatter: bool, container: bool) -> Workflow:
    """The owner's segmentation shape, but for its command, which reads its folder at `root`, and `./models`, and
    copies them into its output folder, staged writable and given a name, which it returns: `cp -RL <input> models
    <output>`; in a container, asking for a GPU, when `container`; scattered over two output folders, or one."""
    inputs = Inputs(input=Input(cwl.directory, position=1), output=Input(cwl.directory, position=3),
                    model=Input(cwl.directory))
    tool = (CommandLineTool('segment', inputs, Outputs(output=Output(cwl.directory, glob='$(inputs.output.basename)')))
            .base_command('cp', '-RL').argument('models', position=2)
            .stage(inputs.output, writable=True).stage(inputs.model, entryname='models'))
    if container:
        tool = tool.gpu(device_count_min=1).docker('debian:bookworm-slim')
    step = Step(tool, step_name='autoseg')
    step.inputs.input, step.inputs.model = f'{root}/input_directory', f'{root}/model_directory'
    step.inputs.output = ['segmentation-a', 'segmentation-b'] if scatter else 'segmentation'
    if scatter:
        step.scatter_on(step.inputs.output)
    workflow = Workflow([step], 'autoseg')
    workflow.outputs.segmentation = step.outputs.output
    return workflow


def _made(value: Any) -> Any:
    """An output as both targets return it, wherever: a File or Directory, whose location is its path's, as its class,
    basename and what it holds, a file's text or each file's in a folder, by its path there; a list item by item."""
    match value:
        case {'class': kind, 'location': location, 'path': str() as path, 'basename': basename}:
            assert location == Path(path).as_uri() and Path(path).name == basename, value
            held = Path(path).read_text('utf-8') if kind == 'File' else {
                str(file.relative_to(path)): file.read_text('utf-8') for file in Path(path).rglob('*')
                if file.is_file()}
            return kind, basename, held
        case list():
            return [_made(item) for item in value]
    return value


def test_a_containerized_gpu_tool_returns_on_nextflow_the_output_folders_it_returns_on_cwl(
        toil_cluster: Cluster, nextflow_cluster: Cluster) -> None:
    """The owner's segmentation shape, with real Nextflow and real Toil, run with one folder given a name, its paths
    absolute, and scattered over two, its paths relative to `basepath`. On Nextflow it runs in its container, through
    the stand-in for `singularity`, which finds no runscript, and each task job asks Slurm for the GPU; on CWL with no
    container and no GPU, which Toil refuses on a machine with neither. `outputs()` is the same, attached to or not:
    the same names, the same shapes, the same basenames, holding the same files, each in the run's outputs folder.
    The fake sbatch may give two jobs submitted at once one id, so one Nextflow run goes at a time."""
    targets: tuple[tuple[Cluster, TargetName], ...] = ((toil_cluster, 'cwl'), (nextflow_cluster, 'nextflow'))
    for cluster, _ in targets:
        (cluster.shared / 'lab' / 'input_directory').mkdir(parents=True)
        (cluster.shared / 'lab' / 'input_directory' / 'a.txt').write_text('a\n', encoding='utf-8')
        (cluster.shared / 'lab' / 'model_directory').mkdir()
        (cluster.shared / 'lab' / 'model_directory' / 'w.pt').write_text('w\n', encoding='utf-8')
    held = {'input_directory/a.txt': 'a\n', 'models/w.pt': 'w\n'}
    for scatter, made in ((False, {'segmentation': ('Directory', 'segmentation', held)}),
                          (True, {'segmentation': [('Directory', 'segmentation-a', held),
                                                   ('Directory', 'segmentation-b', held)]})):
        runs = {target: compute.Service(cluster.url).submit(
            _segments('lab' if scatter else str(cluster.shared / 'lab'), scatter, container=target == 'nextflow'),
            RunConfig('segmented').basepath(str(cluster.shared)) if scatter else None, target=target)
            for cluster, target in targets}
        for cluster, target in targets:
            run = runs[target]
            assert _ended(cluster, run).state is RunState.COMPLETED, run.logs()
            outputs = run.outputs()
            assert outputs is not None and {name: _made(value) for name, value in outputs.items()} == made, target
            assert compute.Service(cluster.url).attach(run.id).outputs() == outputs
            assert {Path(entry['path']).parent for entry in (
                outputs['segmentation'] if scatter else [outputs['segmentation']])} == {
                cluster.shared / 'segmented' if scatter else cluster.directory(run) / 'out'}
        assert len([path for path in nextflow_cluster.slurm.glob('*.script') if
                    f'#SBATCH --comment=slurm-driver:{runs["nextflow"].id} --gres=gpu:1\n' in path.read_text('utf-8')
                    ]) == (2 if scatter else 1)
    calls = (nextflow_cluster.slurm.parent / 'apptainer' / 'calls').read_text('utf-8').splitlines()
    assert {call.split(' ')[0] for call in calls} == {'pull', 'exec'}
    assert all(call.endswith(' docker://debian:bookworm-slim') for call in calls if call.startswith('pull'))
    assert len([call for call in calls if call.startswith('exec') and ' --nv ' in call]) == 3


def test_outputs_of_one_file_name_and_a_folder_staged_writable_agree_on_nextflow_and_on_cwl(
        toil_cluster: Cluster, nextflow_cluster: Cluster) -> None:
    """With real Nextflow and real Toil: two outputs whose files share a name, declared out of the order of their
    names, and a third returning one of those files again, land as the same copies on both, under the same names: the
    outputs in the order of their names, each path copied once, under the first name no copy has taken. A folder
    returned with a file of that name in it, which an output whose name sorts before the folder's returns too, holds
    the file on both, which is not copied on its own. Each is read by its class and its path in the outputs folder:
    Toil's basename of a copy named `_2` is the name before the copy. A tool that stages a folder writable and returns
    something else reads the folder it is given, the user's files in it, on both: it is no output folder, which a
    tool returns."""
    say = (CommandLineTool('say', Inputs(word=Input(cwl.string, position=1)),
                           Outputs(out=Output(cwl.file, glob='out.txt'))).base_command('echo').stdout('out.txt'))
    first, second = Step(say, step_name='first'), Step(say, step_name='second')
    data = Inputs(data=Input(cwl.directory, position=1))
    count = Step(CommandLineTool('count', data, Outputs(n=Output(cwl.file, glob='n.txt')))
                 .base_command('sh', '-c', 'ls "$0" | wc -l | tr -d " " > n.txt').stage(data.data, writable=True),
                 step_name='count')
    keep = Step(CommandLineTool('keep', Inputs(), Outputs(kept=Output(cwl.directory, glob='kept'),
                                                          held=Output(cwl.file, glob='kept/out.txt')))
                .base_command('sh', '-c', 'mkdir kept && echo kept > kept/out.txt'), step_name='keep')
    first.inputs.word, second.inputs.word, count.inputs.data = 'first', 'second', 'plate1'
    workflow = Workflow([first, second, count, keep], 'copies')
    workflow.outputs.z, workflow.outputs.a = first.outputs.out, second.outputs.out
    workflow.outputs.m, workflow.outputs.n = first.outputs.out, count.outputs.n
    workflow.outputs.k, workflow.outputs.b = keep.outputs.kept, keep.outputs.held
    targets: tuple[tuple[Cluster, TargetName], ...] = ((toil_cluster, 'cwl'), (nextflow_cluster, 'nextflow'))
    for cluster, _ in targets:
        (cluster.shared / 'plate1').mkdir()
        (cluster.shared / 'plate1' / 'a.tif').write_text('a.tif', encoding='utf-8')
        (cluster.shared / 'plate1' / 'b.tif').write_text('b.tif', encoding='utf-8')
    runs = {target: compute.Service(cluster.url).submit(workflow, RunConfig('copies').basepath(str(cluster.shared)),
                                                        target=target) for cluster, target in targets}
    for cluster, target in targets:
        run, folder = runs[target], cluster.shared / 'copies'
        assert _ended(cluster, run).state is RunState.COMPLETED, run.logs()
        assert {name: (value['class'], str(Path(value['path']).relative_to(folder)))
                for name, value in (run.outputs() or {}).items()} == {
            'a': ('File', 'out.txt'), 'b': ('File', 'kept/out.txt'), 'k': ('Directory', 'kept'),
            'm': ('File', 'out.txt_2'), 'n': ('File', 'n.txt'), 'z': ('File', 'out.txt_2')}, target
        assert {str(path.relative_to(folder)): path.read_text('utf-8') for path in folder.rglob('*') if path.is_file()
                } == {'out.txt': 'second\n', 'out.txt_2': 'first\n', 'n.txt': '2\n', 'kept/out.txt': 'kept\n'}, target
