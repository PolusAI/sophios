"""Canonical end-to-end example: tool_builder -> Workflow -> the compute service.

The outputs are copied into a folder on the cluster, the one `RunConfig` is made with (the output mode
`workflowOutDir`): the supported way.
"""

import json
import sys

from sophios.api.python.workflow import RunState, Step, Workflow

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.compute import RunConfig, Service, build_request

#: The compute service: the host it runs on, over your own ssh, such as 'ssh://login.cluster.example.org'. None
#: only builds and prints the request.
SERVICE_URL: str | None = None
#: The directory on the cluster the paths below are relative to.
BASEPATH = '/projects/collabs/mock_common'


def build_autoseg_CLT() -> CommandLineTool:
    """Create the SAM2.1 autosegmentation CLT, the ichnaea segmentation plugin's, using the declarative API."""
    inputs = Inputs(
        input=Input(cwl.directory, flag="--inpDir", position=1).label(
            "Input OME-Zarr").doc("The OME-Zarr volume: its root, which holds .zattrs"),
        output=Input(cwl.directory, flag="--outDir", position=2).label("Output segmentation OME-Zarr").doc(
            "An empty folder; filled with a copy of the input whose level 0 holds the labels"
        ),
        model=Input(cwl.directory)
        .label("Models folder")
        .doc("Staged as ./models, from which the plugin loads sam2.1_hiera_small.pt"),
    )
    outputs = Outputs(output=Output(
        cwl.directory, glob="$(inputs.output.basename)").label("Output segmentation OME-Zarr"))

    return (
        CommandLineTool("ichnaea_segmentation", inputs, outputs)
        .describe(
            "ichnaea segmentation",
            "SAM2.1 autosegmentation of an OME-Zarr volume, one Z slice at a time.\n",
        )
        .edam()
        # One device: it runs on torch.device("cuda"); its torch wheels are built for CUDA 12.6.
        .gpu(cuda_version_min="12.6", compute_capability="3.0", device_count_min=1)
        .docker("vjaganat90/ichnaea-segmentation-plugin:devn")
        .stage(inputs.output, writable=True)
        # It opens models/sam2.1_hiera_small.pt relative to its working directory, the job's output folder.
        .stage(inputs.model, entryname="models")
        .resources(cores=4, ram=64000)
        # No base command: the image's ENTRYPOINT, python3 -m ichnaea.segmentation, is the command. Docker and
        # `singularity run` put it in front of these options, and CWL cannot override it, so this tool relies on it
        # (see docs/running.md, Images that rely on an entrypoint).
    )


def workflow(input_dicts: dict[str, str], workflow_name: str) -> Workflow:
    """Build the test workflow and return Workflow object"""
    # =========== BUILD CLT ==========================
    autoseg_clt = build_autoseg_CLT()  # directly building the CLT in memory
    # =========== CREATE A STEP ======================
    # build a step from the defined CLT
    autoseg = Step(autoseg_clt, step_name='autoseg')
    # assign input values to the step
    autoseg.output = input_dicts['output_dir']
    autoseg.input = input_dicts['input_dir']
    autoseg.model = input_dicts['model_dir']
    # =========== CREATE A SOPHIOS WORKFLOW ==========
    # arrange steps
    steps = [autoseg]
    # create workflow from steps DAG
    wkflw = Workflow(steps, workflow_name)
    # =========== RETURN SOPHIOS WORKFLOW OBJECT =====
    return wkflw


def main() -> int:
    """Build the workflow and its request; run it on the compute service when SERVICE_URL is set."""
    # ========== INPUTS TO WORKFLOW ==================
    # What the tool reads: paths on the cluster, relative to BASEPATH, as written; Sophios never looks at them.
    # The input is the OME-Zarr volume's root; the models folder holds sam2.1_hiera_small.pt.
    # The output folder is just a name: the service makes it in the run, and copies it into the RunConfig's folder
    # under that name.
    input_dicts = {'input_dir': 'input_directory', 'output_dir': 'segmentation_compact',
                   'model_dir': 'model_directory'}

    # ========== BUILD WORKFLOW ======================
    autoseg_workflow = workflow(input_dicts, "autoseg_workflow")
    # The outputs are copied into results_compact: the output folder lands in results_compact/segmentation_compact.
    config = (RunConfig(f'{BASEPATH}/results_compact')
              .basepath(BASEPATH)
              .steps(partition='normal', gpu_partition='normal_gpu'))

    if SERVICE_URL is None:
        request = build_request(autoseg_workflow, config)
        print(json.dumps(request, indent=4))
        print("Set SERVICE_URL to run it.")
        return 0

    # =========  RUN ON THE COMPUTE SERVICE ===========
    run = Service(SERVICE_URL).submit(autoseg_workflow, config)
    if run.wait().state is not RunState.COMPLETED:
        print(run.logs(), file=sys.stderr)  # Toil's log of the whole run
        return 1
    print(f'{run.id} COMPLETED: {run.outputs()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
