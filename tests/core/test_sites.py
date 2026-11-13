"""Sites, `sophios.sites.Machine` and `Slurm`, with a local `bash` for `ssh <host>`, the one thing these tests replace,
and for Slurm a fake of Sophios's own (`fake_slurm.py`)."""
# pylint: disable=protected-access  # the properties read the module's own transport, quoting and leader script
import copy
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sysconfig
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from hypothesis import given, strategies as st

from sophios import sites, targets
from sophios.api.python._inference import Inference
from sophios.api.python._workflow_runtime import lowered_for_site
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.backends import RunRequest
from sophios.ir.resolved import Requirement
from sophios.lang.diagnostics import Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode as Code
from sophios.run_status import RunState
from sophios.sites import Folder, Job, decide

from . import fake_slurm
from .budgets import budget
from .helpers import adapter_step, live_workflow

#: A run id no run has.
_NO_RUN = 'site-20261109T000000-00000000'
_SITES = {'machine': sites.Machine, 'slurm': sites.Slurm}


@contextmanager
def _local(home: Path | None = None) -> Iterator[None]:
    """`ssh <host>` as a local `bash -c`; with `home`, a login profile of the test's own."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sites, '_transport', lambda host: ['bash', '-c'])
        if home is not None:
            patch.setenv('HOME', str(home))
        yield


@pytest.fixture(name='site')
def _site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The project folder: the working directory here, `root` there, with `data/in.txt`; a run's status read every
    half second, not every five."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sites, 'POLL', 0.5)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'in.txt').write_text('hello\n', encoding='utf-8')
    with _local():
        yield tmp_path


@pytest.fixture(name='slurm')
def _slurm(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The fake Slurm's state folder, its commands first on PATH; every job it still has is cancelled after."""
    folder = tmp_path_factory.mktemp('slurm')
    for name, value in fake_slurm.install(folder).items():
        monkeypatch.setenv(name, value)
    yield folder / 'state'
    for job in fake_slurm.jobs(folder / 'state'):
        (folder / 'state' / f'cancel-{job["id"]}').touch()


def _settled(slurm: Path) -> list[dict[str, Any]]:
    """The fake Slurm's jobs, once each has ended, within a minute."""
    for _ in range(600):
        jobs = fake_slurm.jobs(slurm)
        if all('end' in job for job in jobs):
            return jobs
        time.sleep(0.1)
    raise AssertionError(f'a job of the fake Slurm has not ended: {jobs}')


def _step(name: str, *command: str, source: str | None = None,
          tool: Callable[[CommandLineTool], CommandLineTool] = lambda tool: tool) -> Workflow:
    """A workflow of one step, `<command> <source>`, which keeps its stdout as `out.txt`."""
    inputs = Inputs(source=Input(cwl.file, position=1)) if source is not None else Inputs()
    built = tool(CommandLineTool(name, inputs, Outputs(out=Output(cwl.file, glob='out.txt'))).base_command(*command))
    step = Step(built.stdout('out.txt'), step_name=name)
    if source is not None:
        step.inputs.source = {'class': 'File', 'location': source}
    workflow = Workflow([step], 'site')
    workflow.outputs.out = step.outputs.out
    return workflow


def _bin(program: str, site: Path) -> str:
    """The folder that holds `program` there: this Python's own, or, for Nextflow, one holding the one on PATH."""
    if program != 'nextflow':
        return sysconfig.get_path('scripts')
    (site / 'bin').mkdir()
    (site / 'bin' / 'nextflow').symlink_to(shutil.which('nextflow') or 'nextflow')
    return str(site / 'bin')


#: A run's life: each snapshot of its folder and of the jobs Slurm lists, the state its cause gives, and what `status`
#: must do there: claim the run `none`, stop it, or cancel the step job `8` a killed runner left.
_Life = list[tuple[Folder, tuple[Job, ...], RunState, str]]
_LEADER, _STEP = partial(Job, '7', True), Job('8', False, 'RUNNING')
_QUEUED, _RUNNING, _FAILED = RunState.QUEUED, RunState.RUNNING, RunState.FAILED


def _on_a_machine(claim: str, code: int | None) -> _Life:
    """Unclaimed; then claimed `cancelled` or `none`, or by its leader, which runs, may write `exit` while it ends, and
    is gone."""
    life = [(Folder('', False, None), (), _QUEUED, '')]
    if not claim.isdigit():
        return [*life, (Folder(claim, False, None), (), RunState.CANCELLED if claim == 'cancelled' else _FAILED, '')]
    life.append((Folder(claim, True, None), (), _RUNNING, ''))
    if code is None:   # a hard kill: gone, with no `exit`
        return [*life, (Folder(claim, False, None), (), _FAILED, '')]
    end = {0: RunState.COMPLETED, 143: RunState.CANCELLED}.get(code, _FAILED)
    return [*life, (Folder(claim, True, code), (), end, ''), (Folder(claim, False, code), (), end, '')]


def _on_slurm(code: int | None, ended: str) -> _Life:
    """Submitted and not listed yet, pending, running unclaimed, claimed; `exit` written (`code`, or none for a hard
    kill) while its leader and a step job run; Slurm ends the leader `ended` and lists it no more; two minutes go by."""
    end = (RunState.COMPLETED if code == 0 else RunState.CANCELLED if ended.startswith('CANCELLED') and code in (
        None, 143) else _FAILED)
    told = '' if code in range(129) else 'cancel'
    return [(Folder('', False, None), (), _QUEUED, ''), (Folder('', False, None), (_LEADER('PENDING', 'Priority'),),
                                                         _QUEUED, ''),
            (Folder('', False, None), (_LEADER('RUNNING'),), _QUEUED, ''),
            (Folder('7', False, None), (_LEADER('RUNNING'), _STEP), _RUNNING, ''),
            (Folder('7', False, code), (_LEADER('RUNNING'), _STEP), end if code in range(129) else _RUNNING, ''),
            (Folder('7', False, code), (_LEADER(ended, ended=0), _STEP), _RUNNING if code is None else end,
             '' if code is None else told),
            (Folder('7', False, code), (_LEADER(ended, ended=sites.ENDED_GRACE), _STEP), end, told)]


_STOPPED = ("Slurm will never start its step job 9 (QOSMaxCpuPerJobLimit): ask for no more than the partition and "
            "your limits allow: a step asks for its tool's resources")
#: Unclaimed ten minutes with no job listed, on a machine and on Slurm; with one that ended unclaimed; and a run one
#: of whose jobs Slurm will never start, which is stopped, then reads as its `reason`.
_LIVES: list[_Life] = [
    [(Folder('', False, None, age=599), (), _QUEUED, ''), (Folder('', False, None, age=600), (), _FAILED, 'none'),
     (Folder('none', False, None, age=600), (), _FAILED, '')],
    [(Folder('', False, None, age=599), (_LEADER('FAILED', ended=sites.ENDED_GRACE - 1),), _QUEUED, ''),
     (Folder('', False, None, age=600), (_LEADER('FAILED', ended=sites.ENDED_GRACE - 1),), _QUEUED, ''),
     (Folder('', False, None, age=600), (_LEADER('FAILED', ended=sites.ENDED_GRACE),), _FAILED, 'none')],
    [(Folder('7', False, None), (_LEADER('RUNNING'), Job('9', False, 'PENDING', 'QOSMaxCpuPerJobLimit')), _FAILED,
      'stop'), (Folder('7', False, 143, _STOPPED), (), _FAILED, '')],
    [(Folder('', False, None), (_LEADER('PENDING', 'PartitionTimeLimit'),), _FAILED, 'stop'),
     (Folder('cancelled', False, None, 'Slurm will never start the run (PartitionTimeLimit): give a shorter time='), (),
      _FAILED, '')]]


def _failing() -> Sequence[Job]:
    raise SophiosError.error(Code.SITE_UNREACHABLE, 'squeue exited with status 1')


@pytest.mark.fast
def test_a_runs_state_is_its_folders_then_its_schedulers_and_an_end_never_changes() -> None:
    """Over every life a run can have on a machine and on Slurm (design Appendix B), each claim, each exit status,
    each state Slurm ends a leader in, a leader Slurm lists no more: each snapshot reads as the state that brought it
    about, with what `status` must do there, and once a status read an end, every later one reads the same end, for
    the same reason. The folder decides first: with Slurm not answering, a status the folder decides reads the same,
    and any other raises, deciding nothing. Only a hard kill of a Toil run names the scratch it leaves, and only an
    end Slurm told, or an `exit` above 128, cancels the step jobs a runner left."""
    lives = [(_on_a_machine(claim, code), False) for claim in ('cancelled', 'none', '4242') for code in (None,
                                                                                                         *range(256))]
    lives += [(_on_slurm(code, ended), True) for code in (None, *range(256))
              for ended in ('COMPLETED', 'FAILED', 'CANCELLED by 1000', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL')]
    lives += [(life, True) for life in _LIVES] + [(_LIVES[0], False)]
    lives += [([(Folder('7', False, None), (_STEP,), _RUNNING, ''), (Folder('7', False, code), (_STEP,), _FAILED,
                                                                     'cancel')], True) for code in range(129, 256)]
    for (life, on_slurm), scratch in itertools.product(lives, (None, '/r/toil')):
        ended: tuple[RunState, str | None] | None = None
        for folder, jobs, state, act in life:
            found = decide(folder, scratch, partial(tuple, jobs) if on_slurm else None)
            assert (found.state, act) == (state, ('none' if found.claim_none else 'stop' if found.stop else
                                                  'cancel' if found.cancel == ('8',) else '')), (folder, jobs, found)
            assert ended is None or (found.state, found.reason) == ended, (folder, jobs, found)
            try:
                alone = decide(folder, scratch, _failing if on_slurm else None)
            except SophiosError:
                alone = None
            decided = folder.reason is not None or folder.leader in ('cancelled', 'none') or folder.exit in range(129)
            assert alone == (found if decided or not on_slurm else None), (folder, jobs)
            ended = (found.state, found.reason) if state not in (_QUEUED, _RUNNING) else ended
        last = life[-1][0]
        assert ended is not None
        assert (scratch is not None and last.leader.isdigit() and last.exit is None) is (
            f'rm -rf {scratch}' in str(ended[1])), ended


@pytest.mark.fast
@budget(20)
@given(st.lists(st.text(st.characters(exclude_categories=['Cs'], exclude_characters='\x00')), max_size=4))
def test_every_value_a_script_carries_reaches_bash_as_it_was(tmp_path_factory: pytest.TempPathFactory,
                                                             values: list[str]) -> None:
    """Every value enters a script quoted by `_q`, and the script goes through the guard on ssh's input: bash prints
    each value back byte for byte, whatever it holds."""
    with _local(tmp_path_factory.getbasetemp()):
        said = sites._call('localhost', 'printf "%s\\0" start ' + ' '.join(map(sites._q, values)))
    assert said == b''.join(f'{value}\0'.encode() for value in ['start', *values])


@pytest.mark.fast
@pytest.mark.parametrize(('profile', 'code', 'said'), [
    ('', Code.SITE_RUN_UNKNOWN, 'has no run'), ('echo Welcome to the cluster', Code.SITE_RUN_UNKNOWN, 'has no run'),
    ('read -r eaten', Code.SITE_UNREACHABLE, 'did not arrive whole')])
def test_a_script_runs_there_only_as_it_was_sent(tmp_path: Path, profile: str, code: Code, said: str) -> None:
    """What a login profile prints is never read as the script's; a profile that eats part of the script stops it
    whole (`wic057`), so `attach` of a run that is not there says so (`wic059`) only when the call ran."""
    (tmp_path / '.bash_profile').write_text(profile + '\n', encoding='utf-8')
    with _local(tmp_path), pytest.raises(SophiosError) as caught:
        sites.Machine('localhost', root=str(tmp_path), bin='/b').attach(_NO_RUN)
    [found] = caught.value.diagnostics
    assert found.code is code and said in found.message


_NAMES = st.from_regex(r'[a-z][a-z0-9]{0,6}', fullmatch=True)


@pytest.mark.fast
@budget(25)
@given(st.integers(1, 1000), st.sampled_from(['always', 'onSuccess', 'never']),
       st.sampled_from(['docker', 'podman', 'singularity']), st.sampled_from(['out', 'results/p1']),
       st.none() | st.builds(sites._Queue, st.tuples(_NAMES, st.just('')), st.just(('9', '')), st.none() | _NAMES,
                             st.just('0') | st.from_regex(sites._TIME, fullmatch=True), st.none() | _NAMES))
def test_toils_command_line_reads_back_as_the_site_set_it(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        tmp_path_factory: pytest.TempPathFactory, max_parallel: int, clean: str, engine: str, outdir: str,
        queue: sites._Queue | None) -> None:
    """The runner's line of `job.sh`, as Toil's own parser reads it: Toil's own launch and the site's flags (design
    Appendix A), `clean=` winning over the launch's `--clean always`, run from `bin`; on Slurm (a `queue`), each step a
    Slurm job on the run's partition, a GPU step on its own, with the steps' time as Toil reads it, the run's comment
    and account, and the steps' Slurm logs in the run."""
    from toil.batchSystems.slurm import parse_slurm_time  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    from toil.cwl.cwltoil import get_options  # pylint: disable=import-outside-toplevel
    run = str(tmp_path_factory.mktemp('runs') / 'wf-1')
    site = (sites.Machine if queue is None else sites.Slurm)('localhost', root='/r', bin='/opt/run/bin',
                                                             container_engine=engine)
    _, script = site._job(RunRequest('wf', Path(run), runner='toil-cwl-runner', container_engine=engine,
                                     outdir=f'{run}/{outdir}'), max_parallel, clean, (), queue)
    words = shlex.split(script.splitlines()[-1])
    argv = words[1:words.index('|')]
    os.makedirs(f'{run}/toil/work')
    read = get_options(argv[1:])
    assert argv[0] == '/opt/run/bin/toil-cwl-runner'
    assert (read.max_jobs, read.clean, read.defaultMemory, read.cwl_default_ram) == (
        max_parallel, clean, 4 * 2 ** 30, False)
    assert (read.bypass_file_store, read.rm_tmpdir, read.workDir, read.tmpdir_prefix, read.tmp_outdir_prefix) == (
        True, False, f'{run}/toil/work', f'{run}/toil/tmp/t', f'{run}/toil/tmp/o')
    assert (read.singularity, read.user_space_docker_cmd) == (engine == 'singularity', {'podman': 'podman'}.get(engine))
    assert (read.outdir, read.jobStore, read.cwltool, read.cwljob) == (
        f'{run}/{outdir}', f'file:{run}/jobStore_wf', f'{run}/wf.cwl', [f'{run}/wf_inputs.yml'])
    assert (read.batchSystem, read.batch_logs_dir) == (('single_machine', None) if queue is None else (
        'slurm', f'{run}/batch'))
    if queue is not None:
        assert (read.slurm_partition, read.slurm_gpu_partition, read.slurm_args) == (
            queue.partition[0], queue.gpu_partition,
            '--comment=sophios:wf-1' + (f' --account={queue.account}' if queue.account else ''))
        assert read.slurm_time == parse_slurm_time(queue.steps_time) == sites._seconds(queue.steps_time)


#: Slurm's answers to a check, but `sinfo`'s; the run's settings; your `SBATCH_ACCOUNT`, `_PARTITION` and
#: `_TIMELIMIT`; `sinfo -a`'s rows; the runner and what its step asks for; and where Slurm puts its jobs, or the codes.
_PLACES = [
    ({}, ('', '', ''), 'normal*|2-00:00:00|(null) short|30:00|(null)', 'toil-cwl-runner', None,
     (('normal', "the cluster's default"), ('2-00:00:00', "the partition's longest"), None, '2-00:00:00', None)),
    ({}, ('lab', 'short', '10:00'), 'normal*|2-00:00:00|(null) short|30:00|(null)', 'toil-cwl-runner', None,
     (('short', 'your SBATCH_PARTITION'), ('10:00', 'your SBATCH_TIMELIMIT'), 'lab', '30:00', None)),
    ({'partition': 'normal', 'time': '1-00:00:00', 'account': 'mine'}, ('lab', 'short', '10:00'),
     'normal*|2-00:00:00|(null) short|30:00|(null)', 'toil-cwl-runner', None,
     (('normal', 'partition='), ('1-00:00:00', 'time='), 'mine', '1-00:00:00', None)),
    ({}, ('', '', ''), 'cpu*|infinite|(null) gpu|infinite|gpu:2 gpu|infinite|(null)', 'toil-cwl-runner', 'gpu',
     (('cpu', "the cluster's default"), ('UNLIMITED', "the partition's longest"), None, '0', 'gpu')),
    ({}, ('', '', ''), 'cpu*|1-00:00:00|(null) gpu|2:00:00|gpu:2', 'toil-cwl-runner', 'gpu',
     (('cpu', "the cluster's default"), ('1-00:00:00', "the partition's longest"), None, '2:00:00', 'gpu')),
    ({}, ('', '', ''), 'cpu*|1-00:00:00|(null) a100|2:00:00|gpu:4 v100|9:00:00|gpu:2', 'toil-cwl-runner', 'gpu',
     [Code.SITE_SLURM]),
    ({'partition': 'nowhere', 'gpu_partition': 'nowhere'}, ('', '', ''), 'cpu*|1-00:00:00|(null)', 'toil-cwl-runner',
     'gpu', [Code.SITE_SLURM, Code.SITE_SLURM]),
    ({}, ('', '', ''), 'cpu|1-00:00:00|(null)', 'toil-cwl-runner', None, [Code.SITE_SLURM]),
]


@pytest.mark.parametrize(('settings', 'variables', 'rows', 'runner', 'asks', 'placed'), _PLACES)
def test_a_run_goes_where_its_settings_say_else_where_slurms_conventions_put_it(  # pylint: disable=R0913,R0917
        settings: dict[str, Any], variables: tuple[str, str, str], rows: str, runner: str, asks: str | None,
        placed: Any) -> None:
    """The partition, the leader's time and the account from the setting that names it, else your `SBATCH_` one,
    else the cluster's default partition and its longest limit (`infinite` is UNLIMITED); the steps' time `time=`,
    else the shortest limit of their partitions (`infinite` is Toil's 0); a GPU step's partition the one with GPUs.
    A partition `sinfo -a` does not list, or a GPU step with no one partition with GPUs, is `wic062`."""
    workflow = _step('copy', 'cat', tool={None: lambda tool: tool, 'gpu': lambda tool: tool.gpu()}[asks])
    lowered, _ = lowered_for_site(workflow, target='cwl', root=PurePosixPath('/r'), tool_registry=None,
                                  inference=Inference())
    assert lowered is not None
    said = ['0 slurm 24.05'] * 4 + [f'0 {rows}', *variables, '', '']
    problems, queue = sites.Slurm('h', root='/r', bin='/b', **settings)._place(said, runner, lowered.view)
    assert ([problem.code for problem in problems] if problems else queue) == (
        placed if isinstance(placed, list) else sites._Queue(*placed))


@pytest.mark.fast
@pytest.mark.needs_cwltool
def test_a_step_declares_memory_exactly_when_cwltools_lookup_which_toil_asks_finds_it() -> None:
    """Over every ResourceRequirement a tool's requirements and hints can write, in either of CWL's spellings, a
    list's after another one too, and every one it can inherit: `check` reads a step's memory as declared exactly
    when cwltool's own lookup, which Toil asks before it gives a step `--defaultMemory`, finds `ramMin` or `ramMax`."""
    from cwltool.context import LoadingContext  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    from cwltool.load_tool import load_tool  # pylint: disable=import-outside-toplevel
    from cwltool.workflow import default_make_tool  # pylint: disable=import-outside-toplevel
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
        assert sites._declares_memory(tool, inherited) is ('ramMin' in found or 'ramMax' in found), (tool, inherited)


@pytest.mark.fast
@pytest.mark.parametrize('program', ['toil-cwl-runner', 'nextflow'])
@pytest.mark.parametrize('clean', ['always', 'onSuccess', 'never'])
@pytest.mark.parametrize('code', [0, 1, 143])
def test_the_leader_records_its_exit_last_once_toils_scratch_is_gone_as_clean_says(
        tmp_path: Path, program: str, clean: str, code: int) -> None:
    """The leader's EXIT trap, run by bash: Toil's `toil/tmp` and `toil/work` (made unwritable by a tool) are
    removed under 'always', under 'onSuccess' only after a success, and never under 'never'; other runners leave
    no scratch to remove. Then `exit` holds the job's status, written whole."""
    for scratch in ('tmp', 'work'):
        (tmp_path / 'toil' / scratch / 'locked').mkdir(parents=True)
        (tmp_path / 'toil' / scratch / 'locked' / 'out.txt').touch()
        (tmp_path / 'toil' / scratch / 'locked').chmod(0o500)
    trap = sites._recorded(str(tmp_path), program, clean)
    assert subprocess.run(['bash', '-c', f'trap {shlex.quote(trap)} EXIT; exit {code}'], check=False).returncode == code
    removed = program == 'toil-cwl-runner' and (clean == 'always' or (clean == 'onSuccess' and code == 0))
    assert sorted(os.listdir(tmp_path / 'toil')) == ([] if removed else ['tmp', 'work'])
    assert sorted(os.listdir(tmp_path)) == ['exit', 'toil'] and (tmp_path / 'exit').read_text() == f'{code}\n'


#: The site, target, runner, whether `bin` holds no runner, the container engine, the workflow, outdir=, the Slurm
#: commands that do not answer and your settings there, and what check finds.
_PROBLEMS = [
    ('machine', 'cwl', 'toil-cwl-runner', True, 'docker', lambda: _step('copy', 'cat', source='data/gone.txt'),
     'data', '', {}, [Code.MISSING_INPUT_FILE, Code.SITE_RUNNER, Code.DIRECTORY_NOT_WRITABLE]),
    *(('machine', 'cwl', 'toil-cwl-runner', False, 'docker', lambda path=path: _step('copy', 'cat', source=path),
       None, '', {}, [Code.MISSING_INPUT_FILE]) for path in ('data/../data/in.txt', '~/in.txt', '$HOME/in.txt')),
    ('machine', 'cwl', 'toil-cwl-runner', False, 'no-such-engine', lambda: live_workflow('site'), None, '', {},
     [Code.REALTIME_NOT_ASYNC, Code.CONTAINER_ENGINE_UNAVAILABLE]),
    ('machine', 'nextflow', 'nextflow', True, 'podman',
     lambda: Workflow([adapter_step('touch', filename='empty.txt')], 'site'), None, '', {},
     [Code.SITE_RUNNER, Code.SITE_NEXTFLOW_CONTAINER]),
    ('slurm', 'cwl', 'toil-cwl-runner', True, 'singularity', lambda: _step('copy', 'cat', source='data/in.txt'),
     None, 'sacct', {'SBATCH_CLUSTERS': 'other', 'SBATCH_TIMELIMIT': 'two days'},
     [Code.SITE_RUNNER, *[Code.SITE_SLURM] * 3])]


@pytest.mark.parametrize(('kind', 'target', 'runner', 'empty', 'engine', 'workflow', 'outdir', 'broken', 'variables',
                          'codes'), _PROBLEMS)
def test_check_finds_every_problem_at_once_and_writes_nothing_and_submit_raises_them(  # pylint: disable=R0913,R0917,R0914
        site: Path, slurm: Path, monkeypatch: pytest.MonkeyPatch, kind: str, target: str, runner: str, empty: bool,
        engine: str, workflow: Any, outdir: str | None, broken: str, variables: dict[str, str],
        codes: list[Code]) -> None:
    """A missing input, one a site cannot read as written (`..`, `~`, `$`), a runner not in `bin`, an outdir that
    exists, a real-time analysis, an engine not there, a Nextflow container without Docker; on Slurm a command that
    does not answer, `sacct` too, a setting that sends jobs to another cluster, and one no job script can carry: all
    at once, each by its code; neither side changes, and `submit` raises the same."""
    for folder in ('empty', '~', '$HOME'):   # each path a site refuses names a file that is there
        (site / folder).mkdir()
        (site / folder / 'in.txt').touch()
    (slurm / 'broken').write_text(broken, encoding='utf-8')
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    machine = _SITES[kind]('localhost', root=str(site), container_engine=engine,
                           bin=str(site / 'empty') if empty else sysconfig.get_path('scripts'))
    workflow = workflow()
    before = sorted(site.rglob('*'))
    found = machine.check(workflow, target=target, runner=runner, outdir=outdir)
    assert sorted(d.code for d in found if d.severity is Severity.ERROR) == sorted(codes)
    with pytest.raises(SophiosError) as caught:
        machine.submit(workflow, target=target, runner=runner, outdir=outdir)
    assert sorted(d.code for d in caught.value.diagnostics if d.severity is Severity.ERROR) == sorted(codes)
    assert sorted(site.rglob('*')) == before and not fake_slurm.jobs(slurm)


@pytest.mark.fast
@pytest.mark.parametrize('call', [
    lambda: sites.Machine('-oProxyCommand=sh', root='/r', bin='/b'), lambda: sites.Machine('h', root='r', bin='/b'),
    lambda: sites.Machine('h', root='/r', bin='/b;sh'), lambda: sites.Machine('h', root='/r b', bin='/b'),
    lambda: sites.Machine('h', root='/r', bin='/b', container_engine='docker sh'),
    lambda: sites.Machine('h', root='/r', bin='/b', modules=('a;b',)),
    lambda: sites.Machine('h', root='/r', bin='/b', modules='gcc'),  # type: ignore[arg-type]
    lambda: sites.Slurm('h', root='/r', bin='/b', partition='a b'), lambda: sites.Slurm('h', root='/r', bin='/b',
                                                                                        time='2 days'),
    lambda: sites.Slurm('h', root='/r', bin='/b', account='$(id)'), lambda: sites.Slurm('h', root='/r', bin='/b',
                                                                                        time='0-12'),
    lambda: sites.Machine('h', root='/r', bin='/b').attach('../x'),
    *(lambda options=options, target=target: sites.Machine('h', root='/r', bin='/b').check(
        Workflow([], 'site'), target=target, **options) for target, options in [
        ('nextflow', {'clean': 'never'}), ('cwl', {'clean': 'some'}), ('cwl', {'max_parallel': 0}),
        ('cwl', {'outdir': 'a b'})]),
    lambda: sites.Machine('h', root='/r', bin='/b').check(Workflow([], 'site'), target='nextflow', outdir='o')])
def test_no_setting_takes_shell_code_and_no_runner_an_option_it_does_not_take(call: Any) -> None:
    """Each setting is checked against its pattern when the site is built, a run id when it is attached, and each
    option against the runner before anything is compiled: a ValueError, with nothing sent."""
    with pytest.raises(ValueError):
        call()


#: The header's options' `SBATCH_` variables, which beat its lines, and two it does not set, which `sbatch` keeps.
_VARIABLES = {'SBATCH_JOB_NAME': 'other', 'SBATCH_ACCOUNT': 'lab', 'SBATCH_PARTITION': 'normal',
              'SBATCH_TIMELIMIT': '1-00:00:00', 'SBATCH_OUTPUT': '/nowhere', 'SBATCH_ERROR': '/nowhere',
              'SBATCH_OPEN_MODE': 'truncate', 'SBATCH_REQUEUE': '1', 'SBATCH_NO_REQUEUE': '1',
              'SBATCH_MEM_PER_CPU': '1', 'SBATCH_QOS': 'lab'}
_RUNNERS = [pytest.param(kind, target, runner, id=f'{kind}-{runner}',
                         marks=[pytest.mark.nextflow] if (kind, runner) == ('machine', 'nextflow') else [])
            for kind in _SITES for target, row in targets.TARGETS.items() for runner in row.runners]


@pytest.mark.parametrize(('kind', 'target', 'runner'), _RUNNERS)
def test_every_runner_of_the_target_table_runs_on_every_site_or_is_refused_with_its_code(  # pylint: disable=R0913,R0917,R0914
        site: Path, slurm: Path, monkeypatch: pytest.MonkeyPatch, kind: str, target: str, runner: str) -> None:
    """Each runner of `targets.TARGETS` runs on each site from `bin`, reading a path relative to `root`, and `check`
    notes only Toil's step that declares no memory; or the site refuses it with its code before anything is compiled
    or sent: cwltool, `wic058`, naming Toil, a site's default (decision 22); Nextflow on Slurm, `wic062`. Its
    outputs are its backend's own outcome, read over the transport; the run's line attaches to it from anywhere; and
    its folder is its record: claimed by the leader's pid, or on Slurm its job id, `exit` 0, no Toil scratch left.
    Nextflow's executor takes `max_parallel` tasks at once, from the run's configuration. On Slurm every job of the
    run carries its comment, and `sbatch` gets none of the `SBATCH_` variables of the options the leader's header
    sets."""
    for name, value in _VARIABLES.items():
        monkeypatch.setenv(name, value)
    machine = _SITES[kind]('localhost', root=str(site), bin=_bin(targets.backend(target, runner).program, site))
    workflow = _step('copy', 'cat', source='data/in.txt')
    if targets.backend(target, runner).program not in machine._RUNNERS:
        with monkeypatch.context() as patch:
            patch.setattr(sites, '_call', None)   # nothing is sent
            patch.setattr(sites, 'lowered_for_site', None)   # nothing is compiled
            refused = [(d.code, 'toil-cwl-runner' in d.message) for d in machine.check(workflow, runner=runner)]
            with pytest.raises(SophiosError) as caught:
                machine.submit(workflow, runner=runner)
        assert refused == [(d.code, 'toil-cwl-runner' in d.message) for d in caught.value.diagnostics] == [
            (Code.SITE_RUNNER, True)]
        return
    found = machine.check(workflow, target=target, runner=runner)
    if sites.ON_SLURM[runner] is None and kind == 'slurm':
        assert [d.code for d in found] == [Code.SITE_SLURM]
        return
    assert [d.code for d in found if d.severity is Severity.ERROR or d.code is Code.SITE_DEFAULT_MEMORY] == (
        [Code.SITE_DEFAULT_MEMORY] if runner == 'toil-cwl-runner' else [])   # `copy` declares no memory
    run = machine.submit(workflow, target=target, runner=runner)
    status = run.wait(timeout=600)
    assert status.state is RunState.COMPLETED, run.logs()
    assert status.outputs is not None and Path(status.outputs['out']['path']).read_text(encoding='utf-8') == 'hello\n'
    assert repr(run) == f'{machine!r}.attach({run.id!r})' and machine.attach(run.id).status() == status
    folder = site / 'sophios' / 'runs' / run.id
    assert (folder / 'exit').read_text(encoding='utf-8') == '0\n' and os.readlink(folder / 'leader').isdigit()
    assert not any((folder / 'toil' / scratch).exists() for scratch in ('tmp', 'work'))
    if runner == 'nextflow':   # `max_parallel`, as Nextflow's executor took it
        assert re.findall(r'capacity[:=] ?(\d+);', (folder / 'nextflow_site' / '.nextflow.log').read_text()) == ['64']
    if kind == 'slurm':
        jobs = fake_slurm.jobs(slurm)
        assert [job['comment'] for job in jobs] == [f'sophios:{run.id}'] * 2   # the leader, Toil's step
        assert (os.readlink(folder / 'leader'), jobs[0]['variables']) == (jobs[0]['id'], ['SBATCH_MEM_PER_CPU',
                                                                                          'SBATCH_QOS'])


#: The site; the step's command; how the test ends the run: by `cancel`, by aging its record past the ten minutes,
#: or by asking for more time than its partition allows; the claim its job finds when the job is held until after
#: that; and how the run ends.
_NEVER = 'Slurm will never start the run (PartitionTimeLimit): give a shorter time='
_ENDS: list[tuple[str, tuple[str, ...], str | None, str | None, tuple[Any, ...]]] = [
    *((kind, ('false',), None, None, (RunState.FAILED, 'the runner exited with status 1', [], 1))
      for kind in _SITES),
    *((kind, ('sleep', '600'), 'cancel', None, (RunState.CANCELLED, 'cancelled', [], 143)) for kind in _SITES),
    *((kind, ('sleep', '600'), 'cancel', 'cancelled', (RunState.CANCELLED, 'cancelled', [], None))
      for kind in _SITES),
    ('machine', ('sleep', '600'), 'age', 'none', (RunState.FAILED, sites._NEVER_CLAIMED, [], None)),
    ('slurm', ('true',), 'never', None, (RunState.FAILED, _NEVER, [], None))]


@pytest.mark.parametrize(('kind', 'command', 'end', 'held', 'ended'), _ENDS)
def test_a_run_that_fails_is_cancelled_or_never_starts_ends_as_its_folder_says(  # pylint: disable=R0913,R0917
        site: Path, slurm: Path, monkeypatch: pytest.MonkeyPatch, kind: str, command: tuple[str, ...],
        end: str | None, held: str | None, ended: tuple[Any, ...]) -> None:
    """A failed run is read by Toil's own outcome, over the transport, which names no failed step. `cancel` stops a
    running leader, whose trap records 143, and a second cancel finds it CANCELLED. A run that no job has claimed is
    claimed by `cancel`, or by `status` ten minutes after its submit, which reads it QUEUED while it cannot write the
    claim: its job, held until then, finds the claim, and exits writing nothing.
    On Slurm, a run whose leader Slurm will never start is stopped, and `reason` says which setting to change; while
    Slurm does not answer, `status` raises and decides nothing, and while it refuses the stop's `scancel`, it writes
    neither the claim nor the `reason`."""
    if held and kind == 'machine':   # the job's login profile, not a call's, waits
        (site / '.bash_profile').write_text('[ -n "${BASH_EXECUTION_STRING-}" ] || sleep 3\n', encoding='utf-8')
        monkeypatch.setenv('HOME', str(site))
    elif held:
        (slurm / 'hold').touch()
    settings: dict[str, Any] = {'partition': 'short', 'time': '1:00:00'} if end == 'never' else {}
    run = _SITES[kind]('localhost', root=str(site), bin=_bin('toil-cwl-runner', site), **settings).submit(
        _step('step', *command))
    folder = site / 'sophios' / 'runs' / run.id
    while end == 'cancel' and not held and run.status().state is RunState.QUEUED:
        time.sleep(0.5)
    while end == 'never' and fake_slurm.jobs(slurm)[0]['reason'] != 'PartitionTimeLimit':
        time.sleep(0.1)
    for broken in ('squeue', 'scancel') if end == 'never' else ():
        (slurm / 'broken').write_text(broken, encoding='utf-8')
        with pytest.raises(SophiosError, match='Unable to contact slurm controller'):
            run.status()
        assert not (folder / 'leader').exists() and not (folder / 'reason').exists()
    (slurm / 'broken').unlink(missing_ok=True)
    assert end != 'cancel' or (run.cancel() and run.cancel())
    if end == 'age':   # a claim that cannot be written decides nothing
        os.utime(folder / 'run.json', (time.time() - 60 * sites.UNCLAIMED_MINUTES - 60,) * 2)
        folder.chmod(0o555)
        assert run.status().state is RunState.QUEUED
        folder.chmod(0o755)
    status = run.wait(timeout=300)
    assert (status.state, status.reason, [(s.name, s.exit_status) for s in status.steps], status.exit_code) == ended
    (slurm / 'hold').unlink(missing_ok=True)
    time.sleep(4 if held and kind == 'machine' else 0)
    _settled(slurm)   # Slurm's job, cancelled, ends
    assert not held or (os.readlink(folder / 'leader') == held and not (folder / 'exit').exists())
    assert end != 'never' or (folder / 'reason').read_text() == _NEVER and fake_slurm.jobs(slurm)[0]['state'] == (
        'CANCELLED by 1000')


@pytest.mark.parametrize('lost', ['before', 'after'])
def test_a_submit_whose_answer_is_lost_adopts_the_job_slurm_took_or_submits_it_again(
        site: Path, slurm: Path, lost: str) -> None:
    """When `sbatch` fails before Slurm took the job, or after, `submit` asks Slurm for a job named after the run:
    it adopts the one Slurm took, or submits the same folder again; either way the run has one leader, and runs. A
    second leader of the same folder starts only once the first has ended (`singleton`), finds the run claimed, and
    exits 0 having changed nothing."""
    (slurm / 'lose').write_text(lost, encoding='utf-8')
    (slurm / 'hold').touch()
    run = sites.Slurm('localhost', root=str(site), bin=_bin('toil-cwl-runner', site)).submit(
        _step('copy', 'cat', source='data/in.txt'))
    folder = site / 'sophios' / 'runs' / run.id
    again = subprocess.run(['sbatch', '--parsable', str(folder / 'job.sh')], capture_output=True, text=True,
                           check=True).stdout.strip()
    (slurm / 'hold').unlink()
    status = run.wait(timeout=300)
    assert status.state is RunState.COMPLETED, run.logs()
    first, later = [job for job in _settled(slurm) if job['name'] == run.id]   # the leaders, not Toil's step
    assert (first['name'], later['id'], later['state'], later['start'] >= first['end']) == (
        run.id, again, 'COMPLETED', True)
    assert (os.readlink(folder / 'leader'), (folder / 'exit').read_text()) == (first['id'], '0\n')
    assert sites.Slurm('localhost', root=str(site), bin='/b').attach(run.id).status() == status


def test_a_leader_killed_hard_reads_failed_and_the_step_jobs_it_left_are_cancelled(site: Path, slurm: Path) -> None:
    """Toil's leader killed with its job (SIGKILL), its step job still running: two minutes after Slurm says the
    leader ended, the run reads FAILED, naming the scratch it left and how to remove it, and its step job, found by
    the run's comment, is cancelled; while Slurm refuses that `scancel`, `status` raises and decides nothing."""
    run = sites.Slurm('localhost', root=str(site), bin=_bin('toil-cwl-runner', site)).submit(_step('step', 'sleep',
                                                                                                   '600'))
    while len(jobs := fake_slurm.jobs(slurm)) < 2 or jobs[1]['state'] != 'RUNNING':
        time.sleep(0.5)
    os.killpg(jobs[0]['pid'], signal.SIGKILL)
    while 'end' not in (leader := fake_slurm.jobs(slurm)[0]):
        time.sleep(0.2)
    (slurm / f'job-{leader["id"]}.json').write_text(json.dumps({**leader, 'end': leader['end'] - 121}))
    (slurm / 'broken').write_text('scancel', encoding='utf-8')
    with pytest.raises(SophiosError, match='Unable to contact slurm controller'):
        run.status()
    (slurm / 'broken').unlink()
    status = run.status()
    scratch = site / 'sophios' / 'runs' / run.id / 'toil'
    assert (status.state, status.reason) == (RunState.FAILED, 'its job was killed before it recorded how the run ended '
                                             f'(Slurm: FAILED); {scratch} is left: rm -rf {scratch} removes it')
    assert _settled(slurm)[1]['state'] == 'CANCELLED by 1000'
