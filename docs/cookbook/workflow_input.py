"""A workflow input: the value is given when the workflow runs."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Touch a file whose name is the workflow input ``filename``."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    flow = Workflow([touch], 'workflow_input')
    touch.inputs.filename = flow.inputs.filename
    return flow
