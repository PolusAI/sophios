"""A named workflow output."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Echo a greeting and name the result ``greeting``."""
    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    echo.inputs.message = 'Hello'
    flow = Workflow([echo], 'named_output')
    flow.outputs.greeting = echo.outputs.stdout
    return flow
