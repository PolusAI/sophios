"""Raw CWL: the name is written into the CWL as it is."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Echo the workflow input ``greeting``."""
    echo = Step(clt_path='cwl_adapters/echo.cwl')
    flow = Workflow([echo], 'raw_cwl')
    echo.inputs.message = flow.inputs.greeting
    return flow


if __name__ == '__main__':
    workflow().run()
