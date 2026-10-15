"""A conditional step, deciding on a value its tool does not take."""
from sophios.api.python.workflow import Step, StepInput, Workflow


def workflow() -> Workflow:
    """Echo only when the workflow input ``loud`` is true."""
    echo = Step(clt_path='cwl_adapters/echo.cwl')
    echo.inputs.message = 'Hello'
    flow = Workflow([echo], 'when')
    echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type('boolean'))
    echo.when = '$(inputs.enabled)'
    return flow


if __name__ == '__main__':
    workflow().run()
