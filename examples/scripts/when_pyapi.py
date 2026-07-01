from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Build a workflow with a conditional step."""
    to_string = Step(clt_path=ADAPTERS / "toString.cwl")
    to_string.inputs.input = 27

    echo = Step(clt_path=ADAPTERS / "echo.cwl")
    echo.inputs.message = to_string.outputs.output

    # Alternate JavaScript syntax:
    # echo.when = '$(inputs["message"] < "27")'
    echo.when = '$(inputs.message < "27")'

    return Workflow([to_string, echo], "when_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
