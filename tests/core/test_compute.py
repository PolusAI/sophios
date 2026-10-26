"""The compute service's request, as Sophios builds it: `sophios.compute.build_request`.

Fast and without a service: what the request holds, and what Sophios refuses before sending. What the service
answers is `test_compute_service.py`'s, against the service itself.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from jsonschema import Draft202012Validator

from sophios import compute
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.lang.error_codes import SophiosErrorCode
from sophios.run_status import RunState

from . import fake_ssh

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def _writer() -> CommandLineTool:
    """A tool that writes into the folder its input `out` names, as the tool builder writes one (a list of
    requirements), reading `model`."""
    inputs = Inputs(out=Input(cwl.directory, position=1), model=Input(cwl.file, position=2))
    outputs = Outputs(out=Output(cwl.directory, glob='$(inputs.out.basename)'))
    return CommandLineTool('writer', inputs, outputs).stage(inputs.out, writable=True).base_command('touch')


def _workflow(out: str = 'results/run7', model: str = 'models/sam3.pt', name: str = 'segment') -> Workflow:
    step = Step(_writer(), step_name='write')
    step.inputs.out = out
    step.inputs.model = model
    return Workflow([step], name)


@pytest.mark.fast
def test_the_request_carries_the_compiled_workflow_and_every_path_as_written() -> None:
    """One inline document, its inputs, and `basepath` only when given; nothing is joined or checked."""
    request = compute.build_request(_workflow(model='https://example.org/sam3.pt'), basepath='/projects/lab')
    assert set(request) == {'id', 'workflow', 'inputs', 'basepath'}
    assert request['basepath'] == '/projects/lab'
    assert request['workflow']['class'] == 'Workflow' and request['workflow']['cwlVersion'] == 'v1.2'
    assert all(isinstance(step['run'], dict) for step in request['workflow']['steps'])
    assert request['inputs'] == {'write~out': {'class': 'Directory', 'location': 'results/run7'},
                                 'write~model': {'class': 'File', 'location': 'https://example.org/sam3.pt'}}
    assert 'basepath' not in compute.build_request(_workflow(out='/projects/lab/out'))


@pytest.mark.fast
def test_an_output_folder_is_sent_as_a_folder_not_as_the_local_runs_bare_name(tmp_path: Path) -> None:
    """`compile()` gives a local runner an output folder as a bare name, and refuses an absolute one; the service
    is sent the folder where the input names it, for it to check and write on the cluster."""
    tool = tmp_path / 'writes_in.cwl'
    tool.write_text("""cwlVersion: v1.2
class: CommandLineTool
requirements:
  InlineJavascriptRequirement: {}
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.out)
        writable: true
inputs:
  out:
    type: Directory
outputs:
  result:
    type: Directory
    outputBinding:
      glob: $(inputs.out.basename)
baseCommand: touch
""", encoding='utf-8')
    step = Step(clt_path=tool, step_name='write')
    step.inputs.out = 'results'
    workflow = Workflow([step], 'in_place')
    assert workflow.compile().cwl_job_inputs['write~out'] == 'results'
    request = compute.build_request(workflow)
    assert request['inputs']['write~out'] == {'class': 'Directory', 'location': 'results'}
    (run,) = (step['run'] for step in request['workflow']['steps'])
    declared = run['inputs'] if isinstance(run['inputs'], dict) else {i['id']: i for i in run['inputs']}
    assert declared['out']['type'] == 'Directory'
    step.inputs.out = '/projects/lab/results'
    assert compute.build_request(workflow)['inputs']['write~out']['location'] == '/projects/lab/results'


@pytest.mark.fast
def test_a_run_id_is_the_workflow_name_the_time_and_four_hex_digits_within_the_services_rule() -> None:
    """`<name>-<UTC yyyymmddThhmmss>-<4 hex>`; a name outside the rule is fitted to it."""
    rule = Draft202012Validator(compute.SCHEMA['$defs']['Id'])
    now = datetime(2026, 10, 26, 14, 22, 5, tzinfo=timezone.utc)
    assert re.fullmatch(r'segment-20261026T142205-[0-9a-f]{4}', compute.new_run_id('segment', now))
    for name in ['segment', 'a b/c', '.hidden', '__x', 'é' * 5, '', 'x' * 300, '-', 'ok.name_1-2']:
        made = compute.new_run_id(name, now)
        assert rule.is_valid(made) and len(made) <= 128, (name, made)
    assert compute.new_run_id('a b/c', now).startswith('a-b-c-2026')
    assert compute.new_run_id('...', now).startswith('run-2026')
    assert compute.new_run_id('a', now) != compute.new_run_id('a', now)


@pytest.mark.fast
def test_a_path_that_names_nothing_on_the_cluster_is_refused_before_sending_naming_its_input() -> None:
    """`~` and an environment variable are this machine's; a URL is the service's to read."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.build_request(_workflow(out='~/results', model='$HOME/sam3.pt'))
    assert [(d.code, d.message.split(':')[0]) for d in refused.value.diagnostics] == [
        (SophiosErrorCode.COMPUTE_REQUEST_REFUSED, 'input write~out'),
        (SophiosErrorCode.COMPUTE_REQUEST_REFUSED, 'input write~model')]
    assert "'~/results' names nothing on the cluster" in refused.value.diagnostics[0].message
    compute.build_request(_workflow(model='https://example.org/$file?x=~y'))
    compute.build_request(_workflow(out='results/a~b', model='models/50$'))


@pytest.mark.fast
def test_every_field_the_schema_refuses_is_a_line_in_the_schemas_words() -> None:
    """All at once, each where it is and what the field is."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.build_request(_workflow(), leader={'time': '3 days', 'memory': '8GB'}, log_level='LOUD',
                              steps={'partition': 'a b'})
    assert sorted(d.message for d in refused.value.diagnostics) == [
        "leader.memory: '8GB' is not the leader's memory, as Slurm writes it: a whole number with K, M, G or T, "
        'such as 8G',
        "leader.time: '3 days' is not a Slurm time limit: M, M:S, H:M:S, D-H, D-H:M or D-H:M:S, such as 3-00:00:00",
        "logLevel: 'LOUD' is not Toil's log level: DEBUG, INFO (the default), WARNING or ERROR",
        "steps.partition: 'a b' is not a Slurm partition of this cluster: letters, digits, '_', '.' or '-'"]
    request = compute.build_request(_workflow(), leader={'partition': 'normal', 'time': '1-00:00:00', 'memory': '8G'},
                                    steps={'partition': 'normal', 'gpuPartition': 'gpu', 'time': '10'},
                                    log_level='DEBUG')
    assert (request['leader']['time'], request['steps']['gpuPartition'], request['logLevel']) == (
        '1-00:00:00', 'gpu', 'DEBUG')


@pytest.mark.fast
def test_a_workflow_with_a_real_time_analysis_is_refused_the_service_never_runs_one() -> None:
    """Refused before sending, naming the analysis, rather than dropped."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    declaration = Step(clt_path=ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl')
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    with pytest.raises(compute.RequestRefused, match="real-time analysis 'touch', which the compute service never"):
        compute.build_request(Workflow([touch, declaration], 'live'))


@pytest.mark.fast
def test_where_the_outputs_go_is_one_choice_and_workflow_out_dir_is_its_folder() -> None:
    """`workflow_out_dir` copies them into a folder; `workflow_in_place` writes each where its input names it."""
    request = compute.build_request(_workflow(), workflow_out_dir='/projects/lab/results')
    assert (request['outputMode'], request['workflowOutDir']) == ('workflowOutDir', '/projects/lab/results')
    assert compute.build_request(_workflow(), workflow_out_dir='results')['workflowOutDir'] == 'results'
    assert compute.build_request(_workflow(), workflow_in_place=True)['outputMode'] == 'workflowInPlace'
    assert 'outputMode' not in compute.build_request(_workflow())
    with pytest.raises(ValueError, match='not both'):
        compute.build_request(_workflow(), workflow_out_dir='results', workflow_in_place=True)


@pytest.mark.fast
def test_the_vendored_schema_is_a_json_schema_2020_12_document_and_its_example_a_request() -> None:
    """The copy the requests are checked with is itself valid, and takes its own example."""
    Draft202012Validator.check_schema(compute.SCHEMA)
    assert Draft202012Validator(compute.SCHEMA).is_valid(compute.SCHEMA['examples'][0])


def _answer(status: int, body: object, headers: dict[str, str] | None = None) -> requests.Response:
    """An HTTP answer, as `requests` gives one."""
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()  # pylint: disable=protected-access
    response.headers.update({'Content-Type': 'application/json', **(headers or {})})
    return response


def _serving(monkeypatch: pytest.MonkeyPatch,
             answers: dict[tuple[str, str], requests.Response | Exception]) -> list[tuple[str, str, object]]:
    """Have every HTTP call answered from `answers`, by method and path; each call is appended to the list returned,
    as its method, path and timeout."""
    calls: list[tuple[str, str, object]] = []

    def request(_: requests.Session, method: str, url: str, **options: object) -> requests.Response:
        path = url.split('://', 1)[1].split('/', 1)[1]
        calls.append((method, path, options.get('timeout')))
        found = answers[(method, path)]
        if isinstance(found, Exception):
            raise found
        return found
    monkeypatch.setattr(requests.Session, 'request', request)
    return calls


_CONTRACT = _answer(200, {'openapi': '3.1.0', 'info': {'version': '2.0.0'}})


@pytest.mark.fast
def test_a_service_that_speaks_another_contract_is_refused_before_anything_is_sent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The major version is read once, from openapi.json."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _answer(200, {'info': {'version': '3.1.0'}})})
    with pytest.raises(compute.ServiceUnavailable, match='speaks contract 3.1.0, and this Sophios speaks 2'):
        compute.Service('http://127.0.0.1:1').submit(_workflow())


@pytest.mark.fast
def test_a_submission_lost_on_the_way_says_the_run_may_exist_and_how_to_follow_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Sent again until PATIENCE runs out; then the id it was sent under, and `attach`."""
    monkeypatch.setattr(compute, 'PATIENCE', 0.0)
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): requests.ConnectionError('gone')})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('http://127.0.0.1:1').submit(_workflow())
    could_not, may_exist = (d.message for d in unavailable.value.diagnostics)
    assert could_not == 'the compute service at http://127.0.0.1:1 could not be reached (ConnectionError), for 0 s'
    assert re.fullmatch(r"run (segment-\S+) may exist all the same: Service\('http://127.0.0.1:1'\)\.attach\('\1'\) "
                        r'follows it, and the same request sent again never starts a second one', may_exist)


@pytest.mark.fast
def test_an_answer_the_contract_does_not_document_is_the_services_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 is the service's; its message is shown."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT,
                           ('GET', 'runs/r1'): _answer(500, {'errors': [{'message': 'the driver failed'}]})})
    with pytest.raises(compute.ServiceUnavailable, match='answered 500: the driver failed'):
        compute.Service('http://127.0.0.1:1').attach('r1')


@pytest.mark.fast
def test_a_status_takes_what_it_knows_and_leaves_the_rest(monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """Unknown fields are ignored, a step without a name is its Slurm job, nothing is parsed out of a message;
    the warnings are printed where they cannot be missed, at the input they name."""
    answer: dict[str, object] = {
        'id': 'r1', 'state': 'RUNNING', 'reason': None, 'slurmJobId': '7', 'startedAt': '2026-10-26T10:00:00-04:00',
        'endedAt': None, 'outputs': None, 'targets': {}, 'new': 'field',
        'steps': [{'name': None, 'state': 'FAILED', 'exitCode': '1:0', 'slurmJobId': '8', 'logs': []}],
        'warnings': [{'pointer': '/inputs/write~out/location', 'message': 'is empty; the run writes into it'}]}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, answer)})
    run = compute.Service('http://127.0.0.1:1').attach('r1')
    assert capsys.readouterr().err == ('WARNING from the compute service: input write~out: is empty; the run '
                                       'writes into it\n')
    status = run.status()
    assert status.state is RunState.RUNNING and status.started_at == datetime.fromisoformat('2026-10-26T10:00:00-04:00')
    assert [(s.name, s.state, s.exit_status, s.signal) for s in status.steps] == [
        ('(Slurm job 8)', RunState.FAILED, None, None)]


@pytest.mark.fast
def test_ctrl_c_while_waiting_leaves_the_run_going_and_says_how_to_follow_it(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The interrupt goes on up, after one line on how to attach again."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT,
                           ('GET', 'runs/r1'): _answer(200, {'id': 'r1', 'state': 'QUEUED'})})
    run = compute.Service('http://127.0.0.1:1').attach('r1')

    def interrupted() -> None:
        raise KeyboardInterrupt
    monkeypatch.setattr(run, 'status', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run.wait()
    assert capsys.readouterr().err.strip() == (
        "Run r1 goes on in Slurm: Service('http://127.0.0.1:1').attach('r1') follows it again, and its cancel() "
        'stops it.')


@pytest.mark.fast
def test_a_failure_whose_log_cannot_be_read_still_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leader Slurm never started has no log: the reason stands, and the log is said to be unreadable."""
    ended = {'id': 'r1', 'state': 'FAILED', 'reason': 'Slurm will never start the leader (PartitionTimeLimit)'}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, ended),
                           ('GET', 'runs/r1/logs/leader.log'): _answer(404, {'errors': [{'message': 'no log yet'}]})})
    assert compute.Service('http://127.0.0.1:1').attach('r1').failure() == [
        'run r1 ended FAILED: Slurm will never start the leader (PartitionTimeLimit)',
        'the end of leader.log:\n(it cannot be read: error [cmp001] no log yet)']


@pytest.mark.fast
def test_a_tunnel_ssh_cannot_open_without_a_login_fails_at_once_saying_how_to_log_in(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BatchMode: ssh never asks for a password; the error says what ssh said, and to log in once in a terminal."""
    calls = fake_ssh.install(tmp_path)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        with compute.Service.over_ssh('nologin'):
            pass
    ended, how = (d.message for d in unavailable.value.diagnostics)
    assert ended == ('ssh nologin ended (nologin: Permission denied (publickey).) before its tunnel to '
                     '/run/slurm-driver/api.sock was open')
    assert how.startswith('when ssh needs a password or a code, run `ssh nologin true` in a terminal once')
    (argv,) = (json.loads(line) for line in calls.read_text('utf-8').splitlines())
    assert argv[:-2] == ['-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-L']
    assert re.fullmatch(r'127\.0\.0\.1:\d+:/run/slurm-driver/api\.sock', argv[-2]) and argv[-1] == 'nologin'


@pytest.mark.fast
def test_a_submission_may_take_longer_than_a_status_read_before_it_is_sent_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The service asks Slurm several times, 30 s each at most, before it answers a POST /runs: a slow but healthy
    submission is waited for, not sent again; every other call is given up on sooner."""
    accepted = _answer(201, {'id': 'x', 'state': 'QUEUED', 'slurmJobId': '7'})
    calls = _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): accepted,
                                   ('GET', 'runs/x'): _answer(200, {'id': 'x', 'state': 'QUEUED'})})
    compute.Service('http://127.0.0.1:1').submit(_workflow()).status()
    assert [(method, path, timeout) for method, path, timeout in calls] == [
        ('GET', 'openapi.json', (10.0, 60.0)), ('POST', 'runs', (10.0, 300.0)), ('GET', 'runs/x', (10.0, 60.0))]
