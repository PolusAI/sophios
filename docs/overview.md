# Overview

Sophios is a high-level way to create, build, and execute
[Common Workflow Language (CWL)](https://www.commonwl.org/) workflows. It is
designed for people who have useful command-line tools and need those tools to
become portable, inspectable workflows without making raw CWL the day-to-day
authoring language.

CWL is an excellent execution target: it is explicit, portable, runner-backed,
and designed for reproducible command-line workflows. The tradeoff is that raw
CWL can be verbose when the work you are trying to express is direct: run this
tool, pass its output to that tool, name the results that matter, and keep the
final workflow inspectable.

Sophios is the layer above CWL. You author in a higher-level model, then
compile to ordinary CWL artifacts that can be inspected, executed, submitted,
tested, archived, or compared.

The mental model is:

- **Tool contracts** describe one command-line tool: its inputs, outputs,
  command, bindings, requirements, and runtime assumptions.
- **Workflow graphs** place those tools into named steps and connect outputs to
  inputs.
- **Execution artifacts** are the generated CWL workflow, generated job inputs,
  exported `.wic` source documents, local runner outputs, optional debug
  artifacts, and optional submission requests.

The typical Python-first path is:

```text
Python tool contract
  -> Sophios Step
  -> Sophios Workflow
  -> compiled CWL workflow + job inputs
  -> local execution or remote/HPC/cloud execution
```

The important point is that Sophios does not replace CWL. It produces CWL. The
generated workflow, generated job inputs, and execution artifacts remain visible
because transparency is part of the design.

For a workflow author, that means Sophios should help answer practical questions
before a workflow is run on expensive or shared infrastructure:

- What command is going to run?
- What inputs must the user provide?
- What files, directories, or values will the workflow produce?
- Which step consumes which prior output?
- What exact CWL and job input object will the runner receive?
- Where should I look when local execution and remote execution behave
  differently?

## Why This Matters

Command-line tools are powerful because they are direct, scriptable, and easy to
test in isolation. The difficulty begins when a collection of command-line tools
has to become a reliable workflow.

A typical project might have:

- a segmentation command,
- a conversion command,
- a plotting command,
- a post-processing command,
- a Slurm script,
- a half-documented JSON request body,
- and several hidden assumptions about file names, containers, GPUs, or working
  directories.

That can work for one person on one machine. It becomes fragile when the
workflow must be reused, validated, transferred to another environment, submitted
to a service, or inspected months later.

Sophios addresses that problem by making workflow structure explicit without
requiring users to write every low-level CWL field by hand. A Sophios workflow
should make the important questions obvious:

- What does each command expect?
- What does each command produce?
- Which values are fixed by the workflow and which are supplied at runtime?
- Which output feeds which input?
- Which named results should downstream users or services consume?
- What exact CWL workflow and job input object will a runner receive?

That is the core promise: use a high-level authoring interface without giving up
the explicit, portable workflow description that CWL provides.

## Authoring Modes

Sophios has two workflow authoring modes.

The **Python Workflow API** is the primary interface. It is the recommended
starting point for most users because it gives workflow authors names,
functions, imports, tests, refactoring tools, and the ability to compose
workflows inside ordinary Python packages and applications.

Use Python when you want to:

- build workflows incrementally,
- reuse helper functions,
- construct `CommandLineTool` objects in code,
- test workflow construction with Python test tools,
- compile and inspect CWL in memory,
- run locally while developing,
- prepare workflows for execution on remote, HPC, or cloud resources.

The **YAML-based DSL** uses `.wic` files. It is the file-native interface for
standalone workflows, headless operation, CI, auditing, direct compiler
inspection, and advanced compiler-oriented features such as custom tags,
namespaces, inference controls, and static dispatch.

Use `.wic` YAML when you want a workflow to be a durable file artifact that can
be stored, diffed, validated, archived, and run without importing project
Python code.

Both modes compile through the Sophios compiler to CWL workflows and generated
job inputs. The difference is the authoring surface: Python is the default for
building workflows in code; `.wic` is the DSL for file-based and operational
workflows.

## What This Means in Practice

Sophios workflows are built from ordinary command-line tools, but the workflow
definition is more structured than a shell script. Each tool has a typed
interface. Each step has named inputs and outputs. Each workflow has a graph,
named results, and generated execution artifacts.

That structure matters because workflow failures rarely come from command order
alone. They come from unclear interfaces:

- Was this value meant to be fixed in the workflow or supplied at runtime?
- Did this step consume the output file from the previous step, or an older file
  with the same type?
- Which outputs are stable enough for downstream use?
- What CWL workflow and input object will a runner actually receive?
- Which artifacts should be inspected when local execution differs from remote
  execution?

Sophios makes those questions answerable from the workflow code and the
generated artifacts. A workflow can start as a few Python objects, but it still
has a concrete compiled CWL representation. A `.wic` workflow can stay as a
single file, but it still compiles through the same workflow machinery.

## What Sophios Handles

Sophios handles the repetitive workflow-building work that otherwise tends to
turn into boilerplate:

- loading existing CWL `CommandLineTool` files as workflow steps,
- building CWL `CommandLineTool` objects directly in Python,
- binding literal values and upstream outputs,
- inferring straightforward linear edges from compatible CWL types and formats,
- translating Python workflow objects into compiler-ready workflow definitions,
- compiling workflows to CWL,
- exporting Python-authored workflows as `.wic` source files,
- writing generated workflow artifacts to disk,
- running workflows locally through a CWL runner,
- preparing schema-validated requests from compiled CWL for remote execution.

The generated artifacts remain visible. Sophios is not a black-box execution
wrapper; it is an authoring and compilation layer that keeps the compiled
workflow available for inspection.

## What You Author

### 1. Tool Contracts

A tool contract says what one command-line tool needs and what it returns.

In Python, this is a `CommandLineTool`:

```python
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl

inputs = Inputs(
    message=Input(cwl.string, position=1),
)

outputs = Outputs(
    text_file=Output(cwl.file, glob="stdout.txt"),
)

tool = (
    CommandLineTool("emit_text", inputs, outputs)
    .base_command("echo")
    .stdout("stdout.txt")
)
```

This object is not a workflow yet. It is the contract for one executable unit:

- the command is `echo`,
- it accepts one string input named `message`,
- it writes stdout to `stdout.txt`,
- it exposes that file as an output named `text_file`.

That distinction matters. A tool contract should be understandable before it is
placed inside a workflow.

### 2. Workflow Steps

A `Step` is a tool contract used inside a workflow. A step has inputs you can
bind and outputs that later steps can consume.

```python
from pathlib import Path

from sophios.api.python.workflow import Step

echo = Step(clt_path=Path("cwl_adapters") / "echo.cwl")
echo.inputs.message = "hello from Sophios"
```

The assignment is a workflow binding. Sophios records that the `message` input
of this step should receive the literal value `"hello from Sophios"`.

Bindings can also connect steps:

```python
cat.inputs.file = echo.outputs.stdout
```

That line means: the `file` input of `cat` comes from the `stdout` output of
`echo`. It is an edge in the workflow graph.

### 3. Workflows

A `Workflow` is an ordered collection of steps and nested workflows.

```python
from sophios.api.python.workflow import Workflow

workflow = Workflow([echo], "hello_python")
compiled = workflow.compile()
compiled.write_cwl("autogenerated")
compiled.write_job_inputs("autogenerated")
```

Compilation checks the Sophios workflow object and emits CWL artifacts. Running
the workflow uses those artifacts with a CWL runner.

```python
workflow.run()
```

For service integration or remote execution, keep the compiled result in memory:

```python
compiled = workflow.compile()
```

That in-memory compiled object is the bridge to submission request construction.

## What Sophios Produces

Sophios produces artifacts that can be inspected. This is one of the most
important parts of the system.

Depending on how you compile or run, you may see:

- a root CWL workflow document,
- generated CWL job inputs,
- a `.wic` source file exported from a Python workflow,
- optional compiler-internal `.wic` trees when explicitly requested for
  debugging,
- Graphviz sources and diagrams,
- local runner output summaries,
- provenance files,
- remote execution request JSON.

These artifacts make the workflow debuggable and reviewable. They let you ask
concrete questions:

- Which command ran?
- Which input fed this step?
- Which output became a named workflow result?
- What exactly would be submitted for remote execution?
- Where did the generated CWL differ from the Python object I expected?

The generated artifacts are the concrete inspection surface. They give users
something specific to examine before a workflow becomes part of a larger
pipeline or a remote execution request.

## Why Python First

Python is the recommended authoring interface because it gives workflow authors
names, functions, imports, tests, static analysis, and refactoring tools. It is
also the most direct way to compose generated tools, reusable helper functions,
and application code.

Python lets teams build workflows incrementally:

1. Start with one tool.
2. Wrap it in a step.
3. Bind one literal input.
4. Compile.
5. Add a second step.
6. Name workflow outputs when downstream code needs stable result names.
7. Prepare remote execution requests only after the workflow is clear.

That progression keeps responsibilities separated. Tool contracts, workflow
edges, runtime inputs, compiled CWL, and submission requests can each be
inspected at the point where they become relevant.

Python is the authoring layer. CWL is the portable execution target. YAML is the
file-native mode for advanced and operational workflows.

## Why CWL Still Matters

Sophios does not try to replace CWL. It uses CWL as the execution target.

CWL is valuable because it is portable, explicit, and supported by existing
runners. The cost is that raw CWL can be verbose for everyday authoring. Sophios
is the layer above it:

- Python makes workflows easier to author.
- Sophios compiles those workflows into CWL.
- CWL runners execute the compiled result.

This gives you a practical split. Users can work in Python, generated CWL can be
inspected when needed, and execution remains grounded in a workflow standard.

## Where YAML Fits

The `.wic` YAML format is still first-class, but it serves a different role from
the Python API.

Use YAML when you need:

- a workflow that should live as a single file,
- headless execution from CI or batch scripts,
- a compact artifact for review or archive,
- explicit custom tags for inline inputs and anchors,
- schema-backed editor validation,
- detailed inspection of inference and compiler behavior.

YAML is especially useful for auditability. A `.wic` file can be stored, diffed,
validated, and run without importing a Python module from a project repository.

The docs therefore treat YAML as advanced usage: important, powerful, and best
used when a file-native workflow representation is the right operational shape.

## Local and Remote Execution

Sophios supports two common execution paths.

### Local Execution

Use local execution while developing:

```python
workflow.run()
```

Local runs compile the workflow, invoke a CWL runner, and write output
artifacts. Sophios supports `cwltool` as the default local runner and
`toil-cwl-runner` as the Toil-based local runner. This is the direct path for
verifying development-scale workflows end to end.

### Remote Execution

When a workflow is ready to leave the local development loop, compile it first
and use the compiled CWL plus generated job inputs as the handoff point for
execution on remote, HPC, or cloud resources.

That boundary is intentionally separate from workflow authoring. A workflow
should be understandable before it becomes a remote job. The service-specific
request shape belongs in the execution integration layer, not in the conceptual
workflow definition.

## What Makes Sophios Different

Sophios is not just a wrapper around command execution. Its value comes from the
combination of authoring ergonomics and inspectable outputs.

### It is tool-centered

The basic unit is the command-line tool contract. This keeps workflow design
close to the software people already use.

### It is graph-aware

Step outputs can be connected to later step inputs. Sophios can also infer many
linear edges from type and format information, whether the workflow was authored
in Python or `.wic` YAML.

### It is artifact-oriented

Compilation produces files and data structures that can be inspected. This is
essential for debugging, review, CI, and reproducibility.

### It is execution-agnostic at the authoring layer

The same Python-authored workflow can be compiled, run locally, inspected as CWL,
or packaged for execution on remote, HPC, or cloud resources.

### It is honest about advanced complexity

Features like scatter, conditionals, inference rules, namespaces, and static
dispatch are powerful, but they should be used deliberately. The documentation
keeps the base workflow model separate from these advanced controls so their
runtime effects are easier to review.

## What Sophios Is Not

Sophios is not a general-purpose scheduler. It does not replace Docker, Podman,
Slurm, CWL runners, or remote execution services. It coordinates with those
systems.

Sophios is not a magic planner. It can infer some edges and can perform limited
automatic insertion in advanced YAML workflows, but users should still inspect
important generated DAGs and artifacts.

Sophios is not a reason to skip tool design. Clear tool inputs and outputs are
still the foundation of a clear workflow.

## Recommended Adoption Path

For a new project or integration, use this order:

1. Install Sophios and verify that the Python API imports.
2. Build a minimal Python workflow with `Step` and `Workflow`.
3. Bind a step output into a later step input.
4. Name workflow outputs when downstream code needs stable result names.
5. Compile and inspect generated CWL.
6. Author a new `CommandLineTool` in Python.
7. Convert the generated tool into a workflow step.
8. Build a submission request only after the workflow is clear.
9. Use `.wic` YAML for advanced standalone, CI, or audit-focused workflows.

Each stage introduces one responsibility and one artifact boundary. Keep those
boundaries explicit in the workflow definition and generated artifacts.

## What To Read Next

- [Install Guide](installguide.md): set up Sophios and verify the environment.
- [Python Workflow API](userguide.md): use `Step`, `Workflow`, bindings, compile, and run.
- [Building Tool Contracts in Python](tool_builder_sam3.md): define CWL `CommandLineTool` contracts.
- [Using Tool Builder and the Workflow Python API Together](tool_builder_workflow.md): compose generated tools in memory.
- [From Python Workflow to Compute Request](compute_request_workflow.md): package compiled workflows for validated remote execution requests.
- [Advanced YAML and Operations](advanced.md): use `.wic` files for auditability, CI, debugging, and advanced compiler features.
