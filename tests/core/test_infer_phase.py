"""Inference, judged on cases in files: tests/core/data/infer/<theme>/<case> is a workflow.wic with the tools and child
workflows it names (beside it, or shared by the theme), and the theme's expected.yaml states what each case infers.
That every inferred edge is the independent model's choice is a law of test_emit.py's drawn workflows."""
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from sophios.compiler import CompileSettings, compile_source
from sophios.ir import InferencePolicy, InsertionCatalog, front_end, infer, link
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.names import Names
from sophios.ir.types import EdgeOrigin, WorkflowGraph
from sophios.lang.diagnostics import Diagnostics, SophiosError
from sophios.utils_cwl import desugar_into_canonical_normal_form

from .cases import DATA, diagnostic, match
from .helpers import stated
from .synthetic_tools import registered


def _tools(*directories: Path) -> Any:
    """The synthetic tools, and each tool in `directories`, the later winning, desugared as discovery desugars them."""
    return registered({path.stem: desugar_into_canonical_normal_form(yaml.safe_load(path.read_text(encoding='utf-8')))
                       for directory in directories for path in sorted(directory.glob('*.cwl'))})


def _inferred(case: Path, expected: dict[str, Any]) -> tuple[WorkflowGraph | None, Diagnostics, int]:
    """The graph inference gives the case, every diagnostic its door gives, and how many passes it took: the phases
    run directly under `policy`, or the whole compile where `compiled` is set."""
    others = {'global': {path.stem: path for path in case.glob('*.wic') if path.name != 'workflow.wic'}}
    bundled = bundle_from_disk(case / 'workflow.wic', others, _tools(case.parent, case))
    policy = expected.get('policy', {})
    if expected.get('compiled'):
        options = CompileSettings(replace(CompileSettings().inference, strict=policy.get('strict', False)))
        try:
            compiled = compile_source(bundled, options, quiet=True)
        except SophiosError as error:
            return None, error.diagnostics, 0
        return compiled.graph, compiled.diagnostics, 0
    front = front_end(bundled.parsed, bundled.registry, name='workflow')
    assert front.graph is not None, list(front.diagnostics)
    linked = link(front.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    result = infer(linked.graph, InferencePolicy(**policy), InsertionCatalog.from_registry(bundled.registry))
    return result.graph, result.diagnostics, result.iterations


@pytest.mark.fast
@pytest.mark.parametrize('theme', [pytest.param(path, id=path.name)
                                   for path in sorted((DATA / 'infer').iterdir()) if path.is_dir()])
def test_inference_decides_each_case_as_its_theme_states(theme: Path) -> None:
    """For each case: `refused` (default false) is no graph; `diagnostics` is every diagnostic, in order, by code,
    severity, step and port, and what a guess or refusal names; `edges` the inferred edges as sink: source; `inputs`
    the workflow inputs left to the job; `inserted` how many converter steps were inserted, in `iterations` passes."""
    for name, expected in stated(theme).items():
        graph, diagnostics, iterations = _inferred(theme / name, expected)
        assert (graph is None) is expected.get('refused', False), (name, list(diagnostics))
        assert match(expected.get('diagnostics', []), [diagnostic(d) for d in diagnostics]), (name, list(diagnostics))
        if graph is None:
            continue
        names = Names.of(graph)
        edges = {f'{e.sink.step.name}@{e.sink.step.index}.{names.port(e.sink.port)}':
                 f'{e.source.step.name}@{e.source.step.index}.{names.port(e.source.port)}'
                 for e in graph.linked_edges if e.origin is EdgeOrigin.INFERRED}
        assert match(expected.get('edges', edges), edges), name
        assert match(expected.get('inputs', [names.port(port.name) for port in graph.workflow_inputs]),
                     [names.port(port.name) for port in graph.workflow_inputs]), name
        assert expected.get('inserted', 0) == sum(step.synthesized for step in graph.steps), name
        assert expected.get('iterations', iterations) == iterations, name
