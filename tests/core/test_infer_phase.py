"""Inference, judged on cases in files: tests/core/data/infer/<theme>/<case> is a workflow.wic with the tools and child
workflows it names (beside it, or shared by the theme), and the theme's expected.yaml states what each case infers.
That every inferred edge is the independent model's choice is a law of test_emit.py's drawn workflows."""
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import example, given, strategies as st

from sophios.compiler import CompileSettings, compile_source
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.infer import InferencePolicy, InsertionCatalog, infer
from sophios.ir.link import link
from sophios.ir.names import Names
from sophios.ir.pipeline import front_end
from sophios.ir.types import WorkflowGraph
from sophios.lang.diagnostics import Diagnostics, SophiosError
from sophios.wic_types import StepId, Tool

from .budgets import COMPILED
from .cases import DATA, diagnostic, match
from .helpers import stated
from .hermetic import compile_hermetic
from .synthetic_tools import SYNTHETIC_NS, clt, registered


def _tools(*directories: Path) -> Any:
    """The synthetic tools, and each tool in `directories` (a `.cwl`, or a `.wic` in a `tools` folder), the later
    winning, as discovery reads them: a `.wic` is read when a step runs it."""
    written = {StepId(path.stem, SYNTHETIC_NS): Tool(str(path))
               for directory in directories for path in sorted((directory / 'tools').glob('*.wic'))}
    return {**registered({path.stem: yaml.safe_load(path.read_text(encoding='utf-8'))
                          for directory in directories for path in sorted(directory.glob('*.cwl'))}), **written}


def _inferred(case: Path, expected: dict[str, Any]) -> tuple[WorkflowGraph | None, Diagnostics]:
    """The graph inference gives the case and every diagnostic its door gives: the phases run directly under
    `policy`, or the whole compile where `compiled` is set."""
    others = {'global': {path.stem: path for path in case.glob('*.wic') if path.name != 'workflow.wic'}}
    bundled = bundle_from_disk(case / 'workflow.wic', others, _tools(case.parent, case))
    policy = expected.get('policy', {})
    if expected.get('compiled'):
        options = CompileSettings(replace(CompileSettings().inference, strict=policy.get('strict', False)))
        try:
            compiled = compile_source(bundled, options)
        except SophiosError as error:
            return None, error.diagnostics
        return compiled.graph, compiled.diagnostics
    front = front_end(bundled.parsed, bundled.registry, name='workflow')
    assert front.graph is not None, list(front.diagnostics)
    linked = link(front.graph)
    assert linked.graph is not None, list(linked.diagnostics)
    result = infer(linked.graph, InferencePolicy(**policy), InsertionCatalog.from_registry(bundled.registry)
                   if policy.get('insert_steps_automatically') else InsertionCatalog())
    return result.graph, result.diagnostics


@pytest.mark.fast
@pytest.mark.parametrize('theme', [pytest.param(path, id=path.name)
                                   for path in sorted((DATA / 'infer').iterdir()) if path.is_dir()])
def test_inference_decides_each_case_as_its_theme_states(theme: Path) -> None:
    """For each case: `refused` (default false) is no graph; `diagnostics` is every diagnostic, in order, by code,
    severity, step and port, and what a guess or refusal names; `edges` the inferred edges as sink: source; `inputs`
    the workflow inputs left to the job; `inserted` how many converter steps were inserted."""
    for name, expected in stated(theme).items():
        graph, diagnostics = _inferred(theme / name, expected)
        assert (graph is None) is expected.get('refused', False), (name, list(diagnostics))
        assert match(expected.get('diagnostics', []), [diagnostic(d) for d in diagnostics]), (name, list(diagnostics))
        if graph is None:
            continue
        names = Names.of(graph)
        edges = {f'{e.sink.step.name}@{e.sink.step.index}.{names.port(e.sink.port)}':
                 f'{e.source.step.name}@{e.source.step.index}.{names.port(e.source.port)}'
                 for e in graph.linked_edges if e.inferred}
        assert match(expected.get('edges', edges), edges), name
        assert match(expected.get('inputs', [names.port(port.name) for port in graph.workflow_inputs]),
                     [names.port(port.name) for port in graph.workflow_inputs]), name
        assert expected.get('inserted', 0) == sum(step.synthesized for step in graph.steps), name


def _kinds(formats: tuple[int, ...]) -> dict[str, dict[str, Any]]:
    return {f'file{n}': {'type': 'File', 'format': f'edam:format_{n}'} for n in formats}


_CONVERTERS = st.lists(st.tuples(st.sets(st.integers(1, 3), min_size=1), st.integers(1, 3)), max_size=3)


@pytest.mark.fast
@given(_CONVERTERS, st.lists(st.tuples(st.sampled_from(['mk', 'use']), st.integers(1, 3)), min_size=1, max_size=8))
@example([({1}, 2)], [('mk', 1), ('use', 2)])
@example([({1, 2}, 3)], [('mk', 1), ('mk', 2), ('use', 3)])
@COMPILED
def test_an_inserted_converter_reads_upstream_and_feeds_the_input_it_was_inserted_for(
        converters: list[tuple[set[int], int]], steps: list[tuple[str, int]]) -> None:
    """With insertion on, whichever converters exist: each converter inserted reads a step the author wrote before
    it, the job gives the rest of its inputs, and the step after it reads one of its outputs."""
    specs = {f'mk{n}': clt({}, _kinds((n,))) for n in (1, 2, 3)}
    specs |= {f'use{n}': clt(_kinds((n,)), {}) for n in (1, 2, 3)}
    specs |= {f'insert_steps_automatically_c{k}': clt(_kinds(tuple(sorted(read))), _kinds((made,)))
              for k, (read, made) in enumerate(converters)}
    graph = compile_hermetic({'steps': [{'id': f'{kind}{n}'} for kind, n in steps]}, tools=registered(specs, {}),
                             insert_steps_automatically=True).graph
    position = {step.id: at for at, step in enumerate(graph.steps)}
    for at, step in enumerate(graph.steps):
        if not step.synthesized:
            continue
        reads = {edge.sink: edge.source for edge in graph.linked_edges if edge.sink.step == step.id}
        given_by_job = {port for _, ports in graph.input_mapping for port in ports}
        assert reads and all(port.id in reads or port.id in given_by_job for port in step.inputs), step
        assert all(position[source.step] < at and not graph.steps[position[source.step]].synthesized
                   for source in reads.values()), step
        assert any(edge.source.step == step.id and edge.sink.step == graph.steps[at + 1].id
                   for edge in graph.linked_edges), step
