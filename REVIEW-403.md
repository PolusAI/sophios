# Adversarial review — PR #403 (`semrefac_7.1..semrefac_8`)

Reviewed: `tests/core/{synthetic_tools,hermetic,ast_strategies,equivalence}.py` and
`tests/core/test_{hermeticity,generators,equivalence}.py`.

Method: 18 mutations, each applied as a single textual patch, run against the whole
fast+slow lane of the three test files, then restored and verified byte-for-byte with
`shasum -a 256`. Six survived the pre-fix suite. Five were defects; one was correct
behaviour.

## Findings

### C1 — Critical. `UP_TO_RENAMING` was blind to which workflow input feeds which port

`_dataflow` turned a step's `in[].source` into a graph edge only when the source split
on `/` into `producer/port`. Every source that named a *workflow-level input* instead —
which is what the compiler emits for every argument a step does not receive from another
step — hit `continue` and was dropped. `_step_body` had already stripped `source` from
the binding on the grounds that `_dataflow` re-expressed it. For these sources nothing
re-expressed it, so the value was compared nowhere at all.

Reproduction against the pre-fix relation, using the shape a hermetic compilation
actually emits:

```python
def doc(left, right):
    return {'steps': [{'id': 'a__step__1__join',
                       'in': {'left': {'source': left}, 'right': {'source': right}}}],
            'inputs': {'a___p': {'type': 'File'}, 'a___q': {'type': 'string'}},
            'outputs': {}}

equivalent(doc('a___p', 'a___q'), doc('a___q', 'a___p'), Strength.UP_TO_RENAMING)
# -> None   (the File input and the string input have been swapped)
```

The port-shape multiset is identical on both sides, so nothing else caught it. This is
the exact failure mode Spec 3's differential properties exist to catch: an IR migration
that rewires an argument reports zero divergences.

Fix: a workflow-level input becomes a node in `_dataflow`, keyed `<input> {name}` so it
cannot collide with a step id, and labelled with the *shape* the document declares for
it (`type`/`format`) — never its name. Differently-typed inputs can no longer be
swapped silently; swapping two identically-shaped ones stays equivalent, because that
swap is precisely what re-rooting a namespace does. Both directions are asserted, so
the fix cannot overshoot into rejecting legitimate renamings (mutation M16).

Test: `test_the_dag_check_sees_a_port_rewired_onto_a_different_workflow_input`.

### C2 — `format` was in `_SHAPE_KEYS` and untested

Removing `'format'` from `_SHAPE_KEYS` left all 52 tests green. `format` is the only
thing separating `mk_file` from `mk_text` in the synthetic registry, so the relation
could not have told a workflow ending in one from a workflow ending in the other.
Fix: a `MUST_DIFFER` row (`'an output changed format'`), checked at all three strengths.

### C3 — `_stem`'s fallback was untested, and it is the load-bearing half

The docstring argues the fallback to the whole string is the *stricter* choice. Making
it return a constant — lumping every unparsable id under one anonymous stem — left the
file green. Fix: `test_an_unparsable_producer_keeps_its_whole_name`.

### C4 — nothing pinned that `UP_TO_EMBEDDING` forgives `run` and only `run`

Widening the normalisation by one key (deleting `label` alongside `run`) survived the
whole suite. That is the shape of change an IR migration makes when a key starts moving
around, and the module's own thesis is that a normalisation without a reason is where a
real difference hides. Fix: `test_up_to_embedding_forgives_run_and_nothing_else`.

### C5 — the `NOT_YET_COMPILABLE` companion test was deleted with the CE-13 entry

`ast_strategies.py` claimed "`test_generators.py` has a companion asserting every one of
these still genuinely fails to compile". That companion existed through `ee1856d` and
was removed in `b0fa2d0` along with the entry it checked. Consequences: setting an
exclusion predicate to `lambda d: True` — emptying `compilable_documents()` and with it
`workflows()`, the strategy every Task 3-7 property quantifies over — left the suite
green (M11); and `compilable_documents`, `excluded_documents`, `workflows` and `to_yml`
had no test reaching them at all, so deleting `to_yml`'s
`desugar_into_canonical_normal_form` call (which its own docstring warns against, and
without which every mapping-form document raises `KeyError: 0`) was also invisible (M17).

Fix, in `test_generators.py`:

* `test_the_compilable_subset_still_reaches_every_construct_it_does_not_exclude` — holds
  the *filter* to the standard P26 holds the generator to. Non-vacuous while
  `NOT_YET_COMPILABLE` is empty, which is exactly when a filter that excluded everything
  would otherwise go unnoticed. A per-entry "still fails to compile" check would not be.
* `test_the_workflows_strategy_produces_documents_the_compiler_accepts` — both surface
  forms must reach a successful hermetic compilation, and a majority overall. The bar is
  a majority, not everything, because the module's declared `!ii` literal/type residual
  is real and measured at about one document in ten; a stricter threshold would be flaky
  rather than stronger.

Docstrings corrected: the CE-13 paragraph narrated in the present tense a state
`b0fa2d0` removed (`documents()` "keeps generating it", the filter, the companion), and
referred to an `edge_def` row of `CONSTRUCTS` that no longer exists.

### C6 — `ORACLE_FILES` and `ORACLE_MODULES` are two lists with nothing linking them

`tests/core/test_generators.py` is in `ORACLE_FILES` (run under the poisoned subprocess)
but was not in `ORACLE_MODULES` (the roots of the static import scan), and nothing
imports a test module, so it was never reached transitively either. Adding
`import sophios.plugins` to it left `test_no_oracle_module_reaches_plugin_discovery`
green — the static half of P25, which the module docstring calls the broader of the two.

Fix: `core.test_generators` added, plus
`test_the_static_scan_covers_every_file_the_poisoned_run_covers` as the link.

### C7 — `synthetic_tools` documents a ninth tool that does not exist

The module docstring's bullet list names `passthru` ("an optional input with a default,
so `args_required` is a proper subset of the inputs somewhere") in a registry of eight
stems that contains no such tool. The claim itself is true of `scale.factor`, which is
what `test_the_registry_reaches_the_branches_it_claims_to` actually exercises. Fix:
the claim moved onto the stem that carries it; the phantom bullet deleted.

## Mutation table

| # | Mutation | Pre-fix | Killed by |
|---|---|---|---|
| M1 | `_step_body` stops re-adding bindings | killed | `test_up_to_renaming_forgives_nothing_inside_a_step...[a binding moved]` |
| M2 | node label drops the step body | killed | `test_up_to_renaming_forgives_nothing_inside_a_step...` |
| M3 | node label drops the tool stem | killed | `test_no_strength_accepts_a_real_difference[a step names a different tool]` |
| M4 | `UP_TO_EMBEDDING` compares key order | killed | `test_only_identical_compares_key_order` |
| M5 | embedding normalisation widened by one key (`label`) | **survived** | now `test_up_to_embedding_forgives_run_and_nothing_else` |
| M6 | `_SHAPE_KEYS` drops `format` | **survived** | now `test_no_strength_accepts_a_real_difference[an output changed format]` |
| M7 | requirements compared by count, not name | killed | `test_the_array_forms_of_ports_and_requirements_are_read` |
| M8 | `_stem` fallback returns a constant | **survived** | now `test_an_unparsable_producer_keeps_its_whole_name` |
| M9 | external producers dropped instead of kept | killed | `test_the_dag_check_sees_two_edges_dangling_at_different_places` |
| M10 | edge label drops the input name | killed | `test_the_dag_check_sees_one_of_two_edges_replaced_by_an_outside_source` |
| M11 | exclusion predicate `lambda d: True` empties the strategy | **survived** | now `test_the_compilable_subset_still_reaches_every_construct...` |
| M12 | generator stops emitting `interpreted_when` | killed | `test_every_construct_appears_within_a_bounded_sample` |
| M13 | `core.equivalence` dropped from `ORACLE_MODULES` | **survived — not a defect** | `_reachable` is transitive; reached via `core.test_equivalence` |
| M14 | workflow-input sources dropped again (reverts C1) | n/a | `test_the_dag_check_sees_a_port_rewired_onto_a_different_workflow_input` |
| M15 | input node label drops the shape | n/a | same |
| M16 | input node keyed by name, not shape (over-fix) | n/a | `test_each_rewrite_lands_at_exactly_the_strength_it_claims` |
| M17 | `to_yml` skips desugaring | **survived** | now `test_the_workflows_strategy_produces_documents_the_compiler_accepts` |
| M18 | poisoned-run file left out of the static scan | **survived** | now `test_the_static_scan_covers_every_file_the_poisoned_run_covers` |

## Declared, not fixed

* **`recursively_delete_dict_key('run', ...)` is document-wide.** `UP_TO_EMBEDDING`'s
  stated reason covers `steps[].run` only, but the deletion reaches any key named `run`
  anywhere, including a tool input literally named `run`. The recursion is load-bearing
  for inlined subworkflow bodies, no synthetic stem has such an input, and narrowing it
  would need a step-aware walk. Recorded rather than changed; C4's test pins that no
  *other* key joins `run`.
* **A step with no `id` is invisible to `UP_TO_RENAMING`.** `_dataflow`'s `declared`
  filter drops it, so `{'steps': [{'out': ['f']}]}` and `{'steps': []}` are equivalent at
  that strength. Emitted CWL always carries ids, so this is reachable only from
  hand-written fixtures. Left as is; noted so a Spec 3 fixture author knows.

Nothing found here requires a change under `src/`. The two `src/` findings this branch
already declares — CE-13 (settled upstream) and the `!ii` literal/type `ValueError`
totality violation in `populate_scalar_val` — stand as written.
