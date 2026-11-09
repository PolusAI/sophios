# One Target Table and nf-s Codes — Design

**Status:** Implemented. A commit that changes this file has a subject beginning with `design:`.
**Baseline:** `4c7f0295`, the tip of PR C (`feat/checks-before-writing`).
**Scope:** one PR, D, on top of C. It builds on `design_docs/resolved-view-design.md`, whose §7 (the CWL target
outside the protocol, the `target=` dispatch, error codes), §10 (`check()` raising `NotLowered`) and §12 (the PR
list) this commit revises to point here.

---

## 1. What this is

Two things were hand-written that should each be one table.

**The target was picked by branches.** Each entry point decided what `cwl` and `nextflow` mean on its own: the
command line (`main.py`, `if args.target == 'nextflow'` after the compile, a runner picked by target, a CWL backend
named for the raw-CWL check), `Workflow.compile` and `check` (each with its own `target not in (...)`), the Python
run (`DEFAULT_RUN_ARGS if target == "cwl"`, a CWL and a Nextflow branch in `_run_request`), `run_local` and the
command line's option groups. The run side had a table of its own, `backends.BACKENDS[(target, runner)]`. An
unknown target was "unsupported compilation target" from `compile` and `check`, and `run` took the CWL branch.

**A Nextflow refusal had no code.** Every other problem Sophios reports is a `Diagnostic` with a code, printed as
`file:line:col: error [code] message (step, port)`, explained by `sophios --explain` and listed in
`docs/error_codes.md`. A Nextflow refusal was a `Refusal` in a `NotLowered`, printed in its own layout. Compiling

```yaml
steps:
  echo:
    in:
      message: !ii hi
    when: $(inputs.message < "27")
```

with `sophios --yaml refused.wic --target nextflow` printed `Failed to generate Nextflow artifacts. See
error_refused.txt.` on stdout, exited 1, and wrote into `error_refused.txt`, in the working directory whatever
`--basepath` said,

```text
Nextflow capability analysis failed:
- refused.wic:2:3: step 'echo': when: '$(inputs.message < "27")' is outside the safe JavaScript subset: < requires number operands, not string
```

D makes both one table: one table of targets that every entry point reads, CWL answering the same contract as
Nextflow (§2, §3); and every Nextflow refusal a diagnostic with a code, `nf-sNNN` ("Nextflow support"), in the
ordinary layout, from the command line, `compile()`, `run()` and `check()` (§4, §5). The nf-s list is Nextflow's
support table.

## 2. The table

`sophios.targets` (`targets.py`) is the one module that names every target. A row has a `name`, as `--target` and
`target=` give it; `runners`, the default first; `runner_option`, the run argument that picks the runner
(`cwl_runner`, or None for one runner); `run_defaults`, a Python run's `run_args_dict` defaults (was
`_api_config.DEFAULT_RUN_ARGS`); `write_option`, the flag that asks it to write (`generate_cwl_workflow`, or None:
always); `takes_plain_cwl`; and `lower`, which returns the model, its notes and the view it read, or raises
`NotLowered`, `clear`, which removes what an earlier write left, and `write`. `target()` is the one place an unknown
target is reported; `backend()` replaces `backends.backend`.

| | CWL | Nextflow |
|---|---|---|
| `lower` refuses | nothing | every nf-s code, and `wic012`, `wic056` as errors (§5) |
| `lower` builds | `result.artifact`, as the compile emitted it; notes `compiler.cwl_notes(view)` | `utils_nf.NEXTFLOW.lower(view)` |
| `write` | `write_artifacts_to_disk` and the analyses beside it | the four files; a compile writes no analysis |
| `clear` | nothing | the four files |
| `runners` | `cwltool` (default), `toil-cwl-runner` | `nextflow` |

**The names alone are cheap.** `wic_types.TargetName = Literal['cwl', 'nextflow']` and `TARGET_NAMES`: the command
line's parser builds one option group per name and offers them to `--target` without importing a target, so
`--explain` and `--version` load no compiler. `api/python/workflow.py` may not import the compiler side
(`pyproject.toml`), so its five methods keep `target: Literal["cwl", "nextflow"]` and pass the name to the runtime
adapter, which calls `targets.target`. `tests/core/test_targets.py` keeps `TARGETS`, `--target`'s choices and the
`Literal` of each method one set.

Every entry point looks its target up first, and `Backend.prepare` takes the lowered model and never lowers.

**What stays keyed by name outside the table**, each because it is the Python API's own surface, which the table
may not import: `_workflow_runtime._COMPILED`, the object `compile()` returns for each target (`CompiledWorkflow`,
`CompiledNextflowWorkflow`, as its overloads say), and `_REQUESTS`, how each target's run takes `run_args_dict`
(CWL's `--<key> <value>` to the runner, Nextflow's `-<key> <value>`, and refusing a CWL-only key). Both are keyed by
the row's name and reached only after `targets.target`; rows that built the API's objects would put the Python API
inside the run path. Two command-line options act on CWL's model and are already refused for other targets by
`validate_target_args`: `--cwl_inline_subworkflows` and `--cwl_inline_runtag` rewrite it between `lower` and
`write`. `--allow_raw_cwl` asks the row: CWL `takes_plain_cwl` and passes a plain CWL workflow through before any
compile, as before; Nextflow does not, and the command line says so on stderr (`--allow_raw_cwl: --target nextflow
does not take a plain CWL workflow as it is, so <file> is compiled as a Sophios document`) before compiling it,
which it did silently before.

**Lowered once, from one view.** Every target is given the same view of a compilation: on the command line, the
graph's values and every output; from Python, `_view(workflow, result)`, the values bound on the workflow and the
outputs it declares (for CWL too, where C built a bare view; `cwl_notes` reads only each step's authored `when`, so
its notes are unchanged by values and kept outputs). A run first reads the view's relative paths against its root
(`run_setup.absolute_view`, as `NextflowBackend.prepare` did inside), lowers once, and `prepare` writes that model.

`flow.run(target="wdl")` used to compile and run cwltool; now every method raises `ValueError: unknown target
'wdl'; the targets are cwl, nextflow` before compiling. `sophios.targets` joins `run_setup`, `backends` and
`run_local` in the import contract "The run path takes values, never the command line's arguments".

## 3. CWL answers the same contract

`Target` moved from `sophios.ir.resolved` to `sophios.targets`, and `lower` takes the compilation beside its view.
It could not stay view-only in `ir.resolved`: CWL's model is `result.artifact`, which the view does not hold and
`ir.resolved` may not reach. The Nextflow row passes only the view to `utils_nf.NEXTFLOW.lower`, so "The Nextflow
target reads the resolved view, never emitted CWL" stands. `NextflowTarget.refusals` had no caller and went.

- **Refusals:** none; `CWL.lower` never raises `NotLowered`.
- **Lowering:** today's emitter, unchanged: the artifact the compile emitted. Emitted CWL is byte-identical (§7).
- **Notes:** `wic056` from the shared `when` check, `compiler.cwl_notes(view)`, which now takes the view it is
  given instead of building its own.

## 4. Refusals are diagnostics

A `Refusal` (`ir/resolved.py`) carries its code first, an nf-s code or `wic012` or `wic056` (§5), and its
`.diagnostic`. `NotLowered(refusals)`, whose text is its diagnostics with no header, is still a `ValueError`
exported from `sophios.api.python.nextflow`, which `compile()` and `run()` raise.

**What the command line prints.** Before, a refusal went through `main._not_lowered` from the write path and from
`--run_local`, `--generate_run_script` and `--check`: it wrote `str(NotLowered)` to `error_<stem>.txt` in the
working directory, whatever `--basepath` said, and printed only the pointer to it on stdout. Now both paths catch
`NotLowered` alone (they caught any `ValueError`, turning an unexpected error of writing or preparing into a
"refusal"), print `Failed to compile <yaml>` on stderr as a compile error does, and raise the diagnostics as a
`SophiosError`, which `main()` reports on stderr in the form `--diagnostics` asks for and turns into exit 1. Neither
the pointer nor the file stays:
- nothing in the docs promised it: `docs/cli-reference.md`, `docs/running.md` and `docs/checks-and-errors.md` name
  `error_<workflow>.txt` only for a failure Sophios did not expect, and `docs/nextflow.md` named it nowhere;
- one test pinned it, `test_nextflow_cli_conversion_error_is_concise_and_actionable`, now folded into
  `test_a_refusal_is_reported_on_stderr_as_a_diagnostic`, which keeps its point: a refusal leaves none of the four
  files an earlier write left;
- a refusal is a reported problem, which exit 1 already describes as "its diagnostics are on stderr"; the file is
  for a traceback, and a refusal has none. The crash handler keeps its `error_<stem>.txt`.

The `.wic` of §1 now prints, on stderr, exit 1, no file:

```text
Failed to compile refused.wic
refused.wic:2:3: error [nf-s010] when: '$(inputs.message < "27")' is outside the safe JavaScript subset: < requires number operands, not string (step 'echo')
```

`--diagnostics json` prints it as one object with `"kind": "target"` and the catalog's `fix`.

`flow.check(target="nextflow")` returns the refusals as error diagnostics where C raised the `NotLowered`: every
note the compile made, every refusal as an error, and when nothing is refused every note the lowering made. It still
raises `SophiosError` for a workflow that does not compile, and `ValueError` for an unknown target.

## 5. The nf-s codes

**Machinery.** Each code is a member of `SophiosErrorCode` with value `nf-sNNN`, an `Explanation` and a row in
`docs/error_codes.md`, whose second paragraph names the third range and whose kind list gains `target` and a tool's
CWL file under `document`. `--explain` parses any member's value. `Kind.TARGET = 'target'`: nothing is wrong with
the workflow, this target cannot run it yet; its fix names the construct to change, or `--target cwl`. Three codes
are kind `document` (§9, question 1). Every code has a provocation (`tests/core/provocations.py`), as every code
does.

**In the lowering** (`utils_nf.py`):
- a finding is a `_Finding(code, text)`, `text` the `<locator>: <reason>` the refusal is built from;
- a helper that cannot lower raises `_Unsupported(code, message)`, a `ValueError`; each `except` that turns one
  into a finding catches `_Unsupported` alone and keeps its code, so a plain `ValueError` is an invariant, and
  escapes as a crash;
- `nf_support.Row` gains `code`, set exactly on a `REJECTED` row (checked in `__post_init__`), and a refusal of a
  field takes its row's code; a field the table does not list is `nf-s054`;
- a value that is either an expression the target does not evaluate or not CWL at all picks its code by which it
  is (`_computed_or_invalid`, `_empty_or_invalid`).

**In the model** (`nf_types.py`). Nine of the model's checks are reached by a real workflow. Each is now one
predicate the model's own validation calls, and the lowering calls before it builds the model, refusing with its
code: `environment_name_problem` (`nf-s041`), `entryname_problem` (`nf-s043`), `staged_write_problems`
(`nf-s035`), `condition_without_inputs` (`nf-s012`), `absent_path_problems` (`nf-s020`) and
`optional_output_problems` (`nf-s030`). The public model keeps raising its plain `ValueError` for hand-built input,
and the importer keeps reporting it as `wic053`. The two catch-alls that turned any `ValueError` of the lowering or
the model into an uncoded refusal are gone (`_analysis`, `_assembled`). A `when` the model's typing refuses over
its typed inputs is `nf-s010`.

**What the catch-alls hid**, each now refused with a code before the model is built: an `outputSource` naming a
port its step lacks (`endpoint_problems`, `nf-s028`, as a step that is not there); an array input read outside its
own binding, as in `glob: $(inputs.xs).txt` (`reference_problems`, `nf-s050`; an undeclared input is `nf-s045`);
NaN or an infinity as a value or a default (`json_value_problem`, `nf-s015`); a NUL byte in any literal
(`literal_problem`, `nf-s055`); a section or requirement body of the wrong shape (`_tool_shape_findings`,
`_as_list`, `_as_mapping`, `nf-s055`); and an optional union, a record or another type the target does not lower
as a tool input's `default` (the type's own refusal).

`tests/core/nextflow/test_lowering_outcomes.py` asserts, over drawn tool and workflow shapes, that lowering ends in
a model, a `NotLowered` or a `SophiosError`; it runs at `budget(60)`, and every reason it found reuses a code.

**Principles.** One code per reason, not per raise site. Every code is reached by a real workflow: a `.wic`
document compiled through `compile_source`, as the rejected cases are, or a Python workflow, and a case pins it
(§7). A raise another check always pre-empts is deleted; an internal invariant gets no code. A reason that is
already a code keeps it: `wic012` (a required input that gets no value) and `wic056` (a `when` that cannot give a
boolean), which the shared check of C now finds first on Nextflow too. Numbered straight through, grouped workflow,
scatter, `when`, inputs, outputs, requirements, expressions, tools, and the CWL-invalid code last; never reused once
released; when Nextflow learns a construct its code is retired and its row says "supported since <version>".

**Two codes no workflow reaches alone.** `nf-s004`, a scatter over an array of arrays: a workflow input of that type
is refused for its type (`nf-s048`) too, and an array step output is refused at its producer (`nf-s031`).
`nf-s007`, a `nested_crossproduct` result read by a later step: two scattered inputs make an array of arrays, which
the reading port must declare (`nf-s048`) or Link refuses (`wic023`), and one scattered input is a `dotproduct`,
which lowers (`tests/core/data/nextflow/rejected/scatter.yaml`, `a_nested_crossproduct_result_read_by_a_later_step`).
Each stays its own code: the scatter is its own construct, and a target that learns nested arrays still refuses it.

**The list** is `docs/error_codes.md`; `test_the_error_codes_page_says_what_the_code_says` keeps it equal to the
catalog. A source written as text has two codes: `nf-s027`, a nested call's output, valid CWL as
`tests/core/data/python_api_pairs/re_export.wic` shows; `nf-s028`, a step that is not there, the document's mistake.

## 6. What D deletes, and why

- The target branches of §2, `backends.BACKENDS` and `backends.backend` (the rows' `runners`), `_api_config.py`
  (the CWL row's `run_defaults`), `run_setup.Settings.view` and `Prepared.artifact` and `Prepared.notes` (the
  lowered model carries its view and notes; `Prepared` names the workflow), `ir.resolved.Target` and
  `NextflowTarget.refusals` (§3), and the "unsupported compilation target" messages.
- `main._write_nextflow_target`, `_run_nextflow`, `_not_lowered` and the `error_<stem>.txt` a refusal wrote (§4); the
  `Nextflow capability analysis failed:` header.
- The two catch-alls (§5).
- Nothing in the field table: the two `WorkflowOutputParameter` rows `linkMerge` and `pickValue` stay, since the
  table holds every field cwl-utils declares; their note says the compiler refuses them first (`wic038`), which is
  the code a user meets.
- Refusals another check always pre-empts, each with the case or probe that shows it: the boolean-flag source check
  (`_flag_source_findings`, `wic023` refuses a non-boolean source first); "a scattered input must have exactly one
  source" (the step-input pass refuses the same input for the same reason, `nf-s006`, case
  `a_scattered_input_with_two_sources`).

Refusals a case still reaches stay, coded, and so do the listing checks a tool with a finding never reaches
(`_iwdr_entries`), as the finding pass's control flow.

## 7. Tests, and what was measured

Every rejected case (`tests/core/data/nextflow/rejected/*.yaml`) names its refusals' codes in order, and new cases
reach each code and each family of §5 no case reached. Other tests pin the table against `--target` and the
`Literal`, an unknown target, a refusal's layout from Python and the command line, `check()` returning refusals,
`--explain nf-s007`, and `--allow_raw_cwl` under Nextflow.

- Expectations whose text D changes by design: a workflow input of a type the target does not lower names the type
  (`scatter_over_an_array_typed_port`, `scatter_over_a_non_array_workflow_input`); a `when` that is text is
  `wic056`'s wording (`tests/core/data/when_boolean.yaml`); the made-input test and the no-inputs `when` test read
  the diagnostic.

**Measured.** The byte harness, taught that from D on only a `NotLowered` is a refusal and any other `ValueError` a
crash, on `4c7f0295` and on D's tip, over the gate set and every test data set: 537 workflows at the base, 560 at
the tip (the 23 new rejected cases). Every file written in every mode is byte-identical; no item changes status in
any mode; all 522 mode results refused at the base are refused at the tip with a code; nothing crashes.

## 8. What changes against C, and what does not

**Changes**, none of them in 0.7.0 (`Refusal` and `NotLowered` arrived in this chain): `Refusal` gains `code`,
first, `message` and `diagnostic`, and loses its own `__str__` layout; `NotLowered(refusals)` replaces
`NotLowered(target, refusals)`, with no header; `check(target="nextflow")` returns the refusals; the command line
reports a refusal on stderr and writes no `error_<stem>.txt` (§4). Internal: `compiler.cwl_notes` takes a view;
`Backend.prepare` takes a `Lowered`; `run_setup.Prepared` and `Settings` lose the fields §6 names.

**Unchanged:** emitted CWL, byte for byte; every file the Nextflow target writes for a workflow it lowers; every
refusal's construct, span and reason but for the ones §7 lists; the Python API's signatures and `target:
Literal["cwl", "nextflow"]`; `--target` and every other flag; `NotLowered` a `ValueError` from
`sophios.api.python.nextflow`. No new Nextflow feature.

## 9. Open questions

1. **Three codes of kind `document`.** `nf-s028` (a source naming no step), `nf-s046` (a path reference whose CWL
   value is not the text it stands for) and `nf-s055` (CWL cwltool would not run) are the workflow's problems, which
   under `--target cwl` the compile writes and the runner meets. Kept as nf-s codes of kind `document`: only the
   Nextflow target reads those fields closely enough to find them. `SophiosErrorCode.is_language` counts them with
   the `wic` codes, as describing the document, which `test_every_code_is_explained` holds kind `document` to.
   Moving them to `wic` codes for both targets would make CWL compiles that succeed today fail, and for `nf-s028` it
   would stop `re_export.wic`'s valid shape too.
2. **`nf-s011`**, a `when` reading an input its step does not bind, stays kind `target`: cwltool reads `undefined`,
   so `$(inputs.n > 3)` is false and skips the step.
3. **Refusals of what cwltool ignores**, left as they are (no new features): `scatterMethod` with no `scatter`
   (`nf-s008`), a feature class on a tool (`nf-s036`), an empty `prefix` (`nf-s052`), a `when` on a step whose tool
   has no inputs (`nf-s012`).
