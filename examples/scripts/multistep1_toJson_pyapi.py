import json
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = REPO_ROOT / "cwl_adapters"


def workflow() -> Workflow:
    """Build a workflow and expose it as a compiled CWL JSON object."""
    touch = Step(clt_path=ADAPTERS / "touch.cwl")
    touch.inputs.filename = "empty.txt"

    append = Step(clt_path=ADAPTERS / "append.cwl")
    append.inputs.file = touch.outputs.file
    append.inputs.str = "Hello"

    cat = Step(clt_path=ADAPTERS / "cat.cwl")
    cat.inputs.file = append.outputs.file

    return Workflow([touch, append, cat], "multistep1_toJson_pyapi_py")


if __name__ == "__main__":
    multistep1 = workflow()
    compiled = multistep1.compile()
    example_dir = Path(__file__).parent
    with open(example_dir / "ground_truth_multistep1.json", "r", encoding="utf-8") as file:
        ground_truth = json.load(file)
    expected_workflow = {key: value for key, value in ground_truth.items() if key not in {"name", "yaml_inputs"}}
    assert ground_truth["name"] == compiled.name
    assert ground_truth["yaml_inputs"] == compiled.cwl_job_inputs
    assert expected_workflow == compiled.cwl_workflow
