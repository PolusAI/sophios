"""The parts of `sophios.sites` that need no site: its states, its URL, how it quotes and reads values, the scripts it
writes, run by a local `bash`, where Slurm places a run, and what it refuses before anything is sent."""
# pylint: disable=protected-access  # the properties read the package's own transport, reader, scripts and states
import copy
import dataclasses
import itertools
import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given, strategies as st

from sophios import sites, targets
from sophios.api.python._inference import Inference
from sophios.api.python._workflow_runtime import _view, compile_workflow_result
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.ir.resolved import Requirement
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode as Code
from sophios.runtime import runners
from sophios.runtime.backends import RunRequest
from sophios.runtime.runners import Cwltool, Nextflow, Runner, RunSettings, SlurmSettings, Toil
from sophios.runtime.status import RunState
from sophios.sites import RunConfig, _runners, _site, _slurm, _ssh
from sophios.sites._states import ENDED_GRACE, Decision, Folder, Job, decide, read_jobs

from . import fake_slurm
from .budgets import budget

#: Spaces, both quotes, a command substitution and a newline: a name a shell reads as many words, or runs.
_HOSTILE = 'a b\'c"d$(touch PWNED)e\nf'
#: The same but the newline, a control character, which an outputs folder may not hold, as the compute service refuses.
_HOSTILE_FOLDER = _HOSTILE.replace('\n', '')


@contextmanager
def _local(home: Path | None = None) -> Iterator[None]:
    """`ssh [user@]host` as a local `bash -c`; with `home`, a login profile of the test's own."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_ssh, '_transport', lambda url: ['bash', '-c'])
        if home is not None:
            patch.setenv('HOME', str(home))
        yield


def _step(name: str, *command: str, source: str | None = None, kind: str = 'File',
          tool: Callable[[CommandLineTool], CommandLineTool] = lambda tool: tool) -> Workflow:
    """A workflow of one step, `<command> <source>`, `source` a File or a Directory, keeping its stdout as `out.txt`,
    its tool as `tool` makes it."""
    inputs = Inputs(source=Input(cwl.file if kind == 'File' else cwl.directory, position=1)) \
        if source is not None else Inputs()
    made = tool(CommandLineTool(name, inputs, Outputs(out=Output(cwl.file, glob='out.txt'))).base_command(*command))
    step = Step(made.stdout('out.txt'), step_name=name)
    if source is not None:
        step.inputs.source = {'class': kind, 'location': source}
    workflow = Workflow([step], 'site')
    workflow.outputs.out = step.outputs.out
    return workflow


def _gpu(tool: CommandLineTool, count: int | None = None) -> CommandLineTool:
    """`tool`, asking for `count` GPUs, or for a GPU with no count."""
    return tool.gpu(cuda_version_min='11.4', compute_capability='3.0', device_count_min=count)


@pytest.fixture(name='slurm')
def _fake_slurm(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The fake Slurm's state folder, its commands first on PATH; every job it still has is cancelled after."""
    folder = tmp_path_factory.mktemp('slurm')
    for name, value in fake_slurm.install(folder).items():
        monkeypatch.setenv(name, value)
    yield folder / 'state'
    for job in fake_slurm.jobs(folder / 'state'):
        (folder / 'state' / f'cancel-{job["id"]}').touch()


#: A run's life: each snapshot of its folder and of the jobs Slurm lists, the state it reads as, and what `status` must
#: do there: claim it `none`, stop it, cancel the step job `8` a runner that ended left, or empty its outputs folder.
_Life = list[tuple[Folder, tuple[Job, ...], RunState, str]]
_LEADER, _STEP = partial(Job, '7', True), Job('8', False, 'RUNNING')
_PENDS = Job('8', False, 'PENDING', 'BadConstraints')
_QUEUED, _RUNNING, _FAILED = RunState.QUEUED, RunState.RUNNING, RunState.FAILED


def _on_a_machine(claim: str, code: int | None) -> _Life:
    """Unclaimed; then claimed `cancelled` or `none`, or by its leader, which runs, may write `exit`, and is gone."""
    life = [(Folder('', False, None), (), _QUEUED, '')]
    if not claim.isdigit():
        return [*life, (Folder(claim, False, None), (), RunState.CANCELLED if claim == 'cancelled' else _FAILED, '')]
    life.append((Folder(claim, True, None), (), _RUNNING, ''))
    if code is None:   # a hard kill: gone, with no `exit`
        return [*life, (Folder(claim, False, None), (), _FAILED, 'empty')]
    end = {0: RunState.COMPLETED, 143: RunState.CANCELLED}.get(code, _FAILED)
    return [*life, (Folder(claim, True, code), (), end, ''), (Folder(claim, False, code), (), end, '')]


def _on_slurm(code: int | None, ended: str) -> _Life:
    """Pending, running unclaimed, claimed; `exit` written (none for a hard kill) while a step job runs; Slurm ends
    the leader `ended`, and two minutes go by."""
    end = (RunState.COMPLETED if code == 0 else RunState.CANCELLED if ended.startswith('CANCELLED') and code in (
        None, 143) else _FAILED)
    told = '' if code in range(129) else 'cancel' if code is not None else 'cancel empty'
    claimed = Folder('7', False, code)
    waiting = Folder('', False, None, age=600)   # past the ten minutes, while Slurm lists its leader
    return [(Folder('', False, None), (), _QUEUED, ''), (waiting, (_LEADER('PENDING', 'Priority'),), _QUEUED, ''),
            (waiting, (_LEADER('RUNNING'),), _QUEUED, ''),
            (Folder('7', False, None), (_LEADER('RUNNING'), _STEP), _RUNNING, ''),
            (claimed, (_LEADER('RUNNING'), _STEP), end if code in range(129) else _RUNNING, ''),
            (claimed, (_LEADER(ended, ended=0), _STEP), _RUNNING if code is None else end,
             '' if code is None else told),
            (claimed, (_LEADER(ended, ended=ENDED_GRACE), _STEP), end, told)]


#: Unclaimed ten minutes with no job listed; with a leader that ended unclaimed; a run Slurm will never start, or one
#: of whose steps it will never start, which is stopped; and a runner that exited, whose leader's end decides.
_LIVES: list[_Life] = [
    [(Folder('', False, None, age=599), (), _QUEUED, ''), (Folder('', False, None, age=600), (), _FAILED, 'none'),
     (Folder('none', False, None, age=600), (), _FAILED, '')],
    [(Folder('', False, None, age=600), (_LEADER('FAILED', ended=ENDED_GRACE - 1),), _QUEUED, ''),
     (Folder('', False, None, age=600), (_LEADER('FAILED', ended=ENDED_GRACE),), _FAILED, 'none')],
    [(Folder('', False, None), (_LEADER('PENDING', 'PartitionTimeLimit'),), _FAILED, 'stop')],
    [(Folder('7', False, None), (_LEADER('RUNNING'), Job('9', False, 'PENDING', 'BadConstraints')), _FAILED, 'stop')],
    [(Folder('7', False, 143), (_LEADER('RUNNING'), _PENDS), _RUNNING, ''),
     (Folder('7', False, 143), (_LEADER('CANCELLED by 1000', ended=0), _PENDS), RunState.CANCELLED, 'cancel')]]


def _failing() -> Sequence[Job]:
    raise SophiosError.error(Code.SITE_UNREACHABLE, 'squeue exited with status 1')


@pytest.mark.fast
def test_a_runs_state_is_its_folders_then_its_schedulers_and_an_end_never_changes() -> None:
    """Over every life a run can have on a machine and on Slurm (design Appendix B), each snapshot reads as its cause,
    with what `status` must do there; an end never changes; and Slurm not answering decides what the folder has not."""
    lives = [(_on_a_machine(claim, code), False) for claim in ('cancelled', 'none', '4242') for code in (None,
                                                                                                         *range(256))]
    lives += [(_on_slurm(code, ended), True) for code in (None, *range(256))
              for ended in ('COMPLETED', 'FAILED', 'CANCELLED by 1000', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL')]
    lives += [(life, True) for life in _LIVES] + [(_LIVES[0], False)]
    lives += [([(Folder('7', False, None), (_STEP,), _RUNNING, ''), (Folder('7', False, code), (_STEP,), _FAILED,
                                                                     'cancel')], True) for code in range(129, 256)]
    for (life, on_slurm), scratch in itertools.product(lives, (None, '/r/toil')):
        ended: tuple[RunState, str | None] | None = None
        failing = _failing if on_slurm else None
        for folder, jobs, state, act in life:
            found = decide(folder, scratch, partial(tuple, jobs) if on_slurm else None)
            acts = ' '.join(word for word, done in (('none', found.claim_none), ('stop', found.stop),
                                                    ('cancel', found.cancel == ('8',)), ('empty', found.empty)) if done)
            assert (found.state, acts) == (state, act) and ended in (None, (found.state, found.reason)), (folder, jobs)
            assert (f'rm -rf {scratch}' in str(found.reason)) is (scratch is not None and found.empty), found
            try:
                alone: Any = decide(folder, scratch, failing)
            except SophiosError:
                alone = None
            assert alone == (found if not on_slurm or folder.leader in ('cancelled', 'none') or folder.exit in range(
                129) else None), (folder, jobs)
            for written in ['stopped', *([found.reason] if found.empty or found.stop else [])]:   # decides alone
                after = decide(dataclasses.replace(folder, reason=written), scratch, failing)
                assert (after.state, after.reason, after.empty, after.stop, after.cancel) == (
                    found.state if written == found.reason else _FAILED, written, False, False, ())
            ended = (found.state, found.reason) if state not in (_QUEUED, _RUNNING) else ended
        assert ended is not None


#: Why Slurm keeps a job pending, and a word of the setting its reason names, for the run's own job and for a step's;
#: None for a wait that clears in time.
_PENDING: dict[str, tuple[str, str] | None] = {
    **dict.fromkeys(['PartitionTimeLimit', 'QOSMaxWallDurationPerJobLimit', 'AssocMaxWallDurationPerJobLimit'],
                    ('(time=', '(steps_time=')),
    **dict.fromkeys(['InvalidAccount', 'AccountNotAllowed'], ('SBATCH_ACCOUNT',) * 2),
    **dict.fromkeys(['InvalidQOS', 'QOSNotAllowed'], ('SBATCH_QOS',) * 2),
    **dict.fromkeys(['QOSMaxCpuPerJobLimit', 'AssocMaxNodePerJobLimit', 'QOSMaxMemoryPerNode',
                     'AssocMaxCpuPerNodeLimit', 'QOSMinCpuNotSatisfied', 'MaxMemPerLimit', 'BadConstraints'],
                    ('(memory=', "tool's resources")),
    'Priority': None, 'AssocGrpCpuLimit': None}
#: What the folder, `squeue -o '%i|%T|%r|%k|%j'` and `sacct -o JobID,State,End` say of the run `r` at 1120 s (lines
#: that are no job's, another run's job, a second leader among them), and the state and a word of the reason.
_SAID: list[tuple[Folder, str, str, RunState, str | None]] = [
    (Folder('', False, None), 'squeue: warning: x|y\n7|PENDING|Priority|sophios:r|r', '', _QUEUED, 'Priority'),
    (Folder('7', False, None), '9|PENDING|PartitionTimeLimit|sophios:other|o', 'sacct: x|y\n7|RUNNING|Unknown',
     _RUNNING, None),
    (Folder('7', False, 143), '', '7|TIMEOUT|1000', _FAILED, 'time limit'),
    *((Folder('7', False, None), '', f'7|{end}|1000', _FAILED, f'job {end}: ')
      for end in ('OUT_OF_MEMORY', 'PREEMPTED', 'NODE_FAIL')),
    (Folder('7', False, None), '9|PENDING|Dependency|sophios:r|r', '7|FAILED|1000', _FAILED, '(Slurm: FAILED)'),
    (Folder('7', False, 137), '', '', _FAILED, 'no longer lists its job'),
    (Folder('7', False, None), '', '7|CANCELLED by 1000|1000', RunState.CANCELLED, 'cut short'),
    *((Folder('', False, None), f'7|PENDING|{why}|sophios:r|r', '', *((_FAILED, words[0]) if words else (_QUEUED, why)))
      for why, words in _PENDING.items()),
    *((Folder('7', False, None), f'7|RUNNING|None|sophios:r|r\n9|PENDING|{why}|sophios:r|toil|9', '',
       *((_FAILED, words[1]) if words else (_RUNNING, None))) for why, words in _PENDING.items())]


@pytest.mark.fast
@pytest.mark.parametrize(('folder', 'listed', 'accounted', 'state', 'word'), _SAID)
def test_a_run_on_slurm_reads_as_its_folder_and_its_own_jobs_say(folder: Folder, listed: str, accounted: str,
                                                                 state: RunState, word: str | None) -> None:
    """The run's jobs are those squeue lists with its comment and the leaders sacct lists by its name; a job that
    Slurm will never start names the setting to change, the run's for its own job, the steps' for a step's."""
    found = decide(folder, '/r/toil', partial(read_jobs, 'r', listed, accounted, 1000 + 2 * 60))
    assert found.state is state and (found.reason is None if word is None else word in str(found.reason)), found


@pytest.mark.fast
@budget(20)
@given(st.lists(st.text(st.characters(exclude_categories=['Cs'], exclude_characters='\x00')), max_size=4))
def test_every_value_a_script_carries_reaches_bash_as_it_was(tmp_path_factory: pytest.TempPathFactory,
                                                             values: list[str]) -> None:
    """Every value enters a script quoted by `_q`, through the guard on ssh's input: bash prints each back as it was."""
    with _local(tmp_path_factory.getbasetemp()):
        said = _ssh._call('ssh://localhost', 'printf "%s\\0" start ' + ' '.join(map(_ssh._q, values)))
    assert said == b''.join(f'{value}\0'.encode() for value in ['start', *values])


@pytest.mark.fast
@pytest.mark.parametrize(('profile', 'code', 'said'), [
    ('', Code.SITE_RUN_UNKNOWN, ''), ('echo Welcome to the cluster', Code.SITE_RUN_UNKNOWN, ''),
    ('read -r eaten', Code.SITE_UNREACHABLE, 'arrive whole')])
def test_a_script_runs_there_only_as_it_was_sent(tmp_path: Path, profile: str, code: Code, said: str) -> None:
    """What a login profile prints is never the script's, and one that eats part of the script stops it (`wic062`):
    `attach` of a run that is not there says so (`wic064`), and needs no `bin`."""
    (tmp_path / '.bash_profile').write_text(profile + '\n', encoding='utf-8')
    with _local(tmp_path), pytest.raises(SophiosError) as caught:
        sites.Machine('ssh://localhost', runs=str(tmp_path)).attach('-site-20261109T000000-00000000')
    [found] = caught.value.diagnostics
    assert found.code is code and said in found.message


@pytest.mark.fast
@pytest.mark.parametrize(('url', 'ssh'), [
    ('ssh://my-ec2', ['my-ec2']), ('ssh://me@cluster.example.org', ['me@cluster.example.org']),
    ('ssh://me@10.0.0.7:2222', ['-p', '2222', 'me@10.0.0.7']), ('ssh://_h:65535', ['-p', '65535', '_h']),
    *((url, None) for url in ('my-ec2', 'me@my-ec2', 'ssh://', 'ssh://h/', 'ssh://h/runs', 'ssh://h?x=1', 'ssh://h#x',
                              'ssh://-oProxyCommand=sh', 'ssh://-v', 'ssh://-l@h', 'ssh://me:secret@h', 'ssh://h:0',
                              'ssh://h:x', 'ssh://h:65536', 'ssh://h:123456', 'ssh://h b', 'sftp://h', 'SSH://h',
                              'ssh://[::1]', 7))])
def test_a_site_is_named_by_an_ssh_url_and_ssh_is_given_its_user_host_and_port(url: str, ssh: list[str] | None) -> None:
    """`ssh://[user@]host[:port]` and nothing else; ssh gets its own options, then `-p <port>` and `[user@]host`, none
    of which reads to it as an option. Messages name the host alone."""
    if ssh is None:
        with pytest.raises(ValueError, match='ssh://'):
            sites.Machine(url, runs='/r')
        return
    assert _ssh._transport(sites.Machine(url, runs='/r').url) == [
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'RemoteCommand=none', '-o', 'ConnectTimeout=20', '-o',
        'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', *ssh]
    assert str(_ssh._unreachable(url, 'why')).startswith(f'error [wic062] {ssh[-1].rpartition("@")[2]} cannot')


#: What `check` refuses on a Singularity site before anything is compiled or sent (`run_id`: what `attach` refuses), and
#: what the ValueError names.
_CHECKED: list[tuple[dict[str, Any], str]] = [
    ({'config': RunConfig(7)}, 'folder 7 is not'),  # type: ignore[arg-type]  # what the test sends
    ({'config': RunConfig('o').relative_to(7)}, 'relative_to 7'),  # type: ignore[arg-type]  # as above
    ({'config': RunConfig('')}, 'is empty'),
    ({'target': 'bash'}, 'unknown target'), ({'runner': Nextflow()}, 'No backend runs cwl'),
    ({'runner': 'toil-cwl-runner'}, 'runner= takes a value'),
    ({'runner': Toil(container_engine='podman')}, 'the engine is'),
    ({'runner': Nextflow(container_engine='singularity'), 'target': 'nextflow'}, 'the engine is'),
    *(({'runner': runner}, 'pulls') for runner in (
        Cwltool(pull_dir='/p'), Toil(provenance=False), Cwltool(cachedir='c'), Cwltool(remove_entrypoints=True),
        Cwltool(ignore_install=True), Cwltool(ignore_processes=True))),
    ({'runner': Toil('--maxJobs', '0')}, "--maxJobs '0'"), ({'runner': Toil('--maxJobs=')}, "--maxJobs ''"),
    ({'runner': Nextflow('-qs', 'x'), 'target': 'nextflow'}, "-qs 'x'"),
    ({'runner': Nextflow('-queue-size', '0'), 'target': 'nextflow'}, "-queue-size '0'"),
    ({'runner': Toil('--maxJobs')}, '--maxJobs needs a value'), ({'runner': Toil('--clean', 'some')}, 'Toil takes'),
    ({'runner': Toil('--maxJobs', 8)}, 'each flag is a string'),  # type: ignore[arg-type]  # as above
    ({'runner': Nextflow('-w=/x'), 'target': 'nextflow'}, 'sets -work-dir'),
    ({'runner': Toil('--singularity')}, 'sets --singularity'),
    ({'run_id': '../x'}, 'run id'), ({'run_id': 7}, 'run id')]
#: Each setting a site refuses when it is made, with what the ValueError names (the same on both kinds but `bin`'s
#: absence); a Slurm setting the compute client refuses, in its words; then each of `_CHECKED`.
_REFUSED: list[tuple[type[sites.Machine | sites.Slurm], dict[str, Any], dict[str, Any] | None, str]] = [
    *((sites.Machine, {name: value}, None, f'{name}={value!r}') for name in ('runs', 'bin') for value in (
        'r', '/r b', '/r\tb', "/r'b", '/r"b', '/r$(touch x)', '/r`x`', '/r\nb', '/r:b', '/r#b', '/r%j', '/r\\b',
        '/r\u00e9')),
    (sites.Machine, {'container_engine': 7}, None, 'container_engine='),
    *((kind, {'bin': None}, {}, f'as {kind.__name__}(url, runs=..., bin=...)')
      for kind in (sites.Machine, sites.Slurm)),
    (sites.Slurm, {}, {'config': RunConfig().slurm_settings(SlurmSettings(time='0'))}, "SlurmSettings(time=...): '0'"),
    (sites.Slurm, {}, {'config': RunConfig().slurm_settings(SlurmSettings(steps_time='0-0', memory='8GB'))},
     "8G\nSlurmSettings(steps_time=...): '0-0'"),   # under Toil, the steps' too, every problem at once
    *((sites.Machine, {}, options, said) for options, said in _CHECKED)]


@pytest.mark.fast
@pytest.mark.parametrize(('kind', 'settings', 'options', 'said'), _REFUSED)
def test_no_setting_is_taken_that_a_site_cannot_read(kind: type[sites.Machine | sites.Slurm], settings: dict[str, Any],
                                                     options: dict[str, Any] | None, said: str) -> None:
    """Each is a ValueError naming what to change, before anything is sent; a runner value's own settings stay their
    defaults, and a flag whose option the site sets, by any of its names, is refused."""
    with _local(), pytest.raises(ValueError, match=re.escape(said)):
        machine = kind('ssh://h', **{'runs': '/r', 'bin': '/b', 'container_engine': 'singularity', **settings})
        if options is not None and 'run_id' in options:
            machine.attach(options['run_id'])
        elif options is not None:
            machine.check(_step('s', 'true'), **options)


@pytest.mark.fast
@pytest.mark.parametrize(('runner', 'engine', 'passed', 'read'), [
    (Toil(), 'docker', (), {'max_parallel': ('64', "Sophios's default"), 'clean': ('always', "Sophios's default")}),
    (Toil('--logLevel', 'DEBUG', '--maxJobs=8', '--clean', 'never', '--maxJobs', '9', '--targetTime', '1'), 'docker',
     ('--logLevel', 'DEBUG', '--targetTime', '1'), {'max_parallel': ('9', '--maxJobs'), 'clean': ('never', '--clean')}),
    (Toil('--clean=onError', '--singularity'), 'docker', ('--singularity',),
     {'max_parallel': ('64', "Sophios's default"), 'clean': ('onError', '--clean')}),
    (Nextflow('-qs=3', '-resume', '-name', 'x'), 'singularity', ('-resume', '-name', 'x'),
     {'max_parallel': ('3', '-qs')}),
    (Nextflow('-qs', '3', '-queue-size', '5'), 'docker', (), {'max_parallel': ('5', '-queue-size')}),
    (Cwltool('--parallel', '--disable-pull', '--quiet', quiet=False), 'docker',
     ('--parallel', '--disable-pull', '--quiet'), {}),
    (Cwltool(quiet=True), 'podman', (), {})])
def test_a_runner_values_flags_are_read_by_the_site_or_passed_on_in_order(
        runner: Runner, engine: str, passed: tuple[str, ...], read: dict[str, tuple[str, str]]) -> None:
    """Toil's `--maxJobs` and `--clean` and Nextflow's `-qs` (`-queue-size`) are the site's, in both spellings, the last
    winning, else Sophios's default; every other flag passes on in order, `--quiet` and another engine's too."""
    assert sites.Machine('ssh://h', runs='/r', container_engine=engine)._read(runner) == (passed, read)


#: A partition's name, in every form the compute service takes.
_NAMES = st.from_regex(r'[A-Za-z0-9_.-]{1,7}', fullmatch=True)
#: Every form of a Slurm time above 0, days of 0 among them.
_TIMES = st.builds(lambda days, parts: f'{days}-' * (days is not None) + ':'.join(map(str, parts)),
                   st.none() | st.integers(0, 3), st.lists(st.integers(0, 70), min_size=1, max_size=3)).filter(
    lambda time: bool(set(time) - set('0:-')))
#: Where a Slurm site placed a run: the steps' and the run's partition and time; a GPU partition, an account, a memory,
#: and cwltool's cores and GPUs, which the header writes whatever the runner.
_PLACED = st.fixed_dictionaries(
    {'steps_partition': _NAMES, 'steps_time': _TIMES, 'partition': _NAMES, 'time': _TIMES},
    optional={'gpu_partition': _NAMES, 'account': st.from_regex(_slurm._ACCOUNT, fullmatch=True),
              'memory': st.sampled_from(['8G', '512M']), 'cpus': st.integers(1, 64).map(str),
              'gpus': st.integers(1, 8).map(str)})


@pytest.mark.fast
@budget(25)
@given(st.integers(1, 1000), st.sampled_from(['always', 'onError', 'onSuccess', 'never']),
       st.sampled_from(['docker', 'podman', 'singularity']), st.sampled_from(['out', 'results/p1']),
       st.none() | _PLACED)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # the draw, and what Toil read
def test_toils_command_line_reads_back_as_the_site_set_it(tmp_path_factory: pytest.TempPathFactory, max_parallel: int,
                                                          clean: str, engine: str, outdir: str,
                                                          placed: dict[str, str] | None) -> None:
    """The runner's line of `job.sh`, as Toil's own parser reads it (design Appendix A); on Slurm (`placed`) each step
    a job where the site placed it, and the leader's header, with each `SBATCH_` variable that beats a line unset."""
    from toil.cwl.cwltoil import get_options  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    run = str(tmp_path_factory.mktemp('runs') / 'wf-1')
    site = (sites.Machine if placed is None else sites.Slurm)('ssh://h', runs='/r', bin='/opt/run/bin',
                                                              container_engine=engine)
    read = {'max_parallel': str(max_parallel), 'clean': clean, **(placed or {})}
    runner = Toil(*site._RUNNERS[Toil.name](run, read), '--logLevel', 'DEBUG', '--targetTime', '7',
                  container_engine=engine)
    script = site._job(RunRequest('wf', Path(run), RunSettings(runner, f'{run}/{outdir}')), read, ())
    words = shlex.split(script.splitlines()[-1])
    argv = words[1:words.index('|')]
    os.makedirs(f'{run}/toil/work')
    found = get_options(argv[1:])
    assert argv[0] == '/opt/run/bin/toil-cwl-runner'
    assert (found.max_jobs, found.clean, found.defaultMemory, found.cwl_default_ram, found.logLevel,
            found.targetTime) == (max_parallel, clean, 4 * 2 ** 30, False, 'DEBUG', 7)
    assert (found.bypass_file_store, found.rm_tmpdir, found.workDir, found.tmpdir_prefix, found.tmp_outdir_prefix) == (
        True, False, f'{run}/toil/work', f'{run}/toil/tmp/t', f'{run}/toil/tmp/o')
    assert (found.singularity, found.user_space_docker_cmd) == (engine == 'singularity',
                                                                {'podman': 'podman'}.get(engine))
    assert (found.outdir, found.jobStore, found.cwltool, found.cwljob) == (
        f'{run}/{outdir}', f'file:{run}/jobStore_wf', f'{run}/wf.cwl', [f'{run}/wf_inputs.yml'])
    header = dict(line.removeprefix('#SBATCH --').partition('=')[::2] for line in script.splitlines()
                  if line.startswith('#SBATCH'))
    if placed is None:
        assert (found.batchSystem, found.batch_logs_dir, header) == ('single_machine', None, {})
        return
    account = [f'--account={placed["account"]}'] if 'account' in placed else []
    assert (found.batchSystem, found.batch_logs_dir, found.slurm_partition, found.slurm_gpu_partition,
            shlex.split(found.slurm_args), found.slurm_time) == (   # as Toil splits them: the account one word
        'slurm', f'{run}/batch', placed['steps_partition'], placed.get('gpu_partition'),
        ['--comment=sophios:wf-1', *account], fake_slurm.seconds(placed['steps_time']))
    lines = {'account': 'account', 'partition': 'partition', 'time': 'time', 'mem': 'memory', 'cpus-per-task': 'cpus'}
    assert header == {'job-name': 'wf-1', 'comment': 'sophios:wf-1', 'output': f'{run}/run.log',
                      'error': f'{run}/run.log', 'open-mode': 'append', 'chdir': run, 'no-requeue': '',
                      'dependency': 'singleton', **({'gres': f'gpu:{placed["gpus"]}'} if 'gpus' in placed else {}),
                      **{option: placed[name] for option, name in lines.items() if name in placed}}
    [unset, sbatch] = site._leader(run, script)
    assert sbatch == f'sbatch --parsable {run}/job.sh' and set(unset.split()[1:]) == {
        'SBATCH_JOB_NAME', 'SBATCH_PARTITION', 'SBATCH_TIMELIMIT', 'SBATCH_OUTPUT', 'SBATCH_ERROR', 'SBATCH_OPEN_MODE',
        'SBATCH_NO_REQUEUE', 'SBATCH_REQUEUE', *(['SBATCH_ACCOUNT'] if 'account' in placed else []),
        *(['SBATCH_MEM_PER_NODE', 'SBATCH_MEM_PER_CPU', 'SBATCH_MEM_PER_GPU'] if 'memory' in placed else []),
        *(['SBATCH_GRES'] if 'gpus' in placed else [])}


#: A partition a setting names, which `sinfo` lists or not; a time a setting or your `SBATCH_TIMELIMIT` gives.
_PARTITION, _TIME = st.sampled_from([None, 'cpu', 'gpu', 'nowhere']), st.sampled_from([None, '45:00', '0-3'])


def _rows(default: str | None, limits: list[str], gpus: int) -> list[list[str]]:
    """`sinfo -a`'s rows: `cpu`, `short`, `gpu` and `v100`, each with its limit, `default` marked, the first `gpus` of
    `gpu` and `v100` with GPUs."""
    return [[f'{name}*' if name == default else name, limit, 'gpu:2' if name in ('gpu', 'v100')[:gpus] else '(null)']
            for name, limit in zip(['cpu', 'short', 'gpu', 'v100'], limits)]


def _first(*found: tuple[str | None, str]) -> tuple[str, str]:
    """The first of `found`, a value and where it came from, that has a value; else none, from the last."""
    return next(((value, source) for value, source in found if value), ('', found[-1][1]))


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # what placement is given
def _oracle(slurm: SlurmSettings, variables: tuple[str, str], rows: list[list[str]], runner: str,
            needs: tuple[int, int, int]) -> dict[str, tuple[str, str]] | None:
    """Where the fallback rules (design §5) put a run's jobs, each with where it came from; None when one goes to a
    partition `sinfo` does not list, or the run has no time limit."""
    (partition, limit), (cores, memory, devices) = variables, needs
    limits = {name.rstrip('*'): row_limit for name, row_limit, _ in rows}
    gpus = [name.rstrip('*') for name, _, gres in rows if gres != '(null)']
    default = next((name[:-1] for name, _, _ in rows if name.endswith('*')), None)
    run = _first((slurm.partition, 'SlurmSettings(partition=...)'), (partition, 'your SBATCH_PARTITION'),
                 (default, "the cluster's default"))
    gpu = _first((slurm.gpu_partition, 'SlurmSettings(gpu_partition=...)'),
                 (gpus[0] if len(gpus) == 1 else None, 'the one partition with GPUs'))
    toil = runner == Toil.name
    placed = {'partition': gpu if devices and not toil else run}
    if toil:
        placed['steps_partition'] = _first((slurm.steps_partition, 'SlurmSettings(steps_partition=...)'),
                                           (run[0], "the run's partition"))
        placed.update({'gpu_partition': gpu} if devices else {})
    if any(name not in limits for name, _ in placed.values()):
        return None
    placed['time'] = _first((slurm.time, 'SlurmSettings(time=...)'), (limit, 'your SBATCH_TIMELIMIT'),
                            (limits[placed['partition'][0]], "the partition's longest"))
    if placed['time'][0] in ('UNLIMITED', 'infinite'):
        return None
    if slurm.memory or (memory and not toil):
        placed['memory'] = _first((slurm.memory, 'SlurmSettings(memory=...)'),
                                  (f'{memory}M', "the largest step's ramMin"))
    if toil:
        steps = [placed['steps_partition'][0], *([gpu[0]] if devices else [])]
        limited = [(limits[name], "the shortest limit of the steps' partitions") for name in steps]
        placed['steps_time'] = _first((slurm.steps_time, 'SlurmSettings(steps_time=...)'), min(
            [(placed['time'][0], "the run's time"), *limited], key=lambda given: fake_slurm.seconds(given[0])))
    else:
        placed['cpus'] = (str(cores), "the largest step's coresMin")
        placed.update({'gpus': (str(devices), "the largest step's cudaDeviceCountMin")} if devices else {})
    return placed


@pytest.mark.fast
@budget(300)
@given(st.builds(SlurmSettings, partition=_PARTITION, time=_TIME, memory=st.sampled_from([None, '8G']),
                 steps_partition=_PARTITION, steps_time=_TIME, gpu_partition=_PARTITION),
       st.tuples(st.sampled_from(['', 'short', 'a,b']), st.sampled_from(['', '0-02:00:00', 'UNLIMITED'])),
       st.builds(_rows, st.sampled_from([None, 'cpu', 'short', 'gpu']),
                 st.lists(st.sampled_from(['30:00', '2:00:00', '0-12', '1-00:00:00', 'infinite']), min_size=4,
                          max_size=4), st.integers(0, 2)),
       st.sampled_from([Toil.name, Cwltool.name]), st.tuples(st.integers(1, 4), st.sampled_from([0, 300]),
                                                             st.integers(0, 2)))
@example(SlurmSettings(), ('', ''), _rows('cpu', ['2:00:00', '1:00:00', '30:00', '30:00'], 1), Toil.name, (1, 0, 1))
def test_a_run_goes_where_its_settings_say_else_where_slurms_conventions_put_it(
        slurm: SlurmSettings, variables: tuple[str, str], rows: list[list[str]], runner: str,
        needs: tuple[int, int, int]) -> None:
    """Each setting from the config, else your `SBATCH_` one, else the cluster's (design §5), as the oracle says; a job
    sent to a partition `sinfo -a` does not list, or a run with no time limit, is refused."""
    problems, placed = _slurm.placement(rows, variables, slurm, runner, needs, 'h')
    expected = _oracle(slurm, variables, rows, runner, needs)
    assert (placed, bool(problems)) == (expected or {}, expected is None)


_CPU = 'normal*|2-00:00:00|(null) short|30:00|(null)'
#: The config's settings; your `SBATCH_ACCOUNT`, `_PARTITION` and `_TIMELIMIT`; what `sinfo -a` prints; the runner;
#: the steps' needs; and where the run goes, or the setting each problem names.
_EDGES: list[tuple[SlurmSettings, tuple[str, str, str], str, str, tuple[int, int, int], Any]] = [
    # sinfo read as Slurm prints it, a warning among its rows; your account
    (SlurmSettings(), ('lab', '', ''), f'sinfo: warning: a|b is old {_CPU}', Cwltool.name, (1, 0, 0), {
        'partition': 'normal', 'time': '2-00:00:00', 'cpus': '1', 'account': 'lab'}),
    # what Slurm or your profile gives in a form the setting would not take
    (SlurmSettings(), ('', '', ''), 'p*|n/a|(null)', Toil.name, (1, 0, 0), ['time']),
    *((SlurmSettings(), ('', partition, ''), rows, Toil.name, (1, 0, gpu), [named]) for partition, rows, gpu, named in [
        ("p'q", f"{_CPU} p'q|1:00:00|(null)", 0, 'partition'), ('', "p'q*|1:00:00|(null)", 0, 'partition'),
        ('', "cpu*|1-0|(null) p'q|2:00:00|gpu:1", 1, 'gpu_partition')]),
    (SlurmSettings(time='1:00:00', steps_partition='odd'), ('', '', ''), 'normal*|1:00:00|(null) odd|n/a|(null)',
     Toil.name, (1, 0, 0), ['steps_time'])]


@pytest.mark.fast
@pytest.mark.parametrize(('slurm', 'variables', 'rows', 'runner', 'needs', 'placed'), _EDGES)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments  # a row, and the patch
def test_a_run_is_placed_from_what_slurm_and_your_profile_say_as_they_say_it(
        monkeypatch: pytest.MonkeyPatch, slurm: SlurmSettings, variables: tuple[str, str, str], rows: str,
        runner: str, needs: tuple[int, int, int], placed: Any) -> None:
    """What Slurm and your profile say is read as they print it; a value in a form the setting would not take is
    `wic067`, a line naming the setting to give."""
    monkeypatch.setattr(_slurm, '_needs', lambda view: needs)
    said = ['0 slurm 24.05'] * 4 + [f'0 {rows}', *variables, '', '']
    problems, found = sites.Slurm('ssh://h', runs='/r', bin='/b')._place(said, slurm=slurm, runner=runner,
                                                                         view=None)  # type: ignore[arg-type]
    named = [re.findall(r'SlurmSettings\((\w+)=', problem.message)[-1] for problem in problems
             if problem.code is Code.SITE_SLURM]
    assert len(named) == len(problems) and (named or {name: value for name, (value, _) in found.items()}) == placed


@pytest.mark.fast
@pytest.mark.parametrize(('tools', 'needs'), [
    ((lambda tool: tool.resources(ram_max=300), lambda tool: tool.resources(cores_min=2.5, ram_min=100)), (3, 300, 0)),
    ((lambda tool: tool.resources(cores_max=4, ram_min='$(inputs.source.size)'),), (4, 0, 0)),
    ((lambda tool: _gpu(tool.resources(cores_min='$(inputs.source.size)')), partial(_gpu, count=2)), (1, 0, 2)),
    ((_gpu,), (1, 0, 1))])
def test_a_runs_one_job_asks_for_the_most_any_step_states(tools: tuple[Callable[[CommandLineTool], CommandLineTool]],
                                                          needs: tuple[int, int, int]) -> None:
    """Under cwltool the run's one job asks for the most cores, at least 1, memory and GPUs a step states, its minimum
    else its maximum, rounded up; an expression is not read, and a GPU with no count is one."""
    workflow = Workflow([step for at, tool in enumerate(tools)
                         for step in _step(f's{at}', 'cat', source='in.txt', tool=tool).steps], 'site')
    view = _view(workflow, compile_workflow_result(workflow, tool_registry=None, inference=Inference()))
    assert _slurm._needs(view) == needs


@pytest.mark.fast
@pytest.mark.needs_cwltool
def test_a_step_declares_memory_exactly_when_cwltools_lookup_which_toil_asks_finds_it() -> None:
    """Over every ResourceRequirement a tool's requirements and hints can write, in each spelling, and every one it can
    inherit: `check` reads memory as declared exactly when cwltool's lookup, which Toil asks, finds ramMin or ramMax."""
    from cwltool.context import LoadingContext  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    from cwltool.load_tool import load_tool  # pylint: disable=import-outside-toplevel  # as above
    from cwltool.workflow import default_make_tool  # pylint: disable=import-outside-toplevel  # as above
    kinds: list[Any] = [None, {'coresMin': 1}, {'ramMin': 100}, {'ramMax': 100}]
    spellings = {'map': lambda body: {'ResourceRequirement': body},
                 'list': lambda body: [{'class': 'ResourceRequirement', **body}],
                 'after another': lambda body: [{'class': 'ResourceRequirement', 'coresMin': 2},
                                                {'class': 'ResourceRequirement', **body}]}
    for own, hint, spelling, outside, outside_hint in itertools.product(kinds, kinds, spellings.values(), kinds,
                                                                        (False, True)):
        tool: dict[str, Any] = {
            'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'id': 'file:///tool.cwl', 'baseCommand': 'true',
            'inputs': [], 'outputs': [],
            **{section: spelling(body) for section, body in [('requirements', own), ('hints', hint)] if body}}
        inherited = [] if outside is None else [Requirement('ResourceRequirement', outside, outside_hint)]
        written = {} if outside is None else {
            'hints' if outside_hint else 'requirements': [{'class': 'ResourceRequirement', **outside}]}
        context = LoadingContext({'construct_tool_object': default_make_tool, **written})
        found = load_tool(copy.deepcopy(tool), context).get_requirement('ResourceRequirement')[0] or {}
        assert _site._declares_memory(tool, inherited) is ('ramMin' in found or 'ramMax' in found), (tool, inherited)


@pytest.mark.fast
@pytest.mark.parametrize('clean', [None, 'always', 'onError', 'onSuccess', 'never'])
@pytest.mark.parametrize('code', [0, 1, 143])
def test_the_leader_records_its_exit_last_once_its_scratch_is_gone_and_a_run_that_did_not_succeed_left_nothing(
        tmp_path: Path, clean: str | None, code: int) -> None:
    """The leader's EXIT trap, run by bash: Toil's scratch removed as `--clean` says (kept for None), the outputs
    folder emptied unless the run succeeded, unwritable folders in either too; then `exit`, whole."""
    for folder in ('toil/tmp', 'toil/work', f'{_HOSTILE}/locked'):
        (tmp_path / folder / 'locked').mkdir(parents=True)
        (tmp_path / folder / 'locked' / 'out.txt').touch()
        (tmp_path / folder / 'locked').chmod(0o500)
    trap = _runners._recorded(str(tmp_path), clean, str(tmp_path / _HOSTILE))
    assert subprocess.run(['bash', '-c', f'trap {shlex.quote(trap)} EXIT; exit {code}'], check=False).returncode == code
    removed = clean == 'always' or (clean == 'onSuccess' and code == 0) or (clean == 'onError' and code != 0)
    assert sorted(os.listdir(tmp_path / 'toil')) == ([] if removed else ['tmp', 'work'])
    assert os.listdir(tmp_path / _HOSTILE) == (['locked'] if code == 0 else [])
    assert sorted(os.listdir(tmp_path)) == [_HOSTILE, 'exit', 'toil'] and (tmp_path / 'exit').read_text() == f'{code}\n'


@pytest.mark.fast
def test_run_log_is_only_added_to_whoever_writes_it(tmp_path: Path) -> None:
    """`run.log` holds what the job's profile said, then the runner, then the leader's trap, each whole; the start
    ignores hang-ups, which here each `mkdir` of it sends."""
    run, runner = tmp_path / 'run', tmp_path / Toil.name
    (tmp_path / '.bash_profile').write_text('[ -n "${BASH_EXECUTION_STRING-}" ] || echo profile\n'
                                            'if [ -n "${BASH_EXECUTION_STRING-}" ]; then '
                                            'mkdir() { kill -HUP $$; command mkdir "$@"; }; fi\n', encoding='utf-8')
    runner.write_text('#!/bin/sh\necho runner\nrmdir toil/tmp\n', encoding='utf-8')
    runner.chmod(0o755)
    machine = sites.Machine('ssh://localhost', runs=str(tmp_path), bin=str(tmp_path))
    job = machine._job(RunRequest('wf', run, RunSettings(Toil(), f'{run}/out')), {'clean': 'always'}, ())
    with _local(tmp_path):
        _ssh._call(machine.url, machine._start(str(run), {'outdir': [f'{run}/out', "the run's own folder"]},
                                               {'job.sh': job}))
    while not (run / 'exit').exists():
        time.sleep(0.1)
    said = (run / 'run.log').read_text(encoding='utf-8').splitlines()
    assert said[:2] == ['profile', 'runner'] and len(said) == 3 and 'toil/tmp' in said[2]


@pytest.mark.fast
@pytest.mark.parametrize(('folder', 'relative_to', 'read'), [
    (None, None, None), ('/lab/out', None, '/lab/out'), ('results', '/lab', '/lab/results'),
    ('a/../b/', '/lab', '/lab/b'), ('//x//y/', None, '/x/y'), ('.', '/lab/', '/lab'), ('../up', '/lab/p', '/lab/up'),
    ('/..', None, '/'), (f'{_HOSTILE_FOLDER}/', '/lab', f'/lab/{_HOSTILE_FOLDER}')])
def test_the_output_folder_is_read_as_the_compute_service_reads_it(folder: str | None, relative_to: str | None,
                                                                   read: str | None) -> None:
    """From `relative_to`, `..`, `.` and doubled or trailing `/` resolved by name, as the service's driver does; None
    for the run's own. The log level and the Slurm settings are read as given."""
    config = RunConfig(folder) if relative_to is None else RunConfig(folder).relative_to(relative_to)
    slurm = SlurmSettings(partition='p', time='1')
    assert _site.site_config(config.log_level('INFO')) == (read, relative_to, 'INFO', SlurmSettings())
    assert _site.site_config(config.slurm_settings(slurm)) == (read, relative_to, 'INFO', slurm)


@pytest.mark.fast
def test_a_site_runs_cwltool_no_older_than_sophios_requires_and_has_a_runner_for_every_target() -> None:
    """The oldest cwltool a site runs is the one `pyproject.toml` requires; every target has a default runner, and
    every runner a floor and a machine's flags. The package exports both kinds of site."""
    assert sites.__all__ == ['Machine', 'Run', 'RunConfig', 'Slurm', 'SlurmSettings']
    project = tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text(encoding='utf-8'))
    [required] = [found for found in project['project']['dependencies'] if found.startswith('cwltool>=')]
    assert '.'.join(map(str, _runners.OLDEST[Cwltool.name])) == required.removeprefix('cwltool>=')
    assert set(_runners.DEFAULT_RUNNER) == set(targets.TARGETS)
    assert set(_runners.OLDEST) == {runner.name for runner in runners.RUNNERS} == set(_runners.ON_A_MACHINE)


@pytest.mark.fast
# pylint: disable-next=too-many-locals  # the runs, their jobs, and what is asked
def test_slurm_is_asked_for_the_runs_own_jobs_and_no_one_elses(tmp_path: Path, slurm: Path) -> None:
    """A status, a lost submit's second look, a check, a stop and a killed leader's end ask for the run's jobs alone;
    with Slurm not answering, a status decides nothing, a check holds every run with no `exit`, and nothing is done."""
    a, c, d = (tmp_path / f'site-20261109T000000-0000000{name}' for name in 'acd')
    for folder in (a, c, d):
        folder.mkdir()
    (a / 'run.json').write_text(json.dumps({'id': a.name, 'site': 'Slurm', 'workflow': 'site', 'target': 'cwl',
                                            'runner': [Toil.name, ''], 'flags': [], 'outdir': [str(a / 'out'), '']}))
    os.utime(a / 'run.json', (time.time() - 86400,) * 2)   # past the claim window: only Slurm's queue holds it
    (d / 'exit').write_text('0\n')
    for number, name, job in [(101, a.name, {}), (102, 'toil_job_1', {}), (103, a.name, {'user': 'other'}),
                              (104, d.name, {'comment': f'sophios:{d.name}'}), (105, c.name, {'user': 'other'})]:
        fake_slurm.enqueue(slurm, number, name, **{'comment': f'sophios:{a.name}', **job})
    machine, lost = sites.Slurm('ssh://localhost', runs=str(tmp_path)), SophiosError.error(Code.SITE_UNREACHABLE, 'x')
    acts = (Decision(RunState.FAILED, 'x', empty=True, cancel=('102',)), Decision(RunState.FAILED, 'x', stop=True))
    held = [f'unfinished {_ssh._q(str(run))} && echo yes || echo no' for run in (a, c, d)]   # as a check asks

    def ask(*lines: str) -> bytes:
        return _ssh._call(machine.url, '\n'.join(lines))
    with _local():
        said = _ssh._Said(ask(f'cd {_ssh._q(str(a))}', *machine._asked(a.name)))
        assert [job.id for job in machine._queue(a.name, said, int(time.time()))()] == ['101', '102', '101']
        assert machine._again(machine.attach(a.name), '', lost) == b'101\n'
        assert ask(machine._unfinished(), *held).split() == [b'yes', b'no', b'no']
        (slurm / 'broken').write_text('squeue', encoding='utf-8')   # then every run with no `exit` is held
        assert ask(machine._unfinished(), *held).split() == [b'yes', b'yes', b'no']
        for broken in ('squeue', 'sacct'):   # and a status decides nothing
            (slurm / 'broken').write_text(broken, encoding='utf-8')
            said = _ssh._Said(ask(f'cd {_ssh._q(str(a))}', *machine._asked(a.name)))
            with pytest.raises(SophiosError, match=rf'\[wic062\].* {broken} exited'):
                machine._queue(a.name, said, int(time.time()))()
        (a / 'out').mkdir()
        (a / 'out' / 'kept').touch()
        (slurm / 'broken').write_text('scancel', encoding='utf-8')   # a killed leader's end, or a stop, does nothing
        for decision in acts:
            with pytest.raises(SophiosError):
                machine.attach(a.name)._act(decision)
        assert not (a / 'leader').exists() and not (a / 'reason').exists() and os.listdir(a / 'out') == ['kept']
        (slurm / 'broken').unlink()
        for decision in acts:
            machine.attach(a.name)._act(decision)
    assert sorted(path.name for path in slurm.glob('cancel-*')) == ['cancel-101', 'cancel-102']
    assert (os.readlink(a / 'leader'), (a / 'reason').read_text(), os.listdir(a / 'out')) == ('cancelled', 'x', [])
