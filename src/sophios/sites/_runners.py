"""What a site adds to each runner (the table of design §5), how it reads a runner value, and the parts of the leader
script (Appendix A)."""
import re
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Final

from .. import targets
from ..runtime.backends import Launch, RunRequest
from ..runtime.runners import Cwltool, Nextflow, Runner, RunSettings, Toil

from ._ssh import _q


#: How many steps a run has at once when its runner value does not say: Toil's `--maxJobs`, Nextflow's
#: `executor.queueSize`.
MAX_PARALLEL: Final = 64
#: What `--clean` takes, Toil's own names; only Toil leaves scratch outside its outputs, so no other runner has it.
_CLEAN: Final = ('always', 'onError', 'onSuccess', 'never')
#: The flags a site reads from a runner value, by the runner's name, and the setting of the run each gives (decision
#: 27): how many steps run at once, and when Toil's scratch is removed. The site gives each to the runner where the
#: runner reads it, and needs it itself: in the run's record, and in the leader's EXIT trap.
_READ: Final[Mapping[str, Mapping[str, str]]] = {
    'cwltool': {}, 'toil-cwl-runner': {'--maxJobs': 'max_parallel', '--clean': 'clean'},
    'nextflow': {'-qs': 'max_parallel'}}
#: The other name each runner gives an option a site sets or reads, by the runner's name, and the name the site spells
#: it with, as the runner's own parser has them (Toil's and cwltool's, `nextflow run -h`): a flag of the other name is
#: that option, refused or read as it is.
_OTHER_NAMES: Final[Mapping[str, Mapping[str, str]]] = {
    'cwltool': {'-w': '--write-summary'}, 'toil-cwl-runner': {'--jobstore': '--jobStore'},
    'nextflow': {'-queue-size': '-qs', '-o': '-output-dir', '-w': '-work-dir'}}
#: Each setting a site reads, when its flag is not given.
_DEFAULTS: Final[Mapping[str, str]] = {'max_parallel': str(MAX_PARALLEL), 'clean': 'always'}
_COUNT: Final = re.compile(r'[1-9][0-9]*')
#: The runner a site runs each target with when `runner=` is None, by name: Toil for CWL, where a local run's default
#: is cwltool.
DEFAULT_RUNNER: Final[Mapping[str, str]] = {'cwl': Toil.name, 'nextflow': Nextflow.name}
#: The oldest version of each runner a site runs: cwltool's is the one Sophios requires (pyproject.toml); Toil's and
#: Nextflow's, those the sites design was read against (design_docs/sites-design.md §7).
OLDEST: Final[Mapping[str, tuple[int, ...]]] = {
    'cwltool': (3, 1, 20241217163858), 'toil-cwl-runner': (9, 5, 0), 'nextflow': (26, 4, 2)}


def _toil(run: str, max_parallel: str, clean: str, system: str) -> tuple[str, ...]:
    """Toil's flags on a site (design Appendix A): its batch system, the scratch in the run, and `--clean`."""
    return ('--batchSystem', system, '--bypass-file-store', '--leave-tmpdir', '--no-cwl-default-ram',
            '--defaultMemory', '4Gi', '--maxJobs', max_parallel, '--workDir', f'{run}/toil/work',
            '--tmpdir-prefix', f'{run}/toil/tmp/t', '--tmp-outdir-prefix', f'{run}/toil/tmp/o', '--clean', clean)


#: The design's table: what a machine adds to the launch of each runner, by its name, given the run's folder and the
#: settings the site read (`_READ`). Nextflow takes none: what a site sets for it is a block of its configuration
#: (`nextflow_block`).
ON_A_MACHINE: Final[Mapping[str, Callable[[str, Mapping[str, str]], tuple[str, ...]]]] = {
    'cwltool': lambda run, read: (),
    'toil-cwl-runner': lambda run, read: _toil(run, read['max_parallel'], read['clean'], 'single_machine'),
    'nextflow': lambda run, read: (),
}


def nextflow_block(read: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """What a site sets for Nextflow, the one function it sets it with (decision 25): settings of the run's
    configuration, by scope, each a Groovy value, which `nextflow_config` writes as a block appended to the run's copy
    of the generated `nextflow.config`, the one configuration Nextflow reads (`-C`), since only a configuration sets a
    task's options by the task. At most `max_parallel` tasks at once."""
    return {'executor': {'queueSize': read['max_parallel']}}


def nextflow_config(block: Mapping[str, Mapping[str, str]]) -> str:
    """`block` as the lines of a Nextflow configuration."""
    return ''.join(f'{scope} {{\n' + ''.join(f'    {name} = {value}\n' for name, value in settings.items()) + '}\n'
                   for scope, settings in block.items())


def argv(bin_: str, runner: Runner, launch: Launch) -> tuple[str, ...]:
    """The runner's command line on a site: its backend's launch, the runner run from `bin`, by its name. Without
    the `--disable-pull` cwltool's backend gives it, before every flag of the user's, as the local pre-flight has
    pulled every image: on a site nothing has, so cwltool pulls them itself."""
    words = list(launch.argv[1:])
    if runner.name == Cwltool.name:
        words.remove('--disable-pull')
    return (f'{bin_}/{runner.name}', *words)


def set_by_the_site(there: Runner, block: Mapping[str, Mapping[str, str]]) -> frozenset[str]:
    """Every option a site or the runner's backend sets: those of the command line the site runs, `there`, the runner
    it runs with none of the user's flags, and each setting of the Nextflow configuration block it writes, `block`, as
    `-<scope>.<name>`; but those the site reads from the flags (`_READ`)."""
    launch = targets.backend(there).launch(RunRequest('w', Path('/run'), RunSettings(there, '/out')))
    return frozenset({word.partition('=')[0] for word in argv('/bin', there, launch)[1:] if word.startswith('-')}
                     | {f'-{scope}.{name}' for scope, settings in block.items() for name in settings}) - set(
                         _READ[there.name])


def read_runner(runner: Runner, refused: frozenset[str]) -> tuple[tuple[str, ...], dict[str, tuple[str, str]]]:
    """What a runner value asks of a site, the one place it is read (decision 27): its flags that are passed on, in
    order, and each setting a site reads from its flags (`_READ`), with the flag that gave it, or Sophios's default.
    `refused` is what the site sets itself (`set_by_the_site`). A flag of an option's other name (`_OTHER_NAMES`) is
    that option. Its `quiet` is not read: a site's run is never quiet, as its log is where a failed step is read from.

    Raises:
        ValueError: A setting of the value's own that is not its default; a flag that is not a string; a flag a site
            reads with no value, or a value it cannot have; a flag whose option the site or the runner's backend
            already sets.
    """
    kind = type(runner)
    problems = [f"{kind.__name__}({field.name}={getattr(runner, field.name)!r}): " + (
        "the engine is the site's container_engine=" if field.name == 'container_engine' else
        'a site pulls, checks and caches by itself') + ", so a runner value's own settings stay their defaults there"
        for field in fields(runner) if field.name not in ('flags', 'quiet')
        and getattr(runner, field.name) != getattr(kind(), field.name)]
    if problems:
        raise ValueError('\n'.join(problems))
    if not all(isinstance(flag, str) for flag in runner.flags):
        raise ValueError(f'{kind.__name__}{runner.flags!r}: each flag is a string, as the program reads it')
    taken, other = _READ[runner.name], _OTHER_NAMES[runner.name]
    read = {setting: (value, "Sophios's default") for setting, value in _DEFAULTS.items() if setting in taken.values()}
    passed: list[str] = []
    flags = iter(runner.flags)
    for flag in flags:
        written, given, value = flag.partition('=')
        option = other.get(written, written)
        if option in taken:
            found = value if given else next(flags, None)
            if found is None:
                raise ValueError(f'{kind.__name__}{runner.flags!r}: {written} needs a value')
            read[taken[option]] = (found, written)
        elif option in refused:
            raise ValueError(f'{kind.__name__}{runner.flags!r}: a site sets {option} itself'
                             + (f', which {written} names too' if written != option else '')
                             + ', so its runner takes it from no runner value')
        else:
            passed.append(flag)
    if 'max_parallel' in read and not _COUNT.fullmatch(read['max_parallel'][0]):
        raise ValueError(f"{read['max_parallel'][1]} {read['max_parallel'][0]!r}: a whole number of steps at once, "
                         "as '8'")
    if 'clean' in read and read['clean'][0] not in _CLEAN:
        raise ValueError(f"--clean {read['clean'][0]!r}: Toil takes {', '.join(_CLEAN)}")
    return tuple(passed), read


def _emptied(outdir: str) -> str:
    """The lines that empty the outputs folder: every folder in it writable first, as a tool may leave one it cannot
    write, which find could not empty; neither follows a link."""
    return f'chmod -R u+rwX {_q(outdir)}; find {_q(outdir)} -mindepth 1 -delete'


def _recorded(run: str, clean: str | None, outdir: str) -> str:
    """The leader's EXIT trap, once the runner has stopped: Toil's scratch removed as its `--clean`, `clean`, says
    (None for a runner that leaves none); the outputs folder emptied unless the run succeeded, as it was missing or
    empty when the run started, so all it holds is the run's; then the exit status, written last."""
    scratch = f'{_q(f"{run}/toil/tmp")} {_q(f"{run}/toil/work")}'
    cleanup = f'chmod -R u+rwX {scratch}; rm -rf {scratch}; ' if clean not in (None, 'never') else ''
    kept = {'onSuccess': '!= 0', 'onError': '= 0'}.get(clean or '')   # the end that keeps it
    if cleanup and kept:
        cleanup = f'[ "$code" {kept} ] || {{ {cleanup}}}; '
    partial_, done = _q(f'{run}/exit.partial'), _q(f'{run}/exit')
    return (f'code=$?; set +e; {cleanup}[ "$code" = 0 ] || {{ {_emptied(outdir)}; }}; echo "$code" > {partial_}; '
            f'mv {partial_} {done}')
