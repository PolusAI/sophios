"""`sophios.compute` against the compute service itself: the slurm driver, under its own fake Slurm and fake Toil.

The driver runs as it does on a cluster, `node src/main.ts`, from the checkout `SOPHIOS_SLURM_DRIVER` names: the
`packages/slurm-driver` directory of PolusAI/compute at the commit this client is written against, which the
lane checks out (`lint_and_test.yml`). Its fake `sbatch` runs each leader's job script, whose fake Toil prints an
output object; what Slurm says of a job is the line a test writes into `<job>.sacct` beside the fakes, as the
driver's own tests do. Node 26 or later must be on PATH. Marked `skip_pypi_ci`: only that lane has the driver.
"""
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from sophios import compute
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.run_status import RunState

from . import fake_ssh

pytestmark = pytest.mark.skip_pypi_ci


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

    def leader(self, run: compute.Run, state: str) -> None:
        """Make Slurm say `run`'s leader is `state`, once its job script has written the output object."""
        if state == 'COMPLETED':
            for _ in range(100):
                if (self.runs / run.id / 'outputs.json').exists():
                    break
                time.sleep(0.1)
        self.says(run.slurm_job, f'{run.id}|{state}|0:0|2026-10-26T10:00:00|2026-10-26T10:05:00|1-00:00:00')


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope='module', name='cluster')
def _cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """The driver of `SOPHIOS_SLURM_DRIVER`, listening on a free port of 127.0.0.1, until the module ends."""
    if 'SOPHIOS_SLURM_DRIVER' not in os.environ:
        pytest.fail('set SOPHIOS_SLURM_DRIVER to the packages/slurm-driver directory of a PolusAI/compute checkout '
                    'at the commit lint_and_test.yml pins')
    driver = Path(os.environ['SOPHIOS_SLURM_DRIVER']).resolve()
    node = shutil.which('node')
    assert node is not None, 'the slurm driver needs Node 26 or later on PATH'
    root = tmp_path_factory.mktemp('cluster')
    shutil.copytree(driver / 'test' / 'fake', root, dirs_exist_ok=True)
    for name in ('data', 'shared', 'images', 'home'):
        (root / name).mkdir()
    port = _free_port()
    env = {'HOME': str(root / 'home'), 'PATH': f'{root / "slurm"}:/usr/bin:/bin', 'DATA_DIR': str(root / 'data'),
           'SHARED_ROOTS': str(root / 'shared'), 'TOIL_ENV': str(root / 'toil'), 'IMAGE_CACHE': str(root / 'images'),
           'LEADER_PARTITION': 'normal', 'LEADER_TIME': '1-00:00:00', 'LEADER_MEMORY': '8G', 'LEADER_CPUS': '2',
           'PORT': str(port)}
    with subprocess.Popen([node, 'src/main.ts'], cwd=driver, env=env, stderr=subprocess.PIPE, text=True) as process:
        assert process.stderr is not None
        started = process.stderr.readline()
        assert 'listening on http://127.0.0.1' in started, started + process.stderr.read()
        try:
            yield Cluster(f'http://127.0.0.1:{port}', driver, root / 'slurm', root / 'shared', root / 'data' / 'runs')
        finally:
            process.terminate()


def _reader() -> CommandLineTool:
    inputs = Inputs(file=Input(cwl.file, position=1))
    return CommandLineTool('reader', inputs, Outputs()).base_command('cat')


def _workflow(cluster: Cluster, location: str = 'data/a.txt', name: str = 'svc') -> Workflow:
    (cluster.shared / 'data').mkdir(exist_ok=True)
    (cluster.shared / 'data' / 'a.txt').write_text('a\n', encoding='utf-8')
    step = Step(_reader(), step_name='show')
    step.inputs.file = location
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
    run = service.submit(_workflow(cluster), basepath=str(cluster.shared))
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


def test_a_request_the_driver_refuses_names_each_input_and_says_to_pass_basepath(cluster: Cluster) -> None:
    """Every problem at once, each at its authored input; a relative path sent without basepath gets the fix."""
    service = compute.Service(cluster.url)
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster, location='data/missing.txt'), basepath=str(cluster.shared))
    (problem,) = refused.value.diagnostics
    assert problem.message.startswith('input show~file: data/missing.txt') and 'does not exist' in problem.message
    with pytest.raises(compute.RequestRefused) as refused:
        service.submit(_workflow(cluster))
    assert [d.message for d in refused.value.diagnostics][-1].startswith(
        'these paths are relative and the request has no basepath: pass basepath=')
    with pytest.raises(compute.RequestRefused, match='there is no run nope'):
        service.attach('nope')


def test_a_run_that_fails_raises_naming_why_and_the_end_of_its_log(cluster: Cluster) -> None:
    """`Service.run` raises `RunFailed` with the run's reason and `leader.log`'s last lines."""
    jobs = cluster.slurm / 'jobs'
    after = 1000 + (len(jobs.read_text('utf-8').splitlines()) if jobs.exists() else 0)
    cluster.says(str(after), 'svc|FAILED|1:0|2026-10-26T10:00:00|2026-10-26T10:05:00|1-00:00:00')
    (cluster.slurm.parent / 'toil' / 'bin' / 'toil-cwl-runner.fail').write_text('', encoding='utf-8')
    try:
        with pytest.raises(compute.RunFailed) as failed:
            compute.Service(cluster.url).run(_workflow(cluster), basepath=str(cluster.shared))
    finally:
        (cluster.slurm.parent / 'toil' / 'bin' / 'toil-cwl-runner.fail').unlink()
    assert failed.value.status.state is RunState.FAILED and failed.value.run.slurm_job == str(after)
    first, tail = (d.message for d in failed.value.diagnostics)
    assert first == f'run {failed.value.run.id} ended FAILED: the workflow failed; see leader.log'
    assert tail.startswith('the end of leader.log:') and 'the workflow failed' in tail


def test_a_failed_step_is_named_with_the_end_of_its_own_log(cluster: Cluster) -> None:
    """The step's name as the author wrote it, and the last lines of the first of its logs, errors first."""
    run = compute.Service(cluster.url).submit(_workflow(cluster), basepath=str(cluster.shared))
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
    assert run.failure() == [f'run {run.id} ended FAILED: step show failed (exit code 1:0); see its logs',
                             f'the end of failed_CWLJob_{unit}_000.log, of step show:\nreading\nboom']
    assert run.logs('show') == f'==> failed_CWLJob_{unit}_000.log <==\nreading\nboom\n'
    with pytest.raises(ValueError, match="no step 'nope' that has started; those that have are show"):
        run.logs('nope')


def test_cancel_ends_a_run_cancelled_by_request(cluster: Cluster) -> None:
    """Safe to send again; the answer is the run, cancelled."""
    run = compute.Service(cluster.url).submit(_workflow(cluster), basepath=str(cluster.shared))
    cluster.leader(run, 'RUNNING')
    assert run.cancel().state in (RunState.RUNNING, RunState.CANCELLED)
    status = run.cancel()
    assert (status.state, status.reason) == (RunState.CANCELLED, 'cancelled by request')


def test_waiting_ends_at_its_timeout_and_the_run_goes_on(cluster: Cluster, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """A QUEUED run not ended within the timeout raises, and is still there."""
    monkeypatch.setattr(compute, 'POLL', 0.05)
    run = compute.Service(cluster.url).submit(_workflow(cluster), basepath=str(cluster.shared))
    with pytest.raises(TimeoutError, match=f'run {run.id} has not ended within 0.2 s; it goes on'):
        run.wait(timeout=0.2)
    assert f'{run.id}: QUEUED' in capsys.readouterr().out
    assert run.status().state is RunState.QUEUED


def test_a_busy_service_is_asked_again_after_retry_after_and_then_given_up_on(
        cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """503 while Slurm cannot be asked: the call is made again after Retry-After, until PATIENCE runs out."""
    run = compute.Service(cluster.url).submit(_workflow(cluster), basepath=str(cluster.shared))
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
        assert time.monotonic() - started >= 4  # one Retry-After of 5 s
    finally:
        failing.unlink(missing_ok=True)


def test_over_ssh_reaches_the_service_through_a_tunnel_the_block_closes(
        cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service answers through the tunnel ssh opens, and the tunnel is gone once the block ends."""
    calls = fake_ssh.install(tmp_path)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    with compute.Service.over_ssh('cluster', socket=cluster.url.removeprefix('http://')) as service:
        run = service.submit(_workflow(cluster), basepath=str(cluster.shared))
        assert run.status().state is RunState.QUEUED
        local = repr(service).split("'")[1]
    (argv,) = (json.loads(line) for line in calls.read_text('utf-8').splitlines())
    tunnel = f'{local.removeprefix("http://")}:{cluster.url.removeprefix("http://")}'
    assert argv[-2:] == [tunnel, 'cluster']
    with pytest.raises(requests.ConnectionError):
        requests.get(f'{local}/openapi.json', timeout=5)
