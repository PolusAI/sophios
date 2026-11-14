"""Contributions, judged on cases in files: a document contributes to a step of a subworkflow it calls by nesting
`wic:` blocks keyed `(index, name)` or by a bare id, and a contribution gone astray is named on stderr and ignored.
tests/core/data/contributions/<theme>/<case> is a workflow.wic and the child workflows it calls; the theme's
expected.yaml states what each case's steps bind once resolved. That resolution reads the registry and nothing else
is a law of test_emit.py's drawn workflows."""
from pathlib import Path
from typing import Any, Final

import pytest

from sophios.ir.resolve import RegistrySnapshot, resolve
from sophios.lang import EdgeRef, InlineLiteral, RawCwlRef, UnresolvedName, parse

from .cases import DATA, match
from .helpers import stated
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

_PAYLOAD: Final = {InlineLiteral: 'value', EdgeRef: 'name', RawCwlRef: 'expression', UnresolvedName: 'name'}


def _resolved(case: Path) -> Any:
    """The case's workflow.wic resolved against the synthetic tools and the other `.wic` files beside it."""
    parsed = parse((case / 'workflow.wic').read_text(encoding='utf-8'), 'workflow.wic')
    assert parsed.document is not None, list(parsed.diagnostics)
    registry = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows={
        (SYNTHETIC_NS, path.stem): parse(path.read_text(encoding='utf-8'), path.name)
        for path in case.glob('*.wic') if path.name != 'workflow.wic'})
    result = resolve(parsed.document, registry, name='workflow')
    assert result.document is not None, list(result.diagnostics)
    return result.document


@pytest.mark.fast
@pytest.mark.parametrize('theme', [pytest.param(path, id=path.name)
                                   for path in sorted((DATA / 'contributions').iterdir()) if path.is_dir()])
def test_a_contribution_binds_each_case_as_its_theme_states(theme: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each `expect` entry names the step at `step` (default 0) of the document `depth` calls below the root, each
    call its document's first step, and what it binds: `inputs` as `[construct, payload]` and `outputs` as
    `[name, edge defined]`. `said` is every line printed on stderr (none, where it is left out)."""
    for name, expected in stated(theme).items():
        document = _resolved(theme / name)
        for entry in expected['expect']:
            found = document
            for _ in range(entry.get('depth', 0)):
                found = found.steps[0].process.child
            step = found.steps[entry.get('step', 0)].source
            bound = {**entry, 'outputs': [[out.name, out.edge_def and out.edge_def.name] for out in step.outputs],
                     'inputs': {port: [type(value).__name__, getattr(value, _PAYLOAD[type(value)])]
                                for port, value in step.inputs}}
            assert match(entry, bound), (name, bound)
        assert match(expected.get('said', []), capsys.readouterr().err.splitlines()), name
