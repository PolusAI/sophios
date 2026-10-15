"""An explicit edge: one step's output feeds a later step's input."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Touch a file, then append a line to that file."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'hello.txt'
    append = Step(clt_path='cwl_adapters/append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'
    return Workflow([touch, append], 'explicit_edge')


if __name__ == '__main__':
    workflow().run()
