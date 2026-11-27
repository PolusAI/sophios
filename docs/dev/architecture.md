# Architecture

How is a workflow turned into CWL inside the compiler, and which decisions are
made where? Compilation is a one-way typed pipeline from a parsed document to
its graph and the tools its steps run. This page names the phases, the names the output carries,
how references are placed, how inference works inside, and what the adapters
around the compiler do.

## The pipeline

`compile_source(bundle, settings)` in `sophios.compiler` is the one entry
point. It takes a `SourceBundle`, the parsed root document, the registry of
every parsed workflow and tool it reaches, and their version pins, plus
its `CompileSettings`, and returns a `CompilationResult`: the final graph, the
tools each step runs, the diagnostics, the resolved language version and the
real-time declarations.

1. **Parse** (`sophios.lang`) turns YAML into the versioned AST. Unknown CWL
   stays opaque. A document with `command:` or `script:` is read as a
   `ToolDocument`, a place for each word and port, and lowered once, by
   `lang/commandline.py`, to the `CommandLineTool` it means; the parser keeps
   that CWL on the node. The front door (`sophios.ir.frontdoor`) parses each `.wic`
   file once, follows the steps to the files they reach, registers inline
   `run:` bodies and `run:` paths, and builds the bundle. A tool root becomes a
   workflow of one step that runs it. The Python API
   builds its bundle from the `Document` it constructs, with no parse.
2. **Resolve** pairs each step with its process (a tool, a workflow, an
   implementation, an inline body) from an immutable registry snapshot, applying `wic: steps:` contributions. It
   discovers nothing and parses nothing. Each document it reaches, its contributions applied, first passes
   **Check** (`sophios.ir.check`): the rules of its own nodes (step ids, names, one producer per edge name, the
   shape of `scatter:` and of workflow outputs), stated once for a parsed document and a Python-built one alike.
   Resolve looks nothing up for a document Check has not made a `CheckedDocument` of, so a bad step id is reported
   as itself, never as a process that is absent, and the phases after it assume those rules. A registry `Tool` reads its one file
   the first time anything asks, a `.cwl` through the CWL loader and a `.wic`
   through `parse()` and its lowering, so every consumer sees one kind of
   tool, and a registry key is fixed once, when discovery finds the file (a
   `.cwl` beside a `.wic` of one stem keeps the stem). The compiler reads the
   version the tools need after the front end, `wic060` if the pin is lower.
3. **Lower** builds an immutable `WorkflowGraph`: steps with complete port
   declarations, authored bindings, the workflow boundary, child graphs for
   subworkflows, and the real-time declarations. The compiler takes those out of
   the graph after the front end.
4. **Link** composes child graphs, places authored references at their
   lowest common ancestor, and discharges deferred obligations.
5. **Infer** adds the missing edges, and converter steps when asked, in one pass.
6. **Declare namespaces** gives every workflow the `$namespaces` the `format`
   CURIEs of its promoted ports need, reading the registry as Resolve does,
   and reports `wic031` where a prefix is bound two ways by the sources that
   count.
7. **Emit** projects the resolved view of the final graph to CWL v1.2 documents and job inputs. The
   CWL target runs it after the compile (`cwl.emit.artifact_tree`); a compile
   emits for no target. It does not reopen source, consult registries, or revisit a decision.

A completion step, run once before Link, gives each workflow the boundary its
steps already determine (an input for each written value, an output for each
step output); it looks nothing up and decides nothing, and Infer keeps that
boundary current for what it adds. The phases are tested one by one (`tests/core/test_resolve.py`,
`test_lower.py`, `test_infer_phase.py`), together by the laws of `test_emit.py`, and
on example workflows in files (`tests/core/data/examples`, `tests/core/data/compiler`).

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
checked by `sophios.ir.check` before Resolve, and by `Step` and `Workflow`,
`wic045`), so it contains
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
with the reference relation, and places the edge in exactly one
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
naming conventions and their renames, format rules, insertion, strictness). For each required unbound input it
scans the earlier steps backwards and takes, in the most recent step with a match, the first declared match by
`types_match` and the format rule, recording the alternatives so that a
`wic043` note can count what else fitted. `types_match` is a candidate
heuristic and has no authority to reject an authored reference; that is the
relation above.

Insertion adds an ordinary step: when no output matches an input, the tools
named `insert_steps_automatically_*` are read, and one that has an output
fitting the input and a required input of its own fitting an output already
upstream (the relation the backward scan uses) is inserted before the step,
wired by that same scan; the job gives what the scan does not find. Inference looks only backwards, so the step
after the insertion is read again and nothing decided before it changes: one
pass, no loop. A converter reads the authored steps only, never another converter. Two such converters are a
guess, reported as `wic043`.

A linear step order is a total order, and the compiler's job is to promote it
to the topological order the DAG needs by adding edges. More than one order
can be plausible, which is why every choice between equals is reported.

## Post-compile rewrites

`sophios.cwl.post_compile` rewrites the emitted artifact tree after compilation,
never the source:

- `flatten_subworkflows` (`--cwl_inline_subworkflows`) changes the shape: it
  replaces a call by the child's steps when the call carries only `in` and
  `out` and the child allows it, and leaves the root's inputs, outputs and job
  inputs alone;
- `inline_artifact_runs` (`--cwl_inline_runtag`, `Workflow.compile()` and
  `Workflow.run()`, the REST route) changes the packaging: every `run:`
  carries its process, and a v1.0 process keeps the defaults it had as a file
  through two hints, as cwltool's updater adds them.

Neither packaging option is a second implementation of workflow-call
semantics.

Partial failure is no rewrite. `--partial_failure_enable` and its success
codes are compile input, a `RunPolicy` (`sophios.ir.policy`), and
`cwl.emit.artifact_tree`, run by the CWL target, writes for it: each step's `when` through
`effective_when` and each tool through `tolerant_tool`. A real-time
analysis, compiled under the same options, is written for it too.

Each process an artifact carries is a description, never data. When
the compile reads a step's tool, a tool whose text refers to a file
relative to itself (`$include`, `$import`, a relative `location`) is packed by
cwltool where it was read (`cwl.tools.packed`), once per tool per compile; a
tool cwltool cannot read is `wic057` at the step, before it is packed. Inference and Link have already read
the tool as written. Lower writes a relative `location` in a `.wic` `default:`
absolute against the `.wic`'s directory, and the Python adapter does the same
for a `StepInput` default against the working directory.

## Around the compiler

- `sophios.main` is the command-line adapter: it parses arguments once,
  converts them to settings at the boundary (`cli.settings_from_args`),
  loads the registry through `plugins`, runs the compile, the rewrites, the
  drawing, the pre-flight and the run, and converts a `SophiosError` to
  diagnostics and exit code 1. Nothing below it sees an argparse namespace.
- `sophios.api.python._workflow_runtime` is the Python API's adapter: it
  builds the `Document` and the registry from `Step` and `Workflow` objects,
  calls `compile_source`, and runs the one document `compile()` returns
  through the same `runtime.setup` and `runtime.local` as the command line. The import-linter contract keeps
  `workflow.py` and `tool_builder.py` from reaching the compiler any other
  way.
- `sophios.runtime.setup.prepare` is the one path from an artifact to a run, for
  both adapters and the CI harness, through the backend's `prepare`: it binds
  and writes the job, writes the workflow and its analyses, and calls the
  pre-flight. The command line gives it the tree, or
  one document under `--cwl_inline_runtag`; Python gives it one document.
- `sophios.runtime.preflight` answers what a local run needs from this machine, with
  one diagnostic per problem, and pulls images; it is called by both
  adapters and never by a compute submission.
- `sophios.runtime.runners` is the one value of a local run: `Cwltool`, `Toil` and
  `Nextflow` hold the flags their program is given and the settings Sophios
  reads for it (`quiet` and `cachedir` are cwltool's alone; Nextflow's one
  setting is its `container_engine`, which the pipeline is written for), and a
  `RunSettings` is a runner with what is given to the job (`outdir`,
  `inputs_file`, `generate_run_script`). The command line (`cli.run_settings_from_args`) and
  the Python API (`Workflow.run(runner=..., config=...)`) build the same value,
  and the deprecated dictionary form of `run()` is translated onto it in one
  function. The parser reads its defaults and choices from it. It also holds
  `RunConfig` and `SlurmSettings`, the one configuration a local run, a site
  and the compute service take, and `read_config`, which every door reads a config
  with and where the rules of its outputs folder and `relative_to` live; the
  Python API resolves a config's folder into `RunSettings.outdir` once.
- `sophios.runtime.backends` describes a run once, as a `RunRequest` that carries the
  `RunSettings`, which the command line, the Python API and the CI harness
  each build. The backend of its runner (`targets.backend`) describes the
  run and starts nothing; only `prepare` acts, on this machine: `write` (the
  runner's files under a folder, and the folders its machine must make),
  `needs` (the paths the jobs name and the images, which the pre-flight
  judges), `prepare` (`write`, then the pre-flight, the pulls and the
  folders), `launch` (the runner's argv, the variables it adds and its
  working directory, from the request alone; `--provenance` unless the
  runner's `provenance` is off) and `outcome` (after exit: the output object
  and the failed steps, read from what the runner printed and left, writing
  nothing), so a site can run the same launch on another machine, as the
  runner's `name` in a folder of its own (`design_docs/sites-design.md`).
- `sophios.runtime.local` is the one path that runs a launch: `launch` places it
  on this machine (absolute paths, the default output directory, this
  process's environment under the run's), and it runs always as a child
  process in its own session, whose two streams it echoes line by line and
  tees into `<basepath>/run_<workflow>.log`, and writes
  `output_<workflow>.json` for a runner that prints its output object rather
  than writing the file (Toil, Nextflow; cwltool writes its own,
  `Backend.writes_summary`). `start` returns the
  `RunHandle` (`wait`, `collect`, `status`, `logs`, `outputs`, `cancel`) that
  `Workflow.start()` hands out, which lives as long as the Python process;
  `run` is start and wait, or writes `run.sh`. `wait` returns the run's
  status however it ended; `collect` waits, and returns its result or raises
  `WorkflowRunError`, and `Workflow.run()` is `start().collect()`. Ctrl-C
  during `wait` is `cancel`: SIGTERM to the runner, then a kill after a grace
  period. `sophios.runtime.status` holds the states, status and result it
  reports, those of the compute service's contract, and the error `collect`
  raises.
  `Workflow.run_async()` prepares in a worker thread, then awaits `wait` from
  another, and cancels the handle when its task is cancelled. `sophios.runtime.realtime` compiles each declaration once and runs a
  `Watcher` per plan beside the runner.
- `sophios.compute` sends a workflow to the compute service, the cluster's
  slurm driver. It compiles through the runtime adapter's `service_document`,
  which is `compile()` without the local run's Directory literals for output
  folders, or, for a Nextflow submission, its `service_pipeline`, the
  pipeline written for Singularity without the record of its outputs
  (`render_nextflow(model, engine='singularity', record=False)`), reads the
  `RunConfig` with `read_config` and maps it onto the request (`SLURM_FIELDS`
  for its `SlurmSettings`), checks the request against its copy of the
  service's `run-request.schema.json`, its `SlurmSettings` through
  `slurm_problems`, which a Slurm site shares, and follows the run over HTTP
  on the service's Unix socket, reached through the user's own `ssh`. The
  Python API does not import it, and it never goes through the pre-flight.
- `sophios.sites` runs a workflow on another computer, a machine or a Slurm
  cluster, over the user's own `ssh` (`design_docs/sites-design.md`). It
  reads the `RunConfig` with `read_config` as the compute service does, and
  refuses itself what it leaves to the service (`_site.site_config`);
  compiles and lowers through `targets`, the workflow's output folders made
  in the run; builds the `RunSettings` of the runner on that computer, its
  engine the site's, and calls the backend's `write` into a local folder and
  its `needs`; judges them there in one call; copies the files into a folder
  of the run there; and starts the backend's `launch` from `bin` in a job
  script that records how it ended and empties the outputs folder of a run
  that did not succeed. `status` reads the folder, on Slurm then Slurm's
  `squeue` and `sacct`, and the backend's `outcome` reads what the run left.
  A package: `_ssh` (the transport and the shell functions), `_states` (a
  run's state from its folder, and on Slurm its jobs), `_runners` (what a
  site reads from a runner value and what each kind of site adds to its
  command line, or to a Nextflow run's configuration: on Slurm each task a
  job of its own), `_site` (the base class, whose scheduler hooks a
  `Machine` fills, and `Machine`), `_slurm` (`Slurm`, which fills them for
  a cluster: where a run's jobs go, Toil's steps and Nextflow's tasks by one
  placement, and its leader's header) and `_run`
  (`Run`). The Python API does not import it, and it never goes through
  the pre-flight.

## Compatibility contract

The compiler promises behaviourally identical workflows, not byte-identical
documents. The end-to-end contract is `UP_TO_EMBEDDING`
(`tests/core/equivalence.py`): generated `run:` paths may move when artifacts
are embedded differently, while structure, bindings, ports, types,
requirements and opaque payloads are equivalent. Partition independence is
checked at `UP_TO_RENAMING`: splitting a workflow into subworkflows renames
derived ports and moves no edge. Narrower phase properties assert `IDENTICAL`
where exact projection or determinism is the behaviour being specified.
