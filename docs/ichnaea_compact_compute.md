# Canonical Python-to-Compute-Slurm Flow with `ichnaea_compact.py`

This document describes the recommended Python path in Sophios for taking a
tool definition all the way to a compute-slurm submission payload.

The canonical reference implementation is
[`examples/scripts/ichnaea_compact.py`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/ichnaea_compact.py).

The goal of the example is precise:

1. define a CWL `CommandLineTool` in Python,
2. convert that tool into a Sophios `Step`,
3. wrap the step in a `Workflow`,
4. compile the workflow fully in memory,
5. package the compiled workflow and job inputs as a schema-valid
   compute-slurm payload,
6. submit that payload to a compute-slurm service chosen by the user.

This guide is intended to be read after:

- [Building a CWL CommandLineTool in Python](cwl_builder_sam3.md)
- [Using `cwl_builder` and the Workflow Python API Together](cwl_builder_workflow.md)
- [From Python Workflow to Compute Payload](compute_payload_workflow.md)

Those documents explain the individual APIs.
This one explains how they fit together in the current end-to-end path.

## Scope

This document is specifically about submission to **compute-slurm**.

That distinction matters for two reasons:

- the payload schema is the compute-slurm schema checked into Sophios
- the submission helper is designed around the compute-slurm HTTP API

This is not a generic remote-execution tutorial, and it does not describe
arbitrary third-party compute backends.

## What this example demonstrates

`ichnaea_compact.py` is the canonical example because it captures the intended
division of responsibilities across the Python surface:

- `cwl_builder` defines the tool contract
- the workflow Python API defines orchestration
- `ComputeWorkflowPayload` defines the compute-slurm submission payload
- `submit_compute_payload(...)` performs submission and status polling

That separation is the architectural point of the example.

Sophios is not asking one object to behave simultaneously as:

- a CLT authoring API,
- a workflow DSL,
- a JSON payload builder,
- and a network client.

Instead, each layer contributes one well-scoped transformation.

## The conceptual pipeline

The complete flow is:

```text
Python CLT definition
    -> Sophios Step
    -> Sophios Workflow
    -> compiled CWL workflow + job inputs
    -> compute-slurm payload
    -> compute-slurm submission
```

This is the simplest useful mental model for the example.

## Where this document fits

The Python documentation now forms a sequence:

1. [cwl_builder_sam3](cwl_builder_sam3.md) explains how to author one CLT in Python
2. [cwl_builder_workflow](cwl_builder_workflow.md) explains how a built CLT becomes a workflow step
3. [compute_payload_workflow](compute_payload_workflow.md) explains the generic compute payload API
4. this document explains the recommended end-to-end compute-slurm path

For most users, that means:

- learn the CLT builder first
- learn the CLT-to-workflow bridge second
- use this document when moving to real compute-slurm submission

## What `ichnaea_compact.py` is responsible for

The compact example is intentionally narrow.
It does not attempt to demonstrate every capability of the Python APIs.
Instead, it demonstrates one coherent workflow:

- define the Ichnaea autosegmentation CLT
- turn it into a one-step Sophios workflow
- compile that workflow
- package the compiled result for compute-slurm
- optionally submit it

That narrow scope is deliberate.
It makes the example suitable both as documentation and as a reference client.

## Layer 1: the CLT definition

The first major function in the example is
[`build_autoseg_CLT()`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/ichnaea_compact.py).

This function belongs entirely to the `cwl_builder` layer.
It is responsible for the CLT itself:

- inputs
- outputs
- labels and docs
- base command
- Docker image
- GPU hint
- staging requirements
- resource requests

That boundary is important.
The workflow layer should not have to reconstruct tool-level concerns later.

### What to look for in the CLT definition

When reading `build_autoseg_CLT()`, focus on three questions:

1. What is the external command contract?
2. What runtime assumptions are encoded in the CLT?
3. Which details are intrinsic to the tool, rather than to any particular workflow?

For example, the following all belong in the CLT:

- the input and output zarr directories
- the optional model override
- the optional tiling and LoRA parameters
- the Ichnaea container image
- the GPU hint
- the `InitialWorkDirRequirement`
- the resource request

Those are properties of the tool itself.

If you need a slower introduction to this style of CLT construction, return to
[cwl_builder_sam3](cwl_builder_sam3.md).

## Layer 2: the workflow wrapper

The second major function is
[`workflow(...)`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/ichnaea_compact.py).

This function is intentionally small.
Its purpose is not to redescribe the tool.
Its purpose is to place the tool in a Sophios workflow context.

It does two things:

1. converts the generated CLT into a `Step`
2. binds concrete input values and wraps that step in a `Workflow`

### CLT-to-step conversion

The boundary crossing is:

```python
autoseg_clt = build_autoseg_CLT()
autoseg = autoseg_clt.to_step(step_name="autoseg")
```

This is the intended handoff from `cwl_builder` to the workflow API.

No intermediate `.cwl` file is required.
The CLT remains in memory and becomes a normal Sophios `Step`.

That is a key part of the current design.
It keeps the authoring API and the workflow API loosely coupled while still
allowing them to work together directly.

### Value binding and workflow construction

The example then binds the concrete values:

```python
autoseg.output = input_dicts["output_dir"]
autoseg.input = input_dicts["input_dir"]
autoseg.model = input_dicts["model_file"]
```

and wraps the step in a workflow:

```python
wkflw = Workflow([autoseg], workflow_name)
```

This workflow layer is intentionally thin.
In this example the workflow is mainly an orchestration wrapper around one
already well-specified tool.

That is an acceptable and useful use of the workflow API.

## Layer 3: compiled workflow output

The next boundary is the compiled workflow object:

```python
workflow_json = autoseg_workflow.get_cwl_workflow()
```

This object contains:

- the workflow name
- the generated `yaml_inputs`
- the compiled CWL workflow document

The example then separates those pieces explicitly:

```python
workflow_name = workflow_json["name"]
workflow_inputs = copy.deepcopy(workflow_json["yaml_inputs"])
workflow_json.pop("name")
workflow_json.pop("yaml_inputs")
compiled_cwl_workflow = copy.deepcopy(workflow_json)
```

This split is not incidental.
It is the exact boundary between:

- the result of workflow compilation
- the input expected by the compute-slurm payload layer

After the split:

- `compiled_cwl_workflow` is the CWL workflow document
- `workflow_inputs` is the compute job input object
- `workflow_name` is the submission identifier

That explicit separation keeps the transition to the compute layer transparent.

## Layer 4: compute-slurm payload construction

The next function,
[`create_compute_payload(...)`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/ichnaea_compact.py),
packages those pieces into a schema-backed `ComputeWorkflowPayload`.

The construction is intentionally direct:

```python
compute_object = ComputeWorkflowPayload(
    workflow_id=workflow_id,
    cwl_workflow=cwl_workflow,
    cwl_job_inputs=cwl_job_inputs,
    compute_config=ComputeConfig(
        toil=ToilConfig(log_level="INFO"),
        output=OutputConfig.workflow_declared(),
        slurm=SlurmConfig(partition="normal_gpu", cpus_per_task=4),
    ),
)
```

This is where compute-slurm-specific concerns are meant to live:

- Toil configuration
- output handling
- Slurm scheduler settings

The workflow layer should not encode those concerns directly.
Likewise, the compute payload layer should not need to know how the workflow was
authored.

That is why the payload layer stays small and declarative.

If you want the lower-level payload API in isolation, see
[compute_payload_workflow](compute_payload_workflow.md).

## Submission behavior

The final step is submission:

```python
submit_compute_payload(compute_object, submit_url)
```

The compute-slurm URL is supplied by the user:

```bash
PYTHONPATH=src python examples/scripts/ichnaea_compact.py \
  --submit-url http://127.0.0.1:7998/compute/
```

This is the correct contract for an example client:

- the script does not assume a fixed deployment endpoint
- the user decides whether a real submission should occur
- omitting `--submit-url` keeps the script in build-only mode

That makes the script useful both as documentation and as a real client entry
point.

## Why this path is reliable

The value of this design is that each boundary can be checked before the next
one is crossed.

### Tool boundary

The CLT can be validated as a real CWL `CommandLineTool`.

### Workflow boundary

The workflow can be compiled fully in memory before any submission occurs.

### Compute boundary

The payload is constructed through `ComputeWorkflowPayload`, which validates the
result against the checked-in compute-slurm schema.

This means the trust model is incremental:

- first confirm the tool
- then confirm the workflow
- then confirm the payload
- only then submit

That is a much stronger model than assembling one large opaque object at the
end.

## The verification-oriented sibling: `ichnaea_integrated.py`

The compact example is the canonical path because it stays in memory as long as
possible.

However, Sophios also provides
[`examples/scripts/ichnaea_integrated.py`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/ichnaea_integrated.py)
for cases where explicit artifacts are desirable.

It follows the same overall logic, but writes outputs at each major boundary.

### Generated CLT

The CLT is written to disk with validation:

```python
autoseg_clt.save(
    Path(__file__).with_name("built-ichnaea-autosegmentation.cwl"),
    validate=True,
)
```

### Compiled workflow artifacts

The workflow is compiled with disk output enabled:

```python
autoseg_workflow.compile(write_to_disk=True)
```

This writes the compiled workflow artifacts under `autogenerated/`.

### Compute JSON

The exact compute payload is written before submission:

```python
with open(f"compute_{workflow_name}_integrated.json", "w", encoding="utf-8") as f:
    json.dump(compute_json, f, indent=4, sort_keys=True)
```

That makes `ichnaea_integrated.py` the appropriate choice when:

- the generated CLT must be reviewed directly
- the compiled workflow artifacts must be preserved
- the exact submission body must be inspected or archived

In other words:

- use `ichnaea_compact.py` as the example to follow when creating your own
  end-to-end Python workflow submission scripts for compute-slurm
- use `ichnaea_integrated.py` when the same structure is needed but the CLT,
  compiled workflow artifacts, and final payload must also be written to disk

## Recommended reading order

For a first reading of the example, the most useful order is:

1. `build_autoseg_CLT()`
2. `workflow(...)`
3. `create_compute_payload(...)`
4. `main()`

That order follows the actual transformation pipeline:

- tool definition
- workflow construction
- payload construction
- orchestration and optional submission

## Practical guidance

Use `ichnaea_compact.py` as the example to follow when creating or adapting
your own end-to-end compute-slurm submission scripts. Its structure is the
recommended baseline:

- define the CLT in Python
- convert it to a Sophios workflow
- compile the workflow in memory
- construct the compute-slurm payload from the compiled result
- submit only when a concrete compute-slurm URL is supplied

Use `ichnaea_integrated.py` when the same overall structure is required but the
workflow must also produce explicit artifacts:

- the generated CLT on disk
- the compiled workflow artifacts on disk
- the exact compute payload JSON on disk

When diagnosing problems, the most effective order is:

1. validate the CLT
2. inspect the compiled workflow
3. inspect the compute payload
4. then investigate submission or runtime behavior

That keeps the investigation aligned with the actual system boundaries.

## Commands

Compact path:

```bash
PYTHONPATH=src python examples/scripts/ichnaea_compact.py
PYTHONPATH=src python examples/scripts/ichnaea_compact.py \
  --submit-url http://127.0.0.1:7998/compute/
```

Integrated path:

```bash
PYTHONPATH=src python examples/scripts/ichnaea_integrated.py
PYTHONPATH=src python examples/scripts/ichnaea_integrated.py \
  --submit-url http://127.0.0.1:7998/compute/
```

The first integrated command writes the generated CLT, compiled workflow
artifacts, and compute JSON without submission.
The second performs the same steps and then submits the payload to
compute-slurm.

## Summary

`ichnaea_compact.py` is the canonical Sophios Python example for
compute-slurm submission because it keeps the four layers of the system clear:

- CLT authoring
- workflow composition
- payload construction
- submission

That clarity is the main value of the example.
It makes the path from Python-authored tool to compute-slurm payload direct,
verifiable, and suitable for both documentation and real client use.
