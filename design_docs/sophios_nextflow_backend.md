# Sophios Nextflow Backend — Locked Design

**Status:** Approved architecture. Locked.
**Authority:** This document governs the complete Nextflow backend across all phases.
**Change control:** A commit that modifies this file must have a subject beginning with `design:`. Implementation, test, documentation, fix, refactor, and release commits must not modify it.

---

## 1. Governing rule

> A workflow is either fully lowered into a validated executable Nextflow model, or compilation fails with an actionable diagnostic.

Sophios never emits a partially supported workflow and never silently changes source semantics. A rendered TODO, retained metadata, plausible-looking script, successful preview, or hand-built intermediate representation is not evidence that a source workflow is executable.

This rule outranks feature breadth and schedule. When a source construct has no approved lowering and is not one the Sophios compile alone uses (§3), the compiler rejects it before artifact generation.

---

## 2. Product boundary

Sophios supports Nextflow DSL2 as a target alongside CWL. The backend has four responsibilities:

1. lower the supported subset of a compiled Sophios/CWL workflow into an executable Nextflow model;
2. render deterministic inspectable artifacts from that model;
3. read a deliberately supported DSL2 subset without silently discarding unknown content; and
4. expose the supported behavior through the existing Python, CLI, and later service boundaries.

CWL remains the canonical authored/compiled semantic substrate until a successor design explicitly changes that decision. The Nextflow backend consumes the core compiler's `CompilationResult`: an immutable `WorkflowGraph` paired with its `CompilationArtifact` tree. The adapter projects already-resolved leaf processes, edges, boundary mappings, and job bindings into the backend capability boundary; it does not repeat link resolution, inference, or subworkflow composition. This private seam may evolve without changing the public Python API or the backend's semantic contract.

The backend does not perform a second forward inference pass. Imported workflows may use normal Sophios compilation and inference after reconstruction; they do not call low-level inference routines directly.

### Non-goals

- General Groovy parsing or execution equivalence for arbitrary `.nf` files.
- Silent preservation of unsupported semantics in executable output.
- A Sophios-managed Nextflow runner in Phases 1 or 2.
- Nextflow-native graph inference, until a successor design. The backend does not perform a second forward inference pass.
- Backward compatibility for private, unversioned intermediate Python objects. Serialized schemas are versioned explicitly.
- User-written Nextflow configuration. The generated `nextflow.config` is the only configuration Sophios supports.
- User-written Groovy in a workflow compiled to Nextflow. `target="nextflow"` has no Groovy passthrough; the gate under which one could ever be admitted is stated in §6, Expressions.

---

## 3. Closed-world capability contract

Every source feature considered by the backend is classified as exactly one of:

| State | Meaning |
|---|---|
| **Lowered** | Has an approved lowering, executable-model representation, renderer implementation, and real-runtime evidence. |
| **Compile-time** | Sophios uses it while compiling; the Nextflow output carries nothing for it, and the generated pipeline's execution does not depend on it. |
| **Rejected** | Produces a structured diagnostic before executable-model construction, and no artifacts. |

Imported content is a separate matter (§4.2): what the reader does not recognize may be retained loss-aware for import or inspection as an opaque region, but cannot be promoted to executable form. That is a property of imported content, not a state a Sophios/CWL field can be in.

The backend's support is one table in code, `src/sophios/nf_support.py`. It gives every Sophios/CWL field the backend considers one of the three states, capability analysis takes the fields it consumes from it, and the user documentation is rendered from it. The table is the authority for which state a field is in; this document is the authority for what each state and each lowering means. Moving a field into **Lowered** or **Compile-time** is a `design:` revision here before it is a change to the table (§10).

Capability analysis is closed-world. Each semantic source field is consumed by a lowering, used by the Sophios compile alone, or reported as rejected. The compile-time fields are `format` on tool inputs and outputs and on workflow inputs and outputs, which Sophios uses for EDAM inference, and on a File value, which the parameters file does not carry; `streamable`; `doc`; `label`; `intent`; `NetworkAccess` whose `networkAccess` is the literal `true`, which asks for what a Nextflow task already has; and `cudaVersionMin` and `cudaComputeCapability` of `cwltool:CUDARequirement`, which the backend reports with a note and the generated pipeline does not check (§6, Resources and containers). Every other value of `NetworkAccess` is rejected. Unknown requirements, bindings, expressions, directives, operators, and executable syntax are never ignored.

Diagnostics identify the source path, the rejected capability, and the next supported action when one exists. Capability analysis aggregates independent findings so users do not have to fix unsupported constructs one at a time.

---

## 4. Architecture

### 4.1 Forward export

```text
Python Workflow or .wic
  -> existing Sophios/CWL compilation (once)
  -> private compiled-semantic adapter
  -> closed-world capability analysis
  -> semantic lowering
  -> validated ExecutableNextflowWorkflow
  -> deterministic renderer
  -> JSON IR, workflow.nf, nextflow.config, nextflow_params.json
```

Unsupported input exits through structured diagnostics before executable IR or artifacts exist.

### 4.2 Import

```text
.nf
  -> supported DSL2 reader
  -> structural NextflowDocument with explicit opaque regions
  -> optional capability analysis and promotion to executable form
  -> structural CWL/Sophios reconstruction where representable
```

The direct `.nf -> NextflowDocument -> .nf` path is loss-aware. Opaque syntax is preserved and reported. A generated source read beside its executable model is the one exception, and it is exact rather than loss-aware: when the model renders to the source byte for byte, the renderer's determinism accounts for every construct in it, so the model supplies the structural document and nothing is opaque. Without that model, the same source is read by the supported DSL2 reader alone, and whatever it does not recognize stays opaque. Export followed by import therefore returns every generated workflow exactly, whatever channel wiring its lowering renders. Structural conversion to CWL compares representable names, ports, resources, and topology; it does not claim executable equivalence for opaque Groovy.

### 4.3 Public boundary

The intended Python surface is:

```python
workflow.compile()                    # CompiledWorkflow
workflow.compile(target="cwl")        # CompiledWorkflow
workflow.compile(target="nextflow")   # ExecutableNextflowWorkflow
workflow.to_nextflow(outdir)           # four artifact paths
```

One compile operation and one clearly named artifact-writing operation are sufficient. Duplicate convenience methods are not part of the target design.

The intended CLI surface is:

```text
sophios --yaml workflow.wic --target nextflow
```

`--target nextflow` is sufficient by itself. CWL-specific run flags are rejected with structured diagnostics. Resolved nested workflows are projected recursively; executable semantics attached to a workflow-call boundary or child workflow must be explicitly supported or rejected before that structural boundary is removed.

Import is exposed from the concrete `sophios.api.python.nextflow` module when the reader/import phase lands. The removed generic API aggregator is not restored.

---

## 5. Intermediate representations

### 5.1 Structural `NextflowDocument`

The structural representation is designed for parsing, inspection, hydration, and loss-aware round trips. It may contain typed recognized elements plus explicit opaque regions. Opaque regions are never treated as executable merely because they survived parsing.

### 5.2 Executable `ExecutableNextflowWorkflow`

The executable representation contains only fully lowered Nextflow semantics. Its constructors are validated and its values are deeply immutable.

Conceptual components include:

- typed workflow parameters with explicit presence/default state;
- typed process input and output ports;
- typed command tokens that distinguish literals, input references, and any approved shell operators;
- typed output capture and glob templates;
- typed resource and container directives;
- typed connection variants for workflow input, process edge, and workflow output; and
- unique normalized process, port, parameter, and emit identifiers.

Nullable process endpoints, magic directive keys, raw CWL expressions, mutable collections inside frozen records, and unclassified executable fragments are not valid executable-model states.

Connection variants make boundary-to-boundary dangling edges unrepresentable. Keyed output collections and post-normalization validation make duplicate emits unrepresentable. A workflow parameter feeding multiple ports is valid only when every sink agrees on how the parameter's channel is built: its qualifier, its path kind, and whether it carries a list. The parameter's declared type (§5.3) fixes the channel the renderer builds for it; one definition computes the construction a sink needs, and the graph validator holds every sink to the declared type's, so the channel the renderer builds is the one the validator compared. A source of a step input's idioms (§6, Topology) takes the parameter's own value. A scatter adapter (§6, Inputs and channels) belongs to the scattered process and takes no part in the comparison: a scattered input carries its whole array, so one array parameter may be scattered at one step and consumed whole at another.

### 5.3 Serialization

Serialized IR declares a schema version and representation kind. Hydration validates all invariants; it never relies on best-effort dictionary loading.

The schema is not stable until it meets users, so it promises no compatibility yet. Any change to the serialized form bumps the schema version, serialization always writes the current version, and hydration accepts the current version only: a payload declaring any other version is refused with one diagnostic naming both, and its artifacts are regenerated rather than migrated. A kind, field or value the current model does not have is refused like any other unknown content. A source generated from a refused model is read by the supported DSL2 reader alone (§4.2).

The model records each workflow parameter's declared type, which the entry check (§6, Inputs and channels) reads.

---

## 6. Semantic lowering contracts

### Expressions

**Expression handling admits two closed classes and nothing else.** A CWL expression is representable in the executable model only when it is a *static projection* or a member of the *safe JavaScript subset* below. A static projection is a finite path into a known data structure, decided entirely at compile time from the CWL source text, where every step has a provably identical Nextflow counterpart; it involves no computation. No expression text of either class reaches the executable model or the generated pipeline. Any other form — string manipulation, and any arithmetic, comparison, conditional, operator, or function call outside the subset — is not representable and fails closed. A form whose Nextflow counterpart is *close but not exact* is not representable either: the divergence is never repaired by compensating logic, because that logic would itself be unapproved computation.

The approved projections are `$(inputs.<name>)`, its spelling `$(inputs.<name>.path)` on a File or Directory input, which lowers to the same reference and is admitted on no other input, and `$(inputs.<name>.basename)` in template positions (§6, Commands; §6, Outputs and globs), and the two `outputBinding` capture declarations in §6, Outputs and globs. Each is one closed literal shape recognized by exact match against frozen data in the compiler, never by a grammar that could be widened a case at a time, and widening the set is a `design:` revision. Recognizing a shape is not the same as understanding a language: the compiler matches admitted projection text and rejects everything else, and outside the safe subset's parser no code path inspects, transforms, or composes expression text.

A lowering's own fixed rendered idiom is not the computation this rule forbids. The Groovy conditional a boolean flag renders to, the per-item join an array binding renders to, and the size and encoding preconditions the file-text capture renders to are each part of one mapping, chosen once at design time and identical for every workflow. What the rule forbids is deriving Groovy from CWL expression text by any route other than the safe subset's typed nodes, and repairing a mapping that does not already agree. A precondition that makes the generated pipeline fail exactly where CWL fails is the fatal-error half of a mapping, not a repair of one; a transform that massages a value into agreement *is* a repair, and disqualifies the projection outright.

**Arbitrary JavaScript is ruled out, not deferred.** This is deliberately a narrower claim than "no JavaScript". Sophios is built on CWL, whose only syntax for arithmetic and conditions is the JavaScript expression, and useful workflows depend on both; so the backend admits that syntax for the safe subset below, and for nothing else. Sophios does not evaluate JavaScript in any form: not at compile time, not at runtime, not by embedding a JavaScript engine, not by shelling out to `node`, and not by translating JavaScript outside the subset into Groovy. Nor does anything evaluate in the renderer, which §7 fixes as a pure transformation of the executable model. The reasons are structural rather than practical. Evaluation breaks §3's closed-world contract, because an evaluated expression's result space cannot be classified in advance. It breaks §5.2's and §7's prohibition on opaque strings, because the expression text would have to survive into the executable model in order to be evaluated later. It makes generated pipelines non-self-describing, because the emitted `.nf` would no longer say what it does. Shelling out to `node` adds a container runtime dependency, which §6, Resources and containers requires evidence for. And it is the same category §9 already refuses for arbitrary Groovy equivalence. The subset escapes each of these reasons by construction rather than by exemption: compile-time typing classifies its result space, typed nodes replace its text before the executable model exists, and its one fixed Groovy rendering says exactly what it does. This is recorded as a decision rather than a deferral so that it is not re-litigated as one: admitting arbitrary JavaScript requires a successor design document, in the same posture §9 takes toward remote Nextflow execution, not a `design:` revision to this file.

**User-written Groovy is ruled out today, and gated if it ever comes.** Three decisions, recorded here so they are not reopened piecemeal. First, `target="nextflow"` has no Groovy passthrough: a workflow carries no user-written Groovy in any position, and the backend admits none. Second, JavaScript and Groovy never coexist in one workflow. That is a statement about what the user writes — the CWL expression syntax of the safe subset on one hand, Groovy on the other — never about the Groovy the backend generates from the subset, which is compiler output and unaffected. Third, if Groovy passthrough ever comes to this backend, it comes only for workflows that use neither `when` nor `scatter`: those two constructs follow CWL semantics and need JavaScript, so a workflow that uses either has passthrough blocked up front rather than reconciled case by case. Admitting passthrough at all is a successor design, in the same posture as arbitrary JavaScript above; this paragraph fixes the shape it would have to take.

**The safe JavaScript subset (approved Phase 3 lowering).** Every expression the subset admits has exactly one meaning, identical under JavaScript — the CWL reference path, where `node` evaluates it — and under its fixed Groovy rendering. The subset is parsed at compile time against the closed grammar below into typed executable-model nodes; an expression the grammar or the typing rules do not admit is rejected with a diagnostic naming the construct.

- *Position.* A step's `when`, and nowhere else. Every other expression position keeps the projection rule, `valueFrom` on a command-line binding or `arguments` entry included, so a computed `valueFrom` there is rejected by name, and the executable model has no token for one (§5.3). `valueFrom` on a workflow step input is rejected by name in any form.
- *Form.* The whole field is one `$( … )` parameter reference. `${ … }` function bodies, and an expression interpolated into surrounding text, are rejected.
- *Literals.* Numbers; `true` and `false`; `null`; strings in double or single quotes containing printable ASCII and no backslash.
- *References.* `inputs.<name>`, where the input is a `boolean`, `int`, `float`, or `string`, optional or not. The name is a step input, which CWL leaves untyped; it carries the declared type, optionality included, of the tool input it binds, so a `when` is checked against the tool's declarations. No other property access; `self` and `runtime` are rejected.
- *Operators,* with JavaScript precedence and parentheses: unary `!`, `-`, `+`; `*`, `/`, `%`, `+`, `-`; `<`, `<=`, `>`, `>=`; `==`, `!=`, `===`, `!==`; `&&`, `||`. The conditional operator, `**`, bitwise and assignment operators are rejected.
- *Calls.* `Math.abs`, `Math.sqrt`, `Math.floor`, `Math.ceil`, `Math.round`, `Math.pow`, and `Math.min` and `Math.max` over two or more arguments. No other call is admitted.

Typing is checked at compile time and is what makes JavaScript and Groovy agree. An optional reference has type T-or-null. It appears only as an operand of `==` or `!=` against `null` (either spelling of equality); every other operator rejects it by name. JavaScript would coerce an absent value — `null + 1` is `1`, `!null` is `true` — and the `[]` sentinel would not, so the coercion is not representable. A guard such as `inputs.n != null && inputs.n > 3` is rejected with it: `&&` does not narrow, and admitting the guard means defining that narrowing. Arithmetic operators and `Math` calls take and give numbers; `+` never concatenates strings. Ordered comparisons take two numbers. Equality otherwise takes two operands of the same type — number, boolean, or string; with operands typed this way `==` and `===` coincide, so both spellings are accepted. `!`, `&&`, and `||` take and give booleans, so neither language's short-circuit value can be a non-boolean. There is no truthiness: a `when` must be boolean-typed.

Every number is an IEEE-754 binary64 value on both paths, as it is in JavaScript. `int` values are converted, so integers are exact only within ±2^53. `/` is always true division. Numbers are always finite: an evaluation in which any subexpression becomes NaN or ±Infinity — division by zero, `Math.sqrt` of a negative, overflow — fails with a diagnostic naming the step, the field, the authored expression, the subexpression that became non-finite, and the input values it read. This deliberately diverges from the CWL path, where JavaScript carries the value on: a comparison with NaN is quietly false there, so a condition quietly skips its step, and a quiet skip is far harder to debug than a named failure. `Math.pow` renders as `StrictMath.pow`: Java's `Math.pow` is only within 1 ulp and may be intrinsified, while `StrictMath.pow` is the fdlibm the CWL path's `node` uses. `Math.sqrt`, `Math.floor`, `Math.ceil`, and `Math.abs` are exact on both sides and render as `Math`. `Math.round` rounds half toward +∞ and is rendered by a fixed idiom exact over the whole binary64 range, since Java's `Math.round` saturates at the `long` range. Nothing else rounds, truncates, or wraps. The runtime that executes the rendering is the pinned one: Nextflow 26.04.2 on Temurin 17.

Two authorities must agree here — JavaScript and the Groovy rendering — so conformance is a differential property rather than a set of examples: generated in-subset expressions and input values are evaluated by `node` and by the rendered Groovy under the pinned Nextflow, in one batched run. Every expression whose JavaScript evaluation keeps each subexpression finite must agree; every other expression must fail with the non-finite diagnostic. Source-to-runtime cases additionally compare the generated Nextflow run with the CWL reference runner. Widening the subset — a new operator, call, type, or position — is a `design:` revision to this section.

Declaring `InlineJavascriptRequirement` is not itself the use of JavaScript, so a tool that declares it and then uses only approved forms stays legal — the same shape of decision as `ShellCommandRequirement` with no `shellQuote: false` binding, which changes nothing to render (§6, Commands). The requirement is a capability declaration; the violation is a non-admitted form. A later change must not "fix" this by rejecting the declaration.

### Commands

CWL command construction is normalized before rendering. Ordering follows the pinned CWL version, including defaults and tie-breaking. Command parts remain typed as literal data or interpolation references; quoting is decided per token or segment, never by scanning a completed string for `$`.

A `$` that is not followed by `(` or `{` in a literal `arguments` entry or `baseCommand` is plain text, as in CWL: it is a literal segment, and the shell-quoting helper passes it to the command as that character. A `shellQuote: false` literal, which no helper quotes, may not hold a `$`: the task's `bash -ue` would expand it in the task's environment, not CWL's `sh -c` in its own.

**Boolean flags (approved Phase 2 lowering).** A `boolean` input with an `inputBinding` and no `valueFrom` lowers to a typed conditional flag token that references the input by name and carries a non-empty prefix. At runtime, `true` renders the shell-quoted prefix as exactly one argv word and `false` renders nothing; a boolean binding without a prefix contributes nothing for either value. Flag tokens are valid only in command token position — never in globs or stream targets — and only against `val` ports. An optional boolean lowers the same way whether a value is present or absent: a flag token never dereferences its channel value, only tests it, so the absent-optional `val` lowering below covers this case directly and an absent optional boolean renders identically to `false`.

The rendered flag is a conditional over its channel value, so the lowering is sound only when that value is a JSON boolean at runtime. Capability analysis therefore requires every input consumed by a flag token to resolve to a boolean-typed source, whether that source is a workflow input or a producing process output port. Truthiness of a staged path or of a string such as `"false"` must never be allowed to decide a flag.

**`valueFrom` self-reference on a boolean binding (approved Phase 2 lowering).** CWL evaluates `valueFrom` and then applies boolean flag semantics to the result, so a lowering must reproduce that ordering. Without a JavaScript evaluator, the only `valueFrom` shape provably boolean at runtime is the supported template form's bare `$(inputs.<name>)` reference to the binding's own input — a redundant but legal restatement of the original value. That shape lowers to the identical flag token the no-`valueFrom` case produces, so it inherits the same runtime rendering and the same boolean-source requirement. Every other `valueFrom` shape on a boolean binding — literal text, a reference to a different input, a `.path`/`.basename` suffix, or any expression outside the supported template form — is rejected, because none of those are provably boolean without either a JavaScript evaluator or aliasing another channel's presence semantics onto this one.

**Diagnostics and security for this lowering.** A rejected flag construct names the source path and the rejected capability, and independent findings aggregate like every other capability diagnostic. An empty prefix is a source error and is reported; an absent prefix is valid CWL and contributes nothing. The prefix is data, never syntax: it is emitted through the generated shell-quoting helper, so it reaches the process as exactly one argv word and cannot introduce shell operators, redirections, or command substitution.

**Shell-mode raw literal tokens (approved Phase 2 lowering, one supported shape).** Nextflow's generated `script:` block already joins every command token into one shell-quoted line and runs it through a shell, so `ShellCommandRequirement` with no `shellQuote: false` binding present changes nothing to render: that is already byte-identical to CWL's own shell-mode quoting (every token individually quoted, joined, invoked via `/bin/sh -c`), runtime-proven against the pinned Nextflow with an adversarial argument. `shellQuote: false` is CWL's explicit request to emit one binding's text unquoted so it can carry real shell syntax — pipes, redirections, globs — instead of one safely-quoted argv word, and it is approved only for the one shape that keeps that opt-out provably safe: a binding (`inputBinding` or `arguments` entry) on a tool that declares `ShellCommandRequirement`, carrying no `prefix`, whose `valueFrom` is required (a binding with no `valueFrom` binds the input's own runtime value, never a CWL-author literal, so that shape is never eligible) and parses to a template made entirely of literal segments — no `$(inputs...)` reference of any kind, in any form. That text is the CWL author's own, fixed at compile time, so rendering it unquoted opens no path from workflow input data to shell syntax; the generated shell-quoting helper is bypassed for that one token only, and every other token keeps going through it exactly as before. For an `inputBinding`, the source value must also be present: CWL omits the entire binding when the optional input is null, while a static raw-literal token cannot conditionally disappear without a distinct lowering, so an absent optional source is rejected. `shellQuote: false` on a binding with no `valueFrom`, with a `prefix`, or whose `valueFrom` references any input — directly, or via a `.path`/`.basename` suffix — is rejected: unquoting a runtime-supplied value, even a staged file's own path, would let workflow input data or a chosen file name be interpreted as shell syntax, which is exactly the boundary this lowering must not cross. `shellQuote: false` declared without `ShellCommandRequirement` is also rejected outright rather than silently treated as inert, matching the closed-world contract. Every other `shellQuote: false` shape remains rejected, as does every `InitialWorkDirRequirement` listing shape beyond the entry lowering below, and every other unapproved expression form.

**Array command-line binding (approved Phase 2 lowering, one supported shape).** An array-typed `inputBinding` without `itemSeparator` lowers to a typed conditional binding, runtime-proven against the pinned Nextflow: when the array is empty it contributes nothing at all — not even its own prefix, matching CWL — and otherwise it contributes its optional prefix once followed by each element as its own shell-quoted argv word, in array order. `itemSeparator` (joining every element into one argv word) and `separate: false` on an array binding are rejected: both need a materially different render shape from the per-item form above, so picking one supported shape here means deferring the other rather than half-supporting both. `valueFrom` on an array-typed binding is also rejected, for the same JS-evaluator reason `valueFrom` is restricted elsewhere in this document.

### Inputs and channels

File and Directory inputs lower to path semantics. The JSON scalar types `string`, `int`, `float`, and `boolean` lower to value semantics; `long` and `double` are rejected with a diagnostic naming the type, since the safe subset's number rules (§6, Expressions) do not cover them. Defaults are explicit model values. Missingness is distinct from JSON `null`.

**Workflow input values are checked at entry.** The generated pipeline checks every workflow input value against its declared type when it starts, before any process runs, as cwltool rejects an invalid job value before it runs anything. The check goes by the value's runtime type, because the pinned Nextflow's v2 script parser, its default, gives a command-line value and a params-file JSON string the same type, a string, and keeps nothing that says where a value came from. The v1 parser (`NXF_SYNTAX_PARSER=v1`) converts a command-line value itself, `--name 010` arriving as the integer 10, which the check could not tell from a number, so the entry workflow's first statement stops the run under it, before any process runs, saying the pipeline needs the v2 parser. A value that already has the declared type passes, a number for a `float` only when it is finite. A string given for a `boolean`, `int`, or `float` converts only when its text is exact: `true` or `false` for a `boolean`, integral text within the `int` range for an `int`, and finite float text for a `float`. A `File` or `Directory` value is path text, or a mapping with a `path` (a CWL File or Directory object, which only the params file can give); a mapping that has a `class` passes only when it is the declared type. The path text passes when it is not empty and Nextflow's `file(…, glob: false)` names an entry of the declared kind there: `isFile()` for a `File`, `isDirectory()` for a `Directory`. Array and record values come only from the params file. `[]` stands for an absent optional value and passes for an optional `boolean`, `int`, `float`, or `string`; for an optional `File` or `Directory` it stops the run, since no path parameter has a lowering of it (absent-optional inputs below), so compilation rejects an absent optional `File` or `Directory` workflow input, whether a step reads it or not, naming the input. Any other value stops the run with a diagnostic naming the parameter. The check is the fatal-error half of the mapping, not a repair of it: it converts nothing that is not exact, so a flag, a `when`, or a value port never sees a value of the wrong type.

**Absent-optional `val` inputs (approved Phase 2 lowering).** An absent optional value lowers to a reserved empty-array sentinel `[]` carried by a `Channel.value(...)` element — one element, never zero — so the channel always terminates and a receiving process can observe the exact value it received. Runtime proof ruled out the naive representation: `Channel.value(null)` never binds under the pinned Nextflow runtime, so the process that consumes it never starts and the run hangs indefinitely — a literal JSON `null` is not a terminating channel value here, whatever CWL's own null means. `[]` is Groovy-falsy like `null` and `false`, so a flag ternary treats it identically, and it is unambiguous within this lowering's scope because this lowering never applies to an array-typed port: no scalar (`string`/`int`/`float`/`boolean`) value is ever legitimately an array, so this sentinel can never collide with a genuinely empty array under the array lowering below. This is sound only where nothing dereferences the value unconditionally: interpolating the sentinel into a plain template segment (`.toString()` on a Groovy list) renders `"[]"`, not a clean absence signal. The lowering is therefore scoped to `val`-qualifier inputs whose consuming tool port is either never referenced in that tool's command tokens, stream targets, or output globs, or referenced solely as the boolean-flag token it drives — both positions already treat their value as a condition rather than dereferencing it, so the sentinel renders safely as falsy. Every other reference position — a plain value interpolation anywhere in a command token, stream target, or glob — keeps rejecting an absent optional source; the one presence-gated value binding approved is an optional input's own binding, below. Absent-optional `path` (File/Directory) inputs also keep rejecting: `path`-qualifier channel construction always stages its value unconditionally, so a safe representation needs its own runtime-proven convention, and none is approved. An absent-optional array-typed input is likewise out of scope for this lowering.

**Optional value bindings (approved lowering).** An optional `string`, `int`, or `float` input's own `inputBinding` with no `valueFrom` lowers to a typed optional binding token that names the input and carries the binding's prefix and `separate`. The `[]` sentinel contributes nothing, neither prefix nor value, as CWL adds nothing for a null input; any other value contributes what the same binding contributes for a required input. Every such binding lowers this way, whatever value the compiled workflow gives the input, because the entry check passes `[]` for any optional scalar when the pipeline starts. The token compares its value with the sentinel and never interpolates the sentinel, so, like a flag, it is a position the absent-optional lowering above admits; any other reference to the input still makes an absent value rejected. Runtime proof against the pinned Nextflow shows an absent value and a present one each rendering as cwltool renders them.

Channel construction considers all consumers. Connection order never selects a qualifier or staging policy.

**Array-typed inputs (approved Phase 2 lowering).** A CWL array input — `{type: array, items: <scalar>}`, where `<scalar>` is `File`, `Directory`, `string`, `int`, `float`, or `boolean` — lowers to a port carrying the same qualifier its item type would carry alone (`path` for File/Directory items, `val` for scalar items), plus an explicit array marker. Whether a sink carries a list is part of the one-parameter rule (§5.2), so a parameter that one sink takes as one scalar and another as a list is rejected. Cardinality is otherwise a channel-construction concern, not a new qualifier, and both forms are runtime-proven: a `val` array is a JSON list flowing through `Channel.value(...)` exactly as any other JSON value; a `path` array is a single `Channel.value(...)` element holding a Groovy list built with `file(...)` per element, so Nextflow stages every element for one process invocation rather than fanning the channel out over several. Nextflow hands a `path` input holding exactly one file to the script as a bare `Path`, which has no list operations, so every array-typed `path` port declares `path <name>, arity: '0..*'`, whether a workflow input feeds it, it sits outside a scattered tuple, a gather fills it, or a step input joins it: runtime proof against the pinned Nextflow shows that range delivers zero, one, and many files as a list, which is what the array binding's emptiness test and per-item join read. Every such port also stages each file in its own numbered directory, as cwltool does, so same-named files from different directories never collide; Nextflow otherwise stops the task with an input file name collision. An array-typed input is referenced only by an array binding, which reads the staged paths, so the directory changes nothing else. An empty array is a present, valid, zero-length value — distinct from the absent-optional lowering above, which represents no value at all and is out of scope for array-typed ports entirely; this lowering never emits or accepts the absent-optional sentinel for an array port, so the two representations cannot collide.

The shorthand string form (`"File[]"`), nested arrays, and a per-item `inputBinding` on the array's `items` schema are all rejected; only the flat mapping form with a bare scalar item type is representable. Array-typed outputs are also rejected: only array-typed inputs have an approved lowering.

**`InitialWorkDirRequirement` entries (approved lowering, one supported shape).** A `listing` entry names one File or Directory input: the bare shorthand expression `$(inputs.<name>)`, or a `Dirent` whose `entry` is that same bare reference and whose `entryname` is absent, the self-referencing `$(inputs.<name>.basename)`, or a literal string with no expression and no path separator. The entry's CWL name is that literal, or the input's own basename otherwise, and `writable`, when present, is a literal boolean. Every entry stages its input under a hidden name the backend reserves, and the task makes the entry under its CWL name before the command runs: a copy when `writable: true`, so the command may change the entry without touching the staged input, and a symlink otherwise. An entry under the input's own basename that is not writable is made this way too, not left to Nextflow's own staging: Nextflow leaves a task's staged inputs out of a pattern output's matches, while a CWL glob matches the entry as it matches any file in the output directory. Such a port carries a typed entry marker, the CWL name when it is a literal and whether the entry is a copy, and the renderer emits the hidden staging name and the command that makes the entry. The marker replaces the `stage_as` rename. The port's own value is the hidden staged file, not the entry. A reference to an input whose entry keeps the input's own basename — plain, `.path` or basename, in a command token, stream target or output glob — names the entry, as cwltool maps the input's path to its entry; that is the shape of a tool that writes into a writable output directory and captures it with `glob: $(inputs.<name>.basename)`. Such an output directory is a `Directory` input like any other, so it must exist when the pipeline starts; Sophios's CWL path makes one given as a bare name, and the generated pipeline does not. An input whose entry has a literal name may not be referenced anywhere in that tool's command tokens, stream targets, or output globs: the supported pattern is for the command to address the entry by the name the CWL author already chose, hard-coded directly into `baseCommand`/`arguments`. Each claim — the copy is independent of the staged input, the symlink resolves to it, a pattern glob matches the entry, and the entry exists before the command runs — is admitted only with runtime proof against the pinned Nextflow.

Every other listing shape is rejected with a named diagnostic: an `entry` that is not a bare reference to one File/Directory input (inline content construction), an `entryname` that is any other expression (a computed rename), a listing entry naming a `val`-qualifier or array-typed input, a literal name that begins with the reserved hidden prefix or is `.` or `..`, and a listing entry that is neither a string nor a `Dirent` mapping. Each input may occur in at most one listing entry, because one port has one staging; repeating an input, whether as two renames or as an entry under its own basename plus a rename, is rejected instead of silently selecting the last declaration. Two entries with one CWL name within one process are also rejected, as an unresolvable staging collision. A name known only when the task runs can still collide, with another entry's or a staged input's basename, and `cp` and `ln` would then make the entry inside the directory that holds the name; the task stops with an error naming the entry instead, as cwltool refuses the job.

**Required channel adapters (approved Phase 2 lowering; indexed in Phase 3).** Scatter is the only approved topology whose two ends disagree about channel cardinality: a scattered process consumes one element per task where an unscattered one consumes a whole value. Lowering represents that with a typed adapter on the scattered input's connection, drawn from a closed set in the executable model. Every scatter, single-input included, is indexed: the scattered inputs' whole-array value channels are combined, each boxed in a one-element list so `combine` carries the array rather than splicing its elements, and one `flatMap` turns the whole arrays into a queue of `[index, elements…]` invocations — by index under `dotproduct`, the adapter a single-input scatter receives, and as every combination under the crossproducts. The process takes the index as a hidden `val` beside its inputs and returns it with its outputs, and a `gather` adapter on the process edge out of the scatter sorts by it (`toSortedList { it[0] }`) and strips it, so a gathered array is in invocation order whatever order the tasks finish in. A process has one adapter: every scattered input of one process carries the same one. The adapter is a property of the scattered process, not of the workflow parameter, so it leaves the parameter's channel-construction comparison (§5.2), and connection order never selects channel behavior.

Phase 3 widens the closed set, and only by the fixed whole-array idioms of §6, Topology: `dotproduct`, `flat_crossproduct` and `nested_crossproduct` on a scattered input's connection, whether its whole array comes from a workflow input, from a gathered output, or from an unscattered step's output, and `gather` on a process edge out of an indexed scatter. Pairing or crossing two queue channels and every other operator remain rejected with a named diagnostic rather than approximated. The approved set is closed data in the executable model, so the renderer maps an adapter to its idiom and can neither widen nor infer one.

### Outputs and globs

Every output has a concrete capture mechanism. Phase-specific support may include path globs and declared stdout/stderr file capture. Primitive outputs are not represented as bare variable names. A rendered glob remains a glob at run time: a name assembled from runtime data is still pattern-interpreted, so a staged name carrying a glob metacharacter does not match the file the process wrote. Rendering an assembled name literally is a distinct lowering, because it changes emitted bytes and the generated-subset reader with them.

Supported glob expressions are parsed into typed literal and input-reference components. Raw CWL expression strings cannot enter the executable model.

Beyond the glob, `outputBinding` capture behavior falls into three statuses this section keeps distinct, because they are not the same kind of "no":

- **Approved.** The two `outputEval` capture declarations below, each one exact literal text under a literal-glob restriction.
- **Ruled out on the record.** Evaluating any other `outputEval` text, which would require the arbitrary JavaScript evaluation §6, Expressions permanently refuses; `outputEval` is not a safe-subset position. These are not awaiting a lowering.
- **Rejected with a known relaxation path.** An approved capture declaration paired with a wildcard or input-reference glob, whose gate is stated below as a specific proof obligation.

`secondaryFiles`, record-typed outputs, array-typed outputs, and expression forms in a workflow output's `outputSource` remain rejected with no approved lowering.

**Basename references (approved Phase 2 lowering).** `$(inputs.<name>.basename)` lowers to a typed basename segment valid in every template position: command tokens, stream targets, and output globs. The referenced input must be a path port, because the lowering relies on Nextflow staging an input under its original file name, which makes the staged path's name property exactly the CWL `basename`; an input staged under the hidden name of an `InitialWorkDirRequirement` entry renders the entry's name instead (§6, Inputs and channels). Basename segments against value ports are unrepresentable in the executable model, and the same requirement is reported by path during capability analysis, so a source document naming a value input reports every offending position at once rather than failing later on a normalized identifier. In output-glob position the reference must derive a new name. A glob that is one reference to a File or Directory input, plain or basename, names the staged input, which the task directory holds under that name and a CWL glob does not match, so it is rejected; the exception is an input made an `InitialWorkDirRequirement` entry under its own basename, whose glob names the entry the task made.

**Optional outputs (approved lowering).** A `File?` or `Directory?` output is `null` when its glob matches nothing, as in CWL, and the workflow output it reaches reports that `null` as the `[]` sentinel (§6, Topology). A literal glob with no `$(self[0])` names at most one path, so its port is `optional: true` and emits nothing when nothing matches. A pattern glob, or a literal one under `$(self[0])`, must match at most one file, and CWL fails the step when a pattern matches several; the pinned Nextflow refuses an arity of `0..1`, and fails `arity: '1'` on no match even beside `optional: true`. Such a port therefore declares `arity: '0..*'`, and its workflow output takes the one file, gives `[]` for none, and stops the run naming the output when there are more. That check runs at the workflow output, so the port must reach one. An optional output reaches workflow outputs only, and a scattered process has none, since a gather would drop a null's position. Runtime proof against the pinned Nextflow shows zero, one, and two matches each ending as CWL ends.

**Output cardinality declaration (approved Phase 2 lowering, one supported literal).** An `outputBinding.outputEval` whose text is exactly `$(self[0])` — surrounding whitespace trimmed, no tolerance for internal variation — is recognized as a *cardinality declaration*, not as an expression: it states that the port carries one value rather than a list. It lowers to a closed capture marker on the output port, mirroring the array marker on input ports, and no `outputEval` token of any kind enters the executable model. On a required output, the marker renders as Nextflow's `glob: false, arity: '1'` options on the `path` output (an optional one declares `arity: '0..*'`; see Optional outputs above): the first makes the approved CWL literal remain literal even when it contains a Nextflow-only pattern character such as `{`, `}`, or `\`, and the second is runtime-proven against the pinned Nextflow to emit a single path value rather than a list and to fail the task when nothing matches. The generated-subset reader consumes `glob: false` as a derived rendering detail and reconstructs the same closed capture marker.

The declaration is admitted only when the paired `glob` is one literal containing no wildcard character — no `*`, `?`, or `[` — and no input reference. `self[0]` means "the first of the matched list", so projecting it is provably identical only where the match set has exactly one member by construction, and a literal glob names at most one filesystem entry. A wildcard glob would instead make the two runtimes' glob match *ordering* load-bearing, and that ordering is unproven. An input-reference glob is rejected for the same reason one step removed: its runtime value could itself contain a wildcard, so wildcard-freedom is not decidable from the source text. The restriction lives in the executable model rather than only in capability analysis — a capture marker beside a non-literal or wildcard-bearing glob is an unrepresentable state, so no later change can reach it by a different code path.

Zero-match and multi-match are pinned under that restriction. Multi-match cannot arise: a literal glob names one path. Zero-match fails the run in both runtimes — CWL cannot satisfy a required `File` output from an empty match set, and the generated `arity: '1'` output fails the Nextflow task with a missing-output error rather than emitting an empty channel.

**Relaxation path for wildcard globs.** Admitting `$(self[0])` against a wildcard glob is a gated extension with known requirements, not a dead end. A future contributor must demonstrate that CWL's glob match ordering and Nextflow's are identical, covering at least the cases where they could plausibly diverge: filenames of differing length, mixed upper and lower case, numeric suffixes where lexical and numeric order disagree (`part2` against `part10`), and the locale sensitivity of the collation each runtime's underlying glob implementation uses. Absent that proof the two runtimes may silently select *different files* for the same workflow with no error raised anywhere, which is exactly the failure mode §1 forbids; the restriction therefore stands until the ordering is proven rather than assumed. The same proof would unlock the input-reference glob, whose only defect is that it cannot be shown wildcard-free at compile time.

**Rejected `self` projections.** Every `outputEval` text outside the two admitted literals is rejected, and the shapes a CWL author is most likely to reach for are rejected by name rather than by a generic miss: `.dirname`, `.path`, `.location`, `.checksum`, `.secondaryFiles`, and `.format`. Most of these simply have no lowering. `.dirname` and `.path` are a different case and are worth stating explicitly, because they are permanently unattractive rather than merely unimplemented: under this backend's staging model a produced file's directory *is* the Nextflow task work directory — an opaque hashed path Nextflow owns, is free to relocate, and may clean up — and it is never the directory the CWL author was describing. A mapping onto `task.workDir` would therefore be technically straightforward and semantically a lie: it would type-check, run, and hand a downstream consumer a path whose meaning did not survive the translation. That is silently changing source semantics, which §1 forbids, so these two stay rejected precisely because they are easy.

**File-text capture (approved Phase 2 lowering, one supported literal).** `loadContents: true` paired with an `outputEval` of exactly `$(self[0].contents)`, on an output declared `type: string`, under the same literal-glob restriction as the cardinality declaration, lowers to a closed file-text-capture marker: the port becomes a value port whose value is the named file's decoded text. Both halves are required and neither is admitted alone — `loadContents` without that `outputEval` is rejected, that `outputEval` without `loadContents: true` is rejected, and the pairing on any output type other than `string` is rejected.

`type: int` and `type: float` are rejected specifically, and the diagnostic says why: parsing text into a number is computation outside the safe subset, and this is exactly where §6, Expressions draws its line. Capturing bytes is a projection; interpreting them is not.

The semantics are the pinned CWL version's, and both halves of them had to be established by runtime proof because the obvious Nextflow idiom is wrong twice over.

- **Whole file, with a hard size limit — not truncation.** CWL v1.2, the version the compiler pins, requires a `loadContents` file to be a UTF-8 text file of 65536 bytes or fewer and requires the implementation to read it *entirely*, raising a fatal error above that size. CWL v1.0's "read up to the first 64 KiB" was replaced by that hard limit, so under the pinned version there is no truncation point to reproduce and no partially-decoded multibyte sequence to define. The reference runner agrees: it reads one byte past the limit and fails rather than truncating.
- **Trailing newline preserved verbatim.** A trailing newline is part of the contents under CWL, while shell- and `stdout`-style capture strips it. Runtime proof against the pinned Nextflow confirms the trap concretely: its `eval` output type, which is the natural reading of "capture the file's text", strips the trailing newline, so it is not an admissible counterpart however convenient it looks. Compensating for that by re-appending a newline is exactly the repair §6, Expressions forbids.

The lowering therefore renders as a `val` output over a generated helper that reads the file from the task work directory, refuses a file larger than 65536 bytes, and decodes UTF-8 strictly. Runtime proof pins each claim separately: a trailing newline preserved byte-for-byte, a 65536-byte file accepted, a 65537-byte file failing the task, multibyte text decoded to the same characters, and malformed UTF-8 failing the task rather than being silently replaced with U+FFFD as a lenient decoder would. The size and encoding preconditions are the fatal-error half of the CWL contract rather than a repair of a mismatched mapping. The helper is emitted only into pipelines that actually use this capture, so it adds nothing to the rendering of a model without one (§5.3).

**The sink of a captured value.** A file-text-capture output's only approved sink is a workflow output. A process-to-process edge out of one is rejected with a named diagnostic, for a reason that is about the model rather than about this lowering: apart from this lowering every process output carries the `path` qualifier, so a process edge could not disagree with its destination port about channel semantics, and the executable graph has no source-to-destination qualifier agreement check to lean on. Relaxing this means adding that check — the process-edge analogue of the workflow parameter consistency rule in §5.2 — and proving a captured value feeding a downstream `val` port at runtime. That is a self-contained relaxation, not a redesign, and without it the restriction stands. The restriction is enforced in the executable model as well as in capability analysis, so no other code path can reach the combination.

**Cardinality and scatter.** The capture marker is per task and scatter's cardinality is per collection, so the two compose without overlap. `arity: '1'` says each task of a process produces one matching file; the single-input scatter lowering (§6, Topology) says how many tasks run. A scattered step over N elements therefore runs N tasks, each producing one file, and its output queue channel carries N single-file elements — exactly the array-typed workflow output CWL declares for a scattered step whose tool output is a single `File`. The marker needs no scatter-specific case, and the scatter contract's rules for a scattered step's outputs (§6, Topology) apply unchanged. This is the model's first output-cardinality concept, and it deliberately does not attempt to restate the collection cardinality scatter already owns.

### Topology

The executable graph validates endpoint existence, direction, multiplicity, acyclicity where required, unique emits, normalized-name collisions, and workflow-boundary consistency before rendering.

**Single-input executable scatter (approved Phase 2 lowering, one supported shape).** A step whose `scatter` names exactly one input — the string form, or a one-element list — lowers to that input's port receiving one element of the source array per task through the `dotproduct` adapter above, so the process runs once per element while its remaining inputs stay value channels and broadcast to every task. Multi-input scatter is rejected, and with it every case where `scatterMethod` is load-bearing: `dotproduct`, `flat_crossproduct`, and `nested_crossproduct` all describe how two or more scattered arrays combine, which is the genuinely hard cardinality decision Phase 2 defers rather than guesses; the multi-input scatter lowering below decides it for Phase 3. At exactly one scattered input all three coincide — one task per element, output nesting depth one — so a `scatterMethod` carrying one of those three values alongside a single-input `scatter` is consumed as an inert restatement instead of rejected; the compiler emits `scatterMethod: dotproduct` unconditionally, so rejecting the field outright would put executable scatter out of reach of the public Python and CLI surfaces and out of reach of the real-fixture runtime proof this document requires. Any other `scatterMethod` value is rejected by name.

The scattered input's source must be exactly one array-typed workflow input whose item type carries the same qualifier and path kind as the scattered port itself. That is the Phase 2 shape. Phase 3 also admits the gathered output of an upstream scatter as that source, under the gather lowering below, and the output of an unscattered step, taken as one value under the collection cardinality rule below; a wildcard-glob array output still cannot carry an array, so scattering over an array-marked port stays rejected for the same reason nested arrays are.

**Collection cardinality.** A scattered step's output port carries one value per task, not one value overall. The gather lowering below delivers those values as the one array CWL gives a workflow output or a downstream input, in invocation order. An output of an unscattered step reaches a scattered step as one value (Nextflow's `.first()`), both as an input broadcast to every task and as the array the step scatters over. An unscattered process emits exactly one element per output, so taking the first element is exact. Nextflow emits a value channel from a process whose inputs are all value channels, which is the common case because every workflow input is one; there `.first()` changes nothing, and Nextflow warns that it is useless. A process emits a queue channel when one of its inputs is a queue, such as a conditional step's output; without `.first()`, that one-element queue paired with the scatter's queue would silently truncate the run to one task instead of N. The backend applies `.first()` to every unscattered source rather than tracking which channels are queues, since a wrong guess truncates the run and the warning costs nothing. The second use needs an unscattered step with an array-typed output, and array-typed tool outputs stay rejected (§6, Inputs and channels), so no unscattered step produces an array a step can scatter over until a lowering for array-typed outputs is approved.

**Empty collections.** Scattering over an empty array runs zero tasks. Runtime proof against the pinned Nextflow shows the derived queue channel terminates immediately, unscattered processes in the same workflow still run, the workflow output channel is simply empty, and the run exits successfully — no hang, unlike the `Channel.value(null)` representation ruled out above. An empty array is a present value here exactly as it is under the array-input lowering, and an absent-optional array-typed input stays rejected, so the absent-optional sentinel can never be mistaken for a zero-length scatter.

**Nested workflows (approved Phase 2 lowering).** A step whose compiled `run` is itself a `Workflow` lowers by inlining, at any depth. The core compiler does the inlining, not the backend (§2): its compiled graph already holds the flattened leaf steps, with each declared subworkflow input replaced by whatever the outer step binds it to, every reference to the outer step's outputs resolved to the inner endpoint that subworkflow output's `outputSource` names, and each leaf's compiled step id. The backend takes those leaves and ids as they are. Its own work is the capability checks below, which see every nested step and its workflow-call boundary before the boundary is projected away (§4.3). Inlining is chosen over emitting a nested DSL2 `workflow` block: every executable connection variant addresses a process endpoint, so a subworkflow call would introduce a new endpoint kind across the graph validator, the renderer, and the generated-subset reader at once, while inlining reaches the same observable execution — the same tasks, the same data flow, the same output files — with no new endpoint kind at all. Inlining is never a source-language decision in the renderer, and it is not silent: the emitted artifacts are flat by construction, so the composition structure lives in the source workflow and in the process names, not in the generated DSL2.

Composition findings are resolved before the rest of capability analysis, because the flat graph every other pass analyzes cannot be built while its composition is unsupported. Composition findings still aggregate among themselves across every nested step.

**Nested namespacing.** Each inlined step is named by its compiled step id, which the core compiler makes: a step's id as written, less a `.wic` suffix, with `@<position>` added when another step in its workflow, or one of that workflow's inputs or outputs, has the same id; a nested step's id is joined to those of the steps that call it with `~`, outermost first and at any depth. The backend takes that id as it is and normalizes it. Two instantiations of one subworkflow therefore cannot collide at any depth, and the normalized-name collision check applies to the namespaced names rather than to the inner ones, naming both colliding ids as written.

**Subworkflow I/O.** Every declared subworkflow input must be bound by the outer step's `in`, and every name in the outer step's `out` must be a declared subworkflow output; an unbound input or an undeclared output is rejected rather than defaulted. Nested input and output definitions are subject to the same closed-world per-port field checks as the outer workflow, so a field the outer workflow rejects, such as `secondaryFiles` or a workflow output's `linkMerge` and `pickValue`, cannot disappear during inlining. Each subworkflow output must carry exactly one `outputSource` resolving to one of that subworkflow's own step outputs: a subworkflow output that forwards one of its inputs is rejected, exactly as boundary passthrough is rejected at the outer boundary. An inner step's sources resolve either to another inner step's output or to a subworkflow input, which the outer binding replaces; anything else is rejected by name.

**Scatter and nesting.** The naming and binding rules above hold at every depth, because the core compiler flattens every level the same way. `scatter` on a subworkflow step is rejected: scattering an inlined sub-DAG is a fan-out over a set of processes, not the single-process shape the scatter lowering above approves. `scatter` on a step *inside* a subworkflow is not a separate lowering — after inlining such a step is indistinguishable from an outer scattered step, and the scatter contract above applies to it verbatim, with its sources resolved in the flattened workflow.

**Workflow-level requirements.** `ScatterFeatureRequirement`, `SubworkflowFeatureRequirement`, `MultipleInputFeatureRequirement`, and `InlineJavascriptRequirement` — which Sophios adds for every `when`, and whose declaration is not the use of JavaScript (§6, Expressions) — declare only that a document uses a feature whose lowering this phase decides per step, so each is consumed as an inert no-op carrying no fields beyond `class`. Every other workflow-level requirement is rejected by name, at the outer and the subworkflow level alike. Workflow-level `hints`, and a step's own `hints` and `requirements`, are rejected by name. On a workflow input, `secondaryFiles`, `inputBinding`, and `loadListing` are rejected by name.

**Step inputs.** A step input's `default`, `linkMerge`, and `pickValue` are lowered with no JavaScript, each by one fixed Groovy idiom over the step's bound values, chosen once and identical for every workflow:

- `default` is the value when the step input has no source, and takes the place of a source value that is `null` (the `[]` sentinel), as in CWL.
- `linkMerge` joins the sources in their declared order, one source included: `merge_nested` makes a list with one element per source, and `merge_flattened` concatenates them, an array source contributing its elements and any other source one element. Two or more sources with no `linkMerge` merge as `merge_nested`, the CWL default; one source with no `linkMerge` is passed unchanged.
- `pickValue` applies after `linkMerge`, over the first level of the list: `first_non_null` takes the first non-null element, `the_only_non_null` the one non-null element, and `all_non_null` the list without its nulls. `first_non_null` with no non-null element, and `the_only_non_null` with none or more than one, stop the run with a diagnostic naming the step input, where CWL fails; that is the fatal-error half of the mapping.

Every source of a step input that has two or more sources, a `linkMerge`, or a `pickValue` is one value — a workflow parameter, a gathered output, or an unscattered step's output — so the idiom combines values, never queue channels. A `pickValue`, or a `default` beside a source, over an array-typed source is rejected: the `[]` sentinel standing for a `null` array source is the same value as a genuinely empty array, so neither idiom can tell the two apart. The collision is in the source as a whole, not in its elements: no File or scalar item is ever `[]`. The possibly-null admission rule below applies to the value the idioms produce. A `pickValue` on one source with no `linkMerge` is rejected, since it has no list to pick from, and so is a `default` beside a `linkMerge` or a `pickValue`, which never applies because the joined value is not null. A source that may be null — a conditional step's output or an optional workflow input — joins a list only beside a `pickValue`, since a joined list has no lowering of a null element. A nested workflow step's input takes one source as it is: `default`, `linkMerge`, `pickValue`, and a list of sources there are rejected by name. Each file of a list a step input joins is staged in its own numbered directory, as every array-typed `path` port's files are (§6, Inputs and channels), so same-named files from several sources never collide. A `default` of a `File` or `Directory` input is rejected by name: CWL writes it as a File or Directory object, which the JSON-scalar default does not take. A step input's `label` is documentation and carries nothing into the Nextflow output (§3). `valueFrom`, `loadContents`, and `loadListing` on a step input are rejected by name. Each idiom is admitted only with runtime proof against the pinned Nextflow.

**Multi-input scatter (approved Phase 3 lowering).** A step may scatter two or more inputs under `dotproduct`, `flat_crossproduct`, or `nested_crossproduct`. Each scattered source arrives as one whole array — a workflow input, the gathered output of an upstream scatter, or an unscattered step's output taken as one value (collection cardinality above) — and the invocations are computed from the whole arrays by one fixed Groovy idiom per method, never by channel operators whose pairing depends on arrival:

- `dotproduct` pairs elements by index. Unequal lengths fail the run with a diagnostic naming each scattered input and its length; nothing is ever truncated.
- `flat_crossproduct` enumerates the Cartesian product by nested loops with the first declared input outermost, which is the CWL reference order.
- `nested_crossproduct` enumerates the same invocations and keeps, for each, its position in every scattered dimension.

Phase 3 lowers a single-input scatter through this same lowering, as one scattered input under `dotproduct`, where all three methods coincide. Every invocation of the Phase 3 lowering carries its index as a hidden leading value through the scattered process's inputs and outputs, and a gather sorts by it, so output order never depends on task completion order. An empty scattered array produces zero invocations. Unscattered inputs broadcast to every invocation as value channels. Whatever the number of scattered inputs, an unscattered input comes from a workflow parameter, from a gathered upstream output, or from an unscattered step's output taken as one value (collection cardinality above), so each broadcasts as one value and nothing pairs a queue with the scatter's queue.

**Gather.** A scattered step's output feeds a downstream array-typed input once, as one array in invocation order. A gathered `path` array is delivered to its consumer as a list however many files it holds, one included (§6, Inputs and channels). That gathered array is the only process-produced array admitted as a scatter source: array-typed tool outputs stay rejected (§6, Inputs and channels), and one from a wildcard glob would also need proof that CWL's and Nextflow's glob match orders agree. A `nested_crossproduct` result has one array dimension per scattered input and is admitted only as a workflow output; a downstream nested-array input is rejected by name, so the Phase 2 array marker keeps meaning exactly one dimension.

**Conditional steps (approved Phase 3 lowering).** A step's `when` is a boolean-typed safe-subset expression (§6, Expressions), evaluated once per invocation — once per combination under scatter — after the step's inputs are bound. A true condition runs the process. A false condition runs no task, and the step's outputs are `null`, as in CWL: never absent, because an absent value would let a downstream step run under CWL and silently not run under Nextflow.

`null` is carried by the reserved `[]` sentinel the absent-optional `val` lowering (§6, Inputs and channels) already uses, so the backend has exactly one representation of a CWL `null`. The rendering evaluates the predicate over each invocation's bound inputs and splits the invocations on it: the true branch feeds the process, the false branch emits the sentinel in place of each output, and the two are merged back into the output channel in invocation order. Every skipped invocation therefore emits exactly one element per output, so channels terminate and downstream completion cannot hang. Under scatter the gathered collection keeps the sentinel at each skipped position, which is CWL's array with `null` at that position. That array is not itself null, so the admission rule for a possibly-null value does not cover it. The gathered output of a conditional scattered step reaches a workflow output only, where each inner sentinel stands for `null` (nothing in the generated pipeline rewrites it, so the emitted array carries `[]` at each skipped position); that claim is admitted only with runtime proof against the pinned Nextflow. Every other consumer of it, re-scattering included, is rejected with a named diagnostic.

A value that may be `null` is admitted only where the sentinel is already sound:

- a workflow output, where the sentinel stands for `null`; the generated pipeline emits `[]` and does not rewrite it; or
- a `val` input port of a later step, declared optional, whose use in the consuming tool is one the absent-optional lowering admits — never referenced, or referenced only by a boolean-flag token or its own optional binding.

Every other consumer is rejected with a named diagnostic, including a consumer port not declared optional, a `path` consumer (the absent-optional `path` convention has no approved lowering), and an array-typed output of a conditional step outside scatter, where the sentinel would collide with a genuinely empty array. `when` on a step whose `run` is a workflow is rejected by name: skipping an inlined sub-DAG is not the single-process shape this lowering approves. Each claim — sentinel emission for a skipped invocation, order-preserving merge, clean termination, and the sentinel at a workflow output — is admitted only with runtime proof against the pinned Nextflow.

### Resources and containers

Only explicitly mapped directives are executable. Unsupported requirements and hints are diagnosed rather than ignored. Text rendering is not runtime proof; CPU, memory, environment, and container claims require the evidence appropriate to the phase.

**Host and container tasks (approved lowering).** A tool's `DockerRequirement` image lowers to its process's `container` directive, and a tool without one runs on the host, as under cwltool. One workflow may hold both kinds: the generated `nextflow.config` enables Docker when any process has a container, and Nextflow runs a process with no `container` directive on the host either way. Runtime proof against the pinned Nextflow shows both kinds of task running in one workflow, each where its tool asks.

**Image entrypoints (approved lowering).** CWL gives a containerized tool's command to the container as its arguments, so an image's `ENTRYPOINT`, when it has one, runs with the command after it; a tool with no `baseCommand` relies on that to name its program. A Nextflow task gives the container its script to run instead, which an entrypoint would take as its arguments. Every process with a container therefore clears the image's entrypoint (`containerOptions "--entrypoint ''"`) and runs its command after the entrypoint's words, read when the task runs: its `beforeScript`, which runs on the host where Nextflow starts the container, pulls the image when it is missing and writes the image's `ENTRYPOINT` words, each ended by a NUL, to a file in the task directory under the reserved hidden prefix, and the script reads them back and runs them first. An image without an entrypoint adds nothing. The words are those of the image the task runs, as in CWL, not a copy taken when compiling, so the compile needs no container engine. A tool with a `DockerRequirement` and a `ShellCommandRequirement` is rejected by name: CWL runs its command as `/bin/sh -c` after the entrypoint, which this lowering does not render. So is a containerized tool with no `baseCommand`, no `arguments` and no bound input: CWL gives its container no arguments, so the image runs its `CMD`, after its entrypoint when it has one, which this lowering does not render either. Runtime proof against the pinned Nextflow shows a command run after an image's entrypoint, without a `baseCommand` and with one, and run alone in an image without one.

**GPUs (approved lowering).** A tool's `cwltool:CUDARequirement` lowers to one Nextflow `accelerator N` directive. N is `cudaDeviceCountMin`, or `cudaDeviceCountMax` when the minimum is absent, and 1 when both are; that is the count cwltool's default job requests, and the rule `ResourceRequirement`'s cores and memory already follow. `cudaVersionMin` and `cudaComputeCapability` have no Nextflow counterpart: the lowering reads each one present and reports it with a coded diagnostic of severity note saying so, and the generated pipeline does not check it. A note is a `Severity.NOTE` diagnostic and does not stop compilation; a successful compile reports each note through Python's `warnings` module as the diagnostic's text, the channel the Nextflow reader already uses for findings that do not stop it, and the CLI shows it on standard error. Sophios diagnostics have no warning severity. No CI lane has a GPU, so the runtime evidence is the rendered directive together with a run under the local executor, which accepts the directive and runs the task; whether a scheduler places the task on the requested devices is that executor's contract, not a claim this backend proves.

**Environment variables (approved lowering).** `EnvVarRequirement` lowers when every `envDef` value is literal text or exactly one bare `$(inputs.<name>)` reference to a `string` input: the task exports each variable, its value shell-quoted, before the command. A literal value follows the `$` rule of §6, Commands. The referenced input must be present, because cwltool requires the value to be a string and an absent optional value would render the `[]` sentinel. Any other value — another type, an absent optional input, a `.path` or `.basename` suffix, an expression interpolated into text, or any other expression — is rejected by name, and so is an `envName` that is not a shell variable name. Runtime proof against the pinned Nextflow shows the command sees each value byte for byte.

---

## 7. Renderer and artifacts

The renderer accepts only `ExecutableNextflowWorkflow`. It is a pure deterministic transformation and makes no source-language decisions.

It does not:

- inspect raw CWL;
- reinterpret opaque strings;
- infer channel behavior from connection order;
- consume magic metadata keys;
- emit semantic TODOs;
- skip unsupported fields; or
- downgrade a compiler diagnostic into a comment.

For a valid executable model, rendering is total. Unsupported-user-input errors belong to capability analysis; renderer invariant failures are compiler defects.

Dry runs are not supported. Every generated process carries a `stub:` block that prints that Sophios-generated Nextflow does not support dry runs (`-stub-run`) and exits 1, so a dry run stops at its first task: a process with no stub block would run its real command under `-stub-run`. The block is part of the fixed rendering of every process, and the generated-subset reader reads it as such. It changes the rendering of every model, so a source generated without it no longer matches its model byte for byte: its artifact pair stops promoting exactly (§5.3), and the source is read by the supported DSL2 reader alone (§4.2).

The artifact set is:

| Artifact | Contract |
|---|---|
| `nextflow_workflow.json` | Versioned, validated executable IR. |
| `workflow.nf` | Deterministic DSL2 generated solely from executable IR. |
| `nextflow.config` | Deterministic supported configuration; the only Nextflow configuration Sophios supports. |
| `nextflow_params.json` | Deterministic serialized workflow parameters. |

Repeated rendering is byte-stable and does not mutate its input.

---

## 8. Verification architecture

Support is established across the complete source-to-runtime seam:

```text
real Sophios/CWL fixture
  -> real compiler boundary
  -> capability analysis and lowering
  -> executable-model validation
  -> rendering
  -> nextflow preview
  -> nextflow run
  -> observable output assertions
```

Hand-built executable IR is useful for renderer unit tests but cannot prove source-semantic correctness. Text assertions and preview prove shape and syntax only. Every behavior described as runnable requires installed-Nextflow execution from a real converted fixture.

When practical, supported CWL fixtures are also executed with a CWL reference runner and their declared observable outputs are compared with the generated Nextflow run.

Every rejected capability has a negative source-level test proving a precise diagnostic and absence of artifacts. Runtime CI installs the pinned Java and Nextflow versions; a missing executable is a CI failure, not a skip. Runs are offline, isolated and time-bounded. A failure surfaces as a structured diagnostic with a stable code and the authored names, emitted by Sophios itself; nothing relies on CI retaining or uploading logs or work directories.

Earlier-phase conformance suites remain cumulative gates for later phases.

---

## 9. Phase architecture

### Phase 1 — Safe flat backend

Phase 1 establishes the closed executable boundary for flat `CommandLineTool` DAGs, deterministic artifacts, public Python/CLI targeting, a generated-subset reader, structural import, and versioned round trips.

The executable subset includes only semantics with approved lowerings and real-runtime evidence: basic scalar/path inputs, scalar defaults, homogeneous fan-out, safe command tokens with CWL ordering, simple typed path globs and declared stream-file capture, flat DAG topology, and approved container/CPU/memory mappings.

Phase 1 rejects absent optional inputs until a runtime-safe lowering exists; executable scatter; nested workflows; shell mode and in-place staging; arbitrary command/glob expressions; primitive `loadContents`/`outputEval` capture; mixed channel qualifiers; arbitrary Groovy; and unknown requirements or bindings.

The Phase 1 reader recognizes the generated DSL2 subset. Unknown content remains explicit and structural; it cannot be promoted to executable form without capability approval.

### Phase 2 — Semantic expansion and composition

Phase 2 may add executable scatter and nested workflows, absent-option lowering, File/array behavior, command-construction expansion such as conditional flag tokens, shell-mode command construction, `InitialWorkDirRequirement`, richer output capture and expression handling, and any required channel adapters.

Each addition requires a separate lowering decision, executable-model extension, compatibility plan, negative boundary tests, and focused real-Nextflow proof. Scatter methods, collection cardinality, empty collections, nested namespacing, subworkflow I/O, shell security, expression scope, and schema migration are resolved before implementation.

Phase 2 does not authorize arbitrary Groovy parsing.

Approved Phase 2 lowerings:

- Boolean `inputBinding` flags (§6, Commands).
- `$(inputs.<name>.basename)` references (§6, Outputs and globs).
- `valueFrom` self-reference on a boolean binding (§6, Commands).
- Absent-optional `val` inputs unreferenced or flag-only in their consuming command (§6, Inputs and channels).
- Array-typed inputs of File/Directory/scalar items, with per-item command-line binding (§6, Commands; §6, Inputs and channels).
- `ShellCommandRequirement` with a literal-only, prefix-free `shellQuote: false` binding (§6, Commands).
- `InitialWorkDirRequirement` entries for one File or Directory input, under its own basename or a literal name, copied when writable and linked otherwise (§6, Inputs and channels).
- One queue fan-out channel adapter (§6, Inputs and channels).
- Single-input executable scatter over an array-typed workflow input (§6, Topology).
- Nested workflows at any depth, lowered by inlining (§6, Topology).
- `outputEval: $(self[0])` as an output cardinality declaration over a literal glob (§6, Outputs and globs).
- `loadContents` with `outputEval: $(self[0].contents)` as file-text capture on a `string` output (§6, Outputs and globs).

### Phase 3 — Safe expressions, conditional steps, and multi-input scatter

Phase 3 may add the safe JavaScript subset (§6, Expressions) in a step's `when`; conditional step execution; multi-input scatter under `dotproduct`, `flat_crossproduct`, and `nested_crossproduct`; and the collection composition these need — gathering scattered results into a downstream step, and process-produced arrays as scatter sources.

Approved Phase 3 lowerings:

- The safe JavaScript subset in a step's `when` (§6, Expressions).
- Multi-input scatter under all three methods, with index-ordered gather (§6, Topology).
- Conditional steps, with CWL `null` for skipped outputs carried by the absent-optional sentinel (§6, Topology).

Phase 3 adds none of the following. Each is out of this phase and needs a successor design, in the same posture as arbitrary JavaScript, rather than a later phase of this document:

- Nextflow-native graph inference. Precedence among explicit edges, imported topology, CWL inference, and a native pass is designed before any implementation, and ambiguity, cycles, fan-in, and type incompatibility produce deterministic diagnostics. Dropping "before Phase 3" from the non-goal records that the pass is out, not that it has become a silent default.
- Resource and environment directives beyond those §6, Resources and containers approves. Each one needs runtime observation before it is approved.
- REST artifact delivery. It is target-neutral mainline work, and it still needs an explicit security and operations contract: authentication, authorization, input limits, timeouts, secrets, provenance, retention, and artifact delivery.

Phase 3 does not authorize arbitrary JavaScript, user-written Groovy in any position (§6, Expressions), arbitrary Groovy equivalence, or remote Nextflow execution; each requires a successor design.

---

## 10. Compatibility and evolution

- Default and explicit CWL compilation preserve the public `CompiledWorkflow` contract.
- Existing CWL behavior remains a cumulative regression gate.
- The backend adapter follows the compiler's private semantic boundary without modifying inference behavior.
- Unsupported capability diagnostics are part of the user contract and remain stable enough for tooling.
- A phase may narrow an unimplemented candidate subset without a design revision; it may not claim or implement semantics this document rejects.
- Expanding executable semantics, changing phase ownership, changing the canonical representation, or weakening fail-closed behavior requires a `design:` revision to this file before implementation.
