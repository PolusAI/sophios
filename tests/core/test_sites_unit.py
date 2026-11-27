"""The parts of `sophios.sites` that need no site: its states, its URL, how it quotes and reads values, the scripts it
writes, run by a local `bash`, and what it refuses before anything is sent."""
# pylint: disable=protected-access  # the properties read the package's own transport, reader, scripts and states
import copy
import dataclasses
import itertools
import os
import re
import shlex
import subprocess
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, strategies as st

from sophios import sites, targets
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.ir.resolved import Requirement
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode as Code
from sophios.runtime import runners
from sophios.runtime.backends import RunRequest
from sophios.runtime.runners import Cwltool, Nextflow, Runner, RunSettings, SlurmSettings, Toil
from sophios.runtime.status import RunState
from sophios.sites import RunConfig, _runners, _site, _ssh
from sophios.sites._states import Folder, decide

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


def _step(name: str, *command: str, source: str | None = None, kind: str = 'File') -> Workflow:
    """A workflow of one step, `<command> <source>`, `source` a File or a Directory, keeping its stdout as `out.txt`."""
    inputs = Inputs(source=Input(cwl.file if kind == 'File' else cwl.directory, position=1)) \
        if source is not None else Inputs()
    tool = CommandLineTool(name, inputs, Outputs(out=Output(cwl.file, glob='out.txt'))).base_command(*command)
    step = Step(tool.stdout('out.txt'), step_name=name)
    if source is not None:
        step.inputs.source = {'class': kind, 'location': source}
    workflow = Workflow([step], 'site')
    workflow.outputs.out = step.outputs.out
    return workflow


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
    """Over every life a run's folder can have (design Appendix B), each snapshot reads as its cause, an end once read
    never changes, a `reason` a status wrote is the end whatever else is there, and only a hard kill empties."""
    for claim, code, scratch in itertools.product(('cancelled', 'none', '4242'), (None, *range(256)),
                                                  (None, '/r/toil')):
        ended: tuple[RunState, str | None] | None = None
        for folder, state in _life(claim, code):
            found = decide(folder, scratch)
            assert found.state is state and ended in (None, (found.state, found.reason)), (claim, code, folder)
            stopped = decide(dataclasses.replace(folder, reason='stopped'), scratch)
            assert (stopped.state, stopped.reason, stopped.empty) == (RunState.FAILED, 'stopped', False)
            if state not in (RunState.QUEUED, RunState.RUNNING):
                ended = (found.state, found.reason)
                assert found.empty is (claim.isdigit() and code is None)
                if found.empty:
                    after = decide(Folder(claim, False, None, found.reason), scratch)
                    assert (after.state, after.reason, after.empty) == (state, found.reason, False)
        assert ended is not None
        assert (scratch is not None and claim.isdigit() and code is None) is (f'rm -rf {scratch}' in str(ended[1]))


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
#: Each setting a Machine refuses when it is made, with what the ValueError names, then each of `_CHECKED`.
_REFUSED: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = [
    *(({name: value}, None, f'{name}={value!r}') for name in ('runs', 'bin') for value in (
        'r', '/r b', "/r'b", '/r"b', '/r$(touch x)', '/r`x`', '/r\nb', '/r:b', '/r#b', '/r%j', '/r\\b', '/r\u00e9')),
    ({'container_engine': 7}, None, 'container_engine='), ({'bin': None}, {}, 'bin='),
    *(({}, options, said) for options, said in _CHECKED)]


@pytest.mark.fast
@pytest.mark.parametrize(('settings', 'options', 'said'), _REFUSED)
def test_no_setting_is_taken_that_a_site_cannot_read(settings: dict[str, Any], options: dict[str, Any] | None,
                                                     said: str) -> None:
    """Each is a ValueError naming what to change, before anything is sent; a runner value's own settings stay their
    defaults, and a flag whose option the site sets, by any of its names, is refused."""
    with _local(), pytest.raises(ValueError, match=re.escape(said)):
        machine = sites.Machine('ssh://h', **{'runs': '/r', 'bin': '/b', 'container_engine': 'singularity', **settings})
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


@pytest.mark.fast
@budget(25)
@given(st.integers(1, 1000), st.sampled_from(['always', 'onError', 'onSuccess', 'never']),
       st.sampled_from(['docker', 'podman', 'singularity']), st.sampled_from(['out', 'results/p1']))
def test_toils_command_line_on_a_machine_reads_back_as_the_site_set_it(
        tmp_path_factory: pytest.TempPathFactory, max_parallel: int, clean: str, engine: str, outdir: str) -> None:
    """The runner's line of `job.sh`, as Toil's own parser reads it (design Appendix A): run from `bin`, the run's
    `--clean` winning over the launch's, then the user's flags."""
    from toil.cwl.cwltoil import get_options  # pylint: disable=import-outside-toplevel  # it imports `pwd`
    run = str(tmp_path_factory.mktemp('runs') / 'wf-1')
    runner = Toil(*_runners.ON_A_MACHINE[Toil.name](run, {'max_parallel': str(max_parallel), 'clean': clean}),
                  '--logLevel', 'DEBUG', '--targetTime', '7', container_engine=engine)
    script = sites.Machine('ssh://h', runs='/r', bin='/opt/run/bin', container_engine=engine)._job(
        RunRequest('wf', Path(run), RunSettings(runner, f'{run}/{outdir}')), clean, ())
    words = shlex.split(script.splitlines()[-1])
    argv = words[1:words.index('|')]
    os.makedirs(f'{run}/toil/work')
    read = get_options(argv[1:])
    assert argv[0] == '/opt/run/bin/toil-cwl-runner'
    assert (read.batchSystem, read.max_jobs, read.clean, read.defaultMemory, read.cwl_default_ram, read.logLevel,
            read.targetTime) == ('single_machine', max_parallel, clean, 4 * 2 ** 30, False, 'DEBUG', 7)
    assert (read.bypass_file_store, read.rm_tmpdir, read.workDir, read.tmpdir_prefix, read.tmp_outdir_prefix) == (
        True, False, f'{run}/toil/work', f'{run}/toil/tmp/t', f'{run}/toil/tmp/o')
    assert (read.singularity, read.user_space_docker_cmd) == (engine == 'singularity', {'podman': 'podman'}.get(engine))
    assert (read.outdir, read.jobStore, read.cwltool, read.cwljob) == (
        f'{run}/{outdir}', f'file:{run}/jobStore_wf', f'{run}/wf.cwl', [f'{run}/wf_inputs.yml'])


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
    job = machine._job(RunRequest('wf', run, RunSettings(Toil(), f'{run}/out')), 'always', ())
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
    for the run's own. A machine reads the log level and ignores Slurm settings."""
    config = RunConfig(folder) if relative_to is None else RunConfig(folder).relative_to(relative_to)
    assert _site.site_config(config.log_level('INFO')) == (read, relative_to, 'INFO')
    assert _site.site_config(config.slurm_settings(SlurmSettings(partition='p', time='1'))) == (read, relative_to,
                                                                                                'INFO')


@pytest.mark.fast
def test_a_site_runs_cwltool_no_older_than_sophios_requires_and_has_a_runner_for_every_target() -> None:
    """The oldest cwltool a site runs is the one `pyproject.toml` requires; every target has a default runner, and
    every runner a floor and a machine's flags."""
    project = tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text(encoding='utf-8'))
    [required] = [found for found in project['project']['dependencies'] if found.startswith('cwltool>=')]
    assert '.'.join(map(str, _runners.OLDEST[Cwltool.name])) == required.removeprefix('cwltool>=')
    assert set(_runners.DEFAULT_RUNNER) == set(targets.TARGETS)
    assert set(_runners.OLDEST) == {runner.name for runner in runners.RUNNERS} == set(_runners.ON_A_MACHINE)
