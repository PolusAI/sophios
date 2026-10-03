"""Real-time analysis: compile each declared analysis once, and run it beside the workflow.

A `cwl_subinterpreter` step declares an analysis (see `sophios.ir.realtime`).
After the main compile, `compile_analyses` compiles each analysis as a one-step
workflow and `write` puts it under `<basepath>/realtime/<name>/`, with a manifest
`<basepath>/<workflow>.realtime.json`, and returns a `Plan` for each. While cwltool
runs the workflow with a `--cachedir`, a `Watcher` per plan runs the analysis on the
host as the files it watches change in this run's job directories, when the job
holding them finishes, and once more when the workflow ends.

Watching is polling, on purpose: inotify and its kin miss changes on network
filesystems and container mounts, which is where long jobs write.
"""
import json
import os
import re
import subprocess as sub
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml

from . import compiler, plugins
from . import input_output as io
from .ir import frontdoor
from .ir.artifacts import CompilationArtifact
from .ir.realtime import Declaration
from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .utils_yaml import Key
from .wic_types import CompilerOptions, GraphSettings, Tools, Yaml

#: The directory, under the base path and under the cachedir, that holds each analysis's files.
DIRECTORY: Final = 'realtime'


@dataclass(frozen=True, slots=True)
class Analysis:
    """A declaration with its analysis compiled, under the name its files are kept by."""

    name: str
    declaration: Declaration
    artifact: CompilationArtifact


def compile_analyses(declarations: tuple[Declaration, ...],
                     yml_paths: dict[str, dict[str, Path]], tools: Tools,
                     compiler_options: CompilerOptions,
                     graph_settings: GraphSettings) -> tuple[Analysis, ...]:
    """Compile each declaration's analysis once, as a one-step workflow.

    Raises:
        SophiosError: `wic044` at the declaration, carrying each of the analysis's own
            diagnostics, when an analysis does not compile.
    """
    analyses: list[Analysis] = []
    taken: set[str] = set()
    for declaration in declarations:
        name = declaration.document
        suffix = 2
        while name in taken:
            name, suffix = f'{declaration.document}_{suffix}', suffix + 1
        taken.add(name)
        source = yaml.dump(_wrapper(declaration), Dumper=io.NoAliasDumper, sort_keys=False, line_break='\n')
        try:
            bundle = frontdoor.bundle_from_source(source, f'{Path(declaration.analysis).stem}_only', yml_paths, tools)
            result = compiler.compile_source(bundle, compiler_options, graph_settings,
                                             relative_run_path=True, testing=True)
        except SophiosError as e:
            raise SophiosError(Diagnostic(
                Severity.ERROR, SophiosErrorCode.REALTIME_DECLARATION,
                f'real-time analysis {declaration.analysis!r} does not compile: {diagnostic}',
                declaration.span) for diagnostic in e.diagnostics) from e
        analyses.append(Analysis(name, declaration, result.artifact))
    return tuple(analyses)


def _wrapper(declaration: Declaration) -> Yaml:
    """The one-step workflow that runs `declaration`'s analysis, configured by its `config`.

    Every `in:` value is a literal: a plain string there would otherwise be read as
    an edge, and a file is named by its basename, found when the analysis runs.
    """
    analysis = declaration.analysis
    config = _literal_inputs(declaration.config)
    if analysis.endswith('.wic'):
        return {'steps': [{'id': analysis}], 'wic': {'steps': {f'(1, {analysis})': {'wic': {'steps': config}}}}}
    # id last, so a config carrying its own `id` cannot retarget the step.
    return {'steps': [{**config, 'id': analysis}]}


def _literal_inputs(config: Any) -> Any:
    """`config` with every value of every `in:` mapping in it made an inline literal."""
    match config:
        case dict():
            return {key: ({port: {Key.INLINE_INPUT: value} for port, value in item.items()}
                          if key == 'in' and isinstance(item, dict) else _literal_inputs(item))
                    for key, item in config.items()}
        case list():
            return [_literal_inputs(item) for item in config]
    return config


def manifest_path(basepath: Path, workflow_name: str) -> Path:
    """Where `write` records the analyses of `workflow_name`, beside its names map."""
    return basepath / f'{workflow_name}.realtime.json'


def documents(analyses: tuple[Analysis, ...], basepath: Path) -> list[Path]:
    """The root CWL file of each analysis, as `write` writes it."""
    return [basepath / DIRECTORY / analysis.name / f'{analysis.artifact.name}.cwl' for analysis in analyses]


@dataclass(frozen=True, slots=True)
class Plan:  # pylint: disable=too-many-instance-attributes
    """One analysis to run beside the workflow, with the files `write` wrote for it."""

    name: str
    analysis: str
    file_pattern: str
    max_times: int
    interval: float
    cwl: Path
    inputs: Path
    root_dir: Path
    declared_at: str


def write(analyses: tuple[Analysis, ...], basepath: Path, workflow_name: str, root_dir: Path) -> tuple[Plan, ...]:
    """Write each analysis under `<basepath>/realtime/<name>/` and the manifest that lists them.

    With no analyses there is no manifest, and one an earlier compile wrote is removed.

    Args:
        root_dir (Path): The root workflow's directory, where an analysis input that no step
            wrote is looked for, as the main run stages its own inputs.

    Returns:
        The plan of each analysis, which `run_local` is given to run them.
    """
    manifest = manifest_path(basepath, workflow_name)
    if not analyses:
        manifest.unlink(missing_ok=True)
        return ()
    plans = []
    for analysis, cwl in zip(analyses, documents(analyses, basepath)):
        io.write_artifacts_to_disk(analysis.artifact, cwl.parent, True)
        declaration = analysis.declaration
        plans.append(Plan(analysis.name, declaration.analysis, declaration.file_pattern, declaration.max_times,
                          declaration.interval, cwl.absolute(),
                          cwl.with_name(f'{analysis.artifact.name}_inputs.yml').absolute(), root_dir.absolute(),
                          str(declaration.span) if declaration.span else declaration.document))
    manifest.write_text(json.dumps([{
        'name': plan.name,
        'analysis': plan.analysis,
        'file_pattern': plan.file_pattern,
        'max_times': plan.max_times,
        'interval': plan.interval,
        'cwl': str(plan.cwl.relative_to(basepath.absolute())),
        'inputs': str(plan.inputs.relative_to(basepath.absolute())),
        'root_dir': str(plan.root_dir),
        'declared_at': plan.declared_at,
    } for plan in plans], indent=2), encoding='utf-8')
    return tuple(plans)


def without_entrypoints(analyses: tuple[Analysis, ...]) -> tuple[Analysis, ...]:
    """`analyses` naming the no-entrypoint images `--docker_remove_entrypoints` builds."""
    return tuple(replace(analysis, artifact=plugins.dockerPull_append_noentrypoint_artifact(analysis.artifact))
                 for analysis in analyses)


#: The argv that runs an analysis: (its CWL, its job file, the output dir, its own cachedir) -> argv.
Command = Callable[[Path, Path, Path, Path], list[str]]

#: A cwltool job directory's name: the md5 of the job's cache key.
_JOB_KEY: Final = re.compile(r'[0-9a-f]{32}')

#: The stem of an analysis run's output directory, job file and log.
_RUN: Final = re.compile(r'run-(\d+)')


@contextmanager
def watching(plans: tuple[Plan, ...], cachedir: Path, command: Command,
             env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Run a `Watcher` for each plan while the body runs the workflow.

    When the body returns, each watcher does its final run and stops. When it raises,
    Ctrl-C included, each running analysis is terminated before the exception goes on.
    """
    watchers = [Watcher(plan, cachedir, command, env=env) for plan in plans]
    for watcher in watchers:
        watcher.start()
    try:
        yield
        for watcher in watchers:
            watcher.finish()
    except BaseException:
        for watcher in watchers:
            watcher.cancel()
        raise


class Watcher:  # pylint: disable=too-many-instance-attributes
    """Runs one analysis as the files it watches change in this run's cwltool job directories.

    cwltool runs a job in `<cachedir>/<key>/`, recreating that directory when it runs the
    job and leaving it alone on a cache hit, and writes `<cachedir>/<key>.status` when the
    job ends. Against the mtime of a marker touched at `start` (the filesystem's clock, so
    right on NFS), a job directory is *of this run* when its mtime is at or after the
    marker, and *finished* when its status file is too and holds a status. Only this run's
    unfinished directories are searched for `file_pattern`, so a reused cachedir costs one
    `stat` per old entry per poll.

    An analysis runs when a watched file changed and `interval` seconds have passed since
    the last run started; at once when a job holding a watched file finishes; and once more
    when the workflow ends, if anything changed since the last run and no job holding a
    watched file failed. Runs triggered while the workflow runs stop at `max_times - 1`, so
    the final run always fits. A failed analysis is a line, never the workflow's outcome.
    """

    def __init__(self, plan: Plan, cachedir: Path, command: Command, *,
                 env: Mapping[str, str] | None = None, poll: float = 2.0) -> None:
        self.plan = plan
        self._cachedir = cachedir.absolute()
        self._dir = self._cachedir / DIRECTORY / plan.name
        self._command = command
        self._env = dict(env) if env is not None else None
        self._poll = poll
        self._since = 0
        self._ours: set[str] = set()
        self._done: set[str] = set()
        self._failed: list[str] = []
        self._matches: dict[Path, tuple[int, int]] = {}
        self._snapshot: dict[Path, tuple[int, int]] = {}
        self._last_start: float | None = None
        self._reported: set[str] = set()
        self.runs = 0
        self._earlier_runs = 0
        self.succeeded = 0
        self._ending = threading.Event()
        self._cancelled = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f'realtime-{plan.name}', daemon=True)

    def _say(self, message: str) -> None:
        print(f'real-time analysis {self.plan.name} ({self.plan.analysis}): {message}', flush=True)

    def start(self) -> None:
        """Mark the start of this run and start watching. Runs are numbered after an earlier workflow run's."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._earlier_runs = max((int(match[1]) for path in self._dir.iterdir()
                                  if (match := _RUN.fullmatch(path.stem))), default=0)
        marker = self._dir / 'started'
        marker.touch()
        self._since = marker.stat().st_mtime_ns
        self._say(f'runs when files matching {self.plan.file_pattern!r} change, at most {self.plan.max_times} '
                  f'times and at least {self.plan.interval:g} s apart; outputs and logs in {self._dir}')
        self._thread.start()

    def finish(self) -> None:
        """The workflow ended: do the final run if it is due, and stop. Waits for that run."""
        self._ending.set()
        self._wake.set()
        self._thread.join()

    def cancel(self) -> None:
        """Stop now, terminating a running analysis."""
        self._cancelled.set()
        self._wake.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._cancelled.is_set():
            ending = self._ending.is_set()
            finished = self._scan()
            if ending:
                self._final(finished)
                break
            if self.runs < self.plan.max_times - 1:
                if finished:
                    self._run(f'the job that wrote {finished[0].name} finished')
                elif self._changed() and (self._last_start is None
                                          or time.monotonic() - self._last_start >= self.plan.interval):
                    self._run(f'{self._changed()[0].name} changed')
            # Cleared before the flags are read: a finish or cancel after this wakes the wait.
            self._wake.clear()
            if not (self._ending.is_set() or self._cancelled.is_set()):
                self._wake.wait(self._poll)
        self._say(f'{self.runs} runs, {self.succeeded} succeeded')

    def _final(self, finished: list[Path]) -> None:
        if self._failed:
            self._say(f'no final run: the job that wrote {self._failed[0]} failed')
        elif (finished or self._changed()) and self.runs < self.plan.max_times:
            self._run('the workflow finished')

    def _changed(self) -> list[Path]:
        """The watched files that changed since the last run started, by path."""
        return sorted(path for path, stat in self._matches.items() if self._snapshot.get(path) != stat)

    def _job_dirs(self) -> list[os.DirEntry[str]]:
        """Every cwltool job directory in the cachedir, old and new."""
        try:
            with os.scandir(self._cachedir) as entries:
                return [entry for entry in entries if _JOB_KEY.fullmatch(entry.name) and entry.is_dir()]
        except FileNotFoundError:
            return []

    def _scan(self) -> list[Path]:
        """Record the watched files in this run's unfinished jobs; return those of jobs that just succeeded."""
        succeeded: list[Path] = []
        for entry in self._job_dirs():
            try:
                if entry.name in self._done or entry.stat().st_mtime_ns < self._since:
                    continue
            except FileNotFoundError:  # cwltool is recreating it
                continue
            self._ours.add(entry.name)
            status = self._status(entry.name)
            matches = self._match(Path(entry.path))
            if status:
                self._done.add(entry.name)
                if matches and status == 'success':
                    succeeded.extend(matches)
                elif matches:
                    self._failed.append(matches[0].name)
        return succeeded

    def _status(self, key: str) -> str:
        """The status cwltool wrote for job `key` in this run; empty while it runs."""
        path = self._cachedir / f'{key}.status'
        try:
            if path.stat().st_mtime_ns < self._since:
                return ''
            return path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            return ''

    def _match(self, job_dir: Path) -> list[Path]:
        """Record each file in `job_dir` that `file_pattern` matches, and return them."""
        found = []
        try:
            for path in job_dir.rglob(self.plan.file_pattern):
                stat = path.stat()
                if path.is_file():
                    self._matches[path] = (stat.st_size, stat.st_mtime_ns)
                    found.append(path)
        except FileNotFoundError:  # the job is being recreated or its files moved
            pass
        return sorted(found)

    def _run(self, reason: str) -> None:
        self._last_start = time.monotonic()
        job = self._job()
        if job is None:
            return
        self._snapshot = dict(self._matches)
        self.runs += 1
        number = self._earlier_runs + self.runs
        run = self._dir / f'run-{number:03d}'
        run.with_suffix('.yml').write_text(json.dumps(job, indent=2), encoding='utf-8')
        log = run.with_suffix('.log')
        argv = self._command(self.plan.cwl, run.with_suffix('.yml'), run, self._dir / 'cache')
        with log.open('w', encoding='utf-8') as stream, \
                sub.Popen(argv, stdout=stream, stderr=sub.STDOUT, cwd=self._dir, env=self._env) as child:
            code = self._wait(child)
        if code is None:
            self._say(f'run {number} ({reason}) cancelled; log: {log}')
        elif code == 0:
            self.succeeded += 1
            self._say(f'run {number} ({reason}) finished in {time.monotonic() - self._last_start:.0f} s')
        else:
            self._say(f'run {number} ({reason}) failed with exit code {code}; log: {log}')

    def _wait(self, child: 'sub.Popen[bytes]') -> int | None:
        """The child's exit code, or None after terminating it on `cancel`."""
        while True:
            try:
                return child.wait(timeout=0.2)
            except sub.TimeoutExpired:
                if self._cancelled.is_set():
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except sub.TimeoutExpired:
                        child.kill()
                        child.wait()
                    return None

    def _job(self) -> Yaml | None:
        """The analysis's job, each File pointed at the file it names; None if one is nowhere."""
        job = yaml.safe_load(self.plan.inputs.read_text(encoding='utf-8')) or {}
        missing: list[str] = []

        def resolve(value: Any) -> Any:
            match value:
                case {'class': 'File', 'location': str() as location}:
                    found = self._find(location)
                    if found is None:
                        missing.append(location)
                        return value
                    return {**{key: item for key, item in value.items() if key != 'path'}, 'location': str(found)}
                case dict():
                    return {key: resolve(item) for key, item in value.items()}
                case list():
                    return [resolve(item) for item in value]
            return value

        resolved: Yaml = resolve(job)
        for location in missing:
            self._once(location, f'skipped a run: no job of this run wrote {Path(location).name}, no earlier '
                       f'job in {self._cachedir} did, and it is not in {self.plan.root_dir} '
                       f'(declared at {self.plan.declared_at})')
        return None if missing else resolved

    def _find(self, location: str) -> Path | None:
        """Where the file the job names as `location` is, newest first: this run's jobs, earlier jobs, the project."""
        basename = Path(location).name
        ours = [path for key in sorted(self._ours) for path in (self._cachedir / key).rglob(basename)
                if path.is_file()]
        jobs = sorted({path.relative_to(self._cachedir).parts[0] for path in ours})
        if len(jobs) > 1:
            self._once(basename, f'{basename} was written by {len(jobs)} jobs of this run ({", ".join(jobs)}); '
                       'using the newest')
        if not ours:
            ours = [path for entry in self._job_dirs() if entry.name not in self._ours
                    for path in Path(entry.path).rglob(basename) if path.is_file()]
        if ours:
            return max(ours, key=lambda path: path.stat().st_mtime_ns)
        project = self.plan.root_dir / location
        return project if project.is_file() else None

    def _once(self, key: str, message: str) -> None:
        if key not in self._reported:
            self._reported.add(key)
            self._say(message)
