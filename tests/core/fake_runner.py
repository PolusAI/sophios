"""A stand-in for cwltool and Toil that prints what it is told, records how it was started and exits, so tests drive
the whole run path without a runner or container engine; `install` puts it where a run finds it. POSIX only."""
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sophios import preflight

#: Skips a test that starts the stand-in where a script cannot be its own program.
POSIX_ONLY = pytest.mark.skipif(sys.platform == 'win32', reason='the stand-in runner is a script with a shebang')

_SCRIPT = '''#!{python}
import json, os, signal, subprocess, sys, time
spec = json.load(open({spec!r}, encoding='utf-8'))
record = {record!r}

def write(**fields):
    # Whole or not at all: SIGTERM may arrive while the first record is written.
    partial = f'{{record}}.{{len(fields)}}'
    with open(partial, 'w', encoding='utf-8') as f:
        json.dump(dict(argv=sys.argv, env=dict(os.environ), pid=os.getpid(), sid=os.getsid(0), cwd=os.getcwd(),
                       **fields), f)
    os.replace(partial, record)

def stopped(signum, frame):
    write(signal=signum)
    sys.exit(143)

signal.signal(signal.SIGTERM, signal.SIG_IGN if spec['ignore_sigterm'] else stopped)
write()
for line in spec['stdout']:
    print(line, flush=True)
for line in spec['stderr']:
    print(line, file=sys.stderr, flush=True)
if spec['leave_behind']:
    # Outlives the stand-in, holding its streams open.
    subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(float(sys.argv[1]))',
                      str(spec['leave_behind'])])
deadline = time.monotonic() + spec['sleep']
while time.monotonic() < deadline:
    time.sleep(0.05)
sys.exit(spec['exit_code'])
'''


@dataclass
class FakeRunner:
    """What the stand-in prints and how it exits; `record()` is how it was last started."""

    directory: Path
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    exit_code: int = 0
    sleep: float = 0.0
    ignore_sigterm: bool = False
    leave_behind: float = 0.0

    @property
    def record_path(self) -> Path:
        """Where the stand-in writes how it was started, and again when it is stopped."""
        return self.directory / 'record.json'

    def write(self) -> Path:
        """Write the stand-in and its instructions; returns the program."""
        spec = self.directory / 'spec.json'
        spec.write_text(json.dumps({'stdout': self.stdout, 'stderr': self.stderr, 'exit_code': self.exit_code,
                                    'sleep': self.sleep, 'ignore_sigterm': self.ignore_sigterm,
                                    'leave_behind': self.leave_behind}),
                        encoding='utf-8')
        program = self.directory / 'runner'
        program.write_text(_SCRIPT.format(python=sys.executable, spec=str(spec), record=str(self.record_path)),
                           encoding='utf-8')
        program.chmod(0o755)
        return program

    def record(self) -> dict[str, Any]:
        """How the stand-in was started: its argv, environment, process and session ids and working directory."""
        return dict(json.loads(self.record_path.read_text(encoding='utf-8')))

    def wait_until_started(self, timeout: float = 30.0) -> None:
        """Block until the stand-in has started."""
        deadline = time.monotonic() + timeout
        while not self.record_path.exists():
            assert time.monotonic() < deadline, 'the stand-in runner never started'
            time.sleep(0.02)


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **behaviour: Any) -> FakeRunner:
    """Make every run started from now on start the stand-in, whichever runner it names."""
    directory = tmp_path / 'fake_runner'
    directory.mkdir()
    fake = FakeRunner(directory, **behaviour)
    program = fake.write()
    monkeypatch.setattr(preflight, 'runner_program', lambda _name: str(program))
    return fake
