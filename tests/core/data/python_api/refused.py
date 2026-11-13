"""Python the API can prove wrong, one mistake to a function; refused.yaml beside it states what each raises."""
# pylint: disable=missing-function-docstring  # each function's name is the mistake it makes
import asyncio
import shutil
from decimal import Decimal
from pathlib import Path

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow

HERE = Path(__file__).resolve().parent
ADAPTERS = HERE.parents[3] / 'cwl_adapters'


def _step(tool: str, name: str | None = None, **values: object) -> Step:
    """A step running the shipped `tool`, with `values` bound."""
    step = Step(clt_path=ADAPTERS / f'{tool}.cwl', step_name=name)
    for port, value in values.items():
        setattr(step.inputs, port, value)
    return step


def _sink(declared: object) -> Step:
    """A step taking `x`, declared `declared`, and making nothing, from the document of a built tool."""
    tool = CommandLineTool('sink', Inputs(x=Input(declared)), Outputs()).base_command('true')
    return Step.from_cwl_document(tool.to_cwl_document(), process_name='sink')


def _made() -> tuple[Step, Step]:
    """Steps `a` and `b`, each touching a file of its name."""
    return _step('touch', 'a', filename='a.txt'), _step('touch', 'b', filename='b.txt')


# Bindings, refused where they are written: a port the tool does not declare, which only a StepInput may bind; a
# source of a type the port cannot take; a port object where a literal goes, at any depth; a string as a source.

def misspelled_port() -> None:
    _step('append').misspelled = 'Hello'


def undeclared_port() -> None:
    _step('echo').inputs.go = True


def undeclared_port_a_record_bound() -> None:
    _step('echo', go=StepInput(default=True)).inputs.go = True


def output_of_another_type() -> None:
    _step('append').inputs.str = _made()[0].outputs.file


def workflow_input_of_another_type() -> None:
    append = _step('append')
    flow = Workflow([append], 'w')
    append.inputs.file = flow.inputs.text.as_type('string')


def merged_sources_of_another_type() -> None:
    a, b = _made()
    _sink(cwl.int).inputs.x = StepInput(source=[a.outputs.file, b.outputs.file], link_merge='merge_flattened')


def picked_sources_of_another_type() -> None:
    a, b = _made()
    _sink(cwl.int).inputs.x = StepInput(source=[a.outputs.file, b.outputs.file], pick_value='first_non_null')


def port_in_a_list() -> None:
    _step('cat').inputs.file = [_made()[0].outputs.file]


def workflow_input_in_a_tuple() -> None:
    cat = _step('cat')
    cat.inputs.file = (Workflow([cat], 'w').inputs.f,)


def port_in_a_nested_list() -> None:
    _step('cat').inputs.file = [[_made()[0].outputs.file]]


def port_in_a_mapping() -> None:
    _step('cat').inputs.file = {'x': _made()[0].outputs.file}


def port_in_a_list_of_mappings() -> None:
    _step('cat').inputs.file = [{'x': [_made()[0].outputs.file]}]


def step_input_in_a_list() -> None:
    _step('cat').inputs.file = [StepInput(source=_made()[0].outputs.file)]


def string_as_a_source() -> None:
    StepInput(source='go')


def strings_as_sources() -> None:
    StepInput(source=['go'])


def _decimal_for(declared: object) -> None:
    sink = _sink(declared)
    sink.inputs.x = Decimal(3)
    Workflow([sink], 'w').compile()


def decimal_for_an_int() -> None:
    _decimal_for(cwl.int)


def decimal_for_a_float() -> None:
    _decimal_for(cwl.float)


def decimal_for_a_boolean() -> None:
    _decimal_for(cwl.boolean)


def decimal_for_a_string() -> None:
    _decimal_for(cwl.string)


# Workflows wired wrong, refused when compiled: a step reads only an earlier step of its own workflow, never its
# workflow's own output; a merge the bind cannot judge is the compiler's.

def step_before_its_source() -> None:
    a = _made()[0]
    Workflow([_step('append', file=a.outputs.file, str='Hello'), a], 'w').compile()


def source_outside_the_workflow() -> None:
    Workflow([_step('append', file=_made()[0].outputs.file, str='Hello')], 'w').compile()


def record_before_its_source() -> None:
    a = _made()[0]
    Workflow([_step('cat', file=StepInput(source=[a.outputs.file], pick_value='first_non_null')), a], 'w').compile()


def reads_its_own_output() -> None:
    a, cat = _made()[0], _step('cat')
    flow = Workflow([a, cat], 'loop')
    flow.outputs.made = a.outputs.file
    cat.inputs.file = flow.outputs.made
    flow.compile()


def merge_into_a_file() -> None:
    a, b = _made()
    sink = _sink(cwl.file)
    sink.inputs.x = StepInput(source=[a.outputs.file, b.outputs.file], link_merge='merge_flattened')
    Workflow([a, b, sink], 'w').compile()


def _sharing_a_stem() -> Workflow:
    return Workflow([Step(clt_path=HERE / 'cat.cwl'), _step('cat', 'shipped_cat')], 'shared_stem')


def two_tools_one_stem() -> None:
    _sharing_a_stem().compile()


def two_tools_one_stem_written() -> None:
    _sharing_a_stem().write_wic(Path('.'))


def cwl_workflow_as_a_tool() -> None:
    step = Step(clt_path=HERE / 'say.cwl')
    step.inputs.f = 'a.txt'
    Workflow([step], 'wsay').compile()


def absolute_output_directory() -> None:
    step = Step(clt_path=HERE / 'write_dir.cwl')
    step.inputs.outDir = Path('/absolute.outDir')
    Workflow([step], 'w').compile()


# Names Sophios cannot write: a step's name is an id of the compiled CWL, a workflow's the stem of its files.

def step_named_with_a_tilde() -> None:
    _step('touch', 'a~b')


def step_named_hidden() -> None:
    _step('touch', '.hidden')


def workflow_named_for_the_directory_above() -> None:
    Workflow([_step('touch')], '..')


# Scatter needs a bound, array-valued port of the step itself. A pickValue that picks one source delivers one value,
# and a nested workflow adds no array layer of its own.

def scatter_over_an_unbound_port() -> None:
    echo = _step('echo')
    echo.scatter = [echo.inputs.message]


def scatter_over_a_scalar() -> None:
    echo = _step('echo', message='hello')
    echo.scatter = [echo.inputs.message]


def scatter_over_a_path() -> None:
    cat = Step(clt_path=HERE / 'cat.cwl')
    cat.inputs.f = Path('/cluster/images.ome.zarr')
    cat.scatter_on(cat.inputs.f)


def scatter_over_another_steps_port() -> None:
    _step('echo').scatter = [_step('echo', message=['a', 'b']).inputs.message]


def scatter_over_a_picked_value() -> None:
    a, b = _made()
    sink = _sink(cwl.file)
    sink.inputs.x = StepInput(source=[a.outputs.file, b.outputs.file], pick_value='the_only_non_null')
    sink.scatter_on(sink.inputs.x)


def scatter_over_a_nested_workflows_output() -> None:
    touch = _step('touch', filename='empty.txt')
    inner = Workflow([touch], 'inner')
    inner.outputs.made = touch.outputs.file
    cat = _step('cat', file=inner.outputs.made)
    cat.scatter_on(cat.inputs.file)


# Each door: an unknown target, before anything is compiled; what the Nextflow target cannot run; a real-time
# analysis, which run_async() never runs.

def unknown_target_to_compile() -> None:
    Workflow([_step('touch')], 'elsewhere').compile(target='wdl')  # type: ignore[call-overload]


def unknown_target_to_check() -> None:
    Workflow([_step('touch')], 'elsewhere').check(target='wdl')  # type: ignore[arg-type]


def unknown_target_to_run() -> None:
    Workflow([_step('touch')], 'elsewhere').run(target='wdl')  # type: ignore[arg-type]


def unknown_target_to_start() -> None:
    Workflow([_step('touch')], 'elsewhere').start(target='wdl')  # type: ignore[arg-type]


def string_comparison_under_nextflow() -> None:
    echo = _step('echo', message='hi')
    echo.when = '$(inputs.message < "27")'
    Workflow([echo], 'refused').compile(target='nextflow')


def real_time_analysis_under_run_async() -> None:
    declaration = Step(clt_path=ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl')
    declaration.inputs.file_pattern = 'empty.txt'
    declaration.inputs.cwl_tool = 'touch'
    declaration.inputs.max_times = 2
    declaration.inputs.config = {'in': {'filename': 'again.txt'}}
    asyncio.run(Workflow([_step('touch', filename='empty.txt'), declaration], 'py_async').run_async())


# Files: a .wic is written to a .wic file or a directory, by write_wic, which replaced the removed .wic text
# accessors, and never over another tool of the stem it names; a step inputs file belongs to a tool on disk.

def wic_text_of_a_workflow() -> None:
    _ = Workflow([], 'wf').to_wic_yaml


def bundle_to_a_yaml_file() -> None:
    Workflow([], 'wf').write_wic(Path('workflow.yaml'))


def bundle_over_another_tool_of_its_stem() -> None:
    shutil.copyfile(HERE / 'false.cwl', 'echo.cwl')
    Workflow([_step('echo', message='hi')], 'wf').write_wic(Path('.'))


def step_inputs_file_for_a_built_tool() -> None:
    tool = CommandLineTool('emit', Inputs(), Outputs()).base_command('true')
    Step(tool, step_inputs_file='inputs.yml')  # type: ignore[call-overload]


# The tool builder: raw CWL may not overwrite what the builder manages or how the document is loaded, and a port is
# referenced by its object, never by its name as text.

def raw_key_the_builder_manages() -> None:
    CommandLineTool('custom', Inputs(), Outputs()).extra(inputs={'bad': 'idea'})


def loader_key_in_a_requirement() -> None:
    CommandLineTool('custom', Inputs(), Outputs()).requirement({'class': 'EnvVarRequirement', '$import': 'bad'})


def port_named_by_text() -> None:
    CommandLineTool('demo', Inputs(input=Input(cwl.file)), Outputs()).stage('input')
