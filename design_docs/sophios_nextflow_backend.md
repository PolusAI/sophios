# Sophios Nextflow Backend — Locked Design

**Status:** Approved architecture. Locked.
**Authority:** This document governs the complete Nextflow backend across all phases.
**Change control:** A commit that modifies this file must have a subject beginning with `design:`. Implementation, test, documentation, fix, refactor, and release commits must not modify it.

---

## 1. Governing rule

> A workflow is either fully lowered into a validated executable Nextflow model, or compilation fails with an actionable diagnostic.

Sophios never emits a partially supported workflow and never silently changes source semantics. A rendered TODO, retained metadata, plausible-looking script, successful preview, or hand-built intermediate representation is not evidence that a source workflow is executable.

This rule outranks feature breadth and schedule. When a source construct has no approved lowering, the compiler rejects it before artifact generation.

---

## 2. Product boundary

Sophios supports Nextflow DSL2 as a target alongside CWL. The backend has four responsibilities:

1. lower the supported subset of a compiled Sophios/CWL workflow into an executable Nextflow model;
2. render deterministic inspectable artifacts from that model;
3. read a deliberately supported DSL2 subset without silently discarding unknown content; and
4. expose the supported behavior through the existing Python, CLI, and later service boundaries.

CWL remains the canonical authored/compiled semantic substrate until a successor design explicitly changes that decision. The Nextflow backend consumes the compiler's private compiled semantic graph through an adapter. That graph is currently exposed as `CompilerInfo.rose`; a future core IR may replace it without changing the public Python API or this backend's semantic contract.

The backend does not perform a second forward inference pass. Imported workflows may use normal Sophios compilation and inference after reconstruction; they do not call low-level inference routines directly.

### Non-goals

- General Groovy parsing or execution equivalence for arbitrary `.nf` files.
- Silent preservation of unsupported semantics in executable output.
- A Sophios-managed Nextflow runner in Phases 1 or 2.
- Nextflow-native inference before Phase 3.
- Backward compatibility for private, unversioned intermediate Python objects. Serialized schemas are versioned explicitly.

---

## 3. Closed-world capability contract

Every source feature considered by the backend is classified as exactly one of:

| State | Meaning |
|---|---|
| **Supported** | Has an approved lowering, executable-model representation, renderer implementation, and real-runtime evidence. |
| **Rejected** | Produces a structured diagnostic before executable-model construction. |
| **Deferred** | Rejected now, with the owning future phase identified. |
| **Structural only** | May be retained loss-aware for import or inspection, but cannot be promoted to executable form. |

Capability analysis is closed-world. Each semantic source field is consumed by a supported lowering or reported as unsupported. Unknown requirements, bindings, expressions, directives, operators, and executable syntax are never ignored.

Diagnostics identify the source path, unsupported capability, current support state, and next supported action when one exists. Capability analysis aggregates independent findings so users do not have to fix unsupported constructs one at a time.

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

The direct `.nf -> NextflowDocument -> .nf` path is loss-aware. Opaque syntax is preserved and reported. Structural conversion to CWL compares representable names, ports, resources, and topology; it does not claim executable equivalence for opaque Groovy.

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

`--target nextflow` is sufficient by itself. CWL-specific run flags are rejected with structured diagnostics. Nested workflows too deeply composed for the current phase point users to the supported flattening option when applicable.

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

Connection variants make boundary-to-boundary dangling edges unrepresentable. Keyed output collections and post-normalization validation make duplicate emits unrepresentable. A workflow parameter feeding multiple ports is valid only when every sink agrees on channel semantics or an explicit adapter has been approved.

### 5.3 Serialization

Serialized IR declares a schema version and representation kind. Hydration validates all invariants. Schema evolution is backward-compatible or is accompanied by an explicit migration; it never relies on best-effort dictionary loading.

Additive extensions — new typed token or segment kinds — bump the schema version. Serialization always writes the current version. Hydration accepts the current version plus earlier versions whose value spaces are strict subsets of the current model; every other version is rejected.

Subset acceptance is enforced, not documentary: each additive token or segment kind declares the version that introduced it, and hydration rejects a payload that carries a kind newer than its declared `schema_version`. Version acceptance alone is not a compatibility claim — the promotion path compares rendered output byte-for-byte against stored source, so renderer output for previously representable models is the binding compatibility surface, and changing it invalidates existing artifact pairs regardless of the version number.

---

## 6. Semantic lowering contracts

### Expressions

**Expression handling is projection-only.** A CWL expression is representable in the executable model only when it is a *static projection*: a finite path into a known data structure, decided entirely at compile time from the CWL source text, where every step has a provably identical Nextflow counterpart. No expression is evaluated — at compile time or at runtime — and no expression text reaches the executable model or the generated pipeline. Any form requiring computation — arithmetic, string manipulation, comparison, conditional, operator, or function call — is not representable and fails closed. A projection whose Nextflow counterpart is *close but not exact* is not representable either: the divergence is never repaired by compensating logic, because that logic would itself be computation.

The approved projections are `$(inputs.<name>)` and `$(inputs.<name>.basename)` in template positions (§6, Commands; §6, Outputs and globs) and the two `outputBinding` capture declarations in §6, Outputs and globs. Each is one closed literal shape recognized by exact match against frozen data in the compiler, never by a grammar that could be widened a case at a time, and widening the set is a `design:` revision. Recognizing a shape is not the same as understanding a language: the compiler matches admitted text and rejects everything else, and no code path inspects, transforms, or composes expression text.

A lowering's own fixed rendered idiom is not the computation this rule forbids. The Groovy conditional a boolean flag renders to, the per-item join an array binding renders to, and the size and encoding preconditions the file-text capture renders to are each part of one mapping, chosen once at design time and identical for every workflow. What the rule forbids is deriving Groovy from CWL expression text, and repairing a mapping that does not already agree. A precondition that makes the generated pipeline fail exactly where CWL fails is the fatal-error half of a mapping, not a repair of one; a transform that massages a value into agreement *is* a repair, and disqualifies the projection outright.

**JavaScript evaluation is ruled out, not deferred.** Sophios does not evaluate CWL JavaScript expressions in any form: not at compile time, not at runtime, not by transpiling JavaScript to Groovy, not by embedding a JavaScript engine, and not by shelling out to `node`. Nor does anything evaluate in the renderer, which §7 fixes as a pure transformation of the executable model. The reasons are structural rather than practical. Evaluation breaks §3's closed-world contract, because an evaluated expression's result space cannot be classified in advance. It breaks §5.2's and §7's prohibition on opaque strings, because the expression text would have to survive into the executable model in order to be evaluated later. It makes generated pipelines non-self-describing, because the emitted `.nf` would no longer say what it does. Shelling out to `node` adds a container runtime dependency, which §6, Resources and containers requires evidence for. And it is the same category §9 already refuses for arbitrary Groovy equivalence. This is recorded as a decision rather than a deferral so that it is not re-litigated as one: revisiting it requires a successor design document, in the same posture §9 takes toward remote Nextflow execution, not a `design:` revision to this file.

Declaring `InlineJavascriptRequirement` is not itself the use of JavaScript, so a tool that declares it and then uses only approved forms stays legal — the same shape of decision as `ShellCommandRequirement` with no `shellQuote: false` binding, which changes nothing to render (§6, Commands). The requirement is a capability declaration; the violation is a non-admitted form. A later change must not "fix" this by rejecting the declaration.

### Commands

CWL command construction is normalized before rendering. Ordering follows the pinned CWL version, including defaults and tie-breaking. Command parts remain typed as literal data or interpolation references; quoting is decided per token or segment, never by scanning a completed string for `$`.

**Boolean flags (approved Phase 2 lowering).** A `boolean` input with an `inputBinding` and no `valueFrom` lowers to a typed conditional flag token that references the input by name and carries a non-empty prefix. At runtime, `true` renders the shell-quoted prefix as exactly one argv word and `false` renders nothing; a boolean binding without a prefix contributes nothing for either value. Flag tokens are valid only in command token position — never in globs or stream targets — and only against `val` ports. An optional boolean lowers the same way whether a value is present or absent: a flag token never dereferences its channel value, only tests it, so the absent-optional `val` lowering below covers this case directly and an absent optional boolean now renders identically to `false`.

The rendered flag is a conditional over its channel value, so the lowering is sound only when that value is a JSON boolean at runtime. Capability analysis therefore requires every input consumed by a flag token to resolve to a boolean-typed source, whether that source is a workflow input or a producing process output port. Truthiness of a staged path or of a string such as `"false"` must never be allowed to decide a flag.

**`valueFrom` self-reference on a boolean binding (approved Phase 2 lowering).** CWL evaluates `valueFrom` and then applies boolean flag semantics to the result, so a lowering must reproduce that ordering. Without a JavaScript evaluator, the only `valueFrom` shape provably boolean at runtime is the supported template form's bare `$(inputs.<name>)` reference to the binding's own input — a redundant but legal restatement of the original value. That shape lowers to the identical flag token the no-`valueFrom` case produces, so it inherits the same runtime rendering and the same boolean-source requirement. Every other `valueFrom` shape on a boolean binding — literal text, a reference to a different input, a `.path`/`.basename` suffix, or any expression outside the supported template form — is rejected, because none of those are provably boolean without either a JavaScript evaluator or aliasing another channel's presence semantics onto this one.

**Diagnostics and security for this lowering.** A rejected flag construct names the source path and the deferred capability, and independent findings aggregate like every other capability diagnostic. An empty prefix is a source error and is reported; an absent prefix is valid CWL and contributes nothing. The prefix is data, never syntax: it is emitted through the generated shell-quoting helper, so it reaches the process as exactly one argv word and cannot introduce shell operators, redirections, or command substitution.

**Shell-mode raw literal tokens (approved Phase 2 lowering, one supported shape).** Nextflow's generated `script:` block already joins every command token into one shell-quoted line and runs it through a shell, so `ShellCommandRequirement` with no `shellQuote: false` binding present changes nothing to render: that is already byte-identical to CWL's own shell-mode quoting (every token individually quoted, joined, invoked via `/bin/sh -c`), runtime-proven against the pinned Nextflow with an adversarial argument. `shellQuote: false` is CWL's explicit request to emit one binding's text unquoted so it can carry real shell syntax — pipes, redirections, globs — instead of one safely-quoted argv word, and it is approved only for the one shape that keeps that opt-out provably safe: a binding (`inputBinding` or `arguments` entry) on a tool that declares `ShellCommandRequirement`, carrying no `prefix`, whose `valueFrom` is required (a binding with no `valueFrom` binds the input's own runtime value, never a CWL-author literal, so that shape is never eligible) and parses to a template made entirely of literal segments — no `$(inputs...)` reference of any kind, in any form. That text is the CWL author's own, fixed at compile time, so rendering it unquoted opens no path from workflow input data to shell syntax; the generated shell-quoting helper is bypassed for that one token only, and every other token keeps going through it exactly as before. `shellQuote: false` on a binding with no `valueFrom`, with a `prefix`, or whose `valueFrom` references any input — directly, or via a `.path`/`.basename` suffix — is rejected: unquoting a runtime-supplied value, even a staged file's own path, would let workflow input data or a chosen file name be interpreted as shell syntax, which is exactly the boundary this lowering must not cross. `shellQuote: false` declared without `ShellCommandRequirement` is also rejected outright rather than silently treated as inert, matching the closed-world contract. Every other `shellQuote: false` shape remains rejected, as does every `InitialWorkDirRequirement` listing shape beyond the self-staging lowering below, and every other unapproved expression form.

**Array command-line binding (approved Phase 2 lowering, one supported shape).** An array-typed `inputBinding` without `itemSeparator` lowers to a typed conditional binding, runtime-proven against the pinned Nextflow: when the array is empty it contributes nothing at all — not even its own prefix, matching CWL — and otherwise it contributes its optional prefix once followed by each element as its own shell-quoted argv word, in array order. `itemSeparator` (joining every element into one argv word) and `separate: false` on an array binding are rejected: both need a materially different render shape from the per-item form above, so picking one supported shape here means deferring the other rather than half-supporting both. `valueFrom` on an array-typed binding is also rejected, for the same JS-evaluator reason `valueFrom` is restricted elsewhere in this document.

### Inputs and channels

File and Directory inputs lower to path semantics. Supported JSON scalar inputs lower to value semantics. Defaults are explicit model values. Missingness is distinct from JSON `null`.

**Absent-optional `val` inputs (approved Phase 2 lowering).** An absent optional value lowers to a reserved empty-array sentinel `[]` carried by a `Channel.value(...)` element — one element, never zero — so the channel always terminates and a receiving process can observe the exact value it received. Runtime proof ruled out the naive representation: `Channel.value(null)` never binds under the pinned Nextflow runtime, so the process that consumes it never starts and the run hangs indefinitely — a literal JSON `null` is not a terminating channel value here, whatever CWL's own null means. `[]` is Groovy-falsy like `null` and `false`, so a flag ternary treats it identically, and it is unambiguous within this lowering's scope because this lowering never applies to an array-typed port: no scalar (`string`/`int`/`float`/`boolean`) value is ever legitimately an array, so this sentinel can never collide with a genuinely empty array under the array lowering below. This is sound only where nothing dereferences the value unconditionally: interpolating the sentinel into a plain template segment (`.toString()` on a Groovy list) renders `"[]"`, not a clean absence signal. The lowering is therefore scoped to `val`-qualifier inputs whose consuming tool port is either never referenced in that tool's command tokens, stream targets, or output globs, or referenced solely as the boolean-flag token it drives — both positions already treat their value as a condition rather than dereferencing it, so the sentinel renders safely as falsy. Every other reference position — a plain value interpolation anywhere in a command token, stream target, or glob — keeps rejecting an absent optional source, because a general presence-gated value binding (the value analogue of a flag) has no approved lowering yet. Absent-optional `path` (File/Directory) inputs also keep rejecting: `path`-qualifier channel construction always stages its value unconditionally, so a safe representation needs its own runtime-proven convention, deferred. An absent-optional array-typed input is likewise out of scope for this lowering.

Channel construction considers all consumers. Connection order never selects a qualifier or staging policy.

**Array-typed inputs (approved Phase 2 lowering).** A CWL array input — `{type: array, items: <scalar>}`, where `<scalar>` is `File`, `Directory`, `string`, `int`, `float`, or `boolean` — lowers to a port carrying the same qualifier its item type would carry alone (`path` for File/Directory items, `val` for scalar items), plus an explicit array marker that participates in the same channel-qualifier-consistency check as `path_kind`: a scalar port and an array port can never share one workflow parameter. Cardinality is otherwise a channel-construction concern, not a new qualifier, and both forms are runtime-proven: a `val` array is a JSON list flowing through `Channel.value(...)` exactly as any other JSON value; a `path` array is a single `Channel.value(...)` element holding a Groovy list built with `file(...)` per element, so Nextflow stages every element for one process invocation rather than fanning the channel out over several. An empty array is a present, valid, zero-length value — distinct from the absent-optional lowering above, which represents no value at all and is out of scope for array-typed ports entirely; this lowering never emits or accepts the absent-optional sentinel for an array port, so the two representations cannot collide.

The shorthand string form (`"File[]"`), nested arrays, and a per-item `inputBinding` on the array's `items` schema are all rejected; only the flat mapping form with a bare scalar item type is representable. Array-typed outputs are also rejected: only array-typed inputs have an approved lowering.

**`InitialWorkDirRequirement` self-staging (approved Phase 2 lowering, one supported shape).** Nextflow already stages every `path`-qualifier input under its own original file name, so a `listing` entry that only asks for that — the bare shorthand expression `$(inputs.<name>)`, or a `Dirent` whose `entry` is that same bare reference and whose `entryname` is absent or the self-referencing `$(inputs.<name>.basename)` — is a no-op under this lowering: it is accepted and lowers to nothing beyond the port that input's declaration already produces. Requesting a genuinely different staged name is a `Dirent` whose `entryname` is a literal string with no expression and no path separator; runtime proof against the pinned Nextflow shows its `path` input qualifier accepts a `stageAs:` option that stages the channel value under that literal name while still binding it to its own port variable, so such a port carries an explicit `stage_as` marker and the renderer emits `path <name>, stageAs: '<literal>'` in place of the bare `path <name>`. Runtime proof also shows a renamed port's own `.name` value reports the *staged* name, not the original CWL basename, so this lowering forbids any other reference — plain or basename — to a renamed input anywhere in that tool's command tokens, stream targets, or output globs: resolving what such a reference would mean is out of scope, and the supported pattern is for the command to address the staged file by the literal name the CWL author already chose, hard-coded directly into `baseCommand`/`arguments`.

Every other listing shape is rejected with a named diagnostic: `writable: true` (copy-and-mutate semantics), an `entry` that is not a bare reference to one File/Directory input (inline content construction), an `entryname` that is any other expression (a computed rename), a listing entry naming a `val`-qualifier or array-typed input, and a listing entry that is neither a string nor a `Dirent` mapping. Two ports renamed to the same literal within one process is also rejected, as an unresolvable staging collision.

**Required channel adapters (approved Phase 2 lowering, exactly one adapter).** Scatter, below, is the only approved topology whose two ends disagree about channel cardinality: a scattered process consumes one element per task where an unscattered one consumes a whole value. That disagreement is represented by exactly one typed adapter attribute on a workflow-input connection, carrying one operator from a closed approved set whose sole member is the queue fan-out. It renders as Nextflow's `flatten` operator applied at the consumption site — `PROCESS(<parameter>.flatten())` — so the parameter's own channel construction is unchanged: the value channel carrying the whole JSON list is exactly the array-typed-input construction already runtime-proven above, and each consumption site derives its own queue channel from it. Runtime proof against the pinned Nextflow shows one such parameter feeding two scattered processes yields two independent per-element channels rather than a channel consumed once, and that the other inputs of a scattered process, still value channels, broadcast to every task. The adapter participates in the workflow parameter's channel-semantics consistency check alongside `path_kind` and the array marker, so one parameter is either adapted at every sink or at none, and connection order still never selects channel behavior.

Every other adaptation a topology might require is rejected with a named diagnostic rather than approximated: collecting a queue channel back into one list-valued element (the gather half of scatter/gather), pairing or crossing two queue channels, and every other operator. The approved set is closed data in the executable model, so the renderer maps an adapter to its operator and can neither widen nor infer one.

### Outputs and globs

Every output has a concrete capture mechanism. Phase-specific support may include path globs and declared stdout/stderr file capture. Primitive outputs are not represented as bare variable names. A rendered glob remains a glob at run time: a name assembled from runtime data is still pattern-interpreted, so a staged name carrying a glob metacharacter does not match the file the process wrote. Rendering an assembled name literally is a distinct lowering, because it changes emitted bytes and the generated-subset reader with them.

Supported glob expressions are parsed into typed literal and input-reference components. Raw CWL expression strings cannot enter the executable model.

Beyond the glob, `outputBinding` capture behavior falls into three statuses this section keeps distinct, because they are not the same kind of "no":

- **Approved.** The two `outputEval` capture declarations below, each one exact literal text under a literal-glob restriction.
- **Ruled out on the record.** Evaluating any other `outputEval` text, which would require the JavaScript evaluation §6, Expressions permanently refuses. These are not awaiting a lowering.
- **Rejected with a known relaxation path.** An approved capture declaration paired with a wildcard or input-reference glob, whose gate is stated below as a specific proof obligation.

`secondaryFiles`, `format`, record-typed outputs, array-typed outputs, and expression forms in a workflow output's `outputSource` remain rejected with no approved lowering.

**Basename references (approved Phase 2 lowering).** `$(inputs.<name>.basename)` lowers to a typed basename segment valid in every template position: command tokens, stream targets, and output globs. The referenced input must be a path port, because the lowering relies on Nextflow staging an input under its original file name, which makes the staged path's name property exactly the CWL `basename`. Basename segments against value ports are unrepresentable in the executable model, and the same requirement is reported by path during capability analysis, so a source document naming a value input reports every offending position at once rather than failing later on a normalized identifier. In output-glob position the reference must derive a new name: a declaration matching only a staged input captures nothing.

**Output cardinality declaration (approved Phase 2 lowering, one supported literal).** An `outputBinding.outputEval` whose text is exactly `$(self[0])` — surrounding whitespace trimmed, no tolerance for internal variation — is recognized as a *cardinality declaration*, not as an expression: it states that the port carries one value rather than a list. It lowers to a closed capture marker on the output port, mirroring the array marker on input ports, and no `outputEval` token of any kind enters the executable model. The marker renders as Nextflow's `arity: '1'` option on the `path` output, runtime-proven against the pinned Nextflow to emit a single path value rather than a list and to fail the task when nothing matches.

The declaration is admitted only when the paired `glob` is one literal containing no wildcard character — no `*`, `?`, or `[` — and no input reference. `self[0]` means "the first of the matched list", so projecting it is provably identical only where the match set has exactly one member by construction, and a literal glob names at most one filesystem entry. A wildcard glob would instead make the two runtimes' glob match *ordering* load-bearing, and that ordering is unproven. An input-reference glob is rejected for the same reason one step removed: its runtime value could itself contain a wildcard, so wildcard-freedom is not decidable from the source text. The restriction lives in the executable model rather than only in capability analysis — a capture marker beside a non-literal or wildcard-bearing glob is an unrepresentable state, so no later change can reach it by a different code path.

Zero-match and multi-match are pinned under that restriction. Multi-match cannot arise: a literal glob names one path. Zero-match fails the run in both runtimes — CWL cannot satisfy a required `File` output from an empty match set, and the generated `arity: '1'` output fails the Nextflow task with a missing-output error rather than emitting an empty channel.

**Relaxation path for wildcard globs.** Admitting `$(self[0])` against a wildcard glob is a gated extension with known requirements, not a dead end. A future contributor must demonstrate that CWL's glob match ordering and Nextflow's are identical, covering at least the cases where they could plausibly diverge: filenames of differing length, mixed upper and lower case, numeric suffixes where lexical and numeric order disagree (`part2` against `part10`), and the locale sensitivity of the collation each runtime's underlying glob implementation uses. Absent that proof the two runtimes may silently select *different files* for the same workflow with no error raised anywhere, which is exactly the failure mode §1 forbids; the restriction therefore stands until the ordering is proven rather than assumed. The same proof would unlock the input-reference glob, whose only defect is that it cannot be shown wildcard-free at compile time.

**Rejected `self` projections.** Every `outputEval` text outside the two admitted literals is rejected, and the shapes a CWL author is most likely to reach for are rejected by name rather than by a generic miss: `.dirname`, `.path`, `.location`, `.checksum`, `.secondaryFiles`, and `.format`. Most of these simply have no lowering. `.dirname` and `.path` are a different case and are worth stating explicitly, because they are permanently unattractive rather than merely unimplemented: under this backend's staging model a produced file's directory *is* the Nextflow task work directory — an opaque hashed path Nextflow owns, is free to relocate, and may clean up — and it is never the directory the CWL author was describing. A mapping onto `task.workDir` would therefore be technically straightforward and semantically a lie: it would type-check, run, and hand a downstream consumer a path whose meaning did not survive the translation. That is silently changing source semantics, which §1 forbids, so these two stay rejected precisely because they are easy.

**File-text capture (approved Phase 2 lowering, one supported literal).** `loadContents: true` paired with an `outputEval` of exactly `$(self[0].contents)`, on an output declared `type: string`, under the same literal-glob restriction as the cardinality declaration, lowers to a closed file-text-capture marker: the port becomes a value port whose value is the named file's decoded text. Both halves are required and neither is admitted alone — `loadContents` without that `outputEval` is rejected, that `outputEval` without `loadContents: true` is rejected, and the pairing on any output type other than `string` is rejected.

`type: int` and `type: float` are rejected specifically, and the diagnostic says why: parsing text into a number is computation, and this is exactly where §6, Expressions draws its line. Capturing bytes is a projection; interpreting them is not.

The semantics are the pinned CWL version's, and both halves of them had to be established by runtime proof because the obvious Nextflow idiom is wrong twice over.

- **Whole file, with a hard size limit — not truncation.** CWL v1.2, the version the compiler pins, requires a `loadContents` file to be a UTF-8 text file of 65536 bytes or fewer and requires the implementation to read it *entirely*, raising a fatal error above that size. CWL v1.0's "read up to the first 64 KiB" was replaced by that hard limit, so under the pinned version there is no truncation point to reproduce and no partially-decoded multibyte sequence to define. The reference runner agrees: it reads one byte past the limit and fails rather than truncating.
- **Trailing newline preserved verbatim.** A trailing newline is part of the contents under CWL, while shell- and `stdout`-style capture strips it. Runtime proof against the pinned Nextflow confirms the trap concretely: its `eval` output type, which is the natural reading of "capture the file's text", strips the trailing newline, so it is not an admissible counterpart however convenient it looks. Compensating for that by re-appending a newline is exactly the repair §6, Expressions forbids.

The lowering therefore renders as a `val` output over a generated helper that reads the file from the task work directory, refuses a file larger than 65536 bytes, and decodes UTF-8 strictly. Runtime proof pins each claim separately: a trailing newline preserved byte-for-byte, a 65536-byte file accepted, a 65537-byte file failing the task, multibyte text decoded to the same characters, and malformed UTF-8 failing the task rather than being silently replaced with U+FFFD as a lenient decoder would. The size and encoding preconditions are the fatal-error half of the CWL contract rather than a repair of a mismatched mapping. The helper is emitted only into pipelines that actually use this capture, so artifacts for previously representable models stay byte-identical and existing artifact pairs keep promoting (§5.3).

**The sink of a captured value.** A file-text-capture output's only approved sink is a workflow output. A process-to-process edge out of one is rejected with a named diagnostic, for a reason that is about the model rather than about this lowering: until now every process output carried the `path` qualifier, so a process edge could not disagree with its destination port about channel semantics, and the executable graph has no source-to-destination qualifier agreement check to lean on. Relaxing this means adding that check — the process-edge analogue of the workflow parameter consistency rule in §5.2 — and proving a captured value feeding a downstream `val` port at runtime. That is a self-contained follow-up, not a redesign, and the restriction stands until it is done. The restriction is enforced in the executable model as well as in capability analysis, so no other code path can reach the combination.

**Cardinality and scatter.** The capture marker is per task and scatter's cardinality is per collection, so the two compose without overlap. `arity: '1'` says each task of a process produces one matching file; the single-input scatter lowering (§6, Topology) says how many tasks run. A scattered step over N elements therefore runs N tasks, each producing one file, and its output queue channel carries N single-file elements — exactly the array-typed workflow output CWL declares for a scattered step whose tool output is a single `File`. The marker needs no scatter-specific case, and the scatter contract's restriction of a scattered step's output to a workflow-output sink applies unchanged. This is the model's first output-cardinality concept, and it deliberately does not attempt to restate the collection cardinality scatter already owns.

### Topology

The executable graph validates endpoint existence, direction, multiplicity, acyclicity where required, unique emits, normalized-name collisions, and workflow-boundary consistency before rendering.

**Single-input executable scatter (approved Phase 2 lowering, one supported shape).** A step whose `scatter` names exactly one input — the string form, or a one-element list — lowers to that input's port receiving a queue channel of the source array's elements through the one approved channel adapter above, so the process runs once per element while its remaining inputs stay value channels and broadcast to every task. Multi-input scatter is rejected, and with it every case where `scatterMethod` is load-bearing: `dotproduct`, `flat_crossproduct`, and `nested_crossproduct` all describe how two or more scattered arrays combine, which is the genuinely hard cardinality decision this phase defers rather than guesses. At exactly one scattered input all three coincide — one task per element, output nesting depth one — so a `scatterMethod` carrying one of those three values alongside a single-input `scatter` is consumed as an inert restatement instead of rejected; the compiler emits `scatterMethod: dotproduct` unconditionally, so rejecting the field outright would put executable scatter out of reach of the public Python and CLI surfaces and out of reach of the real-fixture runtime proof this document requires. Any other `scatterMethod` value is rejected by name.

The scattered input's source must be exactly one array-typed workflow input whose item type carries the same qualifier and path kind as the scattered port itself. That is less a restriction than the only representable shape: array-typed tool outputs have no approved lowering, so no process output can carry an array to scatter over. Scattering over an array-marked port is rejected for the same reason nested arrays are.

**Collection cardinality.** A scattered step's output port carries one value per task, not one value overall. Its only approved sink is a workflow output, where a queue channel of N elements is exactly the array-typed workflow output CWL declares for a scattered step. A process-to-process edge out of a scattered step is rejected with a named diagnostic: CWL gives the downstream step one invocation receiving an array, while a queue channel of N elements drives N downstream invocations, and closing that gap needs the gather adapter this phase defers. For the same reason a scattered step's own non-scattered inputs must come from workflow parameters — a process output is a queue channel, and pairing it with the scatter's queue channel would silently truncate the run to one task instead of N.

**Empty collections.** Scattering over an empty array runs zero tasks. Runtime proof against the pinned Nextflow shows the derived queue channel terminates immediately, unscattered processes in the same workflow still run, the workflow output channel is simply empty, and the run exits successfully — no hang, unlike the `Channel.value(null)` representation ruled out above. An empty array is a present value here exactly as it is under the array-input lowering, and an absent-optional array-typed input stays rejected, so the absent-optional sentinel can never be mistaken for a zero-length scatter.

**One level of nested workflows (approved Phase 2 lowering).** A step whose compiled `run` is itself a `Workflow` lowers by inlining, before any other analysis: the subworkflow's own steps become steps of the outer workflow, each declared subworkflow input is replaced by whatever the outer step binds it to, and every reference to the outer step's outputs is rewritten to the inner endpoint that subworkflow output's `outputSource` names. Emitting a nested DSL2 `workflow` block instead is deferred: every executable connection variant addresses a process endpoint, so a subworkflow call would introduce a new endpoint kind across the graph validator, the renderer, and the generated-subset reader at once, while inlining reaches the same observable execution — the same tasks, the same data flow, the same output files — with no new endpoint kind at all. Inlining is a lowering decision recorded in the executable graph before capability analysis runs, never a source-language decision in the renderer, and it is reported rather than silent: the emitted artifacts are flat by construction, so the composition structure lives in the source workflow and in the namespaced process names, not in the generated DSL2.

Composition findings are resolved before the rest of capability analysis, because the flat graph every other pass analyzes cannot be built while its composition is unsupported. Composition findings still aggregate among themselves across every nested step.

**Nested namespacing.** Each inlined step is named by joining the outer step's local identifier and the inner step's own with `___`, before normalization. Two instantiations of one subworkflow in the same parent therefore cannot collide, and the existing normalized-name collision validation applies to the namespaced names rather than to the inner ones.

**Subworkflow I/O.** Every declared subworkflow input must be bound by the outer step's `in`, and every name in the outer step's `out` must be a declared subworkflow output; an unbound input or an undeclared output is rejected rather than defaulted. Each subworkflow output must carry exactly one `outputSource` resolving to one of that subworkflow's own step outputs: a subworkflow output that forwards one of its inputs is rejected, exactly as boundary passthrough is rejected at the outer boundary. An inner step's sources resolve either to another inner step's output or to a subworkflow input, which the outer binding replaces; anything else is rejected by name.

**Depth and scatter.** Nesting deeper than one level is rejected with a named diagnostic: a second level buys nothing this phase needs, and its namespacing and binding proof would have to be re-established at every depth. `scatter` on a subworkflow step is also rejected: scattering an inlined sub-DAG is a fan-out over a set of processes, not the single-process shape the scatter lowering above approves. `scatter` on a step *inside* a subworkflow is not a separate lowering — after inlining such a step is indistinguishable from an outer scattered step, and the scatter contract above applies to it verbatim, including its requirement that the scattered source be an array-typed workflow input of the flattened workflow.

**Workflow-level requirements.** `ScatterFeatureRequirement` and `SubworkflowFeatureRequirement` declare only that a document uses a feature whose lowering this phase decides per step, so each is consumed as an inert no-op carrying no fields beyond `class`. Every other workflow-level requirement is rejected by name, at the outer and the subworkflow level alike, and workflow-level `hints` remain unconsumed.

### Resources and containers

Only explicitly mapped directives are executable. Unsupported requirements and hints are diagnosed rather than ignored. Text rendering is not runtime proof; CPU, memory, environment, and container claims require the evidence appropriate to the phase.

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

The artifact set is:

| Artifact | Contract |
|---|---|
| `nextflow_workflow.json` | Versioned, validated executable IR. |
| `workflow.nf` | Deterministic DSL2 generated solely from executable IR. |
| `nextflow.config` | Deterministic supported configuration. |
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

Every rejected or deferred capability has a negative source-level test proving a precise diagnostic and absence of artifacts. Runtime CI installs the pinned Java and Nextflow versions; a missing executable is a CI failure, not a skip. Runs are offline, isolated, time-bounded, and retain diagnostics and work artifacts on failure.

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

Approved Phase 2 lowerings to date:

- Boolean `inputBinding` flags (§6, Commands).
- `$(inputs.<name>.basename)` references (§6, Outputs and globs).
- `valueFrom` self-reference on a boolean binding (§6, Commands).
- Absent-optional `val` inputs unreferenced or flag-only in their consuming command (§6, Inputs and channels).
- Array-typed inputs of File/Directory/scalar items, with per-item command-line binding (§6, Commands; §6, Inputs and channels).
- `ShellCommandRequirement` with a literal-only, prefix-free `shellQuote: false` binding (§6, Commands).
- `InitialWorkDirRequirement` self-staging under an input's own basename or an explicit literal rename (§6, Inputs and channels).
- One queue fan-out channel adapter (§6, Inputs and channels).
- Single-input executable scatter over an array-typed workflow input (§6, Topology).
- One level of nested workflows, lowered by inlining (§6, Topology).
- `outputEval: $(self[0])` as an output cardinality declaration over a literal glob (§6, Outputs and globs).
- `loadContents` with `outputEval: $(self[0].contents)` as file-text capture on a `string` output (§6, Outputs and globs).

### Phase 3 — Native inference, advanced execution, and service delivery

Phase 3 may add Nextflow-native graph inference, approved advanced resource/environment directives, and REST artifact delivery.

Inference precedence among explicit edges, imported topology, CWL inference, and Nextflow-native inference is designed before implementation. Ambiguity, cycles, fan-in, and type compatibility produce deterministic diagnostics. Advanced directives require runtime observation. REST requires an explicit security and operations contract covering authentication, authorization, input limits, timeouts, secrets, provenance, retention, and artifact delivery.

Phase 3 does not automatically authorize remote Nextflow execution or arbitrary Groovy equivalence; either requires a successor design.

---

## 10. Compatibility and evolution

- Default and explicit CWL compilation preserve the public `CompiledWorkflow` contract.
- Existing CWL behavior remains a cumulative regression gate.
- The backend adapter follows the compiler's private semantic boundary without modifying inference behavior.
- Unsupported capability diagnostics are part of the user contract and remain stable enough for tooling.
- A phase may narrow an unimplemented candidate subset without a design revision; it may not claim or implement semantics forbidden or deferred by this document.
- Expanding executable semantics, changing phase ownership, changing the canonical representation, or weakening fail-closed behavior requires a `design:` revision to this file before implementation.
