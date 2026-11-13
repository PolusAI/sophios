# pylint: disable=too-many-lines
"""The compute service's request as Sophios builds it (`sophios.compute.build_request`) and a `Run`'s handling of its
answers, fast and without a service; test_compute_service.py runs against the service itself."""
import dataclasses
import inspect
import json
import os
import re
import socketserver
import stat
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
from hypothesis import example, given, strategies as st
from jsonschema import Draft202012Validator

from sophios import compute
from sophios.compute import RunConfig
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.run_status import RunState, RunStatus
from sophios.wic_types import Yaml

from .budgets import budget
from .cases import cases, match
from .helpers import REPO_ROOT, live_workflow, python_process

DOCS = REPO_ROOT / 'docs'


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
@budget(50)
@given(*[st.lists(st.sampled_from('/.~$: ') | st.text(max_size=3), max_size=6).map(''.join)] * 2, st.booleans(),
       st.booleans())
@example('results/run7', 'https://example.org/sam3.pt', False, True)
@example('/projects/lab/out', 'models/sam3.pt', False, False)
@example('~/results', '$HOME/sam3.pt', False, False)
@example('results/run7', 'models/sam3.pt', True, False)
def test_the_request_carries_the_compiled_workflow_and_every_path_as_written(out: str, model: str, on_workflow: bool,
                                                                             basepath: bool) -> None:
    """One inline document, its inputs, and `basepath` only when given; nothing is joined or checked, `~` and
    `$HOME` included: the service is the one authority on paths. A value bound on a workflow input is the job's value
    of it (docs/inputs-and-values.md), so the service is sent it as written, as it is sent a step's."""
    workflow = _workflow(out, model)
    if on_workflow:
        step = workflow.steps[0]
        step.inputs.out, step.inputs.model = workflow.inputs.out, workflow.inputs.model
        workflow.inputs.out, workflow.inputs.model = out, model
    config = RunConfig('results').basepath('/projects/lab') if basepath else None
    request = compute.build_request(workflow, config)
    assert set(request) == {'id', 'workflow', 'inputs', *(['basepath', 'workflowOutDir'] if basepath else [])}
    assert request.get('basepath', '/projects/lab') == '/projects/lab'
    assert request['workflow']['class'] == 'Workflow' and request['workflow']['cwlVersion'] == 'v1.2'
    assert all(isinstance(step['run'], dict) for step in request['workflow']['steps'])
    named = '' if on_workflow else 'write~'
    assert request['inputs'] == {f'{named}out': {'class': 'Directory', 'location': out},
                                 f'{named}model': {'class': 'File', 'location': model}}


#: The service's id rule, and the fit of each name outside it a case pins, by hand from the rule.
_ID: typing.Final = Draft202012Validator(compute.SCHEMA['$defs']['Id'])
_FITTED: typing.Final = {'a b/c': 'a-b-c', '...': 'run'}
_NOW: typing.Final = datetime(2026, 10, 26, 14, 22, 5, tzinfo=timezone.utc)
#: Any text, and text of the rule's own kinds of character, at every length a cut may meet.
_NAMES: typing.Final = st.one_of(st.integers(0, 300).flatmap(lambda size: st.text(min_size=size, max_size=size)),
                                 st.integers(0, 130).flatmap(lambda size: st.text('aZ09._-', min_size=size,
                                                                                  max_size=size)))


@pytest.mark.fast
@budget(100)
@given(name=_NAMES, now=st.datetimes(datetime(1000, 1, 1), timezones=st.just(timezone.utc)))
@example(name='segment', now=_NOW)
@example(name='a b/c', now=_NOW)
@example(name='...', now=_NOW)
@example(name='.hidden', now=_NOW)
@example(name='__x', now=_NOW)
@example(name='é' * 5, now=_NOW)
@example(name='', now=_NOW)
@example(name='x' * 300, now=_NOW)
@example(name='-', now=_NOW)
@example(name='ok.name_1-2', now=_NOW)
def test_a_run_id_is_the_workflow_name_the_time_and_four_hex_digits_within_the_services_rule(
        name: str, now: datetime) -> None:
    """`<name>-<UTC yyyymmddThhmmss>-<4 hex>` within the service's id rule: a name inside it that leaves the 21
    characters the rest takes is kept as it is, and any other is fitted to it; the 4 hex differ between two runs."""
    made = compute.new_run_id(name, now)
    prefix, stamp, suffix = made.rsplit('-', 2)
    assert _ID.is_valid(made) and re.fullmatch('[0-9a-f]{4}', suffix)
    assert stamp == re.sub('[-:]', '', now.isoformat()[:19])
    if name in _FITTED or _ID.is_valid(name) and len(name) <= 107:
        assert prefix == _FITTED.get(name, name)
    assert len({compute.new_run_id(name, now) for _ in range(8)}) > 1


def _camel(name: str) -> str:
    first, *rest = name.split('_')
    return first + ''.join(word.capitalize() for word in rest)


#: A valid value of every field RunConfig's setters set, as the request holds it.
_VALID: typing.Final[dict[str, typing.Any]] = {
    'basepath': '/projects/lab', 'leader': {'partition': 'normal', 'time': '1-00:00:00', 'memory': '8G'},
    'steps': {'partition': 'normal', 'gpuPartition': 'gpu', 'time': '10'}, 'logLevel': 'DEBUG'}


def _setters() -> dict[str, inspect.Signature]:
    """RunConfig's setters, each named as the field of the request it sets: every public attribute of it, so that one
    which is not a setter fails `_set_every_field`."""
    return {name: inspect.signature(getattr(RunConfig, name)) for name in vars(RunConfig) if not name.startswith('_')}


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
def test_a_request_is_refused_or_sent_as_the_vendored_schema_says() -> None:
    """The vendored schema is a JSON Schema 2020-12 document that takes its own example. Every field it refuses is a
    line in its words, all at once, each where it is and what the field is; a workflow with a real-time analysis is
    refused before sending, naming the analysis, rather than dropped. The constructor and every setter of RunConfig,
    each given a valid value, reach exactly the schema's top-level properties but id, workflow and inputs, each under
    its own name; the keywords of `leader` and `steps` are the properties of its Leader and Steps, camelCased; the
    folder is left out of the request without one, for the run's own folder, so `basepath`, which requires it, is then
    refused before sending, saying what to do; and no setter touches where the outputs go. Called again, a setter
    replaces what it is passed again and keeps the rest; what no setter sets is left out of the request, for the
    service's own settings. The vendored schema and RunConfig cannot drift apart."""
    Draft202012Validator.check_schema(compute.SCHEMA)
    assert Draft202012Validator(compute.SCHEMA).is_valid(compute.SCHEMA['examples'][0])
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
    with pytest.raises(compute.RequestRefused, match="real-time analysis 'touch', which the compute service never"):
        compute.build_request(live_workflow('live'))
    properties, defs = compute.SCHEMA['properties'], compute.SCHEMA['$defs']
    workflow = {'class': 'Workflow', 'cwlVersion': 'v1.2'}
    core = {'id': 'r1', 'workflow': workflow, 'inputs': {}}
    named = compute.request_for('r1', workflow, {}, _set_every_field(RunConfig(workflow_out_dir='results')))
    assert set(named) == set(properties)
    assert named == core | {'workflowOutDir': 'results'} | _VALID
    with pytest.raises(compute.RequestRefused) as refused:
        compute.request_for('r1', workflow, {}, _set_every_field(RunConfig()))
    assert refused.value.messages == (
        'workflowOutDir: required with basepath: send workflowOutDir, or send no basepath',)
    assert compute.request_for('r1', workflow, {}, RunConfig()) == compute.request_for('r1', workflow, {}) == core
    for name in ('leader', 'steps'):
        keywords = {_camel(parameter.name) for parameter in _setters()[name].parameters.values()
                    if parameter.kind is parameter.KEYWORD_ONLY}
        assert keywords == set(defs[name.capitalize()]['properties']), name
    levels = typing.get_args(typing.get_type_hints(RunConfig.log_level)['level'])
    assert set(levels) == set(properties['logLevel']['enum'])
    config = RunConfig('results').basepath('/shared/lab').leader(time='2:00:00', memory='4G')
    config.steps(time='30:00').leader(memory='8G', partition='normal').basepath('/shared/lab2')
    request = compute.request_for('r1', {'class': 'Workflow', 'cwlVersion': 'v1.2'}, {}, config)
    assert request == {'id': 'r1', 'workflow': {'class': 'Workflow', 'cwlVersion': 'v1.2'}, 'inputs': {},
                       'workflowOutDir': 'results',
                       'basepath': '/shared/lab2', 'leader': {'time': '2:00:00', 'memory': '8G', 'partition': 'normal'},
                       'steps': {'time': '30:00'}}


#: The header the service names itself in, on every answer.
_SERVICE: typing.Final = {'Server': 'slurm-driver/3.0.0'}


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


#: Why a test that makes a Service skips on Windows itself, where Service refuses every URL. The fake service, its
#: socket and the stand-in for ssh each skip so, and so does a test that makes one without them.
_WINDOWS: typing.Final = 'the compute client needs macOS, Linux or WSL'


def _serving(monkeypatch: pytest.MonkeyPatch,
             answers: dict[tuple[str, ...], requests.Response | BaseException | list[requests.Response | BaseException]]
             ) -> list[tuple[str, str, typing.Any]]:
    """Have every HTTP call answered from `answers`, by method and path, a list's answers one per call in turn; each
    call is appended to the list returned, as its method, path and timeout."""
    if sys.platform == 'win32':
        pytest.skip(_WINDOWS)
    calls: list[tuple[str, str, typing.Any]] = []

    def request(_: requests.Session, method: str, url: str, **options: object) -> requests.Response:
        path = url.split('://', 1)[1].split('/', 1)[1]
        calls.append((method, path, options.get('timeout')))
        found = answers[(method, path)]
        if isinstance(found, list):
            found = found.pop(0)
        if isinstance(found, BaseException):
            raise found
        return found
    monkeypatch.setattr(requests.Session, 'request', request)
    return calls


#: What a case's calls are answered when it says nothing of them: the service's contract, and the run r1 RUNNING.
_SPOKEN: typing.Final = {'GET openapi.json': {'json': {'openapi': '3.1.0', 'info': {'version': '3.0.0'}}},
                         'GET runs/r1': {'json': {'id': 'r1', 'state': 'RUNNING'}}}


def _given(answer: Yaml) -> requests.Response | BaseException:
    """An answer as a case writes it: an exception it `raise`s, or a `status` (200 by default) with the service's
    `json` and `headers`, a log's `text`, or the `page` of something else, which names no service."""
    if 'raise' in answer:
        name = answer['raise']
        return KeyboardInterrupt() if name == 'KeyboardInterrupt' else getattr(requests.exceptions, name)(name)
    status = answer.get('status', 200)
    if 'json' in answer:
        return _answer(status, answer['json'], answer.get('headers'))
    return _raw(status, answer['text'], _SERVICE) if 'text' in answer else _raw(status, answer['page'], {})


def _seen(value: object) -> object:
    """What a verb returned, as a case writes it: a run as its id, a status as its fields, states by name and times in
    ISO 8601."""
    if isinstance(value, RunStatus):
        return json.loads(json.dumps(dataclasses.asdict(value), default=lambda field: field.name
                                     if isinstance(field, RunState) else field.isoformat()))
    return value.id if isinstance(value, compute.Run) else value


@pytest.mark.fast
def test_a_run_makes_of_the_services_answers_what_its_contract_says(monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """Each case of data/compute-service: each call answered from `answers` by 'METHOD path', over `_SPOKEN`, a list's
    answers one per call in turn, so a call past them or answered nothing fails the case; then `raises`
    ({exception: [message, ...]}, nothing by default), and those it gives of `returns` (what each verb returned),
    `stdout`, `stderr`, `pauses` and `calls` ([method, path, connect and read timeouts]). A key the test does not
    read, or a case that expects none of these, fails. cmp001 and cmp002 belong to sophios.compute: a ComputeError is
    not a SophiosError, no SophiosErrorCode is one of them, docs/compute.md documents exactly them, and the message is
    every line under its code. A workflow that does not compile is a SophiosError with Sophios's own code, before the
    service is asked anything: the one failure of `submit` that is not a ComputeError."""
    expected = {'raises', 'returns', 'stdout', 'stderr', 'pauses', 'calls'}
    for case in (param.values[0] for param in cases('compute-service', 'do', 'answers', 'raises', 'returns', 'stdout',
                                                    'stderr', 'pauses', 'calls', 'patience')):
        assert case.keys() - {'id', 'why', 'do', 'patience', 'answers'} <= expected and case.keys() & expected, case
        with monkeypatch.context() as patches:
            seen = _answered(patches, capsys, case)
        for key in case.keys() & seen.keys() | {'raises'}:
            assert match(case.get(key), seen[key]), (case['id'], key, seen[key])

    errors = compute.ComputeError.__subclasses__()
    assert [(error, error.code) for error in errors] == [
        (compute.RequestRefused, 'cmp001'), (compute.ServiceUnavailable, 'cmp002')]
    assert not issubclass(compute.ComputeError, SophiosError)
    assert not {error.code for error in errors} & {str(code) for code in SophiosErrorCode}
    documented = set(re.findall(r'\bcmp\d{3}\b', (DOCS / 'compute.md').read_text('utf-8')))
    assert documented == {error.code for error in errors}
    refused = compute.RequestRefused('one', 'two')
    assert (refused.messages, str(refused)) == (('one', 'two'), '[cmp001] one\n[cmp001] two')

    calls = _serving(monkeypatch, {})
    step = Step(CommandLineTool('count', Inputs(n=Input(cwl.int, position=1)), Outputs()).base_command('echo'))
    step.inputs.n = 'not an int'
    with pytest.raises(SophiosError) as raised:
        compute.Service('unix:///api.sock').submit(Workflow([step], 'count'))
    assert [diagnostic.code for diagnostic in raised.value.diagnostics] == [SophiosErrorCode.LITERAL_TYPE_MISMATCH]
    assert not calls


def _answered(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: Yaml) -> dict[str, typing.Any]:
    """What a run did with the answers of `case`: what each verb returned or the run raised, what it printed, the
    pauses it made and the calls."""
    pauses: list[float] = []
    monkeypatch.setattr(compute.time, 'sleep', pauses.append)
    monkeypatch.setattr(compute, 'PATIENCE', float(case.get('patience', compute.PATIENCE)))
    calls = _serving(monkeypatch, {tuple(call.split(' ')): [_given(each) for each in answer] if isinstance(answer, list)
                                   else _given(answer) for call, answer in {**_SPOKEN, **case['answers']}.items()})
    returned: list[object] = []
    raised: dict[str, list[str]] | None = None
    first, *then = case['do']
    try:
        run = getattr(compute.Service('unix:///api.sock'), first)(_workflow() if first == 'submit' else 'r1')
        returned = [run, *(getattr(run, verb)() for verb in then)]
    except (compute.ComputeError, TimeoutError, KeyboardInterrupt) as error:
        raised = {type(error).__name__: list(getattr(error, 'messages', [str(error)]))}
    printed = capsys.readouterr()
    return {'returns': [_seen(value) for value in returned], 'stdout': printed.out, 'stderr': printed.err,
            'raises': raised, 'pauses': pauses, 'calls': [[method, path, *limits] for method, path, limits in calls]}


class _Driver(BaseHTTPRequestHandler):
    """The service, as far as these tests ask it, on a Unix socket: its contract, the run r1, and a cancel of r1
    refused, as the service refuses one by an account that did not submit the run."""

    server_version, sys_version = 'slurm-driver/3.0.0', ''
    answers: typing.ClassVar = {
        ('GET', '/openapi.json'): (200, {'openapi': '3.1.0', 'info': {'version': '3.0.0'}}),
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
@pytest.mark.skipif(sys.platform == 'win32', reason=_WINDOWS)
# pylint: disable-next=too-many-locals,too-many-statements
def test_the_service_is_reached_at_its_socket_or_through_an_ssh_forward_that_never_outlives_it(
        driver: Path, ssh_calls: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """unix:///socket: every call goes over the socket, the Server header read as from any service, and never by way of
    the proxy the environment names; a cancel the service refuses (403, a run another account submitted) is raised in
    its words, never taken for a run that ended. http:// and https:// need an HTTPS gateway in front of the service,
    which there is not yet. ssh:// is the user's own ssh, its prompts left to the terminal (no BatchMode), on a
    connection no ControlMaster holds, deaf to the Ctrl-C the terminal sends Python too: a socket in a directory only
    its owner may open, no TCP port, forwarded to the socket the URL names. Closed, ssh has ended, the directory is
    gone, and a run of it is refused at once, never asked again for PATIENCE seconds as a service that cannot be
    reached is; so is a call after ssh ended, a dropped connection or a laptop asleep, in ssh's own words. Ctrl-C
    before ssh forwards, at a password prompt say, ends ssh, which ignores it; Python exiting closes every Service it
    left open. An unreachable host, a refused key, or nothing on the socket the URL names, by default the service
    node's, is ssh's own words, no ssh left running and no directory left; and with no ssh on PATH, cmp002 names ssh,
    never a bare FileNotFoundError."""
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:3128')
    with compute.Service(f'unix://{driver}') as service:
        assert service.attach('r1').status().state is RunState.RUNNING
        with pytest.raises(compute.RequestRefused) as refused:
            service.attach('r1').cancel()
    assert refused.value.messages == ('run r1 was submitted by alice: only alice, or the service account, may '
                                      'cancel it',)
    for url in ('http://cluster:7998', 'https://compute.example.org', 'cluster'):
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            compute.Service(url)
        assert unavailable.value.messages == (
            f'{url} is not where the compute service is: it is reached over SSH, as ssh://[user@]host[:port][/socket] '
            '(the socket by default /run/slurm-driver/api.sock), or on its own host, as unix:///socket; http:// and '
            'https:// need an HTTPS gateway in front of the service, which there is not yet',)

    with compute.Service(f'ssh://alice@cluster:2222{driver}') as service:
        run = service.attach('r1')
        assert run.status().state is RunState.RUNNING
        pid, ignores_ctrl_c, arguments, directory = _ssh_run(ssh_calls)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert arguments == ['-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'StreamLocalBindUnlink=yes',
                         '-o', 'ControlPath=none', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
                         '-L', f'{directory / "api.sock"}:{driver}', 'ssh://alice@cluster:2222']
    assert ignores_ctrl_c and _ended(pid) and not directory.exists()
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        run.status()
    assert unavailable.value.messages == (
        f"Service('ssh://alice@cluster:2222{driver}') is closed: a new one follows its runs again, as "
        f"Service('ssh://alice@cluster:2222{driver}').attach(run id)",)

    ssh_calls.unlink()
    with compute.Service(f'ssh://cluster{driver}') as service:
        forward = service._client.forward  # pylint: disable=protected-access
        assert forward is not None
        ssh, held = forward
        ssh.kill()
        ssh.wait()
        (Path(held) / 'ssh.log').write_text('Timeout, server cluster not responding.\n', 'utf-8')
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            service.attach('r1')
    assert unavailable.value.messages == (
        f'the compute service at ssh://cluster{driver} could not be reached: ssh ended, and its forward with it; a '
        'new Service forwards it again', 'Timeout, server cluster not responding.')
    assert not Path(held).exists()

    ssh_calls.unlink()
    sleep = compute.time.sleep

    def interrupted(_: float) -> None:
        while not ssh_calls.exists() or not ssh_calls.read_text('utf-8'):  # until ssh is up, and asks
            sleep(0.01)
        raise KeyboardInterrupt
    with monkeypatch.context() as patches:
        patches.setattr(compute.time, 'sleep', interrupted)
        with pytest.raises(KeyboardInterrupt):
            compute.Service('ssh://slow')
    pid, _, _, directory = _ssh_run(ssh_calls)
    assert _ended(pid) and not directory.exists()

    ssh_calls.unlink()
    python_process('-c', f"from sophios.compute import Service; Service('ssh://cluster{driver}')",
                   check=True, timeout=60)
    pid, _, _, directory = _ssh_run(ssh_calls)
    assert _ended(pid) and not directory.exists()

    for url, said in (('ssh://alice@nowhere', 'ssh: Could not resolve hostname nowhere: Name or service not known'),
                      ('ssh://cluster', 'channel 2: open failed: connect failed: No such file or directory')):
        ssh_calls.unlink()
        with pytest.raises(compute.ServiceUnavailable) as unavailable:
            compute.Service(url)
        pid, _, arguments, directory = _ssh_run(ssh_calls)
        assert unavailable.value.messages == (
            f"ssh to {url.removeprefix('ssh://')} did not forward the compute service's socket "
            '/run/slurm-driver/api.sock:', said)
        assert arguments[-2] == f'{directory / "api.sock"}:/run/slurm-driver/api.sock'
        assert _ended(pid) and not directory.exists()

    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'temp'))
    (tmp_path / 'temp').mkdir()
    with pytest.raises(compute.ServiceUnavailable) as unavailable:
        compute.Service('ssh://cluster')
    assert unavailable.value.messages == ("ssh could not be run: [Errno 2] No such file or directory: 'ssh'",)
    assert not list((tmp_path / 'temp').iterdir())
