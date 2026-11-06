"""Python workflows, each the spelling of the `.wic` of its name beside it, which is written by hand: the expected
result. Each docstring says what the pair pins."""
from pathlib import Path
from typing import Any

from sophios.api.python.workflow import Step, StepInput, Workflow

TOOLS = Path(__file__).resolve().parents[4] / 'cwl_adapters'


def _step(tool: str, name: str | None = None, **values: Any) -> Step:
    """A step named `name` that runs the shipped `tool`, with `values` bound to its inputs."""
    step = Step(clt_path=TOOLS / f'{tool}.cwl', step_name=name)
    for port, value in values.items():
        setattr(step.inputs, port, value)
    return step


def _sink(declared: str, name: str = 'sink', **values: Any) -> Step:
    """A step named `name` that takes `x`, declared `declared`, with `values` bound."""
    step = Step.from_cwl_document({'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'true',
                                   'inputs': {'x': {'type': declared}}, 'outputs': {}}, process_name=name)
    for port, value in values.items():
        setattr(step.inputs, port, value)
    return step


def attribute_sugar() -> Workflow:
    """The bare `step.x` sugar binds as `step.inputs.x` does; it resolves through `__getattr__`, so it can drift."""
    touch, append = Step(clt_path=TOOLS / 'touch.cwl'), Step(clt_path=TOOLS / 'append.cwl')
    touch.filename = 'empty.txt'
    append.file = touch.file
    append.str = 'Hello'
    return Workflow([touch, append], 'attribute_sugar')


def linear_inference() -> Workflow:
    """An unbound input is left for inference, not guessed by the API."""
    return Workflow([_step('touch', filename='empty.txt'), _step('append', str='Hello'), _step('cat')],
                    'linear_inference')


def equal_producers() -> Workflow:
    """A guessed edge is a note on the compiled object, naming the step it chose."""
    return Workflow([_step('touch', 'first', filename='a.txt'), _step('touch', 'second', filename='b.txt'),
                     _step('cat')], 'equal_producers')


def empty_string() -> Workflow:
    """An empty string is a value, not an absent one: a truthiness test anywhere on this path would drop it."""
    return Workflow([_step('echo', message='')], 'empty_string')


def array_input_scatter() -> Workflow:
    """A `string[]` workflow input binds to the `string` port its step scatters over."""
    touch = _step('touch')
    workflow = Workflow([touch], 'array_input_scatter')
    touch.inputs.filename = workflow.inputs.names.as_type('string[]')
    touch.scatter_on(touch.inputs.filename)
    return workflow


def renamed_output() -> Workflow:
    """A workflow output names the step that produces it as the user named it."""
    touch = _step('touch', filename='empty.txt')
    add = _step('append', 'add', file=touch.outputs.file, str='Hello')
    workflow = Workflow([touch, add], 'renamed_output')
    workflow.outputs.file = add.outputs.file
    return workflow


def re_export() -> Workflow:
    """A nested workflow's output feeds a later sibling step, as a step's output does, and backs a parent output."""
    touch = _step('touch', filename='empty.txt')
    inner = Workflow([touch], 'inner')
    inner.outputs.made = touch.outputs.file
    workflow = Workflow([inner, _step('cat', file=inner.outputs.made)], 're_export')
    workflow.outputs.made = inner.outputs.made
    return workflow


def chained_scatter() -> Workflow:
    """`cat` reads `echo` before `echo` is scattered: refused once, because the bound type was taken before
    `scatter_on` ran."""
    echo = _step('echo', message=['a', 'b'])
    cat = _step('cat', file=echo.outputs.stdout)
    echo.scatter_on(echo.inputs.message)
    cat.scatter_on(cat.inputs.file)
    return Workflow([echo, cat], 'chained_scatter')


def scatter_in_subworkflow() -> Workflow:
    """A subworkflow output typed at bind time stayed `File` once its step was scattered, so a sibling could neither
    scatter over it nor consume it unscattered."""
    echo = _step('echo', message=['a', 'b'])
    scattered = Workflow([echo], 'scattered')
    scattered.outputs.out = echo.outputs.stdout
    cat = _step('cat', file=scattered.outputs.out)
    echo.scatter_on(echo.inputs.message)
    cat.scatter_on(cat.inputs.file)
    return Workflow([scattered, cat], 'scatter_in_subworkflow')


def step_input_fields() -> Workflow:
    """Every field of a `StepInput` but `source` is written to the step input as CWL spells it."""
    return Workflow([_step('echo', message=StepInput(default=3, value_from='$(String(self + 1))', label='n'))],
                    'step_input_fields')


def merged_scatter() -> Workflow:
    """Merged sources arrive as one list, so an input of their element type can be scattered over it."""
    a, b = _step('touch', 'a', filename='a.txt'), _step('touch', 'b', filename='b.txt')
    sink = _sink('File', x=StepInput(source=[a.outputs.file, b.outputs.file], link_merge='merge_flattened'))
    sink.scatter_on(sink.inputs.x)
    return Workflow([a, b, sink], 'merged_scatter')


def picked_all_non_null() -> Workflow:
    """`all_non_null` keeps the list the sources make, so its input can be scattered."""
    a, b = _step('touch', 'a', filename='a.txt'), _step('touch', 'b', filename='b.txt')
    sink = _sink('File', x=StepInput(source=[a.outputs.file, b.outputs.file], pick_value='all_non_null'))
    sink.scatter_on(sink.inputs.x)
    return Workflow([a, b, sink], 'picked_all_non_null')


def similar_step_names() -> Workflow:
    """A record's edges on step `sink` never take the name of a plain link to step `sink1`."""
    a, b, c = (_step('touch', name, filename=f'{name}.txt') for name in 'abc')
    sink = _sink('File[]', x=StepInput(source=[a.outputs.file, b.outputs.file], link_merge='merge_flattened'))
    return Workflow([a, b, sink, c, _sink('File', 'sink1', x=c.outputs.file)], 'similar_step_names')


def transforming_record_any() -> Workflow:
    """A workflow input read only through a `valueFrom` has no type to borrow, so it is `Any`."""
    echo = _step('echo')
    workflow = Workflow([echo], 'transforming_record_any')
    echo.inputs.message = StepInput(source=workflow.inputs.n, value_from='$(String(self))')
    return workflow
