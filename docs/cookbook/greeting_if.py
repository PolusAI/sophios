"""A condition: cat reads the greeting back only when the workflow input ``show`` is true."""
from pathlib import Path

from sophios.api.python.workflow import Step, StepInput, Workflow


#: The tools that ship with Sophios, where `sophios --generate_config` put them.
ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'


def workflow() -> Workflow:
    """The greeting workflow, whose last step runs only when asked."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello, Sophios'
    cat = Step(clt_path=ADAPTERS / 'cat.cwl')
    cat.inputs.file = append.outputs.file
    flow = Workflow([touch, append, cat], 'greeting_if')
    cat.inputs.show = StepInput(source=flow.inputs.show.as_type('boolean'))
    cat.when = '$(inputs.show)'
    return flow


if __name__ == '__main__':
    workflow().run(inputs_file='job.yml')
