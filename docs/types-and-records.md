# Types and records

What may flow along an edge, and how do I feed one input from several sources,
or transform a value on the way in? Types decide the first question. Sophios
reads CWL's types as the tools declare them, compares them where it connects
things, and rejects only what it can prove wrong. The step-input record
answers the second: one binding that names several sources, or a default, or
a transformation, written in either form.

## Types

A port's type is what its tool declares: an atom (`null`, `boolean`, `int`,
`long`, `float`, `double`, `string`, `File`, `Directory`), an array of a type
(`File[]`, or `{type: array, items: File}`), a union (`[null, File]`, which
`File?` also spells), an enum, a record, or `Any`. Sophios does not define
types of its own.

Where a value meets a port, Sophios asks one question: can this value ever be
of the port's type?

- A **literal** must have the port's type, with the two conversions
  [Inputs and values](inputs-and-values.md) lists (integer to `float`,
  number or boolean to `string`). Anything else is `wic020`, in both forms:
  `touch.inputs.filename = 20` binds `"20"`, while a float on an `int` port is
  reported at the assignment's line.
- A **reference** (an edge or a workflow input) is rejected only when its type
  and the port's are provably disjoint: two different atoms, an array against
  a scalar atom, or unions none of whose members overlap. That is `wic023` in
  `.wic` and `api003` in Python, raised at the assignment. `Any`, a named
  schema, a record or an enum make the relation unknown, and an unknown
  relation is left to the runner, which validates the compiled CWL when it
  loads it. A scatter is taken into account on both sides: a scattered
  producer's output is an array, and an input the consumer scatters over is
  compared as an array of its declared type.
- **Inference** is stricter than rejection, because it chooses rather than
  checks: an output is a candidate only when its type equals the input's, or
  one is a union containing the other or the two unions share a member, and its
  format fits. `Any` matches only
  `Any`. [Edge inference](edge-inference.md) has the full rule.

A workflow input takes the type of the first port it feeds, in Python, or the
type you declare under `inputs:`, in `.wic`. A workflow output takes the type
of its source. A tool's `stdout` and `stderr` shorthands are `File` at a
workflow boundary.

## Step-input records

```{literalinclude} cookbook/record.wic
:language: yaml
:caption: record.wic
```

```{literalinclude} cookbook/record.py
:language: python
:caption: record.py
```

Two outputs are merged into one `File[]` input; Sophios resolves the two
sources into one binding ([Compiling to CWL](compiling-to-cwl.md#records)
shows the result). In `.wic` the record is `!cwl` on a mapping; in Python it
is a `StepInput`, bound like any value. The fields are the step input's, in
their own spelling in `.wic` and in Python spelling in `StepInput`:

| CWL field | `StepInput` | Meaning |
|---|---|---|
| `source` | `source` | one step output or workflow input, or a list of them |
| `linkMerge` | `link_merge` | how several sources combine: `merge_nested` (a list with one entry per source, the default) or `merge_flattened` (arrays concatenated) |
| `pickValue` | `pick_value` | which of several sources to keep when some are `null`: `first_non_null`, `the_only_non_null` or `all_non_null` |
| `valueFrom` | `value_from` | an expression over the value (`self`) whose result is bound instead |
| `default` | `default` | the value when the source gives none |
| `loadContents` | `load_contents` | read the first 64 KiB of a file into `contents` |
| `loadListing` | `load_listing` | how deep a directory is listed |
| `label` | `label` | a title |

The rules:

- `source` entries are references: `!* name` or a workflow-input name in
  `.wic`, port objects in Python. A `!ii` or a `!cwl` inside `source`, a
  string in a `StepInput`'s `source`, is an error (`wic038`, `api001`).
- A record may bind an input the step's tool does not declare. That is how a
  `when:` or a `valueFrom` reads an extra value; in Python a `StepInput` is
  the only value that may be assigned to an undeclared name. The input is
  typed `Any`.
- A source that a merge, a pick or a `valueFrom` transforms is not checked
  against the input's type, since the transformation decides the type. A record with one source and nothing else is checked as a
  plain edge would be.
- A record with one source resolves it as the same reference written alone,
  so it may use an edge its caller defined. A record with several sources
  takes each from its own document: an edge no step of the document defines
  is `wic025` there, even when the caller defines it; declare the value in
  `inputs:` instead.
- These are `wic038`: a key that is not one of the fields above; a record with
  no `source`, `default` or `valueFrom`, which gives the input no value; and
  an untagged mapping of these fields in an input position.

## Where a record is the only way

- Several sources into one input: a merge, as above.
- A value for `when:`, bound to a name the tool does not take; see
  [Scatter and conditions](scatter-and-conditions.md).
- A default for an input that may receive nothing, with `default`.
- A transformation on the way in, with `valueFrom`:
  `minval: !cwl {source: count, valueFrom: '$(self + 1)'}`.
- Picking among the outputs of conditional steps, with `pickValue`.

Everything else, a literal, a workflow input or a single edge, has its plain
spelling, which is the one to use.
