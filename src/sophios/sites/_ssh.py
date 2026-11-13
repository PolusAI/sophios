"""The transport, your own ssh, and the shell functions a site's scripts use."""
import hashlib
import shlex
import subprocess
from typing import Final

from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode as Code


#: Every call: your own ssh, so your keys, agent, `~/.ssh/config` and jump hosts apply. It asks for nothing, so a host
#: that wants a password or a second factor is reached through a shared connection (`ControlMaster`) opened by hand.
SSH: Final = ('ssh', '-T', '-o', 'BatchMode=yes', '-o', 'RemoteCommand=none', '-o', 'ConnectTimeout=20',
              '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')


def _q(value: str) -> str:
    """`value` as one word of a script: every value enters a script this way, and only this way."""
    return shlex.quote(value)


def _transport(host: str) -> list[str]:
    """The command that runs a shell command on `host`: `ssh <host>`, which the tests replace with a local `bash -c`,
    and nothing else."""
    return [*SSH, host]


def _call(host: str, script: str) -> bytes:
    """Run `script` with bash, as a login shell, on `host`, and return what it printed on its output.

    The script goes on ssh's input, and the command runs it only when its sha256 is the one sent: a login profile
    that reads its input would eat part of the script and run the rest. What the profile prints comes before the hash,
    which the command prints once the script has arrived whole, so it is never read as the script's.

    Raises:
        SophiosError: `wic057`: ssh could not be run or failed, the script did not arrive whole, or it failed there.
    """
    data = script.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    guard = ('s=$(cat; echo .); s=${s%.}; h=$(printf %s "$s" | { sha256sum 2> /dev/null || shasum -a 256; }); '
             f'[ "${{h%% *}}" = {digest} ] || {{ echo "the script Sophios sent did not arrive whole: a login '
             f'profile there reads its input" >&2; exit 1; }}; echo {digest}; eval "$s"')
    try:
        done = subprocess.run([*_transport(host), f"bash -l -c '{guard}'"], input=data, capture_output=True,
                              check=False)
    except OSError as error:
        raise _unreachable(host, f'ssh could not be run ({error})') from error
    _, marker, said = done.stdout.partition(f'{digest}\n'.encode())
    if done.returncode != 0 or not marker:
        lines = done.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise _unreachable(host, f'the call there exited with status {done.returncode} '
                           f"({' / '.join(lines[-3:]) or 'it said nothing'})")
    return said


def _unreachable(host: str, why: str) -> SophiosError:
    return SophiosError.error(Code.SITE_UNREACHABLE, f'{host} cannot be used as a site: {why}.')


#: A script's function that prints a file from byte `$2` on: its length in bytes on a line, then the bytes; -1 for no
#: file. The length is read first, so a file that grows meanwhile is cut where it was.
_PUT: Final = ('put() { if [ -f "$1" ]; then n=$(($(wc -c < "$1") - $2)); [ "$n" -gt 0 ] || n=0; echo "$n"; '
               'if [ "$n" -gt 0 ]; then tail -c +$(($2 + 1)) "$1" | head -c "$n"; fi; else echo -1; fi; }')
#: A script's function that runs a command and prints its exit status and the length of what it said, on a line, then
#: what it said on both streams.
_ANSWER: Final = 'answer() { out=$("$@" 2>&1); code=$?; echo "$code $(printf %s "$out" | wc -c)"; printf %s "$out"; }'


class _Said:
    """What a script printed: lines, files as `_PUT` prints them, and answers as `_ANSWER` prints them."""

    def __init__(self, data: bytes) -> None:
        self._data, self._at = data, 0

    def line(self) -> str:
        """The next line, without its line break."""
        end = self._data.index(b'\n', self._at)
        found, self._at = self._data[self._at:end], end + 1
        return found.decode('utf-8', 'replace')

    def _bytes(self, size: int) -> bytes:
        found, self._at = self._data[self._at:self._at + size], self._at + size
        return found

    def file(self) -> bytes | None:
        """The next file's bytes, None for no file."""
        size = int(self.line())
        return None if size < 0 else self._bytes(size)

    def answer(self) -> tuple[int, str]:
        """The next command's exit status, and what it said."""
        status, size = self.line().split()
        return int(status), self._bytes(int(size)).decode('utf-8', 'replace')


def _alive(run: str) -> str:
    """A script's function: the leader whose pid `$1` is, the claim, runs `<run>/job.sh` (a reused pid runs another)."""
    return (f'alive() {{ case $1 in ""|*[!0-9]*) return 1 ;; esac; '
            f'case $(ps -ww -o args= -p "$1" 2> /dev/null) in *{_q(run + "/job.sh")}*) return 0 ;; esac; return 1; }}')


#: The checks' functions, each printing one line: what a path is, what a program printed and its exit status, a
#: variable's value, and whether a folder can be made.
_CHECKS: Final = '\n'.join((
    'kind() { if [ -d "$1" ]; then if [ -r "$1" ] && [ -x "$1" ]; then echo Directory; else echo unreadable; fi; '
    'elif [ -e "$1" ]; then if [ -r "$1" ]; then echo File; else echo unreadable; fi; else echo missing; fi; }',
    'said() { echo "$1 $(printf %s "$2" | tr "\\n\\r" "  ")"; }',
    'runs() { if command -v "$1" > /dev/null 2>&1; then out=$("$@" 2>&1); said $? "$out"; else echo missing; fi; }',
    'probe() { if command -v "$1" > /dev/null 2>&1; then out=$("$@" 2>&1 > /dev/null); said $? "$out"; '
    'else echo missing; fi; }',
    'variable() { printf %s "${!1-}" | tr "\\n\\r" "  "; echo; }',
    'writable() { p=$1; while [ ! -e "$p" ]; do p=$(dirname "$p"); done; '
    'if [ -d "$p" ] && [ -w "$p" ] && [ -x "$p" ]; then echo yes; else echo "no $p"; fi; }',
    'new() { if [ -e "$1" ] || [ -L "$1" ]; then echo exists; else writable "$1"; fi; }'))
