# Scatter and conditions

How do I run a step once per element of an array, and how do I run a step only
when a condition holds? A step may scatter over some of its inputs, and a
step may carry a condition. Sophios carries both into the compiled workflow
and checks what can be checked before anything runs: that a scattered name is
an input of the step, that its literal is a list, that the step's outputs are
typed as arrays downstream, and that the expression of a condition reads
something the step has.

## Scatter

```{literalinclude} cookbook/scatter.wic
:language: yaml
:caption: scatter.wic
```

```{literalinclude} cookbook/scatter.py
:language: python
:caption: scatter.py
```

The step runs once per combination of the two arrays, four times here, and
its output `stdout` is an array of `File`. [Compiling to CWL](compiling-to-cwl.md#scatter-and-conditions)
shows the compiled step.

In `.wic`, `scatter:` lists the step's inputs to scatter over and
`scatterMethod:` is one of `dotproduct`, `flat_crossproduct` and
`nested_crossproduct`, carried through as written. In Python,
`step.scatter_on(step.inputs.a, step.inputs.b, method='flat_crossproduct')`
does the same; `method` defaults to `dotproduct`. One scattered input with
`dotproduct` walks its array.

The rules:

- A `scatter:` entry that is not an input of the step is `wic032`. On a
  subworkflow call the inputs are those the subworkflow declares in
  `inputs:`; a name the compiler generates for an input inside it is not one
  of them. To scatter a call over an inner step's input, declare the input in
  the subworkflow and bind the inner step to it.
- A literal bound to a scattered input must be a list: `!ii a.txt` on a
  scattered `string` input is `wic020`; write `!ii [a.txt, b.txt]`.
- A scattered input that nothing binds, once inference is done, is `wic032`:
  a scatter needs a value to split.
- The step's outputs are arrays. Under `nested_crossproduct` each scattered
  input adds one array level; the other methods add one in all. A later step
  may scatter over such an output, and inference compares types with these
  levels in place, so a scattered consumer of a scattered producer is inferred
  without a pin.
- In Python, `scatter_on` checks at the call that each input belongs to this
  step, is bound, and is bound to array-valued data, so bind an input before
  scattering over it, and scatter a producer before the step that scatters
  over its output.

## Conditions

```{literalinclude} cookbook/when.wic
:language: yaml
:caption: when.wic
```

```{literalinclude} cookbook/when.py
:language: python
:caption: when.py
```

`when:` is an expression over the step's inputs, carried through as written;
the step runs only when it is true. In Python,
`step.when = '$(inputs.enabled)'` sets it, and a value that does not start
with `$(` and end with `)` is `ValueError`.

The expression here reads `inputs.enabled`, which `echo.cwl` does not declare.
A tool's inputs are fixed, so the value reaches the step through a
[step-input record](types-and-records.md) bound to a name of your choosing:
`enabled: !cwl {source: loud}` in `.wic`, and
`echo.inputs.enabled = StepInput(source=flow.inputs.loud.as_type('boolean'))`
in Python. A record that feeds an input the tool does not declare gives the
workflow input no type, which is why the `.wic` declares `loud` under
`inputs:` and the Python calls `.as_type('boolean')`.

What a `when:` reads:

- The runner evaluates an `inputs.<name>` that the step does not have as
  `null`, which usually keeps the step from running. The compile prints a line on
  stderr when a `when:` reads such a name, and carries on.
- A name the compiler generates for an input inside a called subworkflow
  resolves, but only because of how the subworkflow's steps are laid out; the
  compile prints a different line asking you to declare the port in the
  subworkflow's `inputs:` and read that.

A step that did not run gives `null` for each of its outputs, and the runner
warns that a source from a conditional step may produce `null`.
A later step that reads such an output declares how to treat it with
`pickValue` in a record (`first_non_null`, `the_only_non_null`,
`all_non_null`), or declares the input optional. Under
`--partial_failure_enable`, Sophios itself writes a `when:` on every step that
has required inputs, and it replaces a `when:` you wrote on that step: for
`cookbook/when.wic` the compiled step reads
`when: $(inputs["message"] != null && inputs["enabled"] != null)`. A step with
no required inputs keeps its own `when:`; see
[Partial failures](partial-failures.md).
