"""Real-time analysis: a `cwl_subinterpreter` step declares an analysis Sophios runs beside the workflow."""
import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from sophios import realtime
from sophios.cli import default_compilation_settings
from sophios.input_output import NoAliasDumper
from sophios.ir.realtime import Declaration
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.realtime import Analysis
from sophios.wic_types import StepId, Tool, Tools, Yaml

from .hermetic import compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

ADAPTER_PATH: Final = Path(__file__).resolve().parents[2] / 'cwl_adapters' / 'file_watchers' / 'cwl_subinterpreter.cwl'
TOOLS: Final[Tools] = {
    **SYNTHETIC_TOOLS,
    StepId('cwl_subinterpreter', SYNTHETIC_NS): Tool(str(ADAPTER_PATH),
                                                     yaml.safe_load(ADAPTER_PATH.read_text(encoding='utf-8'))),
}


def _ii(value: Any) -> Yaml:
    return {'wic_inline_input': value}


def _declaration_step(**overrides: Any) -> Yaml:
    """A well-formed declaration: count lines of `a.txt` whenever a `*.txt` changes."""
    bindings: Yaml = {'file_pattern': _ii('*.txt'), 'cwl_tool': _ii('count'), 'max_times': _ii('3'),
                      'config': _ii({'in': {'file': 'a.txt'}})}
    bindings.update(overrides)
    return {'id': 'cwl_subinterpreter', 'in': {key: value for key, value in bindings.items() if value is not None}}


def _step_ids(cwl: Yaml) -> list[str]:
    return [step['id'] for step in cwl['steps']]


def _declaration_line(document: Yaml) -> int:
    """The line the declaration step starts on in `document` as `hermetic.bundle` writes it."""
    text = yaml.dump(document, Dumper=NoAliasDumper, sort_keys=False, line_break='\n', indent=2)
    return next(number for number, line in enumerate(text.splitlines(), start=1)
                if line.strip() == '- id: cwl_subinterpreter')


@pytest.mark.fast
def test_a_declaration_is_not_emitted_and_is_returned_instead() -> None:
    """The declaration step leaves the CWL, and what it declares is in the result."""
    document = {'steps': [{'id': 'mk_file', 'in': {'name': _ii('a.txt')}}, _declaration_step()]}
    result = compile_hermetic(document, tools=TOOLS)

    assert _step_ids(result.artifact.cwl) == ['oracle__step__1__mk_file']
    assert result.artifact.job_inputs == {'oracle__step__1__mk_file___name': 'a.txt'}
    (declaration,) = result.realtime
    assert declaration == Declaration('oracle', 'count', '*.txt', 3, 60, {'in': {'file': 'a.txt'}},
                                      declaration.span)
    assert declaration.span is not None and declaration.span.start_line == _declaration_line(document)


@pytest.mark.fast
def test_a_declaration_in_a_subworkflow_is_taken_out_and_its_workflow_may_be_left_empty() -> None:
    """A subworkflow whose only step is a declaration is emitted with no steps; the declaration names it."""
    producer = {'id': 'mk_file', 'in': {'name': _ii('a.txt')}}
    analysis = subworkflow_step('watch.wic', {'steps': [_declaration_step(interval=_ii(5))]})
    result = compile_hermetic({'steps': [producer, analysis]}, tools=TOOLS)

    (child,) = [child for child in result.artifact.children if child.name == 'watch']
    assert child.cwl['steps'] == []
    assert [(d.document, d.interval) for d in result.realtime] == [('watch', 5)]


@pytest.mark.fast
def test_a_workflow_of_only_a_declaration_compiles_to_no_steps() -> None:
    """The at-least-one-step rule reads what the author wrote, so the workflow is valid and empty."""
    result = compile_hermetic({'steps': [_declaration_step()]}, tools=TOOLS)
    assert result.artifact.cwl['steps'] == []
    assert len(result.realtime) == 1


@pytest.mark.fast
@pytest.mark.parametrize(('port', 'overrides'), [
    ('file_pattern', {'file_pattern': {'wic_alias': 'made'}}),
    ('cwl_tool', {'cwl_tool': None}),
    ('cwl_tool', {'cwl_tool': _ii('')}),
    ('max_times', {'max_times': _ii('many')}),
    ('max_times', {'max_times': _ii(0)}),
    ('interval', {'interval': _ii(-1)}),
    ('config', {'config': _ii('in: a.txt')}),
])
def test_a_malformed_declaration_is_wic044_at_its_step(port: str, overrides: Yaml) -> None:
    """Each malformed input is reported at compile time, at the step and port, before anything runs."""
    producer = {'id': 'mk_file', 'in': {'name': _ii('a.txt')}, 'out': [{'file': {'wic_anchor': 'made'}}]}
    document = {'steps': [producer, _declaration_step(**overrides)]}
    with pytest.raises(SophiosError) as raised:
        compile_hermetic(document, tools=TOOLS)

    (diagnostic,) = raised.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.REALTIME_DECLARATION
    assert diagnostic.span is not None and diagnostic.span.start_line == _declaration_line(document)
    assert diagnostic.locator is not None and diagnostic.locator.port == port


def _analyses(document: Yaml, yml_paths: dict[str, dict[str, Path]] | None = None) -> tuple[Analysis, ...]:
    """Compile `document` and then the analyses it declares, as the CLI does."""
    options, graph_settings = default_compilation_settings()
    return realtime.compile_analyses(compile_hermetic(document, tools=TOOLS).realtime, yml_paths or {}, TOOLS,
                                     options, graph_settings)


def _only_analysis(document: Yaml, yml_paths: dict[str, dict[str, Path]] | None = None) -> Analysis:
    """The one analysis `document` declares."""
    analyses = _analyses(document, yml_paths)
    assert len(analyses) == 1
    return analyses[0]


def _file_inputs(job: Yaml) -> list[str]:
    """The location of each job input that is a File, sorted."""
    return sorted(value['location'] for value in job.values()
                  if isinstance(value, dict) and value.get('class') == 'File')


@pytest.mark.fast
def test_a_tool_analysis_compiles_with_every_config_input_a_literal() -> None:
    """A plain file name and a nested `!ii` in `config: in:` are both literals of the analysis step.

    The shape of the corpus `npt_amber.wic` declaration: read as written, `a.txt` in an
    `in:` would be an edge reference and fail with wic011.
    """
    config = _ii({'in': {'file': 'a.txt', 'name': _ii('b.txt')}})
    analysis = _only_analysis({'steps': [_declaration_step(cwl_tool=_ii('xform'), config=config)]})

    assert analysis.name == 'oracle'
    assert _file_inputs(analysis.artifact.job_inputs) == ['a.txt']
    assert 'b.txt' in analysis.artifact.job_inputs.values()


@pytest.mark.fast
def test_a_wic_analysis_compiles_with_its_config_as_the_sidecar_of_its_steps(tmp_path: Path) -> None:
    """A `.wic` analysis is configured per step, as `wic: steps:`; the shape of `cwl_subinterpreter_protein.wic`."""
    (tmp_path / 'measure.wic').write_text(yaml.safe_dump({'steps': [{'id': 'count'}, {'id': 'count'}]}),
                                          encoding='utf-8')
    config = _ii({'(1, count)': {'in': {'file': 'a.txt'}}, '(2, count)': {'in': {'file': 'b.txt'}}})
    declaration = _declaration_step(cwl_tool=_ii('measure.wic'), config=config)
    analysis = _only_analysis({'steps': [declaration]}, {'global': {'measure': tmp_path / 'measure.wic'}})

    assert _file_inputs(analysis.artifact.job_inputs) == ['a.txt', 'b.txt']


@pytest.mark.fast
def test_two_declarations_in_one_workflow_get_distinct_names() -> None:
    """Each analysis keeps its files under its own name; a repeated document name gets a suffix."""
    analyses = _analyses({'steps': [_declaration_step(), _declaration_step()]})
    assert [analysis.name for analysis in analyses] == ['oracle', 'oracle_2']


@pytest.mark.fast
def test_an_analysis_that_does_not_compile_is_wic044_at_the_declaration() -> None:
    """An unknown analysis fails the compile at the declaration, before anything runs."""
    document = {'steps': [{'id': 'mk_file', 'in': {'name': _ii('a.txt')}}, _declaration_step(cwl_tool=_ii('nope'))]}
    with pytest.raises(SophiosError) as raised:
        _analyses(document)

    assert {diagnostic.code for diagnostic in raised.value.diagnostics} == {SophiosErrorCode.REALTIME_DECLARATION}
    assert all(diagnostic.span is not None and diagnostic.span.start_line == _declaration_line(document)
               for diagnostic in raised.value.diagnostics)
    assert "'nope'" in str(raised.value)


@pytest.mark.fast
def test_the_manifest_lists_each_analysis_and_goes_when_there_are_none(tmp_path: Path) -> None:
    """`write` writes each analysis and a manifest naming it; a compile with none removes an old manifest."""
    analysis = _only_analysis({'steps': [_declaration_step(interval=_ii('5'))]})
    realtime.write((analysis,), tmp_path, 'wf', tmp_path / 'project')

    (entry,) = json.loads(realtime.manifest_path(tmp_path, 'wf').read_text(encoding='utf-8'))
    assert {key: entry[key] for key in ('name', 'analysis', 'file_pattern', 'max_times', 'interval')} == {
        'name': 'oracle', 'analysis': 'count', 'file_pattern': '*.txt', 'max_times': 3, 'interval': 5}
    assert (tmp_path / entry['cwl']).is_file() and (tmp_path / entry['inputs']).is_file()
    assert entry['root_dir'] == str(tmp_path / 'project')

    realtime.write((), tmp_path, 'wf', tmp_path)
    assert not realtime.manifest_path(tmp_path, 'wf').exists()
