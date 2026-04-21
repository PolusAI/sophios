# From Python Workflow to Compute Payload

Sophios now has a clean path from Python-authored CWL all the way to a
schema-validated compute-slurm submission payload.

The key idea is simple:

1. build a CWL tool in Python,
2. compose it into a `Workflow`,
3. ask the workflow API for the compiled CWL and job inputs in memory,
4. wrap that compiled result in `ComputeWorkflowPayload`,
5. submit it when you are ready.

You do not need to hand-build JSON.
You do not need to write an intermediate `.cwl` file just to produce the
compute request body.

A runnable version of this pattern lives in
[examples/scripts/compute_payload_workflow.py](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/compute_payload_workflow.py).

If you want the canonical production-like example that starts from the Ichnaea
autosegmentation CLT and carries that tool all the way through workflow
construction and compute submission, see [ichnaea_compact_compute](ichnaea_compact_compute.md).

## What this buys you

This split gives you confidence at the right boundaries:

- `CommandLineTool(...)` keeps tool authoring structured and readable.
- `Workflow(...)` keeps step wiring explicit and reviewable.
- `Workflow.get_cwl_workflow()` gives you the exact compiled workflow plus job inputs.
- `ComputeWorkflowPayload.get_compute_payload()` validates that request against the checked-in compute schema.

That last point matters. The payload is not just "some JSON that happens to look right". It is checked against [`src/sophios/compute_payload_schema.json`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/src/sophios/compute_payload_schema.json) before you submit it.

## Minimal mental model

Think in terms of layers:

- `cwl_builder` defines a single CWL tool
- the workflow Python API composes tools into a CWL workflow
- `ComputeWorkflowPayload` packages that compiled workflow for compute-slurm

Each layer owns one job.
That keeps the implementation understandable and the user-facing API small.

## Full example

```python
from datetime import datetime

from sophios.apis.python import (
    CommandLineTool,
    Input,
    Inputs,
    Output,
    Outputs,
    Workflow,
    cwl,
)
from sophios.compute_payload import (
    ComputeConfig,
    ComputeWorkflowPayload,
    OutputConfig,
    SlurmConfig,
    ToilConfig,
)


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
        .describe("Emit text", "Small generated CLT that prints one message.")
        .base_command("python", "-c")
        .argument("import sys; print(sys.argv[1])", position=0)
        .stdout("stdout.txt")
    )


def build_workflow(message: str) -> Workflow:
    emit_step = build_emit_text_tool().to_step(step_name="emit_text")

    workflow = Workflow([emit_step], "compute_payload_workflow_demo")
    emit_step.inputs.message = message
    workflow.add_output("text_file", emit_step.outputs.text_file)
    return workflow


workflow = build_workflow("hello from compute")
compiled = workflow.get_cwl_workflow()
cwl_workflow = {key: value for key, value in compiled.items() if key not in {"name", "yaml_inputs"}}
cwl_job_inputs = dict(compiled["yaml_inputs"])

payload = ComputeWorkflowPayload(
    cwl_workflow=cwl_workflow,
    cwl_job_inputs=cwl_job_inputs,
    workflow_id=f"{workflow.process_name}__{datetime.now():%Y_%m_%d_%H.%M.%S}__",
)

compute_json = payload.get_compute_payload()
```

## Why this shape is useful

There are three design choices here that are worth keeping in mind.

### 1. The workflow stays in memory

`workflow.get_cwl_workflow()` returns a plain Python object with:

- the compiled CWL workflow document
- the generated `yaml_inputs` payload

That is exactly what compute-slurm needs.

So instead of rebuilding the request manually, you split the compiled object once
at the boundary and hand the two pieces to `ComputeWorkflowPayload`.

### 2. The payload object is intentionally small

The core constructor only needs:

- `cwl_workflow`
- `cwl_job_inputs`
- optionally `workflow_id`

That keeps the compute layer loosely coupled to the Python workflow DSL.
It does not need to know what a `Workflow` is. It only needs the compiled output.

In this example the message is bound directly to `emit_step.inputs.message`.
That is deliberate: it produces a real `cwlJobInputs` payload immediately, which
is the most useful shape for a ready-to-submit demo.

### 3. Validation happens before submission

This line is the trust boundary:

```python
compute_json = payload.get_compute_payload()
```

That call renders the payload and validates it against the checked-in compute schema.

If the payload shape drifts from the schema, it fails here, before any network call.

## Optional compute configuration

Most workflows only need the default payload shape:

```python
payload = ComputeWorkflowPayload(
    cwl_workflow=cwl_workflow,
    cwl_job_inputs=cwl_job_inputs,
)
```

When you do need compute-specific settings, add a `ComputeConfig`:

```python
from sophios.compute_payload import (
    ComputeConfig,
    OutputConfig,
    SlurmConfig,
    ToilConfig,
)

payload = ComputeWorkflowPayload(
    cwl_workflow=cwl_workflow,
    cwl_job_inputs=cwl_job_inputs,
    workflow_id="demo_job",
    compute_config=ComputeConfig(
        toil=ToilConfig(log_level="INFO"),
        output=OutputConfig.from_json(
            mode="userSpecified",
            outputDir="/tmp/compute-demo-out",
        ),
        slurm=SlurmConfig(partition="normal_gpu", cpus_per_task=4),
    ),
)
```

That keeps compute-specific concerns explicit without leaking them into the workflow DSL.
If you prefer the more Pythonic helpers, `OutputConfig.user_specified(...)` and
`OutputConfig.workflow_declared()` still work too.

## Submission

Submission is intentionally a separate concern:

```python
from sophios.compute_submit import submit_compute_json, submit_compute_payload

retval = submit_compute_payload(payload, "http://127.0.0.1:7998/compute/")
retval = submit_compute_json(compute_json, "http://127.0.0.1:7998/compute/")
```

Submission behavior is intentionally narrow:

- send the validated payload
- poll `/status/` until the job reaches a started or terminal state
- print logs only after the job reaches `RUNNING`

That makes the client behavior predictable and easy to demonstrate.

## Run the example

From the repository root:

```bash
PYTHONPATH=src python examples/scripts/compute_payload_workflow.py
PYTHONPATH=src python examples/scripts/compute_payload_workflow.py --validate-tool
PYTHONPATH=src python examples/scripts/compute_payload_workflow.py --submit-url http://127.0.0.1:7998/compute/
```

The first command writes a validated compute payload JSON file.
The second also validates the generated CLT first.
The third submits the payload to compute-slurm.

## Summary

The intended flow is now:

- author tools with `cwl_builder`
- compose them with the workflow Python API
- compile in memory with `Workflow.get_cwl_workflow()`
- package and validate with `ComputeWorkflowPayload`
- submit only when the payload is already known to match the schema

That gives you a path from Python authoring to compute submission without raw JSON assembly and without losing confidence in what is being sent.
