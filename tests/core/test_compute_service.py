"""`sophios.compute` against the compute service itself: the slurm driver, under its own fake Slurm and fake Toil.

The driver runs as it does on a cluster, `node src/main.ts`, from the checkout `SOPHIOS_SLURM_DRIVER` names: the
`packages/slurm-driver` directory of vjaganat90/compute, branch vasu_revamp, at the commit this client is written
against, 02e2a23. Its fake `sbatch` runs each leader's job script, whose fake Toil prints an output object; what
Slurm says of a job is the line a test writes into `<job>.sacct` beside the fakes, as the driver's own tests do.
Node 26 or later must be on PATH.

The driver's repository is private, so no CI lane runs this file: it is a local lane, run by hand,

    SOPHIOS_SLURM_DRIVER=<checkout>/packages/slurm-driver pytest tests/core/test_compute_service.py -m skip_pypi_ci

and `test_ci_coverage.LOCAL_ONLY` names it. Marked `skip_pypi_ci`, which keeps it out of the packaging lane.
Without SOPHIOS_SLURM_DRIVER the module is skipped, as the repository's other environment-gated tests are, so a
plain `pytest tests/core` passes where there is no driver; the lane's command always sets it, so the lane never
passes by skipping.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from sophios import compute
from sophios.compute import RunConfig
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.run_status import RunState, RunStatus
from sophios.python_cwl_adapter import import_python_file

pytestmark = [pytest.mark.skip_pypi_ci, pytest.mark.skipif(
    'SOPHIOS_SLURM_DRIVER' not in os.environ,
    reason='set SOPHIOS_SLURM_DRIVER to the packages/slurm-driver directory of a vjaganat90/compute checkout at '
    '02e2a23, on its branch vasu_revamp, the commit this client is written against')]


@dataclass(frozen=True)
class Cluster:
    """A driver running under fake Slurm: its URL, and the directories a test reads and steers."""

    url: str
    driver: Path
    slurm: Path
    shared: Path
    runs: Path

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
        """Make Slurm say `run`'s leader is `state`: an end only once its job script has ended, with the status it
        ended with, as Slurm does. The driver cancels what a run left in Slurm's queue once it records the end, so an
        end said earlier would cancel the job script before it has written what the test reads."""
        status = '0' if state in ('PENDING', 'RUNNING') else self.exit_of(run)
        self.says(run.slurm_job, f'{run.id}|{state}|{status}:0|2026-10-26T10:00:00|2026-10-26T10:05:00|1-00:00:00')


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serving(root: Path, toil_env: Path) -> Iterator[Cluster]:
    """The driver of `SOPHIOS_SLURM_DRIVER`, over the fakes copied into `root` and the Toil env `toil_env`,
    listening on a free port of 127.0.0.1; its log is `root/driver.log`."""
    driver = Path(os.environ['SOPHIOS_SLURM_DRIVER']).resolve()
    node = shutil.which('node')
    assert node is not None, 'the slurm driver needs Node 26 or later on PATH'
    shutil.copytree(driver / 'test' / 'fake', root, dirs_exist_ok=True)
    for name in ('data', 'shared', 'images', 'home'):
        (root / name).mkdir()
    port = _free_port()
    env = {'HOME': str(root / 'home'), 'PATH': f'{root / "slurm"}:/usr/bin:/bin', 'DATA_DIR': str(root / 'data'),
           'SHARED_ROOTS': str(root / 'shared'), 'TOIL_ENV': str(toil_env), 'IMAGE_CACHE': str(root / 'images'),
           'LEADER_PARTITION': 'normal', 'LEADER_TIME': '1-00:00:00', 'LEADER_MEMORY': '8G', 'LEADER_CPUS': '2',
           'PORT': str(port)}
    log = root / 'driver.log'
    with log.open('w', encoding='utf-8') as stderr, \
            subprocess.Popen([node, 'src/main.ts'], cwd=driver, env=env, stderr=stderr) as process:
        try:
            for _ in range(300):
                if 'listening on http://127.0.0.1' in log.read_text('utf-8') or process.poll() is not None:
                    break
                time.sleep(0.1)
            assert process.poll() is None, log.read_text('utf-8')
            yield Cluster(f'http://127.0.0.1:{port}', driver, root / 'slurm', root / 'shared', root / 'data' / 'runs')
        finally:
            process.terminate()


@pytest.fixture(scope='module', name='cluster')
def _cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver under fake Slurm and the driver's fake Toil, until the module ends."""
    root = tmp_path_factory.mktemp('cluster')
    with _serving(root, root / 'toil') as cluster:
        yield cluster


@pytest.fixture(scope='module', name='toil_cluster')
def _toil_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver under fake Slurm with real Toil: this Python's own env, which has Toil and cwltool as Sophios
    does. Each job script is run on this machine, as the driver's own real-Toil lane runs it: Toil's single_machine
    batch system instead of Slurm's, and no containers."""
    toil_env = Path(sys.prefix)
    assert (toil_env / 'bin' / 'toil-cwl-runner').exists(), f'{toil_env} has no toil-cwl-runner, which Sophios needs'
    root = tmp_path_factory.mktemp('toil_cluster')
    with _serving(root, toil_env) as cluster:
        (cluster.slurm / 'sbatch.then').write_text(
            "sed -e 's|--batchSystem slurm|--batchSystem single_machine|' -e 's|--singularity|--no-container|' "
            '"$script" > "$script.local" && mv "$script.local" "$script"\n', encoding='utf-8')
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
    served = requests.get(f'{cluster.url}/run-request.schema.json', timeout=10).json()
    assert served == compute.SCHEMA == json.loads((cluster.driver / 'run-request.schema.json').read_text('utf-8'))
    contract = requests.get(f'{cluster.url}/openapi.json', timeout=10).json()
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

    contract = requests.get(f'{cluster.url}/openapi.json', timeout=10).json()
    registry = Registry().with_resources([
        ('openapi.json', Resource.from_contents(contract, default_specification=DRAFT202012)),
        ('run-request.schema.json', Resource.from_contents(compute.SCHEMA))])

    def valid(answer: requests.Response, schema: str) -> None:
        validator = Draft202012Validator({'$ref': f'openapi.json#/components/schemas/{schema}'}, registry=registry)
        assert not list(validator.iter_errors(answer.json())), answer.text

    valid(requests.get(f'{cluster.url}/runs/{run.id}', timeout=10), 'Run')
    valid(requests.get(f'{cluster.url}/runs/nope', timeout=10), 'Errors')


def test_a_request_the_driver_refuses_names_each_input_and_says_to_send_basepath(cluster: Cluster) -> None:
    """Every problem at once, each at its authored input, in the driver's words: a relative path sent without
    basepath is told to send one."""
    service = compute.Service(cluster.url)
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster, location='data/missing.txt'),
                       RunConfig('results').basepath(str(cluster.shared)))
    (problem,) = refused.value.messages
    assert problem.startswith('input show~file: data/missing.txt') and 'does not exist' in problem
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster, location='data/a.txt'))
    assert refused.value.messages == (
        'input show~file: data/a.txt is relative: send basepath, the cluster directory it is relative to',)
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
        RunState.FAILED, 'the workflow failed; see leader.log', str(after))
    assert 'the workflow failed' in run.logs()


def test_a_failed_step_is_named_in_the_status_and_the_logs_stay_the_leaders(cluster: Cluster) -> None:
    """The step's name as the author wrote it, with its exit code in the reason; `logs` is `leader.log`, Toil's log
    of the whole run, not the step's."""
    run = compute.Service(cluster.url).submit(_workflow(cluster))
    directory = cluster.runs / run.id
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


def test_workflow_out_dir_takes_an_output_folder_given_as_a_name_and_files_its_result_under_that_name(
        toil_cluster: Cluster, tmp_path: Path) -> None:
    """With real Toil: an output folder sent as just a name is made in the run, never read against basepath, and
    lands in <the RunConfig's folder>/<name>; that folder is created when missing."""
    results = toil_cluster.shared / 'results'
    config = RunConfig(str(results)).basepath(str(toil_cluster.shared))
    run = compute.Service(toil_cluster.url).submit(_writes(tmp_path, 'segmentation'), config)
    status = _ended(toil_cluster, run)
    assert status.state is RunState.COMPLETED, run.logs()
    assert (results / 'segmentation' / 'result.txt').read_text('utf-8') == 'segmented\n'
    assert not (toil_cluster.shared / 'segmentation').exists()


def test_workflow_in_place_writes_an_absolute_output_folder_there_without_a_copy_and_warns_it_is_advanced(
        toil_cluster: Cluster, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With real Toil: the folder, created by the service, holds the result, and `outputs()` names it there, under its
    output's id; nothing is copied into the run's own folder; and the service's ADVANCED warning is printed when the
    run is submitted."""
    folder = toil_cluster.shared / 'in_place'
    run = compute.Service(toil_cluster.url).submit(_writes(tmp_path, str(folder)),
                                                   RunConfig.in_place())
    assert ('WARNING from the compute service: outputMode workflowInPlace is an ADVANCED feature'
            in capsys.readouterr().err)
    status = _ended(toil_cluster, run)
    assert status.state is RunState.COMPLETED, run.logs()
    assert (folder / 'result.txt').read_text('utf-8') == 'segmented\n'
    assert run.outputs() == {'write~out': {'class': 'Directory', 'location': folder.as_uri(), 'path': str(folder),
                                           'basename': 'in_place'}}
    assert not list((toil_cluster.runs / run.id / 'out').rglob('result.txt'))


@pytest.mark.parametrize('script', ['ichnaea_compact', 'ichnaea_integrated'])
def test_the_ichnaea_examples_run_on_the_service(cluster: Cluster, script: str, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """Each example's `main()`, its BASEPATH and SERVICE_URL pointed at this service, submits a request the service
    accepts and follows the run to its end, then prints its outputs (the fake Toil's); Slurm says COMPLETED once the
    job script has ended."""
    monkeypatch.setattr(compute, 'POLL', 0.2)
    for name in ('input_directory', 'model_directory'):
        (cluster.shared / name).mkdir(exist_ok=True)
    (cluster.shared / 'model_directory' / 'sam3.pt').write_text('weights\n', encoding='utf-8')
    source = tmp_path / f'{script}.py'
    shutil.copyfile(Path(__file__).resolve().parents[2] / 'examples' / 'scripts' / f'{script}.py', source)
    example = import_python_file(script, source)
    monkeypatch.setattr(example, 'BASEPATH', str(cluster.shared))
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
    if script == 'ichnaea_compact':
        assert (cluster.shared / 'results_compact').is_dir()
    else:
        assert (cluster.shared / 'output_directory3').is_dir()
        assert 'outputMode workflowInPlace is an ADVANCED feature' in printed.err
