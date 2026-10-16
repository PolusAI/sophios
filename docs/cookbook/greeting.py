"""Create a file, append a line to it, and print it."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """The same three steps as greeting.wic."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path='cwl_adapters/append.cwl')
    append.inputs.str = 'Hello, Sophios'
    cat = Step(clt_path='cwl_adapters/cat.cwl')
    return Workflow([touch, append, cat], 'greeting')


if __name__ == '__main__':
    workflow().run()
