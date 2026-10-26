"""Artifact-first variant of the ichnaea example, writing its output folder in place on the cluster.

`workflow_in_place` is the compute service's advanced choice: the tool writes straight into the folder its `output`
input names, with no copy, and the service warns of what that asks of every tool. `workflow_out_dir`, as in
`ichnaea_compact.py`, is the supported way.
"""

import json
from pathlib import Path

from sophios.api.python.workflow import Step, Workflow

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.compute import Service, build_request

#: The compute service: the local end of an SSH tunnel to it, such as 'http://127.0.0.1:7998'. None only writes
#: the request.
SERVICE_URL: str | None = None
#: The directory on the cluster the paths below are relative to.
BASEPATH = '/projects/collabs/mock_common'


def build_autoseg_CLT() -> CommandLineTool:
    """Create the SAM3 autosegmentation CLT using the declarative API."""
    inputs = Inputs(
        input=Input(cwl.directory, position=1).label(
            "Input Zarr dataset").doc("Path to input zarr dataset"),
        output=Input(cwl.directory, position=2).label("Output segmentation Zarr").doc(
            "Path for output segmentation zarr"
        ),
        model=Input(cwl.file, flag="--model", required=False)
        .label("Model override file")
        .doc("Path containing sam3.pt to override baked-in models/sam3"),
        tile_size=Input(cwl.int, flag="--tile-size", required=False)
        .label("Tile size")
        .doc("Tile size for large slices (default 1024)"),
        overlap=Input(cwl.int, flag="--overlap", required=False)
        .label("Tile overlap")
        .doc("Overlap between adjacent tiles in pixels (default 128)"),
        iou_threshold=Input(cwl.float, flag="--iou-threshold", required=False)
        .label("IoU threshold")
        .doc("IoU threshold for matching labels across tiles (default 0.5)"),
        batch_size=Input(cwl.int, flag="--batch-size", required=False)
        .label("Batch size")
        .doc("Number of tiles per GPU forward pass (default 8)"),
        lora_weights=Input(cwl.file, flag="--lora-weights", required=False)
        .label("LoRA weights")
        .doc("Path to LoRA adapter weights (.pt file) - optional"),
        lora_rank=Input(cwl.int, flag="--lora-rank", required=False)
        .label("LoRA rank")
        .doc("LoRA rank used when lora_weights is set (default 16)"),
        lora_alpha=Input(cwl.int, flag="--lora-alpha", required=False)
        .label("LoRA alpha")
        .doc("LoRA alpha scaling factor used when lora_weights is set (default 32)"),
    )
    outputs = Outputs(output=Output(
        cwl.directory, glob="$(inputs.output.basename)").label("Output segmentation Zarr"))

    return (
        CommandLineTool("sam3_ome_zarr_autosegmentation", inputs, outputs)
        .describe(
            "SAM3 OME Zarr autosegmentation",
            "Run SAM3 autosegmentation on a zarr volume.\n",
        )
        .edam()
        .gpu(cuda_version_min="11.7", compute_capability="3.0", device_count_min=2)
        .docker("polusai/ichnaea-api:latest")
        .stage(inputs.output, writable=True)
        .stage(inputs.input)
        .resources(cores=4, ram=64000)
        .base_command(
            "/backend/.venv/bin/python",
            "/backend/dagster_pipelines/jobs/autosegmentation/logic.py",
        )
    )


def workflow(input_dicts: dict[str, str], workflow_name: str) -> Workflow:
    """Build the test workflow"""
    # =========== BUILD CLT ==========================
    autoseg_clt = build_autoseg_CLT()  # directly building the CLT in memory
    # write validated built CWL CLT to disk for verifiability (optional)
    autoseg_clt.write_cwl(Path(__file__).with_name(
        'built-ichnaea-autosegmentation.cwl'), validate=True)
    # =========== CREATE A STEP ======================
    # build a step from the defined CLT
    autoseg = Step(autoseg_clt, step_name='autoseg')
    # assign input values to the step
    autoseg.output = input_dicts['output_dir']
    autoseg.input = input_dicts['input_dir']
    autoseg.model = input_dicts['model_file']
    # =========== CREATE A SOPHIOS WORKFLOW ==========
    # arrange steps
    steps = [autoseg]
    # create workflow from steps DAG
    wkflw = Workflow(steps, workflow_name)
    # =========== RETURN SOPHIOS WORKFLOW OBJECT =====
    return wkflw


def main() -> int:
    """Write the built tool, the compiled workflow and its request to disk; run it when SERVICE_URL is set."""
    # ========== INPUTS TO WORKFLOW ==================
    # Paths on the cluster, relative to BASEPATH, as written: Sophios never looks at them.
    input_dicts = {'input_dir': 'input_directory', 'output_dir': 'output_directory3',
                   'model_file': 'model_directory/sam3.pt'}

    # ========== BUILD WORKFLOW ======================
    autoseg_workflow = workflow(input_dicts, "autoseg_workflow")
    # write compiled workflow artifacts to autogenerated/ for verifiability (optional)
    compiled_workflow = autoseg_workflow.compile()
    compiled_workflow.write("autogenerated")
    steps = {'partition': 'normal', 'gpuPartition': 'normal_gpu'}

    # ========== WRITE THE REQUEST ====================
    request = build_request(autoseg_workflow, basepath=BASEPATH, workflow_in_place=True, steps=steps)
    output_path = Path(f'compute_{request["id"]}_integrated.json')
    output_path.write_text(json.dumps(request, indent=4, sort_keys=True), encoding='utf-8')

    if SERVICE_URL is None:
        print(
            "Wrote built-ichnaea-autosegmentation.cwl, compiled workflow artifacts under autogenerated/, "
            f"and {output_path}. Set SERVICE_URL to run it."
        )
        return 0

    # =========  RUN ON THE COMPUTE SERVICE ===========
    run = Service(SERVICE_URL).run(autoseg_workflow, basepath=BASEPATH, workflow_in_place=True, steps=steps)
    print(f'{run.id} COMPLETED; its output folders: {run.targets}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
