"""Run a workflow on a Slurm cluster, through the cluster's compute service (the slurm driver).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it. It takes a
`Workflow` built with that API, compiles it, and sends it with its inputs as one request:

    from sophios.compute import RunConfig, Service

    service = Service('ssh://login.cluster.example.org')  # your own ssh, to the service's socket there
    config = RunConfig('/projects/lab/results/run7').basepath('/projects/lab/data').leader(time='2:00:00')
    run = service.submit(workflow, config)
    status = run.wait()

A `RunConfig` is everything of the request but the workflow and its inputs: the folder it is made with is where
the outputs go, and each of its setters sets a field more; without one, the run takes the service's settings.

A `Run` is followed as the local `RunHandle` of `Workflow.start()` is: `status`, `wait`, `outputs`, `logs` and
`cancel`. `Service.attach` follows a run from any session or machine; the run goes on in Slurm whatever happens to
the session.

Every path goes as written, and the service, not Sophios, reads it on the cluster and refuses what it cannot read
there, `~` and `$VARIABLE`s included. A path a tool reads is a path on the cluster, absolute or relative to
`basepath`, or an http(s) URL. An output folder (a Directory input a tool writes into and returns) given as a name,
such as `segmentation`, is made in the run, never read against `basepath`, and its result lands in the outputs'
folder under that name; one given as an absolute path is that folder. The outputs' folder is the one
`RunConfig(folder)` names (output mode `workflowOutDir`, the supported way: the outputs are copied into it), or the
run's own folder on the cluster without a config; `RunConfig.in_place()` (output mode `workflowInPlace`), an advanced
choice, has each tool write an output folder with an absolute path there, once, without a copy. The request's shape
is `run-request.schema.json`, a copy of the service's own: Sophios checks every request against it before sending,
and the service again on arrival.
"""
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import weakref
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal
from urllib.parse import quote, urlsplit

import requests
import urllib3
from jsonschema import Draft202012Validator

from .api.python._inference import Inference
from .api.python._workflow_runtime import service_document
from .run_status import RunState, RunStatus, StepStatus
from .wic_types import Json, Tools

if TYPE_CHECKING:
    from .api.python.workflow import Workflow

#: The major version of the service's contract (its openapi.json's `info.version`) this Sophios speaks.
CONTRACT: Final = 2
#: How often a waiting run's status is read, in seconds: the service asks clients to wait at least 10 s.
POLL: Final = 10.0
#: How long a call is made again while the service cannot be reached or is busy, and `Run.cancel` waits for the run
#: to end, in seconds.
PATIENCE: Final = 300.0
#: How often a call is made again meanwhile, in seconds: the Retry-After the service sends with every 503.
_RETRY: Final = 5.0
#: How long a call may take to connect, and to answer: a submission asks Slurm several times, 30 s each at most,
#: so it gets longer than the other calls, lest a slow but healthy one be sent again while it is still going on.
_TIMEOUT: Final = (10.0, 60.0)
_SUBMIT_TIMEOUT: Final = (10.0, 300.0)
_ENDED: Final = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)
#: The socket the service listens on on a service node, as its README sets it up: the one `ssh://host` reaches.
SOCKET: Final = '/run/slurm-driver/api.sock'
#: The host every call names, in its URL and its Host header: the calls go over the socket alone.
_HOST: Final = 'slurm-driver'

#: The schema every request is checked against: the service's own, as Sophios was released with.
SCHEMA: Final[Json] = json.loads(resources.files('sophios').joinpath('run-request.schema.json').read_text('utf-8'))
_VALIDATOR: Final = Draft202012Validator(SCHEMA)

#: What a run id may not hold, made "-": the service's id rule is letters, digits, ".", "_" and "-".
_NOT_IN_ID: Final = re.compile(r'[^A-Za-z0-9._-]')


class ComputeError(Exception):
    """A call to the compute service that could not do what was asked: `code`, which each subclass names, says
    which, and `messages` holds every problem, a line each.

    The codes are this module's own, `cmp001` and `cmp002` (docs/compute.md), and not Sophios's error codes: a
    ComputeError is never a `SophiosError`.
    """

    code: ClassVar[str]

    def __init__(self, *messages: str) -> None:
        super().__init__('\n'.join(f'[{self.code}] {message}' for message in messages))
        self.messages = messages


class RequestRefused(ComputeError):
    """A request Sophios refused before sending it, or the service refused; every problem is a line."""

    code = 'cmp001'


class ServiceUnavailable(ComputeError):
    """Sophios runs on Windows itself, the URL is not one the service is reached at, ssh failed before it forwarded
    the socket or has ended since, the `Service` is closed, or the service could not be reached, stayed busy, failed,
    speaks another contract, or something else answered in its place."""

    code = 'cmp002'


class RunConfig:
    """Everything of a request but `id`, `workflow` and `inputs`: made with where the outputs go, which nothing
    changes after, and given the rest by setters that set and return it, so the calls chain.

        RunConfig('/shared/lab/results').basepath('/shared/lab').leader(time='2:00:00', memory='4G')

    Each setter sets just what it is passed, and leaves the rest as it was; what no setter sets is left out of the
    request, so the service's own settings apply. The values are strings in Slurm's own forms, which the schema
    checks when the request is built. Without any `RunConfig`, the outputs go into the run's own folder on the
    cluster (output mode `serviceOutDir`), with every setting the service's.

    Args:
        workflow_out_dir: The folder on the cluster the outputs are copied into, absolute or relative to `basepath`,
            created when missing: output mode `workflowOutDir`, the supported way.
    """

    def __init__(self, workflow_out_dir: str) -> None:
        self._fields: Json = {'outputMode': 'workflowOutDir', 'workflowOutDir': workflow_out_dir}

    @classmethod
    def in_place(cls) -> 'RunConfig':
        """Output mode `workflowInPlace`, an advanced choice the service warns of: each output folder given as an
        absolute path is written there, once, without a copy."""
        config = cls.__new__(cls)
        config._fields = {'outputMode': 'workflowInPlace'}
        return config

    def basepath(self, path: str) -> 'RunConfig':
        """The directory on the cluster that the relative paths of what the tools read are relative to; an output
        folder given as a name is made in the run instead."""
        self._fields['basepath'] = path
        return self

    def leader(self, *, partition: str | None = None,
               time: str | None = None,  # pylint: disable=redefined-outer-name  # the request's name
               memory: str | None = None) -> 'RunConfig':
        """The Toil leader's Slurm job, which lasts as long as the run: its partition, time limit and memory."""
        return self._merge('leader', partition=partition, time=time, memory=memory)

    def steps(self, *, partition: str | None = None, gpu_partition: str | None = None,
              time: str | None = None) -> 'RunConfig':  # pylint: disable=redefined-outer-name  # the request's name
        """Where the step jobs go, those that ask for a GPU to `gpu_partition`, and how long each may run."""
        return self._merge('steps', partition=partition, gpu_partition=gpu_partition, time=time)

    def log_level(self, level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR']) -> 'RunConfig':
        """Toil's log level."""
        self._fields['logLevel'] = level
        return self

    def _merge(self, field: str, **values: str | None) -> 'RunConfig':
        """Set each of `values` that is given in the object `field`, under the request's name for it."""
        given = {_key(name): value for name, value in values.items() if value is not None}
        self._fields[field] = {**self._fields.get(field, {}), **given}
        return self


def new_run_id(name: str, now: datetime | None = None) -> str:
    """A new id for a run of the workflow `name`: `<name>-<UTC yyyymmddThhmmss>-<4 hex>`, fitted to the id rule."""
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%S')
    # The suffix takes 21 of the 128 characters the rule allows; an id starts with a letter or a digit.
    base = _NOT_IN_ID.sub('-', name).lstrip('._-')[:107] or 'run'
    return f'{base}-{stamp}-{secrets.token_hex(2)}'


def build_request(workflow: 'Workflow', config: RunConfig | None = None, *, tool_registry: Tools | None = None,
                  inference: Inference = Inference()) -> Json:
    """The request `POST /runs` takes to run `workflow` as `config` says, as `Service.submit` sends it.

    Args:
        workflow: The workflow, compiled here into one document with its inputs.
        config: Everything of the request but the workflow and its inputs; None for the service's settings, the
            outputs in the run's own folder.
        tool_registry: Tools the workflow's steps may name, besides those it was built with.
        inference: How unbound inputs are connected.

    Raises:
        SophiosError: The workflow does not compile, with Sophios's own codes.
        RequestRefused: A real-time analysis, or a value the schema does not take; every one is a line.
    """
    document = service_document(workflow, tool_registry=tool_registry, inference=inference)
    if document.realtime:
        raise RequestRefused(*(f'{declaration.document} declares the real-time analysis {declaration.analysis!r}, '
                               'which the compute service never runs; remove it, or run the workflow locally'
                               for declaration in document.realtime))
    return request_for(new_run_id(workflow.process_name), document.cwl, document.job_inputs, config)


def request_for(run_id: str, cwl: Json, job_inputs: Json, config: RunConfig | None = None) -> Json:
    """The request to run the compiled document `cwl` with `job_inputs` as the run `run_id`, as `config` says: what
    `build_request` builds once it has compiled the workflow.

    Raises:
        RequestRefused: As `build_request` raises it, but for a real-time analysis.
    """
    given = config._fields if config is not None else {}  # pylint: disable=protected-access
    request: Json = {'id': run_id, 'workflow': cwl, 'inputs': job_inputs, **given}
    problems = [_schema_problem(error) for error in _VALIDATOR.iter_errors(request)]
    if problems:
        raise RequestRefused(*problems)
    return request


def _key(name: str) -> str:
    """The request's name for the keyword `name` of a `RunConfig` setter: the same words, in camelCase."""
    first, *rest = name.split('_')
    return first + ''.join(word.capitalize() for word in rest)


def _schema_problem(error: Any) -> str:
    """One line for a value the schema refuses: where it is, and what the field is, in the schema's words."""
    where = _named(''.join('/' + str(part).replace('~', '~0').replace('/', '~1') for part in error.absolute_path))
    what = error.schema.get('description') if isinstance(error.schema, dict) else None
    if what and error.validator in ('type', 'enum', 'pattern'):
        return f'{where}: {error.instance!r} is not {what}'
    return f'{where}: {error.message}'


def _named(pointer: str) -> str:
    """Where `pointer` (RFC 6901, into a request) is, as the author wrote it: an input by its name, or a field."""
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')[1:]]
    if len(parts) > 1 and parts[0] == 'inputs':
        return f'input {parts[1]}'
    return '.'.join(parts) or 'the request'


def _forward(url: str) -> tuple['subprocess.Popen[bytes]', str]:
    """Forward `api.sock`, in a new private directory, to the service's socket on the host of `url`,
    `ssh://[user@]host[:port][/socket]`, with the user's own `ssh`, and return it and the directory once what listens
    on that socket answers through it. Its prompts, a password's or a second factor's, go to the terminal, as they do
    for any ssh. It makes its own connection, never one a ControlMaster shares, whose master would keep the forward
    open once this ssh has ended; and it ignores Ctrl-C, which the terminal sends Python and ssh alike, so the forward
    outlives a Ctrl-C in the session.

    Raises:
        ServiceUnavailable: ssh ended first, or nothing answered through it, with ssh's own error.
    """
    parts = urlsplit(url)
    remote = parts.path or SOCKET
    directory = tempfile.mkdtemp(prefix='sophios-')  # only its owner may open it, and the socket in it
    local = os.path.join(directory, 'api.sock')
    with open(os.path.join(directory, 'ssh.log'), 'wb') as log:
        ssh = subprocess.Popen(  # pylint: disable=consider-using-with,subprocess-popen-preexec-fn  # see above
            ['ssh', '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'StreamLocalBindUnlink=yes', '-o', 'ControlPath=none',
             '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
             '-L', f'{local}:{remote}', f'ssh://{parts.netloc}'], stdin=subprocess.DEVNULL, stderr=log,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    try:
        while ssh.poll() is None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(local)
                except (FileNotFoundError, ConnectionRefusedError):  # ssh does not listen yet
                    time.sleep(0.1)
                    continue
                probe.settimeout(_TIMEOUT[1])
                try:
                    probe.sendall(b'HEAD / HTTP/1.0\r\n\r\n')
                    if probe.recv(1):
                        return ssh, directory
                except OSError:
                    pass
                break  # ssh listens, but nothing answered on the service's socket through it
        ssh.terminate()
        ssh.wait()
        raise ServiceUnavailable(f'ssh to {parts.netloc} did not forward the compute service\'s socket {remote}:',
                                 *_said_by(directory))
    except BaseException:  # Ctrl-C at a prompt too: ssh, which ignores it, would go on asking
        _unforward(ssh, directory)
        raise


def _said_by(directory: str) -> list[str]:
    """What the ssh forwarding to `directory` wrote on its standard error, a line each."""
    with open(os.path.join(directory, 'ssh.log'), encoding='utf-8', errors='replace') as log:
        return log.read().splitlines()


def _unforward(ssh: 'subprocess.Popen[bytes]', directory: str, http: requests.Session | None = None) -> None:
    """End `ssh`'s forward and its directory, once `http`'s connections through it are closed."""
    if http is not None:
        http.close()
    ssh.terminate()
    ssh.wait()
    shutil.rmtree(directory)


class _SocketConnection(urllib3.connection.HTTPConnection):
    """An HTTP connection over the Unix socket `path`, whatever host it is made for."""

    def __init__(self, *args: Any, path: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.path = path

    def _new_conn(self) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.path)
        return connection


class _SocketPool(urllib3.HTTPConnectionPool):
    """A pool of `_SocketConnection`s, each made with the pool's `path`."""

    ConnectionCls = _SocketConnection


class _Socket(requests.adapters.HTTPAdapter):
    """Every call of a session over the Unix socket `path`."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.pool = _SocketPool(_HOST, path=path)

    def get_connection_with_tls_context(self, request: requests.PreparedRequest, verify: bool | str | None,
                                        proxies: Mapping[str, str] | None = None,
                                        cert: tuple[str, str] | str | None = None) -> urllib3.HTTPConnectionPool:
        return self.pool

    def close(self) -> None:
        super().close()
        self.pool.close()


class _Client:
    """The HTTP calls to one service, which `Service` and every `Run` of it share, over its socket: `url`'s own, or
    the local end of an SSH forward to it, which `close` closes."""

    def __init__(self, url: str) -> None:
        if sys.platform == 'win32':  # no Unix socket there, nor the preexec_fn the forward's ssh is started with
            raise ServiceUnavailable('the compute client needs macOS, Linux or WSL: it reaches the service over a Unix '
                                     'socket, which Python on Windows itself cannot open; run Sophios inside WSL')
        self.url = url
        self.http = requests.Session()
        self.http.trust_env = False  # no proxy, nor .netrc: nothing but the socket is between Sophios and the service
        self.close: 'weakref.finalize[..., _Client]'
        self.forward: 'tuple[subprocess.Popen[bytes], str] | None' = None
        if url.startswith('unix://'):
            self.http.mount('http://', _Socket(url.removeprefix('unix://')))
            self.close = weakref.finalize(self, self.http.close)
        elif url.startswith('ssh://'):
            self.forward = _forward(url)
            self.http.mount('http://', _Socket(os.path.join(self.forward[1], 'api.sock')))
            self.close = weakref.finalize(self, _unforward, *self.forward, self.http)
        else:
            raise ServiceUnavailable(f'{url} is not where the compute service is: it is reached over SSH, as '
                                     f'ssh://[user@]host[:port][/socket] (the socket by default {SOCKET}), or on '
                                     'its own host, as unix:///socket; http:// and https:// need an HTTPS gateway '
                                     'in front of the service, which there is not yet')
        self.contract: str | None = None

    def call(self, method: str, path: str, *, body: Json | None = None, run: str | None = None,
             timeout: tuple[float, float] = _TIMEOUT) -> requests.Response:
        """`method path`, made again every _RETRY seconds while the service cannot be reached, cuts its answer short or
        answers 502, 503 or 504, for PATIENCE seconds: every call is safe to make again, a POST of the same request
        answering the same run. Any other answer is taken only from the service, which names itself in the Server
        header of every answer.

        Raises:
            ServiceUnavailable: The Service is closed; the service could not be reached, or stayed busy, for PATIENCE
                seconds, or at once once its SSH forward has ended, for the POST of `run` saying the run may exist all
                the same; or something else answered in its place.
        """
        if not self.close.alive:
            raise ServiceUnavailable(f'Service({self.url!r}) is closed: a new one follows its runs again, as '
                                     f'Service({self.url!r}).attach(run id)')
        give_up = time.monotonic() + PATIENCE
        while True:
            said: list[str] = []
            try:
                response = self.http.request(method, f'http://{_HOST}{path}', json=body, timeout=timeout)
                if response.status_code not in (502, 503, 504):
                    break
                why = f'answered {response.status_code}: {_said(response)}, for {PATIENCE:g} s'
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as error:
                why = f'could not be reached ({type(error).__name__}), for {PATIENCE:g} s'
                if self.forward is not None and self.forward[0].poll() is not None:  # nothing reaches it again
                    why = 'could not be reached: ssh ended, and its forward with it; a new Service forwards it again'
                    said, give_up = _said_by(self.forward[1]), 0.0
            if time.monotonic() + _RETRY > give_up:
                lines = [f'the compute service at {self.url} {why}', *said]
                if run is not None:
                    lines.append(f'run {run} may exist all the same: Service({self.url!r}).attach({run!r}) follows it, '
                                 'and the same request sent again never starts a second one')
                raise ServiceUnavailable(*lines)
            time.sleep(_RETRY)
        if not response.headers.get('Server', '').startswith('slurm-driver/'):
            raise ServiceUnavailable(f'{self.url} answered, but not as the compute service: check that it names the '
                                     'socket the service listens on')
        return response

    def answer(self, response: requests.Response) -> Json:
        """The body of a successful answer of the service, which answers in JSON but for a log.

        Raises:
            RequestRefused: 400, 403, 404 or 409: each problem a line, in the service's words, where it is as the
                author wrote it.
            ServiceUnavailable: Any other failure, or an answer that is not JSON.
        """
        body: Json | None = _json(response)
        if body is None:
            raise ServiceUnavailable(f'the compute service at {self.url} answered {response.status_code}, not in JSON')
        if response.ok:
            return body
        if response.status_code not in (400, 403, 404, 409):
            raise ServiceUnavailable(f'the compute service at {self.url} answered {response.status_code}: '
                                     f'{_said(response)}')
        raise RequestRefused(*(f'{_named(problem["pointer"])}: {problem["message"]}' if problem.get('pointer')
                               else str(problem.get('message')) for problem in _problems(response)))

    def speaks(self) -> None:
        """Read the service's contract, once, and refuse one whose major version is not CONTRACT.

        Raises:
            ServiceUnavailable: It speaks another, or cannot be asked.
        """
        if self.contract is not None:
            return
        version = self.answer(self.call('GET', '/openapi.json'))['info']['version']
        if version.partition('.')[0] != str(CONTRACT):
            raise ServiceUnavailable(f'the compute service at {self.url} speaks contract {version}, '
                                     f'and this Sophios speaks {CONTRACT}: upgrade the older of the two')
        self.contract = version


class Service:
    """The compute service at `url`: `ssh://[user@]host[:port][/socket]` on another host, the socket by default
    SOCKET, a service node's, or `unix:///socket` on this one.

    Over SSH, Sophios runs the user's own `ssh`, so their keys, agent, `~/.ssh/config` and jump hosts apply, and
    Sophios never sees a credential: it forwards a socket in a private temporary directory, no TCP port, to the
    service's, on a connection of its own (never a ControlMaster's), until the Service is closed
    (`with Service(url) as service:`) or the process exits; a Ctrl-C leaves it open. Sophios stores neither the URL
    nor anything else of a service.

    Raises:
        ServiceUnavailable: This is Windows itself, whatever `url` is: the compute client needs macOS, Linux or WSL;
            `url` is neither, http:// and https:// included, which need an HTTPS gateway in front of the service,
            which there is not yet; or ssh ended before it forwarded the socket, with its error.
    """

    def __init__(self, url: str) -> None:
        self._client = _Client(url)

    def __repr__(self) -> str:
        return f'Service({self._client.url!r})'

    def __enter__(self) -> 'Service':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the SSH forward, if there is one, and every Run of it; the runs go on in Slurm, and a new Service
        follows them."""
        self._client.close()

    def submit(self, workflow: 'Workflow', config: RunConfig | None = None, *, tool_registry: Tools | None = None,
               inference: Inference = Inference()) -> 'Run':
        """Send `workflow` to run as `config` says, and return its run at once, having printed its id and every
        warning.

        Every argument is `build_request`'s. It may be sent again, under the same id, while the service cannot be
        reached or is busy.

        Raises:
            SophiosError: The workflow does not compile; nothing is sent.
            RequestRefused: Sophios or the service refused the request: see `build_request`.
            ServiceUnavailable: The service could not be reached, stayed busy, failed, speaks another contract, or
                something else answered in its place.
        """
        request = build_request(workflow, config, tool_registry=tool_registry, inference=inference)
        self._client.speaks()
        posted = self._client.call('POST', '/runs', body=request, run=request['id'], timeout=_SUBMIT_TIMEOUT)
        answer = self._client.answer(posted)
        run = Run(self._client, answer)
        print(f'Submitted run {run.id} (Slurm job {answer.get("slurmJobId")}); from anywhere, {run!r} follows it.')
        _show_warnings(run)
        return run

    def attach(self, run_id: str) -> 'Run':
        """The run `run_id`, from any session or machine, having printed its warnings.

        Raises:
            RequestRefused: There is no such run.
            ServiceUnavailable: As `submit` raises it.
        """
        self._client.speaks()
        run = Run(self._client, self._client.answer(self._client.call('GET', f'/runs/{quote(run_id, safe="")}')))
        _show_warnings(run)
        return run


class Run:
    """A run on the compute service, made by `Service.submit` and `Service.attach`, never by calling the class.

    Followed as the local `RunHandle` is: `status` asks the service where the run is, and `wait` asks until it
    ends and returns its status, raising nothing for a run that FAILED or was CANCELLED; `outputs` and `logs` say
    what it made and printed; `cancel` stops it. Nothing here ends with the session: the run goes on in Slurm, and
    `Service.attach` follows it from anywhere. `id`, `warnings` and `slurm_job` are a compute run's alone.
    """

    def __init__(self, client: _Client, answer: Json) -> None:
        self._client = client
        self._answer = answer
        #: The run's id: the service's name for it, chosen by Sophios at submission.
        self.id: str = answer['id']

    def __repr__(self) -> str:
        return f'Service({self._client.url!r}).attach({self.id!r})'

    @property
    def slurm_job(self) -> str | None:
        """The Slurm job id of the run's Toil leader, for `squeue` and `sacct`."""
        job = self._answer.get('slurmJobId')
        return None if job is None else str(job)

    @property
    def warnings(self) -> tuple[str, ...]:
        """What the service warned of when it accepted the run."""
        return tuple(_line(warning) for warning in self._answer.get('warnings') or ())

    def status(self) -> RunStatus:
        """Where the run is now, as the service says.

        Raises:
            ServiceUnavailable: The service could not be asked.
        """
        self._answer = self._client.answer(self._client.call('GET', f'/runs/{self.id}'))
        return _status(self._answer)

    def wait(self, timeout: float | None = None) -> RunStatus:
        """Ask every POLL seconds until the run ends, printing a line per change of its state and of each step's.

        A run that FAILED or was CANCELLED is returned as one: nothing is raised for it. On Ctrl-C the run goes on,
        and how to follow it again is printed.

        Raises:
            TimeoutError: It had not ended within `timeout` seconds; it goes on.
            ServiceUnavailable: The service could not be asked.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        shown: tuple[RunState, str | None] | None = None
        steps: dict[str, str] = {}
        try:
            while True:
                status = self.status()
                if (status.state, status.reason) != shown:
                    shown = (status.state, status.reason)
                    print(f'{self.id}: {status.state.value}' + (f' ({status.reason})' if status.reason else ''))
                for entry in self._answer.get('steps') or ():
                    if steps.get(str(entry.get('slurmJobId'))) != entry.get('state'):
                        steps[str(entry.get('slurmJobId'))] = str(entry.get('state'))
                        print(f'{self.id}: step {_step_name(entry)} {entry.get("state")}')
                if status.state in _ENDED:
                    return status
                left = POLL if deadline is None else deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f'run {self.id} has not ended within {timeout:g} s; it goes on, and '
                                       f'{self!r}.wait() waits again')
                time.sleep(min(POLL, left))
        except KeyboardInterrupt:
            print(f'\nRun {self.id} goes on in Slurm: {self!r} follows it again, and its cancel() stops it.',
                  file=sys.stderr)
            raise

    def outputs(self) -> Json | None:
        """The workflow's output object, once the run COMPLETED; else None."""
        return self.status().outputs

    def logs(self) -> str:
        """`leader.log`, Toil's log of the whole run, so far: '' until Slurm has started the run's leader, and for good
        when it never does, as the local handle's is '' with no log.

        Raises:
            ServiceUnavailable: The service could not be asked.
        """
        response = self._client.call('GET', f'/runs/{self.id}/logs/leader.log')
        # A Run is one the service answered for: a 404 is its log alone, which Slurm makes as it starts the leader.
        if response.status_code == 404:
            return ''
        if not response.ok:
            self._client.answer(response)
        return response.content.decode('utf-8', errors='replace')

    def cancel(self) -> bool:
        """Cancel the run, and every job of it in Slurm's queue, and return once it has ended, as `wait(PATIENCE)`
        does: the service records the cancel and asks Slurm for it before it answers, so it is sent once.

        Returns:
            bool: True when the run is CANCELLED, by this call or before it; False when it had ended COMPLETED or
            FAILED, which it stays. As `concurrent.futures.Future.cancel`.

        Raises:
            RequestRefused: The run is another account's: only the account that submitted it, or the service's own,
                may cancel it.
            TimeoutError: It had not ended within PATIENCE seconds; the cancel stays recorded, and the run goes on.
            ServiceUnavailable: The service could not be asked; sending the cancel again finishes it.
        """
        self._client.answer(self._client.call('POST', f'/runs/{self.id}/cancel'))
        return self.wait(PATIENCE).state is RunState.CANCELLED


def _show_warnings(run: Run) -> None:
    """Print each warning of `run`, where it cannot be missed."""
    for warning in run.warnings:
        print(f'WARNING from the compute service: {warning}', file=sys.stderr)


def _status(answer: Json) -> RunStatus:
    """A run's answer as a `RunStatus`; its fields Sophios does not know are left out, and no message is parsed."""
    steps = tuple(StepStatus(_step_name(entry), RunState(entry['state'])) for entry in answer.get('steps') or ())
    return RunStatus(RunState(answer['state']), answer.get('reason'), _time(answer.get('startedAt')),
                     _time(answer.get('endedAt')), steps, answer.get('outputs'))


def _step_name(entry: Json) -> str:
    return str(entry.get('name') or f'(Slurm job {entry.get("slurmJobId")})')


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _json(response: requests.Response) -> Any:
    """The body of `response`, or None when it is not JSON: the one place an answer is parsed."""
    try:
        return response.json()
    except ValueError:
        return None


def _problems(response: requests.Response) -> list[Json]:
    """The problems an error answer lists, or one holding its text when it is not the contract's shape."""
    try:
        problems = _json(response)['errors']
        if isinstance(problems, list) and all(isinstance(problem, dict) for problem in problems):
            return problems
    except (KeyError, TypeError):
        pass
    return [{'message': response.text[:500]}]


def _said(response: requests.Response) -> str:
    return '; '.join(str(problem.get('message')) for problem in _problems(response))


def _line(warning: Any) -> str:
    if isinstance(warning, dict):
        return f'{_named(warning["pointer"])}: {warning.get("message")}' if warning.get('pointer') \
            else str(warning.get('message'))
    return str(warning)
