"""The greeting, given when the workflow runs: a workflow input."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


#: The tools that ship with Sophios, where `sophios --generate_config` put them.
ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'


def workflow() -> Workflow:
    """Write the workflow input ``message`` into greeting.txt, and read it back."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.file = touch.outputs.file
    cat = Step(clt_path=ADAPTERS / 'cat.cwl')
    cat.inputs.file = append.outputs.file
    flow = Workflow([touch, append, cat], 'greeting_input')
    append.inputs.str = flow.inputs.message
    return flow


if __name__ == '__main__':
    workflow().run(inputs_file='job.yml')
