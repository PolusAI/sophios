# Checks and errors

Something went wrong, and you want to know where and what to change. This
page shows you how to read every kind of message Sophios prints, which part
of the system each comes from, and how to find problems before a long run
instead of during it.

Sophios checks a workflow in layers, and each layer stops before the next
starts: the shape of the file, then what the file means against the tools it
names, then what a run needs from this machine, and only then does a runner
start. Every problem Sophios finds carries a code, a position when it has
one, and what to do. Problems that belong to another program, a YAML parser,
a CWL loader or the runner, are passed through as that program reports them.

## Reading a diagnostic

```text
greeting.wic:4:15: error [wic011] Did you forget to use !ii before greeting.txt? (step 1 'touch', port 'filename')
```

Left to right: the file, line and column; the severity (`error`, or `note`
for a choice Sophios made and reports without stopping); the code; the
message; and the step by position and name, and the port, when Sophios knows
them. In Python the position is `script.py:line:column` of the statement
that caused it. A compile reports every error it found, then prints
`Failed to compile <file>` and exits with status 1; `Workflow.compile()`
raises `SophiosError`, whose `.diagnostics` hold the same objects.

`sophios --explain <code>` says what a code means and what to change:

```text
$ sophios --explain wic011
wic011 (document): An untagged input value names no workflow input.
Fix: Write a literal as `!ii <value>`, or declare the name under `inputs:` (.wic reference §3.3).
```

The word in parentheses is the kind: what you change. `document` is the
`.wic` file or the Python workflow, `call` the Python API call, `machine`
this machine, `run` nothing in Sophios. [Error codes](error_codes.md) is the
whole catalog.

## The layers

The shape of a message tells you where to look before you read a word of
it. Four shapes cover nearly everything you will see.

A line that starts with a file, a line and a column comes from compiling
your workflow, and the fix is in that file, at that line:

```text
greeting.wic:4:15: error [wic011] Did you forget to use !ii before greeting.txt? (step 1 'touch', port 'filename')
```

A line with a code but no position comes from checking this machine before
a run; the fix is on the machine, a stopped engine or a missing file, not in
the workflow:

```text
error [wic016] input 'cat~file' (from the workflow, whose relative paths are read from /home/you/sophios-tutorial) names 'data.txt', which does not exist at /home/you/sophios-tutorial/data.txt: correct the path, or create the file.
```

`ERROR` and `WARNING` lines followed by a `Failure!` line come from the run:
a tool failed, and its own messages, above the `Failure!` line and under the
step's name, say why:

```text
WARNING [job fail] exited with status: 1
...
Failure! step 'fail' exited with status 1. Its own messages are above, under its name.
```

A Python exception whose message carries a code starting `api` comes from a
call to the Python API, at the line of your script it names:

```text
sophios.api.python._errors.InvalidStepError: /home/you/sophios-tutorial/backwards.py:5:1: error [api002] cat.file is linked to 'touch', which must appear earlier in the workflow step list
```

The API errors, `InvalidStepError` among them, are importable from
`sophios.api.python.workflow`.

Anything else, such as a YAML parser's message quoted inside `wic001`, or a
traceback, belongs to the program that printed it; the sections below say
how Sophios passes those on.

In detail, layer by layer:

| Layer | When | What it checks | Codes |
|---|---|---|---|
| Parse | as each `.wic` file is read | it is YAML, a mapping, with keys of the right shape, known tags in the right places, a well-formed `wic:` block | `wic001`–`wic010`, `wic019`, `wic024`, `wic030`, `wic033`–`wic035`, `wic038` |
| Compile | after every file is read | every step names a tool or subworkflow that exists, every input and output a port it declares, every reference something defined, every literal a value of its type, every version pin a known one | `wic011`–`wic014`, `wic017`, `wic018`, `wic020`, `wic022`–`wic028`, `wic031`, `wic032`, `wic036`, `wic039`, `wic044`; notes `wic042`, `wic043`, and under `--target nextflow` `wic046` |
| Pre-flight | before a local run pulls or runs anything; the base path is checked before every compile | the container engine, input paths, writable directories, installed programs; then the image pulls | `wic015`, `wic016`, `wic021`, `wic029`, `wic037` |
| Run | while the runner runs | nothing of Sophios's; a tool failed | `api005` in Python; `Failure!` lines on the command line |
| The Python call | at the assignment or call | a value an input cannot take, a step the workflow cannot place, a link between mismatched ports, a tool that will not load, a `.wic` construct with no Python spelling, contradictory inference settings, real-time analyses given to `run_async` | `api001`–`api004`, `api006`–`api008` |

Python workflows skip the parse layer, since they build the document in
memory; everything from the compile layer on is the same, reported at the
script line that made the step or the binding.

### Parse

A file that is not valid YAML is `wic001`, with the YAML parser's
own message and position quoted inside. The rest of the layer is about
shape: `steps: 3` is `wic003`, a step in a sequence without `id:` is
`wic006`, a `!&` on an input is `wic019`, an unknown tag is `wic009`, and a
`wic:` key the block does not have is `wic033`. These are reported with the
exact position, and several at once.

### Compile

The compile knows the tools, so it checks names against them: a
step whose tool is on no search path (`wic013`), an input the tool does not
declare (`wic028`), a workflow input nothing declares (`wic011`), an edge
reference nothing defines (`wic025`), a literal of the wrong type (`wic020`),
a reference whose type can never fit (`wic023`). Inference runs here too, and
its notes (`wic042`, `wic043`) report the choices it made; `--inference_strict`
makes them errors. A real-time analysis that does not compile is `wic044` at
its declaration.

### Pre-flight

Mostly for a local run, since it asks about this machine; the base path
is checked before every compile (`wic021`), and `--graphviz` without `dot`
is a `wic029` note on any compile. Pre-flight
reports everything it finds at once, so one run of `--check` lists every
missing file and the stopped engine together:

```text
error [wic016] input 'cat~file' (from the workflow, whose relative paths are read from /home/you/sophios-tutorial) names 'data.txt', which does not exist at /home/you/sophios-tutorial/data.txt: correct the path, or create the file.
error [wic015] docker is installed, but `docker info` exited with status 1 (Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?): run `docker info` to see why, and start the engine if it is stopped.
```

`wic016` lines name the input by its name in the compiled workflow,
`cat~file`, the input `file` of the step `cat`, and say where
Sophios looked, which depends on where the path was written;
[Inputs and values](inputs-and-values.md) has the rule. After the checks,
the image pulls: `wic037` for one that failed, with the engine's last line
quoted.

### Run

A tool that fails is the runner's to report, and its messages name each step
by its id, `[step append]`. Sophios adds, after the run, one `Failure!` line
per failed step with its exit status and, for 126, 127 and 137, what that
status means:

```text
WARNING [job fail] exited with status: 1
WARNING [job fail] completed permanentFail
...
Failure! step 'fail' exited with status 1. Its own messages are above, under its name.
```

The step's own output is above, under its name. In Python the run raises
`WorkflowRunError` with the exit code. The runner also validates the compiled CWL when it loads it,
which is where a type relation Sophios could not decide (`Any`, a record, a
named schema) is settled, and where a missing workflow input is reported
(`Missing required input parameter`).

### The Python call

`api001` is a value an input cannot take, such as a
string where a port was meant or a list holding a port object; `api002` a
step the workflow cannot place (a repeated name, a source later in the list
or outside the workflow, an unbound output, two tools sharing a stem);
`api003` a link between ports whose types are provably disjoint, or from a
workflow's own output; `api004` a tool file that will not load; `api006` a
`.wic` construct `from_wic()` cannot hold; `api007` an `Inference` whose
settings cannot work together, such as renamings without naming
conventions or format rules without step insertion; `api008` a workflow
that declares real-time analyses given to `Workflow.run_async()`, which
never runs them, one per declaration step. The first four are raised as
`InvalidInputValueError`, `InvalidStepError`, `InvalidLinkError` and
`InvalidCLTError`, subclasses of `SophiosError` importable from
`sophios.api.python.workflow`, each carrying the script line; `api006` is a
`SophiosError` holding every refusal at once, and `api007` is an
`InvalidInferenceError`, raised when the `Inference` is made; `api008` is a
`SophiosError`, raised before anything is written or run. Ordinary mistakes in the call
itself (`TypeError` for a wrong argument type, `AttributeError` for an input
the tool does not have, `ValueError` for a bad `when` or scatter method) stay
the built-in exceptions.

## What is passed through

Sophios explains its own layer and nothing below it:

- a YAML error is quoted as the YAML parser wrote it, inside `wic001`;
- an invalid tool file is reported by cwltool's loader, wrapped in `api004`
  (Python) or found by `--validate_plugins`;
- a workflow the runner refuses when it loads the compiled CWL is reported by
  the runner, before any step runs;
- a step that fails is reported by the runner and its tool, with the
  `Failure!` summary after;
- a failure Sophios did not expect prints one line naming the exception and
  writes the traceback to `error_<workflow>.txt` in the working directory,
  with the address to report it at. That line is not about your workflow.

## Checking without running

Two commands find problems without running anything. Compiling alone answers
"is this workflow right?", and needs no container engine, no images and no
input files:

```bash
sophios --yaml greeting_inferred.wic --generate_cwl_workflow
```

```python
from greeting_inferred import workflow

compiled = workflow().compile()
for note in compiled.diagnostics:
    print(note)
```

The command line prints every note and error on stderr and writes the CWL
into the base path; `compile()` raises `SophiosError` on an error, and keeps
the notes in `compiled.diagnostics`, as [Edge inference](edge-inference.md)
showed.

`--check` goes one layer further: it compiles, then makes every machine
check a run would make, and writes, pulls and runs nothing:

```bash
sophios --yaml greeting.wic --check
```

```text
Checked greeting: nothing in the way of a local run on this machine. Nothing was written, pulled or run.
```

It answers "would this run here?" in one pass, with every problem listed.
Run it on a new machine before a long run; an agent that writes workflows
can run it after each one. In Python, `run()` makes the same checks before
it starts.

## For programs: `--diagnostics json`

`--diagnostics json` prints each diagnostic as one JSON object per line on
stderr, with the same keys every time; a value Sophios does not know is
`null`:

```text
{"severity": "error", "code": "wic011", "kind": "document", "message": "Did you forget to use !ii before greeting.txt?", "fix": "Write a literal as `!ii <value>`, or declare the name under `inputs:` (.wic reference \u00a73.3).", "file": "bad.wic", "line": 4, "column": 15, "end_line": 4, "end_column": 27, "step": "touch", "index": 1, "port": "filename"}
```

`fix` is the catalog's fix for the code, so a program need not look it up. In
Python, `Diagnostic.to_json()` gives the same object.

## Recap

A diagnostic reads left to right: where, how serious, the code, the message,
and the step and port. `sophios --explain <code>` says what a code means and
what to change. Problems surface in four places, the file, the compile, the
machine and the run, and Sophios explains its own layers and passes the
others through as their programs report them. `--check` finds every machine
problem at once, before a long run.

## Next

[Running workflows](running.md) covers the choices a run gives you, and
[Error codes](error_codes.md) lists every code with its meaning and fix.
