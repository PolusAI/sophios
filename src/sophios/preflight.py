"""What a compile or a run needs from this machine, checked before the work starts.

The pre-flight is for local runs only: `sophios --run_local`, `--generate_run_script`
and `--check`, a plain CWL workflow run the same way, and `Workflow.run()`. It
asks about this machine's container engine, paths and programs, so a compute
submission, which runs elsewhere, never goes through it; what a submission checks
is its payload (the document compiles, the inputs are well formed).

Each check returns diagnostics rather than raising, so a caller reports every
problem at once, each on one line: what is wrong, then what to do.
"""
import ast
import json
import os
import re
import shutil
import subprocess as sub
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urldefrag, urlparse
from urllib.request import url2pathname

import cwl_utils.parser as cwl
import requests

from . import run_local
from .input_output import input_paths, names_map_path
from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import Yaml

#: Engines `<engine> info` reaches: it needs the engine's daemon or machine, and no container or network.
DOCKER_LIKE: Final = ('docker', 'podman')
#: More Docker Desktop processes than this can make a run hang.
MAX_DOCKER_PROCESSES: Final = 1000
#: Where the docker CLI looks for its daemon when `DOCKER_HOST` does not say.
DEFAULT_DOCKER_SOCKET: Final = Path('/var/run/docker.sock')


@dataclass(frozen=True, slots=True)
class Job:
    """Input values, and the directory their relative paths are read from."""

    values: Yaml
    base: Path
    #: How a message names where the values came from: 'the workflow', '--inputs_file'.
    origin: str


@dataclass(frozen=True, slots=True)
class RunSettings:
    """What a run was asked to do, as far as this machine is concerned."""

    container_engine: str
    pull_dir: str
    ignore_install: bool = False
    ignore_processes: bool = False
    #: The CWL runner the run uses: `cwltool` or `toil-cwl-runner`.
    runner: str = 'cwltool'
    #: Whether the run only writes `run.sh`, which calls the runner as a program.
    run_script: bool = False
    #: Each directory the run writes into: the path, what it holds, what to do instead (see `unwritable`).
    writes: tuple[tuple[Path, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Needs:
    """What running some CWL documents needs from this machine."""

    #: The documents the runner gets: the workflow first, then any real-time analyses.
    documents: tuple[Path, ...]
    #: Every DockerRequirement, as a requirement or a hint, of every process the documents run.
    containers: tuple[cwl.DockerRequirement, ...]
    #: The values the run is given, each with where its relative paths are read from.
    jobs: tuple[Job, ...] = ()


def needs(documents: Sequence[Path], jobs: Sequence[Job] = ()) -> Needs:
    """Read what running `documents` with `jobs` needs, as cwl-docker-extract reads it (no `$schemas` are fetched)."""
    containers = [requirement for document in documents
                  for requirement in _containers(_load(document.absolute().as_uri()))]
    return Needs(tuple(documents), tuple(containers), tuple(jobs))


def _load(uri: str) -> Any:
    """The process at `uri`; a fragment names one process of a packed document.

    Not `cwl.load_document_by_uri`: it turns a `file:` URI back into a path with
    `Path(unquote_plus(...))`, which reads a '+' as a space and, on Windows, `/C:/...` as a
    path on the current drive. schema_salad's fetcher reads a `file:` URI on every platform.
    """
    document, fragment = urldefrag(uri)
    text = cwl.cwl_v1_2.LoadingOptions().fetcher.fetch_text(document)
    return cwl.load_document_by_string(text, document, id_=fragment or None)


def _containers(process: Any) -> list[cwl.DockerRequirement]:
    """Every DockerRequirement `process` declares, then each step's own and its process's, in order.

    The traversal of `cwl_utils.docker_extract`, which is not imported: importing it imports
    `cwl_utils.image_puller`, which puts a handler on the root logger, and cwltool's in-process
    run then fails setting up its own.
    """
    found = _declared(process)
    if isinstance(process, cwl.WorkflowTypes):
        for step in process.steps:
            run = _load(step.run) if isinstance(step.run, str) else step.run
            found += _declared(step) + _containers(run)
    return found


def _declared(node: Any) -> list[cwl.DockerRequirement]:
    """The DockerRequirements a process or step declares itself: its requirements, then its hints."""
    return [requirement for requirement in (*(node.requirements or ()), *(node.hints or ()))
            if isinstance(requirement, cwl.DockerRequirementTypes)]


def check(found: Needs, settings: RunSettings) -> None:
    """Raise every problem a run would hit on this machine, before anything is pulled.

    Raises:
        SophiosError: One diagnostic per problem.
    """
    problems = _path_problems(found)
    problems += [problem for directory, holds, remedy in settings.writes
                 if (problem := unwritable(directory, holds, remedy)) is not None]
    problems += _program_problems(found, settings)
    if found.containers:
        problems += _engine_problems(found, settings)
    if problems:
        raise SophiosError(problems)


def pull(found: Needs, settings: RunSettings) -> None:
    """Make every image the run uses available: pulled, and for docker and podman also loaded or imported.

    Raises:
        SophiosError: `wic037` for the first pull, load or import that failed.
    """
    if not found.containers:
        return
    # cwl-docker-extract recursively `docker pull`s all images in all subworkflows.
    # This is important because cwltool only uses `docker run` when executing
    # workflows, and if there is a local image available,
    # `docker run` will NOT query the remote repository for the latest image!
    # cwltool has a --force-docker-pull option, but this may cause multiple pulls in parallel.
    engine = settings.container_engine
    for document in found.documents:
        if engine == 'singularity':
            cmd = ['cwl-docker-extract', '-s', '--dir', settings.pull_dir, str(document)]
        else:
            cmd = ['cwl-docker-extract', '--force-download', '--container-engine', engine, str(document)]
        _fetch(cmd, f'pull {_images(found)}', engine)
    if engine != 'singularity':
        _load_and_import(found, engine)


def _load_and_import(found: Needs, engine: str) -> None:
    """Load each `dockerLoad` and import each `dockerImport` image, as cwltool does when it forces a pull.

    cwl-docker-extract skips both forms, and the run's `--disable-pull` stops cwltool doing
    them. As in cwltool, `dockerPull` wins when a requirement has it, and `dockerFile` is
    left to cwltool, which builds it at run time even with `--disable-pull`.
    """
    shipped = dict.fromkeys((req.dockerLoad, req.dockerImport, req.dockerImageId)
                            for req in found.containers if req.dockerPull is None)
    for load, import_source, image_id in shipped:
        if load is not None:
            _load_archive(engine, load)
        elif import_source is not None:
            if image_id is None:
                raise SophiosError([_unavailable(f'a dockerImport requirement of {import_source} names no '
                                                 'dockerImageId to import it as')])
            _fetch([engine, 'import', str(import_source), str(image_id)],
                   f'import {image_id} from {import_source}', engine)


def _load_archive(engine: str, source: str) -> None:
    """`<engine> load -i <source>`; a `source` that is not a file is an http(s) URL, fetched first (as cwltool does)."""
    if os.path.exists(source):
        _fetch([engine, 'load', '-i', source], f'load {source}', engine)
    elif urlparse(source).scheme in ('http', 'https'):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'image.tar'
            try:
                with requests.get(source, stream=True, timeout=60) as response, archive.open('wb') as out:
                    response.raise_for_status()
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
            except requests.RequestException as error:
                raise SophiosError([_unavailable(f'could not download {source}, which a dockerLoad requirement '
                                                 f'names ({error})')]) from error
            _fetch([engine, 'load', '-i', str(archive)], f'load {source}', engine)
    else:
        raise SophiosError([_unavailable(f'a dockerLoad requirement names {source}, which is neither a file '
                                         'nor an http(s) URL')])


def _fetch(cmd: list[str], doing: str, engine: str) -> None:
    """Run `cmd`, which `doing` describes; when it exits non-zero, quote the engine's last error line."""
    proc = sub.run(cmd, check=False, stderr=sub.PIPE, text=True, errors='replace')
    if proc.returncode != 0:
        raise SophiosError([_unavailable(f'{engine} could not {doing} ({cmd[0]} exited with status '
                                         f'{proc.returncode}: {_engine_said(proc.stderr)})')])


_WRAPPED_OUTPUT = re.compile(r'SubprocessError: (b\'.*\'|b".*")\s*$')


def _engine_said(stderr: str) -> str:
    """The engine's `Error: ...` line in `stderr`, else its last non-empty line, else 'it said nothing'.

    cwl-docker-extract folds the engine's output into a `SubprocessError(<bytes>)`, so its traceback ends in the
    bytes repr of that output; that output is what gets read."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    wrapped = _WRAPPED_OUTPUT.search(lines[-1]) if lines else None
    if wrapped:
        output = ast.literal_eval(wrapped.group(1))
        lines = [line.strip() for line in output.decode('utf-8', errors='replace').splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith('Error')]
    return (errors or lines or ['it said nothing'])[-1]


def _unavailable(message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, SophiosErrorCode.IMAGE_UNAVAILABLE,
                      f'{message}: check the image name or source, the network and your registry login, '
                      'then run again.')


def prepare(documents: Sequence[Path], settings: RunSettings, jobs: Sequence[Job] = ()) -> None:
    """Check what running `documents` with `jobs` needs from this machine, then pull their images.

    The one call every local run makes before it starts: the CLI's compiled and
    plain-CWL runs and `Workflow.run()`.

    Raises:
        SophiosError: Every problem found, before anything is pulled.
    """
    found = needs(documents, jobs)
    check(found, settings)
    pull(found, settings)


def unwritable(directory: Path, holds: str, remedy: str) -> Diagnostic | None:
    """A `wic021` error when this user cannot create `directory` or write into it, else None.

    The nearest part of the path that exists decides: a directory that does not
    exist yet is fine when it can be created.

    Args:
        directory (Path): Where Sophios is about to write.
        holds (str): What it writes there, for the message: 'the compiled workflow'.
        remedy (str): What to do instead: 'run Sophios from a directory you can write to'.

    Returns:
        Diagnostic | None: The error, or None when `directory` can be written.
    """
    target = directory.absolute()
    existing = next(path for path in (target, *target.parents) if path.exists())
    if existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
        return None
    why = 'is not writable by you' if existing.is_dir() else 'is a file'
    return Diagnostic(Severity.ERROR, SophiosErrorCode.DIRECTORY_NOT_WRITABLE,
                      f'Sophios writes {holds} to {target}, but {existing} {why}: {remedy}.')


def _path_problems(found: Needs) -> list[Diagnostic]:
    """One `wic016` for each File or Directory a job names that the run could not use."""
    spelled = _spellings(found.documents[0])
    problems = []
    for job in found.jobs:
        for name, value in input_paths(job.values):
            written = _written_path(value)
            if written is None:
                continue
            resolved = Path(written) if Path(written).is_absolute() else job.base / written
            if (why := _unusable(resolved, value['class'])) is not None:
                problems.append(Diagnostic(
                    Severity.ERROR, SophiosErrorCode.MISSING_INPUT_FILE,
                    f"input '{spelled(name)}' (from {job.origin}, whose relative paths are read from "
                    f"{job.base}) names {written!r}, which {why}."))
    return problems


def _unusable(path: Path, kind: str) -> str | None:
    """Why a run cannot use `path` as a File or Directory, and what to do; None when it can."""
    if not path.exists():
        return f"does not exist at {path}: correct the path, or create the {'file' if kind == 'File' else 'directory'}"
    if kind == 'File' and path.is_dir():
        return f'is a directory ({path}): bind a file, or declare the input a Directory'
    if kind == 'Directory' and not path.is_dir():
        return f'is a file ({path}): bind a directory, or declare the input a File'
    mode, letters = (os.R_OK | os.X_OK, 'rx') if kind == 'Directory' else (os.R_OK, 'r')
    if not os.access(path, mode):
        return f'you may not read ({path}): give yourself access (chmod u+{letters} {path})'
    return None


def _written_path(value: Mapping[str, Any]) -> str | None:
    """The local path a File or Directory names: its `location` or `path`, without `file://`;
    None for a literal (`contents`, `listing`) or a remote URL."""
    written = value.get('location', value.get('path'))
    if not isinstance(written, str):
        return None
    parsed = urlparse(written)
    if parsed.scheme == 'file':
        return url2pathname(parsed.path)
    return None if len(parsed.scheme) > 1 else written   # one letter is a Windows drive


def _spellings(root: Path) -> Callable[[str], str]:
    """How a message spells a job input: as authored (`cat/file`), from the names map the compile wrote
    beside `root`; as it is when there is no map (a plain CWL workflow)."""
    path = names_map_path(root.parent, root.stem)
    ports = json.loads(path.read_text(encoding='utf-8')).get('ports', {}) if path.exists() else {}

    def spelled(name: str) -> str:
        entry = ports.get(name)
        return '/'.join([*entry['steps'], entry['port']]) if entry and entry['steps'] else name
    return spelled


#: How to install each program a run may call.
_INSTALL: Final = {
    'cwltool_filterlog': 'install Sophios in this environment (pip install sophios), which provides it',
    'toil-cwl-runner': 'install Toil (pip install "toil[cwl]")',
    'cwl-docker-extract': 'install cwl-utils (pip install cwl-utils)',
}


def _program_problems(found: Needs, settings: RunSettings) -> list[Diagnostic]:
    """A program the run calls that is not here, or a runner that cannot run on this platform."""
    problems = []
    if settings.run_script:
        script = 'cwltool_filterlog' if settings.runner == 'cwltool' else settings.runner
        if shutil.which(script) is None:
            problems.append(_missing(f'run.sh calls {script}, which is not on PATH: {_INSTALL[script]}'))
    elif run_local.RUNNER_UNAVAILABLE is not None:
        problems.append(_missing(f'{settings.runner} cannot run here ({run_local.RUNNER_UNAVAILABLE}): run '
                                 'Sophios inside WSL (https://learn.microsoft.com/windows/wsl/install)'))
    if found.containers and shutil.which('cwl-docker-extract') is None:
        problems.append(_missing(f'pulling the images for {settings.container_engine} needs cwl-docker-extract, '
                                 f"which is not on PATH: {_INSTALL['cwl-docker-extract']}"))
    return problems


def _missing(message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, SophiosErrorCode.PROGRAM_MISSING, f'{message}.')


def _engine_problems(found: Needs, settings: RunSettings) -> list[Diagnostic]:
    """What stops the engine running the containers `found` needs; one diagnostic each.

    Only signals that do not depend on wording are classified: whether the engine
    program exists, the exit status of `<engine> info`, and whether the socket it
    would use exists and is accessible. What the engine said is quoted, never read.
    """
    engine = settings.container_engine
    probe = [engine, 'info'] if engine in DOCKER_LIKE else [engine, '--version']
    try:
        proc = sub.run(probe, check=False, stdout=sub.DEVNULL, stderr=sub.PIPE)
    except FileNotFoundError:
        if settings.ignore_install:
            return []
        return [_engine_error(f'{engine} is not installed (it is not on PATH), and this workflow runs tools in '
                              f'containers ({_images(found)}): install {engine}, or name an installed engine '
                              'with --container_engine')]
    problems = []
    if proc.returncode != 0 and not settings.ignore_install:
        problems.append(_unusable_engine(engine, proc.returncode, proc.stderr.decode('utf-8', errors='replace')))
    if engine in DOCKER_LIKE and sys.platform != 'win32' and not settings.ignore_processes:
        count = _docker_processes()
        if count > MAX_DOCKER_PROCESSES:
            problems.append(_engine_error(
                f'{count} docker processes are running, and more than {MAX_DOCKER_PROCESSES} can make a run '
                'hang: quit Docker and start it again (sudo pkill com.docker && sudo pkill Docker), or pass '
                '--ignore_docker_processes'))
    return problems


def _unusable_engine(engine: str, status: int, said: str) -> Diagnostic:
    """The engine is installed but its probe exited non-zero: its socket is not there, or not yours, or it said why.

    A socket is called missing only when `DOCKER_HOST` names it: without that, the CLI may reach its
    daemon through a context, and the default path says nothing.
    """
    line = next((text.strip() for text in said.splitlines() if text.strip()), 'it said nothing')
    socket = _docker_socket() if engine == 'docker' else None
    if socket is not None and 'DOCKER_HOST' in os.environ and not socket.exists():
        return _engine_error(f'{engine} is installed, but its engine is not reachable: the socket {socket} does '
                             f'not exist ({line}): start the engine, then run again')
    if socket is not None and socket.exists() and not os.access(socket, os.R_OK | os.W_OK):
        return _engine_error(f'{engine} is installed, but you may not use its socket {socket} ({line}): add your '
                             'user to the docker group (sudo usermod -aG docker $USER), then log out and back in')
    return _engine_error(f'{engine} is installed, but `{engine} info` exited with status {status} ({line}): '
                         f'run `{engine} info` to see why, and start the engine if it is stopped')


def _docker_socket() -> Path | None:
    """The unix socket the docker CLI connects to: `DOCKER_HOST`'s, else the default; None for a remote host."""
    host = os.environ.get('DOCKER_HOST')
    if host is None:
        return DEFAULT_DOCKER_SOCKET
    return Path(host.removeprefix('unix://')) if host.startswith('unix://') else None


def _images(found: Needs) -> str:
    """The first image the run uses, and how many others: `docker.io/bash:4.4 and 2 more`."""
    names = list(dict.fromkeys(req.dockerPull or req.dockerImageId or 'an image built by the run'
                               for req in found.containers))
    return names[0] if len(names) == 1 else f'{names[0]} and {len(names) - 1} more'


def _docker_processes() -> int:
    """How many Docker Desktop processes run (it names them com.docker.*); 0 without pgrep."""
    try:
        listed = sub.run(['pgrep', 'com.docker'], check=False, stdout=sub.PIPE, stderr=sub.DEVNULL)
    except FileNotFoundError:
        return 0
    return len(listed.stdout.split())


def _engine_error(message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, SophiosErrorCode.CONTAINER_ENGINE_UNAVAILABLE, f'{message}.')
