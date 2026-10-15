"""An inline literal: the value is written into the workflow."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Touch a file whose name is given in the workflow."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'empty.txt'
    return Workflow([touch], 'inline_literal')


if __name__ == '__main__':
    workflow().run()
