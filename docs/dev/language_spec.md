# The language specification

**Version:** `lang_version` 0.1.0. **Substrate:** CWL v1.2.

What must an implementation of the Sophios language do? This page is for
people who implement or change Sophios: what the language does with CWL, how
its two surfaces must agree, how it is versioned, and how its codes are
assigned. To write a workflow, read the [`.wic` reference](../wic-reference.md)
instead.

The executable definition is `sophios.lang`, the typed AST and parser; this
page and it are meant to agree, and where they disagree, that is a bug in one
of them. Every rule here is implemented and pinned by the tests it names. One
gap is known: a step input typed by a `SchemaDefRequirement` name is promoted
to a workflow input without the requirement that defines the name, so cwltool
rejects the compiled workflow (an xfail example of the validation property in
`tests/core/test_emit.py`).

## The language and its two surfaces

Sophios is the language. It has one model, and that model can be written two
ways:

| Surface | How it is written | Where it lives |
|---|---|---|
| **YAML** | the YAML spelling of the language | files named `.wic` |
| **Python API** | `Workflow`, `Step`, `StepInput` | Python source |

Neither is the language and neither is subordinate to the other; section 3
states what each owes the other and how that is checked. The `.wic`
extension, the `wic:` block, the `wic_*` desugared keys and the `wic0NN`
codes are concrete syntax that workflows depend on.

## Sophios semantics

A workflow document means a Sophios workflow graph: its steps, the tools they
run, the edges between them (written or inferred), their types, scatter,
conditions and run policy, after composition, naming and inference. Every
target is a lowering of that graph: CWL, Nextflow and any later one. Where
Sophios takes a construct from CWL (a tool's command line, scatter, `when`,
expressions), it means what CWL v1.2 says, with cwltool as the reference, so
every target is checked against one oracle. What is Sophios's own (inference,
composition, names, run policy) is decided in the graph, before any target
sees it. A workflow imported from another language is translated into this
graph through the same front door; it is never another surface.

## 1. What Sophios does with CWL

Sophios is a leaky abstraction over CWL, deliberately. The document is
shorthand for the common case, with raw CWL available for anything the
shorthand does not cover; sealing the abstraction would mean re-inventing
CWL one feature at a time.

Sophios-owned syntax (`!ii`, `!&`, `!*`, `!cwl`, the `wic:` block) is
consumed and never appears in the output. Everything else is CWL, and what
Sophios does with each field of the five workflow-level CWL v1.2 classes is
declared in one place, `sophios.lang.SUPPORT_MATRIX`
(`src/sophios/lang/support.py`). Each field is one of:

- **native**: Sophios reads it and acts on it, or writes it;
- **passthrough**: copied out unchanged;
- **rejected**: reported with a diagnostic, never silently dropped or
  coerced, positioned at the value (a step's `run` at the step, a workflow
  output at the document).

`support.py` is the source and `tests/core/test_support_matrix.py` the
check: it reads the fields of each class from `cwl_utils.parser.cwl_v1_2`,
so a field the schema has and the matrix lacks fails the build. The field-by-field
support is rendered in the "Sophios" column of [Nextflow](../nextflow.md). A field outside these five classes on a
step's tool is passthrough wholesale: Sophios does not classify
`CommandLineTool` fields.

A tool written in `.wic` is the one place Sophios owns a command line: its
`command:`, `script:`, `docker:`, `needs:`, port lines and output lines are
Sophios-owned syntax that `sophios.lang.wic_tool` reads, and the CWL target
(`sophios.cwl.lowering`) spells as a `CommandLineTool`; every other key of the document is CWL,
copied. A line adds CWL and never changes CWL that is there: a place a line
writes that the author wrote too is `wic058`, found by one walk over a table
of owned places, and `script:` owns the whole command line. The result means
what CWL v1.2 says.

### 1.1 The native rows of `Workflow`

- `class` is written by the compiler: a value you supply does not survive.
- `inputs` and `outputs` are merged into, the compiler winning on a
  collision. `outputs` is also read: each entry's `outputSource` names one
  step output. An output with no `type:` takes the type of the step output it
  names, as an array when that step scatters; one with no producer to take it
  from is `wic036`. A list `outputSource:`, `linkMerge` or `pickValue` on an
  output is `wic038`. A list of `inputs` or `outputs` holding an `$import` or
  `$include` is the exception (section 1.4).
- `cwlVersion` is written by the compiler: always the substrate version.
  `v1.0`, `v1.1` and `v1.2` are accepted in a document; any other value is
  `wic035`, reported by the parser at the value.
- `requirements` is merged into with `setdefault`, so an authored body
  survives: `ScatterFeatureRequirement` for a scattering step,
  `InlineJavascriptRequirement` for `when`, `SubworkflowFeatureRequirement`
  for a `.wic` step, `MultipleInputFeatureRequirement` and
  `StepInputExpressionRequirement` for records, each only when the class is
  not already there. A `requirements:` list holding an `$import` or `$include`
  is emitted as written and nothing is added to it.

### 1.2 Compiler-owned keys outside the schema

- `$schemas` is what the author wrote: authored entries survive, and the
  compiler adds none, so a document with none has no `$schemas` key.
- `$namespaces` is merged: every authored binding survives, except `edam` in a
  document that uses EDAM (a `format` of a workflow input or output, of a File in
  an input's or a step input's `default:`, or of a File in the job inputs, is an
  `edam:` CURIE), which the compiler binds to
  EDAM. The compiler binds no prefix and writes no key of its own. A port the
  document promotes from a step keeps that port's `format:` CURIE, so the
  document also declares each prefix such a format uses, bound as the step's
  tool or subworkflow binds it. If the document and the steps whose promoted
  ports use a prefix do not all bind it to one URI, that is `wic031`; a tool
  that binds the prefix differently but promotes no port using it conflicts
  with nothing, so the result does not depend on how the workflow is split
  into subworkflows. Pinned by the example `tests/core/data/compiler/boundary`.

### 1.3 Passthrough

Any other top-level or step key survives byte-identically, the statement the
properties in `tests/core/test_leak_boundary.py` quantify over. The exception
is the list form of `hints:` and of a step's `requirements:`, which the
parser reads as the mapping form (section 1.4) and writes out as one, unless
the list holds an `$import` or `$include` entry.

### 1.4 List forms

`inputs`, `outputs`, `requirements` and `hints` may be written in CWL's list
form, and a step's `requirements` and `hints` likewise. The parser reads the
list as the mapping form. A port's `id:` may be a fragment (`#name` or
`file.cwl#name`); its key is the name after the last `#`. An entry without
its `id:` or `class:`, and one naming a key twice, are reported.

A list holding an `$import` or `$include` entry is left as written, and its
other entries are not checked: a `requirements:` or `hints:` list reaches the
output for cwltool to resolve, and Sophios adds nothing to it; a list of
`inputs:` or `outputs:` so left is not carried into the compiled document,
and in a workflow another calls it is `wic013`.

### 1.5 The sequence-of-single-key-mappings step form

CWL v1.2 types `Workflow.steps` as an array of `WorkflowStep` with
`jsonldPredicate: {mapSubject: id}`, and Schema Salad lifts a key into `id`
only when the value is a mapping. When `steps:` is already an array, each
item is a plain `WorkflowStep` with no field named after the step, and
cwltool fails such a document with `unknown identifier`. Sophios inherits the
rule: the form is `wic006`, and the message names the two forms that exist.

## 2. Two spellings per construct

Within the YAML surface, every Sophios-owned construct has a tagged form and
a desugared form, and they are equivalent:

| Construct | Tagged | Desugared |
|---|---|---|
| Inline literal | `!ii value` | `{wic_inline_input: value}` |
| Edge definition (`out:` only) | `!& name` | `{wic_anchor: name}` |
| Edge reference | `!* name` | `{wic_alias: name}` |
| Raw CWL reference | `!cwl expr` | `{wic_raw_cwl: expr}` |
| Step-input record | `!cwl {source: [!* a, b]}` | `{wic_raw_cwl: {source: [{wic_alias: a}, b]}}` |

The desugared form is plain YAML, so a tool that cannot write YAML tags, or a
JSON consumer, can spell every construct. `sophios.lang.to_json` produces it,
and the editor schema (section 4) describes it. Both spellings are for
writing by hand; neither is a lesser citizen.

The syntax layer reads YAML with `yaml.SafeLoader` and may never be more
permissive than it. The Sophios tags mean what this specification says. An
unknown tag is `wic009`; `!&` in any position other than an `out:` entry is
`wic019`.

A tag switches off YAML's implicit typing, so the payload of `!ii` is read as
`yaml.SafeLoader` would read the same scalar untagged: a plain scalar by what
it says (`!ii 5` is the integer 5), a quoted or block scalar as its text
(`!ii '5'` is the string `5`). The desugared spelling has no tag, so YAML's
own reading gives it the same rule. The writer keeps the distinction: a
string YAML would read as something else is written quoted.

### 2.1 `wic_` in construct position

Where an input value is expected, a single-key mapping whose key begins
`wic_` is read as one of the constructs above, and otherwise is `wic024`. A
tag is a closed namespace, so `!iii` is `wic009` at once; a desugared key
shares its namespace with passthrough CWL, which is open, so without this
rule a misspelling such as `wic_inline_inpt` would ride into the emitted
document as a passthrough key. Only construct position is claimed: a name
may carry the prefix, and `wic`, `wicked` and `my_wic_key` are ordinary
passthrough keys.

## 3. What each surface must do

**`.wic` files** are the YAML surface as written. They are parsed by
`sophios.lang.parse`, which accepts both spellings, and written by
`sophios.lang.render`, which emits the tagged one; the two are inverses, the
round-trip property in `tests/core/test_lang_render.py`. A nested step entry
keeps its `wic:` wrapper through a render, and an empty `wic:` block renders
as `{}`, never as a null (`tests/core/data/render`).

**The Python API** (`Workflow`, `Step`) is the second surface of the same
language. It builds a `sophios.lang.Document` directly, compiles it through
the same door as a `.wic` file, and writes it with `sophios.lang.render`:
`Workflow.write_wic()` emits the tagged spelling with sequence-form steps and
explicit `id:`. `Workflow.from_wic()` reads a document back into objects; a
construct the Python API cannot hold is `api006`.

A workflow that contains a nested `Workflow` is written as a bundle: each
nested workflow is its own `.wic` file and the parent calls it by name.
Workflow outputs name their authored step, and every step carries
`run: <stem>.cwl`, the tool written beside the document, except a real-time
declaration, which names the adapter by its registry stem
(`run: cwl_subinterpreter`) so that it stays a declaration.

Two obligations follow, both enforced by tests:

1. Whatever the Python API writes as a `.wic` file must parse, for every
   workflow, nested or not
   (`tests/core/test_python_api_workflow.py`).
2. Both spellings must produce the same result: `!ii x` and
   `{wic_inline_input: x}` are the same input, checked as a property over
   generated inputs in `tests/core/test_lang_parser.py`. And both surfaces
   must produce the same result for the same workflow:
   `tests/core/test_cookbook.py` compiles every pair both ways.

## 4. The machine-readable schema

`sophios.lang.wic_schema()` exports a JSON Schema for editors, generated from
the AST and the only schema Sophios produces; `sophios --generate_schemas`
writes it to `<basepath>/schemas/wic.json` (`autogenerated/schemas/wic.json` by default).

Every field of every AST node declares how it is written, next to the field
itself:

```text
class Step:
    id:      ... = surface(Shape.IDENTITY,        'id')
    inputs:  ... = surface(Shape.INPUT_BINDINGS,  'in')
    outputs: ... = surface(Shape.OUTPUT_BINDINGS, 'out')
    passthrough: ... = surface(Shape.PASSTHROUGH)      # every unclaimed key
    span:        ... = surface(Shape.INTERNAL)         # not syntax at all
```

That declaration is the single source of truth for the mapping between the
AST and the surface. The generator walks it; the construct keys and the
`wic:` step-key pattern come from the parser's tables, and the `wic:` block's
keys and value shapes from `Grammar.SIDECAR_VALUES`, which the parser reads
too. Add a field to a node and either the schema gains the key or generation
fails because the field never said how it is written.

`to_json`'s output is always JSON-serialisable: dates and datetimes become
ISO-8601 strings.

The schema is an over-approximation, for two reasons that come from the
language: JSON has no YAML tags, so the schema describes the desugared
projection; and passthrough is open, so the schema cannot close any object
that might carry CWL. It catches structural mistakes and admits everything
else. The `wic:` block is not passthrough, so it is the one object the
schema closes.

## 5. What this does not cover

The language defines syntax: whether a document is well formed. Two further
questions depend on the environment and are deliberately separate:
resolution, whether the step names refer to tools that exist here; and type
checking, whether connected ports are compatible. A well-formed document can
fail to resolve on a machine without the right tools, and that is not a
language error.

## 6. Versioning

`lang_version` is 0.1.0 (0.0.1 is still read), defined against CWL v1.2; the
language version and its CWL substrate move together. 0.1.0 added the tool
document (`command:` or `script:`, [`.wic` reference §2.4](../wic-reference.md#24-a-tool-document)):
a compile pinned to 0.0.1 that reaches a tool is `wic060`, and a file with no
tool compiles unchanged under either. The version tag is optional and expected to
stay unused: an untagged file is compiled at the highest `lang_version`
under which it compiles. A file using only long-standing syntax resolves to
the newest version; one using syntax a later version dropped resolves to the
newest version that still accepts it; one using syntax only a newer version
added resolves there without a tag. A tag is for pinning a file, or for a
construct valid under two versions with different meanings.

The version chosen is always reported: on the command line and on
`CompiledWorkflow.lang_version`. The emitted CWL does not carry it, so it
stays plain CWL. It can be pinned
per file (`wic: {lang_version: 0.1.0}`) or set for a whole compilation
(`--lang_version`, `Workflow.compile(lang_version=...)`); the explicit setting
beats any tag, and one compilation resolves to exactly one version tree-wide.
An unknown version is `wic017`; pins that disagree within one compilation are
`wic018`.

## 7. Diagnostic codes

Codes are contract: a caller matches on `SophiosErrorCode`, suppresses a
code, or reads it out of a log, while message wording is free to improve. The
enum is `sophios.lang.error_codes.SophiosErrorCode`, in three ranges: `wic0NN` for a
document, or for what running it needs from the machine; `api0NN` for the Python
API, where the document is fine and the call was not; and `nf-sNNN` for Nextflow
support, where the workflow is fine and the target cannot run it yet. A code
is never renumbered, and a code that shipped in a release is never reused;
`wic040`, `wic041` and `wic042` are unassigned. `wic043` is a note, an
error under `--inference_strict`.

Every member is provoked: a fixture case under `tests/core/data` states it and
its test compares codes exactly, or, for a code no fixture states, a callable in
`tests/core/provocations.py` raises it. `tests/core/test_lang_parser.py` fails
for a member nothing provokes, so a new code lands with its provocation in the
same commit.
Every member has an `Explanation` in `EXPLANATIONS` (kind, meaning, fix);
`tests/core/test_diagnostics.py` fails for one without, and keeps
`sophios --explain` equal to it; the [error codes page](../error_codes.md)
is built from it.
