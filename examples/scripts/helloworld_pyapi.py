from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Build the smallest useful Sophios workflow."""
    echo = Step(clt_path=ADAPTERS / "echo.cwl")
    echo.inputs.message = "hello world"

    return Workflow([echo], "helloworld_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
