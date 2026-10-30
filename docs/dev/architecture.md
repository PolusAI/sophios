# Architecture

How is a workflow turned into CWL inside the compiler, and which decisions are
made where? Compilation is a one-way typed pipeline from a parsed document to
an artifact tree. This page names the phases, the names the output carries,
how references are placed, how inference works inside, and what the adapters
around the compiler do.

## The pipeline

`compile_source(bundle, options)` in `sophios.compiler` is the one entry
point. It takes a `SourceBundle`, the parsed root document, the registry of
every parsed workflow and tool it reaches, and their version pins, plus
immutable settings, and returns a `CompilationResult`: the final graph, the
artifact tree, the diagnostics, the resolved language version and the
real-time declarations.

1. **Parse** (`sophios.lang`) turns YAML into the versioned AST. Unknown CWL
   stays opaque. The front door (`sophios.ir.frontdoor`) parses each `.wic`
   file once, follows the steps to the files they reach, registers inline
   `run:` bodies and `run:` paths, and builds the bundle. The Python API
   builds its bundle from the `Document` it constructs, with no parse.
2. **Resolve** selects the language version and pairs each step with its
   process (a tool, a workflow, an implementation, an inline body) from an
   immutable registry snapshot, applying `wic: steps:` contributions. It
   discovers nothing and parses nothing.
3. **Lower** builds an immutable `WorkflowGraph`: steps with complete port
   declarations, authored bindings, the workflow boundary, child graphs for
   subworkflows, and the real-time declarations. The compiler takes those out of
   the graph after the front end.
4. **Link** composes child graphs, places authored references at their
   lowest common ancestor, and discharges deferred obligations.
5. **Infer** adds the missing edges, and converter steps when asked. Its
   fixed-point loop is local to this phase; nothing before it is rerun.
6. **Declare namespaces** gives every workflow the `$namespaces` the `format`
   CURIEs of its promoted ports need, reading the registry as Resolve does,
   and reports `wic031` where a prefix is bound two ways by the sources that
   count.
7. **Emit** projects the final graph to CWL v1.2 documents and job inputs. It
   does not reopen source, consult registries, or revisit a decision.

An idempotent completion step, run before Link and again after Infer, materialises emission
facts already decided (boundary spellings, source strings); it looks nothing
up and decides nothing. The phases are tested one by one (`tests/core/test_resolve.py`,
`test_lower.py`, `test_link.py`, `test_infer_phase.py`, `test_emit.py`) and
together (`test_pipeline.py`).

## Names

During compilation a name is a value, never a string: a step occurrence is a
`StepId` (namespace, position, name), a port the compiler exposes is a
`DerivedName`, and a `step/port` reference is a `StepOutputRef`. Only Emit
spells them, through `sophios/ir/names.py`:

```text
{name}                 a step, its id less a `.wic`
{name}@{i}             every step whose id another step, input or output of
                       its document has (case ignored), i its 1-based position
{step}~{port}          a port exposed at a boundary
{outer}~{inner}        nesting (a flattened step, a drawing's node)
{root}~{address}.cwl   the file a step's process is written to, beside the root
```

A written name matches `[A-Za-z0-9_-][A-Za-z0-9_.-]*` (`sophios/lang/names.py`,
checked by Lower and by `Step` and `Workflow`, `wic045`), so it contains
neither `~` nor `@`: a made name, a numbered id and an authored name never
share a text, and the compiled CWL never holds one id twice. A root's name
contains no `~`, so two roots in one directory never share a file. The spelling
is one-way: nothing takes a name apart, and a consumer that needs to know what
a name came from is given the identity.

Subworkflows use relative namespacing: the parts a parent adds are dropped
inside the child, so a child's documents are the same wherever it is called
from. Graphviz needs unique node names, so the drawing uses whole namespaces
instead.

## References and obligations

An explicit edge has a definition site (`!&`, an output port identity) and a
use site (`!*`, an obligation on an input port identity). When a child
refers to a producer outside its scope, Lower records a `DeferredObligation`
rather than inventing a source string. Link owns the composed graph, so it
finds the definition at the lowest common ancestor, judges the endpoint types
with the versioned reference relation, and places the edge in exactly one
graph. An obligation still open at the root is `wic025`.

Workflow boundaries are graph facts: Link exposes whatever intermediate input
or output a child needs after composition, and Emit only spells it. An
omitted call argument stays omitted and remains available to Infer. A wrapper
output maps to its concrete producing port rather than becoming a second
producer.

The reference relation (`sophios.lang.compatibility`) rejects a reference
only when it proves the endpoint types `DISJOINT`; `UNKNOWN`, which covers
`Any`, named schemas and opaque records and enums, is deferred to the
runner's validation of the emitted CWL. Scatter contributes its array rank
on both endpoints.

## Inference

Infer reads a complete graph and an explicit `InferencePolicy` (disabled,
naming conventions and their renames, format rules, insertion, strictness,
the iteration limit). For each required unbound input it scans the earlier
steps backwards, outputs last-declared first, and takes the first match by
`types_match` and the format rule, recording the alternatives so that a
`wic042` or `wic043` note can say what lost. `types_match` is a candidate
heuristic and has no authority to reject an authored reference; that is the
relation above.

Speculative insertion is a constrained search: when no output matches an
input by format, the catalog of tools named `insert_steps_automatically_*`
is searched for one that accepts a format seen and produces the format
wanted, and one synthesized `StepNode` is inserted. The grown graph is
inferred again in the next iteration; no document is rewritten and nothing is
recompiled from scratch. The loop transforms immutable graphs, so a failed
speculation cannot leak into another compilation, and exhausting the limit is
`wic022`.

A linear step order is a total order, and the compiler's job is to promote it
to the topological order the DAG needs by adding edges. More than one order
can be plausible, which is why every choice between equals is reported.

## Post-compile rewrites

`sophios.post_compile` rewrites the emitted artifact tree after compilation,
never the source:

- `flatten_subworkflows` (`--cwl_inline_subworkflows`) changes the shape: it
  replaces a call by the child's steps when the call carries only `in` and
  `out` and the child allows it, and leaves the root's inputs, outputs and job
  inputs alone;
- `inline_artifact_runs` (`--cwl_inline_runtag`, `Workflow.compile()` and
  `Workflow.run()`, the REST route) changes the packaging: every `run:`
  carries its process, and a v1.0 process keeps the defaults it had as a file
  through two hints, as cwltool's updater adds them;
- `cwl_update_outputs_optional_artifact` and the `when:` Emit writes under
  `partial_failure` implement `--partial_failure_enable`.

Neither packaging option is a second implementation of workflow-call
semantics.

Each process an artifact carries is a description, never data. When
`compiler._artifact_tree` emits a step, a tool whose text refers to a file
relative to itself (`$include`, `$import`, a relative `location`) is packed by
cwltool where it was read (`utils_cwl.packed`), once per tool per compile; a
pack error is `wic013` at the step. Inference and Link have already read the
tool as written. Lower writes a relative `location` in a `.wic` `default:`
absolute against the `.wic`'s directory, and the Python adapter does the same
for a `StepInput` default against the working directory.

## Around the compiler

- `sophios.main` is the command-line adapter: it parses arguments once,
  converts them to settings at the boundary (`cli.get_dicts_for_compilation`),
  loads the registry through `plugins`, runs the compile, the rewrites, the
  drawing, the pre-flight and the run, and converts a `SophiosError` to
  diagnostics and exit code 1. Nothing below it sees an argparse namespace.
- `sophios.api.python._workflow_runtime` is the Python API's adapter: it
  builds the `Document` and the registry from `Step` and `Workflow` objects,
  calls `compile_source`, and runs the one document `compile()` returns
  through the same `run_setup` and `run_local` as the command line. The import-linter contract keeps
  `workflow.py` and `tool_builder.py` from reaching the compiler any other
  way.
- `sophios.run_setup.prepare` is the one path from an artifact to a run, for
  both adapters and the CI harness, through the backend's `prepare`: it binds
  and writes the job, writes the workflow and its analyses, and calls the
  pre-flight. The command line gives it the tree, or
  one document under `--cwl_inline_runtag`; Python gives it one document.
- `sophios.preflight` answers what a local run needs from this machine, with
  one diagnostic per problem, and pulls images; it is called by both
  adapters and never by a compute submission.
- `sophios.backends` describes a run once, as a `RunRequest`, which the
  command line, the Python API and the CI harness each build. The backend for
  its target and runner (`BACKENDS`, keyed by `('cwl', 'cwltool')` and
  `('cwl', 'toil-cwl-runner')`) answers four questions and starts nothing:
  `check` (the pre-flight), `prepare` (`run_setup.prepare`), `launch` (the
  runner's argv, environment and working directory; `--provenance` unless
  `no_provenance`) and `outcome` (after exit: the output object and the
  failed steps, read from the runner's log lines).
- `sophios.run_local` is the one path that runs a launch: always as a child
  process in its own session, whose two streams it echoes line by line and
  tees into `<basepath>/run_<workflow>.log`. `start` returns the
  `RunHandle` (`wait`, `collect`, `status`, `logs`, `outputs`, `cancel`) that
  `Workflow.start()` hands out, which lives as long as the Python process;
  `run` is start and wait, or writes `run.sh`. `wait` returns the run's
  status however it ended; `collect` waits, and returns its result or raises
  `WorkflowRunError`, and `Workflow.run()` is `start().collect()`. Ctrl-C
  during `wait` is `cancel`: SIGTERM to the runner, then a kill after a grace
  period. `sophios.run_status` holds the states, status and result it
  reports, those of the compute service's contract, and the error `collect`
  raises.
  `Workflow.run_async()` prepares in a worker thread, then awaits `wait` from
  another, and cancels the handle when its task is cancelled. `sophios.realtime` compiles each declaration once and runs a
  `Watcher` per plan beside the runner.
- `sophios.compute` sends a workflow to the compute service, the cluster's
  slurm driver. It compiles through the runtime adapter's `service_document`,
  which is `compile()` without the local run's rewrite of output folders,
  checks the request against its copy of the service's
  `run-request.schema.json`, and follows the run over HTTP on the service's
  Unix socket, reached through the user's own `ssh`. It is not part of the
  Python API, and never goes through the pre-flight.

## Compatibility contract

The compiler promises behaviourally identical workflows, not byte-identical
documents. The end-to-end contract is `UP_TO_EMBEDDING`
(`tests/core/equivalence.py`): generated `run:` paths may move when artifacts
are embedded differently, while structure, bindings, ports, types,
requirements and opaque payloads are equivalent. Partition independence is
checked at `UP_TO_RENAMING`: splitting a workflow into subworkflows renames
derived ports and moves no edge. Narrower phase properties assert `IDENTICAL`
where exact projection or determinism is the behaviour being specified.
