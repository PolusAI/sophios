"""A step-input record: two sources merged into one array input."""
from sophios.api.python.workflow import Step, StepInput, Workflow


CONCAT = {
    'class': 'CommandLineTool',
    'cwlVersion': 'v1.2',
    'baseCommand': 'cat',
    'inputs': {'files': {'type': 'File[]', 'inputBinding': {'position': 1}}},
    'outputs': {'joined': {'type': 'File', 'outputBinding': {'glob': 'joined.txt'}}},
    'stdout': 'joined.txt',
}


def workflow() -> Workflow:
    """Concatenate the outputs of two steps, given to one ``File[]`` input."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'empty.txt'
    echo = Step(clt_path='cwl_adapters/echo.cwl')
    echo.inputs.message = 'Hello'
    concat = Step.from_cwl_document(CONCAT, process_name='concat')
    concat.inputs.files = StepInput(source=[touch.outputs.file, echo.outputs.stdout], link_merge='merge_flattened')
    return Workflow([touch, echo, concat], 'record')


if __name__ == '__main__':
    workflow().run()
