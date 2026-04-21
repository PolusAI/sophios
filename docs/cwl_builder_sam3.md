# Building a CWL CommandLineTool in Python

This walkthrough shows how to build a real CWL `CommandLineTool` using
`sophios.apis.python.cwl_builder`.

The design goal is simple:

- the required structure of the tool should be obvious at a glance,
- input and output names should come from Python names rather than raw string keys,
- and optional CWL details should feel like optional add-ons, not required boilerplate.

The full working example lives in
[examples/scripts/sam3_cwl_builder.py](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/sam3_cwl_builder.py).

## The core idea

There are only three required pieces:

1. a tool name,
2. an `Inputs(...)` collection,
3. an `Outputs(...)` collection.

That means the basic shape always looks like this:

```python
inputs = Inputs(
    input=Input(cwl.directory, position=1),
    output=Input(cwl.directory, position=2),
)

outputs = Outputs(
    output=Output(cwl.directory, from_input=inputs.output),
)

tool = CommandLineTool("example", inputs, outputs)
```

Everything else is optional and chainable:

```python
tool = (
    CommandLineTool("example", inputs, outputs)
    .base_command("python", "main.py")
    .docker("python:3.12")
    .resources(cores=2, ram=4096)
)
```

That split is intentional. The constructor shows the tool contract. The chained calls describe the runtime and metadata details around that contract.

## Why this is easier to read

The old builder style asked you to mentally assemble the CLT while reading a long chain.

The new style makes the shape visible immediately:

- `Inputs(...)` gives names to inputs using Python keywords,
- `Outputs(...)` gives names to outputs the same way,
- `CommandLineTool(...)` requires those named collections up front.

That helps non-experts because the code now reads more like:

"this tool has these inputs and these outputs"

and less like:

"start a builder, keep chaining methods, and hope the required bits showed up somewhere in the middle."

## Named inputs without raw string keys

One of the important design constraints is that users should not have to write raw string names for input and output definitions.

So instead of:

```python
inputs = {
    "input": ...,
    "output": ...,
}
```

you write:

```python
inputs = Inputs(
    input=Input(cwl.directory, position=1),
    output=Input(cwl.directory, position=2),
)
```

Those Python keyword names become the CWL parameter names.

The same thing applies to outputs:

```python
outputs = Outputs(
    output=Output(cwl.directory, from_input=inputs.output),
)
```

Notice that `from_input=inputs.output` uses a real named input reference, not a raw string like `"output"`.

The other important convention is that CWL types live under the `cwl` namespace:

```python
cwl.int
cwl.float
cwl.file
cwl.directory
```

That keeps CWL vocabulary visually separate from Python builtins and makes intent easier to scan.

## How to think about inputs

Each input answers two questions:

1. what type of thing is this?
2. how does the underlying application receive it?

Examples:

```python
Input(cwl.directory, position=1)
Input(cwl.file, flag="--model", required=False)
Input(cwl.int, flag="--tile-size", required=False)
```

These read very close to application intent:

- positional directory argument,
- optional file passed with `--model`,
- optional integer passed with `--tile-size`.

Optional metadata can then be chained:

```python
Input(cwl.file, flag="--model", required=False).label("Model override file").doc("Path to sam3.pt")
```

That is the intended use of chaining in this API: optional polish on top of a complete required core.

## How to think about outputs

Outputs follow the same pattern:

```python
Output(cwl.directory, from_input=inputs.output)
Output(cwl.file, glob="results.json")
Output.stdout()
```

Again, the goal is to describe what the output means, not to hand-assemble `outputBinding` YAML.

## The SAM3 example

```python
from pathlib import Path

from sophios.apis.python.cwl_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl


inputs = Inputs(
    input=Input(cwl.directory, position=1).label("Input Zarr dataset").doc("Path to input zarr dataset"),
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

outputs = Outputs(
    output=Output(cwl.directory, from_input=inputs.output).label("Output segmentation Zarr"),
)

tool = (
    CommandLineTool("sam3_ome_zarr_autosegmentation", inputs, outputs)
    .describe(
        "SAM3 OME Zarr autosegmentation",
        "Run SAM3 autosegmentation on a zarr volume.\n"
        "Models are baked into the container image at models/sam3, "
        "so no model staging is required.",
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

output_path = Path("sam3_ome_zarr_autosegmentation.cwl")
tool.save(output_path, validate=True)
```

## What the builder is hiding for you

This API is supposed to absorb the repetitive CWL details that regularly trip people up:

- nullable unions for optional values,
- `inputBinding.prefix` vs `inputBinding.position`,
- `outputBinding.glob` expressions derived from input names,
- `InitialWorkDirRequirement` entries for staged inputs,
- `InlineJavascriptRequirement` when helper-generated expressions are present,
- namespaced hints such as `cwltool:CUDARequirement`.

That means most users only need to think about:

- what command runs,
- what the inputs are,
- what the outputs are,
- and which optional runtime constraints apply.

## Sane defaults

For most tools, the happy path is:

```python
CommandLineTool(name, inputs, outputs).base_command(...).docker(...).resources(...)
```

Everything else is optional.

In particular:

- `label` is optional,
- `doc` is optional,
- namespaces and schemas are optional,
- `InlineJavascriptRequirement` is added automatically when helper-generated expressions are present,
- resource requirements are optional,
- EDAM metadata is optional and available through `.edam()`.

## Why you can trust the result

There are two separate sources of confidence.

### 1. The API narrows the common error surface

The builder gives you named operations rather than raw nested dictionaries, which means fewer routine mistakes:

- malformed optional types,
- incorrect binding placement,
- incorrect output glob expressions,
- missing requirement wrappers,
- missing namespace setup for common hints.

### 2. Validation is built in

When you call:

```python
tool.save(output_path, validate=True)
```

or:

```python
tool.validate()
```

the generated CLT is validated through the `cwltool` and schema-salad stack.

That is a much stronger guarantee than "this happened to produce YAML". It means the generated document has gone through the same validation path users already trust.

## Escape hatches

The main API is intentionally structured, but escape hatches still exist for advanced cases:

- `requirement(...)`
- `hint(...)`
- `argument(...)`
- `extra(...)`

Those are for the unusual edges of CWL. They should be the exception, not the starting point.

## Using a built CLT in the workflow DSL

The CLT builder can also hand off directly to the workflow Python API without writing a `.cwl` file:

```python
tool = CommandLineTool(
    "echo_tool",
    Inputs(message=Input(cwl.string, position=1)),
    Outputs(out=Output.stdout()),
).stdout("stdout.txt")

step = tool.to_step(step_name="say_hello")
step.inputs.message = "hello"

workflow = Workflow([step], "wf")
```

That bridge stays intentionally small:

- the builder still only knows how to render a CLT,
- the workflow API still only knows how to work with a parsed CLT document,
- and the in-memory handoff is handled through a tiny adapter layer.

## Run the example

From the repository root:

```bash
PYTHONPATH=src python examples/scripts/sam3_cwl_builder.py
PYTHONPATH=src python examples/scripts/sam3_cwl_builder.py --validate
```

The first command writes the CLT. The second also validates it.

Validation requires `cwltool` and schema-salad to be installed in your Python environment.
