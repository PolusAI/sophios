# Canonical Python-to-Compute Flow with `ichnaea_compact.py`

This document describes the recommended Python path in Sophios for taking a
tool definition all the way to a validated compute submission request.

The canonical reference implementation is
[`examples/scripts/ichnaea_compact.py`](https://github.com/PolusAI/sophios/blob/main/examples/scripts/ichnaea_compact.py).

The goal of the example is precise:

1. define a CWL `CommandLineTool` in Python,
2. convert that tool into a Sophios `Step`,
3. wrap the step in a `Workflow`,
4. compile the workflow fully in memory,
5. package the compiled workflow and job inputs as a schema-valid compute
   request,
6. submit that request to the compute service chosen by the user.

This guide is intended to be read after:

- [Building Tool Contracts in Python](tool_builder_sam3.md)
- [Using Tool Builder and the Workflow Python API Together](tool_builder_workflow.md)
- [From Python Workflow to Compute Request](compute_request_workflow.md)

Those documents explain the individual APIs.
This one explains how they fit together in the current end-to-end path.

## Scope

This document is specifically about the compute submission path currently
implemented by Sophios.

That distinction matters for two reasons:

- the request schema is checked into Sophios,
- the submission helper expects the HTTP API shape used by that compute
  service.

This is not a generic remote-execution tutorial, and it does not describe every
possible third-party compute backend.

## What this example demonstrates

`ichnaea_compact.py` is the canonical example because it captures the intended
division of responsibilities across the Python surface:

- `tool_builder` defines the tool contract
- the workflow Python API defines orchestration
- `ComputeRequest` defines the submission request
- `ComputeRequest.submit(...)` performs typed submission and status polling

That separation is the architectural point of the example.

Sophios is not asking one object to behave simultaneously as:

- a CLT authoring API,
- a workflow API,
- a JSON request builder,
- and a network client.

Instead, each layer contributes one well-scoped transformation.

## The conceptual pipeline

The complete flow is:

```text
Python CLT definition
    -> Sophios Step
    -> Sophios Workflow
    -> compiled CWL workflow + job inputs
    -> compute request
    -> compute submission
```

This is the simplest useful mental model for the example.

## Where this document fits

The Python documentation now forms a sequence:

1. [tool_builder_sam3](tool_builder_sam3.md) explains how to author one CLT in Python
2. [tool_builder_workflow](tool_builder_workflow.md) explains how a built CLT becomes a workflow step
3. [compute_request_workflow](compute_request_workflow.md) explains the generic compute request API
4. this document explains the recommended end-to-end compute submission path

For most users, that means:

- learn the CLT builder first
- learn the CLT-to-workflow bridge second
- use this document when moving to real compute submission

## What `ichnaea_compact.py` is responsible for

The compact example is intentionally narrow.
It does not attempt to demonstrate every capability of the Python APIs.
Instead, it demonstrates one coherent workflow:

- define the Ichnaea autosegmentation CLT
- turn it into a one-step Sophios workflow
- compile that workflow
- package the compiled result for compute submission
- optionally submit it

That narrow scope is deliberate.
It makes the example suitable both as documentation and as a reference client.

## Layer 1: the CLT definition

The first major function in the example is
[`build_autoseg_CLT()`](https://github.com/PolusAI/sophios/blob/main/examples/scripts/ichnaea_compact.py).

This function belongs entirely to the `tool_builder` layer.
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
[tool_builder_sam3](tool_builder_sam3.md).

## Layer 2: the workflow wrapper

The second major function is
[`workflow(...)`](https://github.com/PolusAI/sophios/blob/main/examples/scripts/ichnaea_compact.py).

This function is narrow by design.
Its purpose is not to redescribe the tool.
Its purpose is to place the tool in a Sophios workflow context.

It does two things:

1. builds a `Step` from the generated CLT
2. binds concrete input values and wraps that step in a `Workflow`

### Build a step from the CLT

The boundary crossing is:

```python
autoseg_clt = build_autoseg_CLT()
autoseg = Step(autoseg_clt, step_name="autoseg")
```

This is the intended handoff from `tool_builder` to the workflow API.

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
compiled_workflow = autoseg_workflow.compile()
```

This object contains:

- `compiled_workflow.name`
- `compiled_workflow.cwl_workflow`
- `compiled_workflow.cwl_job_inputs`

This boundary is intentionally named and structured. The workflow layer owns DAG
composition and compilation; the compute layer consumes the compiled result.

```python
workflow_name = compiled_workflow.name
```

This handoff is the exact boundary between:

- the result of workflow compilation
- the input expected by the compute request layer

That explicit separation keeps the transition to the compute layer transparent
without asking users to pick apart a legacy dictionary shape.

## Layer 4: compute request construction

The next function,
[`create_compute_request(...)`](https://github.com/PolusAI/sophios/blob/main/examples/scripts/ichnaea_compact.py),
packages the compiled workflow into a `ComputeRequest`.

The construction is intentionally direct:

```python
compute_request = ComputeRequest(
    compiled_workflow,
    workflow_id=workflow_id,
    compute_config=ComputeExecutionConfig(
        toil=ToilRuntimeConfig(log_level="INFO"),
        output=ComputeOutputConfig.workflow_declared(),
        slurm=SlurmJobConfig(partition="normal_gpu", cpus_per_task=4),
    ),
)
```

This is where compute-specific concerns are meant to live:

- Toil configuration
- output handling
- Slurm scheduler settings

The workflow layer should not encode those concerns directly.
Likewise, the compute request layer should not need to know how the workflow was
authored.

That is why the request layer stays focused and declarative.

If you want the lower-level request API in isolation, see
[compute_request_workflow](compute_request_workflow.md).

## Submission behavior

The final step is submission:

```python
submission = compute_request.submit(submit_url)
return submission.exit_code
```

The compute service URL is supplied by the user in Python:

```python
SUBMIT_URL = "http://127.0.0.1:7998/compute/"
```

This is the correct contract for an example client:

- the script does not assume a fixed deployment endpoint
- the user decides whether a real submission should occur
- leaving `SUBMIT_URL = None` keeps the script in build-only mode

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

The request is constructed through `ComputeRequest`, which validates the
result against the checked-in compute schema.

This means validation is incremental:

- first confirm the tool
- then confirm the workflow
- then confirm the request
- only then submit

That is more reliable than assembling one large opaque object at the
end.

## The verification-oriented sibling: `ichnaea_integrated.py`

The compact example is the canonical path because it stays in memory as long as
possible.

However, Sophios also provides
[`examples/scripts/ichnaea_integrated.py`](https://github.com/PolusAI/sophios/blob/main/examples/scripts/ichnaea_integrated.py)
for cases where explicit artifacts are desirable.

It follows the same overall logic, but writes outputs at each major boundary.

### Generated CLT

The CLT is written to disk with validation:

```python
autoseg_clt.write_cwl(
    Path(__file__).with_name("built-ichnaea-autosegmentation.cwl"),
    validate=True,
)
```

### Compiled workflow artifacts

The workflow is compiled, then the compiled boundary writes its artifacts:

```python
compiled_workflow = autoseg_workflow.compile()
compiled_workflow.write_cwl("autogenerated")
compiled_workflow.write_job_inputs("autogenerated")
```

This writes the compiled workflow artifacts under `autogenerated/` and returns
the same `CompiledWorkflow` boundary used by the in-memory path.

### Compute JSON

The exact compute request JSON is written before submission:

```python
compute_request_json = compute_request.to_json(indent=4, sort_keys=True)
Path(f"compute_{workflow_id}_integrated.json").write_text(
    compute_request_json,
    encoding="utf-8",
)
```

That makes `ichnaea_integrated.py` the appropriate choice when:

- the generated CLT must be reviewed directly
- the compiled workflow artifacts must be preserved
- the exact submission body must be inspected or archived

In other words:

- use `ichnaea_compact.py` as the example to follow when creating your own
  end-to-end Python workflow submission scripts
- use `ichnaea_integrated.py` when the same structure is needed but the CLT,
  compiled workflow artifacts, and final request must also be written to disk

## Recommended reading order

For a first reading of the example, the most useful order is:

1. `build_autoseg_CLT()`
2. `workflow(...)`
3. `create_compute_request(...)`
4. `main()`

That order follows the actual transformation pipeline:

- tool definition
- workflow construction
- request construction
- orchestration and optional submission

## Practical guidance

Use `ichnaea_compact.py` as the example to follow when creating or adapting
your own end-to-end compute submission scripts. Its structure is the
recommended baseline:

- define the CLT in Python
- convert it to a Sophios workflow
- compile the workflow in memory
- construct the compute request from the compiled result
- submit only when a concrete compute service URL is supplied

Use `ichnaea_integrated.py` when the same overall structure is required but the
workflow must also produce explicit artifacts:

- the generated CLT on disk
- the compiled workflow artifacts on disk
- the exact compute request JSON on disk

When diagnosing problems, the most effective order is:

1. validate the CLT
2. inspect the compiled workflow
3. inspect the compute request
4. then investigate submission or runtime behavior

That keeps the investigation aligned with the actual system boundaries.

## Commands

Compact path:

```bash
python examples/scripts/ichnaea_compact.py
```

Integrated path:

```bash
python examples/scripts/ichnaea_integrated.py
```

The integrated command writes the generated CLT, compiled workflow artifacts,
and compute JSON without submission.
To submit from either script, set `SUBMIT_URL` near the top of the file before
running it.

## Summary

`ichnaea_compact.py` is the canonical Sophios Python example for
compute submission because it keeps the four layers of the system clear:

- CLT authoring
- workflow composition
- request construction
- submission

That clarity is the main value of the example.
It makes the path from Python-authored tool to compute request direct,
verifiable, and suitable for both documentation and real client use.
