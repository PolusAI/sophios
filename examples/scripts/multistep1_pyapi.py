from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Build a three-step workflow that creates, appends, and reads a file."""
    touch = Step(clt_path=ADAPTERS / "touch.cwl")
    touch.inputs.filename = "empty.txt"

    append = Step(clt_path=ADAPTERS / "append.cwl")
    append.inputs.file = touch.outputs.file
    append.inputs.str = "Hello"

    cat = Step(clt_path=ADAPTERS / "cat.cwl")
    cat.inputs.file = append.outputs.file

    return Workflow([touch, append, cat], "multistep1_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
