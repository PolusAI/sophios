"""Run a workflow on a Slurm cluster, through the cluster's compute service (the slurm driver).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it. It takes a
`Workflow` built with that API, compiles it, and sends it with its inputs as one request, which `build_request`
builds.

Every path goes as written: a path on the cluster, absolute or relative to `basepath`, or an http(s) URL; the
service checks each one there, and Sophios never looks at them. The request's shape is `run-request.schema.json`, a
copy of the service's own: Sophios checks every request against it before sending, and the service again on
arrival. Where the workflow's outputs go is `workflow_out_dir` (the supported way: they are copied into that folder),
the run's own folder on the cluster when it is not given, or, as an advanced choice, `workflow_in_place`, where
each tool writes straight into the folder its input names.
"""
import json
import re
import secrets
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar, Final

from jsonschema import Draft202012Validator

from .api.python._inference import Inference
from .api.python._workflow_runtime import service_document
from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import Json, Tools

if TYPE_CHECKING:
    from .api.python.workflow import Workflow

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


class ComputeError(SophiosError):
    """A call to the compute service that could not do what was asked; each subclass names its code."""

    code: ClassVar[SophiosErrorCode]

    def __init__(self, *messages: str) -> None:
        super().__init__(Diagnostic(Severity.ERROR, self.code, message) for message in messages)


class RequestRefused(ComputeError):
    """A request Sophios refused before sending it, or the service refused; every problem is a line."""

    code = SophiosErrorCode.COMPUTE_REQUEST_REFUSED


def run_id(name: str, now: datetime | None = None) -> str:
    """A new id for a run of the workflow `name`: `<name>-<UTC yyyymmddThhmmss>-<4 hex>`, fitted to the id rule."""
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%S')
    # The suffix takes 21 of the 128 characters the rule allows; an id starts with a letter or a digit.
    base = _NOT_IN_ID.sub('-', name).lstrip('._-')[:107] or 'run'
    return f'{base}-{stamp}-{secrets.token_hex(2)}'


# pylint: disable-next=too-many-arguments
def build_request(workflow: 'Workflow', *, basepath: str | None = None, workflow_out_dir: str | None = None,
                  workflow_in_place: bool = False, leader: Mapping[str, str] | None = None,
                  steps: Mapping[str, str] | None = None, log_level: str | None = None,
                  tool_registry: Tools | None = None, inference: Inference = Inference()) -> Json:
    """The request `POST /runs` takes to run `workflow`, as `Service.submit` sends it.

    Args:
        workflow: The workflow, compiled here into one document with its inputs.
        basepath: The directory on the cluster that relative paths are relative to.
        workflow_out_dir: The folder on the cluster the workflow's outputs are copied into: the supported way.
        workflow_in_place: Instead, have each tool write its output folder where its input names it (advanced).
        leader: The Toil leader's Slurm job: `partition`, `time`, `memory`; the service's settings otherwise.
        steps: Where step jobs go and how long each may run: `partition`, `gpuPartition`, `time`.
        log_level: Toil's log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`.
        tool_registry: Tools the workflow's steps may name, besides those it was built with.
        inference: How unbound inputs are connected.

    Raises:
        RequestRefused: A path that names nothing on the cluster, a real-time analysis, or a field the schema
            does not take; every one is a line.
        ValueError: Both `workflow_out_dir` and `workflow_in_place`.
    """
    if workflow_out_dir is not None and workflow_in_place:
        raise ValueError('give workflow_out_dir or workflow_in_place, not both: they say where the outputs go')
    document = service_document(workflow, tool_registry=tool_registry, inference=inference)
    if document.realtime:
        raise RequestRefused(*(f'{declaration.document} declares the real-time analysis {declaration.analysis!r}, '
                               'which the compute service never runs; remove it, or run the workflow locally'
                               for declaration in document.realtime))
    request: Json = {'id': run_id(workflow.process_name), 'workflow': document.cwl, 'inputs': document.job_inputs}
    if basepath is not None:
        request['basepath'] = basepath
    if workflow_out_dir is not None:
        request |= {'outputMode': 'workflowOutDir', 'workflowOutDir': workflow_out_dir}
    elif workflow_in_place:
        request['outputMode'] = 'workflowInPlace'
    for key, value in (('leader', leader), ('steps', steps), ('logLevel', log_level)):
        if value is not None:
            request[key] = dict(value) if isinstance(value, Mapping) else value
    problems = [f'{_named(pointer)}: {written!r} names nothing on the cluster ("~" or an environment variable '
                'are this machine\'s); write the path as it is on the cluster'
                for pointer, written in locations(request) if _LAPTOP_ONLY.search(written) and not _URL.match(written)]
    problems += [_schema_problem(error) for error in _VALIDATOR.iter_errors(request)]
    if problems:
        raise RequestRefused(*problems)
    return request


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
