# The `.wic` reference

What may a `.wic` document contain, what does each piece mean, and what does
the compiler say when something is wrong? This page is the complete
reference for the file form, section by section; the pages under **Write
workflows** explain the same constructs with examples in both forms. Error
codes name their section here, as `§3.5`, and [Error codes](error_codes.md)
lists them all.

Compile a document without running it with
`sophios --yaml workflow.wic --generate_cwl_workflow`.

## 1. A document

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

Both forms compile to the same workflow. A sequence of single-key mappings
(`- touch:` with the body under it) is not a third form: it is `wic006`, and
the message names the two forms to write instead. An `id:` that is empty is
`wic007`. A step name repeated in the mapping form is `wic010`, and so is an
`id:` inside a step that already takes its name from its mapping key.

A step may have no body. `multistep1.wic:` under `steps:` calls that
subworkflow with nothing bound, leaving its inputs to inference.

### 2.2 Step keys

| Key | Meaning |
|---|---|
| `in` | input bindings (§3) |
| `out` | output names and edge definitions (§5.1) |
| `run` | the process the step runs (§2.3) |
| `scatter`, `scatterMethod` | scatter over inputs (§6.1) |
| `when` | run the step only when the expression is true (§6.2) |
| anything else | CWL, copied to the step unchanged |

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

### 3.2 Literals: `!ii`

`!ii` takes any YAML value, not only a scalar:

```yaml
in:
  config: !ii
    pdb_code: 1aki
```

A literal must already have the type of the input it binds; Sophios converts
nothing. `!ii 2.9` on an `int` input is `wic020`, and so is `!ii 1` on a
`boolean`. Two conversions are made, because neither loses anything: an
integer on a `float` input is that float, and a number or a boolean on a
`string` input is its text.

Under `!ii` a scalar is read as YAML reads the same scalar. Unquoted, it is
read by what it says: `!ii 20` is the number 20, `!ii 007` the number 7.
Quoted, with either kind of quote, it is its text: `!ii '20'` is the text
`20`, and on an `int` input each is `wic020`. YAML reads `1e-5` as text; write
`!ii 1.0e-5` for the float. A date-shaped unquoted scalar is read as a date.

A literal of `null` on an input that is not optional is `wic012`.

An untagged mapping or list in an input position is a literal too, `!ii`
being the clearer spelling, except a mapping whose keys are all step-input
fields (`source`, `default`, `valueFrom`, `linkMerge`, `pickValue`,
`loadContents`, `loadListing`, `label`): that is `wic038`. Write
`!cwl {default: a.txt}` for the step input, or `!ii {default: a.txt}` for a
literal of that shape.

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

### 3.4 Raw CWL: `!cwl` on a string

`!cwl` on a string writes it into the compiled CWL as it is, without checking
it. What it names must exist in the compiled CWL under that name. A workflow
input does; a step name does not, since steps are renamed, so `!cwl echo/stdout`
points at nothing. To use a step's output, define an edge (§4).

### 3.5 Step-input records: `!cwl` on a mapping

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

These are `wic038`: a key that is not one of those fields; a `source` entry
that is not a reference (`!ii`, `!cwl`); a Sophios tag inside another field;
and a record with no `source`, `default` or `valueFrom`, which gives the
input no value.

A record may bind an input that the step's tool does not declare, which is
how a `when:` or a `valueFrom` reads an extra value (§6). Any other form
there is `wic028`, and so is a record naming an input a called subworkflow
does not declare. A source that a merge, a pick or a `valueFrom` transforms
is not checked against the input's type.

### 3.6 `!&` is not an input form

`!&` defines an edge, where its value comes into being: on a step's `out:`
entry (§5.1). Anywhere else it is `wic019`. If you meant to use an edge,
write `!*`. A tag other than `!ii`, `!&`, `!*` and `!cwl` is `wic009`.

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

### 3.8 Every name is bound once

A key may appear only once in any mapping Sophios reads: an input in `in:`, a
step name, a `wic:` key, a `wic: steps:` key, and any CWL key alike. Binding
twice is `wic010`, not a last-one-wins.

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

## 5. Outputs

### 5.1 A step's `out:`

`out:` is a list. Each entry is an output name, or an output name with an edge
definition:

```yaml
out:
- file                    # names the output
- file: !& file_touch     # names it and defines the edge file_touch
```

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

A generated step name (`o__step__1__touch`) resolves in `outputSource:` but
changes when steps move, so the compile prints a line on stderr naming the
spelling to write instead. A bare id that more than one step has resolves to
the first of them, and the compile says so with the position that spells it.

An input or output you declare that is spelled like a name the compiler
derives for another port is `wic031`, since the compiled CWL could not tell
them apart. So is a `$namespaces` prefix that the document and the steps
whose promoted ports use it bind to different URIs.

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

### 6.2 `when:`

`when:` is written into the CWL as it is, and Sophios adds
`InlineJavascriptRequirement`. CWL evaluates an `inputs.<name>` the step does
not have as `null`; the compile prints a line on stderr when `when:` reads
such a name, and a different line when the name is one the compiler
generates for an input inside a called subworkflow. To give `when:` a value
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

### 6.3 `run:`

See §2.3.

## 7. The `wic:` block

The `wic:` block holds metadata for the compiler and is never written to the
CWL.

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
| `implementation`, `default_implementation`, `lang_version`, `namespace` | a non-empty string |
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

## 8. Inference

An input that no `in:` entry binds is connected by inference: Sophios looks
back through the outputs of the earlier steps, most recent first and within a
step last declared first, and connects the input to the first output whose
type and format match. When more than one candidate matched it still takes
that one and says so with a note: `wic042` when several outputs of the chosen
step match, `wic043` when an earlier step also matched. Notes do not fail the
compile; `--inference_strict` makes them errors. An input that nothing
matches becomes an input of the compiled workflow.

Pin a choice with an edge (§4) and the note goes away. For a port inside a
subworkflow the pin goes on the inner step, and the note names it.
`--inference_disable` turns inference off; `--inference_use_naming_conventions`
and the configuration's `inference_rules` refine it;
`--insert_steps_automatically` lets it insert a converter step, and inference
that does not settle within its iteration limit is `wic022`.
[Edge inference](edge-inference.md) gives the exact rule.
