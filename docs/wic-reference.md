# The `.wic` reference

What may a `.wic` document contain, what does each piece mean, and what does
the compiler say when something is wrong? This page is the complete
reference for the file form, section by section; the pages under **Write
workflows** explain the same constructs with examples in both forms. Error
codes name their section here, as `§3.5`, and [Error codes](error_codes.md)
lists them all.

Compile a document without running it with
`sophios --yaml workflow.wic --generate_cwl_workflow`.

Each section opens with why the construct or rule exists, gives an example
that compiles, lists the codes it raises with a one-line fix, and links the
guide chapter that teaches it with a running example in both forms.
`sophios --explain <code>` prints any code's meaning and fix.

## 1. A document

A `.wic` file is a CWL workflow with the bookkeeping left out: you write the
steps and what each one takes, and Sophios writes the rest. Anything Sophios
does not read passes through as CWL, so every CWL feature stays available
without Sophios having to know about it.

A document is a YAML mapping. Every key is optional.

```yaml
wic:            # compiler metadata, never written to the CWL (§7)
steps:          # the workflow's steps (§2)
inputs:         # CWL workflow inputs (§3.3)
outputs:        # CWL workflow outputs (§5.2)
requirements:   # CWL; this and any other CWL key is copied to the output
```

Sophios reads `steps`, `inputs`, `outputs` and the `wic:` block. Everything
else is CWL and is carried into the compiled workflow as written. Sophios
adds the requirements the workflow needs to a `requirements:` you wrote and
keeps what you wrote; the [language specification](dev/language_spec.md)
lists what happens to each CWL field.

A document that is not a mapping is `wic002`; a file that is not valid YAML
is `wic001`. A key whose value has the wrong shape is reported where the value
is: `steps: 3` is `wic003` (a mapping was expected), `out: 3` is `wic004` (a
sequence was expected), and a mapping key that is itself a list or a mapping
is `wic005`. So is a key carrying a Sophios tag: a key is a name, and
`!ii a: b` is written `a: !ii b`. A YAML alias that contains itself is
`wic030`, unless the key expects another shape and reports that first.

`inputs`, `outputs`, `requirements` and `hints` may be written in CWL's list
form (`- id: x`, `- class: X`), as may a step's `requirements` and `hints`,
and mean the same as the mapping form. A list that holds an `$import` or
`$include` entry is left exactly as written for the runner to resolve:
Sophios adds no requirement to such a `requirements:` list, and an `inputs:`
or `outputs:` list written that way is not carried into the compiled
workflow. A subworkflow whose ports are written that way is `wic013`, because
no step that calls it can be checked against them.

An empty document parses, but a workflow needs at least one step to compile
(`wic013`).

A small complete document, with a workflow input, a step, a named output and
a CWL key, `doc:`, that Sophios passes through:

```yaml
doc: Make an empty file whose name is given when the workflow runs.
inputs:
  name: string
steps:
- id: touch
  in:
    filename: name
outputs:
  made:
    type: File
    outputSource: touch/file
```

| Code | When | Fix |
|---|---|---|
| `wic001` | the file is not valid YAML | correct the YAML at the position shown |
| `wic002` | the top level is not a mapping | make it a mapping with keys such as `steps:` |
| `wic003` | a mapping was expected, as in `steps: 3` | write `key: value` entries there |
| `wic004` | a sequence was expected, as in `out: 3` | write a list there |
| `wic005` | a key is a list or a mapping, or carries a tag | write one name; a tag goes on the value, `a: !ii b` |
| `wic030` | a YAML alias contains itself | remove the alias that refers back to its own anchor |
| `wic013` | the workflow has no steps | give it at least one step |

The guide's [Your first workflow](first-workflow.md) builds a first document
line by line.

A file that is already a plain CWL workflow is not a Sophios document; pass it
with `--allow_raw_cwl` to run it as it is ([Steps and tools](steps-and-tools.md)).

## 2. Steps

### 2.1 Two ways to write `steps:`

Key the steps by name:

```yaml
steps:
  touch:
    in:
      filename: !ii empty.txt
```

or write a sequence with an `id:` on each step. The sequence is the form to
use when the same tool appears twice, since a mapping cannot repeat a key:

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

Two forms exist because each is best at something: the mapping is the
shortest way to write steps that are all different, and the sequence is the
only way to write the same tool twice, since a YAML mapping cannot repeat a
key. Both forms compile to the same workflow. A sequence of single-key mappings
(`- touch:` with the body under it) is not a third form: it is `wic006`, and
the message names the two forms to write instead. An `id:` that is empty is
`wic007`. A step name repeated in the mapping form is `wic010`, and so is an
`id:` inside a step that already takes its name from its mapping key.

A step may have no body. `multistep1.wic:` under `steps:` calls that
subworkflow with nothing bound, leaving its inputs to inference.

A step id, a workflow input or output name, and the name of the `.wic` file
itself are letters, digits, `_`, `.` and `-`, and do not start with `.`;
anything else is `wic045`. These names become ids in the compiled CWL and
parts of its file names, and the names Sophios makes join them with `~` and
number them with `@` ([Compiling to CWL](compiling-to-cwl.md#step-names)),
so a name you write can never be mistaken for one Sophios made. In the
compiled CWL a step keeps its id, less a `.wic`; in the sequence above the
two `append` steps become `append@2` and `append@3`, after their positions.
A tool's own port names are not checked.

| Code | When | Fix |
|---|---|---|
| `wic006` | a step in a sequence has no `id:` | add `id:`, or write `steps:` as a mapping |
| `wic007` | a step's `id:` is empty | write the tool or workflow the step runs |
| `wic010` | a step name is repeated in the mapping form, or a step has both a key and an `id:` | use the sequence form, or keep one name |
| `wic045` | a step id, workflow input or output, or the workflow's name has a character other than letters, digits, `_`, `.` and `-`, or starts with `.` | rename it with those characters |

[Steps and tools](steps-and-tools.md) uses one tool twice under two names.

### 2.2 Step keys

A step has few keys of its own because everything else about it is CWL.

| Key | Meaning |
|---|---|
| `in` | input bindings (§3) |
| `out` | output names and edge definitions (§5.1) |
| `run` | the process the step runs (§2.3) |
| `scatter`, `scatterMethod` | scatter over inputs (§6.1) |
| `when` | run the step only when the expression is true (§6.2) |
| anything else | CWL, copied to the step unchanged |

Sophios reads only the keys that say where values come from and what runs.
Every other key, such as a step's `label:` or `requirements:`, is copied to
the compiled step as written.

### 2.3 What a step runs

By default a step runs the tool or subworkflow whose file stem is the step's
name: `touch` runs `touch.cwl` and `multistep1.wic` runs `multistep1.wic`,
found on the configured search paths. `run:` names it explicitly, in one of
three ways:

- a path to a `.cwl` or `.wic` file, relative to the document that contains
  the step (`run: tools/make_file.cwl`); a value ending in `.cwl` or `.wic`
  is a path, and when no file is there it is read as a stem;
- a registry stem, as the step name would name it (`run: touch`);
- an inline `CommandLineTool` body (`run: {class: CommandLineTool, ...}`).

A `.cwl` file, on a search path or named by `run:`, is one `CommandLineTool`,
or a packed document (`$graph`) whose `main` is one: the step runs `main`. A
packed document with no `main` is `wic013`. A stem nothing on the search
paths provides is `wic013`. An inline body is written out as its own tool file
beside the compiled workflow, at the workflow's CWL version; a `cwlVersion`
inside it is ignored.

A step's process is a tool or a `.wic` subworkflow. A CWL file whose `class`
is `Workflow`, however it is named, is `wic013`: write it as a `.wic`
subworkflow and call that, or run it on its own with `--allow_raw_cwl`.

Naming a process by stem keeps a workflow short and lets it run on machines
whose tools live in different directories; a path suits a tool kept beside
the workflow; an inline body suits a one-off. `run:` is also how one tool
runs under a step name of your choosing:

```yaml
steps:
- id: make_file
  run: touch
  in:
    filename: !ii greeting.txt
```

| Code | When | Fix |
|---|---|---|
| `wic013` | no search path provides the stem, the file is a CWL `Workflow`, a packed document has no `main`, subworkflows call each other in a cycle, or a subworkflow's ports are written in list form with `$import` or `$include` | put the tool on a search path, name it by path, call a `.wic` subworkflow instead, break the cycle, or write the ports as a mapping |

[Steps and tools](steps-and-tools.md) shows each way of naming a tool.

A step that runs the `cwl_subinterpreter` adapter is not a step of the
compiled workflow but a real-time analysis declaration, checked as such and
reported as `wic044` when it is malformed or its analysis does not compile;
[Real-time analysis](realtime-analysis.md) describes it.

## 3. Inputs

### 3.1 The five forms

Each entry under a step's `in:` binds one input of the step's process to
exactly one of:

| Form | Written | Means |
|---|---|---|
| Inline literal | `f: !ii empty.txt` | this value |
| Edge reference | `f: !* name` | the output an `!& name` defines (§4) |
| Workflow input | `f: some_input` | the workflow input named `some_input` |
| Raw CWL reference | `f: !cwl greeting` | written into the CWL exactly as given |
| Step-input record | `f: !cwl {source: [!* a, b], linkMerge: merge_flattened}` | CWL's step input, with its sources resolved by Sophios |

An input left out of `in:` is connected by inference (§8). An input name the
step's process does not declare is `wic028`, except for a record (§3.5). An
input with an empty name is `wic027`.

Each form answers one question, where does this input's value come from:
written here, given from outside, made by an earlier step, or written as CWL
that Sophios does not interpret. Saying which, explicitly, is what lets
Sophios check every binding before a run.

| Code | When | Fix |
|---|---|---|
| `wic028` | the step's process has no input of that name | use an input the tool or subworkflow declares |
| `wic027` | an input has an empty name | write the name |

[Inputs and values](inputs-and-values.md) teaches the forms one at a time.

### 3.2 Literals: `!ii`

`!ii` takes any YAML value, not only a scalar:

```yaml
in:
  config: !ii
    pdb_code: 1aki
```

A literal must already have the type of the input it binds. `!ii 2.9` on an
`int` input is `wic020`, and so is `!ii 1` on a `boolean`. Exactly two
conversions are made, because neither loses anything: an integer on a
`float` input is that float, when the float holds it exactly, and a literal
on a `string` input is its text, a number or a boolean as written and a
mapping or a list as JSON.

Under `!ii` a scalar is read as YAML reads the same scalar. Unquoted, it is
read by what it says: `!ii 20` is the number 20, `!ii 007` the number 7.
Quoted, with either kind of quote, it is its text: `!ii '20'` is the text
`20`, and on an `int` input each is `wic020`. YAML reads `1e-5` as text; write
`!ii 1.0e-5` for the float. A date-shaped unquoted scalar is read as a date.

A literal of `null` on an input that is not optional is `wic012`.

The tag exists because an untagged string has a job of its own: it names a
workflow input (§3.3). `!ii` says "this value, as written", and that is what
lets Sophios tell `filename: !ii greeting.txt` from `filename: greeting`.

An untagged mapping or list in an input position is a literal too, `!ii`
being the clearer spelling, except a mapping whose keys are all step-input
fields (`source`, `default`, `valueFrom`, `linkMerge`, `pickValue`,
`loadContents`, `loadListing`, `label`): that is `wic038`. Write
`!cwl {default: a.txt}` for the step input, or `!ii {default: a.txt}` for a
literal of that shape.

| Code | When | Fix |
|---|---|---|
| `wic020` | the literal does not have the input's type | write a value of that type, quoting text YAML would read as a number or date |
| `wic012` | `null` on an input that is not optional | give the input a value, or make the tool's input optional |
| `wic038` | an untagged mapping of step-input fields | tag it `!cwl` for a step input, or `!ii` for a literal |

[Inputs and values](inputs-and-values.md) shows the conversions and the
quoting rule at work.

### 3.3 Workflow inputs

An untagged string names a workflow input, declared under `inputs:`:

```yaml
inputs:
  name: string
steps:
  touch:
    in:
      filename: name
```

A name that no workflow input declares is `wic011`; the message asks whether
you meant `!ii`. With `--allow_raw_cwl` such a name is written into the CWL
unchecked instead. A workflow input whose type can never feed the input it
binds is `wic023`.

A workflow input keeps a value out of the workflow, so the same file runs on
other data: the value comes from an inputs file, or from the step that calls
the workflow as a subworkflow.

| Code | When | Fix |
|---|---|---|
| `wic011` | an untagged value names no declared workflow input | write `!ii <value>`, or declare the name under `inputs:` |
| `wic023` | the input's type can never feed the step's input | declare a type that fits, or bind a matching source |

[Inputs and values](inputs-and-values.md) makes the greeting a workflow
input and gives it a value from an inputs file.

### 3.4 Raw CWL: `!cwl` on a string

`!cwl` on a string writes it into the compiled CWL as it is, without checking
it. What it names must exist in the compiled CWL under that name. A workflow
input does; a step output is not one to rely on, since Sophios does not read
the string and numbers a step id that a second step shares, so `!cwl echo/stdout`
is no edge it knows of. To use a step's output, define an edge (§4).

It is an escape hatch, for a CWL reference that Sophios does not model.
Prefer the checked forms wherever one fits:

```yaml
inputs:
  greeting:
    type: string
steps:
  echo:
    in:
      message: !cwl greeting
```

The cookbook's `raw_cwl` pair shows the Python twin, `flow.inputs.greeting`.

### 3.5 Step-input records: `!cwl` on a mapping

`!cwl` on a mapping is CWL's step input, for what the other forms cannot say:
several sources, a merge, a pick, a `valueFrom`, a default. Sophios reads it
so that it can resolve each source like any other reference, and check what
can be checked.

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

`source` is one entry or a list, each an edge reference (`!* name`) or a
workflow-input name. A record with one source resolves it as the same
reference written alone, so it may take an edge its caller defined. A record
with several sources takes each from its own document: an edge no step of the
document defines is `wic025` there, even when the caller defines it; declare
the value in `inputs:` instead (§4.1). The other fields (`default`, `label`,
`linkMerge`, `loadContents`, `loadListing`, `pickValue`, `valueFrom`) are CWL
and are written out as they are. Sophios adds the requirements the record
needs: `MultipleInputFeatureRequirement` for several sources or a
`linkMerge`, `StepInputExpressionRequirement` for a `valueFrom`, and
`InlineJavascriptRequirement` when the `valueFrom` is an expression.

A `File` or `Directory` in a `default:`, here or under the workflow's
`inputs:`, that names a relative `location` or `path` is read from the
directory of the `.wic` file and written absolute into the compiled CWL, so
`default: {class: File, location: words.txt}` names the `words.txt` beside
the `.wic` wherever the compiled workflow runs.

These are `wic038`: a key that is not one of those fields; a `source` entry
that is not a reference (`!ii`, `!cwl`); a Sophios tag inside another field;
and a record with no `source`, `default` or `valueFrom`, which gives the
input no value.

A record may bind an input that the step's tool does not declare, which is
how a `when:` or a `valueFrom` reads an extra value (§6). Any other form
there is `wic028`, and so is a record naming an input a called subworkflow
does not declare. A source that a merge, a pick or a `valueFrom` transforms
is not checked against the input's type.

| Code | When | Fix |
|---|---|---|
| `wic038` | an unknown field, a `source` entry that is not a reference, or a record that gives no value | use the CWL step-input fields, with references in `source` |
| `wic025` | a multi-source record names an edge its own document does not define | declare the value in `inputs:` (§4.1) |
| `wic028` | the record names an input the called subworkflow does not declare | declare the input in the subworkflow |

[Types and records](types-and-records.md) merges two outputs into one input,
and [Scatter and conditions](scatter-and-conditions.md) uses a record for a
`when:`.

### 3.6 `!&` is not an input form

`!&` defines an edge, where its value comes into being: on a step's `out:`
entry (§5.1). Anywhere else it is `wic019`. If you meant to use an edge,
write `!*`. A tag other than `!ii`, `!&`, `!*` and `!cwl` is `wic009`.

The rule exists so that every edge has exactly one place where it starts,
which is what makes a connection easy to find by reading the file.

| Code | When | Fix |
|---|---|---|
| `wic019` | `!&` outside an `out:` entry | put `!&` on the producing step's `out:` and use `!*` here |
| `wic009` | a YAML tag Sophios does not know | use `!ii`, `!&`, `!*` or `!cwl`, or remove the tag |

### 3.7 The desugared spelling

Each tag has a second spelling, a single-key mapping whose key begins `wic_`:

| Tagged | Desugared |
|---|---|
| `!ii value` | `{wic_inline_input: value}` |
| `!& name` | `{wic_anchor: name}` |
| `!* name` | `{wic_alias: name}` |
| `!cwl expr` | `{wic_raw_cwl: expr}` |

Both are valid to write by hand; `write_wic()` writes the tagged one. Where
an input value is expected, a single-key `wic_` mapping that is none of these
is `wic024`, so a misspelling is reported instead of being copied into the
CWL. A port or a CWL key may still be named with the prefix.

The second spelling exists for programs that write YAML or JSON without
tags, and for the JSON Schema, which cannot describe a tag. This compiles to
the same workflow as its tagged twin:

```yaml
steps:
- id: touch
  in:
    filename: {wic_inline_input: greeting.txt}
  out:
  - file: {wic_anchor: empty_file}
- id: cat
  in:
    file: {wic_alias: empty_file}
```

| Code | When | Fix |
|---|---|---|
| `wic024` | a single-key `wic_` mapping that is none of the four | correct the spelling, or rename the key |

### 3.8 Every name is bound once

A key may appear only once in any mapping Sophios reads: an input in `in:`, a
step name, a `wic:` key, a `wic: steps:` key, and any CWL key alike. Binding
twice is `wic010`, not a last-one-wins.

A plain YAML loader keeps the last of two equal keys and drops the other
without a word, which hides a mistake; Sophios refuses instead, at the
second key. Fix: keep one of the two and delete the other.

## 4. Edges

`!& name` on an `out:` entry defines an edge from that output; `!* name` on an
input uses it:

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

- A reference comes after its definition; `!*` above its `!&` is `wic025`.
- A name is defined once; a second `!&` of it is `wic026`.
- A definition nothing uses is not an error.
- Edge names are shared by the whole compilation: a subworkflow may use an
  edge its caller defined before the call, and a step after a call may use an
  edge defined inside the subworkflow. A reference no document defines is
  `wic025`, reported at the root. A record with several sources is the
  exception (§3.5).
- An edge whose output type can never feed the input it binds is `wic023`.

A named edge makes each connection visible in the file and checkable before
a run: both ends are known, so Sophios can compare their types and draw the
edge.

| Code | When | Fix |
|---|---|---|
| `wic025` | `!*` comes before its `!&`, or nothing defines the name | define the name with `!&` on an earlier step, or correct its spelling |
| `wic026` | an edge name is defined twice | give each `!&` its own name |
| `wic023` | the output's type can never feed the input | connect a source of a matching type |

[Connecting steps](connecting-steps.md) writes edges by hand, including a
branch and a join.

### 4.1 A value from outside: declare it in `inputs:`

`!*` asks the compilation for an edge. A document that expects a value from
whoever calls it declares the value in `inputs:` and binds it by its bare
name; the caller binds it like any input of a step:

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
  in:
    filename: !ii data.txt
```

Edge names are shared by the whole compilation, which lets a child read an
edge its caller defined. A child meant for reuse should not depend on that:
declaring the value in `inputs:` gives it an interface any caller can see
and bind, and [Subworkflows](subworkflows.md) builds one.

## 5. Outputs

### 5.1 A step's `out:`

`out:` exists so that a connection starts where its value is made: a later
step can only use an output that some step's `out:` names. `out:` is a list. Each entry is an output name, or an output name with an edge
definition:

```yaml
out:
- file                    # names the output
- file: !& file_touch     # names it and defines the edge file_touch
```

An `out:` entry never creates an output: it names one the step's process
declares, so a later step can use it. An output name the process does not
declare is `wic028`; an entry with an empty name is `wic027`.

### 5.2 The workflow's `outputs:`

A workflow output names its producer with `outputSource: <step>/<output>`,
the step by its id:

```yaml
outputs:
  greeting:
    type: File
    outputSource: append/file
```

When a step id repeats, name the occurrence by its 1-based position:
`outputSource: (2, append)/file`. A step at that index with a different name
is `wic039`, and a port that step does not have is `wic028`, with the step's
outputs listed. A position holds in a workflow with inferred edges too, since
inference never moves or renumbers a step.

An output with no `type:` takes the type of the output its `outputSource:`
names, as an array when that step scatters. One with no `type:` and no
producer to take it from is `wic036`. A list `outputSource:`, and `linkMerge`
or `pickValue` on a workflow output, are `wic038`.

A bare id that more than one step has resolves to
the first of them, and the compile says so with the position that spells it.

A name you declare is never spelled like one the compiler makes, which
always contains `~` (§2.1). A `$namespaces` prefix that the document and the
steps whose promoted ports use it bind to different URIs is `wic031`.

A workflow output gives a result a name that stays the same when you add or
move steps, for whoever runs the workflow and for a workflow that calls this
one. Every step output stays available under its generated name as well.

| Code | When | Fix |
|---|---|---|
| `wic039` | `(index, name)` names a different step at that position | use the step's index and id, or write `step/port` |
| `wic028` | the named step has no output of that name | use an output the step declares |
| `wic036` | no `type:` and no producer to take it from | give a `type:` or an `outputSource:` |
| `wic038` | a list `outputSource`, or `linkMerge` or `pickValue` on a workflow output | name one step output; merge in a step's input instead |
| `wic031` | a prefix is bound to two URIs | bind the prefix to one URI |

[Connecting steps](connecting-steps.md) names the greeting file as the
workflow's output.

## 6. Scatter, when, run

### 6.1 `scatter:`

Each `scatter:` entry names an input of its step, and Sophios adds
`ScatterFeatureRequirement`. A name that is not an input of the step is
`wic032`, and so is a scattered input that nothing binds once inference is
done. A literal bound to a scattered input must be a list (`wic020`
otherwise). On a subworkflow call the inputs are exactly those the
subworkflow declares in `inputs:`; a name the compiler generates for an inner
step's input is not one of them. `scatterMethod` is one of `dotproduct`,
`flat_crossproduct` and `nested_crossproduct`, written as given.

A scatter runs one step once per element of a list, where a script would
use a loop, and keeps the workflow a graph without cycles:

```yaml
steps:
  echo_3:
    scatter: [message1, message2]
    scatterMethod: flat_crossproduct
    in:
      message1: !ii [Hello, Goodbye]
      message2: !ii [sun, moon]
      message3: !ii and stars
```

| Code | When | Fix |
|---|---|---|
| `wic032` | a `scatter:` entry is not an input of the step, or nothing binds it | scatter over an input the step declares and binds |
| `wic020` | a scattered input's literal is not a list | give it a list, even of one element |

[Scatter and conditions](scatter-and-conditions.md) greets three people in
one run.

### 6.2 `when:`

`when:` is written into the CWL as it is, and Sophios adds
`InlineJavascriptRequirement`. CWL evaluates an `inputs.<name>` the step does
not have as `null`; the compile prints a line on stderr when `when:` reads
such a name. On a subworkflow call the names it can read include those the
compiler makes for the inputs inside it, such as `touch~filename`; `~`
cannot appear in `inputs.<name>`, so write `inputs["touch~filename"]`, and
the line names that form when `when:` reads `inputs.touch`. To give `when:` a value
the tool does not take, bind it with a record (§3.5):

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

A condition lets one workflow cover cases that differ by a switch, without a
copy of the file for each case. A step that does not run gives `null` for
each of its outputs, so a step that reads them must accept `null`.
[Scatter and conditions](scatter-and-conditions.md) runs a step only when
asked.

### 6.3 `run:`

`run:` is listed here beside the other step keys that change how a step
behaves; what it accepts, and why, is in §2.3.

## 7. The `wic:` block

The `wic:` block holds metadata for the compiler and is never written to the
CWL. Keeping Sophios's own settings in one block, apart from the CWL keys,
is what lets everything else in the document pass through to the CWL
untouched.

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

A key under `wic: steps:` names a step of this document as `(index, name)`,
the 1-based position and id, or as the bare id of a step that occurs once.
When both spellings name one step, the `(index, name)` entry applies. A key of
neither shape is `wic008`. A key that addresses no step is ignored, and the
compile prints a line on stderr saying which step is at that position; a bare
name that several steps carry also addresses no step.

The block is closed: each key has a fixed meaning and a fixed shape of value.

| Key | Value |
|---|---|
| `steps` | a mapping keyed by `(index, name)` or by a step id that occurs once |
| `graphviz` | a mapping of `label` (a non-empty string), `style` (Graphviz styles, comma separated) and `ranksame` (a list of `(index, name)` keys), each optional |
| `implementation`, `default_implementation`, `version`, `lang_version`, `namespace` | a non-empty string |
| `driver` | `slurm` or `argo` |
| `implementations` | a mapping of names to workflow bodies |
| `inlineable` | `true` or `false` |

An entry under `steps:` may also carry `in`, `out`, `scatter` and
`inference`, which contribute to the step they name as if written on it, and
`scatterMethod`. An entry for a step that calls a subworkflow may carry its
own `wic:` block with `steps:` entries that address the steps inside the
subworkflow, so a caller can set an inner step's inputs without editing the
child. Any other key is `wic033`, at the key; a value of the wrong shape
(`inlineable: sometimes`) is `wic034`, at the value. A bare `wic:` with
nothing under it is an empty block.

- `lang_version` pins the language version the document is read as (see the
  [language specification](dev/language_spec.md)). An unknown version is
  `wic017`; two pins that disagree within one compilation are `wic018`. A
  `cwlVersion:` Sophios cannot run (anything but `v1.0`, `v1.1` or `v1.2`) is
  `wic035`; the compiled workflow always says `v1.2`.
- `namespace` on a `wic: steps:` entry selects the search-path namespace the
  step's process comes from ([Configuration](configuration.md)).
- `implementations` and `default_implementation` on a document make it a
  dispatcher: each implementation is a workflow body, and a caller's
  `implementation` on the `wic: steps:` entry that calls it chooses one at
  compile time. A dispatcher with no implementation chosen is `wic013`.
- `inlineable: false` keeps a subworkflow nested under
  `--cwl_inline_subworkflows`.
- `graphviz` changes only the drawing ([Drawing](drawing.md)).

| Code | When | Fix |
|---|---|---|
| `wic008` | a `wic: steps:` key is neither `(index, name)` nor a step id | key it `(index, name)`, or by an id no other step has |
| `wic033` | a key the block does not have | use a key from the table above, or remove it |
| `wic034` | a value of the wrong shape | write the value in the shape the table gives |
| `wic017` | an unknown `lang_version` | pin a version the message lists, or remove the pin |
| `wic018` | two `lang_version` pins disagree | make them agree, or pass `--lang_version` |
| `wic035` | a `cwlVersion` other than `v1.0`, `v1.1` or `v1.2` | declare `cwlVersion: v1.2` |
| `wic013` | a dispatcher with no implementation chosen | choose one with `implementation` on the calling step's entry |

[Subworkflows](subworkflows.md) sets an inner step's value from the caller,
and [Configuration](configuration.md) explains namespaces.

## 8. Inference

An input that no `in:` entry binds is connected by inference: Sophios looks
back through the earlier steps, most recent first, for outputs whose type and
format match the input's, and the most recent step with one supplies the edge.
Within that step, an output whose name pairs with the input's wins, and
otherwise the one declared first. When more than one candidate matched it still takes
that one and lists the edge in a `wic043` note, which counts the guessed edges
and says to draw the workflow to check them. Notes do not fail the compile;
`--inference_strict` makes each guess an error. An input that nothing
matches becomes an input of the compiled workflow.

A duplicate needs evidence. Group the inputs of each tool step by the output
they read, once inference has chosen: the edges you wrote and the edges it
inferred. For a step inside a subworkflow the tool step is the inner step that
declares the input, so a file that fans out to several inner steps is not a
duplicate. Where two or more inputs of one tool step read the same output and at
least one was inferred, inference refuses with `wic055` unless every input in the
group declares file formats and the output's declared formats include one of
them. The inferred inputs of a refused group are not connected. Two written
edges to one output are never refused.

Pin a choice with an edge (§4) and its row goes away. For an output inside a
subworkflow the pin goes on the inner step the row's path names.
`--inference_disable` turns inference off, and
`--no_inference_naming_conventions` turns the names off. The
configuration's `inference_rules` refine inference too;
`--insert_steps_automatically` lets it insert a converter step, and inference
that does not settle within its iteration limit is `wic022`.
[Edge inference](edge-inference.md) gives the exact rule.

Inference exists because, in the straight stretches of a workflow, writing
every connection says nothing a reader could not guess. It reads the steps in
the order you list them, so where the graph branches, write the edges: the
nearest earlier output is not always the branch you meant. This compiles,
with `cat` connected to `append`'s file and a `wic043` note counting `touch`'s
file as the other candidate:

```yaml
steps:
- id: touch
  in:
    filename: !ii greeting.txt
- id: append
  in:
    str: !ii Hello, Sophios
- id: cat
```

| Code | When | Fix |
|---|---|---|
| `wic043` | a note: another output also fitted, so the edge was guessed | draw the workflow, with `--graphviz` or `Workflow.draw()`, and pin the wrong ones with `!&` and `!*`; `--inference_strict` makes it an error |
| `wic055` | two inputs of one tool step would read one output, and an inferred one lacks the format evidence | write the edge each input should read, with `!&` and `!*` |
| `wic022` | inference did not settle within its iteration limit | bind the inputs explicitly, or drop `--insert_steps_automatically` |

