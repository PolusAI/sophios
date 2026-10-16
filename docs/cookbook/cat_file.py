"""A File literal: the workflow names a file that exists before it runs."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Print data.txt, read from the working directory."""
    cat = Step(clt_path='cwl_adapters/cat.cwl')
    cat.inputs.file = {'class': 'File', 'location': 'data.txt'}
    return Workflow([cat], 'cat_file')


if __name__ == '__main__':
    workflow().run()
