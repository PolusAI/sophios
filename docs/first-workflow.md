# Your first workflow

In this chapter you will write a workflow of three steps, run it, and find
what it made. You will write it twice, once as a `.wic` file and once as a
Python script, so that you can see the two say the same thing and choose the
one that suits you. Along the way you will meet the three ideas every Sophios
workflow is built from: steps, the values you give them, and the connections
between them.

You need a machine set up as in [Install](install.md).

## The pipeline

The pipeline in this guide is small on purpose. It makes an empty file,
writes a greeting into it, and reads the file back. Each step runs in about a
second, so you can watch everything it does. Its three steps form a straight
line, the simplest shape a workflow can have: one program makes a file, the
next changes it, and the next reads it. A real analysis is a graph, where one
result feeds several steps and a later step joins them, and later chapters
grow the greeting into one, a single idea at a time: it will take its
greeting when you run it, branch into two steps and join again, greet
several people at once, and count its own words.

## Set up a project directory

Sophios writes its results into the directory you run it from, so give the
guide a directory of its own:

```bash
mkdir sophios-tutorial
cd sophios-tutorial
```

A **tool** is a short file that describes one command-line program: what the
program is called, what it takes, what it produces, and which container it
runs in. [Install](install.md) put the tools that ship with Sophios in
`~/wic/cwl_adapters/`. Here is `~/wic/cwl_adapters/touch.cwl`, which
describes `touch`:

```{literalinclude} ../cwl_adapters/touch.cwl
:language: yaml
:caption: ~/wic/cwl_adapters/touch.cwl
```

It runs `touch` in the container image
`docker.io/library/debian:bookworm-slim`. It takes one input, `filename`, a
string, and it has one output, `file`, the file that `touch` creates. Tools
are written in the [Common Workflow Language](https://www.commonwl.org/)
(CWL), and many scientific programs already have one. You will not write a
tool until [Writing tools in Python](tool-builder.md); this chapter uses
three that ship with Sophios: `touch`, `append`, which adds a line of text
to a file, and `cat`, which reads a file back.

The `.wic` form of the workflow refers to a tool by its name alone, and
Sophios finds it among the tools it ships. The Python form refers to each
tool by its path, starting from `~/wic/cwl_adapters/`.

## Write the workflow

Save this as `greeting.wic` in `sophios-tutorial/`:

```{literalinclude} cookbook/greeting.wic
:language: yaml
:caption: greeting.wic
```

A `.wic` file is YAML. Under `steps:`, each entry that starts with `- id:`
is a **step**. A step's id names the tool it runs: `touch` runs `touch.cwl`.
[Steps and tools](steps-and-tools.md) explains where Sophios looks for
tools, and how to add places of your own.

Under `in:`, a step gets its inputs. `touch` has one input, `filename`, and
`filename: !ii greeting.txt` gives it the value `greeting.txt`. The `!ii`
tag, short for *inline input*, marks a value written into the workflow
itself. It matters: you will see what happens without it at the end of this
chapter. `append` gets a value the same way, the line of text
`Hello, Sophios`.

The other lines are **connections**. Under `out:`, a step can give one of
its outputs a name: `- file: !& empty_file` gives `touch`'s output `file`
the name `empty_file`. In `append`, `file: !* empty_file` feeds that file
into `append`'s own input called `file`. `!&` defines a name and `!*` uses
it, so you can read every connection by matching the two. `append` names its
result `greeting_file` in turn, and `cat` reads it.

These connections also set the order: each step needs the file the one
before it made, so the runner runs the three one after another.

Now the same workflow in Python. Save it as `greeting.py`:

```{literalinclude} cookbook/greeting.py
:language: python
:caption: greeting.py
```

`ADAPTERS` holds the directory of the tools that ship with Sophios,
`~/wic/cwl_adapters` (`C:\Users\<you>\wic\cwl_adapters` on Windows;
`Path.home()` is the same place `sophios --generate_config` wrote to), and
`Step(clt_path=ADAPTERS / 'touch.cwl')` loads one of them and makes a step
that runs it. `clt_path` takes a path, so `/` joins the directory and the
file name, on any operating system. The step's inputs and outputs are the
tool's, under `inputs` and `outputs`: `touch.inputs.filename`, `touch.outputs.file`. Assigning a value
to an input gives it that value. Assigning another step's output to an input
connects them: `append.inputs.file = touch.outputs.file` is the Python
spelling of `!& empty_file` and `!* empty_file`. Finally,
`Workflow([touch, append, cat], 'greeting')` puts the steps in order and
names the workflow `greeting`.

The script builds the workflow in a function, `workflow()`, so that another
script can import it without running it. The last two lines run it when you
run the file itself.

The two files describe the same workflow, and Sophios compiles them to
exactly the same result. Use whichever you prefer: a `.wic` file is short and
easy to share, and a Python script can build a workflow with loops and
functions.

## Run the workflow

From `sophios-tutorial/`, run either form:

```bash
sophios --yaml greeting.wic --run_local
```

```bash
python greeting.py
```

The `.wic` run prints a good deal. Here it is, with the long lines and the
repetitive middle cut short:

```text
 starting compilation of greeting
finishing compilation of greeting
Sophios lang_version: 0.0.1
Running cwltool_filterlog --disable-pull --provenance /home/you/sophios-tutorial/autogenerated/provenance/greeting ...
...
INFO [workflow ] starting step touch
INFO [step touch] start
INFO [job touch] /private/tmp/docker_tmpdc0ozv4p$ docker \
    run \
    ...
    docker.io/library/debian:bookworm-slim \
    touch \
    greeting.txt
INFO [job touch] completed success
INFO [step touch] completed success
INFO [workflow ] starting step append
INFO [step append] start
INFO [job append] /private/tmp/docker_tmp20b16wra$ /bin/sh \
    -c \
    echo Hello, Sophios >> /private/tmp/docker_tmp20b16wra/greeting.txt
INFO [job append] completed success
...
INFO [workflow ] completed success
...
Success! Runner outputs are under /home/you/sophios-tutorial/autogenerated/outdir_cwltool_greeting_2026_10_18_22.20.57/
```

### What just happened

Sophios did four things.

First, it read `greeting.wic`, checked it, and **compiled** it: it
translated your workflow into CWL, the same language the tools are written
in, which programs called workflow runners know how to execute. That is the
work between `starting compilation` and `finishing compilation`.

Second, it checked that this machine can run the result: that Docker is
running and that every file the workflow reads exists. Then it fetched
`docker.io/library/debian:bookworm-slim`, the image `touch` and `cat` name.
Sophios asks for the image before every run, so the first run downloads it
and later runs only confirm it is current.

Third, it handed the compiled workflow to **cwltool**, a CWL runner; that
is the line that begins `Running`. Each `INFO` line is cwltool's log: a
step starting, the exact command it ran, and whether it succeeded. `touch`
and `cat` ran inside `docker.io/library/debian:bookworm-slim`. `append`'s
tool names no container, so it ran directly on your machine, as
`/bin/sh -c echo ...`. The log names every step by the id you gave it,
`[step append]`.

Fourth, it reported where the results are, on the last line.

The Python run does the same work and prints less:

```text
 starting compilation of greeting
finishing compilation of greeting
Running cwltool_filterlog --disable-pull --quiet --provenance /home/you/sophios-tutorial/autogenerated/provenance/greeting ...
...
Final output json metadata blob is in output_greeting.json
Success! Runner outputs are under /home/you/sophios-tutorial/autogenerated/outdir_cwltool_greeting_2026_10_18_22.21.01/
```

`run()` passes `--quiet` to cwltool, so the step-by-step log is hidden. To
see it, run with `workflow().run(run_args_dict={'quiet': 'no'})`. If a step
fails, `run()` raises `WorkflowRunError` instead of printing `Success!`, so a
script that runs a workflow can catch the failure like any other exception.

## Find the results

Look in the directory the last line names. Its name ends with the date and
time of your run, so yours differs from the one here:

```bash
cat autogenerated/outdir_cwltool_greeting_2026_10_18_22.20.57/greeting.txt
```

```text
Hello, Sophios
```

Sophios keeps everything about a run under one directory, its **base
path**. Unless you choose another, the base path is `autogenerated/` in the
directory you ran Sophios from. After both runs, it holds:

```text
autogenerated/
  outdir_cwltool_greeting_2026_10_18_22.20.57/   the files the .wic run produced
  outdir_cwltool_greeting_2026_10_18_22.21.01/   the files the Python run produced
  output_greeting.json                           every output of the latest run, with its value
  greeting.cwl                                   the compiled workflow
  greeting_inputs.yml                            the values the compiled workflow runs with
  greeting~touch.cwl, greeting~append.cwl, ...   the tool each step runs, from the .wic run
  provenance/                                    cwltool's record of the run
```

The command line writes each step's tool as a file of its own beside the
workflow; Python writes one `greeting.cwl` with the tools inside it.

Each run's files go into a new **output directory**, named for the runner,
the workflow and the time the run started, so a later run does not overwrite
an earlier one. The compiled files are rewritten by each run;
[Compiling to CWL](compiling-to-cwl.md) reads them line by line.

`output_greeting.json` lists every output of every step. Shortened, it reads:

```text
{
    "append~file": {
        ...
        "basename": "greeting.txt",
        "class": "File",
        ...
        "size": 15,
        ...
    },
    "cat~output": "Hello, Sophios\n",
    "touch~file": {
        ...
        "basename": "greeting.txt",
        ...
    }
}
```

Each output is listed under a name Sophios made from the step and the
output, joined with `~`: `cat~output` is the output called `output` of the
step `cat`. `cat`'s tool returns the text of the file rather than a file, so
its value is the greeting itself. You will meet names of this shape in
messages from the runner, and they always read the same way.

When you write such a name in Markdown, in an issue or a README on GitHub,
put it in a code span, `` `cat~output` ``: GitHub renders text between two
`~` as struck through.

### Choose where the results go

The base path and the output directory are defaults, and you can change
both. By default the base path is `autogenerated/` under the directory you
run Sophios from, wherever the workflow file is. `--basepath` on the command
line, or `basepath=` in Python, names another directory, relative to the
current one, and everything Sophios generates for the run goes there.
`--outdir`, or `outdir` in `run_args_dict`, names a directory for only the
files the run produces:

```bash
sophios --yaml greeting.wic --run_local --basepath build --outdir results
```

```python
from greeting import workflow

workflow().run(basepath='build', run_args_dict={'outdir': 'results'})
```

Either run keeps its compiled files, `output_greeting.json` and the record
of the run in `build/`, writes `greeting.txt` to `results/`, and creates no
`autogenerated/`. The last line says where the results are:

```text
Success! Runner outputs are under /home/you/sophios-tutorial/results/
```

## When something goes wrong

Mistakes are part of writing workflows, so it is worth seeing what one looks
like. Open `greeting.wic` and delete the `!ii` before `greeting.txt`:

```yaml
- id: touch
  in:
    filename: greeting.txt
```

Run it again:

```text
Failed to compile greeting.wic
greeting.wic:4:15: error [wic011] Did you forget to use !ii before greeting.txt? (step 1 'touch', port 'filename')
```

Sophios stopped before running anything. The line starts with where the
problem is, as file, line and column, then says how serious it is (`error`
means the compile stopped), gives a code, `wic011`, and ends with the step
and the input it is about. Without the tag, Sophios reads `greeting.txt` as
the name of a value to be supplied when the workflow runs, a **workflow
input**, which [Inputs and values](inputs-and-values.md) introduces. This
workflow declares no such input, so Sophios asks whether you meant `!ii`:
put it back.

Every code has a short explanation, which `--explain` prints:

```bash
sophios --explain wic011
```

```text
wic011 (document): An untagged input value names no workflow input.
Fix: Write a literal as `!ii <value>`, or declare the name under `inputs:` (.wic reference §3.3).
```

Python catches mistakes as you make them. Give an input the wrong name in
`greeting.py`, say `touch.inputs.file_name = 'greeting.txt'`, and the script
stops at that line:

```text
Traceback (most recent call last):
  ...
  File "/home/you/sophios-tutorial/greeting.py", line 14, in workflow
    touch.inputs.file_name = 'greeting.txt'
    ^^^^^^^^^^^^^^^^^^^^^^
  ...
AttributeError: 'touch' has no input named 'file_name'
```

Problems can also turn up later: a machine that cannot run the workflow, or
a program that fails while it runs. [Checks and errors](checks-and-errors.md)
shows each kind and how to read it.

## Recap

A workflow is a list of steps, and each step runs a tool, a file that
describes one program. You give a step's inputs values with `!ii` in a
`.wic` file, or by assignment in Python. You connect one step's output to
another's input by naming it with `!&` and using it with `!*`, or in Python
by assigning the output to the input. Both
`sophios --yaml greeting.wic --run_local` and `python greeting.py` compile
the workflow, check the machine and run it. The results land under the base
path, which is `autogenerated/` in the directory you run from unless you
choose another.

## Next

Your pipeline writes the same greeting every time it runs. In
[Inputs and values](inputs-and-values.md) you will make the greeting
something you give it when you run it.
