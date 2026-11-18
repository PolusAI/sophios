"""A step that runs a tool written in .wic: tally_words.wic, beside the workflow."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


#: The tools that ship with Sophios, where `sophios --generate_config` put them.
ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'


def workflow() -> Workflow:
    """Create a file, append a line, and count its words with the tool tally_words.wic."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.str = 'Hello, Sophios'
    count = Step(clt_path=Path(__file__).with_name('tally_words.wic'))
    return Workflow([touch, append, count], 'tool_document')


if __name__ == '__main__':
    workflow().run()
