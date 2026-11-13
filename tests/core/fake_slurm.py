"""A Slurm of Sophios's own, for the sites tests: `sbatch`, `squeue`, `sacct`, `scancel` and `sinfo`, as much of each
as Sophios and its runners ask of them (the compute driver's fake lives in a repository Sophios CI may not check out).
`install` puts a shim for each in a folder, which runs this file over a state folder of one JSON file per job.

A job runs at once, in a process group of its own, under a watcher of its own, unless the state folder holds `hold`
(it pends, `Priority`), its time is longer than its partition's (it pends for ever, `PartitionTimeLimit`, as Slurm
without EnforcePartLimits), or an earlier job of its name has not ended (`--dependency=singleton`). `scancel` asks
the watcher to end it: a pending job ends `CANCELLED by 1000`, a running one gets TERM. `broken` names the commands
that cannot reach Slurm; `lose` makes the next `sbatch` fail before it takes the job (`before`) or after (`after`)."""
# pylint: disable=consider-using-with  # a job's streams and process outlive the call that opens them
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

#: Each partition, as `sinfo -o '%P|%l|%G'` prints it: its name (`*` marks the default), its time limit, its GRES.
PARTITIONS = (('normal*', '2-00:00:00', '(null)'), ('short', '30:00', '(null)'), ('gpu', 'infinite', 'gpu:2'))
#: Each command's short options that take a value.
_TAKES = {'sbatch': 'Jopet', 'squeue': 'unojtp', 'sacct': 'SEoj', 'scancel': 'un', 'sinfo': 'o'}
_LONG = {'J': 'job-name', 'o': 'output', 'e': 'error', 'p': 'partition', 't': 'time'}
_LOST = 'sbatch: error: Batch job submission failed: Socket timed out on send/recv operation'
#: The `sbatch` variables the fake reads, by option: each beats a script's `#SBATCH` line and loses to the command line.
_VARIABLES = {'account': 'SBATCH_ACCOUNT', 'partition': 'SBATCH_PARTITION', 'time': 'SBATCH_TIMELIMIT'}


def install(folder: Path) -> dict[str, str]:
    """The shims in `folder/bin` and an empty state in `folder/state`: the variables a test sets to use them."""
    (folder / 'bin').mkdir(parents=True)
    (folder / 'state').mkdir()
    for command in _TAKES:
        (folder / 'bin' / command).write_text(f'#!/bin/sh\nexec {sys.executable} {__file__} {command} "$@"\n')
        (folder / 'bin' / command).chmod(0o755)
    return {'PATH': f'{folder / "bin"}:{os.environ["PATH"]}', 'SOPHIOS_FAKE_SLURM': str(folder / 'state')}


def jobs(state: Path) -> list[dict[str, Any]]:
    """Every job, in the order it was submitted."""
    return sorted((json.loads(path.read_text()) for path in state.glob('job-*.json')), key=lambda job: int(job['id']))


def _seconds(limit: str) -> float:
    if limit in ('infinite', 'UNLIMITED', '0'):
        return float('inf')
    days, _, rest = limit.rpartition('-')
    parts = [int(part) for part in rest.split(':')] + [0, 0]
    return int(days or 0) * 86400 + (parts[0] * 3600 + parts[1] * 60 + parts[2] if days or rest.count(':') == 2
                                     else parts[0] * 60 + parts[1])


def _options(command: str, argv: list[str]) -> tuple[dict[str, str], list[str]]:
    found: dict[str, str] = {}
    words, rest = iter(argv), []
    for word in words:
        if word.startswith('--'):
            key, equals, value = word[2:].partition('=')
            found[key] = value if equals or key != 'format' else next(words)
        elif word.startswith('-') and len(word) == 2:
            key = _LONG.get(word[1], word[1]) if command == 'sbatch' else word[1]
            found[key] = next(words) if word[1] in _TAKES[command] else ''
        else:
            rest.append(word)
    return found, rest


def _save(state: Path, job: dict[str, Any]) -> None:
    (state / f'job-{job["id"]}.tmp').write_text(json.dumps(job))
    (state / f'job-{job["id"]}.tmp').replace(state / f'job-{job["id"]}.json')


def sbatch(state: Path, argv: list[str]) -> str:
    """Take the job, its options from the command line over its `#SBATCH` lines, and start its watcher."""
    given, rest = _options('sbatch', argv)
    lose = (state / 'lose').read_text().strip() if (state / 'lose').exists() else ''
    (state / 'lose').unlink(missing_ok=True)
    if lose == 'before':
        sys.exit(_LOST)
    script = f'#!/bin/sh\n{given["wrap"]}\n' if 'wrap' in given else Path(rest[0]).read_text(encoding='utf-8')
    options: dict[str, str] = {}
    for line in script.splitlines():
        if line.startswith('#SBATCH '):
            options.update(_options('sbatch', line.split()[1:])[0])
    options.update({option: os.environ[name] for option, name in _VARIABLES.items() if name in os.environ})
    options.update(given)
    limits = {name.rstrip('*'): limit for name, limit, _ in PARTITIONS}
    if options.get('partition', 'normal') not in limits:
        sys.exit('sbatch: error: invalid partition specified')
    number = 4242
    while True:
        try:
            os.close(os.open(state / f'id-{number}', os.O_CREAT | os.O_EXCL))
            break
        except FileExistsError:
            number += 1
    (state / f'script-{number}').write_text(script)
    output = options.get('output', 'slurm-%j.out')
    _save(state, {
        'id': str(number), 'name': options.get('job-name', 'wrap'), 'comment': options.get('comment', ''),
        'account': options.get('account', ''), 'partition': options.get('partition', 'normal'),
        'gres': options.get('gres', ''),
        'limit': limits[options.get('partition', 'normal')], 'time': options.get('time', 'infinite'),
        'chdir': options.get('chdir', os.getcwd()), 'output': output, 'error': options.get('error', output),
        'mode': 'a' if options.get('open-mode') == 'append' else 'w', 'singleton': options.get('dependency') ==
        'singleton', 'state': 'PENDING', 'reason': 'None', 'shell': script.splitlines()[0][2:].split(),
        'env': {name: shlex.split(value)[0] if value else '' for name, _, value in
                (item.partition('=') for item in options.get('export', '').split(',') if '=' in item)},
        'variables': sorted(name for name in os.environ if name.startswith('SBATCH_'))})
    subprocess.Popen([sys.executable, __file__, 'slurmd', str(number)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if lose == 'after':
        sys.exit(_LOST)
    return str(number) if 'parsable' in options else f'Submitted batch job {number}'


def slurmd(state: Path, number: str) -> None:
    """A job's watcher: it starts the job once nothing holds it, ends it when `scancel` asks, and records its end."""
    job = json.loads((state / f'job-{number}.json').read_text())
    process = None
    while True:
        cancelled = (state / f'cancel-{number}').exists()
        if process is None:
            waiting = ('Priority' if (state / 'hold').exists() else
                       'PartitionTimeLimit' if _seconds(job['time']) > _seconds(job['limit']) else
                       'Dependency' if job['singleton'] and any(
                           other['name'] == job['name'] and int(other['id']) < int(number) and 'end' not in other
                           for other in jobs(state)) else '')
            if cancelled:
                return _save(state, {**job, 'state': 'CANCELLED by 1000', 'end': int(time.time()), 'exit': '0:0'})
            if not waiting:
                job['start'] = int(time.time())
                streams = [open(Path(job['chdir']) / job[name].replace('%j', number), job['mode'], encoding='utf-8')
                           for name in ('output', 'error')]
                process = subprocess.Popen(
                    [*job['shell'], str(state / f'script-{number}')], cwd=job['chdir'], stdout=streams[0],
                    stderr=streams[0] if job['error'] == job['output'] else streams[1], stdin=subprocess.DEVNULL,
                    start_new_session=True, env={**os.environ, **job['env'], 'SLURM_JOB_ID': number})
                job['pid'] = process.pid
            if job['reason'] != (waiting or 'None') or not waiting:
                job.update(state='PENDING' if waiting else 'RUNNING', reason=waiting or 'None')
                _save(state, job)
        elif process.poll() is not None:
            code = process.returncode
            return _save(state, {**job, 'state': 'CANCELLED by 1000' if cancelled else 'COMPLETED' if code == 0 else
                                 'FAILED', 'end': int(time.time()), 'exit': f'{max(code, 0)}:{max(-code, 0)}'})
        elif cancelled and not job.get('signalled'):
            job['signalled'] = True
            os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.1)


def _fill(fields: str, value: Callable[[str], str]) -> str:
    """`fields` with each `%<letter>` replaced by its value."""
    return re.sub(r'%(\w)', lambda found: value(found[1]), fields)


def _field(field: str, job: dict[str, Any]) -> str:
    end, state = job.get('end'), job['state']
    return str({'i': job['id'], 'JobID': job['id'], 'JobIDRaw': job['id'], 'j': job['name'], 'k': job['comment'],
                'T': state, 'State': state, 't': {'PENDING': 'PD', 'RUNNING': 'R'}.get(state, 'CG'),
                'r': job['reason'], 'M': '0:01', 'N': '', 'ExitCode': job.get('exit', '0:0'),
                'End': 'Unknown' if end is None else str(end) if os.environ.get('SLURM_TIME_FORMAT') == '%s' else
                time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end))}[field])


def main(command: str, argv: list[str]) -> None:  # pylint: disable=too-many-return-statements
    """Answer as Slurm's `command` does."""
    state = Path(os.environ['SOPHIOS_FAKE_SLURM'])
    if command == 'slurmd':
        return slurmd(state, argv[0])
    if '--version' in argv:
        return print('slurm 24.05.4')
    if (state / 'broken').exists() and command in (state / 'broken').read_text().split():
        sys.exit(f'{command}: error: Unable to contact slurm controller (connect failure)')
    if command == 'sbatch':
        return print(sbatch(state, argv))
    options, rest = _options(command, argv)
    if command == 'sinfo':
        rows = [_fill(options['o'], dict(zip('PlGpcm', (*row, '1', '8', '16000'))).__getitem__) for row in PARTITIONS]
        return print('\n'.join(rows if 'h' in options else ['PARTITION GRES TIMELIMIT PRIO_TIER CPUS MEMORY', *rows]))
    name = options.get('name', options.get('n') if command != 'sacct' else None)
    ids = options['j'].split(',') if 'j' in options else rest
    chosen = [job for job in jobs(state) if name in (None, job['name']) and (not ids or job['id'] in ids)]
    if command == 'scancel':
        for job in chosen:
            (state / f'cancel-{job["id"]}').touch()
        return None
    fields = options.get('o', options.get('format', '%i %j %T'))
    if command == 'squeue':
        return print('\n'.join(_fill(fields, partial(_field, job=job)) for job in chosen if 'end' not in job))
    return print('\n'.join('|'.join(_field(field, job) for field in fields.split(',')) for job in chosen))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
