# From Python Workflow to Compute Request

Sophios provides a clean path from Python-authored CWL to a schema-validated
compute submission request.

The layers stay separate:

1. `tool_builder` defines a CWL `CommandLineTool`.
2. `workflow` composes steps into a `Workflow([steps], name)` DAG.
3. `compute_request` packages compiled CWL for remote execution.
4. `ComputeRequest.submit(...)` sends the validated request JSON to the
   compute service.

You do not need to hand-build JSON, and you do not need to write an
intermediate `.cwl` file just to produce a request body.

A runnable version of this pattern lives in
[examples/scripts/compute_request_workflow.py](https://github.com/PolusAI/sophios/blob/main/examples/scripts/compute_request_workflow.py).

For a larger example that starts from the Ichnaea autosegmentation CLT and
carries that tool through workflow construction and compute submission, see
[ichnaea_compact_compute](ichnaea_compact_compute.md).

## What This Pattern Gives You

This split gives you clear checkpoints:

- `CommandLineTool(...)` keeps tool authoring structured and readable.
- `Workflow([steps], name)` keeps DAG wiring explicit and reviewable.
- `workflow.compile()` returns a `CompiledWorkflow` boundary object.
- `ComputeRequest(compiled)` packages the compiled workflow for compute.

Schema validation catches request-shape mistakes before submission. The schema
lives at
[`src/sophios/compute_request_schema.json`](https://github.com/PolusAI/sophios/blob/main/src/sophios/compute_request_schema.json).

## Full Example

```python
from datetime import datetime

from sophios.api.python.tool_builder import (
    CommandLineTool,
    Input,
    Inputs,
    Output,
    Outputs,
    cwl,
)
from sophios.api.python.workflow import Step, Workflow
from sophios.compute_request import ComputeRequest


def build_emit_text_tool() -> CommandLineTool:
    inputs = Inputs(
        message=Input(cwl.string, position=1)
        .label("Message")
        .doc("Text to print to stdout."),
    )
    outputs = Outputs(
        text_file=Output(cwl.file, glob="stdout.txt")
        .label("Captured stdout")
        .doc("Text emitted by the tool, captured as a file."),
    )
    return (
        CommandLineTool("emit_text", inputs, outputs)
        .describe("Emit text", "Generated CLT that prints one message.")
        .base_command("python", "-c")
        .argument("import sys; print(sys.argv[1])", position=0)
        .stdout("stdout.txt")
    )


def build_workflow(message: str) -> Workflow:
    emit_step = Step(build_emit_text_tool(), step_name="emit_text")
    workflow = Workflow([emit_step], "compute_request_workflow_demo")
    emit_step.inputs.message = message
    workflow.outputs.text_file = emit_step.outputs.text_file
    return workflow


workflow = build_workflow("hello from compute")
compiled = workflow.compile()

request = ComputeRequest(
    compiled,
    workflow_id=f"{compiled.name}__{datetime.now():%Y_%m_%d_%H.%M.%S}__",
)

request_json = request.to_json()
```

## Workflow Boundary

`workflow.compile()` returns a `CompiledWorkflow` object with named
attributes:

- `name`
- `cwl_workflow`
- `cwl_job_inputs`

That object is the public workflow-to-compute handoff. The lower-level
`CompilerInfo` tree is internal and remains available to Sophios internals via
`workflow._compile()`.

## Compute Boundary

The compute API is request-oriented:

```python
request = ComputeRequest(compiled)
request_json = request.to_json()
```

The request constructor takes the compiled workflow boundary object, plus
optional compute-specific execution settings. It does not expose a parallel
schema-shaped constructor for raw CWL dictionaries.

That keeps the compute layer loosely coupled to the workflow API. It does not
need to know how the workflow was authored.

## Optional Compute Configuration

Most workflows only need the default request shape. When you need service or
scheduler settings, add a `ComputeExecutionConfig`:

```python
from sophios.compute_request import (
    ComputeExecutionConfig,
    ComputeOutputConfig,
    ComputeRequest,
    SlurmJobConfig,
    ToilRuntimeConfig,
)

request = ComputeRequest(
    compiled,
    workflow_id="demo_job",
    compute_config=ComputeExecutionConfig(
        toil=ToilRuntimeConfig(log_level="INFO"),
        output=ComputeOutputConfig.user_specified("/tmp/compute-demo-out"),
        slurm=SlurmJobConfig(partition="normal_gpu", cpus_per_task=4),
    ),
)
```

Compute-specific concerns live in the compute request layer, not in
`Workflow(...)` and not in `CommandLineTool(...)`.

## Submission

Submission is intentionally a separate concern:

```python
submission = request.submit("http://127.0.0.1:7998/compute/")
retval = submission.exit_code
```

Submission behavior is narrow:

- render and send validated request JSON text from `request.to_json()`
- use the request id for status polling
- poll `/status/` until the job reaches a started or terminal state
- fetch logs only after the job reaches `RUNNING`
- return structured submission state

## Run the Example

From the repository root:

```bash
python examples/scripts/compute_request_workflow.py
```

The script validates the generated CLT and writes a compute request JSON file by
default. To submit the request, set `SUBMIT_URL` near the top of the script.
