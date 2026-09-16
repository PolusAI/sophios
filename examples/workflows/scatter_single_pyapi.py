from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Scatter one echo step over the selected array values.

    One scattered port, with no `method`: the compiler picks `dotproduct`.
    """
    array_ind = Step(clt_path=ADAPTERS / "array_indices.cwl")
    array_ind.inputs.input_array = ["hello world", "not", "what world?"]
    array_ind.inputs.input_indices = [0, 1]

    echo = Step(clt_path=ADAPTERS / "echo.cwl")
    echo.inputs.message = array_ind.outputs.output_array
    echo.scatter_on(echo.inputs.message)

    return Workflow([array_ind, echo], "scatter_single_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
