# Edge inference

Workflows are graphs, but long stretches of most of them are straight
lines, where each step reads what the step before it wrote. Writing every one
of those connections down, as you have done so far, says nothing a reader
could not guess. In this chapter you will delete the
connections from the greeting workflow and let Sophios fill them in. This is
called **edge inference**. You will see the note Sophios prints when it had
a choice to make, learn the rule it follows, and learn how to take the
choice back when it is not the one you want.

## Leave the connections out

Here is the greeting workflow with every connection deleted. Save it as
`greeting_inferred.wic`, and the Python form as `greeting_inferred.py`:

```{literalinclude} cookbook/greeting_inferred.wic
:language: yaml
:caption: greeting_inferred.wic
```

```{literalinclude} cookbook/greeting_inferred.py
:language: python
:caption: greeting_inferred.py
```

Nothing says which file `append` writes to or which file `cat` reads. Run
either form:

```bash
sophios --yaml greeting_inferred.wic --run_local
```

```bash
python greeting_inferred.py
```

Both succeed, and `greeting.txt` holds `Hello, Sophios`, exactly as before.
Sophios connected `append`'s `file` to the file `touch` made, and `cat`'s
`file` to the file `append` wrote. The compiled workflow is the one you
wrote by hand in chapter 1.

The command line also printed one line before the run:

```text
greeting_inferred.wic:8:3: note [wic043] step 'cat' input 'file' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: file: !* <name>` here (step 3 'cat', port 'file')
```

## Read the note

A **note** is Sophios telling you about a choice it made. It never stops
anything. Read this one in four parts:

- **where:** `greeting_inferred.wic:8:3`, the `cat` step;
- **what it chose:** `append`'s output `file`, the most recent match;
- **what else matched:** `touch`'s output `file`, which is also a file;
- **how to choose yourself:** the two lines to write, `!&` on the output and
  `!*` on the input.

`append`'s own input gets no note: only one earlier step, `touch`, made a
file, so there was nothing to choose.

The Python run prints no notes. They are kept on the compiled workflow, at
the line of your script that made the step:

```python
from greeting_inferred import workflow

for note in workflow().compile().diagnostics:
    print(note)
```

```text
/home/you/sophios-tutorial/greeting_inferred.py:17:1: note [wic043] step 'cat' input 'file' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: file: !* <name>` here (step 3 'cat', port 'file')
```

## Choose yourself

When the choice is the wrong one, or when it matters enough that a reader
should see it in the file, write the connection. That is what the note's
last part says to do, and it is exactly what `greeting.wic` from chapter 1
does. In Python, write the assignment, `cat.inputs.file =
append.outputs.file`. Sophios never infers an input you connected, so the
note goes away, and connecting one input changes nothing for the others.
You can mix the two freely: write the connections that matter and leave the
rest.

To make Sophios refuse to choose, turn every note into an error. On the
command line that is `--inference_strict`; in Python it is
`Inference(strict=True)`, from `sophios.api.python.workflow`, given to
`compile()` or `run()`:

```bash
sophios --yaml greeting_inferred.wic --generate_cwl_workflow --inference_strict
```

```text
Failed to compile greeting_inferred.wic
greeting_inferred.wic:8:3: error [wic043] step 'cat' input 'file' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: file: !* <name>` here (step 3 'cat', port 'file')
```

```python
from sophios.api.python.workflow import Inference

from greeting_inferred import workflow

workflow().compile(inference=Inference(strict=True))
```

In Python, `compile()` raises `SophiosError` with the same error, at the line
of your script that made `cat`:

```text
sophios.lang.diagnostics.SophiosError: /home/you/sophios-tutorial/greeting_inferred.py:17:1: error [wic043] step 'cat' input 'file' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: file: !* <name>` here (step 3 'cat', port 'file')
```

Use it for a workflow that must not depend on the order of its steps. To
turn inference off altogether, use `--inference_disable`, or
`Inference(enabled=False)`. An input nothing connects then stays
unconnected, and the runner refuses the workflow before any step runs:

```text
ERROR Tool definition failed validation:
autogenerated/greeting_inferred.cwl:20:3: Step is missing required parameter 'file'
```

## The rule

For each step, in order, and for each of its inputs that is required and
has no value and no connection, Sophios looks back through the earlier
steps, the most recent first, for an output that fits:

1. An output fits when its type matches the input's type, and its file
   format matches the input's when the input declares one. `File` matches
   `File`; an optional `File?` matches `File`; `Any` matches only `Any`.
2. The most recent step with a fitting output wins. Within that step, an
   output whose name pairs with the input's wins, as the next section shows;
   if none does, the output declared last wins.
3. If an earlier step also had a fitting output, Sophios notes the choice,
   as `wic043`. If the winning step had several, it notes that, as `wic042`.
4. If nothing fits, the input becomes an input of the compiled workflow, for
   the run to supply, and the runner asks for it.

Optional inputs are never inferred. Inference also reaches into a workflow
used as a step and out of it, as [Subworkflows](subworkflows.md) shows. The
complete rule, with how types, formats and scattered steps are compared, is
[§8 of the `.wic` reference](wic-reference.md#8-inference).

## When the graph branches

"Most recent" means the nearest step above in your list, not the branch you
had in mind. Inference reads the list; it cannot know that two steps were
meant to sit side by side. Here is the greeting graph from
[Connecting steps](connecting-steps.md) with its connections deleted:

```{literalinclude} cookbook/greeting_graph_inferred.wic
:language: yaml
:caption: greeting_graph_inferred.wic
```

```{literalinclude} cookbook/greeting_graph_inferred.py
:language: python
:caption: greeting_graph_inferred.py
```

Both forms run, and the command line explains every choice it made:

```text
greeting_graph_inferred.wic:8:3: note [wic043] step 'shout' input 'text' was inferred from the most recent match step 2 'append' output 'file'; earlier steps also match: step 1 'touch' output 'file'; pin it: `out: - file: !& <name>` on step 2 'append' and `in: text: !* <name>` here (step 3 'shout', port 'text')
greeting_graph_inferred.wic:10:3: note [wic043] step 'word_count' input 'text' was inferred from the most recent match step 3 'shout' output 'shouted'; earlier steps also match: step 2 'append' output 'file', step 1 'touch' output 'file'; pin it: `out: - shouted: !& <name>` on step 3 'shout' and `in: text: !* <name>` here (step 4 'word_count', port 'text')
greeting_graph_inferred.wic:12:3: note [wic043] step 'combine' input 'text' was inferred from the most recent match step 4 'word_count' output 'counts'; earlier steps also match: step 3 'shout' output 'shouted', step 2 'append' output 'file', step 1 'touch' output 'file'; pin it: `out: - counts: !& <name>` on step 4 'word_count' and `in: text: !* <name>` here (step 5 'combine', port 'text')
greeting_graph_inferred.wic:12:3: note [wic043] step 'combine' input 'counts' was inferred from the most recent match step 4 'word_count' output 'counts'; earlier steps also match: step 3 'shout' output 'shouted', step 2 'append' output 'file', step 1 'touch' output 'file'; pin it: `out: - counts: !& <name>` on step 4 'word_count' and `in: counts: !* <name>` here (step 5 'combine', port 'counts')
```

The first note is the choice you wanted. The second is not: `word_count`
took `shout`'s file, the nearest file above it, instead of the greeting. The
last two give both of `combine`'s inputs the counts. Drawn, the graph has
become a line:

![greeting_graph_inferred.wic: inference turned the branch into a line](cookbook/greeting_graph_inferred.wic.gv.png)

and `report.txt` holds the count of the shouted file, twice:

```text
2 /var/lib/cwl/stg2f9ef4f9-e17a-44c3-aa96-da5d815535cd/shouted.txt
2 /var/lib/cwl/stg2f9ef4f9-e17a-44c3-aa96-da5d815535cd/shouted.txt
```

Nothing failed, because every connection had the right type, which is why
the notes matter: read them, or draw the workflow, before a long run. The
rule of thumb is to leave out connections along straight stretches, and to
write them wherever the graph branches or joins, as `greeting_graph.wic`
does. A written connection is never second-guessed, so you can write just
the ones at the branch and leave the rest to inference.

## When names should decide

Types cannot always decide. A step often makes two files, and a later step
wants one of them. Here a `summarize` step writes a one-line summary of a
text and a line-numbered copy of it, as `output_summary` and
`output_details`, and a `report` step wants the summary, on its input
`input_summary`. Save the two tools beside your workflows:

```{literalinclude} cookbook/summarize.cwl
:language: yaml
:caption: summarize.cwl
```

```{literalinclude} cookbook/report.cwl
:language: yaml
:caption: report.cwl
```

The two steps that use them say `run: summarize.cwl` and `run: report.cwl`:
a tool named by its file, beside the workflow, instead of by a name Sophios
looks up. In Python, `Path(__file__).with_name('summarize.cwl')` is the same
file, beside the script. [Steps and tools](steps-and-tools.md) explains both
ways of naming a tool; here they only keep the two new tools next to the
workflow. Save the workflow, with every connection left to inference:

```{literalinclude} cookbook/naming.wic
:language: yaml
:caption: naming.wic
```

```{literalinclude} cookbook/naming.py
:language: python
:caption: naming.py
```

### The name decides

Both of `summarize`'s outputs are files, so by type they tie. Run
`sophios --yaml naming.wic --run_local`, or `python naming.py`. Among tied
outputs of the same step, Sophios takes the one whose name pairs with the
input's. It strips `input_` from the input's name and `output_` from each
output's, so `input_summary` pairs with `output_summary`. The names never
move the choice to another step: the most recent step with a fitting output
still wins. A call to a
subworkflow counts as one step here, so names choose among the outputs of the
steps inside it, and may pick one that is not from its most recent inner step. One note remains, saying that `echo`'s file came earlier and lost
to the most recent step, which is the choice you want:

```text
naming.wic:7:3: note [wic043] step 'report' input 'input_summary' was inferred from the most recent match step 2 'summarize' output 'output_summary'; earlier steps also match: step 1 'echo' output 'stdout'; pin it: `out: - output_summary: !& <name>` on step 2 'summarize' and `in: input_summary: !* <name>` here (step 3 'report', port 'input_summary')
```

`report.txt` holds the summary, the text's word count:

```text
9
```

When no output's name pairs with the input's, or several do, the output
declared last wins.

### Turning the names off

If your tools' names mean nothing, or you want the plain rule, switch the
names off with `--no_inference_naming_conventions` on the command line or
`Inference(naming_conventions=False)` in Python:

```bash
sophios --yaml naming.wic --run_local --no_inference_naming_conventions
```

```python
from sophios.api.python.workflow import Inference

from naming import workflow

workflow().run(inference=Inference(naming_conventions=False))
```

By type alone the tie goes to the output declared last, `output_details`, and
the command line says so:

```text
naming.wic:7:3: note [wic042] step 'report' input 'input_summary' was inferred from step 2 'summarize' output 'output_details', but that step also offers step 2 'summarize' output 'output_summary'; pin it: `out: - output_details: !& <name>` on step 2 'summarize' and `in: input_summary: !* <name>` here (step 3 'report', port 'input_summary')
naming.wic:7:3: note [wic043] step 'report' input 'input_summary' was inferred from the most recent match step 2 'summarize' output 'output_details'; earlier steps also match: step 1 'echo' output 'stdout'; pin it: `out: - output_details: !& <name>` on step 2 'summarize' and `in: input_summary: !* <name>` here (step 3 'report', port 'input_summary')
```

`report.txt` then holds the line-numbered copy, not the summary:

```text
     1	The quick brown fox jumps over the lazy dog
```

### A renaming, when the names follow different conventions

Tools from different groups rarely agree on names. Suppose the step that
wants the summary calls it an abstract, on an input `input_abstract`:

```{literalinclude} cookbook/abstract.cwl
:language: yaml
:caption: abstract.cwl
```

```{literalinclude} cookbook/naming_renamed.wic
:language: yaml
:caption: naming_renamed.wic
```

```{literalinclude} cookbook/naming_renamed.py
:language: python
:caption: naming_renamed.py
```

`abstract` pairs with neither output, so the names alone fall back to the
output declared last, and the `wic042` note appears. A **renaming** tells Sophios how
the two conventions correspond. It is a `[before, after]` pair, applied to
the input's name, after `input_` is stripped and before the names are
compared: with `['abstract', 'summary']`, `input_abstract` pairs with
`output_summary`. On the command line, add the pair to `renaming_conventions`
in `~/wic/global_config.json`:

```json
"renaming_conventions": [
    ["energy_", "edr_"],
    ["structure_", "tpr_"],
    ["traj_", "trr_"],
    ["abstract", "summary"]
]
```

and run it:

```bash
sophios --yaml naming_renamed.wic --run_local
```

In Python, give the pair to `Inference`:

```python
from sophios.api.python.workflow import Inference

from naming_renamed import workflow

workflow().run(inference=Inference(renamings=(('abstract', 'summary'),)))
```

```text
naming_renamed.wic:7:3: note [wic043] step 'abstract' input 'input_abstract' was inferred from the most recent match step 2 'summarize' output 'output_summary'; earlier steps also match: step 1 'echo' output 'stdout'; pin it: `out: - output_summary: !& <name>` on step 2 'summarize' and `in: input_abstract: !* <name>` here (step 3 'abstract', port 'input_abstract')
```

and `abstract.txt` holds the summary, `9`, from either form. Renamings are
part of the naming conventions, so `Inference` refuses them when the
conventions are off:

```text
sophios.api.python._errors.InvalidInferenceError: error [api007] Inference.renamings is given but naming_conventions is False, so the renamings would do nothing
error [api007] set naming_conventions=True, or drop renamings
```

`InvalidInferenceError` is importable from `sophios.api.python.workflow`,
like the other API errors. The three pairs already in the configuration are
GROMACS conventions, for the molecular-modelling tools Sophios's own tool
collections hold, which call one file `energy` on one side and `edr` on the
other.

### Conventions or a written connection?

For one connection in one workflow, write it with `!&` and `!*`, or an
assignment in Python: it is shorter than a convention, and a reader sees it
where it happens. Naming conventions pay off when a whole collection of tools
follows one naming scheme and many workflows are built from them: name the
ports `input_<thing>` and `output_<thing>`, add a renaming for each place
two schemes meet, and every workflow connects them without a written line.

One more refinement exists for collections whose file formats are declared:
`--insert_steps_automatically`, `insert_steps=True` in Python, inserts a
converter step when no output's format fits, steered by the configuration's
`inference_rules`, `format_rules=` in Python. Python reads no configuration:
its format rules and renamings are empty unless you pass them, and it inserts
only the converters among the tools in `tool_registry=`, where the command
line searches every tool on its search paths. The rules are read only for an
inserted step, so `format_rules=` without `insert_steps=True` is an error
(`api007`).
[Configuration](configuration.md) and
[§8 of the `.wic` reference](wic-reference.md#8-inference) describe it.

## Recap

Leave out an input's connection and Sophios connects it to the most recent
earlier output that fits. When another output also fitted, it says so in a
note that names the alternative and the two lines that would make the
choice yours. Write a connection to choose for yourself. `--inference_strict`, or
`Inference(strict=True)` in Python, forbids choices, and
`--inference_disable`, or `Inference(enabled=False)`, turns inference off.
When outputs tie by type, naming conventions let names decide, with
renamings where two naming schemes meet.

## Next

So far the steps have run tools that ship with Sophios, or small tool files
beside the workflow that you saved and named with `run:`. In
[Steps and tools](steps-and-tools.md) you will see where Sophios finds
tools, use one tool twice under two names, and add a tool of your own.
