"""`Slurm`, a cluster's login node as a site: the scheduler's part of every site (`_Site`) as Slurm has it, where a
run's jobs go there, and what its steps ask for."""
# pylint: disable=protected-access  # a site starts its run in the run's folder
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, replace
from pathlib import PurePosixPath
from typing import Any, ClassVar, Final

from ..compute import slurm_problems
from ..ir.resolved import ResolvedWorkflow, tool_steps
from ..lang.diagnostics import Diagnostic, SophiosError
from ..lang.error_codes import SophiosErrorCode as Code
from ..runtime.runners import Cwltool, Nextflow, Runner, SlurmSettings, Toil
from ..wic_types import Tool

from ._run import Run
from ._runners import ON_SLURM, nextflow_on_slurm, seconds
from ._site import _Placed, _Site, _error, _started, _winning
from ._ssh import _ANSWER, _Said, _call, _q, _unreachable, host
from ._states import Job, read_jobs


#: The Slurm commands a run on Slurm uses, each asked so it answers; `sacct` must have accounting behind it.
_SLURM: Final = ('sbatch --version', 'squeue --version', 'scancel --version', 'sacct -n -X -P -o JobID -S now',
                 "sinfo -a -h -o '%P|%l|%G'")
#: Your `SBATCH_` settings a check reads: those a run reads, then those that send jobs to another cluster.
_VARIABLES: Final = ('SBATCH_ACCOUNT', 'SBATCH_PARTITION', 'SBATCH_TIMELIMIT', 'SBATCH_CLUSTERS', 'SLURM_CLUSTERS')
#: The submit's time, as `sacct --starttime` reads it, in a script that runs in the run's folder: run.json's.
_SUBMITTED: Final = '"$(date -r run.json +%Y-%m-%dT%H:%M:%S)"'
#: How the `reason` of a run whose job Slurm refused, twice, starts; what `sbatch` said follows.
_REFUSED: Final = 'Slurm refused its job: '
#: Your `SBATCH_ACCOUNT`, as the leader's `#SBATCH` lines, Toil's `--slurmArgs`, which it splits as a shell does, and
#: the `#SBATCH` line of a Nextflow task's `clusterOptions` carry it.
_ACCOUNT: Final = re.compile(r'[A-Za-z0-9_./+-]+')
#: The `sbatch` variables that set each option of a job script's header, the leader's or a Nextflow task's, which beat
#: its `#SBATCH` line (sbatch(1), INPUT ENVIRONMENT VARIABLES): three set the memory, two the requeue.
_SBATCH_VARIABLES: Final[Mapping[str, tuple[str, ...]]] = {
    'job-name': ('SBATCH_JOB_NAME',), 'comment': (), 'account': ('SBATCH_ACCOUNT',),
    'partition': ('SBATCH_PARTITION',), 'time': ('SBATCH_TIMELIMIT',), 'cpus-per-task': (),
    'mem': ('SBATCH_MEM_PER_NODE', 'SBATCH_MEM_PER_CPU', 'SBATCH_MEM_PER_GPU'), 'gres': ('SBATCH_GRES',),
    'output': ('SBATCH_OUTPUT',), 'error': ('SBATCH_ERROR',), 'open-mode': ('SBATCH_OPEN_MODE',), 'chdir': (),
    'no-requeue': ('SBATCH_NO_REQUEUE', 'SBATCH_REQUEUE'), 'dependency': (), 'signal': ('SBATCH_SIGNAL',)}
#: Each option of a task's header Nextflow writes on Slurm (`-J`, `-o`, `--no-requeue`, `--signal`, `-c`, `-t`,
#: `--mem`, `-p`, and the comment, the account and a GPU task's `--gres` of `clusterOptions`), and `error`, which it
#: leaves to `-o`, one file for both streams. The leader's login shell sets your `SBATCH_` variables again, which would
#: beat a task's lines, so its job unsets those of these options before Nextflow starts; the account a task asks for is
#: the one `check` read, written into `clusterOptions`.
_TASK_OPTIONS: Final = ('job-name', 'output', 'error', 'no-requeue', 'signal', 'cpus-per-task', 'time', 'mem',
                        'partition', 'comment', 'account', 'gres')


@dataclass(frozen=True)
class Slurm(_Site):
    """A Slurm cluster's login node you can `ssh` to, as a site: each run is one job, its leader, and under Toil each
    step, under Nextflow each task, a job of its own (design Appendix A). Its compute nodes share `runs`, and the
    folders the run reads and writes, with it. Where the jobs go and for how long is the config's `SlurmSettings`, else
    your `SBATCH_` settings there, else the cluster's; the account is your `SBATCH_ACCOUNT`, else Slurm's default.

    Args:
        url: As `Machine` takes it.
        runs: As `Machine` takes it.
        bin: As `Machine` takes it.
        container_engine: The engine every runner runs containers with: `singularity`, which Apptainer provides.

    Raises:
        SophiosError: `wic062` on Windows itself, as a `Machine`.
        ValueError: As a `Machine` raises it; a `runs` of letters, digits and `/ . _ - +` only is also one a job's
            `#SBATCH` lines hold.
    """

    url: str
    _: KW_ONLY
    runs: str
    bin: str | None = None
    container_engine: str = 'singularity'
    _RUNNERS: ClassVar = ON_SLURM
    _DEFAULT_MEMORY: ClassVar = (Toil.name, Nextflow.name)
    _CLAIMANT: ClassVar = '$SLURM_JOB_ID'

    def _questions(self) -> list[str]:
        """Each Slurm command, then your `SBATCH_` settings a run reads and those that send jobs to another cluster."""
        return [*(f'runs {command}' for command in _SLURM), *(f'variable {name}' for name in _VARIABLES)]

    def _slurm(self, slurm: SlurmSettings, runner: Runner) -> None:
        """Check each setting of `slurm` the run uses is in a form the compute service takes: all six under Toil and
        Nextflow, whose steps are jobs of their own; under cwltool, whose run is one job, all but the steps' partition
        and time, which are neither used nor refused.

        Raises:
            ValueError: Each setting the run uses in a form the compute service refuses, a line each, in the compute
                client's words (`sophios.compute.slurm_problems`).
        """
        if problems := slurm_problems(slurm if runner.name != Cwltool.name else
                                      replace(slurm, steps_partition=None, steps_time=None)):
            raise ValueError('\n'.join(problems))

    def _place(self, said: Sequence[str], *, slurm: SlurmSettings, runner: str,
               view: ResolvedWorkflow) -> tuple[list[Diagnostic], _Placed]:
        """`wic067` at each Slurm command that does not answer, at each of your settings that sends jobs to another
        cluster or that the run cannot take, and at each problem of where the run's jobs go (`placement`), all at once;
        and where they go, which is used only when there is none."""
        there = host(self.url)
        account, partition, limit, *clusters = said[len(_SLURM):]
        problems = [f'on {there}, `{command}` ' + (
            'is not on PATH: your login profile there gives the one the cluster provides' if line == 'missing' else
            f'exited with status {line.partition(" ")[0]} ({line.partition(" ")[2].strip()[:300]})'
            + (': a run is read from sacct, so accounting must answer' if command.startswith('sacct') else ''))
            for command, line in zip(_SLURM, said) if not line.startswith('0 ')]
        problems += [f'your login profile on {there} sets {name}={value.strip()}, which sends jobs to another cluster, '
                     'where no runner follows its step jobs: remove it there'
                     for name, value in zip(_VARIABLES[3:], clusters) if value.strip()]
        if account and not _ACCOUNT.fullmatch(account):
            problems.append(f'your SBATCH_ACCOUNT on {there}, {account!r}, is not an account the leader\'s header and '
                            "each step's options can carry, letters, digits and _ . / + - only: correct it in your "
                            'login profile there')
        placed: _Placed = {}
        if (sinfo := said[len(_SLURM) - 1]).startswith('0 '):
            found, placed = placement([row.split('|') for row in sinfo.split(' ')[1:] if row.count('|') == 2],
                                      (partition, limit), slurm, runner, _needs(view), there)
            problems += found
        return ([_error(Code.SITE_SLURM, problem) for problem in problems],
                {**placed, **({'account': (account, 'your SBATCH_ACCOUNT')} if account else {})})

    def _header(self, run: str, read: Mapping[str, str]) -> list[str]:
        """The leader's `#SBATCH` lines (design Appendix A), given where the site placed the run, `read`: named after
        the run, with its comment, the account, its partition and time, the memory when one is set, and, for cwltool,
        the run's one job, the largest step's cores and GPUs; its log the run's; in one leader at a time (`singleton`),
        never requeued."""
        run_id = PurePosixPath(run).name
        options = [('job-name', run_id), ('comment', f'sophios:{run_id}'), ('account', read.get('account')),
                   ('partition', read['partition']), ('time', read['time']), ('mem', read.get('memory')),
                   ('cpus-per-task', read.get('cpus')), ('gres', f'gpu:{read["gpus"]}' if 'gpus' in read else None),
                   ('output', f'{run}/run.log'), ('error', f'{run}/run.log'), ('open-mode', 'append'), ('chdir', run),
                   ('no-requeue', ''), ('dependency', 'singleton')]
        return [f'#SBATCH --{option}' + (f'={value}' if value else '') for option, value in options
                if value is not None]

    def _block(self, run: str, read: Mapping[str, str], runner: Runner) -> dict[str, dict[str, str]]:
        """For Nextflow, each task a Slurm job of its own, where the run placed its steps (`nextflow_on_slurm`)."""
        return nextflow_on_slurm(run, read) if runner.name == Nextflow.name else {}

    def _before(self, runner: Runner) -> list[str]:
        """For Nextflow, your `SBATCH_` variables of each option of a task's header unset (`_TASK_OPTIONS`)."""
        return [f'unset {" ".join(name for option in _TASK_OPTIONS for name in _SBATCH_VARIABLES[option])}'] \
            if runner.name == Nextflow.name else []

    def _leader(self, run: str, job: str) -> list[str]:
        """`sbatch --parsable`, the `SBATCH_` variables of each option `job`'s header sets unset: they beat its
        lines."""
        unset = [name for option in re.findall(r'^#SBATCH --([a-z-]+)', job, re.M)
                 for name in _SBATCH_VARIABLES[option]]
        return [f'unset {" ".join(unset)}', f'sbatch --parsable {_q(f"{run}/job.sh")}']

    def _again(self, run: Run, job: str, error: SophiosError) -> bytes:
        """The leader Slurm took, adopted, where the compute service looks for it: a job named after the run that squeue
        lists, else one sacct lists since the submit; else `sbatch` again on the same folder, which the claim makes
        safe, as a leader submitted again starts after one that ran (`singleton`), and finds the run claimed. When Slurm
        refuses that too, the run is claimed `none`, so no job of it Slurm took all the same runs it, and its `reason`
        says why: it did not start, and its outputs folder is free. Only when that cannot be told does the error say
        how to follow the run."""
        unset, submitted = self._leader(run._folder, job)
        name = _q(run.id)
        try:
            said = _call(self.url, '\n'.join([
                "trap '' HUP PIPE", f'cd {_q(run._folder)} || exit 1',
                f'jobs=$(squeue -a -h -u "$(id -un)" -n {name} -o %i) || exit 1',
                f'[ -n "$jobs" ] || jobs=$(sacct -n -X --name={name} --starttime={_SUBMITTED} -o JobID) || exit 1',
                'if [ -n "$jobs" ]; then echo $jobs; exit 0; fi', unset,
                f'if {{ refused=$({submitted} 2>&1 1>&3 3>&-); }} 3>&1; then exit 0; fi',
                'ln -s none leader 2> /dev/null || { readlink leader; exit 0; }',
                f'printf %s {_q(_REFUSED)}"$refused" > reason.partial; mv reason.partial reason',
                'printf "refused\\n%s" "$refused"']))
        except SophiosError as again:
            raise SophiosError([*error.diagnostics, *again.diagnostics, _started(run)]) from None
        if not said.startswith(b'refused\n'):
            return said
        raise SophiosError([*error.diagnostics, _error(Code.SITE_UNREACHABLE, (
            f'Slurm refused run {run.id} again ({said[8:].decode("utf-8", "replace").strip()[:300]}): it did not '
            'start, and no job of it will, so its outputs folder is free: correct what Slurm says, and submit again'))])

    def _asked(self, run_id: str) -> list[str]:
        """The jobs of yours squeue lists, with their comment, and the leaders sacct lists by the run's name since the
        submit, with when they ended."""
        return [_ANSWER, 'export SLURM_TIME_FORMAT=%s', 'answer squeue -a -h -u "$(id -un)" -o \'%i|%T|%r|%k|%j\'',
                f'answer sacct -n -X -P --name={_q(run_id)} --starttime={_SUBMITTED} -o JobID,State,End']

    def _queue(self, run_id: str, said: _Said, now: int) -> Callable[[], Sequence[Job]]:
        """The run's jobs, from what `_asked` said; asked for, a Slurm that did not answer raises `wic062`."""
        answers = [('squeue', *said.answer()), ('sacct', *said.answer())]

        def listed() -> tuple[Job, ...]:
            for command, status, text in answers:
                if status:
                    raise _unreachable(self.url, f'Slurm did not answer, and nothing was decided: {command} exited '
                                       f'with status {status} ({text.strip()[:300] or "it said nothing"})')
            return read_jobs(run_id, answers[0][2], answers[1][2], now)
        return listed

    def _cancelling(self, run: str) -> list[str]:
        """`scancel` of every job named after the run, which stops one whether or not it has claimed the run, and
        only then the claim: a `scancel` Slurm refuses ends the call, which raises, having written nothing."""
        return [f'scancel -u "$(id -un)" -n {_q(PurePosixPath(run).name)} || exit 1',
                'ln -s cancelled leader 2> /dev/null || true']

    def _unfinished(self) -> str:
        """A machine's, and a run of which Slurm lists a job has not ended either, whatever its folder says, for its
        leader may yet claim it, or empty its outputs folder: with no `exit`, a run Slurm lists, or any run when squeue
        does not answer, has not ended."""
        return '\n'.join([
            'listed=$(squeue -a -h -u "$(id -un)" -o %j 2> /dev/null) || listed=-',
            super()._unfinished().replace('unfinished()', 'unlisted()', 1),
            'unfinished() { [ ! -e "$1/exit" ] && { [ "$listed" = - ] || printf "%s\\n" "$listed" | grep -qxF -- '
            '"${1##*/}" || unlisted "$1"; }; }'])


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals  # the conventions' table
def placement(rows: Sequence[Sequence[str]], variables: tuple[str, str], slurm: SlurmSettings, runner: str,
              needs: tuple[int, int, int], there: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Where a run's jobs go on Slurm (design §5), each setting with where it came from; else every problem, a line
    each, naming the setting to give.

    Given `sinfo -a`'s rows (a partition's name, `*` marking the default, its time limit and its GRES), your
    `SBATCH_PARTITION` and `SBATCH_TIMELIMIT` (`variables`), the config's Slurm settings, the runner's name, and the
    most cores, memory in MiB and GPUs a step states (`needs`): each setting from the one that names it, else by the
    convention: the run's partition your `SBATCH_PARTITION`, else the cluster's default; its time your
    `SBATCH_TIMELIMIT`, else the partition's longest; under a runner whose steps are jobs of their own (Toil,
    Nextflow), the steps' partition the run's, and their time the shorter of the run's and the shortest limit of their
    partitions; a step that asks for a GPU, the one partition with GPUs. Under cwltool the run is one job, asking for
    the largest cores, memory and GPUs a step states, on the GPU partition when a step asks for a GPU. A partition a
    job goes to must be one `sinfo -a` lists; a value Slurm or your profile gives is held to the forms of the setting
    that would give it (`slurm_problems`), as it reaches a job's `#SBATCH` lines, Toil's flags or Nextflow's
    configuration; and the run must have a time limit, as on the compute service."""
    partition, limit = variables
    limits = {name.rstrip('*'): row_limit for name, row_limit, _ in rows}
    gpus = sorted({name.rstrip('*') for name, _, gres in rows if gres != '(null)'})
    cores, memory, devices = needs
    steps = runner != Cwltool.name
    chosen = ((slurm.partition, 'SlurmSettings(partition=...)') if slurm.partition else
              (partition, 'your SBATCH_PARTITION') if partition else
              (next((name[:-1] for name, _, _ in rows if name.endswith('*')), ''), "the cluster's default"))
    gpu = ((slurm.gpu_partition, 'SlurmSettings(gpu_partition=...)') if slurm.gpu_partition else
           (gpus[0] if len(gpus) == 1 else '', 'the one partition with GPUs')) if devices else None
    placed = {'partition': gpu if gpu and not steps else chosen}
    if steps:
        placed['steps_partition'] = ((slurm.steps_partition, 'SlurmSettings(steps_partition=...)')
                                     if slurm.steps_partition else (chosen[0], "the run's partition"))
        if gpu:
            placed['gpu_partition'] = gpu
    named = {setting: 'gpu_partition' if found == gpu else setting for setting, found in placed.items()}
    problems = [_unlisted(there, name, source, limits, gpus) + f'name one with SlurmSettings({named[setting]}=...)'
                for setting, (name, source) in placed.items() if name not in limits and source != "the run's partition"]
    problems += [f'on {there}, the partition {name!r} ({source}) is not a name a job\'s #SBATCH lines can carry, '
                 f"which SlurmSettings({named[setting]}=...) takes: name one with it" for setting, (name, source)
                 in placed.items() if name in limits and source in ("the cluster's default", 'your SBATCH_PARTITION',
                                                                    'the one partition with GPUs')
                 and slurm_problems(SlurmSettings(partition=name))]
    if problems:
        return problems, {}
    placed['time'] = ((slurm.time, 'SlurmSettings(time=...)') if slurm.time else
                      (limit, 'your SBATCH_TIMELIMIT') if limit else
                      (limits[placed['partition'][0]], "the partition's longest"))
    if not slurm.time and slurm_problems(SlurmSettings(time=placed['time'][0])):
        return [f"on {there}, the run's time would be {placed['time'][0]!r} ({placed['time'][1]}), which is not a "
                'time limit above 0 that SlurmSettings(time=...) takes, as the compute service asks for one too: give '
                'SlurmSettings(time=...)'], {}
    if slurm.memory or (memory and not steps):
        placed['memory'] = ((slurm.memory, 'SlurmSettings(memory=...)') if slurm.memory else
                            (f'{memory}M', "the largest step's ramMin"))
    if steps and slurm.steps_time:
        placed['steps_time'] = (slurm.steps_time, 'SlurmSettings(steps_time=...)')
    elif steps:
        on = [placed['steps_partition'][0], *([gpu[0]] if gpu else [])]
        if unread := [name for name in on if limits[name] != 'infinite'
                      and slurm_problems(SlurmSettings(time=limits[name]))]:
            return [f'on {there}, sinfo -a gives the partition {name} the time limit {limits[name]!r}, which is not a '
                    'time SlurmSettings(steps_time=...) takes: give SlurmSettings(steps_time=...)'
                    for name in unread], {}
        shortest = min((limits[name] for name in on), key=seconds)
        placed['steps_time'] = ((placed['time'][0], "the run's time") if seconds(placed['time'][0]) <= seconds(shortest)
                                else (shortest, "the shortest limit of the steps' partitions"))
    else:
        placed['cpus'] = (str(cores), "the largest step's coresMin")
        if devices:
            placed['gpus'] = (str(devices), "the largest step's cudaDeviceCountMin")
    return [], placed


def _unlisted(there: str, name: str, source: str, limits: Mapping[str, str], gpus: Sequence[str]) -> str:
    """The start of the line for a partition, `name` from `source`, that `sinfo -a` does not list, or for the one the
    convention found none of."""
    if name:
        return f"{there} has no partition {name} ({source}): sinfo -a lists {', '.join(limits) or 'none'}; "
    if source == "the cluster's default":
        return f"{there} has no default partition: sinfo -a lists {', '.join(limits) or 'none'}; "
    if gpus:
        return f"{there} has {len(gpus)} partitions with GPUs, {', '.join(gpus)}, and a step asks for a GPU: "
    return (f"{there} has no partition with GPUs, and a step asks for one: sinfo -a lists "
            f"{', '.join(limits) or 'none'}; ")


def _needs(view: ResolvedWorkflow) -> tuple[int, int, int]:
    """The most cores, at least 1, memory, in MiB, and GPUs any of the workflow's steps states in numbers, as the
    requirement that wins at it states them (`_stated`); 0 memory for none stated, and 0 GPUs when no step asks for
    one, a `cwltool:CUDARequirement`, which asks for at least 1."""
    cores, memory, devices = 1, 0, 0
    for tool in tool_steps(view):
        if not isinstance(tool.step.run, Tool):
            continue
        cwl, inherited = tool.step.run.cwl, tool.step.inherited
        if isinstance(found := _winning(cwl, inherited, 'ResourceRequirement'), Mapping):
            cores, memory = max(cores, _stated(found, 'cores')), max(memory, _stated(found, 'ram'))
        if (found := _winning(cwl, inherited, 'cwltool:CUDARequirement')) is not None:
            devices = max(devices, _stated(found, 'cudaDeviceCount') if isinstance(found, Mapping) else 0, 1)
    return cores, memory, devices


def _stated(requirement: Mapping[str, Any], name: str) -> int:
    """What `requirement` reserves of `name`, as CWL reads it, `<name>Min`, else `<name>Max`, rounded up; 0 when it
    states neither, or an expression, which is not read here."""
    found = requirement.get(f'{name}Min', requirement.get(f'{name}Max'))
    return math.ceil(found) if isinstance(found, (int, float)) else 0
