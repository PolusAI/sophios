# pylint: disable=too-many-lines
"""The compute service's request, as Sophios builds it: `sophios.compute.build_request`, and its run, as a `Run`
follows it.

Fast and without a service: what the request holds, what Sophios refuses before sending, and what a `Run` makes of
the service's answers. What the service answers is `test_compute_service.py`'s, against the service itself.
"""
import inspect
import json
import os
import re
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import typing
from collections.abc import Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
import requests
from jsonschema import Draft202012Validator

from sophios import compute
from sophios.compute import RunConfig
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.run_local import RunHandle
from sophios.run_status import RunState

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / 'cwl_adapters'
DOCS = ROOT / 'docs'


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
    """One inline document, its inputs, and `basepath` only when given; nothing is joined or checked, `~` and
    `$HOME` included: the service is the one authority on paths."""
    config = RunConfig('/projects/lab/results').basepath('/projects/lab')
    request = compute.build_request(_workflow(model='https://example.org/sam3.pt'), config)
    assert set(request) == {'id', 'workflow', 'inputs', 'basepath', 'outputMode', 'workflowOutDir'}
    assert request['basepath'] == '/projects/lab'
    assert request['workflow']['class'] == 'Workflow' and request['workflow']['cwlVersion'] == 'v1.2'
    assert all(isinstance(step['run'], dict) for step in request['workflow']['steps'])
    assert request['inputs'] == {'write~out': {'class': 'Directory', 'location': 'results/run7'},
                                 'write~model': {'class': 'File', 'location': 'https://example.org/sam3.pt'}}
    assert 'basepath' not in compute.build_request(_workflow(out='/projects/lab/out'))
    shell = compute.build_request(_workflow(out='~/results', model='$HOME/sam3.pt'))['inputs']
    assert [shell[name]['location'] for name in ('write~out', 'write~model')] == ['~/results', '$HOME/sam3.pt']


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
def test_every_field_the_schema_refuses_is_a_line_in_the_schemas_words() -> None:
    """All at once, each where it is and what the field is."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.build_request(_workflow(), RunConfig('results').leader(time='3 days', memory='8GB')
                              .log_level('LOUD')  # type: ignore[arg-type]
                              .steps(partition='a b'))
    assert sorted(refused.value.messages) == [
        "leader.memory: '8GB' is not the leader's memory, as Slurm writes it: a whole number with K, M, G or T, "
        'such as 8G',
        "leader.time: '3 days' is not a Slurm time limit: M, M:S, H:M:S, D-H, D-H:M or D-H:M:S, such as 3-00:00:00",
        "logLevel: 'LOUD' is not Toil's log level: DEBUG, INFO (the default), WARNING or ERROR",
        "steps.partition: 'a b' is not a Slurm partition of this cluster: letters, digits, '_', '.' or '-'"]
    request = compute.build_request(_workflow(), RunConfig('results')
                                    .leader(partition='normal', time='1-00:00:00', memory='8G')
                                    .steps(partition='normal', gpu_partition='gpu', time='10').log_level('DEBUG'))
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


def _camel(name: str) -> str:
    first, *rest = name.split('_')
    return first + ''.join(word.capitalize() for word in rest)


#: A valid value of every field RunConfig's setters set, as the request holds it.
_VALID: typing.Final[dict[str, typing.Any]] = {
    'basepath': '/projects/lab', 'leader': {'partition': 'normal', 'time': '1-00:00:00', 'memory': '8G'},
    'steps': {'partition': 'normal', 'gpuPartition': 'gpu', 'time': '10'}, 'logLevel': 'DEBUG'}


def _setters() -> dict[str, inspect.Signature]:
    """RunConfig's setters, each named as the field of the request it sets: its public methods but `in_place`."""
    return {name: inspect.signature(method) for name, method in vars(RunConfig).items()
            if inspect.isfunction(method) and not name.startswith('_')}


def _set_every_field(config: RunConfig) -> RunConfig:
    """Call each setter of `config` with every argument it takes, each the value `_VALID` gives its field."""
    for name, signature in _setters().items():
        field = _camel(name)
        parameters = list(signature.parameters.values())[1:]
        positional = [_VALID[field] for parameter in parameters if parameter.kind is parameter.POSITIONAL_OR_KEYWORD]
        keywords = {parameter.name: _VALID[field][_camel(parameter.name)] for parameter in parameters
                    if parameter.kind is parameter.KEYWORD_ONLY}
        assert getattr(config, name)(*positional, **keywords) is config, name
    return config


@pytest.mark.fast
def test_run_config_reaches_every_field_of_the_request_but_its_workflow_and_inputs_as_the_schema_says() -> None:
    """Both constructors and every setter, each given a valid value, reach exactly the vendored schema's top-level
    properties but id, workflow and inputs, each under its own name; the keywords of `leader` and `steps` are the
    properties of the schema's Leader and Steps, camelCased; the output modes are the two constructors' and none's;
    and no setter touches where the outputs go. The vendored schema and RunConfig cannot drift apart."""
    properties, defs = compute.SCHEMA['properties'], compute.SCHEMA['$defs']
    workflow = {'class': 'Workflow', 'cwlVersion': 'v1.2'}
    core = {'id': 'r1', 'workflow': workflow, 'inputs': {}}
    copied = compute.request_for('r1', workflow, {}, _set_every_field(RunConfig('results')))
    in_place = compute.request_for('r1', workflow, {}, _set_every_field(RunConfig.in_place()))
    assert set(copied) | set(in_place) == set(properties)
    assert copied == core | {'outputMode': 'workflowOutDir', 'workflowOutDir': 'results'} | _VALID
    assert in_place == core | {'outputMode': 'workflowInPlace'} | _VALID
    assert compute.request_for('r1', workflow, {}) == core
    # Without a RunConfig, outputMode is left out: serviceOutDir, the service's default.
    assert {copied['outputMode'], in_place['outputMode'], 'serviceOutDir'} == set(properties['outputMode']['enum'])
    for name in ('leader', 'steps'):
        keywords = {_camel(parameter.name) for parameter in _setters()[name].parameters.values()
                    if parameter.kind is parameter.KEYWORD_ONLY}
        assert keywords == set(defs[name.capitalize()]['properties']), name
    levels = typing.get_args(typing.get_type_hints(RunConfig.log_level)['level'])
    assert set(levels) == set(properties['logLevel']['enum'])


@pytest.mark.fast
def test_each_setter_sets_just_what_it_is_passed_and_leaves_the_rest() -> None:
    """Called again, a setter replaces what it is passed again and keeps the rest; what no setter sets is left out
    of the request, for the service's own settings."""
    config = RunConfig('/shared/lab/results').basepath('/shared/lab').leader(time='2:00:00', memory='4G')
    config.steps(time='30:00').leader(memory='8G', partition='normal').basepath('/shared/lab2')
    request = compute.request_for('r1', {'class': 'Workflow', 'cwlVersion': 'v1.2'}, {}, config)
    assert request == {'id': 'r1', 'workflow': {'class': 'Workflow', 'cwlVersion': 'v1.2'}, 'inputs': {},
                       'outputMode': 'workflowOutDir', 'workflowOutDir': '/shared/lab/results',
                       'basepath': '/shared/lab2', 'leader': {'time': '2:00:00', 'memory': '8G', 'partition': 'normal'},
                       'steps': {'time': '30:00'}}


@pytest.mark.fast
def test_the_compute_codes_are_the_clients_own_and_never_a_sophios_errors() -> None:
    """cmp001 and cmp002 belong to sophios.compute: a ComputeError is not a SophiosError, no SophiosErrorCode is
    one of them, docs/compute.md documents exactly them, and the message is every line under its code."""
    errors = compute.ComputeError.__subclasses__()
    assert [(error, error.code) for error in errors] == [
        (compute.RequestRefused, 'cmp001'), (compute.ServiceUnavailable, 'cmp002')]
    assert not issubclass(compute.ComputeError, SophiosError)
    assert not {error.code for error in errors} & {str(code) for code in SophiosErrorCode}
    documented = set(re.findall(r'\bcmp\d{3}\b', (DOCS / 'compute.md').read_text('utf-8')))
    assert documented == {error.code for error in errors}
    refused = compute.RequestRefused('one', 'two')
    assert (refused.messages, str(refused)) == (('one', 'two'), '[cmp001] one\n[cmp001] two')


@pytest.mark.fast
def test_a_workflow_that_does_not_compile_is_a_sophios_error_and_nothing_is_sent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The compile is Sophios's, so its failure is a SophiosError with Sophios's own code, before the service is
    asked anything: the one failure of `submit` that is not a ComputeError."""
    calls = _serving(monkeypatch, {})
    step = Step(CommandLineTool('count', Inputs(n=Input(cwl.int, position=1)), Outputs()).base_command('echo'))
    step.inputs.n = 'not an int'
    with pytest.raises(SophiosError) as raised:
        compute.Service('unix:///api.sock').submit(Workflow([step], 'count'))
    assert [diagnostic.code for diagnostic in raised.value.diagnostics] == [SophiosErrorCode.LITERAL_TYPE_MISMATCH]
    assert not calls


@pytest.mark.fast
def test_the_vendored_schema_is_a_json_schema_2020_12_document_and_its_example_a_request() -> None:
    """The copy the requests are checked with is itself valid, and takes its own example."""
    Draft202012Validator.check_schema(compute.SCHEMA)
    assert Draft202012Validator(compute.SCHEMA).is_valid(compute.SCHEMA['examples'][0])


#: The header the service names itself in, on every answer.
_SERVICE: typing.Final = {'Server': 'slurm-driver/2.0.0'}


def _raw(status: int, text: str, headers: dict[str, str]) -> requests.Response:
    """An HTTP answer, as `requests` gives one."""
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()  # pylint: disable=protected-access
    response.headers.update(headers)
    return response


def _answer(status: int, body: object, headers: dict[str, str] | None = None) -> requests.Response:
    """An answer of the service."""
    return _raw(status, json.dumps(body), {'Content-Type': 'application/json', **_SERVICE, **(headers or {})})


def _text(text: str) -> requests.Response:
    """A log, as the service answers one."""
    return _raw(200, text, _SERVICE)


#: Why a test that makes a Service skips on Windows itself, where Service refuses every URL. The fake service, its
#: socket and the stand-in for ssh each skip so, and so does a test that makes one without them.
_WINDOWS: typing.Final = 'the compute client needs macOS, Linux or WSL'


def _serving(monkeypatch: pytest.MonkeyPatch,
             answers: dict[tuple[str, str], requests.Response | Exception | list[requests.Response | Exception]]
             ) -> list[tuple[str, str, object]]:
    """Have every HTTP call answered from `answers`, by method and path, a list's answers one per call in turn; each
    call is appended to the list returned, as its method, path and timeout."""
    if sys.platform == 'win32':
        pytest.skip(_WINDOWS)
    calls: list[tuple[str, str, object]] = []

    def request(_: requests.Session, method: str, url: str, **options: object) -> requests.Response:
        path = url.split('://', 1)[1].split('/', 1)[1]
        calls.append((method, path, options.get('timeout')))
        found = answers[(method, path)]
        if isinstance(found, list):
            found = found.pop(0)
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
        compute.Service('unix:///api.sock').submit(_workflow())


@pytest.mark.fast
def test_a_submission_lost_on_the_way_says_the_run_may_exist_and_how_to_follow_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Sent again until PATIENCE runs out; then the id it was sent under, and `attach`."""
    monkeypatch.setattr(compute, 'PATIENCE', 0.0)
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): requests.ConnectionError('gone')})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('unix:///api.sock').submit(_workflow())
    could_not, may_exist = unavailable.value.messages
    assert could_not == 'the compute service at unix:///api.sock could not be reached (ConnectionError), for 0 s'
    assert re.fullmatch(r"run (segment-\S+) may exist all the same: Service\('unix:///api.sock'\)\.attach\('\1'\) "
                        r'follows it, and the same request sent again never starts a second one', may_exist)


@pytest.mark.fast
def test_an_answer_the_contract_does_not_document_is_the_services_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 is the service's; its message is shown."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT,
                           ('GET', 'runs/r1'): _answer(500, {'errors': [{'message': 'the driver failed'}]})})
    with pytest.raises(compute.ServiceUnavailable, match='answered 500: the driver failed'):
        compute.Service('unix:///api.sock').attach('r1')


@pytest.mark.fast
def test_a_refusal_is_each_problem_of_the_service_in_its_words_at_the_authored_input(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A pointer into the request is named as the author wrote it; Sophios adds no line of its own."""
    refusal = {'pointer': '/inputs/write~0model/location',
               'message': 'models/sam3.pt is relative: send basepath, the cluster directory it is relative to'}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): _answer(400, {'errors': [refusal]})})
    with pytest.raises(compute.RequestRefused) as refused:
        compute.Service('unix:///api.sock').submit(_workflow())
    assert refused.value.messages == (
        'input write~model: models/sam3.pt is relative: send basepath, the cluster directory it is relative to',)


@pytest.mark.fast
def test_a_status_takes_what_it_knows_and_leaves_the_rest(monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """Unknown fields are ignored, a step without a name is its Slurm job, nothing is parsed out of a message;
    the warnings are printed where they cannot be missed, at the input they name."""
    answer: dict[str, object] = {
        'id': 'r1', 'state': 'RUNNING', 'reason': None, 'slurmJobId': '7', 'startedAt': '2026-10-26T10:00:00-04:00',
        'endedAt': None, 'outputs': None, 'new': 'field',
        'steps': [{'name': None, 'state': 'FAILED', 'exitCode': '1:0', 'slurmJobId': '8', 'logs': []}],
        'warnings': [{'pointer': '/inputs/write~out/location', 'message': 'is empty; the run writes into it'}]}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, answer)})
    run = compute.Service('unix:///api.sock').attach('r1')
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
    run = compute.Service('unix:///api.sock').attach('r1')

    def interrupted() -> None:
        raise KeyboardInterrupt
    monkeypatch.setattr(run, 'status', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run.wait()
    assert capsys.readouterr().err.strip() == (
        "Run r1 goes on in Slurm: Service('unix:///api.sock').attach('r1') follows it again, and its cancel() "
        'stops it.')


@pytest.mark.fast
@pytest.mark.parametrize('state', [RunState.FAILED, RunState.CANCELLED])
def test_wait_returns_a_run_that_did_not_complete_as_its_status_and_raises_nothing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: RunState) -> None:
    """As the local handle's `wait`: the run's status however it ended, with the service's reason, printed once."""
    ended = {'id': 'r1', 'state': state.value, 'reason': 'Slurm will never start the leader (PartitionTimeLimit)'}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, ended)})
    status = compute.Service('unix:///api.sock').attach('r1').wait()
    assert (status.state, status.reason, status.outputs) == (
        state, 'Slurm will never start the leader (PartitionTimeLimit)', None)
    assert capsys.readouterr().out == f'r1: {state.value} (Slurm will never start the leader (PartitionTimeLimit))\n'


@pytest.mark.fast
def test_logs_are_the_leaders_log_of_the_whole_run_never_a_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """`leader.log`, even when a step failed with logs of its own, as the local handle's `logs` is the runner's."""
    failed = {'id': 'r1', 'state': 'FAILED', 'reason': 'step show failed (exit code 1:0); see its logs',
              'steps': [{'name': 'show', 'state': 'FAILED', 'slurmJobId': '8', 'logs': ['failed_CWLJob_show_000.log']}]}
    calls = _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, failed),
                                   ('GET', 'runs/r1/logs/leader.log'): _text('the leader\n'),
                                   ('GET', 'runs/r1/logs/failed_CWLJob_show_000.log'): _text('boom\n')})
    run = compute.Service('unix:///api.sock').attach('r1')
    assert run.wait().steps[0].name == 'show'
    assert run.logs() == 'the leader\n'
    assert calls[-1][:2] == ('GET', 'runs/r1/logs/leader.log')


@pytest.mark.fast
def test_a_run_whose_leader_slurm_never_started_has_no_log_and_logs_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slurm makes `leader.log` as it starts the leader, so the service answers 404 for a run it never started;
    `logs` is '', as the local handle's is with no log."""
    never = {'id': 'r1', 'state': 'FAILED', 'reason': 'Slurm will never start the leader (PartitionTimeLimit)'}
    missing = {'errors': [{'message': 'the run has no log leader.log, or not yet'}]}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, never),
                           ('GET', 'runs/r1/logs/leader.log'): _answer(404, missing)})
    assert compute.Service('unix:///api.sock').attach('r1').logs() == ''


@pytest.mark.fast
def test_a_compute_run_is_the_local_handle_but_for_what_each_has_alone() -> None:
    """Each method both have takes and returns the same; `slurm_job` and `warnings` are the compute run's alone, and
    `collect` the local handle's, as docs/compute.md's table says."""
    run, handle = ({name for name in dir(kind) if not name.startswith('_')} for kind in (compute.Run, RunHandle))
    assert (run - handle, handle - run) == ({'slurm_job', 'warnings'}, {'collect'})
    for name in run & handle:
        assert inspect.signature(getattr(compute.Run, name)) == inspect.signature(getattr(RunHandle, name)), name


@pytest.mark.fast
@pytest.mark.parametrize('page', [_raw(404, '<html>Not Found</html>', {}), _raw(200, '<html>It works!</html>', {})])
def test_anything_but_the_service_answering_is_unavailable_and_names_the_socket(
        monkeypatch: pytest.MonkeyPatch, page: requests.Response) -> None:
    """The service names itself in every answer's Server header: a page from anything else, a 404 or a 200, is
    neither a refusal nor a run nor a log, for submit, status and logs alike."""
    stranger = ('unix:///api.sock answered, but not as the compute service: check that it names the socket the '
                'service listens on',)
    _serving(monkeypatch, {('GET', 'openapi.json'): page})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('unix:///api.sock').submit(_workflow())
    assert unavailable.value.messages == stranger
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): [_run('RUNNING'), page],
                           ('GET', 'runs/r1/logs/leader.log'): page})
    run = compute.Service('unix:///api.sock').attach('r1')
    for asked in (run.status, run.logs):
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            asked()
        assert unavailable.value.messages == stranger


@pytest.mark.fast
def test_an_answer_of_the_service_that_is_not_json_is_its_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every answer of the service but a log is JSON; one that is not is the service's failure, not a crash."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _text('<html>oops</html>')})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('unix:///api.sock').attach('r1')
    assert unavailable.value.messages == ('the compute service at unix:///api.sock answered 200, not in JSON',)


@pytest.mark.fast
def test_a_busy_service_is_asked_again_every_5_s_whatever_its_retry_after_says(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After is not read: a date in it, as RFC 9110 allows, changes nothing. A call answered 502, 503 or 504
    is made again whoever sends it, a proxy or the service."""
    pauses: list[float] = []
    monkeypatch.setattr(compute.time, 'sleep', pauses.append)
    busy = _answer(503, {'errors': [{'message': 'sacct failed'}]}, {'Retry-After': 'Wed, 28 Oct 2026 07:28:00 GMT'})
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT,
                           ('GET', 'runs/r1'): [busy, _raw(502, '<html>Bad Gateway</html>', {}), _run('QUEUED')]})
    assert compute.Service('unix:///api.sock').attach('r1').id == 'r1'
    assert pauses == [5.0, 5.0]


@pytest.mark.fast
def test_an_answer_cut_short_is_asked_for_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service cuts the connection when it fails after the headers, as a log read may, and so does an SSH forward
    that drops: the call is made again, as when the service cannot be reached."""
    pauses: list[float] = []
    monkeypatch.setattr(compute.time, 'sleep', pauses.append)
    cut = requests.exceptions.ChunkedEncodingError('cut')
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _run('RUNNING'),
                           ('GET', 'runs/r1/logs/leader.log'): [cut, _text('x')]})
    assert compute.Service('unix:///api.sock').attach('r1').logs() == 'x'
    assert pauses == [5.0]


@pytest.mark.fast
def test_the_service_lane_is_skipped_not_errored_without_the_driver() -> None:
    """A plain pytest on a machine without the driver checkout skips test_compute_service.py, saying what to set,
    as the repository's other environment-gated tests skip; the lane's own command always sets it."""
    env = {name: value for name, value in os.environ.items() if name != 'SOPHIOS_SLURM_DRIVER'}
    ran = subprocess.run([sys.executable, '-m', 'pytest', 'tests/core/test_compute_service.py', '-rs',
                          '-p', 'no:cacheprovider'], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0 and 'set SOPHIOS_SLURM_DRIVER to the packages/slurm-driver' in ran.stdout, ran.stdout


def _run(state: str, reason: str | None = None) -> requests.Response:
    return _answer(200, {'id': 'r1', 'state': state, 'reason': reason})


@pytest.mark.fast
@pytest.mark.parametrize(('answers', 'cancelled'), [
    ([_run('CANCELLED', 'cancelled by request')], True),
    ([_run('COMPLETED')], False),
    ([_run('FAILED', "the workflow failed; see the run's log, GET /runs/r1/logs/leader.log")], False),
    ([_run('RUNNING', 'cancel requested at 2026-10-27T10:00:00Z'), _run('CANCELLED', 'cancelled by request')], True)])
def test_cancel_is_sent_once_and_says_whether_the_run_is_cancelled_as_future_cancel_does(
        monkeypatch: pytest.MonkeyPatch, answers: list[requests.Response], cancelled: bool) -> None:
    """True once the run is CANCELLED; False when it had ended COMPLETED or FAILED, which the service answers
    unchanged. The service asks Slurm before it answers, so the cancel is sent once, and the run waited for, every
    POLL seconds, until it has ended."""
    pauses: list[float] = []
    monkeypatch.setattr(compute.time, 'sleep', pauses.append)
    calls = _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs/r1/cancel'): [answers[0]],
                                   ('GET', 'runs/r1'): [_run('RUNNING'), *answers]})
    run = compute.Service('unix:///api.sock').attach('r1')
    assert run.cancel() is cancelled
    assert [path for method, path, _ in calls if method == 'POST'] == ['runs/r1/cancel']
    assert pauses == [compute.POLL] * (len(answers) - 1)


@pytest.mark.fast
def test_a_cancel_slurm_never_acts_on_is_waited_for_as_wait_patience_and_never_sent_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that never ends: TimeoutError once PATIENCE is up, never later, saying it goes on, after one POST."""
    monkeypatch.setattr(compute, 'PATIENCE', 0.0)
    monkeypatch.setattr(compute.time, 'sleep', lambda _seconds: pytest.fail('waited on past PATIENCE'))
    running = _run('RUNNING', 'cancel requested at 2026-10-27T10:00:00Z')
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): running,
                           ('POST', 'runs/r1/cancel'): [running]})
    with pytest.raises(TimeoutError, match='run r1 has not ended within 0 s; it goes on'):
        compute.Service('unix:///api.sock').attach('r1').cancel()


@pytest.mark.fast
def test_a_submission_may_take_longer_than_a_status_read_before_it_is_sent_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The service asks Slurm several times, 30 s each at most, before it answers a POST /runs: a slow but healthy
    submission is waited for, not sent again; every other call is given up on sooner."""
    accepted = _answer(201, {'id': 'x', 'state': 'QUEUED', 'slurmJobId': '7'})
    calls = _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): accepted,
                                   ('GET', 'runs/x'): _answer(200, {'id': 'x', 'state': 'QUEUED'})})
    compute.Service('unix:///api.sock').submit(_workflow()).status()
    assert [(method, path, timeout) for method, path, timeout in calls] == [
        ('GET', 'openapi.json', (10.0, 60.0)), ('POST', 'runs', (10.0, 300.0)), ('GET', 'runs/x', (10.0, 60.0))]


class _Driver(BaseHTTPRequestHandler):
    """The service, as far as these tests ask it, on a Unix socket: its contract, the run r1, and a cancel of r1
    refused, as the service refuses one by an account that did not submit the run."""

    server_version, sys_version = 'slurm-driver/2.0.0', ''
    answers: typing.ClassVar = {
        ('GET', '/openapi.json'): (200, {'openapi': '3.1.0', 'info': {'version': '2.0.0'}}),
        ('GET', '/runs/r1'): (200, {'id': 'r1', 'state': 'RUNNING'}),
        ('POST', '/runs/r1/cancel'): (403, {'errors': [{'message': 'run r1 was submitted by alice: only alice, or '
                                                                   'the service account, may cancel it'}]})}

    def _answer(self) -> None:
        """Answer the call as `answers` says."""
        status, body = self.answers[(self.command, self.path)]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer

    def log_message(self, *_: object) -> None:  # pylint: disable=arguments-differ  # quiet
        pass


@pytest.fixture(name='driver')
def _driver() -> Iterator[Path]:
    """The socket `_Driver` answers on, in a directory whose path is short enough for a Unix socket's."""
    if sys.platform == 'win32':
        pytest.skip(_WINDOWS)
    with tempfile.TemporaryDirectory() as directory, \
            socketserver.ThreadingUnixStreamServer(os.path.join(directory, 'api.sock'), _Driver) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield Path(directory) / 'api.sock'
        server.shutdown()


#: A stand-in for ssh, on PATH: it records its pid, whether it ignores Ctrl-C, and its arguments, a JSON line each
#: run, then forwards each connection to the local socket of `-L local:remote` to its "remote" socket, on this
#: machine, as ssh does, saying why and closing the connection when there is nothing to connect to there; or fails as
#: ssh fails to reach a host called nowhere; or, for a host called slow, waits as ssh waits for a password.
_SSH = """#!{python}
import json, os, signal, socket, sys, threading, time
with open(os.path.join(os.path.dirname(__file__), 'calls'), 'a', encoding='utf-8') as calls:
    print(json.dumps([os.getpid(), signal.getsignal(signal.SIGINT) == signal.SIG_IGN, *sys.argv[1:]]), file=calls)
if sys.argv[-1].endswith('nowhere'):
    sys.exit(print('ssh: Could not resolve hostname nowhere: Name or service not known', file=sys.stderr) or 255)
if sys.argv[-1].endswith('slow'):
    time.sleep(60)
local, remote = sys.argv[sys.argv.index('-L') + 1].split(':')

def relay(source, sink):
    try:
        while data := source.recv(65536):
            sink.sendall(data)
        sink.shutdown(socket.SHUT_WR)
    except OSError:
        pass

listener = socket.socket(socket.AF_UNIX)
listener.bind(local)
listener.listen()
while True:
    near, _ = listener.accept()
    far = socket.socket(socket.AF_UNIX)
    try:
        far.connect(remote)
    except OSError as error:
        print(f'channel 2: open failed: connect failed: {error.strerror}', file=sys.stderr)
        near.close()
        continue
    for ends in ((near, far), (far, near)):
        threading.Thread(target=relay, args=ends, daemon=True).start()
"""


@pytest.fixture(name='ssh_calls')
def _ssh_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the stand-in for ssh, first on PATH, records each run of it."""
    if sys.platform == 'win32':
        pytest.skip(_WINDOWS)
    ssh = tmp_path / 'ssh'
    ssh.write_text(_SSH.replace('{python}', sys.executable), encoding='utf-8')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    return tmp_path / 'calls'


def _ssh_run(calls: Path) -> tuple[int, bool, list[str], Path]:
    """The one run of ssh: its pid, whether it ignores Ctrl-C, its arguments and the directory of its local socket."""
    (line,) = calls.read_text('utf-8').splitlines()
    pid, ignores_ctrl_c, *arguments = json.loads(line)
    return pid, ignores_ctrl_c, arguments, Path(arguments[arguments.index('-L') + 1].split(':')[0]).parent


def _ended(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.fast
def test_a_service_on_this_host_is_reached_at_its_socket(driver: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """unix:///socket: every call goes over the socket, the Server header read as from any service, and never by
    way of the proxy the environment names."""
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:3128')
    with compute.Service(f'unix://{driver}') as service:
        assert service.attach('r1').status().state is RunState.RUNNING


@pytest.mark.fast
def test_a_cancel_the_service_refuses_is_raised_in_its_words(driver: Path) -> None:
    """403, a run another account submitted: refused in the service's words, never taken for a run that ended."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.Service(f'unix://{driver}').attach('r1').cancel()
    assert refused.value.messages == ('run r1 was submitted by alice: only alice, or the service account, may '
                                      'cancel it',)


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32', reason=_WINDOWS)
@pytest.mark.parametrize('url', ['http://cluster:7998', 'https://compute.example.org', 'cluster'])
def test_a_url_but_ssh_and_unix_is_refused_there_being_no_https_gateway(url: str) -> None:
    """http:// and https:// need an HTTPS gateway in front of the service, which there is not yet."""
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service(url)
    assert unavailable.value.messages == (
        f'{url} is not where the compute service is: it is reached over SSH, as ssh://[user@]host[:port][/socket] '
        '(the socket by default /run/slurm-driver/api.sock), or on its own host, as unix:///socket; http:// and '
        'https:// need an HTTPS gateway in front of the service, which there is not yet',)


@pytest.mark.fast
def test_ssh_forwards_a_private_socket_to_the_services_until_the_service_is_closed(
        ssh_calls: Path, driver: Path) -> None:
    """The user's own ssh, its prompts left to the terminal (no BatchMode), on a connection no ControlMaster holds,
    deaf to the Ctrl-C the terminal sends Python too: a socket in a directory only its owner may open, no TCP port,
    forwarded to the socket the URL names; closed, ssh has ended, the directory is gone, and a run of it is refused at
    once, never asked again for PATIENCE seconds as a service that cannot be reached is."""
    with compute.Service(f'ssh://alice@cluster:2222{driver}') as service:
        run = service.attach('r1')
        assert run.status().state is RunState.RUNNING
        pid, ignores_ctrl_c, arguments, directory = _ssh_run(ssh_calls)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert arguments == ['-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'StreamLocalBindUnlink=yes',
                         '-o', 'ControlPath=none', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
                         '-L', f'{directory / "api.sock"}:{driver}', 'ssh://alice@cluster:2222']
    assert ignores_ctrl_c
    assert _ended(pid) and not directory.exists()
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        run.status()
    assert unavailable.value.messages == (
        f"Service('ssh://alice@cluster:2222{driver}') is closed: a new one follows its runs again, as "
        f"Service('ssh://alice@cluster:2222{driver}').attach(run id)",)


@pytest.mark.fast
@pytest.mark.usefixtures('ssh_calls')
def test_a_forward_that_ended_fails_the_next_call_at_once_and_says_so(driver: Path) -> None:
    """ssh gone, a dropped connection or a laptop asleep: nothing reaches the service through it again, and the call
    says so with ssh's own words, without asking again for PATIENCE seconds."""
    with compute.Service(f'ssh://cluster{driver}') as service:
        forward = service._client.forward  # pylint: disable=protected-access
        assert forward is not None
        ssh, directory = forward
        ssh.kill()
        ssh.wait()
        (Path(directory) / 'ssh.log').write_text('Timeout, server cluster not responding.\n', 'utf-8')
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            service.attach('r1')
    assert unavailable.value.messages == (
        f'the compute service at ssh://cluster{driver} could not be reached: ssh ended, and its forward with it; a '
        'new Service forwards it again', 'Timeout, server cluster not responding.')
    assert not Path(directory).exists()


@pytest.mark.fast
def test_ctrl_c_before_ssh_forwards_ends_it_and_leaves_nothing(ssh_calls: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """At a password prompt, say: ssh, which ignores the Ctrl-C, would otherwise go on asking."""
    sleep = compute.time.sleep

    def interrupted(_: float) -> None:
        while not ssh_calls.exists() or not ssh_calls.read_text('utf-8'):  # until ssh is up, and asks
            sleep(0.01)
        raise KeyboardInterrupt
    monkeypatch.setattr(compute.time, 'sleep', interrupted)
    with pytest.raises(KeyboardInterrupt):
        compute.Service('ssh://slow')
    pid, _, _, directory = _ssh_run(ssh_calls)
    assert _ended(pid) and not directory.exists()


@pytest.mark.fast
def test_a_forward_a_process_never_closes_ends_as_the_process_does(ssh_calls: Path, driver: Path) -> None:
    """Python exiting closes every Service it left open."""
    subprocess.run([sys.executable, '-c', f"from sophios.compute import Service; Service('ssh://cluster{driver}')"],
                   check=True, timeout=60)
    pid, _, _, directory = _ssh_run(ssh_calls)
    assert _ended(pid) and not directory.exists()


@pytest.mark.fast
@pytest.mark.parametrize(('url', 'said'), [
    ('ssh://alice@nowhere', 'ssh: Could not resolve hostname nowhere: Name or service not known'),
    ('ssh://cluster', 'channel 2: open failed: connect failed: No such file or directory')])
def test_ssh_that_does_not_forward_fails_at_once_with_its_own_error_and_leaves_nothing(
        ssh_calls: Path, url: str, said: str) -> None:
    """An unreachable host, a refused key, or nothing on the socket the URL names, by default the service node's,
    /run/slurm-driver/api.sock: ssh's own words, no ssh left running and no directory left."""
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service(url)
    pid, _, arguments, directory = _ssh_run(ssh_calls)
    assert unavailable.value.messages == (
        f"ssh to {url.removeprefix('ssh://')} did not forward the compute service's socket /run/slurm-driver/api.sock:",
        said)
    assert arguments[-2] == f'{directory / "api.sock"}:/run/slurm-driver/api.sock'
    assert _ended(pid) and not directory.exists()


@pytest.mark.fast
@pytest.mark.skipif(sys.platform == 'win32', reason=_WINDOWS)
def test_an_ssh_that_cannot_be_run_is_unavailable_in_the_systems_words_and_leaves_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """None on PATH: cmp002 naming ssh, never a bare FileNotFoundError, and no directory left of its forward."""
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('ssh://cluster')
    assert unavailable.value.messages == ("ssh could not be run: [Errno 2] No such file or directory: 'ssh'",)
    assert not list(tmp_path.iterdir())
