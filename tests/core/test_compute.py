"""The compute service's request, as Sophios builds it: `sophios.compute.build_request`.

Fast and without a service: what the request holds, and what Sophios refuses before sending. What the service
answers is `test_compute_service.py`'s, against the service itself.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sophios import compute
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.lang.error_codes import SophiosErrorCode

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def _writer() -> CommandLineTool:
    """A tool that writes into the folder its input `out` names, as the tool builder writes one (a list of
    requirements), reading `model`."""
    inputs = Inputs(out=Input(cwl.directory, position=1), model=Input(cwl.file, position=2))
    outputs = Outputs(out=Output(cwl.directory, glob='$(inputs.out.basename)'))
    return CommandLineTool('writer', inputs, outputs).stage(inputs.out, writable=True).base_command('touch')


def _workflow(out: str = 'results/run7', model: str = 'models/sam3.pt', name: str = 'segment') -> Workflow:
    step = Step(_writer(), step_name='write')
    step.inputs.out = out
    step.inputs.model = model
    return Workflow([step], name)


@pytest.mark.fast
def test_the_request_carries_the_compiled_workflow_and_every_path_as_written() -> None:
    """One inline document, its inputs, and `basepath` only when given; nothing is joined or checked."""
    request = compute.build_request(_workflow(model='https://example.org/sam3.pt'), basepath='/projects/lab')
    assert set(request) == {'id', 'workflow', 'inputs', 'basepath'}
    assert request['basepath'] == '/projects/lab'
    assert request['workflow']['class'] == 'Workflow' and request['workflow']['cwlVersion'] == 'v1.2'
    assert all(isinstance(step['run'], dict) for step in request['workflow']['steps'])
    assert request['inputs'] == {'write~out': {'class': 'Directory', 'location': 'results/run7'},
                                 'write~model': {'class': 'File', 'location': 'https://example.org/sam3.pt'}}
    assert 'basepath' not in compute.build_request(_workflow(out='/projects/lab/out'))


@pytest.mark.fast
def test_an_output_folder_is_sent_as_a_folder_not_as_the_local_runs_bare_name(tmp_path: Path) -> None:
    """`compile()` gives a local runner an output folder as a bare name, and refuses an absolute one; the service
    is sent the folder where the input names it, for it to check and write on the cluster."""
    tool = tmp_path / 'writes_in.cwl'
    tool.write_text("""cwlVersion: v1.2
class: CommandLineTool
requirements:
  InlineJavascriptRequirement: {}
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.out)
        writable: true
inputs:
  out:
    type: Directory
outputs:
  result:
    type: Directory
    outputBinding:
      glob: $(inputs.out.basename)
baseCommand: touch
""", encoding='utf-8')
    step = Step(clt_path=tool, step_name='write')
    step.inputs.out = 'results'
    workflow = Workflow([step], 'in_place')
    assert workflow.compile().cwl_job_inputs['write~out'] == 'results'
    request = compute.build_request(workflow)
    assert request['inputs']['write~out'] == {'class': 'Directory', 'location': 'results'}
    (run,) = (step['run'] for step in request['workflow']['steps'])
    declared = run['inputs'] if isinstance(run['inputs'], dict) else {i['id']: i for i in run['inputs']}
    assert declared['out']['type'] == 'Directory'
    step.inputs.out = '/projects/lab/results'
    assert compute.build_request(workflow)['inputs']['write~out']['location'] == '/projects/lab/results'


@pytest.mark.fast
def test_a_run_id_is_the_workflow_name_the_time_and_four_hex_digits_within_the_services_rule() -> None:
    """`<name>-<UTC yyyymmddThhmmss>-<4 hex>`; a name outside the rule is fitted to it."""
    rule = Draft202012Validator(compute.SCHEMA['$defs']['Id'])
    now = datetime(2026, 10, 26, 14, 22, 5, tzinfo=timezone.utc)
    assert re.fullmatch(r'segment-20261026T142205-[0-9a-f]{4}', compute.run_id('segment', now))
    for name in ['segment', 'a b/c', '.hidden', '__x', 'é' * 5, '', 'x' * 300, '-', 'ok.name_1-2']:
        made = compute.run_id(name, now)
        assert rule.is_valid(made) and len(made) <= 128, (name, made)
    assert compute.run_id('a b/c', now).startswith('a-b-c-2026')
    assert compute.run_id('...', now).startswith('run-2026')
    assert compute.run_id('a', now) != compute.run_id('a', now)


@pytest.mark.fast
def test_a_path_that_names_nothing_on_the_cluster_is_refused_before_sending_naming_its_input() -> None:
    """`~` and an environment variable are this machine's; a URL is the service's to read."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.build_request(_workflow(out='~/results', model='$HOME/sam3.pt'))
    assert [(d.code, d.message.split(':')[0]) for d in refused.value.diagnostics] == [
        (SophiosErrorCode.COMPUTE_REQUEST_REFUSED, 'input write~out'),
        (SophiosErrorCode.COMPUTE_REQUEST_REFUSED, 'input write~model')]
    assert "'~/results' names nothing on the cluster" in refused.value.diagnostics[0].message
    compute.build_request(_workflow(model='https://example.org/$file?x=~y'))
    compute.build_request(_workflow(out='results/a~b', model='models/50$'))


@pytest.mark.fast
def test_every_field_the_schema_refuses_is_a_line_in_the_schemas_words() -> None:
    """All at once, each where it is and what the field is."""
    with pytest.raises(compute.RequestRefused) as refused:
        compute.build_request(_workflow(), leader={'time': '3 days', 'memory': '8GB'}, log_level='LOUD',
                              steps={'partition': 'a b'})
    assert sorted(d.message for d in refused.value.diagnostics) == [
        "leader.memory: '8GB' is not the leader's memory, as Slurm writes it: a whole number with K, M, G or T, "
        'such as 8G',
        "leader.time: '3 days' is not a Slurm time limit: M, M:S, H:M:S, D-H, D-H:M or D-H:M:S, such as 3-00:00:00",
        "logLevel: 'LOUD' is not Toil's log level: DEBUG, INFO (the default), WARNING or ERROR",
        "steps.partition: 'a b' is not a Slurm partition of this cluster: letters, digits, '_', '.' or '-'"]
    request = compute.build_request(_workflow(), leader={'partition': 'normal', 'time': '1-00:00:00', 'memory': '8G'},
                                    steps={'partition': 'normal', 'gpuPartition': 'gpu', 'time': '10'},
                                    log_level='DEBUG')
    assert (request['leader']['time'], request['steps']['gpuPartition'], request['logLevel']) == (
        '1-00:00:00', 'gpu', 'DEBUG')


@pytest.mark.fast
def test_a_workflow_with_a_real_time_analysis_is_refused_the_service_never_runs_one() -> None:
    """Refused before sending, naming the analysis, rather than dropped."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    declaration = Step(clt_path=ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl')
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    with pytest.raises(compute.RequestRefused, match="real-time analysis 'touch', which the compute service never"):
        compute.build_request(Workflow([touch, declaration], 'live'))


@pytest.mark.fast
@pytest.mark.xfail(strict=True, reason="the vendored schema, of the driver's e7ec43b, has no outputMode yet")
def test_where_the_outputs_go_is_one_choice_and_workflow_out_dir_is_its_folder() -> None:
    """`workflow_out_dir` copies them into a folder; `workflow_in_place` writes each where its input names it."""
    request = compute.build_request(_workflow(), workflow_out_dir='/projects/lab/results')
    assert (request['outputMode'], request['workflowOutDir']) == ('workflowOutDir', '/projects/lab/results')
    assert compute.build_request(_workflow(), workflow_out_dir='results')['workflowOutDir'] == 'results'
    assert compute.build_request(_workflow(), workflow_in_place=True)['outputMode'] == 'workflowInPlace'
    assert 'outputMode' not in compute.build_request(_workflow())
    with pytest.raises(ValueError, match='not both'):
        compute.build_request(_workflow(), workflow_out_dir='results', workflow_in_place=True)


@pytest.mark.fast
def test_the_vendored_schema_is_a_json_schema_2020_12_document_and_its_example_a_request() -> None:
    """The copy the requests are checked with is itself valid, and takes its own example."""
    Draft202012Validator.check_schema(compute.SCHEMA)
    assert Draft202012Validator(compute.SCHEMA).is_valid(compute.SCHEMA['examples'][0])
