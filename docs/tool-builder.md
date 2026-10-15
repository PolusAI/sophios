# Writing tools in Python

How do I turn a command-line program into a tool a workflow can run? A tool
is a CWL `CommandLineTool`: the command, its inputs and how each reaches the
command line, its outputs and how each is collected, and what it needs to run
(a container, memory, a GPU). `sophios.api.python.tool_builder` writes that
document from Python objects, validates it with cwltool, and hands it to a
workflow without a file in between. This page goes from a command line to a
validated tool, one part of the document at a time. For the parts of CWL the
builder does not change, the rules the runner applies to files, outputs and
expressions, read [The CWL you still need](cwl-you-still-need.md) next.

## From a command line to a tool

Take `wc -w greeting.txt > counts.txt`: a program, one file argument, and
standard output captured to a file.

```python
from sophios.api.python.tool_builder import (
    CommandLineTool, Input, Inputs, Output, Outputs, cwl,
)

word_count = (
    CommandLineTool('word_count',
                    Inputs(text=Input(cwl.file, position=1)),
                    Outputs(counts=Output(cwl.file, glob='counts.txt')))
    .base_command('wc', '-w')
    .stdout('counts.txt')
    .docker('debian:bookworm-slim')
)
```

The constructor takes the tool's contract, its name, its inputs and its
outputs, and nothing else; everything after it is one chained call per part
of the document. Read it as:

- `word_count` is the tool's `id`, and the name of a step that runs it;
- `Inputs(text=...)`: the input is called `text`, from the keyword; it is a
  `File`, given as the first positional argument;
- `Outputs(counts=...)`: the output is called `counts`; it is the `File`
  named `counts.txt` in the working directory when the tool exits;
- `base_command('wc', '-w')`: the program and the words that always follow
  it, one word per argument;
- `stdout('counts.txt')`: the file standard output is written to;
- `docker('debian:bookworm-slim')`: the image the runner pulls and runs the
  command in.

Three things come out of the object:

```python
word_count.validate()                                 # cwltool loads it, as a runner would
word_count.write_cwl('word_count.cwl', validate=True) # the .cwl file, validated
count = word_count.to_step()                          # a workflow Step, no file written
```

`to_cwl_document()` is the document as a mapping and `to_cwl_yaml()` as
text, for when you want to read what will be written. The written file is
what a `.wic` step runs, by stem from a search path or by `run:` path; the
[word_count pair](steps-and-tools.md) runs this one.

## Inputs

An input answers two questions: what kind of value is it, and how does the
program receive it?

```python
inputs = Inputs(
    reads=Input(cwl.file, position=1),
    threads=Input(cwl.int, flag='--threads', default=4),
    model=Input(cwl.file, flag='--model', required=False),
    verbose=Input(cwl.boolean, flag='-v'),
    tags=Input(cwl.array(cwl.string), flag='--tag', item_separator=','),
    region=Input(cwl.string),
)
```

The type is a value of the `cwl` namespace (`cwl.int`, `cwl.float`,
`cwl.string`, `cwl.boolean`, `cwl.file`, `cwl.directory`, and `null`, `long`,
`double`), a composite made from them (`cwl.array(t)`, `cwl.optional(t)`,
`cwl.enum('a', 'b')`, `cwl.record(Fields(...))`), or a CWL type string
(`'File?'`, `'string[]'`, `'Any'`). `Input.array(t, ...)`,
`Input.enum('a', 'b', ...)` and `Input.record(...)` build the composite and
the input in one call.

How the value reaches the command line is the `inputBinding`:

| Keyword | CWL | Meaning |
|---|---|---|
| `position=1` | `position` | the sort key among inputs and arguments; lower comes first |
| `flag='--threads'` | `prefix` | a flag written before the value |
| `separate=False` | `separate` | join the flag and the value into one word (`--threads=4`) |
| `item_separator=','` | `itemSeparator` | join an array into one word |
| `value_from='$(self * 1024)'` | `valueFrom` | put the expression's result on the command line instead |
| `shell_quote=False` | `shellQuote` | hand the value to a shell unquoted, for a pipe or a redirection; read only when the tool has `.shell_command()` |

An input with none of these is not put on the command line; the tool reads it
through an expression or a staged file. A `boolean` with a flag writes the
flag when the value is true and nothing when it is false. `required=False`
makes the type optional (`['null', 'int']`), and an optional input with no
value writes nothing. `default=` is the value used when none is given.

The rest of an input is description and file handling, each a keyword or a
chained method that returns a changed copy: `.label('...')`, `.doc('...')`,
`.format('edam:format_2572')` for a file format, `.secondary_files(...)` for
the files that travel with a `File`, `.load_contents(True)` to read the first
64 KiB into `contents`, `.load_listing('deep_listing')` for a `Directory`,
`.streamable(True)`. Names are Python identifiers; a name that starts with
`_` or is a method of the collection (`keys`) is `ValueError`.

## Outputs

An output says what the tool produced and where the runner finds it:

```python
outputs = Outputs(
    table=Output(cwl.file, glob='results/summary.csv'),
    plots=Output(cwl.array(cwl.file), glob='*.png'),
    count=Output(cwl.int, glob='count.txt', load_contents=True,
                 output_eval='$(parseInt(self[0].contents))'),
    maybe=Output(cwl.file, glob='optional.txt', required=False),
    log=Output.stderr(),
)
```

- `glob` is matched in the tool's output directory after it exits; a
  pattern, a name, or an expression such as `$(inputs.out.basename)`.
- A `File` output whose glob matches nothing fails the step, and so does one
  that matches more than one file. `required=False` makes no match a `null`;
  `cwl.array(cwl.file)` collects every match.
- `load_contents=True` reads the first 64 KiB of each match into `contents`,
  and `output_eval` turns the matches (`self`) into the output's value, which
  is how a tool returns a number or a string read from a file.
- `Output.stdout()` and `Output.stderr()` are the captured streams. Name the
  files with `.stdout('name')` and `.stderr('name')` on the tool; without a
  name the runner picks one. At a workflow boundary both are `File`.

## The command

```python
tool = (
    CommandLineTool('sort_reads', inputs, outputs)
    .base_command('samtools', 'sort')
    .argument('$(inputs.reads.nameroot).sorted.bam', prefix='-o', position=1)
    .argument('--verbose')
    .stdin('$(inputs.reads.path)')
)
```

`base_command(*parts)` is the program and the words that always follow it;
it replaces an earlier one. `argument(value, position=, prefix=, ...)` adds a
word that comes from no input, with the same binding keywords as an input; a
bare `argument('--verbose')` is the word itself. `stdin(expression)` names
the file read on standard input. The command line is assembled by CWL's rule:
the base command, then every argument and bound input sorted by `position`,
ties in declaration order.

## Requirements

Each requirement is one method. The common ones:

```python
tool = (
    tool
    .docker('quay.io/biocontainers/samtools:1.21--h50ea8bc_0')
    .resources(cores=4, ram=16000)
    .env_var('OMP_NUM_THREADS', '$(runtime.cores)')
    .network_access(True)
    .time_limit(3600)
    .stage(inputs.reads, writable=True)
)
```

| Method | Writes | Notes |
|---|---|---|
| `.docker(image, ...)` | `DockerRequirement` | `dockerPull`; `docker_image_id=` for an image that exists only locally; `docker_load=`, `docker_file=`, `docker_import=`, `docker_output_directory=` |
| `.resources(cores=, ram=, tmpdir=, outdir=)` | `ResourceRequirement` | the `_min` fields; `cores_max=`, `ram_max=`, ... for maxima; memory and disk in mebibytes |
| `.gpu(cuda_version_min=, compute_capability=, device_count_min=)` | `cwltool:CUDARequirement` | a hint by default, and declares the `cwltool` namespace; runners other than cwltool ignore a hint they do not know |
| `.env_var(name, value)` | `EnvVarRequirement` | one variable per call; the tool sees no other |
| `.network_access(True)` | `NetworkAccess` | a v1.2 tool has no network unless it says so |
| `.time_limit(seconds)` | `ToolTimeLimit` | the runner stops the tool and fails the step |
| `.stage(inputs.name, writable=, entryname=)` | `InitialWorkDirRequirement` entry | put the input in the working directory, writable or not |
| `.initial_workdir(listing)` | `InitialWorkDirRequirement` | the whole listing, replacing staged entries |
| `.shell_command()` | `ShellCommandRequirement` | run the command line through a shell, so that `shell_quote=False` arguments can pipe and redirect |
| `.inline_javascript(*expression_lib)` | `InlineJavascriptRequirement` | added for you when any field holds `$(` or `${`; call it only for an `expressionLib` |
| `.schema_definitions(*types)` | `SchemaDefRequirement` | named records and enums |
| `.software([...])`, `.load_listing(...)`, `.work_reuse(...)`, `.inplace_update(...)` | the requirement of that name | |

Every one takes `as_hint=True` to write the entry under `hints`, which a
runner may ignore, instead of `requirements`, which it must honour or refuse
the tool. `.gpu()` and `.inplace_update()` are hints by default. A class
added twice replaces the earlier entry.

## Description and metadata

`.describe(label, doc)`, or `.label()` and `.doc()` separately, title and
describe the tool; the same methods on an `Input` or `Output` describe a
port. `.edam()` declares the `edam` prefix under `$namespaces` and the EDAM
ontology under `$schemas`, for `format` values such as `edam:format_2572`;
`.namespace(prefix, iri)` and `.schema(iri)` declare others, and
`.intent('edam:operation_3443')` records the operation the tool performs.
`.success_codes(0, 1)`, `.temporary_fail_codes(...)` and
`.permanent_fail_codes(...)` say which exit codes mean what.

## Escape hatches

CWL is larger than the builder. Four calls write what no method covers, as
given:

- `.requirement(class_or_object, fields)` and `.hint(...)`: any requirement
  class, by name with its fields (`'cwltool:CUDARequirement', {...}`), as a
  requirement object (`DockerRequirement(docker_pull=...)`), or as a raw
  mapping with a `class` key;
- `.argument(value, binding_extra={...}, extra={...})` and
  `Input(..., binding_extra={...}, extra={...})`: further binding fields and
  further fields of the entry;
- `.extra(**fields)`: further top-level fields of the document, such as
  `s:author`;
- `.add_argument({...})`: a raw `CommandLineBinding` mapping.

A raw mapping is checked for a `class` key or a `$`-prefixed key, which it may
not set, and written with a `UserWarning`, since nothing checks it until
`validate()`. Whatever you write this way is yours to get right; the next
step is to validate.

## Validation

```python
result = tool.validate()
tool.write_cwl('tool.cwl', validate=True)
```

`validate()` loads the document with cwltool, as a runner would, and raises
`ToolBuilderValidationError` naming the field when the document is not a
valid CWL v1.2 `CommandLineTool`; it writes nothing. `write_cwl(path,
validate=True)` writes the file first and validates the file. The ontologies
under `$schemas` are not fetched, as a local run does not fetch them. A
`cwltool:CUDARequirement` written as a requirement rather than a hint is
rejected here, since cwltool reads its extensions only with `--enable-ext`,
which Sophios passes when it runs; keep it a hint.

To Python and to `validate()` an expression is text: `$(inputs.read)` for an
input named `reads` is found by the runner, when the tool runs. Validate at
the tool boundary, then compile the workflow; a mistake in the tool is
reported against the tool, not against the step that runs it.

## Into a workflow

```python
from sophios.api.python.workflow import Step, Workflow

touch = Step(clt_path='cwl_adapters/touch.cwl')
touch.inputs.filename = 'greeting.txt'
append = Step(clt_path='cwl_adapters/append.cwl')
append.inputs.str = 'Hello, Sophios'
count = word_count.to_step()

flow = Workflow([touch, append, count], 'counted')
flow.outputs.counts = count.outputs.counts
flow.run()
```

`Step(tool)` and `tool.to_step()` make a step from the object; `to_step`
also takes `step_name=` and `step_inputs={...}`. The step's inputs and outputs
are the tool's, by name, and inference connects it like any other step: here
`count.text` takes `append`'s file. The compiled workflow carries the tool
under `autogenerated/`, as it carries every tool. The run pulls
`debian:bookworm-slim` with the other images and writes `counts.txt` into the
output directory.

A tool built this way is CWL v1.2; the other tools of a workflow may be
older, since the compiled workflow is v1.2 whatever its tools declare.
