# The workflow model

What is a Sophios workflow, underneath the two ways of writing one? It is a
graph: steps that run processes, ports on those steps, edges between ports,
and a boundary of workflow inputs and outputs. Both forms build that graph,
the compiler completes it, and a target writes it out. This page is the
model, independent of any target; [Compiling to CWL](compiling-to-cwl.md) is
what the CWL target does with it.

## Steps and processes

A workflow is an ordered list of steps. Each step runs one process: a
CommandLineTool, or another workflow. The order is the order you wrote, and
it matters twice: inference looks backwards through it, and a step may read
only from steps before it. Nothing else about the order is kept; the compiled
graph is a DAG, and a runner executes independent steps as it sees fit.

A step has an identity, its position and its name, which is why one tool may
appear twice and why a note says `step 2 'append'` rather than `append`. The
name comes from the tool's file stem unless you give one.

## Ports

Every process declares input ports and output ports, each with a type and,
for files, perhaps a format. A step's ports are its process's. Sophios reads
them from the tool or the subworkflow and never invents one; the single
exception is a [record](types-and-records.md) bound to a name the tool does
not declare, which adds an `Any` input to that step so that a `when` or a
`valueFrom` can read it.

A port is where every check happens. A value is checked against the type of
the port it binds; an edge against the types at both ends; a scatter against
the ports it names. The checks are at the port, so the message names the
port.

## Bindings

Each input port takes one of:

- a literal, a value written in the workflow;
- a workflow input, a value supplied when the workflow runs;
- an edge, the output of an earlier step;
- a record, for a merge of several sources, a pick among them, a default or
  a `valueFrom` transformation;
- nothing, which inference may fill.

Each form has one spelling in `.wic` and one in Python
([Inputs and values](inputs-and-values.md)). The model does not distinguish
how a binding was written; a `!*` and a Python assignment are the same edge.

## Edges and inference

An edge connects an output port of one step to an input port of a later
step. Edges come from two places: the ones you wrote, which are placed first,
and the ones inference adds, one for each required input still unbound,
chosen by the [inference rule](edge-inference.md). An edge records which it
was, so that a drawing can show it and a note can be printed only for the
inferred ones.

Explicit edges are named, in `.wic`, and names are shared across the whole
compilation: a name defined in one document may be used in another, as long
as the definition comes first. A reference nothing defines is an error at the
root, once every document has had its chance to define it.

## The boundary

A workflow's boundary is its inputs and outputs. You declare some: `inputs:`
and `outputs:` in `.wic`, `flow.inputs` and `flow.outputs` in Python. The
compiler adds the rest:

- every literal becomes a workflow input, so that the compiled workflow is a
  function of its job file and no value is buried in it;
- every unbound input that inference could not fill becomes a workflow input,
  for the run to supply;
- every step output is exposed as a workflow output, so that nothing a step
  produced is lost; a Python workflow that names outputs keeps only those.

The names the compiler adds are derived from the step and port:
`<step>__<port>`.

## Nesting

A subworkflow is a workflow used as a step. Its boundary is the step's ports:
its declared inputs are what the caller binds, and its outputs, named and
derived, are what later steps may read. Composition is by boundary, and the
model promises that it is transparent: inference and edges cross it, and
splitting a workflow into parts changes the derived names and nothing else.
The property tests hold the compiler to that promise.

## Scatter and conditions

A step may scatter over some of its inputs, in which case each scattered port
takes an array and each output gives one, one level per scattered port under
`nested_crossproduct` and one in all otherwise. Types are compared with those
levels in place. A step may carry a condition, in which case its outputs may
be `null`, and a consumer declares what to do about that.

## Checks, in order

The model is checked in the order it is built, and each layer stops before
the next:

1. **Syntax.** Is the document well formed? Positions are exact and several
   problems are reported at once. A Python workflow skips this layer.
2. **Resolution.** Does every step name a process that exists on the search
   paths, and every binding a port that process declares? This needs the
   tools, so a document can be well formed and still fail here on a machine
   without them.
3. **Linking and inference.** Are the references consistent, the types
   compatible, and which edges does inference add?
4. **The machine.** Before a run only: the container engine, the files, the
   directories, the programs.

Every finding has a code, and [Checks and errors](checks-and-errors.md)
explains how to read them.

## Versions

The language has a version, `lang_version`, and the compiled artifact says
which one it was read under: `0.0.1` today. A file may pin one
(`wic: {lang_version: 0.0.1}`), a compile may set one (`--lang_version`,
`compile(lang_version=...)`), and without either a document is read under the
newest version that accepts it. Two pins that disagree in one compilation are
`wic018`, and an unknown version is `wic017`.

## One model, two forms

The `.wic` parser and the Python API build the same document type, and the
compiler reads only that. This is why the forms are equal for workflows and
why the cookbook can promise that each pair compiles to the same result:
there is one compiler and one model, and the forms differ only in how a
document is spelled. It is also why conversion works in both directions. The
one asymmetry is at the boundary, where a Python workflow that names outputs
asks for only those.

## What the model does not say

The model says nothing about how a process runs: the container, the command
line, the files a tool collects. Those belong to the process, which for a
CommandLineTool is a CWL document, written by hand or with the
[tool builder](tool-builder.md). And it says nothing about the target
language. Today the compiler writes the graph as CWL, and
[Compiling to CWL](compiling-to-cwl.md) describes exactly what it writes.
