# Inputs and values

How do I give a step a value, and where does a file path point? An input of a
step takes exactly one of five things: a literal written into the workflow, a
workflow input given when the workflow runs, the output of an earlier step, a
raw CWL reference, or a CWL step-input record. This page covers the first two
and the raw reference, and says how values reach the run. Edges have
[their own page](connecting-steps.md), and so do
[records](types-and-records.md).

| Value | `.wic` | Python |
|---|---|---|
| A literal | `filename: !ii empty.txt` | `touch.inputs.filename = 'empty.txt'` |
| A workflow input | `filename: name`, with `name` under `inputs:` | `touch.inputs.filename = flow.inputs.name` |
| An earlier output | `file: !* created` | `cat.inputs.file = touch.outputs.file` |
| A raw CWL reference | `message: !cwl greeting` | no unchecked spelling: a reference is a port object |
| A step-input record | `file: !cwl {source: [!* a, !* b], linkMerge: merge_flattened}` | `cat.inputs.file = StepInput(source=[a.outputs.f, b.outputs.f], link_merge='merge_flattened')` |

An input you give nothing is connected by [edge inference](edge-inference.md).
An input name the step's tool does not declare is `wic028` in `.wic` and
`AttributeError` in Python; only a record may bind one. Binding one input
twice is `wic010` in `.wic`; in Python the later assignment replaces the
earlier one.

## Literals

A literal is a value you know when you write the workflow:

```{literalinclude} cookbook/inline_literal.wic
:language: yaml
:caption: inline_literal.wic
```

```{literalinclude} cookbook/inline_literal.py
:language: python
:caption: inline_literal.py
```

`!ii` takes any YAML value, a mapping or a list as well as a scalar, and a
Python binding takes any value that YAML can hold. The value is not written
into the compiled workflow. It becomes a workflow input named for the step
and port, here `touch__filename`, with its value in
the job file ([Compiling to CWL](compiling-to-cwl.md#inputs-and-the-job-file)).

A literal must already have the type of the input it binds; Sophios converts
nothing, in either form, and reports a mismatch as `wic020`. Two conversions
are made, because neither loses anything: an integer on a `float` input is
that float (`1` is `1.0`), and a number or a boolean on a `string` input is
its text (`20` binds `"20"`). A literal of `null` on an input that is not
optional is `wic012`.

Under `!ii` a scalar is read as YAML reads the same scalar. Unquoted, it is
read by what it says: `!ii 20` is the number 20 and `!ii 007` is the number 7.
Quoted, it is text: `!ii '20'` is the text `20`, and on an `int` input it is
`wic020`. Quote a literal to keep it text; leave it unquoted for a number, a
boolean or a date. YAML reads `1e-5` as text: a float needs a decimal point
and a signed exponent, so write `!ii 1.0e-5`. Python has no such ambiguity;
the type of the value you assign is the type Sophios sees.

An untagged mapping or list in an input position is a literal too, since it
cannot name a workflow input, but `!ii` is the clearer spelling. The one
exception is a mapping whose keys are all fields of CWL's step input
(`source`, `default`, `valueFrom`, `linkMerge`, `pickValue`, `loadContents`,
`loadListing`, `label`): `filename: {default: a.txt}` is `wic038`, because it
reads as CWL that Sophios does not read untagged. Write `!cwl {default: a.txt}`
for the step input, or `!ii {default: a.txt}` for a literal of that shape.

## Workflow inputs

A workflow input is a value the workflow does not fix: whoever runs it, or
whoever calls it as a [subworkflow](subworkflows.md), supplies it.

```{literalinclude} cookbook/workflow_input.wic
:language: yaml
:caption: workflow_input.wic
```

```{literalinclude} cookbook/workflow_input.py
:language: python
:caption: workflow_input.py
```

In `.wic`, an untagged string names a workflow input, which `inputs:`
declares with its type. A name that `inputs:` does not declare is `wic011`,
and the message asks whether you meant `!ii`. In Python, `flow.inputs.<name>`
is the input; the first binding declares it, with the type of the port it
feeds, and `flow.inputs.<name>.as_type('boolean')` declares a type when no
port gives one. A workflow input whose type can never feed the input it binds
(a `string` into a `File`) is `wic023` in `.wic` and `api003` in Python.

The input keeps its name in the compiled CWL, `filename` here, and nothing
about it goes in the job file. The run needs the value from you.

### Giving values at run time

An inputs file is a YAML mapping of workflow input names to values:

```yaml
# job.yml
filename: from_inputs_file.txt
```

```bash
sophios --yaml workflow_input.wic --inputs_file job.yml --run_local
```

```python
from workflow_input import workflow

workflow().run(run_args_dict={'inputs_file': 'job.yml'})
```

Its entries are merged into the job file Sophios writes, by name. The name of
a declared workflow input is the name you gave it; the name of a step literal,
should you want to override one, is the generated `<step>__<port>` name from
the job file. An inputs file value wins over a literal of the same name. A
file that is not YAML is `wic001`; one whose top level is not a mapping is
`wic002`.

Sophios does not check that every workflow input has a value: a declared input
with none is left to the runner, which stops the run with its own message
(`Missing required input parameter`). For a plain CWL workflow under
`--allow_raw_cwl`, the inputs file is the job file, handed to the runner as it
is.

A Python `Step` can also take its values from a file:
`Step(clt_path='cwl_adapters/cat.cwl', step_inputs_file='cat_inputs.yml')`
binds each entry of that YAML mapping as if you had assigned it.

## Files and directories

A `File` or `Directory` value is a mapping with a `class` and a `location`
(or `path`), not a bare path:

```{literalinclude} cookbook/cat_file.wic
:language: yaml
:caption: cat_file.wic
```

```{literalinclude} cookbook/cat_file.py
:language: python
:caption: cat_file.py
```

The two forms resolve a relative path differently, because each reads it from
where the value was written:

- a path in a `.wic` file is read from that file's directory;
- a path in an `--inputs_file` or a Python `inputs_file` is read from the
  inputs file's directory (CWL's rule for a job file);
- a path bound in Python, or read from a `step_inputs_file`, is read from the
  working directory, as Python reads one.

Before a run, Sophios writes every such path absolute into the job file, so
the runner finds the same file wherever it reads the job from, and checks
each one: a path that does not exist, names a directory where a `File` was
declared (or the reverse), or is not readable is a `wic016` line naming the
input and where Sophios looked:

```text
error [wic016] input 'cat__file' (from the workflow, whose relative paths are read from /.../sophios-examples) names 'data.txt', which does not exist at /.../sophios-examples/data.txt: correct the path, or create the file.
```

The check covers `File` and `Directory` values at any depth: in lists, in
records, in a File's `secondaryFiles` and in a Directory's `listing`. A
`location` that is a URL (`https://...`) is left to the runner. A literal
`File` with `contents` and no `location` is a file the runner creates, and is
not checked.

The runner stages each input file read-only in a directory of its own. A tool
that writes next to its input, as `append.cwl` does, declares an
`InitialWorkDirRequirement` that copies the file into the working directory
first; [The CWL you still need](cwl-you-still-need.md) explains that and the
other rules a tool's files follow.

## Raw CWL references

`!cwl` on a string writes it into the compiled CWL exactly as given, without
checking it:

```{literalinclude} cookbook/raw_cwl.wic
:language: yaml
:caption: raw_cwl.wic
```

```{literalinclude} cookbook/raw_cwl.py
:language: python
:caption: raw_cwl.py
```

What the string names must exist in the compiled CWL under that name. A
workflow input does. A step output is not one to rely on: Sophios does not
read the string, so `!cwl echo/stdout` is no edge it knows of, and a step is
numbered in the compiled CWL once a second step has its id (`echo_1`), which
leaves the string pointing at nothing. To use a step's output,
[define an edge](connecting-steps.md). Python has no
unchecked spelling: a reference is always a port object, so the Python twin
of `!cwl greeting` is the workflow input `flow.inputs.greeting`, and both
write the same line, `message: greeting`.

`!cwl` on a mapping is a different construct, the step-input record; see
[Types and records](types-and-records.md).

## The desugared spelling

Each `.wic` tag has a second spelling, a single-key mapping whose key begins
`wic_`, for tools that cannot write YAML tags and for JSON:

| Tagged | Desugared |
|---|---|
| `!ii value` | `{wic_inline_input: value}` |
| `!& name` | `{wic_anchor: name}` |
| `!* name` | `{wic_alias: name}` |
| `!cwl expr` | `{wic_raw_cwl: expr}` |

Both are valid to write by hand and mean the same. Where an input value is
expected, a single-key `wic_` mapping that is none of these is `wic024`, so a
misspelling such as `wic_inline_inpt` is reported rather than copied into the
CWL. Only that position is claimed: a port or a CWL key may still be named
with the prefix.
