# Connecting steps

How do I say which output feeds which input, and which results the workflow
exposes under names of my choosing? An edge connects one step's output to a
later step's input; a workflow output exposes a step output under a name. You
write both when inference would choose wrongly, when the choice matters enough
to be visible, or when a result must have a stable name for whoever runs the
workflow.

## Explicit edges

```{literalinclude} cookbook/explicit_edge.wic
:language: yaml
:caption: explicit_edge.wic
```

```{literalinclude} cookbook/explicit_edge.py
:language: python
:caption: explicit_edge.py
```

In `.wic`, `!& created` on an entry of `touch`'s `out:` list defines an edge
named `created` from the output `file`, and `!* created` on `append`'s input
uses it. In Python, assigning `touch.outputs.file` to `append.inputs.file` is
the edge; the name is internal. Both compile to the same edge
([Compiling to CWL](compiling-to-cwl.md#edges-and-outputs)).

The rules for `.wic` edge names:

- A reference comes after its definition. A `!* created` above the
  `!& created` that defines it is `wic025`, in the same document too.
- A name is defined once. A second `!& created` is `wic026`: an edge name
  identifies one producer.
- A definition nothing uses is not an error. It names a result for a consumer
  outside the document, such as a later step of a caller.
- Edge names are shared by the whole compilation. A subworkflow may use an edge
  its caller defined before calling it, and a step after a subworkflow call may
  use an edge defined inside it. A reference that no document defines is
  `wic025`, reported at the root when nothing in the compilation discharges it.
- `!&` is allowed only on an `out:` entry, since an edge is defined where its
  value comes into being. Anywhere else it is `wic019`; if you meant to use the
  edge, write `!*`.
- An edge whose output type can never feed the input it binds (a `string` into
  a `File`) is `wic023`. Sophios rejects only what it can prove disjoint; see
  [Types and records](types-and-records.md).

The rules for Python, where a port object stands for the edge:

- The source step must come earlier in the workflow's step list than the step
  that reads it, and both must be children of the same `Workflow`; otherwise
  `api002` says which step to move or add.
- A workflow cannot consume one of its own outputs (`api003`), and a string is
  never a reference: `cat.inputs.file = 'touch/file'` binds the text, and a
  list that holds a port object is `api001` ("a port object or a StepInput is
  never a literal").
- Port types are compared as the value is bound, so a mismatch is raised at
  the assignment, with the script line.

A step output may be bound to any number of later inputs.

## Workflow outputs

A workflow output exposes a step output under a name you choose:

```{literalinclude} cookbook/named_output.wic
:language: yaml
:caption: named_output.wic
```

```{literalinclude} cookbook/named_output.py
:language: python
:caption: named_output.py
```

In `.wic`, `outputSource` names the producer as `<step>/<output>`, the step by
its id. When a step id repeats, name the occurrence by its 1-based position,
`(2, append)/file`; a position that holds a different step is `wic039`, and a
port the step does not have is `wic028`, with the step's outputs listed. A
bare id that several steps have resolves to the first of them, and the compile
says so on stderr with the positional spelling to write. The output reaches the
compiled workflow under your name
([Compiling to CWL](compiling-to-cwl.md#edges-and-outputs)).

An output with no `type:` takes the type of the step output it names, as an
array when that step scatters. One with no `type:` and no producer to take it
from is `wic036`. An output names exactly one step output: a list
`outputSource`, or `linkMerge` or `pickValue` on a workflow output, is
`wic038`. To merge several outputs, give them to one step's input as a
[record](types-and-records.md) and expose that step's output.

In Python, `flow.outputs.greeting = echo.outputs.stdout` declares and binds
the output in one line; a workflow input may also be exposed as an output. An
output that is declared and never bound is `api002`.

### What `compile()` returns and what the command line writes

This is the one place the two forms give different CWL. The command line
exposes every step output as a workflow output, named
`<step>__<port>`, whether or not you named any; `named_output.wic` compiles
to `greeting` and `echo__stdout`. When a Python
workflow names outputs, `compile()` returns only those, and `Workflow.run()`
runs that. A Python workflow that names none is compiled exactly as the
command line compiles its `.wic` twin, every step output exposed.

At run time the exposed outputs are what `output_<workflow>.json` lists and
what the runner's output directory holds. A nested workflow's step outputs
are exposed by it and again by its parent, named outputs or not.

## Edges, inference and notes together

An explicit edge binds its input, so inference never touches it. Pinning one
input does not change what inference does for the others, and a pin is also
how you silence the note inference prints when it chose between equal
candidates. [Edge inference](edge-inference.md) has the rule and the exact
pin to write.
