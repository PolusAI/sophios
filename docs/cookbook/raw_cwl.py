"""Raw CWL: the name is written into the CWL as it is."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Echo the workflow input ``greeting``."""
    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    flow = Workflow([echo], 'raw_cwl')
    echo.inputs.message = flow.inputs.greeting
    return flow
