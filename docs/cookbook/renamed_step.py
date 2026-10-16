"""Renamed steps: one tool used twice, each step under its own name."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Run ``echo.cwl`` twice, as the steps ``hello`` and ``goodbye``."""
    hello = Step(clt_path='cwl_adapters/echo.cwl', step_name='hello')
    hello.inputs.message = 'Hello'
    goodbye = Step(clt_path='cwl_adapters/echo.cwl', step_name='goodbye')
    goodbye.inputs.message = 'Goodbye'
    return Workflow([hello, goodbye], 'renamed_step')


if __name__ == '__main__':
    workflow().run()
