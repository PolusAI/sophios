from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Build docs/tutorials/multistep1.wic in Python: create a file, append Hello and World!, read it.

    Python step names are unique, so the two `append` steps are named apart.
    """
    touch = Step(clt_path=ADAPTERS / "touch.cwl")
    touch.inputs.filename = "empty.txt"

    hello = Step(clt_path=ADAPTERS / "append.cwl", step_name="append_hello")
    hello.inputs.file = touch.outputs.file
    hello.inputs.str = "Hello"

    world = Step(clt_path=ADAPTERS / "append.cwl", step_name="append_world")
    world.inputs.file = hello.outputs.file
    world.inputs.str = "World!"

    cat = Step(clt_path=ADAPTERS / "cat.cwl")
    cat.inputs.file = world.outputs.file

    return Workflow([touch, hello, world, cat], "multistep1_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
