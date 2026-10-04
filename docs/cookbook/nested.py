"""A nested workflow, and a later step that reads its output."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

ADAPTERS = Path(__file__).resolve().parents[2] / 'cwl_adapters'


def make_greeting() -> Workflow:
    """Make a file that holds a greeting."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'
    child = Workflow([touch, append], 'make_greeting')
    child.outputs.greeting = append.outputs.file
    return child


def workflow() -> Workflow:
    """Print the greeting the nested workflow made."""
    child = make_greeting()
    cat = Step(clt_path=ADAPTERS / 'cat.cwl')
    cat.inputs.file = child.outputs.greeting
    return Workflow([child, cat], 'nested')
