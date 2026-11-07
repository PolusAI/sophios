# Cookbook

Every example in these pages is here, as a pair: `docs/cookbook/<name>.wic`
and `docs/cookbook/<name>.py` in the Sophios repository. The two files of a
pair compile to the same CWL, the same job file and the same notes, and a
test in the repository fails if they ever differ, so an
example says the same thing to a reader of either form. Copy the one you
need and change it.

## Running an example

From the project directory of [Your first workflow](first-workflow.md),
with the two files copied in:

```bash
sophios --yaml greeting.wic --run_local
python greeting.py
```

Each `.py` defines `workflow()` and runs it under `__main__`; the `.wic`
finds its tools on `search_paths_cwl`, and the `.py` under
`~/wic/cwl_adapters/`, which `sophios --generate_config` filled. To compile without running, use
`--generate_cwl_workflow`, or in Python:

```python
from greeting import workflow

compiled = workflow().compile()
print(compiled.to_cwl_yaml())
print(compiled.to_job_inputs_yaml())
```

Three things hold for every pair, and [Compiling to CWL](compiling-to-cwl.md)
explains them: each step keeps its id; a literal
becomes a workflow input named `<step>~<port>` with its value in the job
file; and every step output is exposed as a workflow output, except that a
Python workflow which names outputs keeps only those, in `compile()` and
`run()` alike.

## The pairs

The guide's own story, in the order the chapters use it:

| Pair | Shows | Explained in |
|---|---|---|
| `greeting` | three steps, every connection written; the first workflow | [Your first workflow](first-workflow.md) |
| `greeting_input` | the greeting as a workflow input, given from an inputs file | [Inputs and values](inputs-and-values.md) |
| `cat_file` | a `File` value, and where its relative path points | [Inputs and values](inputs-and-values.md) |
| `greeting_output` | the greeting file as a named workflow output | [Connecting steps](connecting-steps.md) |
| `greeting_graph` (with `shout.cwl`, `word_count.cwl`, `combine.cwl`) | a branch and a join: the greeting as a graph | [Connecting steps](connecting-steps.md) |
| `greeting_inferred` | the greeting with every connection left to inference, and its note | [Edge inference](edge-inference.md) |
| `greeting_graph_inferred` (with `shout.cwl`, `word_count.cwl`, `combine.cwl`) | the graph with every edge left out, which inference stops at the join | [Edge inference](edge-inference.md) |
| `greeting_graph_partial` (with `shout.cwl`, `word_count.cwl`, `combine.cwl`) | the graph with the edges at the branch and at the join written, and the rest inferred | [Edge inference](edge-inference.md) |
| `naming` (with `summarize.cwl`, `report.cwl`) | a tie by type that naming conventions decide | [Edge inference](edge-inference.md) |
| `naming_renamed` (with `summarize.cwl`, `abstract.cwl`) | a renaming between two naming schemes | [Edge inference](edge-inference.md) |
| `two_lines` | one tool used twice, each step under its own name | [Steps and tools](steps-and-tools.md) |
| `word_count` (with `word_count.cwl`) | a tool file beside the workflow, named by path | [Steps and tools](steps-and-tools.md) |
| `greet_many` | three steps scattered over three people | [Scatter and conditions](scatter-and-conditions.md) |
| `greeting_if` | a step run only when a workflow input is true | [Scatter and conditions](scatter-and-conditions.md) |
| `record` | two outputs merged into one input, and an inline tool | [Types and records](types-and-records.md) |
| `nested` (with `make_greeting.wic`) | a workflow used as a step | [Subworkflows](subworkflows.md) |
| `watched` | a real-time analysis declaration | [Real-time analysis](realtime-analysis.md) |

More small examples, one construct each:

| Pair | Shows |
|---|---|
| `inline_literal` | a value written into the workflow |
| `workflow_input` | a value given when the workflow runs |
| `raw_cwl` | `!cwl` on a string, and its Python twin |
| `explicit_edge` | a connection written by hand |
| `named_output` | a workflow output under a name you choose |
| `inference` | an inferred connection and the note it prints |
| `renamed_step` | one tool used twice, each step under its own name |
| `scatter` | a step run once per combination of two lists |
| `when` | a step run only when a workflow input is true |

Some pairs need something from you before they run. `cat_file` reads a
`data.txt` beside it. `greeting_input`, `greeting_if`, `workflow_input`,
`when` and `raw_cwl` take a value for their workflow input from an inputs
file; the first two read `job.yml` in their own `__main__`:

```bash
sophios --yaml greeting_if.wic --inputs_file job.yml --run_local
python greeting_if.py
```

`word_count`, `greeting_graph`, `greeting_graph_inferred`, `greeting_graph_partial`, `naming` and
`naming_renamed` need their `.cwl` files copied beside them, and `nested` needs `make_greeting.wic` beside it and your
project directory on `search_paths_wic`. The others run as they are.

The tool-builder example, `examples/scripts/count_words.py`, is Python only,
since a `.wic` file cannot hold a tool built in memory; it is shown in full
in [Writing tools in Python](tool-builder.md), and a test in the repository
holds it to what that page says.

## Adding a pair

A pair is a `.wic` and a `.py` of the same stem in `docs/cookbook/`. The
`.py` defines `workflow()` returning a `Workflow` and names its tools
`ADAPTERS / '<tool>.cwl'`, with `ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'`;
the `.wic` names them by stem. A `.wic` without a
`.py` is a subworkflow a pair calls. A test in the repository picks the
pair up by its `.py` and checks the two compile alike; the CI also
type-checks `docs/cookbook/` with mypy and compiles every `.wic` there as
part of its corpus, so a pair that needs an input file is listed under
`fragments` in `docs/cookbook/config_ci.json`.
