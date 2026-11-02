# The workflow model

You have written the greeting in two forms, watched it grow into a graph,
and seen Sophios compile both forms to the same CWL. This chapter explains
what both forms describe: Sophios's **workflow model**. You do not need it
to write workflows. Read it when you want to know why a message says what it
says, why the order of your steps matters for some things and not for
others, or what Sophios could write besides CWL.

## A workflow is a graph

Here again is the greeting graph from [Connecting steps](connecting-steps.md),
as Sophios draws it:

![The greeting graph: append feeds shout and word_count, which both feed combine](cookbook/greeting_graph.wic.gv.png)

The boxes are **steps** and the arrows are **edges**: each edge carries one
output of one step to one input of another. The arrows point one way, from
the step that makes a value to the step that uses it, and they never lead
back to where they started. That makes the workflow a **directed acyclic
graph**, a DAG. A straight line of steps, like the first greeting, is the
simplest DAG; branches, where one output feeds several steps, and joins,
where one step reads several outputs, are what make real analyses graphs.

The model has four parts, and the drawing shows three of them. The fourth,
the boundary, is what the workflow as a whole takes and returns.

## Steps

A step runs one process: a CWL `CommandLineTool`, or another workflow used
as a step ([Subworkflows](subworkflows.md)). A step has an identity, its
position in the list and its name, which is why one tool may appear twice
under two names, and why a note says `step 3 'shout'` rather than `shout`.
The name is the tool's file stem unless you give one.

The list of steps you write is ordered, and the order matters for exactly
two things. A step may read only from steps before it in the list, and
[edge inference](edge-inference.md) looks backwards through the list for an
input's source. The order does not decide when a step runs;
[a later section](#when-steps-run-and-what-runs-at-the-same-time) explains
what does.

## Ports

Every process declares its inputs and outputs, each with a type and, for a
file, perhaps a file format. A step's ports are its process's: `shout` has
an input port `text`, a `File`, and an output port `shouted`, a `File`.
Sophios reads ports from the tool or the subworkflow and never invents one,
with one exception: a [step-input record](types-and-records.md) bound to a
name the tool does not declare adds an input of type `Any` to that step, so
that a `when:` or a `valueFrom` can read it.

Ports are where every check happens. A value is checked against the type of
the port it is given to, an edge against the types at both ends, a scatter
against the ports it names. That is why a message names the step and the
port, as in `(step 3 'shout', port 'text')`.

## Edges, and what an input can take

Each input port takes exactly one of these:

- a value written into the workflow (`!ii`, or an assignment in Python);
- a workflow input, given when the workflow runs;
- an edge from an earlier step's output;
- a step-input record, which merges several sources, picks among them, or
  transforms or defaults a value;
- nothing, which edge inference may fill.

Each has one spelling in `.wic` and one in Python, and the model does not
remember which was used: a `!*` and a Python assignment are the same edge.
Edges come from two places, the ones you wrote, which are placed first, and
the ones inference adds for each required input still unconnected. Each edge
records which it was, which is how a drawing colours them and why a note is
printed only for an inferred one.

## The boundary

A workflow's boundary is what it takes and what it returns. You declare
some of it: `inputs:` and `outputs:` in `.wic`, `flow.inputs` and
`flow.outputs` in Python. The compiler adds the rest:

- every value written into the workflow becomes a workflow input, with its
  value in the job file, so that the compiled workflow holds no data;
- every required input that nothing connects, and inference could not fill,
  becomes a workflow input for the run to supply;
- every step output is exposed as a workflow output, so that nothing a step
  made is lost. A Python workflow that names outputs keeps only those:
  `Workflow.compile()` returns them, and `Workflow.run()`, `start()` and
  `run_async()` run the same document. The command line keeps them all.

The names the compiler adds are made from the step and the port,
`<step>~<port>`.

## Why the graph has no cycles

A step may read only from steps listed before it, so an edge always points
from earlier in the list to later, and no path of edges can return to the
step it left. The graph is acyclic by construction. CWL describes workflows
as acyclic graphs too, and the reason is the same for both: a runner can
then always find an order in which every step's inputs exist before it
starts, and a run always ends.

Reading from a later step is therefore an error, in either form. In Python
the error points at the line that created `cat`:

```text
backwards.wic:4:11: error [wic025] '!* greeting_file' is referenced before '!& greeting_file' defines it. (step 1 'cat', port 'file')
```

```text
sophios.api.python._errors.InvalidStepError: /home/you/sophios-tutorial/backwards.py:5:1: error [api002] cat.file is linked to 'touch', which must appear earlier in the workflow step list
```

`InvalidStepError`, like the other API errors, is importable from
`sophios.api.python.workflow`.

The fix is to move the step that makes the value above the step that reads
it. To repeat a step over many inputs, which a loop does in a script, use a
[scatter](scatter-and-conditions.md): the step runs once per element, and
the graph stays acyclic.

## When steps run, and what runs at the same time

Sophios does not run steps itself; the runner does. The runner starts a
step once every input it needs exists, so the order comes from the edges.
In the greeting graph, `shout` and `word_count` both wait only for
`append`, and neither waits for the other. [Connecting
steps](connecting-steps.md) showed cwltool running them in either order
from one run to the next, whatever their order in the file.

cwltool, the default runner, runs one step at a time, in an order the edges
allow. Toil, chosen with `--cwl_runner toil-cwl-runner`, can run steps that
do not depend on each other at the same time, on the cores of one machine or
on the nodes of a cluster ([Running workflows](running.md)). The jobs of a
scattered step are independent of each other in the same way. The graph is
what makes this safe: two steps with no path of edges between them cannot
see each other's results, so running them in either order, or together,
gives the same result.

## Workflows inside workflows, scatter and conditions

A workflow used as a step is a step like any other: its declared inputs are
the step's input ports, and its outputs, named and generated, are the
step's output ports. Edges and inference cross the boundary of the inner
workflow as if its steps were written in place, so splitting a workflow into
parts changes the generated names and nothing else.

A step may scatter over some of its inputs. Each scattered port then takes
an array and each output gives one, one array level per scattered port under
`nested_crossproduct` and one level in all otherwise, and types are compared
with those levels in place. A step may carry a condition, a `when:`; its
outputs may then be `null`, and a step that reads them has to say what to do
about that ([Scatter and conditions](scatter-and-conditions.md)).

## From the model to a runner: backends

The model belongs to Sophios, not to any workflow language. Both forms build
it, the compiler completes it, with inference, and checks it, and then a
**backend** writes it out in a language a runner understands. That split is
what makes Sophios multi-backend: the model and its checks would stay the
same for another backend. There are two, CWL and [Nextflow](nextflow.md),
and [Compiling to CWL](compiling-to-cwl.md) reads, line by line, what the
CWL one writes.

## Checks, in order

The model is checked in the order it is built, and each layer stops before
the next:

1. The syntax: is the document well formed? Positions are exact and several
   problems are reported at once. A Python workflow skips this layer.
2. Resolution: does every step name a process that exists on the search
   paths, and every binding a port that process declares? This needs the
   tools, so a document can be well formed and still fail here on a machine
   without them.
3. Linking and inference: are the references consistent and the types
   compatible, and which edges does inference add?
4. The machine, before a run only: the container engine, the files, the
   directories, the programs.

Every finding has a code, and [Checks and errors](checks-and-errors.md)
explains how to read them.

## Versions

The language has a version, `lang_version`, and the compile reports which
one it read a document under, `0.0.1` today: printed as
`Sophios lang_version: 0.0.1` on the command line, and as
`CompiledWorkflow.lang_version` in Python. The compiled CWL does not carry
it. A file may pin one (`wic: {lang_version: 0.0.1}`), a compile may set one
(`--lang_version`, `compile(lang_version=...)`), and without either a
document is read under the newest version that accepts it. Two pins that disagree in one
compilation are `wic018`, and an unknown version is `wic017`.

## One model, two forms

The `.wic` parser and the Python API build the same document type, and the
compiler reads only that. This is why the forms are equal for workflows and
why the cookbook can promise that each pair compiles to the same result:
there is one compiler and one model, and the forms differ only in how a
document is spelled. It is also why conversion works in both directions.

## What the model does not say

The model says nothing about how a process runs: the container, the command
line, the files a tool collects. Those belong to the process, which for a
CommandLineTool is a CWL document, written by hand or with the
[tool builder](tool-builder.md).

## Recap

A Sophios workflow is a directed acyclic graph: steps, the ports on them,
the edges between ports, and a boundary of workflow inputs and outputs. A
step reads only from steps before it, which keeps the graph acyclic, and the
runner, not the list, decides when each step runs. Both forms build that one
model, which is why they are equal; the compiler completes it, with
inference, and checks it in layers; and a backend, today the CWL backend,
writes it out for a runner.

## Next

[Compiling to CWL](compiling-to-cwl.md) reads, line by line, what the CWL
backend writes for the greeting workflow.
