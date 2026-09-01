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

`--target nextflow` is sufficient by itself. CWL-specific run flags are rejected with structured diagnostics. Nested workflows rejected by the current phase point users to the supported flattening option when applicable.

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

### Commands

CWL command construction is normalized before rendering. Ordering follows the pinned CWL version, including defaults and tie-breaking. Command parts remain typed as literal data or interpolation references; quoting is decided per token or segment, never by scanning a completed string for `$`.

**Boolean flags (approved Phase 2 lowering).** A `boolean` input with an `inputBinding` and no `valueFrom` lowers to a typed conditional flag token that references the input by name and carries a non-empty prefix. At runtime, `true` renders the shell-quoted prefix as exactly one argv word and `false` renders nothing; a boolean binding without a prefix contributes nothing for either value. Flag tokens are valid only in command token position — never in globs or stream targets — and only against `val` ports. An optional boolean lowers the same way when a value is present; an absent optional value keeps rejecting under the option-lowering rule above.

The rendered flag is a conditional over its channel value, so the lowering is sound only when that value is a JSON boolean at runtime. Capability analysis therefore requires every input consumed by a flag token to resolve to a boolean-typed source, whether that source is a workflow input or a producing process output port. Truthiness of a staged path or of a string such as `"false"` must never be allowed to decide a flag.

`valueFrom` on a boolean `inputBinding` is rejected. CWL evaluates `valueFrom` and then applies boolean flag semantics to the result, so a lowering would have to reproduce that ordering; until one is approved, the construct fails closed rather than emitting the prefix beside a rendered value.

**Diagnostics and security for this lowering.** A rejected flag construct names the source path and the deferred capability, and independent findings aggregate like every other capability diagnostic. An empty prefix is a source error and is reported; an absent prefix is valid CWL and contributes nothing. The prefix is data, never syntax: it is emitted through the generated shell-quoting helper, so it reaches the process as exactly one argv word and cannot introduce shell operators, redirections, or command substitution.

Shell operators exist only under an explicitly supported shell-mode lowering. `ShellCommandRequirement`, `shellQuote: false`, `InitialWorkDirRequirement`, and unapproved expression forms are rejected until that lowering exists.

### Inputs and channels

File and Directory inputs lower to path semantics. Supported JSON scalar inputs lower to value semantics. Defaults are explicit model values. Missingness is distinct from JSON `null`.

Optional absence is supported only after a terminating, observable Nextflow lowering is approved and runtime-proven. Until then, absent optional values are rejected rather than emitted as non-producing channels.

Channel construction considers all consumers. Connection order never selects a qualifier or staging policy.

### Outputs and globs

Every output has a concrete capture mechanism. Phase-specific support may include path globs and declared stdout/stderr file capture. Primitive outputs are not represented as bare variable names.

Supported glob expressions are parsed into typed literal and input-reference components. Raw CWL expression strings cannot enter the executable model. `loadContents`, `outputEval`, arbitrary expressions, and other capture behavior are rejected until their lowering is approved.

**Basename references (approved Phase 2 lowering).** `$(inputs.<name>.basename)` lowers to a typed basename segment valid in every template position: command tokens, stream targets, and output globs. The referenced input must be a path port, because the lowering relies on Nextflow staging an input under its original file name, which makes the staged path's name property exactly the CWL `basename`. Basename segments against value ports are unrepresentable in the executable model.

### Topology

The executable graph validates endpoint existence, direction, multiplicity, acyclicity where required, unique emits, normalized-name collisions, and workflow-boundary consistency before rendering.

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
