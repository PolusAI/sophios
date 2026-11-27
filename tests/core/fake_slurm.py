"""A Slurm of Sophios's own, for the sites tests: `sbatch`, `squeue`, `sacct`, `scancel` and `sinfo`, as much of each
as Sophios and its runners ask of them (the compute driver's fake lives in a repository Sophios CI may not check out).
`install` puts a shim for each in a folder, which runs this file over a state folder of one JSON file per job.

A job runs at once, in a process group of its own, under a watcher of its own, unless the state folder holds `hold`
(it pends, `Priority`), its time is longer than its partition's (it pends for ever, `PartitionTimeLimit`, as Slurm
without EnforcePartLimits), or an earlier job of its name has not ended (`--dependency=singleton`). `scancel` asks
the watcher to end it: a pending job ends `CANCELLED by 1000`, a running one gets TERM. `broken` names the commands
that cannot reach Slurm; `lose` makes the next `sbatch` fail before it takes the job (`before`), after (`after`), or
once the job it took has ended (`ended`), and the one after that as the next word says.

Each option that decides what a command lists is read as Slurm reads it, so that a caller that leaves one out sees
what it would see there: `squeue` lists every user's jobs but with `-u`, a header but with `-h`, and a job of a hidden
partition only with `-a`; `sacct` lists only your own jobs, a header but with `-n`, each started job's steps but with
`-X`, columns of fixed width but with `-P`, and only jobs that had not ended by its start, `-S`, midnight when not
given; `sinfo` a header but with `-h`, and a hidden partition only with `-a`; `scancel` signals every job its filters
name, and fails on another user's. `enqueue` puts a job of anyone's in the queue, with no watcher."""
# pylint: disable=consider-using-with  # a job's streams and process outlive the call that opens them
import json
import math
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

#: Each partition, as `sinfo -o '%P|%l|%G'` prints it: its name (`*` marks the default), its time limit, its GRES; and
#: whether it is hidden.
PARTITIONS = (('normal*', '2-00:00:00', '(null)', False), ('short', '30:00', '(null)', False),
              ('gpu', 'infinite', 'gpu:2', False), ('hidden', '1:00:00', '(null)', True))
#: Each command's short options that take a value.
_TAKES = {'sbatch': 'Jopet', 'squeue': 'unojtp', 'sacct': 'SEoju', 'scancel': 'un', 'sinfo': 'o'}
#: The title of each field a header names.
_TITLES = {'i': 'JOBID', 'j': 'NAME', 'k': 'COMMENT', 'T': 'STATE', 't': 'ST', 'r': 'REASON', 'M': 'TIME',
           'P': 'PARTITION', 'l': 'TIMELIMIT', 'G': 'GRES', 'p': 'PRIO_TIER', 'c': 'CPUS', 'm': 'MEMORY'}
_LONG = {'J': 'job-name', 'o': 'output', 'e': 'error', 'p': 'partition', 't': 'time'}
_LOST = 'sbatch: error: Batch job submission failed: Socket timed out on send/recv operation'
#: The `sbatch` variables the fake reads, by option: each beats a script's `#SBATCH` line and loses to the command line.
_VARIABLES = {'account': 'SBATCH_ACCOUNT', 'partition': 'SBATCH_PARTITION', 'time': 'SBATCH_TIMELIMIT'}


def _me() -> str:
    """Who runs the commands, as `id -un` says it: read as a command runs, since `pwd` is POSIX's and this file
    is imported on Windows too, where the tests collect it and run none of its commands."""
    import pwd  # pylint: disable=import-outside-toplevel  # POSIX's; see the docstring
    return pwd.getpwuid(os.getuid()).pw_name


def install(folder: Path) -> dict[str, str]:
    """The shims in `folder/bin` and an empty state in `folder/state`: the variables a test sets to use them."""
    (folder / 'bin').mkdir(parents=True)
    (folder / 'state').mkdir()
    for command in _TAKES:
        (folder / 'bin' / command).write_text(f'#!/bin/sh\nexec {sys.executable} {__file__} {command} "$@"\n')
        (folder / 'bin' / command).chmod(0o755)
    return {'PATH': f'{folder / "bin"}:{os.environ["PATH"]}', 'SOPHIOS_FAKE_SLURM': str(folder / 'state')}


def jobs(state: Path) -> list[dict[str, Any]]:
    """Every job, in the order it was submitted: its id, name, comment, state and why it pends, its `options` as
    `sbatch` took them, and its end, once it has ended."""
    return sorted((json.loads(path.read_text()) for path in state.glob('job-*.json')), key=lambda job: int(job['id']))


def seconds(limit: str) -> float:
    """A time, as Slurm reads its forms, in seconds; 0 and none, no limit, infinite."""
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
    lose, *later = (state / 'lose').read_text().split() if (state / 'lose').exists() else ['']
    if later:
        (state / 'lose').write_text(' '.join(later))
    else:
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
    limits = {name.rstrip('*'): limit for name, limit, _, _ in PARTITIONS}
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
        'id': str(number), 'user': _me(), 'name': options.get('job-name', 'wrap'),
        'comment': options.get('comment', ''), 'submit': int(time.time()),
        'options': options, 'limit': limits[options.get('partition', 'normal')],
        'time': options.get('time', 'infinite'),
        'chdir': options.get('chdir', os.getcwd()), 'output': output, 'error': options.get('error', output),
        'mode': 'a' if options.get('open-mode') == 'append' else 'w', 'singleton': options.get('dependency') ==
        'singleton', 'state': 'PENDING', 'reason': 'None', 'shell': script.splitlines()[0][2:].split(),
        'env': {name: shlex.split(value)[0] if value else '' for name, _, value in
                (item.partition('=') for item in options.get('export', '').split(',') if '=' in item)}})
    subprocess.Popen([sys.executable, __file__, 'slurmd', str(number)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while lose == 'ended' and 'end' not in json.loads((state / f'job-{number}.json').read_text()):
        time.sleep(0.1)
    if lose in ('after', 'ended'):
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
                       'PartitionTimeLimit' if seconds(job['time']) > seconds(job['limit']) else
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


def enqueue(folder: Path, number: int, name: str, **job: Any) -> None:
    """A job of `name` in the queue of the state `folder`, `RUNNING` and yours unless `job` says otherwise (its `user`,
    `state`, `comment`, `partition`, `start` and `end`), which no watcher runs: as another user's job, or one of a run
    of yours."""
    partition = job.pop('partition', 'normal')
    _save(folder, {'id': str(number), 'user': _me(), 'name': name, 'comment': '', 'state': 'RUNNING', 'reason': 'None',
                   'options': {'partition': partition}, **job})


def _hidden(job: dict[str, Any]) -> bool:
    return job['options'].get('partition', 'normal') in {name for name, _, _, hidden in PARTITIONS if hidden}


def _since(written: str | None) -> float:
    """When `sacct -S` starts: `now`, a local time, or midnight when not given."""
    if written is None:
        return time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
    return time.time() if written == 'now' else time.mktime(time.strptime(written, '%Y-%m-%dT%H:%M:%S'))


def main(command: str, argv: list[str]) -> None:  # pylint: disable=too-many-return-statements  # one answer per command
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
        rows = [_fill(options['o'], dict(zip('PlGpcm', (*row[:3], '1', '8', '16000'))).__getitem__)
                for row in PARTITIONS if 'a' in options or not row[3]]
        return print('\n'.join(rows if 'h' in options else [_fill(options['o'], _TITLES.__getitem__), *rows]))
    name = options.get('name', options.get('n') if command != 'sacct' else None)
    ids = options['j'].split(',') if 'j' in options else rest
    user = options.get('u', options.get('user', _me() if command == 'sacct' else None))
    chosen = [job for job in jobs(state) if name in (None, job['name']) and (not ids or job['id'] in ids)
              and user in (None, job['user'])]
    if command == 'scancel':
        chosen = [job for job in chosen if 'end' not in job]
        for job in chosen:
            if job['user'] == _me():
                (state / f'cancel-{job["id"]}').touch()
        if refused := [job['id'] for job in chosen if job['user'] != _me()]:
            sys.exit('\n'.join(f'scancel: error: Kill job error on job id {job}: Access/permission denied'
                               for job in refused))
        return None
    if command == 'squeue':
        fields = options.get('o', options.get('format', '%i %j %T'))
        listed = [_fill(fields, partial(_field, job=job)) for job in chosen
                  if 'end' not in job and ('a' in options or not _hidden(job))]
        return print('\n'.join(listed if 'h' in options else [_fill(fields, _TITLES.__getitem__), *listed]))
    return print(_accounted(chosen, options))


def _accounted(chosen: list[dict[str, Any]], options: dict[str, str]) -> str:
    """What `sacct` prints of the jobs `chosen`, as `options` ask."""
    since, fields = _since(options.get('S', options.get('starttime'))), options.get('o', options.get('format', ''))
    table = [[_field(field, step) for field in fields.split(',')] for job in chosen if job.get('end', math.inf) >= since
             for step in [job, *([{**job, 'id': f'{job["id"]}.{kind}'} for kind in ('batch', 'extern')]
                                 if 'start' in job and 'X' not in options else [])]]
    if 'n' not in options:
        table = [fields.split(','), *([] if 'P' in options else [['-' * 12] * len(fields.split(','))]), *table]
    return '\n'.join('|'.join(row) if 'P' in options else ' '.join(f'{value:>12}' for value in row) for row in table)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
