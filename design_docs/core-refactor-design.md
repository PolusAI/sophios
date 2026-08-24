# Sophios Core Refactor — Design

**Status:** Approved. This design is locked; changes require a new revision.
**Baseline:** `master` at `6570369`
**Scope:** Four specs, delivered in order. Each is independently shippable.

## Naming

**Sophios** is the language. It has one DSL with two surfaces: a YAML spelling,
conventionally stored in files named `.wic`, and the Python API. Neither is
primary; §5.3 and Spec 1's adherence properties depend on that being true.

`.wic` is a file extension. This document writes "`.wic` files" for files on
disk and "Sophios" for the language. Some concrete syntax still carries the
older `wic` prefix — the `wic:` block, the `!ii` / `!&` / `!*` tags, the `wic_*`
desugared keys — and keeps it, because existing workflows depend on those
spellings. The language's own version tag is `lang_version`, which is
unimplemented and therefore free to name correctly.

---

## 1. Governing constraint

**Existing `.wic` workflows and the existing Python API keep working.**

This outranks every other goal in this document. Where a design choice
conflicts with it, the choice loses. Two deliberate exceptions are recorded in
§3; there are no others.

---

## 2. What is being fixed

Four problems. The third is a consequence of the first.

**1. No typed intermediate representation.** `Cwl`, `Json`, and `Yaml` are all
`dict[str, Any]`. Every semantic operation is untyped dictionary mutation, so
neither the type checker nor a reader can verify anything about the compiler's
subject matter. The types describe the container; they never describe the
concept.

**2. Phases are entangled.** Parsing, tree merging, edge inference,
namespacing, and CWL lowering happen inside one recursive fixed-point function.
No phase can be tested or reasoned about in isolation.

**3. State is threaded through recursion.** `explicit_edge_defs`,
`explicit_edge_calls`, `input_mapping`, and `output_mapping` are mutated across
recursion levels. Deferred satisfaction is implemented as mutation, so
behaviour depends on traversal order. These four dictionaries are fragments of
a graph that has no home, so the call stack became the data structure.

**4. The algorithms carry known hazards.** Speculative compilation is `O(2^n)`
by the documentation's own admission. Edge inference is ambiguous. The
fixed-point loop terminates on an arbitrary `max_iters` cap rather than an
argument.

**Why tests do not currently protect against this.** The suite passes. It is
also almost entirely example-based: partition independence — the compiler's
first stated design criterion — is checked against a fixed set of hand-written
workflows, each with one hand-chosen partitioning. One property-based test
exists, and its only assertion is that compilation does not raise an
unwhitelisted exception. Namespace uniqueness, output determinism, edge type
compatibility, and termination are not checked at all. "The tests pass" is
therefore weak evidence that a change preserved meaning, which is why Spec 2
precedes Spec 3.

**Scale at baseline:** `src/sophios` is 12,247 lines excluding vendored
`ict_spec`; `compiler.py` is 1,199; `compile_workflow` takes 14 parameters;
`compile_workflow_once` carries a 27-field `_WorkflowSetup`.

---

## 3. Compatibility contract

### Guaranteed

- The Sophios DSL, in both surfaces. `lang_version` 0.0.1 is defined to accept
  what exists today, validated against `docs/tutorials/`, `examples/`, and the
  `.wic` files in `mm-workflows` and `image-workflows`. The version tag is
  optional, so no existing file requires editing.
- `Workflow`, `Step`, `CompiledWorkflow`, and the `tool_builder` classes.
- The five types reachable by external callers: `Tools`, `StepId`, `Tool`,
  `Json`, `RawJson`.
- Console entry points and CLI flags, including `--allow_raw_cwl`.
- ICT, Workflow Builder, and REST *behaviour*.

`Workflow.compile()` returns `CompiledWorkflow`, which already walls off the
compiler's internal result (`CompilerInfo`, `RoseTree`) behind the private
`_compile()`. The IR replaces `RoseTree` behind that boundary with no public
change.

### Deliberate exceptions

1. **`sys.exit(1)` from library code becomes structured diagnostics.** A compile
   error currently terminates the host process, which is why the fuzz test must
   whitelist `SystemExit` and why the Python API cannot surface a compile error
   as a catchable exception. CLI exit codes are preserved; only embedders see a
   difference.

2. **Contrib Python import paths move** when the zones are physically separated
   (§4). REST behaviour is unchanged and HTTP clients are unaffected, since they
   never imported Python. The exception is confined to the peripheral zone.

### Not guaranteed

- **Byte-identical generated CWL.** The IR may emit semantically equivalent CWL
  with different key ordering. Semantic equivalence is guaranteed and verified
  by differential testing; byte stability is not.
- **`_compile()` / `CompilerInfo` stability.** Private, but in-repo code already
  reaches past it, so the change is announced rather than assumed invisible.

### Enforcement

The contract is checked, not promised:

| Mechanism | Protects | Spec |
|---|---|---|
| Conformance corpus validated before the spec lands | Every reachable `.wic` still parses | 1 |
| Passthrough-fidelity property | Uninterpreted CWL survives byte-identically | 2 |
| Path-agreement property | API → `write_wic` → compile equals direct compile | 2 |
| Differential testing, old against new | The IR migration preserves behaviour | 3 |
| Import-boundary check in CI | Core acquires no contrib dependency | 0 |
| Entry-point resolution check | Console scripts and container commands name real modules | 0 |

---

## 4. Spec 0 — Quarantine

ICT, the Workflow Builder converter, and the REST API are peripheral to the
compiler. They are supported and must keep working. They must not shape core
design.

### Zones

| Zone | Source | Tests |
|---|---|---|
| **contrib** — ICT, WFB converter, REST | `src/sophios/contrib/` | `tests/contrib/` |
| **core** — compiler, Python API, compute hook | `src/sophios/` (unchanged) | `tests/core/` |

Separation is structural. A boundary visible in the tree is noticed before it
is crossed; a boundary that lives only in a check is discovered after review.

**The asymmetry in `src/` is required.** Only contrib relocates. Moving core to
`src/sophios/core/` would rename every path clients import — `sophios.run_local`,
`sophios.utils`, `sophios.api.python.workflow` — and all four console entry
points, which is precisely what §1 forbids. A single `contrib/` subtree makes
the boundary equally visible. Tests are symmetric because `tests/` is not an
importable package, so the split costs nothing.

### Invariant

> **contrib may import core. core may never import contrib.**

Enforced over the real module graph, not a fixed list, so new core modules are
covered automatically. A breach fails the build.

### Test zoning

Contrib tests run in core CI as their own step, not on a separate schedule.
Tests on a separate cadence are looked at on a separate cadence, which is how a
supported surface quietly stops working. The step boundary makes a red build
attributable to a zone without bisecting.

Zone membership is defined by directory. Any marker is derived from location
rather than applied by hand, so the two cannot drift.

`runtime_inputs.py` remains core; REST merely also consumes it.

### Consequence for Spec 1

The WFB converter currently *emits* `.wic`, so its quirks are implicitly an
input to language design. After quarantine the converter is a *client* of the
specified grammar: non-conforming output is a contrib bug, not a language
constraint.

---

## 5. Spec 1 — Specified and versioned Sophios grammar

### 5.1 What is being specified

Sophios is a leaky abstraction over CWL, deliberately. A user writes shorthand
for the common case and drops into raw CWL for anything the shorthand does not
cover. That design is retained. Sealing the abstraction would mean re-inventing
CWL one feature at a time.

The defect is not the leak; it is that **the boundary was never stated**. A
leaky abstraction with a specified boundary is a contract. Without one it is
unpredictability — which is how a bare `requirements:` became a crash rather
than a diagnostic.

The grammar pins the leak. It does not plug it.

### 5.2 The CWL substrate

Passthrough means "this is CWL, handed over unchanged", so part of Sophios's
meaning is CWL's meaning. A specification written against an unspecified CWL
version specifies nothing. At baseline the version is genuinely unspecified:
`compiler.py` emits `v1.2` for workflows, `python_cwl_adapter.py` hardcodes
`v1.0` for generated CommandLineTools, and the schema validates `cwlVersion` as
any non-empty string.

**Sophios is specified as an abstraction over CWL v1.2** — one declared version,
enforced as an enum rather than a free string, applied consistently across every
emitting path. This gives passthrough a precise meaning and makes the residue
property checkable.

v1.2 is chosen because it is the current released version and already what the
compiler emits for workflows. This is not a migration; it makes an existing de
facto choice explicit and brings the two stragglers into line. Declaring a
version is also what makes a future version bump a scoped task rather than
archaeology.

### 5.3 Layering

Three concerns, currently fused into one generated JSON Schema:

| Layer | Question | Environment-dependent | Artifact |
|---|---|---|---|
| **Syntax** | Is this well-formed Sophios? | No — this is specified | Typed AST + parser with source positions |
| **Resolution** | Do referenced steps exist here? | Yes | Resolution pass, "unknown tool" diagnostics |
| **Type checking** | Do port types line up? | Yes | Type pass |

Fusing them is why the schema is enormous, slow, unstable across environments,
and reports `None is not of type 'object'` instead of naming a file and line.
At baseline, Sophios validity depends on which plugins are installed, because the
schema enumerates every installed tool as a valid step name. That also makes the
existing fuzzer unusable as an oracle: it samples a random subset of an
environment-dependent schema.

The syntax layer is expressed as typed `dataclasses` — stdlib, not pydantic.
Pydantic coerces by default, and a compiler frontend needs exact,
position-aware rejection; it would also define the language in terms of a third
party's validation semantics. A JSON Schema is generated *from* the AST for
editor support, so there is one source of truth.

### 5.4 The leak boundary

Sophios leaks three ways:

1. **Sophios-owned syntax**, consumed and stripped before emitting CWL: `!&`, `!*`,
   `!ii`, `!cwl` (the tag is consumed; its expression is handed to CWL
   unresolved), and the `wic:` sidecar.
2. **Interpreted CWL**, read and acted upon: `scatter` and `when` inject
   `ScatterFeatureRequirement` / `InlineJavascriptRequirement`; an inline `run:`
   registers a tool.
3. **Passthrough CWL**, copied out untouched: `$namespaces`, `$schemas`, most of
   `requirements` / `hints`.

**The interpreted set (2) is enumerated exhaustively. Everything else is
passthrough by definition, and the residue after stripping Sophios-owned syntax
must be a valid CWL v1.2 document.** This yields two directly testable
properties and keeps existing files working.

### 5.5 The AST

A step-input value is currently a singleton dict with a magic key, dispatched by
a `match` whose cases sit 143 lines apart. The sum type already exists; it is
merely untyped. Made explicit:

| Surface | AST node | Meaning |
|---|---|---|
| `!ii v` | `InlineLiteral(v)` | Literal value, never an edge |
| `!& n` | `EdgeDef(n)` | Explicit edge definition site |
| `!* n` | `EdgeRef(n)` | Explicit edge call site |
| `!cwl e` | `RawCwlRef(e)` | **New.** Opaque CWL reference, passed through unresolved |
| bare `s` | `UnresolvedName(s)` | Must resolve to a workflow input, else diagnostic |

Every node carries a source span. That is what buys the diagnostics.

**`--allow_raw_cwl`** operates on step-input *values*, a different axis from the
key-level leak boundary. At baseline a bare string not found in `inputs:`
triggers `sys.exit(1)` unless the flag is set. That is global (one unresolvable
value disables the check across the whole workflow, including where it would
catch a real typo), it exits from library code, and its behaviour was never
pinned down.

The flag is retained with a one-line definition: **reinterpret every failing
`UnresolvedName` as `RawCwlRef`.** Behaviour is identical, plus a deprecation
warning naming every file and line where it was load-bearing. Because source
spans exist, a `--migrate` mode can insert the `!cwl` tags mechanically. Adding
`!cwl` is purely additive.

**The `wic:` sidecar** is unchanged on the surface, including its
`"(1, step_name)"` string keys. The AST normalises them to an `(index, name)`
pair on the way in.

> The grammar fixes the surface; the AST normalises the warts so they stop
> propagating.

### 5.6 Conformance corpus

`mm-workflows` and `image-workflows` are the integration and end-to-end corpus.
They run in CI against their live default branches: **whatever `main` says,
those are the tests, for better or worse.**

Grammar conformance runs against the same live checkouts — CI provisions both
repositories beside this one, and the corpus is discovered there, never copied
in and never pinned. The workflows stay in their own repositories. If upstream
moves and a file stops conforming, that is a signal to act on — fix the file
or fix the grammar — not drift to be insulated from.

We hold commit access to both corpora. Where a corpus `.wic` does not conform,
the default is to **improve the file** — several are stale and worth updating —
while avoiding breakage. Bending the language is the fallback, not the reflex,
because accommodating every exception erodes the specification one case at a
time. Any file that can be neither fixed nor accepted is excluded from the
conformance corpus with the exclusion documented.

### 5.7 Versioning

The language is specified and versioned, not frozen. Pinning without an
evolution path only defers the problem.

- **`lang_version` starts at 0.0.1**, defined against CWL v1.2. The Sophios
  version and its CWL substrate move together.
- **The tag is optional and expected to stay unused.** Downstream files are
  tagless.
- **Versioning is semantic and applied by human judgment**, not derived from
  diffs.

**Resolution.** An untagged file compiles at the **highest `lang_version` under
which that source actually compiles** — not the highest version shipped:

| Case | Resolution |
|---|---|
| Uses only long-standing syntax | Compiles under every version; resolves to the newest |
| Uses syntax a later version dropped | Resolves to the newest version that still accepts it; the file keeps working |
| Uses syntax only a newer version added | Fails under older versions, resolves to the newer one — no tag needed to adopt a feature |

A tag is required only where inference cannot read intent: pinning for
reproducibility, or genuine ambiguity where a construct is valid under two
versions with different meanings.

**Limit of the scheme.** "Compiles" is not "means the same thing". The rule
protects against syntactic divergence completely; it does not protect against a
change that leaves a file compiling under both versions while meaning something
different under each. The consequence is a rule for maintainers: **a change that
alters the meaning of existing valid syntax cannot be released as an inferable
version bump.** It requires either a construct that fails under the old version,
or an explicit tag requirement for affected files.

### 5.8 Surfacing and selecting the version

Version inference is invisible by construction. Two requirements follow, both
hard.

**The resolved version is always visible:**

| Where | Form |
|---|---|
| CLI | Reported on every compile, not only on failure |
| Python API | An attribute on `CompiledWorkflow` |
| Emitted artifact | A namespaced annotation, declared in `$namespaces` so output remains valid CWL v1.2 |

**The version is settable without editing anything** — a `--lang_version` CLI
flag and a matching `lang_version: str | None = None` parameter on the Python API
compile and run entry points. This reaches legacy `.wic` files and deeply nested
`Workflow` objects without touching either.

**Scope is the compilation, and this is enforced rather than recommended.** One
resolved version applies to the entire tree, including every nested subworkflow.
Inference over multiple sources means the highest version under which *all* of
them compile.

**There is no mechanism to set the version per file, per subworkflow, or per
`Workflow` object, and there must never be one.** A mixed-version compilation is
not discouraged — it is unrepresentable. Two versions in one tree would make a
construct's meaning depend on which file it lived in, and an edge crossing a
subworkflow boundary could connect ports governed by different language rules.
Anyone needing two versions runs two compilations.

**Precedence**, most immediate first: the explicit setting, then a per-file tag,
then inference. The explicit setting outranks a file tag so that "compile the
whole corpus as 0.0.1 and show what breaks" is expressible. Overriding a tagged
file emits a diagnostic naming it.

Resolution is bounded and cached rather than re-compiling against every known
version.

---

## 6. Spec 2 — Property suite and benchmarks

### 6.1 Generators

Strategies derive from the Spec 1 AST dataclasses, against a **synthetic tool
registry** of stub tools with known signatures. The suite is hermetic: no
installed plugins, no `search_paths_cwl`, no dependence on cached containers.
This is what lets failures shrink to a minimal reproducing workflow.

Generator adequacy is itself checked: every AST construct kind must appear
within a bounded sample. A generator that silently stopped producing a construct
would disable every property depending on it.

### 6.2 Properties

**Language level:** round-trip (`parse(render(ast)) == ast`); passthrough keys
survive byte-identically; residue validates as CWL v1.2; every corpus file
parses.

**Compiler semantics:**

- **Partition independence** — compiling a workflow equals compiling any
  partitioning of it, modulo namespacing. Generated workflows *and* generated
  partitionings.
- **Determinism** — identical output across `PYTHONHASHSEED`. A live hazard
  exists on the speculative-insertion path, where a `set` of strings is
  converted to a list; corpus workflows never reach that line.
- **Namespace injectivity** — distinct ports never collide after namespacing.
- **Edge soundness** — every inferred edge connects type-compatible ports.
- **Termination** — compilation reaches a fixed point or emits a diagnostic;
  `max_iters` is never silently exhausted.
- **Totality** — every failure is a diagnostic, never an unhandled exception.

**Canonical path** (Python API → Compute): compiled output validates under
`cwltool`; path agreement between `write_wic` → compile and direct compile;
compute payloads conform to their schema.

Every counterexample found is pinned as a permanent regression. A bug found once
must never be findable again by chance.

### 6.3 Benchmarks

Benchmarks run **weekly in CI, and are not gates.** They do not fail a build and
carry no thresholds. They exist to make the trend visible.

Running them in the CI matrix rather than on developer machines settles the
question of what baseline they are measured against: the runner is not fast, but
it is consistent, and consistency is the only property that makes week-over-week
numbers comparable. Coverage spans the corpus plus the speculative-insertion
path.

Accepted limit: a regression can sit for up to a week before it is visible, and
runner variance swamps small changes. Both are acceptable. The guard for
correctness is the property suite, which runs on every change.

---

## 7. Spec 3 — Typed IR and phase separation

### 7.1 The IR

Eight types — `Namespace`, `PortId`, `PortType`, `Port`, `StepNode`, `Edge`,
`DeferredObligation`, `OpaqueCwl` — composed into a `WorkflowGraph`.

`explicit_edge_defs`, `explicit_edge_calls`, `input_mapping`, and
`output_mapping` become **fields of `WorkflowGraph`**, because that is what they
always were. `DeferredObligation` gives deferred satisfaction a name rather than
an implicit convention. `OpaqueCwl` makes the leak boundary a type the compiler
is structurally incapable of reasoning past.

These name concepts the compiler already manipulates — namespacing, deferred
satisfaction, explicit edges, promotion of a linear step ordering to a
topological one. The IR gives them a representation instead of leaving them
implicit in dictionary manipulation and call-stack state.

### 7.2 Phases

| Phase | Transform | Environment-dependent |
|---|---|---|
| Parse | Sophios source → AST | No |
| Resolve | AST + registry → resolved AST | Yes |
| Lower | resolved AST → `WorkflowGraph` | No |
| Link | compose subgraphs; namespacing; LCA explicit edges; obligations | No |
| Infer | edge inference + speculative insertion | No |
| Emit | `WorkflowGraph` → CWL v1.2 | No |

Five of six are pure functions on typed data. With a graph to lower from, an
additional target becomes a new Emit implementation rather than a second path
through the compiler.

### 7.3 Migration

**Nothing lands as a big-bang branch.** Phases are extracted from the edges
inward — `Emit` first, then `Parse`/`Resolve` from the input side, leaving
`Link`/`Infer` last, when the safety net is thickest. Each extraction is its own
change against a working tree.

**Equivalence is demonstrated, not argued.** Old and new pipelines run side by
side and their outputs are compared on every generated input, expressed as a
property: *new ≡ old for all generated workflows*. A divergence blocks the
change; it is never triaged as acceptable without a recorded decision. The old
path is retired only after equivalence holds across the full generator and every
Spec 2 property passes against the new pipeline.

This is why Spec 2 precedes Spec 3. The other order is a rewrite with no oracle.

### 7.4 Performance

The fixed-point loop currently re-runs all of `compile_workflow_once`, including
AST merging, on every speculative insertion. Confining it to `Infer` over an
already-built graph should reduce the documented `O(2^n)` considerably.

This is a hypothesis to be measured, not a commitment. The governing performance
constraint is that it must not significantly regress; improvement is welcome but
is not the justification for this work.

---

## 8. Decisions and rejected alternatives

| Decision | Alternative | Rationale |
|---|---|---|
| Grammar before IR | IR first, grammar as documentation | The oracle needs a fixed vocabulary to state properties in. IR first means refactoring with no stable target. |
| Property tests before IR | Refactor first, test after | Without an oracle a rewrite is unverifiable. Differential testing is what makes Spec 3 verified rather than hopeful. |
| Verification is property-based | More example tests | Example tests find breakage someone already thought to write down. That is the blind spot being removed. |
| `lang_version` 0.0.1 on CWL v1.2 | Leave the version unspecified; target a later revision | Passthrough makes part of our meaning CWL's meaning, so an unspecified version specifies nothing. v1.2 is released and already emitted. |
| Version inferred as highest under which the source compiles | Highest shipped version; require the tag; default to 0.0.1 | "Highest shipped" silently reinterprets files a later version changed. Inference from what compiles is self-correcting and keeps downstream tagless. |
| Version setting global to the compilation | Per-file or per-`Workflow` override | A mixed-version tree makes meaning depend on file location and lets an edge join ports under different language rules. Unrepresentable is better than discouraged. |
| Typed AST via stdlib `dataclasses` | pydantic | Pydantic coerces by default; a frontend needs exact, position-aware rejection. It would also define the language in terms of a third party's validation semantics. |
| Generate JSON Schema from the AST | Hand-maintain both | One source of truth; grammar and implementation cannot drift. |
| Declared interpretation + valid CWL residue | Closed whitelist (unknown key = error) | The whitelist gives better diagnostics but rejects files relying on accidental passthrough, conflicting with §1. |
| Per-value `!cwl` tag | Keep only the global flag | The global flag disables the safety check everywhere for the sake of one value. A per-value tag is local, visible, and additive. |
| Fix stale corpus files by default | Bend the language per exception | Accommodating every exception erodes the specification one case at a time. |
| Physically separate `contrib/`; leave core in place | Symmetric `core/` + `contrib/`; or linter only | A visible boundary is noticed before it is crossed. Moving core would rename every public import path, which §1 forbids. |
| Contrib tests in core CI, zoned by directory | Separate schedule | A separate cadence means a separate cadence of attention. Zoning gives attribution without giving up coverage. |
| Weekly non-gating benchmarks in CI | On-demand local runs; a gating perf job | No laptop baseline is comparable; the CI matrix supplies consistency. Non-gating because an untriaged perf gate becomes noise or gets bypassed. |
| Four independently shippable specs | One large refactor | Work can stop after any one without leaving anything half-built. |

---

## 9. Order

Spec 0 is independent. Spec 1 precedes Spec 2, whose generators are built from
the Spec 1 AST. Spec 3 depends on Spec 2 entirely.

**Specification before oracle, oracle before surgery.** Each step is
independently valuable, the sequence is what makes the last one survivable, and
every step is subordinate to §1.
