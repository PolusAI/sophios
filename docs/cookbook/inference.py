"""Inference: an input left unbound is connected by the compiler."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def workflow() -> Workflow:
    """Print a file; which file is left to inference."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    echo = Step(clt_path=ADAPTERS / 'echo.cwl')
    echo.inputs.message = 'Hello'
    cat = Step(clt_path=ADAPTERS / 'cat.cwl')
    return Workflow([touch, echo, cat], 'inference')
