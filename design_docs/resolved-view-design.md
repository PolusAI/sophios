# Sophios Resolved View — Design

**Status:** Approved. Locked; a commit that changes this file has a subject beginning with `design:`.
**Baseline:** `54d8aab4`. Every `file:line` below is at that commit, under `src/sophios/` unless it starts
with `design_docs/`, `docs/`, `tests/` or `.github/`. cwltool is cited from the installed
3.1.20260108082145 as `cwltool/<file>`, cwl-utils from 0.36 as `cwl_utils/<file>`, and CWL v1.2 from the copy
cwltool ships, `cwltool/schemas/v1.2/<file>`.
**Scope:** four PRs, P, A, B and C, in that order (§12). The Nextflow backend's own contract is
`design_docs/sophios_nextflow_backend.md`; this document says what it reads.

---

## 1. What this is

The Nextflow target reads the CWL Sophios wrote, not the graph Sophios built. `compilation_result_source`
starts from a deep copy of the emitted workflow (`utils_nf.py:1763`) and returns CWL dicts
(`utils_nf.py:1717-1803`). Every later pass reads CWL fields back out of them: `_condition_value` reads
`step["when"]` and types it against the tool's `inputs` (`utils_nf.py:2985-3013`), and the closed-world
checks walk `Workflow`, `WorkflowStep` and `WorkflowStepInput` fields (`utils_nf.py:1806-1892`,
`:2124-2230`). A refusal therefore names a CWL path nobody wrote. Compiling `docs/cookbook/when.wic` with
`--target nextflow` today prints

```text
Nextflow capability analysis failed:
- steps[0].in.enabled: the step's when reads an input its tool does not declare, which is not supported
- workflow.inputs.loud: required workflow input value is missing
```

although the graph holds the binding's position, `when.wic:9:16`.

The **resolved view** is a read-only reading of the compiled graph, `WorkflowGraph` (`ir/types.py:444-480`).
It resolves and parses what the graph already says and nothing more: each step input's sources at its level,
its record fields and the type it delivers, the parsed and typed `when`, `scatter`, workflow inputs and
outputs, each tool step's inherited requirements, and the run policy. Five rules govern it.

1. **Nothing in the view has a meaning of its own.** A construct Sophios took from CWL unchanged means what
   CWL v1.2 says, and cwltool is the reference to test against. What Sophios invented (edge inference, the
   names it makes with `~` and `@`, anchors `!&`/`!*`) is resolved before the view exists; the view holds only
   the result, as references.
2. **The CWL target is unchanged.** The CWL target's output, the inliner behind `--cwl_inline_subworkflows`
   (`post_compile.flatten_subworkflows`, `post_compile.py:333-405`) and every CWL rewriting flag
   (`main.py:125-126`) are unchanged by this design, with one exception. Where today's output contradicts what
   the author wrote, P fixes it: partial failure (§2). Otherwise emission stays on the graph, and the view is
   never written as CWL. The inliner asks whether a CWL document can be rewritten without changing its meaning;
   the Nextflow target asks how to express a nested call at all. They are different questions and share no
   rule.
3. **The Nextflow target reads the view, never emitted CWL.** Tools stay CWL: the view carries each tool's
   document, and a target reads it itself. Tool lowering does not change.
4. **A refusal is at the user's line.** It names the file and line, the step and the construct (§7).
5. **Additive.** No Python API, command-line or `.wic` change other than additions. Internal signatures may
   change. No compatibility shims.

Out of scope: drawing stays on the graph; plain CWL into the view is a separate item; nested-input defaults
and conditional or scattered calls stay refused on Nextflow, as today.

## 2. Partial failure as a policy (PR P)

Today `--partial_failure_enable` is two rewrites of emitted CWL, and each one writes output that contradicts
what the author wrote.

1. **The `when` is overwritten.** `emit._step_spelling` replaces a step's `when` with null checks of its
   required inputs (`ir/emit.py:47-51`). Compiled with the flag, `docs/cookbook/when.wic`'s
   `when: $(inputs.enabled)` becomes `$(inputs["message"] != null && inputs["enabled"] != null)`; cwltool then
   runs `echo` with `loud` false and prints `Hello`. The checks also count `enabled`, an input `echo.cwl` never
   receives, because a step-only input's port has no declaration (`ir/lower.py:240`) and `required(None)` is
   true (`ir/declarations.py:41-45`).
2. **The tools are rewritten after the compile,** on the command line's CWL path only (`main.py:415-418`,
   `plugins.py:186-213`, `:269-279`). Checked by calling the rewrite:
   - `successCodes` is replaced, not merged: a tool declaring `[0, 3]` gets `[0, 1]` (`plugins.py:208`).
   - Only an output whose type is one string becomes optional (`plugins.py:210-212`). `{type: array, items:
     File}`, a union without `null` and a record stay required.
   - `type: stdout` becomes `stdout?`, which cwltool refuses as not a valid Avro schema.
   - An output written as its type alone (`said: stdout`) raises `TypeError`, and outputs written as a list (a
     packed tool's) raise `AttributeError` (`plugins.py:210`).
   - A real-time analysis compiles under the same options (`realtime.py:71`), so its steps get the null checks,
     but its tools never get the tool changes, which only the root's tree receives (`main.py:415-418`).

P makes partial failure a **policy**: compile input, lowered in one place at emission. Every fix comes with a
data case that fails at the baseline for its own reason.

`sophios.ir.policy` holds `RunPolicy(partial_failure, success_codes)`, its codes sorted, unrepeated and holding
0, and the two functions that lower it, `effective_when` and `tolerant_tool`.

- **Input.** The codes are computed once from the two success-code flags with today's expression, 0, the codes
  given and `range(lo, hi)`; A records the policy as `CompilationResult.policy`, where the view reads it (§4).
- **Lowered at emission, in one place.** `_artifact_tree` writes `effective_when` and applies `tolerant_tool`
  to each tool document. Nothing rewrites the artifact after the compile, so a real-time analysis gets it too.

**`effective_when`.** Let G be `inputs["<name>"] != null` for each input that the step's tool or nested workflow
declares and `required` counts, joined by ` && `, as `ir/emit.py:48-51` spells it. A step-only input is not in
G. The change is local to G: `required` keeps its meaning. Its only other caller, inference
(`ir/infer.py:159`), never reaches a step-only port, since such a port exists only where a record binds it
(`ir/lower.py:202-206`, `:236-241`) and so is already bound (`ir/infer.py:279-285`).

With no partial failure, or an empty G, the result is the authored `when` as written, or none. Otherwise, with
T the authored `when` stripped of surrounding whitespace, as cwltool strips it (`cwl_utils/expression.py:169-190`):
- **No authored `when`:** `$(G)`, today's text exactly.
- **T is one `$(E)`,** that is, `cwl_utils.expression.scanner` spans all of T: `$(G && (E))`.
- **T is one `${B}`:** `${if (!(G)) { return false; }B}`.
- **Anything else, a `when` that is not a string included:** the authored `when` as written, without G. cwltool
  evaluates it as text, or schema validation refuses it, and either way it fails as a `when`
  (`cwltool/workflow_job.py:690-694`).

E and B are the author's text verbatim, and each appears once, so the author's condition is evaluated at most
once. The null checks come first because `&&` and the early `return` evaluate the authored condition only
after every required input has arrived. An authored condition may read into an input, as in
`inputs.reads.size > 0`. Evaluated on a `null`, it throws and fails the step; partial failure exists to skip
that step instead.

| Step | Authored `when` | Written under the policy |
|---|---|---|
| `echo` of `docs/tutorials/test_rand_fail.wic` | none | `$(inputs["message"] != null)`, as today (`docs/partial-failures.md:99`) |
| `echo` of `docs/cookbook/when.wic` | `$(inputs.enabled)` | `$(inputs["message"] != null && (inputs.enabled))` |
| the same, written as a body | `${ return inputs.enabled; }` | `${if (!(inputs["message"] != null)) { return false; } return inputs.enabled; }` |

**`tolerant_tool`.** With partial failure set, it changes two things in a tool document, and nothing without it.
- **`successCodes`:** the tool's own codes and the policy's, sorted, without repeats.
- **Each output's type is made to accept `null`.** This holds whether the outputs are written as a mapping or as
  the list a packed tool has, and whether each one is written as its type alone or as a mapping:
  - a string `T` gains `?` unless it ends in `?`, as today;
  - a list, which is a union, gains `'null'` first unless it holds it;
  - a mapping (an array, record or enum written in full) becomes `['null', <the mapping>]`;
  - `stdout` and `stderr` become `File?`, with an `outputBinding` that globs the stream's file. That file is
    the tool's own `stdout` (or `stderr`) when written; otherwise P writes there the sha1 hex digest of the tool
    document, as cwltool names an unnamed stream (`cwltool/load_tool.py:161-178`).

**Byte identity.** In the byte harness's partial-failure modes (§12) every gate-set workflow writes the same
bytes but for three changes: an authored `when` gains G before it; an array output written in full becomes
`['null', {type: array, ...}]`; and a real-time analysis's tool gains the codes and optional outputs. The
harness compiles the fragments CI skips, the cookbook's `when` and `greeting_if`, too.

**What P leaves as it is.**
- the inliner keeps a call that carries the policy's condition nested, as today (`post_compile.py:201-208`);
- partial failure is still absent from the Python API, which C adds (§10);
- `--target nextflow` with any of the flags stays the command line's error (`cli.py:198-207`).

## 3. The types

`sophios.ir.resolved`. Frozen and slotted like the graph's types. Verbatim CWL is `OpaqueCwl`, as in
`ir/types.py`, and no invariant depends on its contents; a target that normalizes a document works on its own
copy, as `utils_nf.py:1799-1801` already does.

What the representation decides; `ir/resolved.py` holds the fields:
- A reference is level-local: `InputRef(name)` names an input of the same workflow, `OutputRef(step, port)` one
  of its steps and one of that step's outputs, each spelled by `Names`.
- A workflow holds its inputs in the order the CWL target writes them, the outputs kept (§4), its steps in graph
  order, and the compilation's `RunPolicy`, the same object at every depth.
- A step's `run` is a `ResolvedTool`, the tool's file and the document the CWL target writes for it, or a
  `ResolvedWorkflow`.
- A `StepInput` keeps `linkMerge`, `pickValue`, `default` and `valueFrom` as written. `declared is None` marks
  a step-only input, and `type` is the type it delivers (§5).
- A `Condition` keeps the authored `when` with its subset tree, the tree's type and the subset's problem; the
  policy's checks are never folded in.
- `fields` keep every other key verbatim, a `scatterMethod` with no `scatter` among them.
- `WorkflowInput.made_from` names the step and input whose written value a made input holds.

`Expr` and the subset's parser and type checker move from `nf_expr` to `sophios.lang.subset`; the Groovy
rendering and reading stay in `nf_expr` (`nf_expr.py:393-626`). PR B adds `ResolvedStep.inherited`, a tool
step's requirements from outside its tool (§8), with the inheritance that computes it.

Invariants, checked where they are cheap (`__post_init__`) and otherwise by the builder:

- Every graph step is one `ResolvedStep`: `run` is a `ResolvedWorkflow` exactly when its `ProcessRun.child` is
  set (`ir/types.py:267-280`).
- Every `Ref` names something of its own workflow: an `InputRef` one of its `inputs`, an `OutputRef` one of its
  steps and one of that step's `outputs`.
- A step's inputs are in the order the CWL target writes its `in:`. A call's inputs are exactly its workflow's
  inputs: Sophios gives a call no step-only input (`ir/lower.py:202-205`, `wic028`).
- `scatter.inputs` names step inputs. `condition.expr is None` exactly when `condition.problem` says why;
  `condition.type` is set only when the tree types.
- Spans are the graph's: a binding's is its value's (`lang/nodes.py:150-161`), a step's is its node's, and a
  `when` or `scatter` takes the step's, since neither has one of its own (`lang/nodes.py:244-259`). In
  `docs/cookbook/when.wic` the step is at `when.wic:5:3` and `enabled`'s binding at `when.wic:9:16`; in
  `when.py` the step is the `Step(...)` call at line 13 and the binding at line 16
  (`api/python/workflow.py:251`, `:606`).

## 4. The builder

`resolve(graph, tools, *, policy, values, outputs)` builds the view:
- `graph` is a finished compilation's `CompilationResult.graph`: linked, inferred and completed.
- `tools` holds every tool step at any depth: the document the CWL target writes for it, its child artifact.
- `policy` is the compilation's (§2).
- `values` are this run's job values by root input, as the job spells it, each replacing the graph's binding;
  `None` keeps the graph's. A key naming no root input is a `ValueError`, the caller's mistake, not the user's.
- `outputs` names the root outputs to keep, `None` all of them; a name naming none is a `ValueError` too.

`compiler.resolved_view(result, *, values=None, outputs=None)` pairs each tool step with its child artifact and
calls `resolve` with `result.policy`, so nothing below `compiler` reads the artifact tree.

Each step input's sources come from `stepin.step_inputs`, the level-local resolution CWL emission spells, so a
source in the view is what the emitted `in:` says, by construction.

It never reads a workflow's emitted CWL and never imports `ir.emit` (§11). It refuses nothing: refusing is a
target's job (§7).

**Who passes what.** The Python API passes its bound values and declared outputs as `values` and `outputs`
instead of editing CWL dicts. The run backend lowers `Settings.view`, where `None` is the compilation's own
view. The command line passes neither.

## 5. What each construct means

| Construct | In the view | Meaning | Defined by |
|---|---|---|---|
| Declared step input | `StepInput`, `declared` set | Bound by the step and passed to the tool. | CWL v1.2 `WorkflowStepInput`, `Workflow.yml:295-306` |
| Step-only input | `declared is None` | Bound by the step; the tool never receives it. `when`, `scatter` and `valueFrom` see it. The step starts only after its sources have values. | `Workflow.yml:302-306`; cwltool starts the step after its source's step completes (below) |
| `source`, `linkMerge` | `sources`, `link_merge` | Sources in written order; two or more merge as `merge_nested` unless written otherwise. | `Workflow.yml:230-236`, `:328-330` |
| `pickValue` | `pick_value` | After `linkMerge`, before `scatter` and `valueFrom`. | `Workflow.yml:350-365` |
| `default` | `default`, `has_default` | The value when there is no source or the source gives `null`; applied before `scatter` and `valueFrom`. | `Workflow.yml:410-416` |
| `valueFrom` | `value_from` | As written. | `Workflow.yml:420-447` |
| Delivered type | `StepInput.type` | The type of what the step input holds after its sources, `linkMerge`, `pickValue` and `default` (below). | CWL v1.2; the merge, pick and scatter layers are Link's `_delivered` and `_rank` (`ir/link.py:312-336`, `:354-369`), which move where Link and the view both call them |
| `when` | `Condition` | Evaluated per scatter job, after the inputs are bound, over every step input; must give `true` or `false`; a skipped step's outputs are `null`. | `Workflow.yml:527-545`; `cwltool/workflow_job.py:676-694` |
| `scatter`, `scatterMethod` | `Scatter` | As CWL. | `Workflow.yml:505-523` |
| Workflow input `default` | `declaration` | The value when the input is missing or `null`. | `Process.yml:757-765` |
| Nested workflow | `run: ResolvedWorkflow` | A step whose inputs are its workflow's inputs and whose outputs are its workflow's outputs. | `Workflow.yml:552-559` |
| Requirements and hints | `requirements`, `inherited` | §8. | `cwltool/workflow.py:202-223` |
| Partial failure | `policy` | Sophios's own: a step runs only when every declared input `required` counts has a value and its authored `when` holds (§2). | This design |

**A source's type** is its producer's, resolved through call boundaries (§6):
- a root workflow input's declared type;
- or a tool step's output type, with one array layer for each scatter on the way from that step out to the
  consumer's level: the producing step's own and those of the calls around it (`_rank`).

It is nullable when the producing tool step, or any call on that path at any depth, has a `when`, because a
skipped step's outputs are `null` (`Workflow.yml:531-532`). Link does not judge that nullability, so the view
adds it. A `valueFrom` makes the type `Any`.

A `when` is typed with each step input's delivered type, not with the tool's declaration. `inputs.x` in a
`when` is the step input's value, which cwltool does not convert to the tool's type first: it evaluates the
`when` over the step's own input object (`cwltool/workflow_job.py:660-675`). It is evaluated per scatter job, so
a scattered input is typed by its element. An input of a nested workflow holds what the call binds to it, so
from PR B a nested workflow's input is typed, inside it, by the call's step input's delivered type, and by its
declaration only where the call leaves it unbound. The Nextflow target types a `when` this way from PR B (§9).
Until then it types a declared input by the tool's declaration, as today (`utils_nf.py:2994-3012`).

**Checked:** a step-only input fed by a step output makes cwltool wait. `wait.wic` (§9, example 2), compiled to
CWL and run with cwltool, logs `[step touch] completed success` before `[workflow ] starting step echo`.

## 6. Through calls: what the Nextflow target inlines

Nextflow has no lowering of a nested call; the backend inlines (backend design §6, Topology).
`tool_steps(workflow)` in `sophios.ir.resolved` gives every tool step at any depth, in graph order, as a
`ToolStep(step, calls, sources)`: the step, the calls it sits in, and each input's sources resolved through call
boundaries, each a `Through(ref)` whose `OutputRef` names a tool step by its qualified name:
- An `InputRef` of a nested workflow becomes what the call binds that input to, at the call's level, and so on
  outward until it names a root input or a tool step's output.
- An `OutputRef` of a call becomes its workflow output's source, and so on inward.
- A crossing that is not one plain source is kept as `Crossing(call, port)`. A plain source has no
  `linkMerge`, `pickValue`, `default` or `valueFrom`. A crossing also arises where an input the call leaves
  unbound has a default, and where a call's output is one whose `outputSource` names no step output.

The root's outputs are resolved the same way, by `output_sources(workflow)`.

The Nextflow target refuses, by name and as it does today, every `Crossing`, every nested workflow input with
a default, and every call that carries `when` or `scatter` (`utils_nf.py:1825-1857`, `:1878-1884`). Nothing
here decides whether a call dissolves into CWL (rule 2).

## 7. Targets and refusals (PR A)

`NEXTFLOW.lower` gathers everything the target cannot lower, all at once, and raises `NotLowered` when there is
any refusal. It also turns every error that its own lowering or the model's validation raises into a refusal of
the step being lowered (today `utils_nf.py:3418-3421`, `:3453-3454`). On success it returns the model and the
notes the lowering made (`wic046`, `utils_nf.py:1036-1047`).

The Nextflow target is `sophios.utils_nf.NEXTFLOW`; the CWL target is not one: it emits from the graph (rule 2)
and refuses nothing. The `target=` dispatch stays where it is (`api/python/workflow.py:1141-1147`, `main.py:406-410`).

`Construct` names the eleven constructs a refusal can be about, from `workflow` to `policy`. A `Refusal` carries
its construct, its subject (for `TOOL` the tool's file and the field's path in it), the root workflow, the
qualified step, a span and the reason. `NotLowered` holds every refusal.

**Printed form.** A refusal prints as `<span>: <owner>: <construct>[ '<subject>']: <reason>`:
- `<span>` is `file:line:column` (`lang/spans.py:43-44`), left out with its colon when there is none;
- `<owner>` is `step '<step>'`, or `workflow '<workflow>'` for a workflow-level construct;
- a `TOOL` refusal keeps the field's path inside the tool, because the user wrote that file. Only the path
  into emitted CWL, `steps[0].in.enabled`, goes;
- a field below the construct comes first in the reason, as in `workflow input 'n': type: long is not
  supported`, unless the reason names it first.

`Construct`, `Refusal` and `NotLowered` live in `sophios.ir.resolved`, beside the view they name.
`nf_support.CONSTRUCTS` joins each construct, by its value, to the `FIELDS` rows it stands for.

**Where a workflow input or output is.** A workflow port gains a span: the parser records each entry of
`inputs:` and `outputs:`, and a Python workflow input keeps the line that made it. A port without one falls
back to the workflow's span.

`NotLowered` prints as today: `Nextflow capability analysis failed:`, then one `- <refusal>` line per refusal
(`utils_nf.py:2975-2977`). The command line writes it to `error_<stem>.txt` (`main.py:99-104`). After PR A, the
example in §1 prints

```text
Nextflow capability analysis failed:
- when.wic:9:16: step 'echo': input 'enabled': the step's when reads an input its tool does not declare, which is not supported
- when.wic:2:3: workflow 'when': workflow input 'loud': required workflow input value is missing
```

From Python the first line starts with the script's path as Python gives it, then `:16:1`, and the second
with the same path, `:16:1`, where `flow.inputs.loud` was made. PR B makes the first line go away.

A view whose `policy.partial_failure` is set is refused as `POLICY`, subject `partial failure`, at the
workflow's span: "partial failure is a policy of the CWL target". In A and B no public path reaches it: the command line stops first (`cli.py:198-207`), and the
Python API has no policy until C (§10).

**Error codes do not change.** A Nextflow refusal carries no code today: it is a `ValueError` and not a
`Diagnostic`, which always has one (`lang/diagnostics.py:52-66`). It keeps none, and every coded diagnostic
keeps its code. A diagnostic that is new gets the next code in the catalog (`lang/error_codes.py:131` holds
`wic055`).

## 8. Inherited requirements (PR B)

A keeps today's three refusals, and only their text changes, to the user's span:
- workflow-level requirements other than the features (`utils_nf.py:2218-2230`);
- workflow hints (`nf_support.py:55`);
- a step's requirements and hints (`nf_support.py:66`, `:71`).

B replaces them with inheritance.

**The rule is cwltool's.** A tool step inherits what cwltool gives the tool it runs.
- A step's requirements are its own, plus each enclosing requirement whose class the step does not name. Its
  hints are the enclosing hints followed by its own (`cwltool/workflow.py:202-223`).
- The tool's own entries come after the inherited ones (`cwltool/process.py:560-584`), and a lookup takes the
  last requirement of the class, then the last hint (`cwltool/utils.py:481-489`).

So for each class a tool reads, in order:
1. its own requirement;
2. the innermost inherited requirement;
3. its own hint;
4. the innermost inherited hint.

The view computes `inherited` once; a target applies it to the tool document by that order.

**The four feature classes** decide only whether a workflow may use a feature. cwltool reads them at the step
(`cwltool/workflow.py:340-352`), and a tool never acts on them.

| Class | Written on a workflow or a step | Written on a tool | Inherited by a tool |
|---|---|---|---|
| `SubworkflowFeatureRequirement`, `ScatterFeatureRequirement`, `MultipleInputFeatureRequirement` | consumed, inert | refused, as today (`nf_support.py:279-285`) | inert, not lowered |
| `StepInputExpressionRequirement` | consumed, inert: a declaration is not a use, as with `InlineJavascriptRequirement` (backend design §6, Expressions). The `valueFrom` it allows stays refused, so the class with no `valueFrom` lowers, and a `valueFrom` is refused by name. | refused, as today | inert, not lowered |

Today `StepInputExpressionRequirement` is refused at the workflow level (`nf_support.py:288-293` has no row for
it), and every step-level requirement is refused.

Every other inherited entry is lowered, or refused, exactly as the same entry written on the tool would be
(`nf_support.py:272-287`). The target writes it into its own copy of the tool, where the order above puts it, and
judges the tool as it judges any: an entry the tool names one that wins over is not written. A refusal of a
written entry is the step's `REQUIREMENT`, subject its class, at the step's span, since the author wrote it outside
the tool's file; every other refusal of the tool stays the tool's. A workflow's or step's own section that is not a
mapping is refused as today, and a feature class there with a field beyond `class` is refused by its row.

**Byte identity.** A workflow that lowers today has no workflow hints and no step requirements or hints. Its
workflow-level classes are features or `InlineJavascriptRequirement` without `expressionLib`
(`nf_support.py:227-230`, `:288-293`). Inherited, `InlineJavascriptRequirement` is a declaration only
(`nf_support.py:277`), so nothing it lowers to changes.

**Runtime cases** under cwltool and Nextflow, compared as `tests/core/nextflow/test_reference_differential.py`
compares:
- a workflow `DockerRequirement` hint against the tool's own `DockerRequirement` requirement (the tool's wins);
- a workflow `DockerRequirement` requirement against the tool's hint (the workflow's wins);
- a step's `EnvVarRequirement` inside a nested workflow against the root's (the innermost wins);
- an `EnvVarRequirement` inherited from the root.

A task cannot see its `ResourceRequirement` (the target refuses `$(runtime.cores)`), so the innermost rule runs
with `EnvVarRequirement`, which the command prints, and `ResourceRequirement` is pinned by the lowering's data
cases. A losing `DockerRequirement` image names nothing, so a run that took it fails.

## 9. Step-only inputs on Nextflow (PR B)

**The carry rule.** A step-only input is carried unless both of these hold:
- its sources, resolved through call boundaries (§6), are all root workflow inputs;
- nothing reads it, neither its step's `when` nor its `scatter`.

Such an input cannot change what the step does or when it can start, and it is left out as today
(`utils_nf.py:1958-1962`, `nf_support.py:80-81`), so every pipeline that compiles today stays byte-identical. A
carried input lowers its `default`, `linkMerge` and `pickValue` as a declared one does; its type is what it
delivers. A `default` of one with no source of a scalar type has no type to check the value against, and is
refused by name. A
step-only input inside a nested workflow, fed by that workflow's input, is carried when the call binds that
input to a step output. Scatter over a step-only input stays refused, since no process port receives the
element. `valueFrom` stays refused on every step input.

**The model.** Two fields, each last and defaulted, so every existing positional and keyword construction still
works:

- `NfProcess.step_only_inputs: Sequence[NfPort] = ()`, after `condition` (`nf_types.py:737`).
  - Each is a `val` port, never a `path`, so nothing is staged. It is array-marked as its source is.
  - The command, environment and globs never reference one; validation rejects it.
  - A condition may read one, and a connection may end at one.
  - "A condition needs at least one input" counts them (`nf_types.py:858-863`).
- `NfPort.scalar_type: str | None = None`, after `optional` (`nf_types.py:629`).
  - It is the CWL type of what a `val` port receives when that is one scalar: `boolean`, `int`, `float` or
    `string`, with `?` when the value may be `null`. It is `None` on every `path` port and every array.
  - One function in `nf_types`, `scalar_type`, computes it from a port's connection: a parameter's declared
    type, one element of it for a scattered input; a process output's captured text, `string?` when its process
    has a condition; or what a step-input connection's `pickValue` picks, which is never `null`, when its
    sources agree on one type. A `val` port built with `None`, as 0.7.0 code builds every one, is filled by the
    workflow from its connection, and a value given that disagrees is refused. The lowering and the reader fill
    it with the same function, since they type a condition before the workflow exists, to refuse it at its step
    or its line; `refusals` assembles the workflow once nothing else is refused, so the model's own refusals,
    such as a condition on a process with no input at all, are among them.
  - The workflow's validation does not hold a step-only input to its source's path kind: a `val` takes a path's
    value and stages nothing. It refuses a scatter adapter into one.
  - The process's validation types its condition with the subset's `check` over its ports' `scalar_type` and
    requires `boolean`, once every port the condition reads has one; the workflow types it again over the ports
    it filled. This replaces the root-operator test (`nf_types.py:724`, `:856-857`).

`ExecutableNextflowWorkflow.SCHEMA_VERSION` goes from 20 to 21 (`nf_types.py:1180`), as backend design §5.3
requires for any change to the serialized form.
- `NfPort.to_dict` writes `scalar_type` on every port, `null` where it is `None`, as it writes `emit` and `glob`
  today (`nf_types.py:698-711`).
- `NfProcess.to_dict` writes `step_only_inputs` only on a process that has some.

Every `nextflow_workflow.json` therefore says `"schema_version": 21` and gains one `"scalar_type"` key per port.
Nothing reads that file back, while the three files Nextflow reads stay byte-identical for every workflow that
compiles today.

**Rendering.** A carried input is an input of its process:
- the process block declares it as `val <name>` after the tool's own inputs;
- the call passes its channel like any other input;
- the process's `ext` directive gets one more carrier, `WorkflowStepInput`, listing the carried names in order
  (backend design §4.2).

The command never reads a carried input, so the task runs what it runs today. A process starts only when every
input has a value, which is CWL's wait, and a `val` stages nothing. No new channel idiom is needed:
- a conditional call already merges every input into one tuple and branches on it
  (`input_output_nf.py:678-706`);
- a scattered call already broadcasts every unscattered input (`input_output_nf.py:709-729`, `:978-1048`);
- a tool with no inputs of its own still has one, so the rule that a conditional step's tool declares an input
  (`utils_nf.py:1974-1978`) goes.

**Why an input of the process.** The alternative carries each such value in the call's tuple and drops it in a
`map` before the call. It would need a new channel form at every call shape, and it fails at the first one:
- an unconditional call would have to merge;
- a scattered call would have to fold the carried value into its scatter channel;
- a tool with no inputs has no call argument to map down to, so it could not wait at all.

The reader would also have to tell a dropped closure variable from a used one and recover its name. As an input
of the process, the value takes the paths every input already takes, the reader reads it as an input, and only
the carrier says which inputs the tool does not declare. The task receives one unused value, which changes
nothing it runs.

**Example 1, the cookbook `when`** (`docs/cookbook/when.wic`, `when.py`). `echo.cwl` declares `message`;
`enabled` is step-only, fed by `loud`, and read by the `when`.

```groovy
process echo {
    ext WorkflowStepInput: ['enabled']

    input:
    val message
    val enabled
    ...
}

    ch_echo_in = echo_message.merge(loud) { __w0, __w1 -> tuple(__w0, __w1) }
    ch_echo_branch = ch_echo_in.branch { __w0, __w1 ->
        run: __w1
        skip: true
    }
    echo(ch_echo_branch.run.map { __w0, __w1 -> __w0 }, ch_echo_branch.run.map { __w0, __w1 -> __w1 })
    ch_echo_stdout = echo.out.stdout.mix(ch_echo_branch.skip.map { [] })
```

**Example 2, a step that waits on a step output.** `echo` reads nothing of `touch`'s, but must run after it:

```yaml
steps:
  touch:
    in:
      filename: !ii db
    out:
    - file: !& db
  echo:
    in:
      message: !ii query
      after: !cwl {source: !* db}
```

The CWL target writes `after: {source: touch/file}` on `echo`, and cwltool starts `echo` once `touch` has
completed (§5). Today the Nextflow target refuses `after` ("CWL runs this step only after that step finishes,
and the Nextflow target would not wait for it"). After PR B `echo`'s block declares `val after` with
`ext WorkflowStepInput: ['after']`, and the call is

```groovy
    touch(touch_filename)
    echo(echo_message, touch.out.file)
```

`touch.out.file` is a path, and `after` takes it as a `val`, so `echo`'s task stages no file for it.

**The reader** (`nf_reader.py`). It reads a carried input as one of the process's `val` inputs that the carrier
names. The carrier must name exactly the last `val` inputs, in order, or the read stops with `wic053`. Text
without the carrier reads as it does today, with no step-only inputs, so a pipeline written before PR B still
reads. `scalar_type` is filled by the function above. `import_nextflow` binds each carried input as
`StepInput(source=...)`, which is how a step binds a name its tool does not declare
(`api/python/workflow.py:657-684`), and builds the tool without it. The fixed point (backend design §4.2,
Verification) gains the cookbook `when`, `greeting_if` and example 2.

**Typing a `when`.** From PR B the Nextflow target types every `when` by `Condition.type` (§5). That has three
effects:
1. It types step-only inputs.
2. It reads an optional tool input fed by a required source as the required value.
3. It refuses a bare read of a value that may be `null` even where the tool declares the input required.

For (3), a `null` makes cwltool fail ("Conditional 'when' must evaluate to 'true' or 'false'",
`cwltool/workflow_job.py:690-694`), where Groovy would read `[]` as false and skip the step.

**Measured.**
- No workflow under image-workflows' `workflows/` or mm-workflows' `examples/` has a `when`.
- In the docs and examples, the cookbook `when` and `greeting_if` read only step-only inputs.
- `$(inputs.message < "27")` (`docs/tutorials/conditional_example.wic:8`, `examples/workflows/when_pyapi.py:20`)
  is refused before and after, since an ordered comparison takes numbers.

**The runtime differential** follows `tests/core/nextflow/test_subset_differential.py:114-118`:
`@pytest.mark.nextflow`, `@pytest.mark.serial`, `@budget(3)` (`tests/core/budgets.py:36-40`, which sets
`print_blob=True`) and `@given` over a list of up to 12 generated workflows, run as one batch under cwltool and
one under Nextflow. A batch is one data case compiled through `compile_source`, as the lowering's cases are,
since the Python API writes no requirement on a workflow or a step. Each generated workflow:
- has at most 4 steps over the host tools of `cwl_adapters`, `echo.cwl` and `append.cwl`;
- uses step-only inputs fed by workflow inputs or earlier outputs, a skipped step's among them;
- has a subset `when` over boolean, `int` and string workflow inputs.

Outputs are compared by name and content. The hand-written cases are one `@example`, a batch of the cookbook
`when` with `loud` true and false, example 2 and the two `EnvVarRequirement` cases of §8. It runs in Lint And Test's
Linux Nextflow step. `greeting_if` and the two `DockerRequirement` cases of §8 run their tools in containers, so
they are one batch in a test marked `docker` beside it.

## 10. Checks before anything is written (PR C)

**One shared check over the view, for every target: a `when` must be able to give `true` or `false`.** It finds
a `when` in two cases:
- The field is a string that is not one expression. That is the case when `cwl_utils.expression.scanner` does
  not span the whole stripped text, as cwltool's `interpolate` reads it (`cwl_utils/expression.py:23-99`,
  `:169-190`), so cwltool would evaluate it to text.
- The subset types it as a number, a string or `null`.

A `when` the subset cannot parse or type is unknown, and the check says nothing about it; so is a `when` that is
not a string, which cwltool's schema validation refuses itself. The check reads the authored `when`, never the
one the policy writes (§2). The two targets treat a finding differently:
- **CWL target:** a note, `wic056`, at the step's span. It never refuses a workflow and never changes emitted
  CWL.
- **Nextflow target:** the refusal it is today ("step when ... must compute a boolean"), now read from the shared
  check, with no code.

`docs/tutorials/conditional_example.wic:8` does not type, since `<` takes numbers, so it gets no note.

**What the check does not repeat.** Two checks run already, before either target: scatter over a value that is
not an array, and source and sink types that do not meet across a call boundary. Both are Link's `wic023`
(`ir/link.py:183-194`, `:274-299`, `:354-369`), and the view adds nothing to them. On this base:
- `scatter: [message]` fed by a `string` input stops both targets with
  `wic023 workflow input 'word' is provably disjoint: 'string' cannot feed {'type': 'array', 'items': 'string'}`,
  at the step;
- the same holds over a step-only input, `{'type': 'array', 'items': 'Any'}`;
- a `File` edge into a nested workflow's `string` input stops at the `!*` line.

```python
def check(self, *, target: Literal["cwl", "nextflow"] = "cwl", tool_registry: Tools | None = None,
          lang_version: str | None = None, inference: Inference = Inference(),
          policy: RunPolicy = RunPolicy()) -> tuple[Diagnostic, ...]: ...
```

`Workflow.check` compiles once and builds the view with the workflow's bound values and declared outputs. It
then runs the shared check, and for `nextflow` the whole lowering in memory.
- **Returns** every note that the compile, the check and the lowering made. It does not print them, so a
  `check()` followed by a `compile()` shows each note once.
- **Writes and checks nothing outside.** It writes nothing, renders no text and pulls no image. It checks
  nothing about the machine: the command line's `--check` does that (backend design §4.4, Check).
- **Raises** what `compile()` raises: `SophiosError` for a compile error, and for `nextflow` a `NotLowered`
  holding every refusal. `check(target="cwl")` never raises for the shared check.

`NotLowered`, `Refusal` and `Construct` are exported from `sophios.api.python.nextflow`, whose 22 names
(`api/python/nextflow.py:84-107`) become 25.

**Partial failure in the Python API.** One argument, `policy: RunPolicy = RunPolicy()`, is added to
`Workflow.compile`, `run`, `start`, `run_async` and `check`, beside `inference: Inference = Inference()`, which
those methods already take as an object (`api/python/workflow.py:1110-1116`, `:1215`, `:1277`, `:1320`). It takes the value P introduces
(§2), for example `flow.run(policy=RunPolicy(partial_failure=True, success_codes=(0, 1, 2)))`. `RunPolicy` is
exported from `sophios.api.python.workflow`, which reaches it through its runtime adapter, since the module may
not import `sophios.ir` (`pyproject.toml:311-318`).

The CWL target lowers it as the command line's flags do. The Nextflow target refuses it by construct (§7), so
`compile(target="nextflow", policy=RunPolicy(partial_failure=True))` raises `NotLowered`.

## 11. Layer contracts

The layers contract stands (`pyproject.toml:306-309`). `sophios.ir.resolved` and `sophios.ir.policy` are in
`ir`, and `sophios.lang.subset` in `lang`. Three contracts are added in PR A's last commit:

- The Nextflow target's modules, `utils_nf`, `input_output_nf` and the `nf_*` modules, import none of
  `ir.artifacts`, `ir.emit`, `compiler` and `post_compile`; `utils_cwl` stays allowed.
- Nor do they import `ir.stepin` or `ir.names` themselves: the view reads both on their behalf.
- `ir.resolved` imports none of `ir.artifacts`, `ir.emit`, `compiler` and `post_compile`.

## 12. The PRs

The PRs land in the order P, A, B, C; P sits directly on the baseline. Each has at most 7 commits.

**The gate set** is:
- the docs examples (`docs/cookbook`, `docs/tutorials`, `examples/workflows`);
- the test cases (`tests/core/data`);
- `image-workflows/workflows`;
- `mm-workflows/examples`, read through the search paths CI writes (`.github/update_sophios_config.py`).

**Byte-identical** means: for every gate-set workflow that compiles today under a mode, the files written in
that mode are equal byte for byte. The byte harness compiles each mode:
- the default CWL compile;
- each inline flag, and both;
- partial failure alone (`cwl-partial-failure`);
- partial failure with `--partial_failure_success_codes 1 2 3` (`cwl-partial-failure-codes`);
- partial failure with `--partial_failure_success_codes_range 2 5` (`cwl-partial-failure-range`);
- `--target nextflow`.

A mode replays what `main.py` does after the compile. From P on, that includes no post-compile tool rewrite.
The byte harness is the gate that lasts.

**PR A's test seam.** The Nextflow cases were CWL dicts handed straight to the lowering, never compiled. From
A's commit 3, `testkit` compiles each case through `compile_source` against a registry of its tools, and the
drivers lower the result. A step-to-step source written as CWL's `step/port` is re-spelled as a `.wic` edge. A
case no compiled workflow reaches leaves the Nextflow data; the core diagnostic it meets is pinned by the
compiler's own tests. The Python builders that rewrote emitted CWL become `subtree` documents in
`rejected/calls.yaml`.

| PR | Delivers | Deletes | Gate |
|---|---|---|---|
| **P** `fix`: partial failure is a policy | `sophios.ir.policy` (`RunPolicy`, `effective_when`, `tolerant_tool`), `CompilerOptions.partial_failure_success_codes`, and `surface` and `_artifact_tree` lowering the policy, with each fix of §2. One data file of partial-failure cases, `tests/core/data/partial_failure.yaml`, one row per fix, each failing at the baseline for its own reason, read by one driver. The driver replaces `test_partial_failure_takes_ordered_success_codes_and_makes_outputs_optional`, whose two rows become rows of it. `docs/partial-failures.md` rewritten. | The post-compile rewrite in `main.py`. `plugins.cwl_update_outputs_optional_artifact`. `plugins.cwl_update_outputs_optional`, whose rewrite moves into `tolerant_tool`. The `when` overwrite in `ir/emit.py`. Its description in `docs/dev/architecture.md`, rewritten. | **The three partial-failure modes:** byte-identical except the three changes §2 lists. Every other mode byte-identical. The fragments of §2 compiled. **cwltool:** compiled with the flag, the cookbook `when` gives `null` with `loud` false. |
| **A** `refactor`: Nextflow lowers from a resolved view of the graph | 1 `design:` this document and the backend revision. 2 `lang.subset`: the subset's parse, type check, canonical form and source text leave `nf_expr`. 3 `testkit` compiles each case through `compile_source`; the cases are re-spelled as above. 4 `ir.resolved`, `resolve`, `compiler.resolved_view` and `CompilationResult.policy`, and `tool_steps`. 5 Nextflow lowers from the view: every workflow-level analysis is over view constructs, run values and kept outputs are passed as §4 says, and tool lowering is unchanged. 6 Refusals at the user's line (§7): `WorkflowPort.span` and `Document.port_spans`, every expectation string that names a CWL path re-spelled in the printed form, and `nf_support.CONSTRUCTS`. That last is a second table joining each `Construct` to the `FIELDS` rows it stands for; `FIELDS` stays keyed by CWL class, so `docs/conf.py` and `tests/core/nextflow/test_support_table.py` keep working, and both gain the join. 7 Deletions and the import-linter contracts. | `CompiledNextflowSource`, `compilation_result_source` and its helpers. The CWL-dict versions of the workflow-level analysis. `testkit.synthetic_source` and `workflow_doc`. | **Byte-identical** in every mode, all four Nextflow files included. **Refusals:** only their text changes. The cases the core refuses leave the Nextflow data. The test count does not grow. |
| **B** `feat(nextflow)`: step-only inputs and inherited requirements | Carried inputs (§9), `NfProcess.step_only_inputs`, `NfPort.scalar_type`, schema 21, the `WorkflowStepInput` carrier, the reader and `import_nextflow` reading output with and without it, and `when` typed by delivered type. Inherited requirements and the four feature classes (§8). The runtime differential (§9), with the §8 cases among its examples. | `_undeclared_inputs`, `_named_sources`, `_undeclared_input_findings` and `_declared_inputs_only` in their view form from A (today `utils_nf.py:1895-1962`). The rule that a conditional step's tool declares an input (`:1974-1978`). The `when`'s typing against the tool (`:2994-3012`). The three requirement and hint refusals A kept (§8). | `workflow.nf`, `nextflow.config` and `nextflow_params.json` byte-identical over the gate set. `nextflow_workflow.json` differs only as §9 says. Every CWL mode byte-identical. Fixed point over the old cases and the new ones. A pipeline written by A imports. The differential passes. The gate set gains no refusal (§9, Measured). |
| **C** `feat`: checks before anything is written | The shared `when` check (§10), `wic056` as a note on the CWL target, `Workflow.check(target=...)`, the three exports from `sophios.api.python.nextflow`, and `policy=` on `compile`, `run`, `start`, `run_async` and `check`. | The Nextflow target's own boolean test of a `when`, which reads the shared check from here on. | Every mode byte-identical. No gate-set workflow gets a `wic056` note. Nextflow refusals unchanged. `check()` returns its notes without printing them, raises what `compile()` raises, and leaves the working directory and base path as it found them. `policy=` writes, for a Python workflow, the CWL the flags write for its `.wic` twin. |

## 13. What stays

- The `.wic` grammar, the Python API's existing names and signatures, and every command-line flag. The 22
  public names of `sophios.api.python.nextflow` keep their fields; two gain one optional field each, last (§9),
  and C adds three names (§10).
- Emitted CWL, the inliner and every CWL rewriting flag (rule 2), but for P's fixes (§2).
- `workflow.nf`, `nextflow.config` and `nextflow_params.json` for every workflow that lowers today.
- Error codes. Drawing.
