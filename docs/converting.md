# Converting between `.wic` and Python

How do I get a `.wic` file from a Python workflow, and Python objects from a
`.wic` file? The two forms describe the same workflows, so Sophios converts
in both directions: `Workflow.write_wic()` writes a `.wic` bundle, and
`Workflow.from_wic()` reads one back into `Workflow` and `Step` objects. Use
them to hand a Python workflow to someone who works in files, to inspect what
a Python script says before compiling it, to bring a `.wic` workflow into a
program, or to compile a Python workflow with command-line options that
`compile()` does not take.

## Python to `.wic`: `write_wic()`

```python
from greeting import workflow

flow = workflow()
flow.write_wic('bundle')
```

`write_wic(path)` takes a `.wic` file path or a directory; with no argument
it writes `<name>.wic` in the working directory. It writes a bundle into one
directory:

```text
bundle/
  greeting.wic     the root workflow
  touch.cwl        one <stem>.cwl per distinct tool
  append.cwl
  cat.cwl
```

```yaml
# bundle/greeting.wic
steps:
- id: touch
  in:
    filename: !ii greeting.txt
  run: touch.cwl
- id: append
  in:
    str: !ii Hello, Sophios
  run: append.cwl
- id: cat
  run: cat.cwl
```

What it writes:

- Steps in the sequence form, each with `id:` and `run: <stem>.cwl`, the tool
  written beside the document. Literals are `!ii`, edges are `!&` and `!*`,
  workflow inputs are bare names under `inputs:`, records are `!cwl {...}`,
  and `scatter`, `scatterMethod` and `when` as written.
- Named outputs under `outputs:`, each `outputSource: <step>/<port>` with the
  step's authored name.
- One `<child>.wic` per nested workflow, called by name, as a `.wic`
  subworkflow is. Put the bundle's directory on `search_paths_wic` to compile
  a bundle that nests, since a nested `.wic` is found on the search paths.
- An input left to inference stays unbound, so the compiler infers it when
  the bundle is compiled.
- A real-time analysis declaration carries `run: cwl_subinterpreter`, the
  adapter's registry stem, which is not written: a step is a declaration only
  when it runs the adapter the registry holds.

Only a tool's own document is written; files it refers to by relative path,
such as an `$import`, are not copied. A `<stem>.cwl` already in the directory
is left alone when it is the same tool, and is an error when it is a different
one. Nothing is compiled: `write_wic` does not write CWL, and the bundle is
the source, to compile with `sophios --yaml bundle/greeting.wic`.

## `.wic` to Python: `from_wic()`

```python
from sophios import plugins
from sophios.api.python.workflow import Workflow

config = {'search_paths_cwl': {'global': ['cwl_adapters']}}
flow = Workflow.from_wic('greeting.wic', tool_registry=plugins.get_tools_cwl(config))

(touch, append, cat) = flow.steps
print(append.inputs.str.value)      # Hello, Sophios
compiled = flow.compile()
```

`from_wic(path, tool_registry=..., workflow_paths=...)` reads the file the
way `sophios --yaml` reads it, and nothing else. A step's tool comes from
`tool_registry` by stem, or from what its `run:` names; a nested
`<child>.wic` comes from `workflow_paths`, shaped `{namespace: {stem: path}}`
like `sophios.plugins.get_yml_paths(config)`. It reads no configuration of
its own; the bundle `write_wic` writes needs no registry, since every step
names its tool by path.

The file is compiled first, and a file that does not compile raises the same
errors the command line prints. Then each step becomes a `Step` holding its
tool, or a nested `Workflow`:

- `!ii` becomes a literal, `!*` the producing step's output object, a bare
  name a workflow input, and `!cwl {...}` a `StepInput`;
- an input the file leaves to inference stays unbound, and `compile()` infers
  it;
- every object carries the `.wic` line it was read from, which is where a
  later error about it is reported.

The objects compile to the CWL the file compiles to, and `write_wic()` of
them writes a bundle that compiles to the same.

### What has no Python spelling

Some `.wic` constructs have no twin in the Python API. `from_wic()` refuses
them with `api006`, all at once, each with the line it is on and what to
write instead:

- a step id used twice in one document (a Python workflow names each step
  once): give one step its own `id:` and `run: <tool>.cwl`;
- a `!*` reading an edge defined in another document, or on a later step;
- a top-level key other than `inputs:`, `outputs:` and `steps:`, and a port
  field other than `type` (and an output's `outputSource`), such as `format`;
- a port name that is not a Python identifier, such as `ref-t`;
- step keys Sophios passes through (`label:`, `hints:`, ...), `scatter:`
  without `scatterMethod:`, and `scatter:` or `when:` on a `.wic` call;
- `run:` naming a `.wic`, and `!cwl` text that is not a declared input;
- one `.wic` called from two places whose `wic: steps:` entries give it
  different bodies (a nested Python workflow is named after its file): copy
  the file for one of the calls;
- `wic:` plugin namespaces, `implementations`, `inference` rules and
  `lang_version` pins (pass `compile(lang_version=...)` instead).

`wic: graphviz` and `wic: inlineable` change nothing a Python workflow
compiles or runs; they are dropped with a `UserWarning` naming the file.

## Round trips

`write_wic` then `from_wic` gives back a workflow that compiles to the same
CWL; so does `from_wic` then `write_wic`. Names are kept: step names, port
names, edge names you wrote. Positions are not, since a Python object has no
file of its own until it is written.

One asymmetry is worth knowing. A Python workflow that names outputs compiles
to those outputs only, while the command line also exposes every step output.
A bundle written from such a workflow and compiled on the command line
therefore has more outputs than `compile()` returned; the named ones are the
same.
