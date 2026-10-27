"""The compute service's request, as Sophios builds it: `sophios.compute.build_request`, and its run, as a `Run`
follows it.

Fast and without a service: what the request holds, what Sophios refuses before sending, and what a `Run` makes of
the service's answers. What the service answers is `test_compute_service.py`'s, against the service itself.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import typing
from datetime import datetime, timezone
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
        compute.Service('http://127.0.0.1:1').submit(Workflow([step], 'count'))
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


def _serving(monkeypatch: pytest.MonkeyPatch,
             answers: dict[tuple[str, str], requests.Response | Exception | list[requests.Response | Exception]]
             ) -> list[tuple[str, str, object]]:
    """Have every HTTP call answered from `answers`, by method and path, a list's answers one per call in turn; each
    call is appended to the list returned, as its method, path and timeout."""
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
        compute.Service('http://127.0.0.1:1').submit(_workflow())


@pytest.mark.fast
def test_a_submission_lost_on_the_way_says_the_run_may_exist_and_how_to_follow_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Sent again until PATIENCE runs out; then the id it was sent under, and `attach`."""
    monkeypatch.setattr(compute, 'PATIENCE', 0.0)
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): requests.ConnectionError('gone')})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('http://127.0.0.1:1').submit(_workflow())
    could_not, may_exist = unavailable.value.messages
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
def test_a_refusal_is_each_problem_of_the_service_in_its_words_at_the_authored_input(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A pointer into the request is named as the author wrote it; Sophios adds no line of its own."""
    refusal = {'pointer': '/inputs/write~0model/location',
               'message': 'models/sam3.pt is relative: send basepath, the cluster directory it is relative to'}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('POST', 'runs'): _answer(400, {'errors': [refusal]})})
    with pytest.raises(compute.RequestRefused) as refused:
        compute.Service('http://127.0.0.1:1').submit(_workflow())
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
@pytest.mark.parametrize('state', [RunState.FAILED, RunState.CANCELLED])
def test_wait_returns_a_run_that_did_not_complete_as_its_status_and_raises_nothing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: RunState) -> None:
    """As the local handle's `wait`: the run's status however it ended, with the service's reason, printed once."""
    ended = {'id': 'r1', 'state': state.value, 'reason': 'Slurm will never start the leader (PartitionTimeLimit)'}
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _answer(200, ended)})
    status = compute.Service('http://127.0.0.1:1').attach('r1').wait()
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
    run = compute.Service('http://127.0.0.1:1').attach('r1')
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
    assert compute.Service('http://127.0.0.1:1').attach('r1').logs() == ''


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
def test_anything_but_the_service_answering_is_unavailable_and_names_the_tunnel(
        monkeypatch: pytest.MonkeyPatch, page: requests.Response) -> None:
    """The service names itself in every answer's Server header: a page from anything else, a 404 or a 200, is
    neither a refusal nor a run nor a log, for submit, status and logs alike."""
    tunnel = ('http://127.0.0.1:1 answered, but not as the compute service: check that your tunnel reaches its socket',)
    _serving(monkeypatch, {('GET', 'openapi.json'): page})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('http://127.0.0.1:1').submit(_workflow())
    assert unavailable.value.messages == tunnel
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): [_run('RUNNING'), page],
                           ('GET', 'runs/r1/logs/leader.log'): page})
    run = compute.Service('http://127.0.0.1:1').attach('r1')
    for asked in (run.status, run.logs):
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            asked()
        assert unavailable.value.messages == tunnel


@pytest.mark.fast
def test_an_answer_of_the_service_that_is_not_json_is_its_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every answer of the service but a log is JSON; one that is not is the service's failure, not a crash."""
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _text('<html>oops</html>')})
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('http://127.0.0.1:1').attach('r1')
    assert unavailable.value.messages == ('the compute service at http://127.0.0.1:1 answered 200, not in JSON',)


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
    assert compute.Service('http://127.0.0.1:1').attach('r1').id == 'r1'
    assert pauses == [5.0, 5.0]


@pytest.mark.fast
def test_an_answer_cut_short_is_asked_for_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service cuts the connection when it fails after the headers, as a log read may, and so does a tunnel
    that drops: the call is made again, as when the service cannot be reached."""
    pauses: list[float] = []
    monkeypatch.setattr(compute.time, 'sleep', pauses.append)
    cut = requests.exceptions.ChunkedEncodingError('cut')
    _serving(monkeypatch, {('GET', 'openapi.json'): _CONTRACT, ('GET', 'runs/r1'): _run('RUNNING'),
                           ('GET', 'runs/r1/logs/leader.log'): [cut, _text('x')]})
    assert compute.Service('http://127.0.0.1:1').attach('r1').logs() == 'x'
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
    ([_run('FAILED', 'the workflow failed; see leader.log')], False),
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
    run = compute.Service('http://127.0.0.1:1').attach('r1')
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
        compute.Service('http://127.0.0.1:1').attach('r1').cancel()


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
