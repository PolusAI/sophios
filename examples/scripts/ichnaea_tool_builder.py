"""Build a CWL CommandLineTool for the ichnaea segmentation plugin: SAM2.1 OME Zarr autosegmentation."""

from pathlib import Path

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl


OUTPUT_PATH = Path(__file__).with_name("ichnaea_segmentation.cwl")
VALIDATE = True


def build_tool() -> CommandLineTool:
    """Create the SAM2.1 autosegmentation CLT using the declarative API."""
    inputs = Inputs(
        input=Input(cwl.directory, flag="--inpDir", position=1).label("Input OME-Zarr").doc(
            "The OME-Zarr volume: its root, which holds .zattrs"
        ),
        output=Input(cwl.directory, flag="--outDir", position=2).label("Output segmentation OME-Zarr").doc(
            "An empty folder; filled with a copy of the input whose level 0 holds the labels"
        ),
        model=Input(cwl.directory)
        .label("Models folder")
        .doc("Staged as ./models, from which the plugin loads sam2.1_hiera_small.pt"),
    )
    outputs = Outputs(output=Output(cwl.directory, glob="$(inputs.output.basename)").label(
        "Output segmentation OME-Zarr"))

    return (
        CommandLineTool("ichnaea_segmentation", inputs, outputs)
        .describe(
            "ichnaea segmentation",
            "SAM2.1 autosegmentation of an OME-Zarr volume, one Z slice at a time.\n"
            "The image loads models/sam2.1_hiera_small.pt from its working directory, "
            "so the models folder is staged there as models.",
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


def main() -> int:
    """Write the generated CLT to disk and optionally validate it."""
    output_path = build_tool().write_cwl(OUTPUT_PATH, validate=VALIDATE)
    print(f"Wrote {output_path}")
    if VALIDATE:
        print("Validation succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
