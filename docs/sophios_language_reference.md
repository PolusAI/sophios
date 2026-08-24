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

## Implementation status

This document specifies the language. Two parts of it are **specified but not
yet wired into the compiler**, and are marked where they appear:

| Construct | Specified | Accepted by `sophios.lang` | Usable in a compiled workflow |
|---|---|---|---|
| `!cwl` raw CWL reference (§4.1) | Yes | Yes | **Not yet** — CR-104 |
| `lang_version` resolution (§7) | Yes | — | **Not yet** — CR-103 |

Everything else describes what Sophios does today. Writing `!cwl` in a `.wic`
file will not work until CR-104 lands, because the loader the compiler uses does
not yet know the tag. It is documented now because the specification is what
the implementation is being built against, not a record written afterwards.

---

## 1. What kind of language this is

Sophios is a **leaky abstraction over CWL, deliberately**. You write shorthand
for the common case and drop into raw CWL for anything the shorthand does not
cover. Sealing the abstraction would mean re-inventing CWL one feature at a
time and asking users to wait.

The abstraction leaks in three ways, and knowing which is which is the whole
point of this document:

| Category | What Sophios does | Examples |
|---|---|---|
| **Sophios-owned** | Consumes it; never appears in the output | `!ii`, `!&`, `!*`, `!cwl`, the `wic:` block |
| **Interpreted CWL** | Reads it *and acts on it* | `scatter`, `scatterMethod`, `when`, inline `run` |
| **Compiler-owned** | Writes or extends it at the workflow level¹ | `class`, `cwlVersion`, `inputs`, `outputs`, `requirements`, `$namespaces`, `$schemas` |
| **Passthrough CWL** | Copies it out unchanged | `hints`, `label`, `doc`, everything else |

The interpreted set is closed and listed in §4.3. **Anything not interpreted
and not compiler-owned is passthrough, by definition.** That rule is what makes
the leak a contract rather than a surprise.

¹ The compiler-owned row exists because "unchanged" has to mean unchanged.
Each key in it is treated differently, and each is pinned by a named test in
`tests/core/test_leak_boundary.py` rather than by a property — a property
broad enough to cover them would have to be weak enough to say nothing:

- `class` is **written by the compiler**: a workflow-level value you supply
  does not survive.
- `inputs` and `outputs` are **merged into**, with the compiler winning on a
  collision: entries you write survive unless the compiler generates one of
  the same name. `outputs` is additionally *read* — each entry's
  `outputSource` feeds the compiler's output mapping — so a workflow-level
  `outputs:` is interpreted, not merely tolerated.
- `cwlVersion` is **written by the compiler**: it is always the one declared
  substrate version, whatever the document says. Sophios generates constructs
  from that version — a workflow that declared `v1.0` and used `when:` used to
  keep the declaration and emit CWL that is invalid against it. Supplying the
  tag is not an error; it is ignored, with a warning naming the version that
  was used instead.
- `requirements` is **merged into**: your entries survive, and Sophios adds
  what the workflow needs — `ScatterFeatureRequirement` for a scattering step,
  `InlineJavascriptRequirement` for `when` or `valueFrom`,
  `SubworkflowFeatureRequirement` for a `.wic` step. The mapping you wrote is
  extended, not replaced, and not copied out byte-identically.
- `$schemas` is **append-only**: your entries survive and the EDAM entry is
  added once.
- `$namespaces` is **merged, with one reserved prefix**: every binding you
  write survives except `edam`, which is replaced by the canonical one.

Everything outside the compiler-owned row survives byte-identically, which is
the statement the properties in that file quantify over.

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

An empty document is well-formed and carries nothing.

---

## 3. Steps

### 3.1 Three surface forms

All three are long-standing, all remain supported, and **all produce the same
result**. Use whichever reads better.

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

**Sequence of single-key mappings** — the key is the step name:

```yaml
steps:
- touch:
    in:
      filename: !ii empty.txt
```

A step may have no body at all:

```yaml
steps:
  some_subworkflow.wic:
```

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

---

## 4. Input values

### 4.1 The five forms

A step input is exactly one of these. There is no sixth form.

| Form | Written | Means |
|---|---|---|
| Inline literal | `f: !ii empty.txt` | A literal value. Never an edge. |
| Edge definition | `f: !& name` | Defines an explicit edge at this point |
| Edge reference | `f: !* name` | Consumes an edge defined elsewhere |
| Raw CWL reference | `f: !cwl step/out` | Opaque to Sophios; passed through unresolved. **Not yet usable — awaits the Spec 3 compiler migration** |
| Unresolved name | `f: some_input` | Must resolve to a workflow input |

An untagged bare string is an **unresolved name**. If it does not name a
workflow input, you get a diagnostic telling you which of the two remedies you
probably meant — `!ii` for a literal, `!cwl` for a CWL reference.

`!ii` accepts any YAML value, not just scalars:

```yaml
in:
  config: !ii
    pdb_code: 1aki
```

An **untagged mapping or sequence** in input position is an inline literal —
the same as writing `!ii` — because a collection cannot name a workflow input,
so a literal is its only possible meaning. The tag is still the recommended
spelling: it states the intent instead of leaving it to be inferred.

A tag outside the four above (`!foo`) is an error, not a fifth-and-a-half
form. The loader has always rejected such documents, and the syntax layer
must never accept more than the language it specifies.

An **untagged mapping or sequence** in input position is an inline literal —
the same as writing `!ii` — because a collection cannot name a workflow input,
so a literal is its only possible meaning. The tag is still the recommended
spelling: it states the intent instead of leaving it to be inferred.

A tag outside the four above (`!foo`) is an error, not a fifth-and-a-half
form. The loader has always rejected such documents, and the syntax layer
must never accept more than the language it specifies.

### 4.2 Every name is bound once

A mapping the language owns may bind each key only once — inputs in `in:`,
step names in mapping-form `steps:`, `wic:` entries, `wic: steps:` keys, and
top-level or step-level passthrough alike. A step body may also not carry an
`id:` of its own when its identity already comes from a mapping key or a
single name key: two identities for one step is a mistake worth reporting,
not resolving. Binding twice is an error, not a last-one-wins:

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

Step keys inside `wic: steps:` have the form `(index, name)` — the index is
1-based and matches the step's position. Sophios parses these into a structured
key; you should never have to parse that string yourself.

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
| Edge definition | `!& name` | `{wic_anchor: name}` |
| Edge reference | `!* name` | `{wic_alias: name}` |
| Raw CWL reference | `!cwl expr` | `{wic_raw_cwl: expr}` |

The desugared form exists for a specific reason: a YAML constructor that
re-emitted its own tag would fire again when the document is reloaded, so the
loader would not be idempotent. Machine-generated documents therefore use the
desugared spelling.

**Write the tagged form.** The desugared form is what tooling emits.

### 6.2 What each surface must do

**`.wic` files** are the YAML surface as written. They are parsed by
`sophios.lang.parse`, which accepts both spellings above, and written by
`sophios.lang.render`, which emits the tagged one. The two are inverses —
a claim that lives as the round-trip property in `tests/core/test_lang_render.py`,
its single home, so a disagreement between this text and the implementation
shows up as a test failure rather than as three subtly different sentences.

**The Python API** (`Workflow`, `Step`) is the second surface of the same
language. `Workflow.write_wic()` and `.to_wic_yaml()` emit `.wic` documents,
using the desugared spelling and sequence-form steps with explicit `id:`.

Two obligations follow, and both are enforced by tests rather than convention:

1. **Whatever the Python API emits must parse.** An API that produced
   documents its own parser rejects would mean two languages wearing one name.
2. **Both spellings must produce the same result.** `!ii x` and
   `{wic_inline_input: x}` are the same input, so compiling either must give
   the same answer.

The second obligation is checked as a property over generated inputs, not by
example — see `tests/core/test_lang_parser.py`.

### 6.3 The machine-readable schema

`sophios.lang.wic_schema()` exports a JSON Schema for editors. It is generated
from the AST, not written by hand.

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
- **Passthrough is open by definition.** Since §1 says anything outside the
  interpreted set is copied through untouched, the schema cannot close any
  object that might carry passthrough CWL.

So the schema catches structural mistakes — `steps:` that is a string, `in:`
that is a list, a malformed `(index, name)` key — and admits everything else.
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

> **Not yet implemented.** This section specifies how versioning will behave;
> `lang_version` is not read or reported by any code path today. Tracked as
> CR-103. Until it lands, every file is compiled by the one implementation that
> exists, and no tag has any effect.

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

The version Sophios chose will always be reported — on the command line, on
`CompiledWorkflow`, and as an annotation in the emitted CWL. You should never
have to guess which language your file was read as.
