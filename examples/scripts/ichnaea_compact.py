"""Canonical end-to-end example: tool_builder -> Workflow -> compute request."""

from datetime import datetime
from pathlib import Path
from typing import Dict

from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.compute_request import (
    ComputeExecutionConfig,
    ComputeOutputConfig,
    ComputeRequest,
    SlurmJobConfig,
    ToilRuntimeConfig,
)


SUBMIT_URL: str | None = None


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
        cwl.directory, from_input=inputs.output).label("Output segmentation Zarr"))

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


def workflow(input_dicts: Dict[str, str], workflow_name: str) -> Workflow:
    """Build the test workflow and return Workflow object"""
    # =========== BUILD CLT ==========================
    autoseg_clt = build_autoseg_CLT()  # directly building the CLT in memory
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


def create_compute_request(workflow_id: str, compiled_workflow: CompiledWorkflow) -> ComputeRequest:
    """Return a compute request object for the compiled workflow."""
    return ComputeRequest(
        compiled_workflow,
        workflow_id=workflow_id,
        compute_config=ComputeExecutionConfig(
            toil=ToilRuntimeConfig(log_level="INFO"),
            output=ComputeOutputConfig.workflow_declared(),
            slurm=SlurmJobConfig(partition="normal_gpu", cpus_per_task=4),
        ),
    )


def main() -> int:
    """Build the workflow, create the compute request, and optionally submit it."""
    # ========== INPUTS TO WORKFLOW ==================
    # The main directory constants
    inputs_dir = Path('/projects/collabs/mock_common/')
    input_dicts = {}
    input_dicts['input_dir'] = str(inputs_dir / 'input_directory')
    input_dicts['output_dir'] = str(inputs_dir / 'output_directory_compact')
    input_dicts['model_file'] = str(inputs_dir / 'model_directory' / 'sam3.pt')

    # ========== BUILD WORKFLOW ======================
    autoseg_workflow = workflow(input_dicts, "autoseg_workflow")
    compiled_workflow = autoseg_workflow.compile()

    # ========== COMPUTE INPUT =======================
    # workflow Name
    workflow_name = compiled_workflow.name
    # adjust workflow name/id to distinguish after submit using a timestamp
    workflow_name = workflow_name + '__' + \
        datetime.now().strftime('%Y_%m_%d_%H.%M.%S') + '__'

    # ========== CONSTRUCT COMPUTE OBJECT ============
    compute_request = create_compute_request(workflow_name, compiled_workflow)

    if SUBMIT_URL is None:
        print("Built compute request object in memory. Set SUBMIT_URL to submit it.")
        return 0

    # =========  SUBMIT TO COMPUTE ===================
    submission = compute_request.submit(SUBMIT_URL)
    return submission.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
