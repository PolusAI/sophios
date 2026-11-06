"""The greeting graph with only the edges at the branch and at the join written."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


#: The tools that ship with Sophios, where `sophios --generate_config` put them.
ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'


def workflow() -> Workflow:
    """The steps of greeting_graph.py; `word_count` and `combine` say which file they read, inference does the rest."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.str = 'Hello, Sophios'
    shout = Step(clt_path=Path(__file__).with_name('shout.cwl'))
    word_count = Step(clt_path=Path(__file__).with_name('word_count.cwl'))
    word_count.inputs.text = append.outputs.file
    combine = Step(clt_path=Path(__file__).with_name('combine.cwl'))
    combine.inputs.text = shout.outputs.shouted
    return Workflow([touch, append, shout, word_count, combine], 'greeting_graph_partial')


if __name__ == '__main__':
    workflow().run()
