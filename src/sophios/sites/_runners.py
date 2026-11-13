"""What each site adds to each runner (the table of design §5), and the parts of the leader script (Appendix A)."""
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import reduce
from pathlib import PurePosixPath
from typing import Final

from ._ssh import _q


#: Sophios's default for `max_parallel`: Toil's `--maxJobs`, Nextflow's `executor.queueSize`.
MAX_PARALLEL: Final = 64
#: A Slurm time, as `--time` and Toil's `--slurmTime` read it alike: minutes, minutes:seconds, hours:minutes:seconds,
#: days-hours, days-hours:minutes or days-hours:minutes:seconds, the days not 0 (Toil 9.5 reads `0-12` as 12 minutes).
_TIME: Final = re.compile(r'[0-9]*[1-9][0-9]*-[0-9]+(?::[0-9]+){0,2}|[0-9]+(?::[0-9]+){0,2}')
#: The `clean=` names, Toil's own; only Toil leaves scratch outside its outputs, so the other runners take 'always'.
_CLEAN: Final = ('always', 'onSuccess', 'never')
#: The oldest version of each runner a site runs, as the sites design was read against them
#: (design_docs/sites-design.md §7).
OLDEST: Final[Mapping[str, tuple[int, ...]]] = {'toil-cwl-runner': (9, 5, 0), 'nextflow': (26, 4, 2)}
#: The `sbatch` variables that set each option of a job script's header, the leader's or a Nextflow task's, which beat
#: its `#SBATCH` line (sbatch(1), INPUT ENVIRONMENT VARIABLES). Three set the memory, two the requeue.
_SBATCH_VARIABLES: Final[Mapping[str, tuple[str, ...]]] = {
    'job-name': ('SBATCH_JOB_NAME',), 'comment': (), 'account': ('SBATCH_ACCOUNT',),
    'partition': ('SBATCH_PARTITION',), 'time': ('SBATCH_TIMELIMIT',), 'cpus-per-task': (),
    'mem': ('SBATCH_MEM_PER_NODE', 'SBATCH_MEM_PER_CPU', 'SBATCH_MEM_PER_GPU'), 'output': ('SBATCH_OUTPUT',),
    'error': ('SBATCH_ERROR',), 'open-mode': ('SBATCH_OPEN_MODE',), 'chdir': (),
    'no-requeue': ('SBATCH_NO_REQUEUE', 'SBATCH_REQUEUE'), 'dependency': (), 'signal': ('SBATCH_SIGNAL',),
    'gres': ('SBATCH_GRES',)}
#: The variables of each option Nextflow writes in a task's header on Slurm (`-J`, `-o`, `--no-requeue`,
#: `--signal`, `-c`, `-t`, `--mem`, `-p`, and the comment, account and a GPU task's `--gres` of `clusterOptions`), and
#: of `error`, which it leaves to `-o`, sending both streams to one file. The leader's login shell sets them again
#: from your profile, so its job unsets them before Nextflow starts. Toil gives each option it sets on `sbatch`'s
#: command line, which beats them.
_TASK_VARIABLES: Final = tuple(name for option in ('job-name', 'output', 'error', 'no-requeue', 'signal',
                                                   'cpus-per-task', 'time', 'mem', 'partition', 'comment', 'account',
                                                   'gres')
                               for name in _SBATCH_VARIABLES[option])


def _seconds(limit: str) -> float:
    """A Slurm time in seconds, as Toil reads it; `infinite`, a partition with no limit, is infinite."""
    if limit == 'infinite':
        return math.inf
    days, _, rest = limit.rpartition('-')
    parts = [int(part) for part in rest.split(':')]
    parts += [0] * ((3 if days else 2) - len(parts))
    return int(days or 0) * 86400 + reduce(lambda total, part: total * 60 + part, parts, 0)


@dataclass(frozen=True, slots=True)
class _Queue:
    """Where a run's jobs go on Slurm, and with what: the leader's partition and time, each with where it comes from;
    the account; the steps' time; and the partition of the steps that ask for a GPU."""

    partition: tuple[str, str]
    time: tuple[str, str]
    account: str | None
    steps_time: str
    gpu_partition: str | None


def _toil(run: str, max_parallel: int, clean: str, system: str, *more: str) -> tuple[str, ...]:
    """Toil's flags on a site (design Appendix A): its batch system, the scratch in the run, `more`, `clean=`."""
    return ('--batchSystem', system, '--bypass-file-store', '--leave-tmpdir', '--no-cwl-default-ram',
            '--defaultMemory', '4Gi', '--maxJobs', str(max_parallel), '--workDir', f'{run}/toil/work',
            '--tmpdir-prefix', f'{run}/toil/tmp/t', '--tmp-outdir-prefix', f'{run}/toil/tmp/o', *more, '--clean', clean)


def _steps_options(run: str, queue: _Queue) -> str:
    """The `sbatch` options a runner gives each step job of its own on Slurm: the run's comment, and its account."""
    return f'--comment=sophios:{PurePosixPath(run).name}' + (f' --account={queue.account}' if queue.account else '')


def _toil_on_slurm(run: str, max_parallel: int, clean: str, queue: _Queue | None) -> tuple[str, ...]:
    """Toil's flags on Slurm: each step a job of its own, on the run's partition, or the GPU partition for a step that
    asks for a GPU, with the steps' time, the run's comment and its account."""
    assert queue is not None, 'a Slurm site places every run'
    return _toil(run, max_parallel, clean, 'slurm', '--batchLogsDir', f'{run}/batch', '--slurmPartition',
                 queue.partition[0], '--slurmTime', queue.steps_time,
                 *(('--slurmGPUPartition', queue.gpu_partition) if queue.gpu_partition else ()),
                 f'--slurmArgs={_steps_options(run, queue)}')


def _nextflow(run: str, max_parallel: int, queue: _Queue | None) -> str:
    """What a site sets for Nextflow, the one function it sets it with (decision 25): a block it appends to the run's
    copy of the generated `nextflow.config`, the one configuration Nextflow reads (`-C`), since only a configuration
    sets a task's options by the task. At most `max_parallel` tasks at once; on Slurm (a `queue`), each task a Slurm
    job of its own, with the steps' time in seconds, Nextflow's own unit (none for no limit), the run's comment and
    its account, on the run's partition, or, for a task with an `accelerator`, on the GPU partition with as many GPUs.
    No value a site takes holds a quote, so each is a Groovy string as it is."""
    block = f'executor {{\n    queueSize = {max_parallel}\n}}\n'
    if queue is None:
        return block
    seconds = int(_seconds(queue.steps_time))
    partition, options = f"'{queue.partition[0]}'", f"'{_steps_options(run, queue)}'"
    if queue.gpu_partition:
        partition = f"{{ task.accelerator ? '{queue.gpu_partition}' : {partition} }}"
        options = f"{{ {options} + (task.accelerator ? ' --gres=gpu:' + task.accelerator.request : '') }}"
    return block + '\n'.join(['process {', "    executor = 'slurm'", f'    queue = {partition}',
                              *([f"    time = '{seconds}s'"] if seconds else []), f'    clusterOptions = {options}',
                              '}', ''])


#: The design's runner-by-scheduler table: what each site adds to a runner's launch, by its program, given the run's
#: folder, `max_parallel`, `clean` and, on Slurm, where its jobs go. It holds every runner a site runs: no cwltool
#: (decision 22). Nextflow takes none: what a site sets for it is a block of its configuration (`_nextflow`).
ON_A_MACHINE: Final[Mapping[str, Callable[[str, int, str, _Queue | None], tuple[str, ...]]]] = {
    'toil-cwl-runner': lambda run, max_parallel, clean, queue: _toil(run, max_parallel, clean, 'single_machine'),
    'nextflow': lambda run, max_parallel, clean, queue: (),
}
ON_SLURM: Final[Mapping[str, Callable[[str, int, str, _Queue | None], tuple[str, ...]]]] = {
    'toil-cwl-runner': _toil_on_slurm,
    'nextflow': lambda run, max_parallel, clean, queue: (),
}


def _header(run: str, queue: _Queue | None) -> list[str]:
    """A leader's `#SBATCH` lines on Slurm (design Appendix A), none on a machine: named after the run, with its
    comment, in one leader at a time (`singleton`), never requeued."""
    if queue is None:
        return []
    run_id = PurePosixPath(run).name
    options = [('job-name', run_id), ('comment', f'sophios:{run_id}'), ('account', queue.account),
               ('partition', queue.partition[0]), ('time', queue.time[0]), ('output', f'{run}/run.log'),
               ('error', f'{run}/run.log'), ('open-mode', 'append'), ('chdir', run), ('no-requeue', ''),
               ('dependency', 'singleton')]
    return [f'#SBATCH --{option}' + (f'={value}' if value else '') for option, value in options if value is not None]


def _recorded(run: str, program: str, clean: str) -> str:
    """The leader's EXIT trap: Toil's scratch removed as `clean` says, once Toil has stopped, then the exit status,
    written last."""
    scratch = f'{_q(f"{run}/toil/tmp")} {_q(f"{run}/toil/work")}'
    cleanup = (f'chmod -R u+rwX {scratch}; rm -rf {scratch}; '
               if program == 'toil-cwl-runner' and clean != 'never' else '')
    if cleanup and clean == 'onSuccess':
        cleanup = f'[ "$code" != 0 ] || {{ {cleanup}}}; '
    partial_, done = _q(f'{run}/exit.partial'), _q(f'{run}/exit')
    return f'code=$?; set +e; {cleanup}echo "$code" > {partial_}; mv {partial_} {done}'
