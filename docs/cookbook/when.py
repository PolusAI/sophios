"""A conditional step, deciding on a value its tool does not take."""
from pathlib import Path

from sophios.api.python.workflow import Step, StepInput, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Echo only when the workflow input ``loud`` is true."""
    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    echo.inputs.message = 'Hello'
    flow = Workflow([echo], 'when')
    echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type('boolean'))
    echo.when = '$(inputs.enabled)'
    return flow
