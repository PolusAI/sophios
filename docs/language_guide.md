# The Sophios Language Guide

This guide is for writing Sophios workflows as `.wic` files: what a document may
contain, what each piece means, and what the compiler says when something is
wrong. Every mistake the compiler reports carries a code such as `wic025`; this
guide names the code next to each rule, [Error codes](error_codes.md) lists them
all, and `sophios --explain <code>` prints one.

The Python API (`Workflow`, `Step`) builds the same documents, so every rule here
holds for a workflow built in Python too; see the [Python Workflow API](userguide.md)
guide. For the rules an implementation of the language must follow (the CWL support
matrix, the two spellings of each construct, versioning), see the
[language specification](dev/language_spec.md).

Compile a document without running it:

```bash
sophios --yaml workflow.wic --generate_cwl_workflow
```

## 1. A document

A document is a YAML mapping. Every key is optional.

```yaml
wic:            # compiler metadata, never written to the CWL (section 7)
steps:          # the workflow's steps (section 2)
inputs:         # CWL workflow inputs
outputs:        # CWL workflow outputs (section 5)
requirements:   # CWL, and any other CWL key, is copied to the output
```

Sophios reads `steps`, `inputs`, `outputs` and the `wic:` block. Everything else is
CWL and is carried into the compiled workflow as you wrote it. Sophios adds the
requirements the workflow needs (for example `ScatterFeatureRequirement` for a
step that scatters) to a `requirements:` you wrote, and keeps what you wrote. The
[specification](dev/language_spec.md#1-what-sophios-does-with-cwl) lists what Sophios
does with each CWL field.

A document that is not a mapping is `wic002`; a file that is not valid YAML is
`wic001`. A key whose value has the wrong shape is reported where the value is:
`steps: 3` is `wic003` (a mapping was expected), `out: 3` is `wic004` (a sequence
was expected), and a mapping key that is itself a list or a mapping is `wic005`. So
is a key carrying a Sophios tag: a key is a name, and `!ii a: b` is written `a: !ii b`.
A YAML alias that contains itself (`x: !ii &a [*a]`) is `wic030`, unless the key
expects another shape and reports that first (`steps: &a [*a]` is `wic003`).

`inputs`, `outputs`, `requirements` and `hints` may be written in CWL's list form
(`- id: x`, `- class: X`), as may a step's `requirements` and `hints`. They mean
the same as the mapping form. A list that holds an `$import` or `$include` entry is
left exactly as written, for the CWL runner to resolve; Sophios adds no requirement
to such a `requirements:` list, and an `inputs:` or `outputs:` list written that way
is not carried into the compiled workflow. A subworkflow whose ports are written
that way is `wic013`, because no step that calls it can be checked against them.

An empty document parses, but a workflow needs at least one step to compile
(`wic013`).

A file that is already a plain CWL workflow, with `cwlVersion`, `class: Workflow`
and no Sophios syntax, is not a Sophios document. Pass it with `--allow_raw_cwl`
to run it as it is:

```bash
sophios --yaml workflow.cwl --allow_raw_cwl --run_local
```

## 2. Steps

### 2.1 Two ways to write `steps:`

Key the steps by name:

```yaml
steps:
  touch:
    in:
      filename: !ii empty.txt
```

or write a sequence with an `id:` on each step. Use the sequence when the same tool
appears twice, since a mapping cannot repeat a key:

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
  out:
  - file: !& start
- id: append
  in:
    file: !* start
    str: !ii Hello
  out:
  - file: !& hello
- id: append
  in:
    file: !* hello
    str: !ii World
```

Both forms compile to the same workflow. A sequence of single-key mappings is not a
third form:

```yaml
steps:
- touch:            # wic006: the step has no id
    in:
      filename: !ii empty.txt
```

This is CWL's own rule: when `steps:` is a list, each item is a step and its name
comes from `id:`. The compiler says which of the two forms to write instead. An
`id:` that is empty is `wic007`. A step name repeated in the mapping form is
`wic010`, and so is an `id:` inside a step that already takes its name from its
mapping key.

A step may have no body at all. This calls the subworkflow `multistep1.wic`
with nothing bound, leaving its inputs to inference (section 8):

```yaml
steps:
  multistep1.wic:
```

### 2.2 Step keys

| Key | Meaning |
|---|---|
| `in` | input bindings (section 3) |
| `out` | output names and edge definitions (section 5) |
| `run` | the process the step runs (below) |
| `scatter`, `scatterMethod` | scatter over inputs (section 6) |
| `when` | run the step only when the expression is true (section 6) |
| anything else | CWL, copied to the step unchanged |

### 2.3 What a step runs

By default a step runs the tool or subworkflow whose file stem is the step's name:
the step `touch` runs `touch.cwl`, the step `multistep1.wic` runs `multistep1.wic`,
found on the configured search paths. `run:` names it explicitly, in one of three
ways:

- a path to a `.cwl` or `.wic` file, relative to the document that contains the
  step:

  ```yaml
  steps:
  - id: create
    run: tools/make_file.cwl
    in:
      filename: !ii empty.txt
  ```

- a registry stem, as the step name would name it (`run: touch`);
- an inline `CommandLineTool` body:

  ```yaml
  steps:
  - id: shout
    run:
      class: CommandLineTool
      baseCommand: echo
      inputs:
        text:
          type: string
          inputBinding: {position: 1}
      outputs:
        out:
          type: stdout
    in:
      text: !ii HELLO
  ```

A path is used when that file exists; otherwise the value is read as a registry stem.
A stem nothing on the search paths provides is `wic013`. An inline body is written
out as its own tool file next to the compiled workflow, like any other tool, and is
written at the workflow's CWL version: a `cwlVersion` inside the body is ignored, as
CWL requires of a process embedded in a workflow.

A step's process is a tool or a `.wic` subworkflow. A CWL file whose `class` is
`Workflow`, whether a stem on `search_paths_cwl`, a `run:` path or an inline body, is
`wic013`: Sophios cannot embed one as a step. Write it as a `.wic` subworkflow and
call that, or run a CWL workflow file on its own with `--allow_raw_cwl`.

## 3. Inputs

### 3.1 The five forms

Each entry under a step's `in:` binds one input of the step's tool to exactly one of
these:

| Form | Written | Means |
|---|---|---|
| Inline literal | `f: !ii empty.txt` | this value |
| Edge reference | `f: !* name` | the output an `!& name` defines (section 4) |
| Workflow input | `f: some_input` | the workflow input named `some_input` |
| Raw CWL reference | `f: !cwl greeting` | written into the CWL exactly as given |
| Step-input record | `f: !cwl {source: [!* a, b], linkMerge: merge_flattened}` | CWL's step input, with its sources resolved by Sophios |

An input left out of `in:` is connected by inference (section 8).

An input name the step's tool or subworkflow does not declare is `wic028`. An input
with an empty name is `wic027`.

### 3.2 Literals: `!ii`

`!ii` takes any YAML value, not just a scalar:

```yaml
in:
  config: !ii
    pdb_code: 1aki
```

A literal must already have the type of the input it binds; Sophios does not convert
it. `!ii 2.9` on an `int` input is `wic020`, and so is `!ii 1` on a `boolean`
input. Two conversions remain, because neither loses anything:

- an integer on a `float` input is that float (`!ii 1` is `1.0`);
- a number or a boolean on a `string` input is its text, so `!ii 20` binds `"20"`.

Under `!ii` a scalar is read by what it says, whatever its quotes: `!ii '20'` is the
number 20, which is why the second conversion exists. The desugared spelling
(section 3.7) keeps the quotes: `{wic_inline_input: '007'}` is the text `007`, and
on an `int` input it is `wic020`.

YAML reads `1e-5` as text, not as a number: a float needs a decimal point and a
signed exponent. `!ii 1e-5` on a `float` input is `wic020`; write `!ii 1.0e-5`.

A literal of `null` on an input that is not optional is `wic012`.

An untagged mapping or list in an input position is a literal too, since it cannot
name a workflow input; `!ii` is still the clearer spelling. The exception is a
mapping whose keys are all fields of CWL's step input (`source`, `default`,
`valueFrom`, `linkMerge`, `pickValue`, `loadContents`, `loadListing`, `label`):
`filename: {default: a.txt}` is `wic038`, because it reads as CWL that Sophios
does not read untagged. Write `!cwl {default: a.txt}` for the CWL step input, or
`!ii {default: a.txt}` for a literal of that shape.

### 3.3 Workflow inputs

An untagged string names a workflow input. Declare it in `inputs:`:

```yaml
inputs:
  name: string
steps:
  touch:
    in:
      filename: name
```

A name that no workflow input declares is `wic011`; the message asks whether you
meant `!ii` for a literal. A workflow input whose type can never feed the input it
binds (a `string` bound to a `File`) is `wic023`.

### 3.4 Raw CWL: `!cwl`

`!cwl` on a string writes it into the compiled CWL as it is, without checking it.
What it names must exist in the compiled CWL under that name. A workflow input
does. A step name does not: steps are renamed in the compiled CWL, so
`!cwl echo/stdout` points at nothing. To use a step's output, define an edge
(section 4).

### 3.5 Step-input records: `!cwl {...}`

`!cwl` on a mapping is CWL's step input, for what the other forms cannot say:
several sources, a merge, a pick, a `valueFrom`, a default.

```yaml
inputs:
  count: int
steps:
- id: echo
  in:
    message: !ii first
  out:
  - stdout: !& first
- id: echo
  in:
    message: !ii second
  out:
  - stdout: !& second
- id: cat
  in:
    file: !cwl
      source: [!* first, !* second]
      linkMerge: merge_flattened
      pickValue: first_non_null
- id: array_int
  in:
    minval: !cwl {source: count, valueFrom: '$(self + 1)'}
- id: touch
  in:
    filename: !cwl {default: a.txt}
```

`source` is one entry or a list, and each entry is an edge reference (`!* name`) or
a workflow-input name. A record with one source resolves it as it would the same
reference written alone, so it may take an edge its caller defined. A record with
several sources takes each from its own document: an edge that no step of the
document defines is `wic025` there, even when the caller defines it, and the value is
declared in `inputs:` instead (section 4.1). The other fields (`default`, `label`,
`linkMerge`, `loadContents`, `loadListing`, `pickValue`, `valueFrom`) are CWL and are
written out as they are.
Sophios adds the requirements the record needs: `MultipleInputFeatureRequirement`
for several sources or a `linkMerge`, `StepInputExpressionRequirement` for a
`valueFrom`, and `InlineJavascriptRequirement` when the `valueFrom` is an
expression.

These are `wic038`: a key that is not one of those fields; a `source` entry that is
not a reference (`!ii`, `!cwl`); a Sophios tag inside another field; and a record
with no `source`, `default` or `valueFrom`, which gives the input no value.

A record may bind an input that the step's tool does not declare. That is how a
`when:` or a `valueFrom` reads an extra value (section 6). Any other form there is
`wic028`, and so is a record naming an input a called subworkflow does not declare.
A source that a merge, a pick or a `valueFrom` transforms is not checked against the
input's type, since CWL types the input from the transformation.

### 3.6 `!&` is not an input form

`!&` defines an edge, and an edge is defined where its value comes into being: on a
step's `out:` entry (section 5). Anywhere else it is `wic019`:

```yaml
in:
  filename: !& name      # wic019: !& belongs on an out: entry
```

If you meant to use an edge, write `!*`. If you meant to name this step's output,
the `!&` belongs in its `out:` list.

A tag other than `!ii`, `!&`, `!*` and `!cwl` is `wic009`.

### 3.7 The desugared spelling

Each tag has a second spelling, a single-key mapping whose key begins `wic_`. Both
are valid to write by hand, and documents the Python API writes use the tagged one:

| Tagged | Desugared |
|---|---|
| `!ii value` | `{wic_inline_input: value}` |
| `!& name` | `{wic_anchor: name}` |
| `!* name` | `{wic_alias: name}` |
| `!cwl expr` | `{wic_raw_cwl: expr}` |

Where an input value is expected, a single-key mapping whose key begins `wic_` and is
none of these is `wic024`, so a misspelling such as `wic_inline_inpt` is reported
instead of being copied into the CWL. A port or a CWL key may still be named with
the prefix: only the position where a construct could be meant is claimed.

### 3.8 Every name is bound once

A key may appear only once in any mapping Sophios reads: an input in `in:`, a step
name, a `wic:` key, a `wic: steps:` key, and any CWL key alike. Binding twice is
`wic010`, not a last-one-wins:

```yaml
in:
  filename: !ii a
  filename: !ii b     # wic010: input 'filename' is bound more than once
```

## 4. Edges

An edge connects one step's output to a later step's input. `!& name` on an `out:`
entry defines it; `!* name` on an input uses it:

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
  out:
  - file: !& created
- id: cat
  in:
    file: !* created
```

The rules:

- A reference must come after its definition. A `!* created` above the `!& created`
  that defines it is `wic025`, even in the same document.
- A name may be defined once. A second `!& created` is `wic026`: an edge name
  identifies one producer.
- A definition that nothing uses is not an error. It names a result for a consumer
  outside the document.
- Edge names are shared by the whole compilation. A subworkflow may use an edge its
  caller defined before calling it, and a step after a subworkflow call may use an
  edge defined inside that subworkflow. A `!cwl` record with several sources is the
  exception: it takes each source from its own document (section 3.5).
- A reference that no document in the compilation defines is `wic025`, wherever it
  sits: a subworkflow's `!* name` is carried up as an obligation and reported at the
  root when nothing discharges it. A document that expects a value from its includer
  declares a parameter in `inputs:` instead (below).
- An edge whose output type can never feed the input it binds is `wic023`.

### 4.1 A value from outside: declare it in `inputs:`

`!*` asks the compilation for an edge. A document that expects a value from
whoever calls it declares the value in `inputs:` and binds it by its bare name:

```yaml
inputs:
  filename: string
steps:
  touch:
    in:
      filename: filename    # given by the caller, or by the user when run alone
```

The caller binds it like any input of a step:

```yaml
steps:
- id: child
  run: child.wic
  in:
    filename: !ii data.txt
```

A document's `inputs:` is its interface: it says what the document needs, and it
separates a workflow that runs alone from one that is meant to be called.

## 5. Outputs

### 5.1 A step's `out:`

`out:` is a list. Each entry is an output name, or an output name with an edge
definition:

```yaml
out:
- file                    # names the output
- file: !& file_touch     # names it and defines the edge file_touch
```

`out:` is the only place `!&` is allowed (section 3.6).

### 5.2 The workflow's `outputs:`

A workflow output names its producer with `outputSource: <step>/<output>`, the step
named by its id:

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: append
  in:
    str: !ii Hello
outputs:
  greeting:
    type: File
    outputSource: append/file
```

When a step id repeats, name the occurrence by its position, `(index, name)/output`
with a 1-based index:

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
  out:
  - file: !& start
- id: append
  in:
    file: !* start
    str: !ii Hello
  out:
  - file: !& hello
- id: append
  in:
    file: !* hello
    str: !ii World
outputs:
  first:
    type: File
    outputSource: (2, append)/file
```

A step at that index with a different name is `wic039`, and a port that step does not
have is `wic028`, with the step's outputs listed. A position holds in a workflow with
inferred edges too: inference never moves or renumbers a step.

An output with no `type:` takes the type of the output its `outputSource:` names, as
an array when that step scatters. An output with no `type:` and no producer to take
it from is `wic036`. A list `outputSource:`, and `linkMerge` or `pickValue` on a
workflow output, are `wic038`: an output names one step output.

The compiled CWL renames every step to `<workflow>__step__<n>__<id>`. That name
still resolves in `outputSource:`, but it changes when steps move, so the compile
prints a line on stderr naming the spelling to write instead, and carries on:

```text
Warning! o_generated.wic: output 'made' names its step 'o_generated__step__1__touch', a name the compiler generates. Write 'touch/file' instead.
```

A bare id that more than one step has resolves to the first of them. The compile says
so on stderr, with the position that spells it:

```text
Warning! o_repeated.wic: output 'o' has outputSource 'touch/file', but 2 steps have the id 'touch' and it means the first. Write '(1, touch)/file' to say so.
```

An input or output you declare that is spelled like a name the compiler derives for
another port is `wic031`, since the compiled CWL could not tell them apart. So is a
`$namespaces` prefix that the document and the steps whose ports it promotes bind to
different URIs.

## 6. Scatter, when, run

### 6.1 `scatter:`

Each `scatter:` entry names an input of its step, and Sophios adds
`ScatterFeatureRequirement`. A name that is not one of the step's inputs is
`wic032`. A literal bound to a scattered input must be a list:

```yaml
steps:
  touch:
    scatter: [filename]
    in:
      filename: !ii [a.txt, b.txt]    # !ii a.txt here is wic020
```

On a subworkflow call, the inputs are exactly those the subworkflow declares in
`inputs:`. A name the compiler generates for an input of a step inside it, such as
`child__step__1__touch___filename`, is not one of them, and scattering over it is
`wic032`. To scatter over such an input, declare it in the subworkflow and bind the
inner step to it:

```yaml
# child.wic
inputs:
  filename: string
steps:
  touch:
    in:
      filename: filename
```

```yaml
# the caller
steps:
- id: child
  run: child.wic
  scatter: [filename]
  in:
    filename: !ii [a.txt, b.txt]
```

`scatterMethod` takes CWL's values (`dotproduct`, `flat_crossproduct`,
`nested_crossproduct`) and is written into the CWL as given.

### 6.2 `when:`

`when:` is written into the CWL as it is, and Sophios adds
`InlineJavascriptRequirement`:

```yaml
steps:
- id: toString
  in:
    input: !ii 27
  out:
  - output: !& text
- id: echo
  when: '$(inputs.message < "27")'
  in:
    message: !* text
```

CWL evaluates an `inputs.<name>` that the step does not have as `null`, which usually
keeps the step from running. The compile prints a line on stderr when `when:` reads
such a name, and carries on:

```text
Warning! w_undeclared.wic: step 'echo' reads inputs.flag in `when:`, which its process does not declare; CWL evaluates it as null.
```

It prints a different line when the name is one the compiler generates for an input
inside a called subworkflow: such a name resolves only because of how the
subworkflow's steps are laid out. Declare the port in the subworkflow's `inputs:` and
read that name. To give `when:` a value the tool does not take, bind it with a
step-input record (section 3.5):

```yaml
inputs:
  go: boolean
steps:
- id: echo
  when: '$(inputs.flag)'
  in:
    message: !ii hi
    flag: !cwl {source: go}
```

### 6.3 `run:`

`run:` is described in section 2.3.

## 7. The `wic:` block

The `wic:` block holds metadata for the compiler. It is never written to the CWL.

```yaml
wic:
  graphviz:
    label: Make and print a file
  steps:
    (1, touch):
      wic:
        graphviz:
          label: create the file
    cat:
      wic:
        graphviz:
          label: print it
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: cat
```

A key under `wic: steps:` names a step of this document in one of two ways:

- `(index, name)`: the 1-based position of the step and its id;
- `name`: the id of a step that occurs once in the document.

When both spellings name one step, the `(index, name)` entry applies. A key of
neither shape is `wic008`. A key that addresses no step is ignored, and the compile
prints a line on stderr that says which step is at that position:

```text
Warning! s_stale.wic: wic: steps: key (2, touch) addresses no step of 's_stale': step 2 is 'cat'; write (2, cat). The key is ignored.
```

A bare `name` that several steps carry also addresses no step; the line asks for
`(index, name)`.

The block is closed: each key has a fixed meaning and a fixed shape of value.

| Key | Value |
|---|---|
| `steps` | a mapping keyed by `(index, name)` or by a step id that occurs once |
| `graphviz` | a mapping of `label` (a non-empty string), `style` (Graphviz styles, comma separated) and `ranksame` (a list of `(index, name)` keys), each optional |
| `implementation`, `default_implementation`, `version`, `lang_version`, `namespace` | a non-empty string |
| `implementations` | a mapping |
| `driver` | `slurm` or `argo` |
| `inlineable` | `true` or `false` |

An entry under `steps:` may also carry `in`, `out`, `scatter` and `inference`, which
are not checked here, and `scatterMethod`, one of `dotproduct`,
`flat_crossproduct` and `nested_crossproduct`. Any other key is `wic033`, reported
at the key. A value of the wrong shape (`inlineable: sometimes`) is `wic034`,
reported at the value.

A bare `wic:` with nothing under it is an empty block, not an error.

`lang_version` pins the language version the document is read as (see the
[specification](dev/language_spec.md#6-versioning)). A version Sophios does not
know is `wic017`; two pins that disagree within one compilation are `wic018`. A
`cwlVersion:` Sophios cannot run (anything but `v1.0`, `v1.1` or `v1.2`) is
`wic035`. The compiled workflow always says `v1.2`.

`graphviz` changes only the drawing `--graphviz` writes:

- `label` on a step replaces the step's id as the label of its box. Write `\n`
  for a line break. With `--graph_label_stepname` every box shows its generated
  step name instead, a `label` included.
- `label` on a workflow, or on the `wic: steps:` entry that calls it, titles the
  workflow's cluster. A subworkflow without one is titled with the id it is
  called by (`setup.wic`).
- `style` on a step is appended to the box's own `rounded, filled`. On a
  workflow it styles the cluster; `invis` hides the cluster and the box of the
  step that calls it.
- `ranksame` lists steps of this document, as `(index, name)`, to draw on one
  rank. An entry that addresses no step is ignored, with a line on stderr like
  the one for a stale `wic: steps:` key.

## 8. What inference does and how to pin it

An input that no `in:` entry binds is connected by inference: Sophios compares CWL
type and format with the outputs of earlier steps and connects the input to a
compatible one. This is the same mechanism for `.wic` files and for Python: in
Python, leaving a required step input unbound lets the compiler infer it.

1. Look back through the outputs of the earlier steps.
2. Compare CWL type and format.
3. Prefer the most recent compatible output; within one step, the last declared one.
4. When more than one candidate is compatible, Sophios still takes that one, and
   says so with a note on stderr: `wic042` when several outputs of the chosen step
   match, `wic043` when an earlier step also matched. Notes do not fail the compile.
   `--inference_strict` makes them errors.

An input that nothing matches becomes an input of the compiled workflow. Bind it in
`in:` to give it a value in the document.

Here the third step's `file` matches the outputs of both earlier steps:

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: append
  in:
    str: !ii Hello
- id: append
  in:
    str: !ii World
```

```text
i_tie.wic:8:3: note [wic043] step 'append' input 'file' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: file: !* <name>` here (step 3 'append', port 'file')
```

Pin the choice with an edge and the note goes away:

```yaml
- id: append
  in:
    str: !ii Hello
  out:
  - file: !& hello
- id: append
  in:
    file: !* hello
    str: !ii World
```

A port inside a subworkflow is named by the path of steps down to the step that
declares it (`steep.wic/mdrun/output_crd_path`), and its pin goes on that inner step.
For example, in a GROMACS chain, `grompp.input_crd_path` after an `mdrun` that
produces both `output_crd_path` and `output_dhdl_path` is inferred from
`output_crd_path` only because it is declared last; `wic042` names
`output_dhdl_path` as the alternative and the pin to write. In `min.wic`, which
calls `steep.wic` and then `cg.wic`, that pin is `out: - output_crd_path: !& crd` on
`steep.wic`'s `mdrun` step and `in: input_crd_path: !* crd` on `cg.wic`'s `grompp`
step.

`--inference_disable` turns inference off. Naming conventions and per-format
inference rules, set in the configuration file, are described in
[Advanced YAML and Operations](advanced.md#edge-inference). Inference that does not
settle within its iteration limit is `wic022`.

## 9. Error codes you will meet

Errors stop the compile; notes (`wic042`, `wic043`) do not, unless
`--inference_strict` is given. The Python API adds its own `api0NN` codes, for a
call that is wrong although the document is fine.

[Error codes](error_codes.md) lists every code with what it means and how to fix it;
`sophios --explain <code>` prints one.
