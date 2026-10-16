# Cookbook

Where do I find a working example of each construct, in both forms? Every
example below is a pair: `docs/cookbook/<name>.wic` and
`docs/cookbook/<name>.py` in the Sophios repository, which compile to the same
CWL, the same job file and the same notes. A test compiles every pair both
ways and fails if they differ, so an example here says the same thing to a
reader of either form. The pages that explain each construct show these same
files.

## Running an example

From the working directory of [Install](install.md), with the two files
copied in:

```bash
sophios --yaml greeting.wic --run_local
python greeting.py
```

Each `.py` defines `workflow()` and runs it under `__main__`; the `.wic` finds
its tools on `search_paths_cwl`, and the `.py` under `cwl_adapters/` in the
working directory. `nested.wic` calls `make_greeting.wic`, which it finds on
`search_paths_wic`, so copy both files and list the working directory there, as
[Install](install.md) describes. To compile without running, use
`--generate_cwl_workflow`, or in Python:

```python
from greeting import workflow

compiled = workflow().compile()
print(compiled.to_cwl_yaml())
print(compiled.to_job_inputs_yaml())
```

Three things hold for every pair, and [Compiling to CWL](compiling-to-cwl.md)
explains them: each step keeps its id; a literal
becomes a workflow input named `<step>__<port>` with its value in the job
file; and every step output is exposed as a workflow output, except that a
Python workflow which names outputs keeps only those.

## The pairs

| Pair | Shows | Explained in |
|---|---|---|
| `greeting` | three steps with two inferred edges; the first workflow | [Your first workflow](first-workflow.md) |
| `inline_literal` | a value written into the workflow | [Inputs and values](inputs-and-values.md) |
| `workflow_input` | a value given when the workflow runs | [Inputs and values](inputs-and-values.md) |
| `cat_file` | a `File` literal, and where its relative path points | [Inputs and values](inputs-and-values.md) |
| `raw_cwl` | `!cwl` on a string, and its Python twin | [Inputs and values](inputs-and-values.md) |
| `explicit_edge` | an edge written by hand | [Connecting steps](connecting-steps.md) |
| `named_output` | a workflow output under a name you choose | [Connecting steps](connecting-steps.md) |
| `inference` | an inferred edge and the note it prints | [Edge inference](edge-inference.md) |
| `renamed_step` | one tool used twice, each step under its own name | [Steps and tools](steps-and-tools.md) |
| `word_count` | a tool named by path, beside the workflow | [Steps and tools](steps-and-tools.md) |
| `record` | two sources merged into one input, and an inline tool | [Types and records](types-and-records.md) |
| `scatter` | a step run once per combination of two arrays | [Scatter and conditions](scatter-and-conditions.md) |
| `when` | a step run only when a workflow input is true | [Scatter and conditions](scatter-and-conditions.md) |
| `nested` (with `make_greeting.wic`) | a workflow used as a step | [Subworkflows](subworkflows.md) |
| `watched` | a real-time analysis declaration | [Real-time analysis](realtime-analysis.md) |

`cat_file`, `raw_cwl`, `when` and `workflow_input` need something from you
before they run: a `data.txt` beside them, or a value for their workflow
input through `--inputs_file`. `word_count` needs `word_count.cwl` copied
beside it, and `nested` needs `make_greeting.wic` beside it and the working
directory on `search_paths_wic`. The others run as they are.

## Adding a pair

A pair is a `.wic` and a `.py` of the same stem in `docs/cookbook/`. The
`.py` defines `workflow()` returning a `Workflow` and names its tools
`cwl_adapters/<tool>.cwl`; the `.wic` names them by stem. A `.wic` without a
`.py` is a subworkflow a pair calls. `tests/core/test_cookbook.py` picks the
pair up by its `.py` and checks the two compile alike; the CI also type-checks
`docs/cookbook/` with mypy and compiles every `.wic` there as part of its
corpus, so a pair that needs an input file is listed under `fragments` in
`docs/cookbook/config_ci.json`.
