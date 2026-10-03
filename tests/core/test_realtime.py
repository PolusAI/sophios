"""Real-time analysis: a `cwl_subinterpreter` step declares an analysis Sophios runs beside the workflow."""
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from sophios.input_output import NoAliasDumper
from sophios.ir.realtime import Declaration
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
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
