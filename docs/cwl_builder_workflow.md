# Using `cwl_builder` and the Workflow Python API Together

Sophios now has two related Python surfaces:

- `sophios.apis.python.cwl_builder` for authoring a single CWL `CommandLineTool`
- `sophios.apis.python.api` for wiring tools into a workflow with `Step` and `Workflow`

Those APIs are intentionally separate, but they can be combined cleanly.

This guide shows the intended end-to-end pattern:

1. define a new tool in Python,
2. validate that tool as a real CWL `CommandLineTool`,
3. convert it into an in-memory `Step`,
4. compose it with the normal Sophios workflow DSL.

The important part is that the handoff stays **in memory**. You do not need to write a temporary `.cwl` file just to use a freshly built tool inside a workflow.

A runnable version of this pattern lives in
[examples/scripts/cwl_builder_workflow.py](https://github.com/PolusAI/workflow-inference-compiler/blob/master/examples/scripts/cwl_builder_workflow.py).

## When to use this pattern

This hybrid style is useful when:

- a tool does not exist yet as a checked-in `.cwl` file,
- you want to generate a family of similar tools from Python,
- you want to validate the generated CLT before putting it into a workflow,
- or you want a workflow to mix generated tools with ordinary file-backed `Step(...)` objects.

If you only need to build a single standalone CLT, start with [cwl_builder_sam3](cwl_builder_sam3.md).

If you already have checked-in `.cwl` tools and only need to compose them, the workflow examples in [userguide](userguide.md) are still the right starting point.

If your next step is compute-slurm submission rather than local execution, continue with [ichnaea_compact_compute](ichnaea_compact_compute.md) for the canonical production-like example or [compute_payload_workflow](compute_payload_workflow.md) for the lower-level compute payload API.

## Mental model

The cleanest way to think about the boundary is:

- `CommandLineTool(...)` defines a **tool contract**
- `tool.validate()` checks that contract as real CWL
- `tool.to_step()` turns that contract into a **workflow node**
- `Workflow(...)` composes that node with other steps

That separation is deliberate.

The builder does not need to know about workflows.
The workflow DSL does not need to know how the tool was authored.
The bridge is small: it passes a normal CWL document from one side to the other.

## What we will build

We will build a tiny tool called `emit_text`:

- it accepts one string input named `message`,
- it runs `echo`,
- it captures stdout into a file,
- and it exposes that file as a normal CWL `File` output.

Then we will:

- convert that built tool into a Sophios `Step`,
- feed its file output into the existing checked-in [`cat.cwl`](https://github.com/PolusAI/workflow-inference-compiler/blob/master/cwl_adapters/cat.cwl),
- expose a workflow input called `message`,
- and expose a workflow output called `result`.

So the final workflow shape is:

```text
workflow input "message"
    -> emit_text (generated in memory)
    -> cat.cwl (file-backed step)
    -> workflow output "result"
```

## Full example

The snippet below assumes you are running from the repository root, so the checked-in adapter path `cwl_adapters/cat.cwl` is valid as written.

```python
from pathlib import Path

from sophios.apis.python import (
    CommandLineTool,
    Input,
    Inputs,
    Output,
    Outputs,
    Step,
    Workflow,
    cwl,
)


def build_emit_text_tool() -> CommandLineTool:
    inputs = Inputs(
        message=Input(cwl.string, position=1)
        .label("Message")
        .doc("Text to print to stdout"),
    )

    outputs = Outputs(
        file=Output(cwl.file, glob="stdout")
        .label("Captured stdout")
        .doc("The file produced by redirecting stdout"),
    )

    return (
        CommandLineTool("emit_text", inputs, outputs)
        .describe(
            "Emit a message",
            "Small example CLT built in Python and consumed by the workflow DSL.",
        )
        .base_command("echo")
        .stdout("stdout")
    )


def build_workflow() -> Workflow:
    emit_tool = build_emit_text_tool()

    # Optional but recommended while developing new generated tools.
    # This requires cwltool/schema-salad in your Python environment.
    emit_tool.validate()

    # No temporary file is needed here. The CLT is handed to Step in memory.
    emit_step = emit_tool.to_step(step_name="emit_text")

    # This is an ordinary checked-in CWL adapter.
    cat_step = Step(Path("cwl_adapters") / "cat.cwl")

    workflow = Workflow([emit_step, cat_step], "builder_and_pyapi_demo")

    # Be explicit about the workflow interface.
    workflow.add_input("message", cwl.string)

    # Recommended explicit binding style.
    emit_step.inputs.message = workflow.inputs.message
    cat_step.inputs.file = emit_step.outputs.file

    # Expose a workflow output.
    workflow.outputs.result = cat_step.outputs.output
    return workflow


workflow = build_workflow()
compiler_info = workflow.compile(write_to_disk=True)
```

## Why this example is structured this way

There are a few details worth calling out.

### 1. The CLT is complete before it becomes a step

The `emit_text` tool is a real `CommandLineTool` first:

```python
inputs = Inputs(
    message=Input(cwl.string, position=1),
)

outputs = Outputs(
    file=Output(cwl.file, glob="stdout"),
)

tool = (
    CommandLineTool("emit_text", inputs, outputs)
    .base_command("echo")
    .stdout("stdout")
)
```

That matters because the builder API is responsible for answering tool-level questions:

- what are the inputs,
- what are the outputs,
- what command runs,
- how are stdout/stderr/files represented.

The workflow API should not need to rebuild that information later.

### 2. `tool.validate()` happens at the tool boundary

Validation belongs naturally on the builder side:

```python
emit_tool.validate()
```

That gives you confidence that the generated CLT is valid CWL **before** it participates in a larger workflow.

For self-authored tools, that is usually the best debugging boundary:

- first make the tool valid,
- then compose it into the workflow.

### 3. `tool.to_step()` is the bridge

This is the key handoff:

```python
emit_step = emit_tool.to_step(step_name="emit_text")
```

That call:

- renders the CLT to a standard CWL document,
- parses it through the Python workflow API,
- and returns a normal `Step`.

After that, you work with the object exactly like any other `Step`:

```python
emit_step.inputs.message = workflow.inputs.message
cat_step.inputs.file = emit_step.outputs.file
```

That is the main design goal of the bridge: once a built tool becomes a step, it should feel boring.

### 4. Workflow bindings should stay explicit

This guide uses the explicit form:

```python
emit_step.inputs.message = workflow.inputs.message
cat_step.inputs.file = emit_step.outputs.file
workflow.outputs.result = cat_step.outputs.output
```

That is easier to read than the legacy shorthand and makes directionality obvious:

- `inputs.*` are places you can bind values,
- `outputs.*` are places you can read values from.

The old shorthand still exists for compatibility, but it is not the best style to teach.

### 5. Workflow interface should be declared deliberately

This line is important:

```python
workflow.add_input("message", cwl.string)
```

Yes, the compatibility layer can still create workflow inputs implicitly in some situations.
But explicit workflow inputs are much easier to reason about, especially when the workflow is meant to be reused or reviewed by someone else.

## What gets written to disk

Only the compiled workflow artifacts are written when you call:

```python
workflow.compile(write_to_disk=True)
```

The generated `emit_text` CLT does **not** need to be written as a standalone `.cwl` file first.

That means this pattern is suitable for:

- generated tools,
- parameterized tools,
- short-lived tools used only inside a larger workflow,
- and tests that want to build tools programmatically.

## How to trust this pattern

There are two separate confidence checks here, and they complement each other.

### 1. Tool confidence

`emit_tool.validate()` checks the generated CLT as a real CWL document.

That tells you:

- the tool structure is valid,
- the CWL fields are in the right shape,
- and the generated CLT is not just "some YAML that looks plausible".

### 2. Workflow confidence

`workflow.compile(...)` checks that the generated step can participate in the normal Sophios compilation path.

That tells you:

- the workflow DSL can consume the built tool,
- the step ports are wired correctly,
- and the result compiles into the same pipeline machinery as any other Sophios workflow.

Those are different guarantees, and you usually want both.

## Recommended workflow for teams

For day-to-day development, this sequence tends to work well:

1. build the tool with `CommandLineTool(...)`
2. call `tool.validate()`
3. convert it with `tool.to_step()`
4. wire it into a `Workflow(...)`
5. call `workflow.compile(...)`
6. only then move on to full execution

That keeps failures close to the layer that caused them.

## Summary

The combined Python story is now:

- use `cwl_builder` to define a proper CWL tool,
- validate it while it is still a tool,
- turn it into a `Step` in memory,
- compose it with ordinary Sophios workflow steps.

That gives you the best of both worlds:

- the rigor of a real CWL `CommandLineTool`,
- and the composability of the Sophios workflow Python API.

## Run the example script

From the repository root:

```bash
PYTHONPATH=src python examples/scripts/cwl_builder_workflow.py --validate
PYTHONPATH=src python examples/scripts/cwl_builder_workflow.py --run
```

The first command validates the generated CLTs and compiles the workflow.
The second runs the full demo workflow through `Workflow.run()`.
