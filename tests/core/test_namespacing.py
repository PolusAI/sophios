"""Parsing leaves global YAML state alone, and gives the same answer from many threads."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from sophios.lang import parse, wic_schema
from sophios.lang.render import render

pytestmark = pytest.mark.package

SOURCE = 'steps:\n- id: touch\n  in:\n    f: !ii empty.txt\n  out:\n  - file: !& out\n'


@pytest.mark.fast
def test_parsing_does_not_touch_the_global_safeloader() -> None:
    """Parsing a `.wic` leaves `yaml.safe_load` alone.

    The parser builds YAML with `yaml.SafeLoader`; registering on it would make
    every later `safe_load` in the process interpret `!ii`, anywhere, including
    in unrelated libraries.
    """
    assert parse(SOURCE, 'tags.wic').ok

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load('x: !ii 5')


@pytest.mark.fast
def test_parsing_and_rendering_are_safe_from_many_threads() -> None:
    """The same source parsed concurrently gives every thread the same answer.

    Not a proof of thread safety — no test is — but it exercises the shared
    tables, the parser's YAML loading, and the schema export from several threads at once,
    which is where per-module mutable state would show up as divergence.
    """
    def once(_: int) -> tuple[str, int]:
        document = parse(SOURCE, 'concurrent.wic').document
        assert document is not None
        return render(document), len(wic_schema()['$defs'])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(once, range(64)))

    assert len(set(results)) == 1, 'concurrent parses disagreed'
