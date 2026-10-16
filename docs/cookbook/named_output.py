"""A named workflow output."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Echo a greeting and name the result ``greeting``."""
    echo = Step(clt_path='cwl_adapters/echo.cwl')
    echo.inputs.message = 'Hello'
    flow = Workflow([echo], 'named_output')
    flow.outputs.greeting = echo.outputs.stdout
    return flow


if __name__ == '__main__':
    workflow().run()
