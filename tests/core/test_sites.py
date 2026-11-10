"""A site, `sophios.sites.Machine`, with a local `bash` for `ssh <host>`: the one thing these tests replace."""
# pylint: disable=protected-access  # the properties read the module's own transport, quoting and leader script
import copy
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sysconfig
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, strategies as st

from sophios import sites, targets
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.backends import RunRequest
from sophios.ir.resolved import Requirement
from sophios.lang.diagnostics import Severity, SophiosError
from sophios.lang.error_codes import SophiosErrorCode as Code
from sophios.run_status import RunState
from sophios.sites import Folder, decide

from .budgets import budget
from .helpers import adapter_step, live_workflow

#: A run id no run has.
_NO_RUN = 'site-20261109T000000-00000000'


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
    """The project folder: the working directory here, `root` there, with `data/in.txt`."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'in.txt').write_text('hello\n', encoding='utf-8')
    with _local():
        yield tmp_path


def _step(name: str, *command: str, source: str | None = None) -> Workflow:
    """A workflow of one step, `<command> <source>`, which keeps its stdout as `out.txt`."""
    inputs = Inputs(source=Input(cwl.file, position=1)) if source is not None else Inputs()
    tool = CommandLineTool(name, inputs, Outputs(out=Output(cwl.file, glob='out.txt'))).base_command(*command)
    step = Step(tool.stdout('out.txt'), step_name=name)
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


def _life(claim: str, code: int | None) -> list[tuple[Folder, RunState]]:
    """A run's life as its folder shows it, each snapshot with the state its cause gives: unclaimed; then claimed
    `cancelled` or `none`, or by its leader, which runs, may write `exit` while it ends, and is gone."""
    life = [(Folder('', False, None), RunState.QUEUED)]
    if not claim.isdigit():
        return [*life, (Folder(claim, False, None), RunState.CANCELLED if claim == 'cancelled' else RunState.FAILED)]
    life.append((Folder(claim, True, None), RunState.RUNNING))
    if code is None:   # a hard kill: gone, with no `exit`
        return [*life, (Folder(claim, False, None), RunState.FAILED)]
    end = {0: RunState.COMPLETED, 143: RunState.CANCELLED}.get(code, RunState.FAILED)
    return [*life, (Folder(claim, True, code), end), (Folder(claim, False, code), end)]


@pytest.mark.fast
def test_a_runs_state_is_its_folders_and_an_end_never_changes() -> None:
    """Over every life a run's folder can have on a machine (design Appendix B): each snapshot reads as the state
    that brought it about, and once a status read an end, every later one reads the same end, for the same reason.
    Only a hard kill of a Toil run names the scratch it leaves."""
    for claim in ('cancelled', 'none', '4242'):
        for code in (None, *range(256)):
            for scratch in (None, '/r/toil'):
                ended = None
                for folder, state in _life(claim, code):
                    found = decide(folder, scratch)
                    assert found[0] is state and (ended is None or found == ended), (claim, code, folder)
                    if state not in (RunState.QUEUED, RunState.RUNNING):
                        ended = found
                assert ended is not None
                assert (scratch is not None and claim.isdigit() and code is None) is (f'rm -rf {scratch}' in
                                                                                      str(ended[1]))


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


@pytest.mark.fast
@budget(25)
@given(st.integers(1, 1000), st.sampled_from(['always', 'onSuccess', 'never']),
       st.sampled_from(['docker', 'podman', 'singularity']), st.sampled_from(['out', 'results/p1']))
def test_toils_command_line_on_a_machine_reads_back_as_the_site_set_it(
        tmp_path_factory: pytest.TempPathFactory, max_parallel: int, clean: str, engine: str, outdir: str) -> None:
    """The runner's line of `job.sh`, as Toil's own parser reads it: Toil's own launch and a machine's flags
    (design Appendix A), `clean=` winning over the launch's `--clean always`, run from `bin`."""
    from toil.cwl.cwltoil import get_options  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    run = str(tmp_path_factory.mktemp('runs') / 'wf-1')
    _, script = sites.Machine('localhost', root='/r', bin='/opt/run/bin', container_engine=engine)._job(
        RunRequest('wf', Path(run), runner='toil-cwl-runner', container_engine=engine, outdir=f'{run}/{outdir}'),
        max_parallel, clean, ())
    words = shlex.split(script.splitlines()[-1])
    argv = words[1:words.index('|')]
    os.makedirs(f'{run}/toil/work')
    read = get_options(argv[1:])
    assert argv[0] == '/opt/run/bin/toil-cwl-runner'
    assert (read.batchSystem, read.max_jobs, read.clean, read.defaultMemory, read.cwl_default_ram) == (
        'single_machine', max_parallel, clean, 4 * 2 ** 30, False)
    assert (read.bypass_file_store, read.rm_tmpdir, read.workDir, read.tmpdir_prefix, read.tmp_outdir_prefix) == (
        True, False, f'{run}/toil/work', f'{run}/toil/tmp/t', f'{run}/toil/tmp/o')
    assert (read.singularity, read.user_space_docker_cmd) == (engine == 'singularity', {'podman': 'podman'}.get(engine))
    assert (read.outdir, read.jobStore, read.cwltool, read.cwljob) == (
        f'{run}/{outdir}', f'file:{run}/jobStore_wf', f'{run}/wf.cwl', [f'{run}/wf_inputs.yml'])


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


#: target, runner, whether `bin` holds no runner, the container engine, the workflow, outdir=, and what check finds.
_PROBLEMS = [
    ('cwl', 'toil-cwl-runner', True, 'docker', lambda: _step('copy', 'cat', source='data/gone.txt'), 'data',
     [Code.MISSING_INPUT_FILE, Code.SITE_RUNNER, Code.DIRECTORY_NOT_WRITABLE]),
    *(('cwl', 'toil-cwl-runner', False, 'docker', lambda path=path: _step('copy', 'cat', source=path), None,
       [Code.MISSING_INPUT_FILE]) for path in ('data/../data/in.txt', '~/in.txt', '$HOME/in.txt')),
    ('cwl', 'toil-cwl-runner', False, 'no-such-engine', lambda: live_workflow('site'), None,
     [Code.REALTIME_NOT_ASYNC, Code.CONTAINER_ENGINE_UNAVAILABLE]),
    ('nextflow', 'nextflow', True, 'podman', lambda: Workflow([adapter_step('touch', filename='empty.txt')], 'site'),
     None, [Code.SITE_RUNNER, Code.SITE_NEXTFLOW_CONTAINER])]


@pytest.mark.parametrize(('target', 'runner', 'empty', 'engine', 'workflow', 'outdir', 'codes'), _PROBLEMS)
def test_check_finds_every_problem_at_once_and_writes_nothing_and_submit_raises_them(  # pylint: disable=R0913,R0917
        site: Path, target: str, runner: str, empty: bool, engine: str, workflow: Any, outdir: str | None,
        codes: list[Code]) -> None:
    """A missing input, one a site cannot read as written (`..`, `~`, `$`), a runner not in `bin`, an outdir that
    exists, a real-time analysis, an engine not there, a Nextflow container without Docker: all at once, each by its
    code; neither side changes, and `submit` raises the same."""
    for folder in ('empty', '~', '$HOME'):   # each path a site refuses names a file that is there
        (site / folder).mkdir()
        (site / folder / 'in.txt').touch()
    machine = sites.Machine('localhost', root=str(site), container_engine=engine,
                            bin=str(site / 'empty') if empty else sysconfig.get_path('scripts'))
    workflow = workflow()
    before = sorted(site.rglob('*'))
    found = machine.check(workflow, target=target, runner=runner, outdir=outdir)
    assert sorted(d.code for d in found if d.severity is Severity.ERROR) == sorted(codes)
    with pytest.raises(SophiosError) as caught:
        machine.submit(workflow, target=target, runner=runner, outdir=outdir)
    assert sorted(d.code for d in caught.value.diagnostics if d.severity is Severity.ERROR) == sorted(codes)
    assert sorted(site.rglob('*')) == before


@pytest.mark.fast
@pytest.mark.parametrize('call', [
    lambda: sites.Machine('-oProxyCommand=sh', root='/r', bin='/b'), lambda: sites.Machine('h', root='r', bin='/b'),
    lambda: sites.Machine('h', root='/r', bin='/b;sh'), lambda: sites.Machine('h', root='/r b', bin='/b'),
    lambda: sites.Machine('h', root='/r', bin='/b', container_engine='docker sh'),
    lambda: sites.Machine('h', root='/r', bin='/b', modules=('a;b',)),
    lambda: sites.Machine('h', root='/r', bin='/b', modules='gcc'),  # type: ignore[arg-type]
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


_RUNNERS = [pytest.param(target, runner, marks=[pytest.mark.nextflow] if runner == 'nextflow' else [], id=runner)
            for target, row in targets.TARGETS.items() for runner in row.runners]


@pytest.mark.parametrize(('target', 'runner'), _RUNNERS)
def test_every_runner_of_the_target_table_runs_on_a_machine_or_is_refused_with_its_code(
        site: Path, monkeypatch: pytest.MonkeyPatch, target: str, runner: str) -> None:
    """Each runner of `targets.TARGETS` runs there from `bin`, reading a path relative to `root`, and `check` notes
    only Toil's step that declares no memory; its outputs are its backend's own outcome, read over the transport; the
    run's line attaches to it from anywhere; and its folder is its record: claimed by the leader's pid, `exit` 0, no
    Toil scratch left. Nextflow's executor takes `max_parallel` tasks at once, from the run's configuration. Or it is
    refused there with its code before anything is compiled or sent: cwltool, `wic058`, naming Toil, a site's default
    (decision 22)."""
    machine = sites.Machine('localhost', root=str(site), bin=_bin(targets.backend(target, runner).program, site))
    workflow = _step('copy', 'cat', source='data/in.txt')
    if targets.backend(target, runner).program not in sites.ON_A_MACHINE:
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


#: The step's command; how the test ends the run, by `cancel` or by aging its record past the ten minutes; the claim
#: its job finds when the job is held in its login profile until after that; and how the run ends.
_ENDS: list[tuple[tuple[str, ...], str | None, str | None, tuple[Any, ...]]] = [
    (('false',), None, None, (RunState.FAILED, 'the runner exited with status 1', [], 1)),
    (('sleep', '600'), 'cancel', None, (RunState.CANCELLED, 'cancelled', [], 143)),
    (('sleep', '600'), 'cancel', 'cancelled', (RunState.CANCELLED, 'cancelled', [], None)),
    (('sleep', '600'), 'age', 'none', (RunState.FAILED, f'its job never claimed the run within '
                                       f'{sites.UNCLAIMED_MINUTES} minutes of the submit', [], None))]


@pytest.mark.parametrize(('command', 'end', 'held', 'ended'), _ENDS)
def test_a_run_that_fails_is_cancelled_or_never_starts_ends_as_its_folder_says(  # pylint: disable=R0913,R0917
        site: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...], end: str | None, held: str | None,
        ended: tuple[Any, ...]) -> None:
    """A failed run is read by Toil's own outcome, over the transport, which names no failed step. `cancel` stops a
    running leader's process group with TERM, whose trap records 143, and a second cancel finds it CANCELLED. A run
    that no job has claimed is claimed by `cancel`, or by `status` ten minutes after its submit: its job, held until
    then, finds the claim, and exits writing nothing."""
    if held:   # the job's login profile, not a call's, waits
        (site / '.bash_profile').write_text('[ -n "${BASH_EXECUTION_STRING-}" ] || sleep 3\n', encoding='utf-8')
        monkeypatch.setenv('HOME', str(site))
    run = sites.Machine('localhost', root=str(site), bin=_bin('toil-cwl-runner', site)).submit(
        _step('step', *command))
    folder = site / 'sophios' / 'runs' / run.id
    while end == 'cancel' and not held and run.status().state is RunState.QUEUED:
        time.sleep(0.5)
    assert end != 'cancel' or (run.cancel() and run.cancel())
    if end == 'age':
        os.utime(folder / 'run.json', (time.time() - 60 * sites.UNCLAIMED_MINUTES - 60,) * 2)
    status = run.wait(timeout=300)
    assert (status.state, status.reason, [(s.name, s.exit_status) for s in status.steps], status.exit_code) == ended
    time.sleep(4 if held else 0)
    assert not held or (os.readlink(folder / 'leader') == held and not (folder / 'exit').exists())
