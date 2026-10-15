"""Ask pytest what each of several invocations collects, in one interpreter.

`test_ci_coverage` runs this as a script: argv lists arrive as JSON on stdin,
and the report (exit code, node ids, output tail per invocation) is written to
the path given as the only argument. Each collection is still `pytest.main`'s
own answer; sharing the process only shares the imports.
"""
import contextlib
import io
import json
import sys

import pytest


class _Selected:  # pylint: disable=too-few-public-methods  # a pytest plugin: one hook
    """Records the items pytest selected."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Keep the node id of every selected item."""
        self.ids = [item.nodeid for item in session.items]


def main() -> None:
    """Collect every invocation read from stdin and write the report."""
    report = []
    for argv in json.load(sys.stdin):
        selected, output = _Selected(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = pytest.main(['--collect-only', '-q', '--no-header', '-p', 'no:randomly',
                                '-p', 'no:cacheprovider', *argv], plugins=[selected])
        report.append([int(code), selected.ids, output.getvalue()[-2000:]])
    with open(sys.argv[1], 'w', encoding='utf-8') as stream:
        json.dump(report, stream)


if __name__ == '__main__':
    main()
