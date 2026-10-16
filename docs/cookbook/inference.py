"""Inference: an input left unbound is connected by the compiler."""
from sophios.api.python.workflow import Step, Workflow


def workflow() -> Workflow:
    """Print a file; which file is left to inference."""
    touch = Step(clt_path='cwl_adapters/touch.cwl')
    touch.inputs.filename = 'empty.txt'
    echo = Step(clt_path='cwl_adapters/echo.cwl')
    echo.inputs.message = 'Hello'
    cat = Step(clt_path='cwl_adapters/cat.cwl')
    return Workflow([touch, echo, cat], 'inference')


if __name__ == '__main__':
    workflow().run()
