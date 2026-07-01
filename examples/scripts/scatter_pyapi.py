from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def small_workflow() -> Workflow:
    """Scatter one echo step over the selected array values."""
    array_ind = Step(clt_path=ADAPTERS / "array_indices.cwl")
    array_ind.inputs.input_array = ["hello world", "not", "what world?"]
    array_ind.inputs.input_indices = [0, 1]

    echo = Step(clt_path=ADAPTERS / "echo.cwl")
    echo.inputs.message = array_ind.outputs.output_array
    echo.scatter_on(echo.inputs.message)

    return Workflow([array_ind, echo], "scatter_pyapi_py")


def workflow() -> Workflow:
    """Scatter one step over two array-valued inputs with a cross product."""
    array_ind = Step(clt_path=ADAPTERS / "array_indices.cwl")
    array_ind.inputs.input_array = ["hello world", "not", "what world?"]
    array_ind.inputs.input_indices = [0, 2]

    echo_3 = Step(clt_path=ADAPTERS / "echo_3.cwl")
    echo_3.inputs.message1 = array_ind.outputs.output_array
    echo_3.inputs.message2 = array_ind.outputs.output_array
    echo_3.inputs.message3 = "scalar"
    echo_3.scatter_on(
        echo_3.inputs.message1,
        echo_3.inputs.message2,
        method="flat_crossproduct",
    )

    return Workflow([array_ind, echo_3], "scatter_pyapi_py")


# Do NOT .run() here

if __name__ == "__main__":
    workflow().run()
