"""A fake `ssh` for `Service.over_ssh`'s tests, written into a directory to put first on PATH.

It appends its arguments, as a JSON list, to `ssh.calls` beside it. Asked for the host `nologin`, it fails as ssh
fails when it would need a password: one line on stderr, exit 255. Otherwise it forwards each connection to the
local port of its `-L 127.0.0.1:<port>:<target>` to `<target>`, read as `<host>:<port>` (a TCP stand-in for the
service's socket), until it is terminated.
"""
import sys
from pathlib import Path

_SCRIPT = '''
import json, socket, sys, threading
args = sys.argv[1:]
with open(sys.argv[0] + '.calls', 'a', encoding='utf-8') as calls:
    calls.write(json.dumps(args) + '\\n')
if args[-1] == 'nologin':
    print('nologin: Permission denied (publickey).', file=sys.stderr)
    sys.exit(255)
_, port, target = args[args.index('-L') + 1].split(':', 2)
host, target_port = target.rsplit(':', 1)
server = socket.create_server(('127.0.0.1', int(port)))


def pump(source, sink):
    try:
        while data := source.recv(65536):
            sink.sendall(data)
        sink.shutdown(socket.SHUT_WR)
    except OSError:
        pass


while True:
    client, _ = server.accept()
    upstream = socket.create_connection((host, int(target_port)))
    for pair in ((client, upstream), (upstream, client)):
        threading.Thread(target=pump, args=pair, daemon=True).start()
'''


def install(directory: Path) -> Path:
    """Write the fake `ssh` into `directory`, and return the file its calls are appended to."""
    ssh = directory / 'ssh'
    ssh.write_text(f'#!{sys.executable}\n{_SCRIPT}', encoding='utf-8')
    ssh.chmod(0o755)
    return directory / 'ssh.calls'
