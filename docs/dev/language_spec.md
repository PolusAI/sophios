# The Sophios Language Specification

**Version:** `lang_version` 0.0.1
**Substrate:** CWL v1.2

This page is for people who implement or change Sophios: what the language does with
CWL, how its two surfaces must agree, and how it is versioned. To write a workflow,
read the [language guide](../language_guide.md) instead.

The executable definition is `sophios.lang`, the typed AST and parser; this page and
it are meant to agree, and where they disagree, that is a bug in one of them. Every
rule here is implemented and pinned by the tests it names. One gap is known: a step
input typed by a `SchemaDefRequirement` name is promoted to a workflow input without
the requirement that defines the name, so cwltool rejects the compiled workflow
(`test_a_schema_def_typed_input_validates` in `tests/core/test_emit.py`, a strict
xfail).

## The language and its two surfaces

Sophios is the language. It has one DSL, and that DSL can be written two ways:

| Surface | How it is written | Where it lives |
|---|---|---|
| **YAML** | The YAML-based spelling of the DSL | Conventionally in files named `.wic` |
| **Python API** | `Workflow`, `Step`, `CommandLineTool` | Python source |

Neither is "the language" and neither is subordinate to the other. They are two ways
of saying the same thing, which is why section 3 can state what each owes the other
and check it.

`.wic` is **a file extension, not a language**. A handful of spellings inside the
syntax still carry the older `wic` prefix: the `wic:` block, the `wic_*` desugared
keys, and the `wic0NN` diagnostic codes. Those are concrete syntax that existing
workflows depend on, so they stay as they are.

## 1. What Sophios does with CWL

Sophios is a **leaky abstraction over CWL, deliberately**. You write shorthand for
the common case and drop into raw CWL for anything the shorthand does not cover.
Sealing the abstraction would mean re-inventing CWL one feature at a time and asking
users to wait.

Sophios-owned syntax (`!ii`, `!&`, `!*`, `!cwl`, the `wic:` block) is consumed and
never appears in the output. Everything else is CWL, and what Sophios does with each
field of the five workflow-level CWL v1.2 classes is declared in one place:
`sophios.lang.SUPPORT_MATRIX` (`src/sophios/lang/support.py`). Each field is one of

- **native**: Sophios reads it and acts on it, or writes it;
- **passthrough**: copied out unchanged;
- **rejected**: reported with a positioned diagnostic, never silently dropped or
  coerced.

`support.py` is the source; `tests/core/test_support_matrix.py` is the check. It
reads the fields of each class from `cwl_utils.parser.cwl_v1_2`, so a field the
schema has and the matrix lacks fails the build, and so does a row whose pinning
test does not exist. The tables below are the matrix at the time of writing; where
they disagree, `support.py` wins. A test named without a file is in
`tests/core/test_leak_boundary.py`. A field outside these five classes on a step's
tool is passthrough wholesale: Sophios does not classify `CommandLineTool` fields.

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
| `run` | native | a registry stem, a path relative to the document, or an inline body | `test_an_inline_run_body_is_registered_and_emitted_as_its_own_tool` (`test_frontdoor.py`) |
| `scatter` | native | | `test_every_declared_key_is_interpreted` |
| `scatterMethod` | native | | `test_every_declared_key_is_interpreted` |
| `when` | native | | `test_every_declared_key_is_interpreted` |

**`WorkflowStepInput`** (an entry of a step's `in:`)

| Field | Support | Note | Pinned by |
|---|---|---|---|
| `id` | native | the `in:` key | `test_input_values_are_closed` (`test_lang_parser.py`) |
| `source` | native | `!*`, a bare workflow-input name, or a list of them inside `!cwl {source: [...]}` | `test_input_values_are_closed` (`test_lang_parser.py`) |
| `default` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `label` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `linkMerge` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `loadContents` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `loadListing` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `pickValue` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |
| `valueFrom` | native | through `!cwl {...}` | `test_a_cwl_record_emits_the_fields_it_carries` (`test_emit.py`) |

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

### 1.1 The native rows of `Workflow`

- `class` is **written by the compiler**: a workflow-level value you supply does not
  survive.
- `inputs` and `outputs` are **merged into**, with the compiler winning on a
  collision: entries you write survive unless the compiler generates one of the same
  name. `outputs` is additionally *read*: each entry's `outputSource` names one step
  output, which feeds the compiler's output mapping. An output with no `type:` takes
  the type of the step output its `outputSource:` names, as an array when that step
  scatters. One whose `outputSource:` names no step output of the workflow, or that
  has none, has no type to take and is `wic036`. A list `outputSource:`, `linkMerge`
  or `pickValue` on an output is `wic038`. A list of `inputs` or `outputs` holding an
  `$import` or `$include` is the exception (section 1.4): the compiler models only
  the mapping form, so it is neither merged into nor carried into the output.
- `cwlVersion` is **written by the compiler**: it is always the one declared
  substrate version, whatever the document says. Supplying `v1.0`, `v1.1` or `v1.2`
  is not an error, and the emitted document says the substrate version. Any other
  value is `wic035`, reported by the parser at the value: accepting a version is a
  promise to process it, and the toolchain processes no other.
- `requirements` is **merged into**: the compiler adds what the workflow needs with
  `setdefault`, so an authored body survives. It adds `ScatterFeatureRequirement`
  for a scattering step, `InlineJavascriptRequirement` for `when`, and
  `SubworkflowFeatureRequirement` for a `.wic` step, each only when the class is not
  already there: an authored `InlineJavascriptRequirement: {expressionLib: [...]}`
  survives a `when`. A `requirements:` list holding an `$import` or `$include` is the
  exception (section 1.4): it is emitted as written, Sophios adds nothing to it, and
  the imported file has to supply what the workflow needs, such as the
  `ScatterFeatureRequirement` of a scattering step.

### 1.2 Compiler-owned keys outside the schema

Two document keys are not schema fields of `Workflow` but are written by the
compiler too:

- `$schemas` is **append-only**: authored entries survive and the EDAM entry is
  added once.
- `$namespaces` is **merged, with two reserved prefixes**: every authored binding
  survives except `edam` and `sophios`, which are replaced by the canonical ones
  (section 6 for `sophios:lang_version`). A port the document promotes from a step
  keeps that port's `format:` CURIE, so the document also declares each prefix such
  a format uses, bound as the step's tool or subworkflow binds it. If the document
  and the steps whose promoted ports use that prefix do not all bind it to the same
  URI, that is `wic031`: the CURIE could only mean one of them. Those bindings are
  the only ones that count. A tool that binds the prefix differently but promotes no
  port using it conflicts with nothing, wherever the step sits, so compiling does
  not depend on how the workflow is split into subworkflows. A prefix no promoted
  format uses is not declared for you. An authored port keeps the prefixes the
  document bound. What a tool binds `edam` or `sophios` to is ignored, since the
  compiler binds both. Pinned by `test_user_namespaces_survive_except_edam` and
  `test_the_sophios_namespace_prefix_is_reserved`, and in `tests/core/test_emit.py`
  by `test_a_prefix_two_promoting_sources_bind_differently_is_reported`,
  `test_a_clash_on_a_prefix_no_promoted_format_uses_is_no_error` and
  `test_a_step_promoting_no_format_with_a_prefix_is_no_source_for_it_wherever_it_sits`.

### 1.3 Passthrough

Any other top-level or step key is passthrough and survives byte-identically, which
is the statement the properties in `tests/core/test_leak_boundary.py` quantify over.
The exception is the list form of `hints:` (on the document or on a step) and of a
step's `requirements:`, which the parser reads as the mapping form (section 1.4) and
which is written out as one, unless the list holds an `$import` or `$include`
entry: that list is not read and survives as written.

### 1.4 List forms

`inputs`, `outputs`, `requirements` and `hints` may be written in CWL's list form
(`- id: x` / `- class: X`), and a step's `requirements` and `hints` likewise. The
parser reads the list as the mapping form, so what follows it sees the mapping. A
port's `id:` may be written as a fragment (`#name` or `file.cwl#name`); its mapping
key is the name after the last `#`, so `a` and `#a` name the same port. An entry
without its `id:` or `class:` (or whose `id:` has nothing after the `#`), and one
naming the same key twice, are reported.

A list holding an `$import` or `$include` entry is the exception. That entry names no
`id:` or `class:` until cwltool has read its file, so the whole list is left as
written, and its other entries are not checked. A `requirements:` or `hints:` list
reaches the output as written, for cwltool to resolve, and Sophios adds no
requirement to a `requirements:` list so left. The compiler models only the mapping
form of `inputs:` and `outputs:`, so a list of those left as written is not carried
into the output of the document compiled. In a workflow that another calls it is
`wic013`: no step can be checked against ports that only cwltool can read.

### 1.5 The sequence-of-single-key-mappings step form

CWL v1.2 types `Workflow.steps` as an array of `WorkflowStep` and attaches
`jsonldPredicate: {mapSubject: id}`, and Schema Salad applies that transformation
only *"if the value of the field is a JSON object"*. When `steps:` is already an
array, no key is lifted into `id`, so each item is a plain `WorkflowStep`, and a
`WorkflowStep` has no field named after the step. `cwltool` fails such a document
with `unknown identifier`. Sophios inherits the rule and reports the form as
`wic006`, naming the two forms that exist.

Until the May 2024 normal-form refactor (`9758e81`), which made the compiler read a
step's name from `id:` and rewrote every tutorial into the `id:` form, it was the
only sequence form that worked. That break is ratified rather than reverted: the
substrate does not admit the form, so a document using it breaks the moment it meets
raw CWL.

## 2. Two spellings per construct

Within the YAML surface, every Sophios-owned construct has a **tagged** form and a
**desugared** form, and they are equivalent:

| Construct | Tagged | Desugared |
|---|---|---|
| Inline literal | `!ii value` | `{wic_inline_input: value}` |
| Edge definition (`out:` only) | `!& name` | `{wic_anchor: name}` |
| Edge reference | `!* name` | `{wic_alias: name}` |
| Raw CWL reference | `!cwl expr` | `{wic_raw_cwl: expr}` |
| Step-input record | `!cwl {source: [!* a, b]}` | `{wic_raw_cwl: {source: [{wic_alias: a}, b]}}` |

The desugared form exists for a specific reason: a YAML constructor that re-emitted
its own tag would fire again when the document is reloaded, so the loader would not
be idempotent. `sophios.lang.to_json` produces the desugared spelling, and the
editor schema (section 4) describes it.

**Both spellings are written by hand.** Every layer Sophios exposes is meant to be
one a person can read and edit, and that includes the document a tool just emitted.
Neither spelling is a lesser citizen.

The syntax layer is stricter than the YAML loader, and may never be more permissive.
An unknown tag (`!foo`) is `wic009`, which the loader has always rejected too. `!&`
in any position other than an `out:` entry is `wic019`, although the loader accepts
it (`anchor_constructor` is registered unconditionally): it is a known tag in a
position with no meaning.

### 2.1 `wic_` in construct position

Where an input value is expected, a **single-key mapping whose key begins `wic_`** is
read as one of the constructs above. If it is not one of them, it is `wic024`.

This exists because the two spellings were equally *accepted* and unequally *safe*. A
tag is a closed namespace, so `!iii` is `wic009` at once. A desugared key shares its
namespace with passthrough CWL, which is open by definition (section 1), so
`wic_inline_inpt` was indistinguishable from a key the compiler should carry through
untouched: the construct silently vanished and the typo rode into the emitted
document.

**Only construct position is claimed.** A *name* may carry the prefix: an input port
called `wic_` or `wic_port` is legal, because names are the user's to choose. So is
any passthrough key: `wic`, `wicked` and `my_wic_key` are ordinary CWL and pass
through untouched.

## 3. What each surface must do

**`.wic` files** are the YAML surface as written. They are parsed by
`sophios.lang.parse`, which accepts both spellings above, and written by
`sophios.lang.render`, which emits the tagged one. The two are inverses, a claim
that lives as the round-trip property in `tests/core/test_lang_render.py`.
A nested step entry keeps its `wic:` wrapper through a render, since every consumer
reads through it, and an empty `wic:` block renders as `{}`, never as a null
(`test_empty_sidecar_renders_as_mapping_not_null`).

**The Python API** (`Workflow`, `Step`) is the second surface of the same language.
It builds a `sophios.lang.Document` directly, compiles it through the same door as a
`.wic` file, and writes it with `sophios.lang.render`: `Workflow.write_wic()` and
`.to_wic_yaml()` emit the tagged spelling with sequence-form steps and explicit
`id:`, and `Workflow.yaml` is the same document's `to_json` projection.

A workflow that contains a nested `Workflow` is written as a bundle: each nested
workflow is its own `.wic` file and the parent calls it by name, like any other
subworkflow. Workflow outputs name their authored step, and a step named apart from
its tool's file stem carries `run: <stem>.cwl`, the tool written beside the
document.

Two obligations follow, and both are enforced by tests rather than convention:

1. **Whatever the Python API writes as a `.wic` file must parse**, for every
   workflow, nested or not
   (`test_python_api_emits_documents_this_parser_accepts` in
   `tests/core/test_lang_parser.py`).
2. **Both spellings must produce the same result.** `!ii x` and
   `{wic_inline_input: x}` are the same input, so compiling either must give the
   same answer. This is checked as a property over generated inputs in
   `tests/core/test_lang_parser.py`.

## 4. The machine-readable schema

`sophios.lang.wic_schema()` exports a JSON Schema for editors. It is generated from
the AST, not written by hand, and it is the only schema Sophios produces:
`sophios --generate_schemas` writes it to `autogenerated/schemas/wic.json`.

Every field of every AST node declares how it is written, next to the field itself:

```python
class Step:
    id:      ... = surface(Shape.IDENTITY,        'id')
    inputs:  ... = surface(Shape.INPUT_BINDINGS,  'in')
    outputs: ... = surface(Shape.OUTPUT_BINDINGS, 'out')
    passthrough: ... = surface(Shape.PASSTHROUGH)      # every unclaimed key
    span:        ... = surface(Shape.INTERNAL)         # not syntax at all
```

That declaration is the single source of truth for the mapping between the AST and
the surface. The schema generator walks those declarations; the construct keys and
the `wic:` step-key pattern come from the parser's own tables, and the `wic:` block's
keys and value shapes from `Grammar.SIDECAR_VALUES`, which the parser reads too.
Nothing restates the shape of a document a second time, so nothing can disagree
about it.

Add a field to a node and one of two things happens: the schema gains the key, or
generation fails because the field never said how it is written. There is no third
outcome in which the schema quietly describes an older language.

`to_json`'s output is always JSON-serialisable; YAML values with no JSON counterpart
are projected: dates and datetimes become ISO-8601 strings.

The schema is an **over-approximation**, for two reasons that come from the language
itself:

- **JSON has no YAML tags.** A validator sees the document after loading, so
  `!ii x` is invisible to it. The schema therefore describes the *desugared*
  projection of section 2, what `sophios.lang.to_json` produces.
- **Passthrough is open by definition.** Since any key outside the support matrix
  is copied through untouched (section 1), the schema cannot close any object that
  might carry passthrough CWL.

So the schema catches structural mistakes (`steps:` that is a string, `in:` that is
a list, a malformed `(index, name)` key, a `wic:` key or value the block does not
admit) and admits everything else. The `wic:` block is not passthrough, so it is the
one object the schema closes. It is an editor aid, not a second implementation of
the language.

## 5. What this does not cover

The language defines **syntax**: whether a Sophios document is well-formed. Two
further questions are deliberately separate because they depend on the environment,
not the language:

- **Resolution**: do the step names refer to tools that exist *here*?
- **Type checking**: do the connected ports have compatible types?

A document can be perfectly well-formed and still fail to resolve on a machine
without the right plugins installed. That is not a language error.

## 6. Versioning

`lang_version` starts at **0.0.1**, defined against CWL v1.2. The Sophios version and
its CWL substrate move together.

The version tag is **optional and expected to stay unused**. An untagged file is
compiled at the highest `lang_version` under which that source actually compiles,
not merely the newest version available. That means:

- A file using only long-standing syntax resolves to the newest version.
- A file using syntax a later version dropped resolves to the newest version that
  still accepts it, and keeps working.
- A file using syntax only a newer version added resolves there automatically, with
  no tag required to adopt a feature.

A tag is needed only to pin a file for reproducibility, or where a construct is
valid under two versions with different meanings.

The version Sophios chose is always reported: on the command line, on
`CompiledWorkflow.lang_version`, and as a `sophios:lang_version` annotation in the
emitted CWL, namespaced so the output stays valid. It can be pinned per file
(`wic: {lang_version: 0.0.1}`) or set for a whole compilation with `--lang_version`
/ `Workflow.compile(lang_version=...)`; the explicit setting beats any tag, and one
compilation resolves to exactly one version tree-wide. An unknown version is
`wic017`; pins that disagree within one compilation are `wic018`.

## 7. Diagnostic codes

Codes are contract: a caller matches on `SophiosErrorCode`, suppresses a code, or
reads it out of a log, while message wording is free to improve. The enum is
`sophios.lang.error_codes.SophiosErrorCode`, in two ranges: `wic0NN` for the
language (a document said something the language does not accept) and `api0NN` for
the Python API (the document is fine, the call was not). A code is never renumbered
or reused: `wic021` is retired, and `wic029`, `wic037`, `wic040` and `wic041` are
unassigned. `wic042` and `wic043` are notes, which report without failing the
compile, and errors under `--inference_strict`.

Every member is provoked in `tests/core/provocations.py`, in one of two tiers:
`PARSE` (source text through `parse()` alone) or `COMPILED` (a callable that drives
the compiler or its helpers). `test_every_code_has_a_registered_provocation` in
`tests/core/test_lang_parser.py` fails for a member with no provocation, so a new
code lands with its provocation in the same commit. The
[guide](../language_guide.md#9-error-codes-you-will-meet) says what each `wic0NN`
means to a user.

| Code | Member | Provoked in |
|---|---|---|
| `wic001` | `INVALID_YAML` | `PARSE` |
| `wic002` | `NOT_A_MAPPING` | `PARSE` |
| `wic003` | `EXPECTED_MAPPING` | `PARSE` |
| `wic004` | `EXPECTED_SEQUENCE` | `PARSE` |
| `wic005` | `EXPECTED_SCALAR` | `PARSE` |
| `wic006` | `MISSING_STEP_ID` | `PARSE` |
| `wic007` | `EMPTY_STEP_ID` | `PARSE` |
| `wic008` | `MALFORMED_WIC_STEP_KEY` | `PARSE` |
| `wic009` | `UNKNOWN_TAG` | `PARSE` |
| `wic010` | `DUPLICATE_KEY` | `PARSE` |
| `wic011` | `UNRESOLVED_INPUT` | `COMPILED` |
| `wic012` | `MISSING_REQUIRED_INPUT` | `COMPILED` |
| `wic013` | `SUBWORKFLOW_INVALID` | `COMPILED` |
| `wic014` | `SCRIPT_ARGUMENT_MISMATCH` | `COMPILED` |
| `wic015` | `CONTAINER_ENGINE_UNAVAILABLE` | `COMPILED` |
| `wic016` | `MISSING_INPUT_FILE` | `COMPILED` |
| `wic017` | `UNKNOWN_LANG_VERSION` | `COMPILED` |
| `wic018` | `LANG_VERSION_CONFLICT` | `COMPILED` |
| `wic019` | `MISPLACED_EDGE_DEF` | `PARSE` |
| `wic020` | `LITERAL_TYPE_MISMATCH` | `COMPILED` |
| `wic022` | `FIXED_POINT_NOT_REACHED` | `COMPILED` |
| `wic023` | `INCOMPATIBLE_INPUT_REFERENCE` | `COMPILED` |
| `wic024` | `RESERVED_KEY` | `PARSE` |
| `wic025` | `UNDEFINED_EDGE` | `COMPILED` |
| `wic026` | `DUPLICATE_EDGE_DEF` | `COMPILED` |
| `wic027` | `EMPTY_NAME` | `COMPILED` |
| `wic028` | `UNDECLARED_PORT` | `COMPILED` |
| `wic030` | `RECURSIVE_ALIAS` | `PARSE` |
| `wic031` | `DUPLICATE_DOCUMENT_NAME` | `COMPILED` |
| `wic032` | `UNKNOWN_SCATTER_PORT` | `COMPILED` |
| `wic033` | `UNKNOWN_WIC_KEY` | `PARSE` |
| `wic034` | `MALFORMED_WIC_VALUE` | `PARSE` |
| `wic035` | `UNSUPPORTED_CWL_VERSION` | `PARSE` |
| `wic036` | `UNTYPED_OUTPUT` | `COMPILED` |
| `wic038` | `STEP_INPUT_RECORD` | `PARSE` |
| `wic039` | `POSITIONAL_OUTPUT_SOURCE` | `COMPILED` |
| `wic042` | `INFERENCE_TIE` | `COMPILED` |
| `wic043` | `INFERENCE_RECENCY` | `COMPILED` |
| `wic044` | `REALTIME_DECLARATION` | `COMPILED` |
| `api001` | `INVALID_INPUT_VALUE` | `COMPILED` |
| `api002` | `INVALID_STEP` | `COMPILED` |
| `api003` | `INVALID_LINK` | `COMPILED` |
| `api004` | `INVALID_TOOL` | `COMPILED` |
| `api005` | `WORKFLOW_RUN_FAILED` | `COMPILED` |
