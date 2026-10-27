"""Run a workflow on a Slurm cluster, through the cluster's compute service (the slurm driver).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it. It takes a
`Workflow` built with that API, compiles it, and sends it with its inputs as one request:

    from sophios.compute import RunConfig, Service

    service = Service('http://127.0.0.1:7998')  # the local end of your own tunnel to the service
    config = RunConfig('/projects/lab/results/run7').basepath('/projects/lab/data').leader(time='2:00:00')
    run = service.submit(workflow, config)
    run.wait()

A `RunConfig` is everything of the request but the workflow and its inputs: the folder it is made with is where
the outputs go, and each of its setters sets a field more; without one, the run takes the service's settings.

`Service.run` submits and waits, and raises when the run does not complete; `Service.attach` follows a run from
any session or machine. The run goes on in Slurm whatever happens to the session.

Every path goes as written, and the service, not Sophios, reads it on the cluster. A path a tool reads is a path on
the cluster, absolute or relative to `basepath`, or an http(s) URL. An output folder (a Directory input a tool writes
into and returns) given as a name, such as `segmentation`, is made in the run, never read against `basepath`, and
its result lands in the outputs' folder under that name; one given as an absolute path is that folder. The outputs'
folder is the one `RunConfig(folder)` names (output mode `workflowOutDir`, the supported way: the outputs are copied
into it), or the run's own folder on the cluster without a config; `RunConfig.in_place()` (output mode
`workflowInPlace`), an advanced choice, has each tool write an output folder with an absolute path there, once,
without a copy. The request's shape is `run-request.schema.json`, a copy of the service's own: Sophios checks every
request against it before sending, and the service again on arrival.
"""
import json
import re
import secrets
import sys
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal
from urllib.parse import quote

import requests
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
#: How long a call is made again while the service cannot be reached or is busy, in seconds.
PATIENCE: Final = 300.0
#: How long a call may take to connect, and to answer: a submission asks Slurm several times, 30 s each at most,
#: so it gets longer than the other calls, lest a slow but healthy one be sent again while it is still going on.
_TIMEOUT: Final = (10.0, 60.0)
_SUBMIT_TIMEOUT: Final = (10.0, 300.0)
#: How much of a failed step's log a failure shows: its last lines, of at most this many bytes.
_TAIL: Final = 4096
_ENDED: Final = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)

#: The schema every request is checked against: the service's own, as Sophios was released with.
SCHEMA: Final[Json] = json.loads(resources.files('sophios').joinpath('run-request.schema.json').read_text('utf-8'))
_VALIDATOR: Final = Draft202012Validator(SCHEMA)

#: What a run id may not hold, made "-": the service's id rule is letters, digits, ".", "_" and "-".
_NOT_IN_ID: Final = re.compile(r'[^A-Za-z0-9._-]')
#: A local location that names nothing on the cluster: a home directory, or an environment variable.
_LAPTOP_ONLY: Final = re.compile(r'^~|\$(\{|[A-Za-z_])')
_URL: Final = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
#: The keys of a path, by the class of the object that holds it.
_PATH_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    'File': ('location', 'path'), 'Directory': ('location', 'path'), 'DockerRequirement': ('dockerLoad',)}


class ComputeError(Exception):
    """A call to the compute service that could not do what was asked: `code`, which each subclass names, says
    which, and `messages` holds every problem, a line each.

    The codes are this module's own, `cmp001` to `cmp003` (docs/compute.md), and not Sophios's error codes: a
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
    """The service could not be reached, stayed busy, failed, or speaks another contract."""

    code = 'cmp002'


class RunFailed(ComputeError):
    """A run that ended FAILED or CANCELLED, raised by `Service.run`: `run` is the run, `status` how it ended."""

    code = 'cmp003'

    def __init__(self, run: 'Run', status: RunStatus, *messages: str) -> None:
        super().__init__(*messages)
        self.run = run
        self.status = status


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
        RequestRefused: A path that names nothing on the cluster, a real-time analysis, or a value the schema does
            not take; every one is a line.
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
    problems = [f'{_named(pointer)}: {written!r} names nothing on the cluster ("~" or an environment variable '
                'are this machine\'s); write the path as it is on the cluster'
                for pointer, written in locations(request) if _LAPTOP_ONLY.search(written) and not _URL.match(written)]
    problems += [_schema_problem(error) for error in _VALIDATOR.iter_errors(request)]
    if problems:
        raise RequestRefused(*problems)
    return request


def _key(name: str) -> str:
    """The request's name for the keyword `name` of a `RunConfig` setter: the same words, in camelCase."""
    first, *rest = name.split('_')
    return first + ''.join(word.capitalize() for word in rest)


def locations(request: Json) -> Iterator[tuple[str, str]]:
    """Every path in the request's workflow and inputs, with its pointer: the location and the path of each File
    and Directory at any depth, and each `dockerLoad`, as the service finds them (its `contract/paths` fixtures)."""
    def walk(node: Any, pointer: str) -> Iterator[tuple[str, str]]:
        if isinstance(node, list):
            for index, item in enumerate(node):
                yield from walk(item, f'{pointer}/{index}')
        elif isinstance(node, dict):
            yield from ((f'{pointer}/{key}', node[key]) for key in _PATH_KEYS.get(str(node.get('class')), ())
                        if isinstance(node.get(key), str))
            # In a map of requirements or hints, a DockerRequirement need not repeat its class.
            for group in ('requirements', 'hints'):
                docker = node[group].get('DockerRequirement') if isinstance(node.get(group), dict) else None
                if isinstance(docker, dict) and docker.get('class') != 'DockerRequirement' \
                        and isinstance(docker.get('dockerLoad'), str):
                    yield f'{pointer}/{group}/DockerRequirement/dockerLoad', docker['dockerLoad']
            for key, value in node.items():
                yield from walk(value, f'{pointer}/{_escape(key)}')
    for field in ('workflow', 'inputs'):
        yield from walk(request.get(field), f'/{field}')


def _schema_problem(error: Any) -> str:
    """One line for a value the schema refuses: where it is, and what the field is, in the schema's words."""
    what = error.schema.get('description') if isinstance(error.schema, dict) else None
    if what and error.validator in ('type', 'enum', 'pattern'):
        return f'{_named(_pointer(error.absolute_path))}: {error.instance!r} is not {what}'
    return f'{_named(_pointer(error.absolute_path))}: {error.message}'


def _escape(key: str) -> str:
    return key.replace('~', '~0').replace('/', '~1')


def _pointer(path: Any) -> str:
    return ''.join(f'/{_escape(str(part))}' for part in path)


def _named(pointer: str) -> str:
    """Where `pointer` (RFC 6901, into a request) is, as the author wrote it: an input by its name, or a field."""
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')[1:]]
    if len(parts) > 1 and parts[0] == 'inputs':
        return f'input {parts[1]}'
    return '.'.join(parts) or 'the request'


class _Client:
    """The HTTP calls to one service, which `Service` and every `Run` of it share."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip('/')
        self.http = requests.Session()
        self.contract: str | None = None

    # pylint: disable-next=too-many-arguments
    def call(self, method: str, path: str, *, body: Json | None = None, params: Mapping[str, str] | None = None,
             run: str | None = None, timeout: tuple[float, float] = _TIMEOUT) -> requests.Response:
        """`method path`, made again while the service cannot be reached or answers 502, 503 or 504, for PATIENCE
        seconds: every call is safe to make again, a POST of the same request answering the same run.

        Raises:
            ServiceUnavailable: The service could not be reached, or stayed busy, for PATIENCE seconds; for the
                POST of `run`, saying the run may exist all the same.
        """
        give_up = time.monotonic() + PATIENCE
        while True:
            try:
                response = self.http.request(method, self.url + path, json=body, params=params, timeout=timeout)
                if response.status_code not in (502, 503, 504):
                    return response
                why, pause = f'answered {response.status_code}: {_said(response)}', float(
                    response.headers.get('Retry-After') or 5)
            except (requests.ConnectionError, requests.Timeout) as error:
                why, pause = f'could not be reached ({type(error).__name__})', 5.0
            if time.monotonic() + pause > give_up:
                lines = [f'the compute service at {self.url} {why}, for {PATIENCE:g} s']
                if run is not None:
                    lines.append(f'run {run} may exist all the same: Service({self.url!r}).attach({run!r}) follows it, '
                                 'and the same request sent again never starts a second one')
                raise ServiceUnavailable(*lines)
            time.sleep(pause)

    def answer(self, response: requests.Response, request: Json | None = None) -> Json:
        """The body of a successful answer.

        Raises:
            RequestRefused: 400, 404 or 409: each problem a line, where it is as the author wrote it; and when
                `request`, the request answered, sent relative paths without a basepath, a line saying to pass one.
            ServiceUnavailable: Any other failure.
        """
        if response.ok:
            body: Json = response.json()
            return body
        if response.status_code not in (400, 404, 409):
            raise ServiceUnavailable(f'the compute service at {self.url} answered {response.status_code}: '
                                     f'{_said(response)}')
        problems = _problems(response)
        lines = [f'{_named(problem["pointer"])}: {problem["message"]}' if problem.get('pointer')
                 else str(problem.get('message')) for problem in problems]
        if request is not None and 'basepath' not in request:
            relative = {pointer for pointer, written in locations(request)
                        if not written.startswith('/') and not _URL.match(written)}
            if relative & {problem.get('pointer') for problem in problems}:
                # A request without outputMode was sent without a RunConfig, and a RunConfig is made with a folder.
                fix = ('give the RunConfig .basepath(<dir>)' if 'outputMode' in request else
                       'make a RunConfig with the folder the outputs go to and give it .basepath(<dir>)')
                lines.append(f'these paths are relative and the request has no basepath: {fix}, '
                             'the directory on the cluster they are relative to')
        raise RequestRefused(*lines)

    def speaks(self) -> None:
        """Read the service's contract, once, and refuse one whose major version is not CONTRACT.

        Raises:
            ServiceUnavailable: It speaks another, or cannot be asked.
        """
        if self.contract is not None:
            return
        version = str(self.answer(self.call('GET', '/openapi.json')).get('info', {}).get('version', ''))
        if version.partition('.')[0] != str(CONTRACT):
            raise ServiceUnavailable(f'the compute service at {self.url} speaks contract {version or "(none)"}, '
                                     f'and this Sophios speaks {CONTRACT}: upgrade the older of the two')
        self.contract = version


class Service:
    """A compute service, at `url`: the local end of the tunnel the user opens to it, such as
    `http://127.0.0.1:7998`. Sophios opens no tunnel, and stores neither the URL nor anything else of a service."""

    def __init__(self, url: str) -> None:
        self._client = _Client(url)

    def __repr__(self) -> str:
        return f'Service({self._client.url!r})'

    def submit(self, workflow: 'Workflow', config: RunConfig | None = None, *, tool_registry: Tools | None = None,
               inference: Inference = Inference()) -> 'Run':
        """Send `workflow` to run as `config` says, and return its run at once, having printed its id and every
        warning.

        Every argument is `build_request`'s. It may be sent again, under the same id, while the service cannot be
        reached or is busy.

        Raises:
            SophiosError: The workflow does not compile; nothing is sent.
            RequestRefused: Sophios or the service refused the request: see `build_request`.
            ServiceUnavailable: The service could not be reached, stayed busy, failed, or speaks another contract.
        """
        request = build_request(workflow, config, tool_registry=tool_registry, inference=inference)
        self._client.speaks()
        posted = self._client.call('POST', '/runs', body=request, run=request['id'], timeout=_SUBMIT_TIMEOUT)
        answer = self._client.answer(posted, request)
        run = Run(self._client, answer)
        print(f'Submitted run {run.id} (Slurm job {answer.get("slurmJobId")}); from anywhere, {run!r} follows it.')
        run.show_warnings()
        return run

    def run(self, workflow: 'Workflow', config: RunConfig | None = None, *, timeout: float | None = None,
            tool_registry: Tools | None = None, inference: Inference = Inference()) -> 'Run':
        """`submit`, then `Run.wait`: the run, once it COMPLETED. `timeout` is `Run.wait`'s; the rest, `submit`'s.

        Raises:
            RunFailed: It ended FAILED or CANCELLED: the message names why, and the end of the failed step's log.
            TimeoutError: It had not ended within `timeout` seconds; it goes on.
            SophiosError, RequestRefused, ServiceUnavailable: As `submit` and `Run.wait` raise them.
        """
        run = self.submit(workflow, config, tool_registry=tool_registry, inference=inference)
        status = run.wait(timeout)
        if status.state is not RunState.COMPLETED:
            raise RunFailed(run, status, *run.failure())
        return run

    def attach(self, run_id: str) -> 'Run':
        """The run `run_id`, from any session or machine, having printed its warnings.

        Raises:
            RequestRefused: There is no such run.
            ServiceUnavailable: As `submit` raises it.
        """
        self._client.speaks()
        run = Run(self._client, self._client.answer(self._client.call('GET', f'/runs/{quote(run_id, safe="")}')))
        run.show_warnings()
        return run


class Run:
    """A run on the compute service, made by `Service.submit` and `Service.attach`, never by calling the class.

    `status` asks the service where the run is; `wait` asks until it ends. Nothing here ends with the session:
    the run goes on in Slurm, and `Service.attach` follows it from anywhere.
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
    def targets(self) -> dict[str, str]:
        """The folders the run writes in place, by the input that names each (only with `workflowInPlace`)."""
        return dict(self._answer.get('targets') or {})

    @property
    def warnings(self) -> tuple[str, ...]:
        """What the service warned of when it accepted the run."""
        return tuple(_line(warning) for warning in self._answer.get('warnings') or ())

    def show_warnings(self) -> None:
        """Print each warning, where it cannot be missed."""
        for warning in self.warnings:
            print(f'WARNING from the compute service: {warning}', file=sys.stderr)

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

    def logs(self, step: str | None = None) -> str:
        """`leader.log`, Toil's log of the whole run; or, for `step` (a name as the status gives it, such as
        `compact/merge`), each of its logs, under its file name.

        Raises:
            ValueError: No step of that name has started.
        """
        if step is None:
            return self._log('leader.log')
        self.status()
        entries = [entry for entry in self._answer.get('steps') or () if entry.get('name') == step]
        if not entries:
            names = sorted({str(entry['name']) for entry in self._answer.get('steps') or () if entry.get('name')})
            raise ValueError(f'run {self.id} has no step {step!r} that has started; '
                             f'those that have are {", ".join(names) or "none yet"}')
        return '\n'.join(f'==> {name} <==\n{self._log(name)}' for entry in entries for name in entry.get('logs') or ())

    def cancel(self) -> RunStatus:
        """Cancel the run, and every job of it in Slurm's queue; safe to send again. Where it is then.

        Raises:
            ServiceUnavailable: The service could not be asked; the cancel stays recorded, and sending it again
                finishes it.
        """
        self._answer = self._client.answer(self._client.call('POST', f'/runs/{self.id}/cancel'))
        return _status(self._answer)

    def failure(self) -> list[str]:
        """Why the run did not complete, as message lines: its state and the service's reason, and the end of the
        log of the first step that failed, or of `leader.log` when none did."""
        status = _status(self._answer)
        lines = [f'run {self.id} ended {status.state.value}' + (f': {status.reason}' if status.reason else '')]
        failed = next((entry for entry in self._answer.get('steps') or ()
                       if entry.get('state') == 'FAILED' and entry.get('logs')), None)
        name = failed['logs'][0] if failed else 'leader.log'
        try:
            tail = '\n'.join(self._log(name, -_TAIL).splitlines()[-20:])
        except ComputeError as error:  # a leader Slurm never started has no log at all
            tail = f'(it cannot be read: {error})'
        lines.append(f'the end of {name}' + (f', of step {_step_name(failed)}' if failed else '') + f':\n{tail}')
        return lines

    def _log(self, name: str, offset: int = 0) -> str:
        response = self._client.call('GET', f'/runs/{self.id}/logs/{quote(name, safe="")}',
                                     params={'offset': str(offset)})
        if not response.ok:
            self._client.answer(response)
        return response.content.decode('utf-8', errors='replace')


def _status(answer: Json) -> RunStatus:
    """A run's answer as a `RunStatus`; its fields Sophios does not know are left out, and no message is parsed."""
    steps = tuple(StepStatus(_step_name(entry), RunState(entry['state'])) for entry in answer.get('steps') or ())
    return RunStatus(RunState(answer['state']), answer.get('reason'), _time(answer.get('startedAt')),
                     _time(answer.get('endedAt')), steps, answer.get('outputs'))


def _step_name(entry: Json) -> str:
    return str(entry.get('name') or f'(Slurm job {entry.get("slurmJobId")})')


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _problems(response: requests.Response) -> list[Json]:
    """The problems an error answer lists, or one holding its text when it is not the contract's shape."""
    try:
        problems = response.json()['errors']
        if isinstance(problems, list) and all(isinstance(problem, dict) for problem in problems):
            return problems
    except (ValueError, KeyError, TypeError):
        pass
    return [{'message': response.text[:500]}]


def _said(response: requests.Response) -> str:
    return '; '.join(str(problem.get('message')) for problem in _problems(response))


def _line(warning: Any) -> str:
    if isinstance(warning, dict):
        return f'{_named(warning["pointer"])}: {warning.get("message")}' if warning.get('pointer') \
            else str(warning.get('message'))
    return str(warning)
