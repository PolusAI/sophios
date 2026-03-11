from pathlib import Path
import json
from sophios.apis.python.api import Step, Workflow


def workflow() -> Workflow:
    # step echo
    touch = Step(clt_path='../../cwl_adapters/touch.cwl')
    touch.filename = 'empty.txt'
    append = Step(clt_path='../../cwl_adapters/append.cwl')
    append.file = touch.file
    append.str = 'Hello'
    cat = Step(clt_path='../../cwl_adapters/cat.cwl')
    cat.file = append.file
    # arrange steps
    steps = [touch, append, cat]

    # create workflow
    filename = 'multistep1_toJson_pyapi_py'
    wkflw = Workflow(steps, filename)
    return wkflw

# Do NOT .run() here


if __name__ == '__main__':
    multistep1 = workflow()
    workflow_json = multistep1.get_cwl_workflow()  # .run() here inside main
    fname = 'workflow.json'
    paren_dir = Path(__file__).parent
    with open(paren_dir / 'ground_truth_multistep1.json', 'r', encoding='utf-8') as file:
        ground_truth = json.load(file)
    assert ground_truth == workflow_json
