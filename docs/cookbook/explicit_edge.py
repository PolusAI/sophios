"""An explicit edge: one step's output feeds a later step's input."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Touch a file, then append a line to that file."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'hello.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'
    return Workflow([touch, append], 'explicit_edge')
