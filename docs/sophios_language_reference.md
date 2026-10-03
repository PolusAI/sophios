# The Sophios Language Reference

**Version:** `lang_version` 0.0.1
**Substrate:** CWL v1.2

This is the human-readable definition of **Sophios**, the workflow language.
The executable definition is `sophios.lang` — the typed AST and parser — and
the two are meant to agree. Where they disagree, that is a bug in one of them.

## The language and its two surfaces

Sophios is the language. It has one DSL, and that DSL can be written two ways:

| Surface | How it is written | Where it lives |
|---|---|---|
| **YAML** | The YAML-based spelling of the DSL | Conventionally in files named `.wic` |
| **Python API** | `Workflow`, `Step`, `CommandLineTool` | Python source |

Neither is "the language" and neither is subordinate to the other. They are two
ways of saying the same thing, which is why §6 can state what each owes the
other and check it.

`.wic` is **a file extension, not a language**. This document says "`.wic`
files" when it means files on disk, and "Sophios" when it means the language.
A handful of spellings inside the syntax still carry the older `wic` prefix —
the `wic:` block, the `!ii` / `!&` / `!*` tags, and the `wic_*` desugared keys.
Those are concrete syntax that existing workflows depend on, so they stay as
they are; they are not evidence that the language is called wic.

---

## 1. What kind of language this is

Sophios is a **leaky abstraction over CWL, deliberately**. You write shorthand
for the common case and drop into raw CWL for anything the shorthand does not
cover. Sealing the abstraction would mean re-inventing CWL one feature at a
time and asking users to wait.

Sophios-owned syntax (`!ii`, `!&`, `!*`, `!cwl`, the `wic:` block) is consumed
and never appears in the output. Everything else is CWL, and what Sophios does
with each field of the five workflow-level CWL v1.2 classes is declared in one
place: `sophios.lang.SUPPORT_MATRIX` (`src/sophios/lang/support.py`). Each
field is one of

- **native**: Sophios reads it and acts on it, or writes it;
- **passthrough**: copied out unchanged;
- **rejected**: reported with a diagnostic, never silently dropped or coerced.
  It is positioned at the value; a step's `run` is positioned at the step, and
  a workflow output at the document.

The matrix is generated from the schema, not from this page:
`tests/core/test_support_matrix.py` reads the fields of each class from
`cwl_utils.parser.cwl_v1_2`, so a field the schema has and the matrix lacks
fails the build, and so does a row whose pinning test does not exist. The
tables below are the matrix at the time of writing; where they disagree,
`support.py` wins. A test named without a file is in
`tests/core/test_leak_boundary.py`. A field outside these five classes on a
step's tool is passthrough wholesale: Sophios does not classify
`CommandLineTool` fields.

**`Workflow`**

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `class` | native | written by the compiler | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `cwlVersion` | native | written; `wic035` for an unrunnable value | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `doc` | passthrough | | `test_top_level_passthrough_is_byte_identical` |
| `hints` | passthrough | | `test_top_level_passthrough_is_byte_identical` |
| `id` | passthrough | | `test_top_level_passthrough_is_byte_identical` |
| `inputs` | native | merged into; the compiler wins on a collision | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `intent` | passthrough | | `test_top_level_passthrough_is_byte_identical` |
| `label` | passthrough | | `test_top_level_passthrough_is_byte_identical` |
| `outputs` | native | read: `outputSource` is resolved; merged into | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `requirements` | native | merged into | `test_user_requirements_are_merged_into_not_copied` |
| `steps` | native | | `test_well_formed_documents_parse` (`test_lang_parser.py`) |

**`WorkflowStep`**

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `doc` | passthrough | | `test_step_passthrough_is_byte_identical` |
| `hints` | passthrough | | `test_step_passthrough_is_byte_identical` |
| `id` | native | | `test_sequence_and_mapping_steps_agree` (`test_lang_parser.py`) |
| `in` | native | | `test_input_values_are_closed` (`test_lang_parser.py`) |
| `label` | passthrough | | `test_step_passthrough_is_byte_identical` |
| `out` | native | | `test_the_two_spellings_of_an_edge_definition_agree_on_an_output` (`test_lang_parser.py`) |
| `requirements` | passthrough | | `test_step_passthrough_is_byte_identical` |
| `run` | rejected | a stem is resolved from the registry; a path or an inline body is `wic013` | `test_a_run_that_is_not_a_registry_stem_is_reported` (`test_lang_parser.py`) |
| `scatter` | native | | `test_every_declared_key_is_interpreted` |
| `scatterMethod` | native | | `test_every_declared_key_is_interpreted` |
| `when` | native | | `test_every_declared_key_is_interpreted` |

**`WorkflowStepInput`** (an entry of a step's `in:`)

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `id` | native | the `in:` key | `test_input_values_are_closed` (`test_lang_parser.py`) |
| `source` | native | spelled `!*` or a bare workflow-input name | `test_input_values_are_closed` (`test_lang_parser.py`) |
| `default` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `label` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `linkMerge` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `loadContents` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `loadListing` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `pickValue` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |
| `valueFrom` | rejected | `wic038` | `test_an_untagged_step_input_record_is_wic038` (`test_lang_parser.py`) |

**`WorkflowOutputParameter`** (an entry of the workflow's `outputs:`)

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `id` | native | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `type` | native | | `test_an_untyped_authored_output_takes_its_producers_type` (`test_emit.py`) |
| `outputSource` | native | one step/port reference; a list is `wic038` | `test_an_authored_output_source_validates` |
| `format` | passthrough | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `doc` | passthrough | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `label` | passthrough | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `secondaryFiles` | passthrough | | `test_a_promoted_output_keeps_the_fields_a_workflow_output_may_state` (`test_emit.py`) |
| `streamable` | passthrough | | `test_a_promoted_output_keeps_the_fields_a_workflow_output_may_state` (`test_emit.py`) |
| `linkMerge` | rejected | `wic038` | `test_link_merge_and_pick_value_on_an_output_are_wic038` (`test_lower.py`) |
| `pickValue` | rejected | `wic038` | `test_link_merge_and_pick_value_on_an_output_are_wic038` (`test_lower.py`) |

**`WorkflowInputParameter`** (an entry of the workflow's `inputs:`)

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `id` | native | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `type` | native | read for the reference judgment | `test_only_proven_disjoint_cross_scope_types_are_rejected` (`test_link.py`) |
| `format` | native | read by inference | `test_promoted_input_preserves_a_cwl_format_expression` (`test_infer_phase.py`) |
| `default` | passthrough | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `doc` | passthrough | | `test_a_referenced_input_merges_the_documentation_of_the_argument_it_binds` (`test_compiler.py`) |
| `label` | passthrough | | `test_a_referenced_input_merges_the_documentation_of_the_argument_it_binds` (`test_compiler.py`) |
| `inputBinding` | passthrough | | `test_class_is_written_while_inputs_outputs_and_version_are_not` |
| `loadContents` | passthrough | | `test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state` (`test_emit.py`) |
| `loadListing` | passthrough | | `test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state` (`test_emit.py`) |
| `secondaryFiles` | passthrough | | `test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state` (`test_emit.py`) |
| `streamable` | passthrough | | `test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state` (`test_emit.py`) |

What the native rows of `Workflow` do, in more detail:

- `class` is **written by the compiler**: a workflow-level value you supply
  does not survive.
- `inputs` and `outputs` are **merged into**, with the compiler winning on a
  collision: entries you write survive unless the compiler generates one of
  the same name. `outputs` is additionally *read*: each entry's
  `outputSource` names one step output, which feeds the compiler's output
  mapping. An output with no `type:` takes the type of the step output its
  `outputSource:` names, as an array when that step scatters. One whose
  `outputSource:` names no step output of the workflow, or that has none, has
  no type to take and is `wic036`. A list `outputSource:`, `linkMerge` or
  `pickValue` on an output is `wic038`. A list of `inputs` or `outputs`
  holding an `$import` or `$include` is the exception (§2): the compiler
  models only the mapping form, so it is neither merged into nor carried into
  the output.
- `cwlVersion` is **written by the compiler**: it is always the one declared
  substrate version, whatever the document says. Supplying `v1.0`, `v1.1` or
  `v1.2` is not an error, and the emitted document says the substrate version.
  Any other value is `wic035`, reported by the parser at the value: accepting
  a version is a promise to process it, and the toolchain processes no other.
- `requirements` is **merged into**: the compiler adds what the workflow needs
  with `setdefault`, so an authored body survives. It adds
  `ScatterFeatureRequirement` for a scattering step,
  `InlineJavascriptRequirement` for `when`, and
  `SubworkflowFeatureRequirement` for a `.wic` step, each only when the class
  is not already there: your `InlineJavascriptRequirement: {expressionLib: [...]}`
  survives a `when`. A `requirements:` list holding an `$import` or
  `$include` is the exception (§2): it is emitted as written, Sophios adds
  nothing to it, and the imported file has to supply what the workflow needs,
  such as the `ScatterFeatureRequirement` of a scattering step.

Two document keys are not schema fields of `Workflow` but are written by the
compiler too:

- `$schemas` is **append-only**: your entries survive and the EDAM entry is
  added once.
- `$namespaces` is **merged, with two reserved prefixes**: every binding you
  write survives except `edam` and `sophios`, which are replaced by the
  canonical ones (see §7 for `sophios:lang_version`). A port the document
  promotes from a step keeps that port's `format:` CURIE, so the document also
  declares each prefix such a format uses, bound as the step's tool or
  subworkflow binds it. If the document and the steps whose promoted ports use
  that prefix do not all bind it to the same URI, that is `wic031`: the CURIE
  could only mean one of them. Those bindings are the only ones that count. A
  tool that binds the prefix differently but promotes no port using it
  conflicts with nothing, wherever the step sits, so compiling does not depend
  on how the workflow is split into subworkflows. A prefix no promoted format
  uses is not declared for you. A port you wrote keeps the prefixes you bound.
  What a tool binds `edam` or `sophios` to is ignored, since the compiler binds
  both. Pinned by `test_user_namespaces_survive_except_edam` and
  `test_the_sophios_namespace_prefix_is_reserved`, and in
  `tests/core/test_emit.py` by
  `test_a_prefix_two_promoting_sources_bind_differently_is_reported`,
  `test_a_clash_on_a_prefix_no_promoted_format_uses_is_no_error` and
  `test_a_step_promoting_no_format_with_a_prefix_is_no_source_for_it_wherever_it_sits`.

Any other top-level or step key is passthrough and survives byte-identically,
which is the statement the properties in `tests/core/test_leak_boundary.py`
quantify over. The exception is the list form of `hints:` (on the document or
on a step) and of a step's `requirements:`, which the parser reads as the
mapping form (§2) and which is written out as one, unless the list holds an
`$import` or `$include` entry: that list is not read and survives as written.

---

## 2. Document structure

In its YAML surface, a Sophios document is a YAML mapping. Every key is optional.

```yaml
wic:            # optional  — compiler metadata, never emitted to CWL
steps:          # the workflow's steps
inputs:         # CWL workflow inputs        (passthrough)
outputs:        # CWL workflow outputs       (passthrough)
$namespaces:    # any other CWL key          (passthrough)
```

`inputs`, `outputs`, `requirements` and `hints` may be written in CWL's list form
(`- id: x` / `- class: X`), and a step's `requirements` and `hints` likewise. The
parser reads the list as the mapping form, so what follows it sees the mapping.
A port's `id:` may be written as a fragment (`#name` or `file.cwl#name`); its
mapping key is the name after the last `#`, so `a` and `#a` name the same port.
An entry without its `id:` or `class:` (or whose `id:` has nothing after the
`#`), and one naming the same key twice, are reported.

A list holding an `$import` or `$include` entry is the exception. That entry
names no `id:` or `class:` until cwltool has read its file, so the whole list is
left as written, and its other entries are not checked. A `requirements:` or
`hints:` list reaches the output as written, for cwltool to resolve, and
Sophios adds no requirement to a `requirements:` list so left. The compiler
models only the mapping form of `inputs:` and `outputs:`, so a list of those
left as written is not carried into the output of the document you compile. In
a workflow that another calls it is `wic013`: no step can be checked against
ports that only cwltool can read.

An empty document is well-formed and carries nothing.

---

## 3. Steps

### 3.1 Two surface forms

Both are long-standing, both remain supported, and **both produce the same
result**. Use whichever reads better. They are the two spellings CWL itself
admits for `steps:`, and Sophios admits no others — see the note below.

**Mapping, keyed by step name** — cannot repeat a step name:

```yaml
steps:
  touch:
    in:
      filename: !ii empty.txt
```

**Sequence with `id:`** — required when the same tool appears twice:

```yaml
steps:
- id: append
  in: {str: !ii Hello}
- id: append
  in: {str: !ii World}
```

A step may have no body at all:

```yaml
steps:
  some_subworkflow.wic:
```

A **sequence of single-key mappings** is not a third form:

```yaml
steps:
- touch:            # error (wic006): the step has no id
    in:
      filename: !ii empty.txt
```

This is not a narrowing Sophios chose; it is CWL's rule, inherited. CWL v1.2
types `Workflow.steps` as an array of `WorkflowStep` and attaches
`jsonldPredicate: {mapSubject: id}`, and Schema Salad applies that
transformation only *"if the value of the field is a JSON object"*. When
`steps:` is already an array, no key is lifted into `id`, so each item is a
plain `WorkflowStep` — and a `WorkflowStep` has no field named `touch`.
`cwltool` fails such a document with `unknown identifier`, having lost the
step's identity exactly as Sophios does.

This document listed the form as supported, and it was: until the May 2024
normal-form refactor (`9758e81`), which made the compiler read a step's name
from `id:` and rewrote every tutorial out of it into the `id:` form, it was the only
sequence form that worked. That break went unnoticed and is now ratified
rather than reverted, for the reason above — the substrate does not admit the
form, so a document using it breaks the moment it meets raw CWL. Writing it now
earns a diagnostic naming the two forms above instead of a failure further
downstream.

### 3.2 Step keys

| Key | Meaning |
|---|---|
| `in` | Input bindings (§4) |
| `out` | Output bindings (§3.3) |
| `scatter`, `scatterMethod` | Interpreted: Sophios adds `ScatterFeatureRequirement` |
| `when` | Interpreted: Sophios adds `InlineJavascriptRequirement` |
| `run` | Interpreted: an inline CWL tool definition |
| *anything else* | Passthrough |

### 3.3 Outputs

`out:` is a sequence. An entry is either a bare name, or a name bound to an
edge definition:

```yaml
out:
- file                    # just names the output
- file: !& file_touch     # names it and defines an edge
```

**This is the only place `!&` is legal.** An edge is defined where its value
comes into being, and that is an output; §4.1.1 says why, and what to write
instead if you meant to consume an edge.

---

## 4. Input values

### 4.1 The four forms

A step input is exactly one of these. There is no fifth form.

| Form | Written | Means |
|---|---|---|
| Inline literal | `f: !ii empty.txt` | A literal value. Never an edge. |
| Edge reference | `f: !* name` | Consumes an edge defined elsewhere |
| Raw CWL reference | `f: !cwl greeting` | Opaque to Sophios; passed through unresolved. |
| Unresolved name | `f: some_input` | Must resolve to a workflow input |

An untagged bare string is an **unresolved name**. If it does not name a
workflow input, you get a diagnostic telling you which of the two remedies you
probably meant — `!ii` for a literal, `!cwl` for a CWL reference.

`!cwl` is passed through exactly as written, so what it names has to be a name
that survives into the emitted CWL. A workflow input does. **A step id does
not**: steps are renamed on emission to `<workflow>__step__<n>__<id>`, so
`!cwl echo/stdout` emits a reference to a step that no longer exists under
that name. To consume a step's output, use `!*` and let Sophios name the
producer; `!cwl` with an emitted id would work but ties the document to a
name that changes when the workflow is embedded or inlined.

`!ii` accepts any YAML value, not just scalars:

```yaml
in:
  config: !ii
    pdb_code: 1aki
```

A literal must already have the declared type of the port it binds. `!ii 2.9` on an `int`
input is `wic020`, as is `!ii 1` on a `boolean` one, `!ii '007'` on an `int` one, and a
number written as text on a `float` one. Two conversions remain, because neither loses
anything: an integer on a `float` port is that float (`!ii 1` is `1.0`; an integer a float
cannot hold exactly is `wic020`), and a scalar on a `string` port is its text, because the
tagged spelling cannot write the text `"20"` without it being read as the number. A boolean
is `true` or `false`, as inside a mapping; a number is written the way Python prints it
(`!ii 1.0e-5` is `"1e-05"`).

YAML reads `1e-5` and `1E3` as text, not as floats: a float needs a decimal point and a
signed exponent. `!ii 1e-5` on a `float` port is therefore `wic020`; write `1.0e-5`.

An **untagged mapping or sequence** in input position is an inline literal —
the same as writing `!ii` — because a collection cannot name a workflow input,
so a literal is its only possible meaning. The tag is still the recommended
spelling: it states the intent instead of leaving it to be inferred. The one
exception is a mapping whose keys are all fields of CWL's step input
(`source`, `default`, `valueFrom`, `linkMerge`, `pickValue`, `loadContents`,
`loadListing`, `label`): `{source: x}` or `{default: 20}` reads as CWL that
Sophios does not interpret there, so it is `wic038`, not a literal. Write
`!ii {default: 20}` for a literal of that shape.

A tag outside the four above is an error, not a fourth-and-a-half form, but
for two different reasons. An *unknown* tag (`!foo`) is `wic009`, and the
loader has always rejected such documents too. `!&` is different: it is a
known tag in the wrong position, so it is `wic019` (§4.1.1) and the loader
does **not** reject it — `anchor_constructor` is registered unconditionally.
The syntax layer is deliberately stricter than the loader here. It may never
be more permissive; stricter is how a construct with no meaning stops being
accepted.

### 4.1.1 `!&` is not an input form

`!&` defines an edge, and an edge is defined where its value comes into being
— on an **output** (§3.3). That is a rule about *position*, not about inputs:
an edge definition anywhere other than an `out:` entry is `wic019`, whether it
appears in an `in:` binding, inside an `!ii` payload, in the `wic:` block, or
at the top level. Both spellings are treated alike, since §6.1 makes them
equivalent.

```yaml
in:
  f: !& name        # error: !& defines an edge and belongs on an out: entry
```

Two reasons, and they agree.

**A name has to name something.** Every `source:` Sophios emits is either a
workflow input or `step/output` — those are the only two addresses CWL has. A
step's *input port* has no address, so an edge anchored there would have
nothing for `!*` to point at; resolving it would mean chasing back to whatever
feeds that input, which is what you would have written in the first place.

**Anchors define, aliases consume.** The notation is borrowed from YAML, where
`&` names a node and `*` refers to one. Here the pairing follows the direction
of dataflow: an output is where a value originates, so that is where it earns
a name; an input is where a value arrives, already named upstream. Anchoring
at a sink names something that is by definition already named.

YAML itself permits an anchor on any node. Sophios is narrower than YAML here,
deliberately — this is a language, not a schema over arbitrary YAML, and a
construct that cannot be given a meaning is not one the grammar should admit.
The parser reports the position with a span rather than leaving the compiler to
guess at intent much later.

If you meant to *consume* an edge, you want `!*`. If you meant to name this
step's output, the `!&` belongs in its `out:` list.

### 4.1.2 An edge reference names a definition

`!* name` names an edge that `!& name` defines. The definition must appear
**before** the reference, in the document itself or in an enclosing one that
has already been compiled past the definition, and there must be only one:

- a reference with no definition is `wic025`;
- a name defined twice is `wic026`, because an edge name identifies one
  producer and a second definition leaves no way to say which output is meant.

A definition that nothing references is **not** an error. That is how a
workflow names an artifact it produces for a consumer outside itself.

Order is part of the rule. A reference is resolved against the definitions
seen so far, so `!* e` written above the `!& e` that defines it is `wic025`
even though the definition is in the same document.

A document included as a subworkflow may reference an edge its includer has
already defined; the includer's definitions are in scope when the child is
compiled.

#### A document needing a value from outside declares it

`!*` is not the way to ask for something the compilation does not produce. A
document that expects a value from whoever includes it declares a parameter in
`inputs:` and references it **by bare name**:

```yaml
inputs:
  sdf_path:
    type: File
    format: [edam:format_3814]

steps:
  convert:
    in:
      input_path: sdf_path          # a declared parameter, bound by the includer
    out:
    - output_mol2_path: !& ligand.mol2
  minimize:
    in:
      input_mol2_path: !* ligand.mol2   # an edge, defined above
```

The two spellings answer different questions. A bare name asks the *includer*
(or the user, when the document is compiled alone) for a value; `!*` asks the
*compilation* for an edge. A document's `inputs:` block is therefore its
interface, and saying what it expects is what distinguishes a workflow that is
complete from one that is meant to be included.

### 4.2 Every name is bound once

A mapping the language owns may bind each key only once — inputs in `in:`,
step names in mapping-form `steps:`, `wic:` entries, `wic: steps:` keys, and
top-level or step-level passthrough alike. A step body may also not carry an
`id:` of its own when its identity already comes from a mapping key: two
identities for one step is a mistake worth reporting, not resolving. Binding twice is an error, not a last-one-wins:

```yaml
in:
  f: !ii a
  f: !ii b     # error: input 'f' is bound more than once
```

YAML itself leaves repeated keys undefined, so honouring either binding would
mean choosing silently on the writer's behalf. The second binding is almost
always a copy-paste mistake, and saying so costs less than debugging the one
that got dropped.

### 4.3 Interpreted CWL keys

The complete set Sophios reads and acts upon:

```
scatter    scatterMethod    when    run
```

Everything else on a step is passthrough.

Each `scatter:` entry must name an input of its step. On a subworkflow call,
those are exactly the inputs the subworkflow declares in its `inputs:`, the
same names its caller's `in:` may bind. A name the compiler generates for a
step inside the subworkflow, such as `child__step__1__touch___filename`, is
not an input of the call: depending on it would tie the caller to how the
callee's steps are laid out. To scatter over such an input, declare it in
the subworkflow and bind the inner step to it:

```yaml
# child.wic
inputs:
  filename: string
steps:
  touch:
    in:
      filename: filename

# caller
  child.wic:
    scatter: [filename]
    in:
      filename: !ii [a.txt, b.txt]
```

Any other name is `wic032`.

---

## 5. The `wic:` block

Compiler metadata. Never emitted to CWL.

```yaml
wic:
  graphviz:
    label: Protein-ligand docking
  default_implementation: gromacs
  steps:
    (1, extract):
      wic:
        graphviz:
          label: extract structures
```

Step keys inside `wic: steps:` are `(index, name)` — the index is 1-based and
the step at that position should be called `name` — or a bare `name`, for a step
whose id occurs once in the document. When both address one step, the
`(index, name)` entry applies. A key that addresses no step is ignored, and
Sophios prints one line to stderr naming the file, the key and the step actually
at that position. Sophios parses keys into a structured key; you should never
have to parse that string yourself.

The block is Sophios's own, not passthrough CWL, so it is closed, and each key
declares the shape of its value:

| Key | Value |
|---|---|
| `steps` | a mapping keyed `(index, name)` or by a unique step id |
| `graphviz` | a mapping of `label` (a non-empty string), `style` (Graphviz styles, comma separated) and `ranksame` (a list of `(index, name)` keys), each optional |
| `implementation`, `default_implementation`, `version`, `lang_version`, `namespace` | a non-empty string |
| `implementations` | a mapping |
| `driver` | `slurm` or `argo` |
| `inlineable` | `true` or `false` |

An entry under `steps:` may also say something about the step it names:
`in`, `out`, `scatter` and `inference`, whose values are not checked here, and
`scatterMethod`, one of `dotproduct`, `flat_crossproduct` and
`nested_crossproduct`. Any other key is `wic033`, reported at the key; a value
of the wrong shape is `wic034`, reported at the value. The parser and the
schema (§6.3) read one declaration of these, `Grammar.SIDECAR_VALUES`.

A bare `wic:` with nothing under it is an empty block, not an error. Nested
step entries keep their `wic:` wrapper through a render — every consumer reads
through it — and an empty block renders as `{}`, never as a null.

---

## 6. How the two surfaces adhere

This is the part that keeps the language single.

### 6.1 Two spellings per construct

Within the YAML surface, every Sophios-owned construct has a **tagged** form
and a **desugared** form, and they are equivalent:

| Construct | Tagged | Desugared |
|---|---|---|
| Inline literal | `!ii value` | `{wic_inline_input: value}` |
| Edge definition (`out:` only — §4.1.1) | `!& name` | `{wic_anchor: name}` |
| Edge reference | `!* name` | `{wic_alias: name}` |
| Raw CWL reference | `!cwl expr` | `{wic_raw_cwl: expr}` |

The desugared form exists for a specific reason: a YAML constructor that
re-emitted its own tag would fire again when the document is reloaded, so the
loader would not be idempotent. Machine-generated documents therefore use the
desugared spelling — the Python API emits it, and skips the sugar entirely.

**Both spellings are written by hand.** Every layer Sophios exposes is meant to
be one a person can read and edit, and that includes the document a tool just
emitted. Neither spelling is a lesser citizen.

#### `wic_` in construct position

Where an input value is expected, a **single-key mapping whose key begins
`wic_`** is read as one of the constructs above. If it is not one of them, it is
`wic024`.

This exists because the two spellings were equally *accepted* and unequally
*safe*. A tag is a closed namespace, so `!iii` is `wic009` at once. A desugared
key shares its namespace with passthrough CWL, which is open by definition
(§1), so `wic_inline_inpt` was indistinguishable from a key the compiler should
carry through untouched: the construct silently vanished and the typo rode into
the emitted document. Since both spellings are authorable, that is a
hand-written mistake as much as a generated one.

**Only construct position is claimed.** A *name* may carry the prefix — an input
port called `wic_` or `wic_port` is legal, because names are the user's to
choose. So is any passthrough key: `wic`, `wicked` and `my_wic_key` are ordinary
CWL and pass through untouched. The rule reaches exactly the place a construct
could have been meant, and no further.

### 6.2 What each surface must do

**`.wic` files** are the YAML surface as written. They are parsed by
`sophios.lang.parse`, which accepts both spellings above, and written by
`sophios.lang.render`, which emits the tagged one. The two are inverses —
a claim that lives as the round-trip property in `tests/core/test_lang_render.py`,
its single home, so a disagreement between this text and the implementation
shows up as a test failure rather than as three subtly different sentences.

**The Python API** (`Workflow`, `Step`) is the second surface of the same
language. It builds a `sophios.lang.Document` directly, compiles it through
the same door as a `.wic` file, and writes it with `sophios.lang.render`:
`Workflow.write_wic()` and `.to_wic_yaml()` emit the tagged spelling with
sequence-form steps and explicit `id:`, and `Workflow.yaml` is the same
document's `to_json` projection.

A workflow that contains a nested `Workflow` can be written two ways. With
`inline_subworkflows=False`, each nested workflow is written as its own `.wic`
file and the parent calls it by name, like any other subworkflow. The default,
inline form instead nests the child's `to_json` projection under a `subtree:`
key of the calling step. That form is what the compiler consumes in memory; it
is not yet a `.wic` document the parser accepts, because the child's edge
definitions sit inside passthrough, where they are `wic019` (§4.1.1).

Two obligations follow, and both are enforced by tests rather than convention:

1. **Whatever the Python API writes as a `.wic` file must parse.** An API that
   produced documents its own parser rejects would mean two languages wearing
   one name. This holds for a flat workflow and for nested workflows written
   with `inline_subworkflows=False`; the inline `subtree:` form above is the
   known exception.
2. **Both spellings must produce the same result.** `!ii x` and
   `{wic_inline_input: x}` are the same input, so compiling either must give
   the same answer.

The second obligation is checked as a property over generated inputs, not by
example — see `tests/core/test_lang_parser.py`.

### 6.3 The machine-readable schema

`sophios.lang.wic_schema()` exports a JSON Schema for editors. It is generated
from the AST, not written by hand, and it is the only schema Sophios produces:
`sophios --generate_schemas` writes it to `autogenerated/schemas/wic.json`.

Every field of every AST node declares how it is written, next to the field
itself:

```python
class Step:
    id:      ... = surface(Shape.IDENTITY,        'id')
    inputs:  ... = surface(Shape.INPUT_BINDINGS,  'in')
    outputs: ... = surface(Shape.OUTPUT_BINDINGS, 'out')
    passthrough: ... = surface(Shape.PASSTHROUGH)      # every unclaimed key
    span:        ... = surface(Shape.INTERNAL)         # not syntax at all
```

That declaration is the single source of truth for the mapping between the AST
and the surface, and it is what this document's tables describe in English.
The schema generator walks those declarations; the construct keys and the
`wic:` step-key pattern come from the parser's own tables. Nothing restates the
shape of a document a second time, so nothing can disagree about it.

Add a field to a node and one of two things happens: the schema gains the key,
or generation fails because the field never said how it is written. There is no
third outcome in which the schema quietly describes an older language.

`to_json`'s output is always JSON-serialisable; YAML values with no JSON
counterpart are projected — dates and datetimes become ISO-8601 strings.

It is an **over-approximation**, for two reasons that come from the language
itself rather than from any shortcut:

- **JSON has no YAML tags.** A validator sees the document after loading, so
  `!ii x` is invisible to it. The schema therefore describes the *desugared*
  projection of §6.1 — what `sophios.lang.to_json` produces.
- **Passthrough is open by definition.** Since §1 says any key outside the
  support matrix is copied through untouched, the schema cannot close any
  object that might carry passthrough CWL.

So the schema catches structural mistakes — `steps:` that is a string, `in:`
that is a list, a malformed `(index, name)` key, a `wic:` key or value §5 does
not admit — and admits everything else. The `wic:` block is not passthrough, so
it is the one object the schema closes.
It is an editor aid, not a second implementation of this document.

### 6.4 What this does *not* cover

This document defines **syntax**: whether a Sophios document is well-formed. Two
further questions are deliberately separate because they depend on the
environment, not the language:

- **Resolution** — do the step names refer to tools that exist *here*?
- **Type checking** — do the connected ports have compatible types?

A document can be perfectly well-formed and still fail to resolve on a machine
without the right plugins installed. That is not a language error.

---

## 7. Versioning

`lang_version` starts at **0.0.1**, defined against CWL v1.2. The Sophios
version and its CWL substrate move together.

The version tag is **optional and expected to stay unused**. An untagged file
is compiled at the highest `lang_version` under which that source actually
compiles — not merely the newest version available. That means:

- A file using only long-standing syntax resolves to the newest version.
- A file using syntax a later version dropped resolves to the newest version
  that still accepts it, and keeps working.
- A file using syntax only a newer version added resolves there automatically,
  with no tag required to adopt a feature.

You need a tag only to pin a file for reproducibility, or where a construct is
valid under two versions with different meanings.

The version Sophios chose is always reported — on the command line, on
`CompiledWorkflow.lang_version`, and as a `sophios:lang_version` annotation in
the emitted CWL, namespaced so the output stays valid. It can be pinned per
file (`wic: {lang_version: 0.0.1}`) or set for a whole compilation with
`--lang_version` / `Workflow.compile(lang_version=...)`; the explicit setting
beats any tag, and one compilation resolves to exactly one version tree-wide.
You should never have to guess which language your file was read as.
