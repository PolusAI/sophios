"""The resolved view's tool steps agree with the projection the Nextflow target reads today.

`tool_steps` and `output_sources` (design_docs/resolved-view-design.md §6) are computed from the graph;
`compilation_result_source` from the emitted CWL. For every compiled case and every workflow the search paths
reach, each tool step, its inputs' sources and fields, the root inputs and values, and the root outputs agree.
The one known disagreement is a crossing: a call's binding that merges, picks, defaults or computes, which the
view keeps as a `Crossing` and the projection joins to one edge. The target refuses both.
"""
# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any

import pytest

from sophios import compiler
from sophios.cli import default_compilation_settings
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.resolved import (
    Crossing, InputRef, OutputRef, ResolvedTool, ResolvedWorkflow, StepInput, Through, output_sources, tool_steps,
)
from sophios.lang.diagnostics import SophiosError
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_nf import compilation_result_source, compiled_source_to_nextflow
from sophios.wic_types import Yaml

from ..cases import cases
from ..helpers import REPO_ROOT
from ..test_examples import _is_workflow_document
from ..test_setup import load_test_registry, workflow_paths
from .testkit import compiled_case

#: The step keys the projection writes for what the view holds apart.
_STEP_KEYS = {'id', 'in', 'out', 'run', 'scatter', 'scatterMethod', 'when'}


def _spelled(found: Through | Crossing) -> str:
    match found:
        case Through(ref=InputRef(name=name)):
            return name
        case Through(ref=OutputRef(step=step, port=port)):
            return f'{step}/{port}'
        case Crossing(call=call, port=port):
            return f'<crossing {call}.{port}>'
    raise TypeError(found)


def _entry(port: StepInput, sources: tuple[Through | Crossing, ...]) -> tuple[list[str], dict[str, Any]]:
    """A view step input as the projection writes its entry: its sources, and its record's other fields."""
    fields = dict(port.fields)
    for key, value, present in (('linkMerge', port.link_merge, port.link_merge is not None),
                                ('pickValue', port.pick_value, port.pick_value is not None),
                                ('default', port.default, port.has_default),
                                ('valueFrom', port.value_from, port.value_from is not None)):
        if present:
            fields[key] = value
    if port.expression is not None:
        return [port.expression], fields
    return [_spelled(found) for found in sources], fields


def _projected(raw: Any) -> tuple[list[str], dict[str, Any]]:
    match raw:
        case str():
            return [raw], {}
        case list():
            return list(raw), {}
        case {'source': source, **rest}:
            return (source if isinstance(source, list) else [source]), {str(key): value for key, value in rest.items()}
        case dict():
            return [], dict(raw)
    return [repr(raw)], {}


def _requirements(view_step: Any) -> dict[str, dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {}
    for requirement in view_step.requirements:
        sections.setdefault('hints' if requirement.hint else 'requirements', {})[requirement.cls] = requirement.body
    return sections


def disagreements(result: CompilationResult) -> list[str]:  # pylint: disable=too-many-locals,too-many-branches
    """Where the view's tool steps, root inputs and outputs differ from the projection's, crossings aside.

    A projection that cannot be made, as for an edge from a nested output the core left unresolved, agrees with a
    view that holds a crossing.
    """
    view: ResolvedWorkflow = compiler.resolved_view(result)
    try:
        projection = compilation_result_source(result)
    except ValueError as error:
        return [] if _crossed(result) else [f'the projection fails, and the view holds no crossing: {error}']
    found: list[str] = []
    steps, projected = tool_steps(view), projection.workflow['steps']
    if [tool.step.name for tool in steps] != [step['id'] for step in projected]:
        return [f'steps: {[tool.step.name for tool in steps]} != {[step["id"] for step in projected]}']
    for tool, step, document in zip(steps, projected, projection.tools, strict=True):
        name = tool.step.name
        if list(step['in']) != [port.name for port in tool.step.inputs]:
            found.append(f'{name}: in {list(step["in"])} != {[port.name for port in tool.step.inputs]}')
            continue
        for port, sources in zip(tool.step.inputs, tool.sources, strict=True):
            if any(isinstance(source, Crossing) for source in sources):
                continue
            if _entry(port, sources) != _projected(step['in'][port.name]):
                found.append(f'{name}.{port.name}: {_entry(port, sources)} != {_projected(step["in"][port.name])}')
        condition = tool.step.condition.written if tool.step.condition is not None else None
        scatter = tool.step.scatter
        method = scatter.method if scatter else dict(tool.step.fields).get('scatterMethod')
        for key, mine in (('when', condition), ('scatterMethod', method)):
            if step.get(key) != mine:
                found.append(f'{name}.{key}: {mine!r} != {step.get(key)!r}')
        if 'scatter' in step and (scatter is None or list(scatter.inputs) != step['scatter']):
            found.append(f'{name}.scatter: {scatter} != {step["scatter"]!r}')
        passthrough = {key: value for key, value in step.items() if key not in _STEP_KEYS}
        fields = {key: value for key, value in tool.step.fields if key != 'scatterMethod'}
        if passthrough != {**fields, **_requirements(tool.step)}:
            found.append(f'{name}: fields {dict(tool.step.fields)} != {passthrough}')
        assert isinstance(tool.step.run, ResolvedTool) and isinstance(tool.step.run.document, dict)
        if desugar_into_canonical_normal_form(dict(tool.step.run.document)) != document:
            found.append(f'{name}: the tool differs')
    if list(projection.workflow['inputs']) != [port.name for port in view.inputs]:
        found.append(f'inputs: {[port.name for port in view.inputs]} != {list(projection.workflow["inputs"])}')
    if dict(projection.params) != {port.name: port.value for port in view.inputs if port.has_value}:
        found.append('values differ')
    outputs = {port.name: [port.source] if isinstance(port.source, str) else sorted(map(_spelled, sources))
               for port, sources in zip(view.outputs, output_sources(view))}
    projected_outputs = {name: sorted(_projected(definition.get('outputSource'))[0])
                         for name, definition in projection.workflow['outputs'].items()}
    if outputs != projected_outputs:
        found.append(f'outputs: {outputs} != {projected_outputs}')
    return found


def _crossed(result: CompilationResult) -> bool:
    view = compiler.resolved_view(result)
    return any(isinstance(source, Crossing) for sources in (*(found for tool in tool_steps(view) for found in
                                                              tool.sources), *output_sources(view))
               for source in sources)


def _agrees(result: CompilationResult) -> None:
    assert not (found := disagreements(result)), found
    if _crossed(result):
        with pytest.raises(ValueError):
            compiled_source_to_nextflow(result)


#: Every key a case of either list holds, which the drivers in test_lowering and test_capabilities read.
_KEYS = ('tool', 'out', 'job', 'workflow', 'tools', 'lowered', 'render', 'notes', 'params_file', 'error', 'findings')


@pytest.mark.fast
@pytest.mark.parametrize('case', [*cases('nextflow/lowered', *_KEYS), *cases('nextflow/rejected', *_KEYS)])
def test_a_compiled_case_agrees(case: Yaml) -> None:
    _agrees(compiled_case(case))


def _corpus() -> list[Any]:
    """Every workflow the search paths reach that compiles, by path."""
    return [pytest.param(path, id=str(path.relative_to(REPO_ROOT.parent)) if REPO_ROOT.parent in path.parents
                         else str(path))
            for paths in workflow_paths().values() for path in sorted(paths.values())
            if path.suffix == '.wic' and _is_workflow_document(path)]


@pytest.mark.parametrize('path', _corpus())
def test_a_reachable_workflow_agrees(path: Path) -> None:
    registry = load_test_registry()
    try:
        result = compiler.compile_source(bundle_from_disk(path, registry.workflows, registry.tools),
                                         default_compilation_settings(), testing=True)
    except SophiosError:
        pytest.skip('a fragment, compiled only inside a parent')
    _agrees(result)
