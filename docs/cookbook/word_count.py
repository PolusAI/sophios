"""A step that names its tool by path: a .cwl file beside the workflow."""
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Create a file, append a line, and count its words with word_count.cwl."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path='cwl_adapters/append.cwl')
    append.inputs.str = 'Hello, Sophios'
    count = Step(clt_path=Path(__file__).with_name('word_count.cwl'))
    return Workflow([touch, append, count], 'word_count')


if __name__ == '__main__':
    workflow().run()
