"""The sites, `sophios.sites.Machine` and `Slurm`, walked through with a local `bash` for `ssh [user@]host`, and on
Slurm a fake Slurm (`fake_slurm`): the two things these tests replace."""
# pylint: disable=protected-access  # the walks read the package's own scripts, records and states
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sysconfig
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

import sophios
from sophios import sites, targets
from sophios.api.python._inference import Inference
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.lang.diagnostics import Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode as Code
from sophios.runtime import backends
from sophios.runtime.runners import Cwltool, Nextflow, Runner, SlurmSettings, Toil
from sophios.runtime.status import RunState
from sophios.sites import RunConfig, _run, _site, _ssh, _states
from sophios.sites._states import Decision
from sophios.wic_types import TargetName

from . import fake_slurm
from .helpers import adapter_step, live_workflow, observed
from .test_sites_unit import _HOSTILE, _HOSTILE_FOLDER, _gpu, _local, _step
from .test_sites_unit import _fake_slurm  # pylint: disable=unused-import  # the fixture `slurm`

#: A runs folder, in the project folder there, of every kind of character a site takes in `runs` and `bin`.
_RUNS = 'Runs.of_a-site+0/runs'
_COPY = partial(_step, 'copy', 'cat', source='data/in.txt')
_Kind = type[sites.Machine | sites.Slurm]


def _end(folder: Path) -> None:
    """End every process group whose command line names `folder`: a run's leader, its runner, and Toil's workers."""
    listed = subprocess.run(['ps', '-ax', '-o', 'pgid=,args='], capture_output=True, text=True, check=False).stdout
    groups = {int(line.split(None, 1)[0]) for line in listed.splitlines() if f'{folder}/' in line}
    for group in groups - {os.getpgid(0)}:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:   # it ended meanwhile
            pass


@pytest.fixture(name='site')
def _site_folder(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
                 request: pytest.FixtureRequest) -> Iterator[Path]:
    """The project folder there: `data/in.txt`, an empty `results/`, and `bin/`, Nextflow in it only for a test marked
    `nextflow`; the working directory here holds no data, a status is read every half second, and every process ends."""
    monkeypatch.chdir(tmp_path_factory.mktemp('here'))
    monkeypatch.setattr(_run, 'POLL', 0.5)
    for folder in ('data', 'results', 'bin'):
        (tmp_path / folder).mkdir()
    (tmp_path / 'data' / 'in.txt').write_text('hello\n', encoding='utf-8')
    for name in (Toil.name, Cwltool.name):
        (tmp_path / 'bin' / name).symlink_to(Path(sysconfig.get_path('scripts')) / name)
    if request.node.get_closest_marker('nextflow'):
        (tmp_path / 'bin' / Nextflow.name).symlink_to(shutil.which(Nextflow.name) or Nextflow.name)
    try:
        with _local():
            yield tmp_path
    finally:
        _end(tmp_path)


def _settled(slurm: Path) -> list[dict[str, Any]]:
    """The fake Slurm's jobs, once each has ended, within a minute."""
    for _ in range(600):
        if all('end' in job for job in fake_slurm.jobs(slurm)):
            return fake_slurm.jobs(slurm)
        time.sleep(0.1)
    raise AssertionError(f'a job of the fake Slurm has not ended: {fake_slurm.jobs(slurm)}')


def _at(kind: _Kind, site: Path, **settings: Any) -> sites.Machine | sites.Slurm:
    """A site of `kind` there, its `runs` and `bin` in the project folder unless `settings` say."""
    return kind('ssh://localhost', **{'runs': str(site / 'runs'), 'bin': str(site / 'bin'), **settings})


_machine, _slurm_site = partial(_at, sites.Machine), partial(_at, sites.Slurm)


def _fill(folder: str | list[str]) -> Step:
    """`fill`, given the output folder `folder`, which it stages writable, writes `made.txt` into, and returns."""
    inputs = Inputs(output=Input(cwl.directory, position=1))
    fill = Step(CommandLineTool('fill', inputs, Outputs(output=Output(cwl.directory, glob='$(inputs.output.basename)')))
                .base_command('sh', '-c', 'echo filled > "$0/made.txt"').stage(inputs.output, writable=True),
                step_name='fill')
    fill.inputs.output = folder
    return fill


def _filled(folder: str = 'masks') -> Workflow:
    """`copy` of `data/in.txt`, and `fill`, given the output folder `folder`."""
    workflow, fill = _COPY(), _fill(folder)
    filled = Workflow([*workflow.steps, fill], 'site')
    filled.outputs.out, filled.outputs.masks = workflow.steps[0].outputs.out, fill.outputs.output
    return filled


def _scattered(folders: list[str]) -> Workflow:
    """`fill` scattered over its output folders, `folders`."""
    fill = _fill(folders)
    fill.scatter = [fill.inputs.output]
    scattered = Workflow([fill], 'site')
    scattered.outputs.masks = fill.outputs.output
    return scattered


def _touch() -> Workflow:
    """A workflow of one step that runs in a container."""
    return Workflow([adapter_step('touch', filename='empty.txt')], 'site')


def _gnu_stat(site: Path) -> Path:
    """A folder there holding a `stat` that reads as GNU's does on this computer, and misses `exit` once after it is
    there, as when a run ends between a status's two looks."""
    real = shutil.which('stat') or 'stat'
    gnu = subprocess.run([real, '-c', '%Y', '.'], capture_output=True, check=False).returncode == 0
    (site / 'gnu').mkdir()
    (site / 'gnu' / 'stat').write_text(f'''#!/bin/sh
if [ "$1" = -c ] && [ "$3" = exit ] && [ -e exit ] && [ ! -e missed ]; then : > missed; exit 1; fi
if [ "$1" = -c ]; then exec {real} {'-c %Y' if gnu else '-f %m'} "$3"; fi
if [ -e "$3" ]; then printf '  File: "%s"\\n    ID: 0 Namelen: 255\\n' "$3"; fi
exit 1
''', encoding='utf-8')
    (site / 'gnu' / 'stat').chmod(0o755)
    return site / 'gnu'


def _answering(folder: Path, status: int = 0, **said: str) -> None:
    """In `folder`, a program of each name (`toil` for Toil's) that prints what it is given to say, and exits
    `status`."""
    folder.mkdir(exist_ok=True)
    for name, line in said.items():
        program = folder / (Toil.name if name == 'toil' else name)
        program.write_text(f'#!/bin/sh\necho {shlex.quote(line)}\nexit {status}\n', encoding='utf-8')
        program.chmod(0o755)


def _job_line(site: Path, runner: Runner, slurm: bool = False, **engine: str) -> tuple[list[str], str, Any]:
    """The runner's line of the `job.sh` a machine, or with `slurm` a Slurm site, writes for `runner`, the run's id
    written `<id>`, with runners that answer in `bin`; the configuration it writes for Nextflow; and the run's record.
    On Slurm a step asks for a GPU and your `SBATCH_ACCOUNT` names an account, so the site sets every option it may."""
    _answering(site / 'fakes', toil='toil-cwl-runner 9.5.0', cwltool='/x/cwltool 3.1.20241217163858',
               nextflow='nextflow version 26.4.2.1')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('SBATCH_ACCOUNT', 'lab')
        config = RunConfig('results').relative_to(str(site)).slurm_settings(SlurmSettings(time='1:00:00'))
        found, record, files = (_slurm_site if slurm else _machine)(site, bin=str(site / 'fakes'), **engine)._prepare(
            _COPY(tool=_gpu if slurm else lambda tool: tool), config, runner.target, runner, None, Inference())
    assert files, found
    line = [word.replace(record['id'], '<id>') for word in shlex.split(files['job.sh'].splitlines()[-1])]
    return line[1:line.index('|')], files.get('nextflow_site/nextflow.config', ''), record


#: Each runner on each site, and flags of the user's that no site sets: cwltool's own `--disable-pull` and `--quiet`
#: among them.
_PASSED = [(on_slurm, kind, flags) for on_slurm in (False, True) for kind, flags in [
    (Cwltool, ('--disable-pull', '--quiet', '--timestamps')), (Toil, ('--targetTime', '1', '--disable-pull')),
    (Nextflow, ('-resume', '-name', 'x'))]]


@pytest.mark.parametrize(('on_slurm', 'kind', 'flags'), _PASSED)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments  # a row, and the site
def test_a_site_refuses_the_options_its_job_sets_and_puts_every_other_flag_into_it(
        site: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, on_slurm: bool,
        kind: type[Runner], flags: tuple[str, ...]) -> None:
    """Every option of the job line, run from `bin`, and of Nextflow's block, found from what the backend launches, is
    refused but those the site reads; the user's flags go in whole and in order; on Slurm cwltool's is a machine's."""
    if on_slurm:
        request.getfixturevalue('slurm')
    machine = (sites.Slurm if on_slurm else sites.Machine)('ssh://h', runs='/r')

    def refused(option: str) -> bool:
        try:
            machine._read(kind(option, '1'))
        except ValueError as error:
            return f'sets {option} itself' in str(error)
        return False
    line, config, _ = _job_line(site, kind(), on_slurm)
    assert line[0] == f'{site}/fakes/{kind.name}'
    assert not (on_slurm and kind is Cwltool) or line == _job_line(site, kind(), container_engine='singularity')[0]
    options = {word.partition('=')[0] for word in line[1:] if word.startswith('-')}
    block = {f'-{scope}.{name}' for scope, settings in re.findall(r'^(\w+) \{\n((?:    .*\n)+)\}$', config, re.M)
             for name in re.findall(r'^    (\w+) =', settings, re.M)}
    assert {option for option in options | block if refused(option)} == (options | block) - {'--maxJobs', '--clean'}
    assert not refused('--brandNew') and not refused('-process.brandNew')
    with_flags, _, record = _job_line(site, kind(*flags), on_slurm)
    assert (record['flags'], record['sophios'], record['site']) == (list(flags), sophios.__version__,
                                                                    type(machine).__name__)
    assert any(with_flags[at:at + len(flags)] == list(flags) and with_flags[:at] + with_flags[at + len(flags):] == line
               for at in range(len(with_flags)))
    launch = type(targets.backend(kind())).launch
    monkeypatch.setattr(type(targets.backend(kind())), 'launch', lambda self, request: backends.Launch(
        (*launch(self, request).argv, '--brandNew'), {}, None, ''))
    monkeypatch.setattr(type(machine), '_block', lambda self, run, read, runner: {'process': {'brandNew': '1'}})
    assert refused('--brandNew') and refused('-process.brandNew')


@pytest.mark.fast
@pytest.mark.needs_cwltool
@pytest.mark.parametrize(('site_kind', 'kind'), [
    *((site_kind, kind) for site_kind in (sites.Machine, sites.Slurm) for kind in (Toil, Cwltool)),
    pytest.param(sites.Machine, Nextflow, marks=pytest.mark.nextflow)])
def test_each_name_a_runner_gives_an_option_the_site_sets_or_reads_is_that_option(tmp_path: Path, site_kind: _Kind,
                                                                                  kind: type[Runner]) -> None:
    """Every name the runner's own parser (`nextflow run -h` for Nextflow) gives an option a site refuses or reads is
    refused or read as that option, on each kind of site."""
    machine = site_kind('ssh://h', runs='/r')

    def taken(name: str) -> bool:
        try:
            _, read = machine._read(kind(name, '1'))
        except ValueError as error:
            return 'a site sets' in str(error)
        return any(source == name for _, source in read.values())
    if kind is Nextflow:
        said = subprocess.run([shutil.which(Nextflow.name) or Nextflow.name, 'run', '-h'], capture_output=True,
                              text=True, check=True, cwd=tmp_path).stdout   # where it writes its log
        names = [found.split(', ') for found in re.findall(r'^ {4}(-[\w.-]+(?:, -[\w.-]+)*)\s*$', said, re.M)]
    elif kind is Toil:
        from toil.common import addOptions  # pylint: disable=import-outside-toplevel  # it imports `pwd`
        from toil.cwl.cwltoil import ArgParser  # pylint: disable=import-outside-toplevel  # as above
        parser = ArgParser(allow_abbrev=False)   # Toil's options, as toil-cwl-runner adds them
        addOptions(parser, jobstore_as_flag=True, cwl=True)
        names = [list(action.option_strings) for action in parser._actions]
    else:
        from cwltool.argparser import arg_parser  # pylint: disable=import-outside-toplevel  # it imports `pwd`
        names = [list(action.option_strings) for action in arg_parser()._actions]
    groups = [group for group in names if any(map(taken, group))]
    assert groups and all(all(map(taken, group)) for group in groups), groups


def _taken(site: Path, ended: bool, made: bool = False) -> None:
    """A run there, unclaimed, ended or submitted a moment ago, whose `out` links to `results/p1`, made when `made`."""
    if made:
        (site / 'results' / 'p1').mkdir()
    other = site / 'runs' / 'site-20261109T000000-00000001'
    other.mkdir(parents=True)
    (other / 'run.json').write_text(json.dumps({'id': other.name, 'outdir': [str(site / 'results' / 'p1'), '']}))
    (other / 'out').symlink_to(site / 'results' / 'p1')
    if ended:
        (other / 'exit').write_text('1\n')


_MISSING, _NOT_WRITABLE = Code.MISSING_INPUT_FILE, Code.DIRECTORY_NOT_WRITABLE
#: What a check finds, by code, for a workflow, target, outputs folder in the project folder there, site settings
#: (`bin` and `runs` in the project folder; on Slurm `kind`, `broken` and `env`) and change made to the site; and a word
#: of a message where codes are shared.
_PROBLEMS: list[Any] = [
    (partial(_step, 'copy', 'cat', source='data/gone.txt'), 'cwl', 'results', {'bin': 'data/empty'},
     lambda s: (s / 'results' / 'x').touch(), [_MISSING, Code.SITE_RUNNER, _NOT_WRITABLE], ''),
    *((partial(_step, 'copy', 'cat', source=path), 'cwl', 'results', {}, None, codes, '')
      for path, codes in [('~/in.txt', [_MISSING]), ('$HOME/in.txt', [_MISSING]), ('data/../data/in.txt', [])]),
    (_COPY, 'cwl', None, {}, None, [_MISSING], 'relative_to'),
    (partial(live_workflow, 'site'), 'cwl', None, {'container_engine': 'no-such-engine'}, None,
     [Code.REALTIME_NOT_ASYNC, Code.CONTAINER_ENGINE_UNAVAILABLE], ''),
    *((made, 'nextflow', None, {'bin': 'data/empty', 'container_engine': engine}, None, [Code.SITE_RUNNER, *refused],
       '') for made, engine, refused in ((_touch, 'podman', [Code.SITE_NEXTFLOW_CONTAINER]),
                                         (partial(_step, 'say', 'true'), 'podman', []),
                                         (_touch, 'docker', []), (_touch, 'singularity', []))),
    *((partial(_filled, folder), target, 'results', {}, None, codes, said) for target in ('cwl', 'nextflow')
      for folder, codes, said in [
          ('masks', [], ''), *((name, [_MISSING], 'not one folder name') for name in ('seg/p1', '..', 'masks/')),
          *((path, [_MISSING], 'the path') for path in ('/lab/masks', 'file:///lab/masks'))]),
    *((partial(_scattered, folders), target, 'results', {}, None, [_MISSING] * 2, said)
      for target in ('cwl', 'nextflow')
      for folders, said in [(['m1', 'm2'], 'in a list'), (['/l/1', '/l/2'], 'the path')]),
    (partial(_step, 'list', 'ls', source='data/../data', kind='Directory'), 'cwl', 'data/../data/masks', {}, None,
     [_NOT_WRITABLE], 'the folder input'),
    (partial(_step, 'copy', 'cat', source='/etc/hosts'), 'cwl', None, {'bin': 'data/empty'},
     lambda s: _answering(s / 'data' / 'empty', uname='CYGWIN_NT', toil='toil-cwl-runner 9.4.9'),
     [Code.SITE_UNREACHABLE, Code.SITE_RUNNER], 'is 9.4.9'),
    (_touch, 'cwl', None, {'bin': 'data/empty', 'container_engine': 'broken'},
     lambda s: _answering(s / 'data' / 'empty', 1, broken='daemon down', toil='no Java'),
     [Code.SITE_RUNNER, Code.CONTAINER_ENGINE_UNAVAILABLE], '(no Java)'),
    (_COPY, 'cwl', 'results', {}, lambda s: (s / 'data' / 'in.txt').chmod(0), [_MISSING], 'may not read'),
    *((_COPY, 'cwl', folder, {}, partial(_taken, ended=ended), [], '')
      for folder, ended in [('results/p1', True), ('results/p2', False)]),
    *((_COPY, 'cwl', folder, settings, setup, [_NOT_WRITABLE], said) for folder, settings, setup, said in [
        ('nowhere/p1', {}, None, 'does not exist'),
        ('results/p1', {}, lambda s: (s / 'results' / 'p1').touch(), 'exists and is not'),
        ('results/p1', {}, partial(_taken, ended=False), 'is used by run'),
        ('results', {}, partial(_taken, ended=False), 'is used, around'),
        ('results/p1/q', {}, partial(_taken, ended=False, made=True), 'is used, inside'),
        ('results/p1', {}, lambda s: (s / 'results').chmod(0o500), 'you can write'),
        ('results/link', {}, lambda s: (s / 'results' / 'link').symlink_to(s / 'bin'), 'a link'),
        ('results', {}, lambda s: (s / 'runs').mkdir(mode=0o500), 'its runs'),
        ('runs/.images', {}, lambda s: (s / 'runs').mkdir(), 'runs='),
        ('results', {'runs': 'results/runs'}, None, 'runs=')]),
    (_COPY, 'cwl', 'results', {'kind': sites.Slurm, 'bin': 'data/empty', 'broken': 'sacct', 'env': {
        'SBATCH_CLUSTERS': 'other', 'SLURM_CLUSTERS': 'other', 'SBATCH_TIMELIMIT': '0', 'SBATCH_ACCOUNT': "a'b\nc"}},
     None, [Code.SITE_RUNNER, *[Code.SITE_SLURM] * 5], 'sacct'),
    (_COPY, 'cwl', 'results', {'kind': sites.Slurm, 'broken': 'sinfo'}, None, [Code.SITE_SLURM], 'sinfo')]
#: The one row whose run needs Nextflow in `bin`, which only the lane that runs `nextflow` tests installs.
_PROBLEMS = [pytest.param(*row, marks=pytest.mark.nextflow) if row[1] == 'nextflow' and not row[5] else row
             for row in _PROBLEMS]


@pytest.mark.parametrize(('workflow', 'target', 'folder', 'settings', 'setup', 'codes', 'said'), _PROBLEMS)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # a row, and the site
def test_check_finds_every_problem_at_once_and_writes_nothing_and_submit_raises_them(
        site: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, workflow: Callable[[], Workflow],
        target: Any, folder: str | None, settings: dict[str, Any], setup: Callable[[Path], object] | None,
        codes: list[Code], said: str) -> None:
    """Every problem of the workflow, config, runner, engine, site and its Slurm at once, each by its code; neither side
    changes, nothing is submitted, and `submit` raises the same. A path's `..` is read by name, as the runner does."""
    (site / 'data' / 'empty').mkdir()
    for shell in ('~', '$HOME'):   # each path a site refuses names a file that is there
        (site / shell).mkdir()
        (site / shell / 'in.txt').touch()
    _answering(site / 'data' / 'empty', docker='', singularity='')   # an engine that answers, in an empty `bin`
    options = dict(settings)
    state = request.getfixturevalue('slurm') if options.pop('kind', None) is sites.Slurm else None
    if state is not None:
        (state / 'broken').write_text(options.pop('broken', ''), encoding='utf-8')
    for name, value in options.pop('env', {}).items():
        monkeypatch.setenv(name, value)
    machine = (_machine if state is None else _slurm_site)(site, **{
        name: str(site / value) if name in ('bin', 'runs') else value for name, value in options.items()})
    if setup is not None:
        setup(site)
    config = None if folder is None else RunConfig(folder).relative_to(str(site))
    made, before = workflow(), sorted(site.rglob('*'))
    found = machine.check(made, config, target=target)
    assert sorted(d.code for d in found if d.severity is Severity.ERROR) == sorted(codes), found
    assert said in ' '.join(d.message for d in found)
    if codes:
        with pytest.raises(SophiosError) as caught:
            machine.submit(made, config, target=target)
        assert sorted(d.code for d in caught.value.diagnostics if d.severity is Severity.ERROR) == sorted(codes)
    assert sorted(site.rglob('*')) == before and not (state and fake_slurm.jobs(state))


def _linked(site: Path) -> None:
    """`results` made a link to an empty folder."""
    (site / 'void').mkdir()
    (site / 'results').rmdir()
    (site / 'results').symlink_to(site / 'void')


@pytest.mark.parametrize('kind', [sites.Machine, sites.Slurm])
@pytest.mark.parametrize('late', [lambda site: (site / 'results' / 'late.txt').touch(), _linked])
def test_an_outputs_folder_must_still_be_missing_or_empty_when_the_run_starts(
        site: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, late: Callable[[Path], None],
        kind: _Kind) -> None:
    """Written into, or made a link, between the check and the start: the start refuses it (`wic062`) before it makes
    the run's folder, submits nothing, and says how to follow the run should it have started all the same."""
    slurm = request.getfixturevalue('slurm') if kind is sites.Slurm else None
    start = _site._Site._start

    def later(self: _site._Site, run: str, record: Any, files: Any) -> str:
        late(site)
        return start(self, run, record, files)
    monkeypatch.setattr(_site._Site, '_start', later)
    with pytest.raises(SophiosError) as caught:
        _at(kind, site).submit(_COPY(), RunConfig('results').relative_to(str(site)))
    [failed, *_, follow] = caught.value.diagnostics
    assert (failed.code, follow.code) == (Code.SITE_UNREACHABLE,) * 2 and 'no longer' in failed.message
    assert f'{kind.__name__}(' in follow.message and '.attach(' in follow.message and not (site / 'runs').exists()
    assert not list(Path.cwd().iterdir()) and not (slurm and fake_slurm.jobs(slurm))


def test_of_two_runs_started_into_one_outputs_folder_one_takes_it_and_the_other_never_starts(
        site: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Of two submits into one folder, the later to start finds it taken (`wic021`) and ends FAILED, never started; the
    other's outputs stay until a status stops it; a machine names no Slurm, and Slurm(...) will not attach its runs."""
    config, start, taken, sent = RunConfig('results/shared').relative_to(str(site)), _site._Site._start, [], []

    def call(url: str, script: str) -> bytes:   # every script a machine sends, kept
        sent.append(script)
        return _ssh._call(url, script)
    monkeypatch.setattr(_site, '_call', call)

    def later(self: _site._Site, run: str, record: Any, files: Any) -> str:
        monkeypatch.setattr(_site._Site, '_start', start)   # the other run starts as any does
        taken.append(_machine(site).submit(_COPY(), config))
        return start(self, run, record, files)
    monkeypatch.setattr(_site._Site, '_start', later)
    with pytest.raises(SophiosError) as caught:
        _machine(site).submit(_step('fail', 'sh', '-c', 'exit 3'), config)
    [other], [used] = taken, caught.value.diagnostics
    assert used.code is Code.DIRECTORY_NOT_WRITABLE and f'by run {other.id}, which has not ended' in used.message
    [never] = [folder.name for folder in (site / 'runs').iterdir() if folder.name != other.id]
    assert os.readlink(site / 'runs' / never / 'leader') == 'none'
    assert other.wait(timeout=120).state is RunState.COMPLETED, other.logs()
    ended = _machine(site).attach(never).status()
    assert (ended.state, ended.outputs) == (RunState.FAILED, None) and 'taken by another run' in str(ended.reason)
    assert os.listdir(site / 'results' / 'shared') == ['out.txt'] and not (site / 'runs' / never / 'exit').exists()
    (site / 'runs' / other.id / 'reason').write_text('stopped')   # as a status that stopped it as it exited 0
    late = _machine(site).attach(other.id).status()
    assert (late.state, late.reason, late.exit_code, late.outputs) == (RunState.FAILED, 'stopped', 0, None)
    assert not [script for script in sent if any(name in script for name in ('SBATCH', 'sbatch', 'squeue', 'sacct',
                                                                             'sinfo', 'scancel', 'slurm'))]
    with pytest.raises(SophiosError, match=re.escape('attach to it with Machine(')) as refused:
        sites.Slurm('ssh://localhost', runs=str(site / 'runs')).attach(other.id)
    assert [d.code for d in refused.value.diagnostics] == [Code.SITE_RUN_UNKNOWN]


def test_every_value_a_user_gives_reaches_the_runner_as_one_word(site: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each value a user gives, `_HOSTILE`, reaches the runner as one word and none runs; the job's environment is the
    site's; and a run is RUNNING by its own id only."""
    (site / '.bash_profile').write_text('export TOIL_SETTING_OF_YOURS=1 PYTHONPATH=/yours\n', encoding='utf-8')
    monkeypatch.setenv('HOME', str(site))
    there, recording = site / _HOSTILE, site / 'recording'
    for made in (there / 'data', there / 'out', there / 'other', recording):
        made.mkdir(parents=True)
    (there / 'data' / f'{_HOSTILE}.txt').write_text('hello\n', encoding='utf-8')
    (recording / Toil.name).write_text('#!/bin/sh\n[ "$1" = --version ] && { echo 9.5.0; exit 0; }\n'
                                       'printf "%s\\0" "$@" > argv\nenv -0 > env\n'
                                       'while [ ! -e go ]; do sleep 0.1; done\n', encoding='utf-8')
    (recording / Toil.name).chmod(0o755)
    machine = _machine(site, bin=str(recording), container_engine=_HOSTILE)
    workflow = _step('copy', 'cat', source=f'data/{_HOSTILE}.txt')
    config = RunConfig(f'out/{_HOSTILE_FOLDER}').relative_to(str(there))
    run = machine.submit(workflow, config, runner=Toil('--writeLogs', _HOSTILE))   # its check finds no problem
    folder = site / 'runs' / run.id
    while not (folder / 'env').exists():   # the recording runner writes it after argv
        assert run.status().state in (RunState.QUEUED, RunState.RUNNING), run.logs()
        time.sleep(0.1)
    assert run.status().state is RunState.RUNNING   # its leader is found by the run's id, which ps prints as it is
    with pytest.raises(TimeoutError, match=run.id):
        run.wait(timeout=0.5)
    other = site / 'runs' / f'{run.id[:-8]}{"0" * 8 if run.id[-8:] != "0" * 8 else "1" * 8}'
    other.mkdir()
    (other / 'run.json').write_text(json.dumps({**run._record, 'id': other.name,
                                                'outdir': [str(there / 'other'), "the run's own folder"]}))
    (other / 'leader').symlink_to(os.readlink(folder / 'leader'))   # a pid that runs another run's job
    assert machine.attach(other.name).status().state is RunState.FAILED
    (folder / 'go').touch()
    assert run.wait(timeout=60).state is RunState.COMPLETED, run.logs()
    env = dict(line.split('=', 1) for line in (folder / 'env').read_text(encoding='utf-8').split('\0') if '=' in line)
    assert (env['PYTHONNOUSERSITE'], env['TOIL_HISTORY']) == ('1', 'False') and env['PATH'].startswith(f'{recording}:')
    assert not {name for name in env if name.startswith(('TOIL_SETTING', 'PYTHONPATH'))}
    argv = (folder / 'argv').read_text(encoding='utf-8').split('\0')[:-1]
    assert argv[argv.index('--outdir') + 1] == str(there / 'out' / _HOSTILE_FOLDER)
    assert argv[argv.index('--user-space-docker-cmd') + 1] == _HOSTILE
    assert argv[-4:] == ['--writeLogs', _HOSTILE, f'{folder}/site.cwl', f'{folder}/site_inputs.yml']
    assert not list(site.rglob('PWNED')) and not list(Path.cwd().rglob('PWNED'))


@pytest.mark.parametrize('runner', [Toil(), Cwltool(), pytest.param(Nextflow(), marks=pytest.mark.nextflow)])
def test_under_singularity_a_machine_writes_each_runners_engine_and_image_cache(
        site: Path, monkeypatch: pytest.MonkeyPatch, runner: Runner) -> None:
    """On a Singularity machine every runner gets the engine and an image cache under `<runs>/.images`, Singularity's
    own unless your profile names one; the run's files are sent as the backend wrote them, a carriage return too."""
    images = site / 'runs' / '.images'
    backend = type(targets.backend(Cwltool() if runner.target == 'cwl' else runner))
    write = backend.write

    def written(self: Any, lowered: Any, analyses: Any, basepath: Path, **settings: Any) -> Any:
        (basepath / 'crlf.txt').write_bytes(b'a\rb\r\n')
        return write(self, lowered, analyses, basepath, **settings)
    monkeypatch.setattr(backend, 'write', written)
    _, _, files = _machine(site, container_engine='singularity')._prepare(
        _COPY(), RunConfig('results').relative_to(str(site)), runner.target, runner, None, Inference())
    job = files['job.sh']
    assert f'export SINGULARITY_CACHEDIR=${{SINGULARITY_CACHEDIR:-{images}/cache}}' in job
    assert f'export APPTAINER_CACHEDIR=${{APPTAINER_CACHEDIR:-{images}/cache}}' in job
    if isinstance(runner, Nextflow):
        assert f'export NXF_SINGULARITY_CACHEDIR={images}/nextflow' in job and 'CWL_SINGULARITY' not in job
        assert files['nextflow_site/nextflow.config'].startswith('singularity { enabled = false; autoMounts = true }')
    else:
        assert f'export CWL_SINGULARITY_CACHE={images}' in job and ' --singularity ' in job
    assert json.loads(files['run.json'])['outdir'] == [str(site / 'results'), 'RunConfig(...)']
    assert files['crlf.txt'] == 'a\rb\r\n'


#: What a run of each runner below leaves in its folder (`docs/sites.md`, The run's folder); Toil on Slurm keeps its
#: steps' Slurm logs, and no provenance, which it writes under no batch system but its own.
_LEFT = {
    Toil.name: ['exit', 'job.sh', 'leader', 'out', 'provenance', 'run.json', 'run.log', 'site.cwl', 'site_inputs.yml',
                'stdout', 'toil'],
    Cwltool.name: ['exit', 'job.sh', 'leader', 'out', 'output_site.json', 'provenance', 'run.json', 'run.log',
                   'site.cwl', 'site_inputs.yml', 'stdout'],
    Nextflow.name: ['exit', 'job.sh', 'leader', 'made', 'nextflow_site', 'out', 'run.json', 'run.log', 'stdout'],
    f'{Toil.name} on Slurm': ['batch', 'exit', 'job.sh', 'leader', 'out', 'run.json', 'run.log', 'site.cwl',
                              'site_inputs.yml', 'stdout', 'toil']}
_TOIL = Toil('--maxJobs=3', '--clean', 'onSuccess', '--targetTime', '1')
#: Each runner on each site, with flags a site reads, in both spellings, and one it passes on; on Slurm under cwltool,
#: the steps' settings in forms Slurm refuses, which its run does not use.
_EACH = [pytest.param(kind, runner, settings, id=f'{kind.__name__}-{runner.name}',
                      marks=[pytest.mark.nextflow] if isinstance(runner, Nextflow) else [])
         for kind, runner, settings in [
             *((sites.Machine, runner, SlurmSettings()) for runner in (_TOIL, Cwltool('--timestamps'),
                                                                       Nextflow('-qs', '3', '-resume'))),
             (sites.Slurm, _TOIL, SlurmSettings()),
             (sites.Slurm, Cwltool('--timestamps'), SlurmSettings(steps_partition='no where', steps_time='never')),
             (sites.Slurm, Nextflow('-qs', '3', '-resume'), SlurmSettings())]]


@pytest.mark.parametrize(('kind', 'runner', 'settings'), _EACH)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # each side, and what each left
def test_each_runner_returns_on_each_site_what_it_returns_in_a_local_run(
        site: Path, request: pytest.FixtureRequest, capsys: pytest.CaptureFixture[str], kind: _Kind, runner: Runner,
        settings: SlurmSettings) -> None:
    """One workflow run by each runner on each site, from `_RUNS`, and here returns the same outputs in the same places;
    what `submit` prints, the run's folder and its job say what was asked, and where it came from."""
    on_slurm = kind is sites.Slurm and bool(request.getfixturevalue('slurm'))
    workflow, there, toil = _filled(), site / 'results' / 'site', isinstance(runner, Toil)
    target: TargetName = 'nextflow' if isinstance(runner, Nextflow) else 'cwl'
    config = RunConfig('results/site').relative_to(str(site)).log_level('DEBUG').slurm_settings(settings)
    machine = _at(kind, site, runs=str(site / _RUNS))
    run = machine.submit(workflow, config, target=target, runner=runner)
    parallel = '' if isinstance(runner, Cwltool) else f', at most 3 at once ({runner.flags[0].partition("=")[0]})'
    printed = capsys.readouterr()   # on Slurm, where its jobs go and the job Slurm has too
    assert [line for line in printed.out.splitlines() if not line.startswith('Slurm')][:3] == [
        f'Submitting {run.id} to localhost: {shlex.join([runner.name, *runner.flags])} (runner=){parallel}.',
        f'Outputs: {there} (RunConfig(...))', f'From anywhere, {run!r} follows it.']
    assert printed.err.count('wic066') == (2 if toil or (on_slurm and target == 'nextflow') else 0)   # at each step
    status, folder = run.wait(timeout=600), site / _RUNS / run.id
    assert status.state is RunState.COMPLETED and status.outputs is not None, run.logs()
    assert run.logs() == (folder / 'run.log').read_text(encoding='utf-8')
    assert status.started_at is not None and status.ended_at is not None and status.started_at <= status.ended_at
    local = workflow.run(config=RunConfig('results/here').relative_to(str(site)), runner=runner, target=target,
                         basepath=str(site / 'here'))
    assert local is not None and local.outputs is not None
    placed = [{name: (observed(value), str(Path(value.get('path') or unquote(urlsplit(value['location']).path))
                                           .relative_to(outdir))) for name, value in outputs.items()}
              for outputs, outdir in ((status.outputs, there), (local.outputs, local.outdir))]
    assert placed[0] == placed[1] and {name: where for name, (_, where) in placed[0].items()} == {
        'out': 'out.txt', 'masks': 'masks'}
    assert repr(run) == f'{machine!r}.attach({run.id!r})' and machine.attach(run.id).status() == status
    assert sorted(os.listdir(folder)) == sorted(_LEFT[f'{runner.name} on Slurm' if on_slurm and toil else runner.name])
    line = shlex.split((folder / 'job.sh').read_text(encoding='utf-8').splitlines()[-1])
    if toil:   # the --maxJobs the site read, and the config's log level, before the user's flags
        assert line[line.index('--maxJobs') + 1] == '3' and line[line.index('--logLevel') + 1] == 'DEBUG'
        assert line.index('--logLevel') < line.index('--targetTime')
    if isinstance(runner, Nextflow):   # as Nextflow's executor took it; the output folder of the workflow's made
        assert re.findall(r"executor '(\w+)' > .*?capacity[:=] ?(\d+);", (folder / 'nextflow_site' / '.nextflow.log')
                          .read_text()) == [('slurm' if on_slurm else 'local', '3')]
        assert (folder / 'made' / 'masks').is_dir()


#: How the test ends the run: by `cancel`, by aging its record past the claim window, or by killing its leader; the
#: claim its job, held in its login profile until then, finds; and the state, a word of its reason, and its exit code.
_Ended = tuple[RunState, str, int | None]
_ENDS: list[tuple[str, str | None, _Ended]] = [
    ('cancel', None, (RunState.CANCELLED, 'cancelled', 143)),
    ('cancel', 'cancelled', (RunState.CANCELLED, 'cancelled', None)),
    ('age', 'none', (RunState.FAILED, 'never claimed', None)),
    ('kill', None, (RunState.FAILED, 'killed', None))]


@pytest.mark.parametrize(('end', 'held', 'ended'), _ENDS)
def test_a_run_that_fails_is_cancelled_killed_or_never_starts_ends_with_no_outputs(
        site: Path, monkeypatch: pytest.MonkeyPatch, end: str, held: str | None, ended: _Ended) -> None:
    """However a run ends, its outputs folder is left empty and taken after, and a held job starts nothing; each
    status is read with a GNU `stat` that misses `exit` once."""
    held_job = '[ -n "${BASH_EXECUTION_STRING-}" ] || sleep 3\n' if held else ''   # the job's profile, not a call's
    (site / '.bash_profile').write_text(f'PATH={_gnu_stat(site)}:$PATH\n{held_job}', encoding='utf-8')
    monkeypatch.setenv('HOME', str(site))
    outdir, config = site / 'results' / 'p1', RunConfig('results/p1').relative_to(str(site))
    workflow = _step('step', 'sh', '-c', 'touch "$0/partial"; sleep 600', str(outdir))
    run = _machine(site).submit(workflow, config)
    folder = site / 'runs' / run.id

    def errors() -> list[Any]:   # of a second submit into its outputs folder
        return [d for d in _machine(site).check(workflow, config) if d.severity is Severity.ERROR]
    while not held and not (outdir / 'partial').exists():
        time.sleep(0.5)
    assert end != 'cancel' or (run.cancel() and run.cancel())
    if end == 'age':   # while the claim cannot be written, nothing is decided
        os.utime(folder / 'run.json', (time.time() - 60 * _states.UNCLAIMED_MINUTES - 60,) * 2)
        folder.chmod(0o555)
        assert run.status().state is RunState.QUEUED
        folder.chmod(0o755)
    if end == 'kill':   # killed outright, it holds its outputs folder until a status has found it so
        os.killpg(int(os.readlink(folder / 'leader')), signal.SIGKILL)
        assert any(f'used by run {run.id}' in d.message for d in errors())
    status = run.wait(timeout=300)
    assert (status.state, status.exit_code, status.steps) == (ended[0], ended[2], ())
    assert ended[1] in str(status.reason) and os.listdir(outdir) == [] and errors() == []
    if end == 'kill':   # the next run there; the reason this one's status wrote, read again; a late status
        after = _machine(site).submit(_step('step', 'sh', '-c', 'echo b > "$0/b.txt"', str(outdir)), config,
                                      runner=Cwltool())
        assert after.wait(timeout=300).state is RunState.COMPLETED, after.logs()
        killed = _machine(site).attach(run.id)
        assert (killed.status().state, killed.status().reason) == (status.state, status.reason)
        killed._act(Decision(RunState.FAILED, 'x', empty=True))   # as a status that read it gone, acting late
        assert sorted(os.listdir(outdir)) == ['b.txt', 'out.txt']
    time.sleep(4 if held else 0)
    assert not held or (os.readlink(folder / 'leader') == held and not any(
        (folder / name).exists() for name in ('exit', 'toil', 'stdout')))


@pytest.mark.parametrize('lost', ['before', 'after', 'ended', 'claimed', 'refused', 'squeue', 'sacct'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # a row, and the site
def test_a_submit_whose_answer_is_lost_adopts_the_job_slurm_took_or_submits_it_again(
        site: Path, slurm: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        lost: str) -> None:
    """When `sbatch`'s answer is lost, `submit` adopts the job Slurm took, by squeue else sacct, or submits again,
    ignoring hang-ups; one leader runs. Refused again, or unable to ask, it raises, and no job runs."""
    asked = 'sacct() { case "$*" in *--name=*) echo "sacct: gone" >&2; return 1 ;; esac; command sacct "$@"; }; '
    (site / '.bash_profile').write_text(  # each sbatch that submits sends hang-ups
        'if [ -n "${BASH_EXECUTION_STRING-}" ]; then sbatch() { [ "$1" != --parsable ] || { kill -HUP $$; '
        'kill -PIPE $$; }; command sbatch "$@"; }; ' + (asked if lost == 'sacct' else '') + 'fi\n', encoding='utf-8')
    monkeypatch.setenv('HOME', str(site))
    (slurm / 'lose').write_text({'refused': '', 'squeue': 'before', 'sacct': 'before', 'claimed': 'before ended'}.get(
        lost, lost), encoding='utf-8')
    (slurm / 'broken').write_text({'refused': 'sbatch', 'squeue': 'squeue'}.get(lost, ''), encoding='utf-8')
    if lost in ('before', 'after'):
        (slurm / 'hold').touch()
    config = RunConfig('results').relative_to(str(site))
    if lost in ('refused', 'squeue', 'sacct'):   # it raises, and submits nothing again
        with pytest.raises(SophiosError) as caught:
            _slurm_site(site).submit(_COPY(), config)
        run_id, said = capsys.readouterr().out.split()[1], [d.message for d in caught.value.diagnostics]
        assert {d.code for d in caught.value.diagnostics} == {Code.SITE_UNREACHABLE} and not fake_slurm.jobs(slurm)
        if lost == 'refused':   # claimed `none`, its reason why
            assert len(said) == 2 and 'did not start' in said[1]
            assert os.readlink(site / 'runs' / run_id / 'leader') == 'none'
            assert 'Slurm refused its job' in str(_slurm_site(site).attach(run_id).status().reason)
        else:   # both errors, and how to follow the run
            assert len(said) == 3 and lost in said[1] and said[2].startswith(f'run {run_id} may have started')
        return
    run = _slurm_site(site).submit(_COPY(), config)
    folder = site / 'runs' / run.id
    printed = capsys.readouterr().out.splitlines()   # where its jobs go, and the job Slurm has
    assert printed[1].startswith('Slurm: partition normal (') and printed[3] == 'Slurm has it as job 4242.'
    again = subprocess.run(['sbatch', '--parsable', str(folder / 'job.sh')], capture_output=True, text=True,
                           check=True).stdout.strip() if lost in ('before', 'after') else None
    (slurm / 'hold').unlink(missing_ok=True)
    assert run.wait(timeout=300).state is RunState.COMPLETED, run.logs()
    first, *later = [job for job in _settled(slurm) if job['name'] == run.id]   # the leaders, not Toil's step
    assert (first['id'], [(job['id'], job['state'], job['start'] >= first['end']) for job in later]) == (
        '4242', [(again, 'COMPLETED', True)] if again else [])
    assert (os.readlink(folder / 'leader'), (folder / 'exit').read_text()) == (first['id'], '0\n')
