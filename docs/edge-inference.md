# Edge inference

When I leave an input unbound, what does Sophios connect it to, and how do I
steer or stop it? Inference fills in the edges a linear pipeline would make
you write by hand: each required input that nothing binds is connected to the
most recent earlier output that fits. This page gives the exact rule, the
notes it prints, how to pin a choice, the two refinements the configuration
adds, and how to turn it off.

## Why it exists

Most pipelines are chains. Each step reads what the previous one wrote, and
spelling that out as an edge per input says nothing a reader did not already
know. Inference lets a workflow say only what is not obvious, the literals and
the few edges that go against the grain, and reports every choice it had to
make between equals, so that a wrong guess is visible before anything runs.

## An example

```{literalinclude} cookbook/inference.wic
:language: yaml
:caption: inference.wic
```

```{literalinclude} cookbook/inference.py
:language: python
:caption: inference.py
```

`cat`'s input `file` is unbound, and two earlier outputs are of type `File`:
`touch`'s `file` and `echo`'s `stdout`. Sophios takes the most recent, and
says so with a note:

```text
inference.wic:8:3: note [wic043] step 'cat' input 'file' was inferred from the most recent match step 2 'echo' output 'stdout'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - stdout: !& <name>` on step 2 'echo' and `in: file: !* <name>` here (step 3 'cat', port 'file')
```

The command line prints it on stderr; in Python it is in
`compiled.diagnostics`, positioned at the line that made the step
(`cat = Step(...)`). The inferred edge is written like one you wrote
([Compiling to CWL](compiling-to-cwl.md#edges-and-outputs)).

## The rule

For each step in order, for each of its inputs that is required and that
nothing binds (no literal, no workflow input, no edge, no record):

1. Look at the earlier steps, from the most recent backwards. Within a step,
   look at its outputs from the last declared to the first.
2. An output matches when its type matches the input's type and its format
   matches the input's format. Types match when they are equal, or when one is
   a union that contains the other. `Any` matches only `Any`. A scattered
   step's outputs are compared as arrays, and an input the step scatters over
   is compared as an array of its declared type, so a step that scatters over
   a scattered producer's output matches without a pin. An output whose name
   contains `_log_` never matches.
3. The input's format is checked when the input declares one: an output with
   a format matches if one of its formats is one of the input's; an output
   with none matches only if its type cannot carry a format, which is to say
   it is not a `File` or an array of them. An input with no format accepts any
   format.
4. The first step with a match wins, and within it the last declared matching
   output. The scan goes on past the winner only to collect the earlier
   matches, so that the choice can be reported; it never changes the choice.
5. When nothing matches, the input becomes an input of the compiled workflow,
   named `<step>___<port>`, for the run to supply. Bind it in the workflow to
   give it a value there.

Inference runs after explicit edges are placed and after each subworkflow has
been compiled, and it crosses subworkflow boundaries: a step after a
subworkflow call sees the outputs the subworkflow exposes, and a step inside a
subworkflow sees what came before the call. A port reached that way is named
by the path of steps down to the one that declares it, as in
`step 1 'child' > step 2 'append' output 'file'`. Inference never moves or renumbers a step,
so a position in a `wic: steps:` key or an `outputSource` holds.

Optional inputs are never inferred. Make a tool's input required if a
workflow should connect it without being asked.

## The notes, and how to pin a choice

Inference reports a choice between equal candidates with a note, which does
not stop the compile:

- `wic042`: the chosen step offered several matching outputs, and the last
  declared one was taken;
- `wic043`: an earlier step also matched, and recency decided.

Each note ends with the pin that makes the choice explicit: `!&` on the
chosen (or the other) output and `!*` on the input, as in
[Connecting steps](connecting-steps.md). In Python the pin is the assignment
`cat.inputs.file = touch.outputs.file`. A pinned input is bound, so inference
does not look at it and no note is printed. For a port inside a subworkflow
the pin goes on the inner step that declares it, and the note says which.

`--inference_strict` turns both notes into errors, for a workflow that must
not depend on step order. It is a command-line flag; `Workflow.compile()`
takes no inference settings, and compiles with the defaults.

## Naming conventions

When several outputs of one step match by type, their names can decide.
`cwl_adapters/ambiguous.cwl` has two `string` outputs, `uniquename1` and
`uniquename2`, each returning the input of the same name. Called twice, the
second call's `uniquename1` is inferred from the first call's `uniquename2`,
the last declared match:

```{literalinclude} tutorials/naming_conventions.wic
:language: yaml
:caption: naming_conventions.wic
```

![Inferred without naming conventions](tutorials/naming_conventions.wic_no_flag.png)

Pinning both edges fixes it:

```{literalinclude} tutorials/naming_conventions_explicit.wic
:language: yaml
:caption: naming_conventions_explicit.wic
```

So does `--inference_use_naming_conventions`, with the original file:

![Inferred with naming conventions](tutorials/naming_conventions.wic_with_flag.png)

With the flag, among the outputs that match by type, the one whose name pairs
with the input's name is taken when exactly one does. The pairing strips a
leading `input_` from the input's name and `output_` from the output's, and
applies the `renaming_conventions` of the [configuration](configuration.md)
to the input's name, each a `[before, after]` replacement; the configuration
Sophios writes pairs `input_energy_path` with `output_edr_path`, for
example. When no output or several pair with the name, the rule above
applies and a note is printed.

## Format rules

`inference_rules` in the configuration maps a format to `break` or
`continue`. When the scan meets an output whose format has the rule `break`,
it finishes that step's outputs and goes no further back: an input that
matched nothing up to there becomes a workflow input rather than reaching an
older step. Use it for a format that marks a boundary, such as a final
structure that a later step should never read past. `continue` is the
default and changes nothing. The command line reads the rules, and the
`renaming_conventions` above, from the configuration; `Workflow.compile()`
reads no configuration, so a Python compile has neither.

## Inserting a converter step

`--insert_steps_automatically` lets inference insert one step when nothing
matches by format. The candidates are the tools on your search paths whose
stem starts with `insert_steps_automatically_`. One is inserted before the
step with the unmatched input when it accepts the format of an output the
scan looked at and produces the format the input wants; when several qualify,
the first by namespace and name is taken. The inserted step is then inferred
like any other, and the names map (`autogenerated/<workflow>.names.json`)
marks it as inserted. At most one step is inserted per unmatched input, and a
compile that keeps inserting without settling stops after 100 passes with
`wic022`.

## Turning it off

`--inference_disable` compiles without inference. An input that nothing
binds then stays unbound in the compiled CWL, and the runner reports it as
missing; Sophios adds no workflow input for it. Use it to see exactly what a
document says, or for a workflow whose every edge must be written.

## What inference does not do

- It does not choose by name unless `--inference_use_naming_conventions` is
  given.
- It does not convert types or formats; see
  [Types and records](types-and-records.md) for what matches what.
- It does not run for an input that is bound in any way, nor for an optional
  input.
- In Python, it cannot be configured: `compile()` and `run()` infer with the
  defaults. To use naming conventions, strict mode or converter insertion,
  write the workflow out with `write_wic()` and compile it on the command
  line.
