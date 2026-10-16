"""A workflow input: the value is given when the workflow runs."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Touch a file whose name is the workflow input ``filename``."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    flow = Workflow([touch], 'workflow_input')
    touch.inputs.filename = flow.inputs.filename
    return flow


if __name__ == '__main__':
    workflow().run()
