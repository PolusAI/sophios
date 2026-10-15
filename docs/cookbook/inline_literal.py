"""An inline literal: the value is written into the workflow."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Touch a file whose name is given in the workflow."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    return Workflow([touch], 'inline_literal')
