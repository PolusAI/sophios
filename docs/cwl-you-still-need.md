# The CWL you still need

What does a tool author still have to know about CWL when the builder writes
the document? The builder writes the YAML; it does not change what the YAML
means. The runner runs the tool by CWL's rules, and a tool that ignores them
fails when it runs, not when it is validated. This page is those rules, one
concept per section, each with the builder call that writes it. The
[CWL v1.2 CommandLineTool specification](https://www.commonwl.org/v1.2/CommandLineTool.html)
is the full story.

## Expressions

A string that holds `$(...)` or `${...}` is evaluated by the runner when the
tool runs. Three spellings:

- `$(inputs.reads.nameroot)` is a parameter reference. It sees `inputs`,
  `self` (the value being bound; in `outputEval`, the matched files) and
  `runtime` (`runtime.cores`, `runtime.ram`, `runtime.outdir`,
  `runtime.tmpdir`).
- `$(self * 1024)` is a JavaScript expression.
- `${ return self[0].size > 0; }` is the body of a JavaScript function.

JavaScript needs an `InlineJavascriptRequirement`, which the builder adds
when any field holds `$(` or `${`, and Node.js on the machine that runs the
tool. To Python and to `validate()` an expression is text; a reference to an
input that does not exist is found by the runner.

```python
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl

inputs = Inputs(
    reads=Input(cwl.file, position=2),
    memory_gb=Input(cwl.int, flag='-m', value_from='$(self * 1024)M', default=2),
)
outputs = Outputs(
    sorted_reads=Output(cwl.file, glob='$(inputs.reads.nameroot).sorted.bam'),
)
sort = (
    CommandLineTool('sort_reads', inputs, outputs)
    .base_command('samtools', 'sort')
    .argument('$(inputs.reads.nameroot).sorted.bam', prefix='-o', position=1)
)
```

In a workflow, Sophios adds the requirement itself for a step's `when` and
for a record whose `valueFrom` is an expression.

## Where outputs come from

When the tool exits, the runner collects each output from the tool's output
directory, which is also its working directory:

- `glob` is matched there, and only there. A file the tool writes elsewhere,
  such as `/tmp/result.csv`, is never collected.
- A `File` output that matches nothing fails the step, and so does one that
  matches more than one file. `required=False` makes no match a `null`;
  `cwl.array(cwl.file)` collects every match, in filename order, not in the
  order the tool wrote them.
- `load_contents=True` reads the first 64 KiB of each match into `contents`,
  and `output_eval` turns the matches (`self`) into the value. An `outputEval`
  that reads `self[0]` fails when nothing matched; under
  [partial failures](partial-failures.md) that is the common case, so write
  `if (!self[0]) return null;` first.
- `Output.stdout()` and `Output.stderr()` are the captured streams; name the
  files with `.stdout(...)` and `.stderr(...)`.

```python
outputs = Outputs(
    table=Output(cwl.file, glob='results/summary.csv'),
    plots=Output(cwl.array(cwl.file), glob='*.png'),
    count=Output(cwl.int, glob='count.txt', load_contents=True,
                 output_eval='$(parseInt(self[0].contents))'),
    log=Output.stderr(),
)
report = CommandLineTool('report', Inputs(), outputs).base_command('make_report').stderr('report.log')
```

## File objects

A `File` value is a mapping, not a path. It has `class: File` and a
`location`; the runner fills in `path`, `basename`, `nameroot`, `nameext`,
`dirname`, `size` and `checksum`, which expressions read. In a workflow a
`File` literal is that mapping, and a relative `location` is read from where
it was written: the `.wic` file's directory, the inputs file's, or the
working directory in Python ([Inputs and values](inputs-and-values.md)).

- The runner stages each input file read-only, in a directory of its
  choosing, and the tool's working directory is empty. `.stage(inputs.x)`
  puts the input in the working directory under its own name, and
  `writable=True` lets the tool change it there; `append.cwl` works this way,
  since it writes to its input.
- `secondaryFiles` travel with the file: `.bai` names `sample.bam.bai`, and
  `^.bai` replaces the extension, `sample.bai`. On an input they are required
  unless `secondary_file('.bai', required=False)`; without `sample.bam.bai`,
  a run of the workflow below stops before the tool starts.
- `format` names the file format with an ontology term, such as
  `edam:format_2572` for BAM; `.edam()` declares the prefix. A file given to
  an input that declares a format must carry one too, with the same name, or
  the runner refuses it. Formats also steer [inference](edge-inference.md):
  an input with a format accepts only an output whose format matches.

```python
from pathlib import Path

from sophios.api.python.tool_builder import secondary_file
from sophios.api.python.workflow import Step, Workflow

index_stats = (
    CommandLineTool(
        'index_stats',
        Inputs(bam=Input(cwl.file, position=1).format('edam:format_2572').secondary_files(secondary_file('.bai'))),
        Outputs(stats=Output.stdout()),
    )
    .base_command('samtools', 'idxstats')
    .stdout('$(inputs.bam.nameroot).idxstats.txt')
    .edam()
)
stats = Step(index_stats)
stats.inputs.bam = {'class': 'File', 'location': str(Path('sample.bam').resolve()), 'format': 'edam:format_2572'}
compiled = Workflow([stats], 'bam_stats').compile()
```

A `Directory` value is the same with `class: Directory`; `load_listing`
decides how much of it the tool sees (`no_listing`, `shallow_listing`,
`deep_listing`).

## Types

| CWL | Builder | Type string |
|---|---|---|
| `int`, `string`, `File`, `Directory`, ... | `cwl.int`, `cwl.string`, `cwl.file`, `cwl.directory`, ... | `'int'`, `'File'` |
| optional | `required=False`, `cwl.optional(t)` | `'int?'` |
| array | `cwl.array(t)`, `Input.array(t, ...)` | `'File[]'` |
| enum | `cwl.enum('a', 'b')`, `Input.enum('a', 'b', ...)` | |
| record | `cwl.record(Fields(...))`, `Input.record(...)` | |
| any value | | `'Any'` |

```python
from sophios.api.python.tool_builder import Field, Fields

region = cwl.record(Fields(chrom=Field(cwl.string), start=Field(cwl.int), end=Field(cwl.int)), name='Region')
inputs = Inputs(
    mode=Input.enum('fast', 'sensitive', flag='--mode'),
    regions=Input(cwl.array(region), required=False),
    label=Input('string?', flag='--label'),
)
```

Types decide what a workflow may connect: Sophios rejects an edge between
types it can prove disjoint and leaves the rest to the runner, and inference
connects only an output whose type equals the input's or shares a union with
it. `Any` is connected only to `Any`. Give outputs the exact type and format
of what the tool writes, and bind an `Any` input explicitly.
[Types and records](types-and-records.md) has the rules.

## Containers

`.docker(image)` writes `dockerPull`. Before a run, Sophios pulls every
`dockerPull` image afresh, so the image must be in a registry; an image built
only on your machine fails that pull. For such an image write `dockerImageId`
alone, `.docker(docker_image_id='my-tool:dev')`: the pull skips it, and the
runner uses the local image as it is. A tool that ships its image, with
`dockerLoad` (a file or an http(s) URL) or `dockerImport` with a
`dockerImageId`, has it loaded into the engine before the run; a `dockerFile`
is built by cwltool at run time. With `--container_engine podman` the pull is
done by podman, with `singularity` as a Singularity image.

The runner starts the container with the tool's command line as the arguments
of the image's `ENTRYPOINT`, when the image has one. Give the full command in
`.base_command(...)` and use images without an entrypoint; a tool that works
only through an entrypoint cannot run outside its container.

`.gpu(...)` writes cwltool's `cwltool:CUDARequirement`, as a hint. As a
requirement (`as_hint=False`) `validate()` rejects it, since cwltool loads
its extensions only with `--enable-ext`, which Sophios passes when it runs
cwltool; other runners skip a hint they do not know.

```python
sort.docker('quay.io/biocontainers/samtools:1.21--h50ea8bc_0')
report.docker(docker_image_id='make-report:dev').gpu(cuda_version_min='11.7', device_count_min=1)
```

## Resources, environment, network, time

```python
(
    sort.resources(cores=4, ram=16000)
    .env_var('OMP_NUM_THREADS', '$(runtime.cores)')
    .network_access(True)
    .time_limit(3600)
)
```

- `.resources()` writes a `ResourceRequirement`. `ram`, `tmpdir` and `outdir`
  are in mebibytes; `$(runtime.cores)` and `$(runtime.ram)` read what the
  runner granted, which may be more than the minimum asked for.
- The tool does not see your shell's environment. cwltool sets `HOME` and
  `TMPDIR`, and `PATH` outside a container; anything else goes in
  `.env_var()`.
- A builder tool is CWL v1.2, where a tool has no network unless it declares
  `NetworkAccess`: in a container, cwltool runs it with `--net=none`. Call
  `.network_access(True)` for a tool that downloads.
- `.time_limit(seconds)` writes a `ToolTimeLimit`; the runner stops the tool
  when the time is up, and the step fails.

## Shell features

A CWL command line is not a shell: there is no pipe, no redirection, no glob
expansion, and every argument is quoted. A tool that needs one declares
`ShellCommandRequirement`, which makes the runner hand the command line to
`/bin/sh`, and marks the argument it wants unquoted with `shellQuote: false`.
Without the requirement, `shellQuote: false` is not read and the argument is
quoted like any other.

```python
first_lines = (
    CommandLineTool('first_lines',
                    Inputs(reads=Input(cwl.file)),
                    Outputs(head=Output.stdout()))
    .shell_command()
    .argument("bash -o pipefail -c 'gzip -dc \"$(inputs.reads.path)\" | head -n 2'", shell_quote=False)
    .stdout('head.txt')
)
```

The input `reads` has no binding of its own: the expression puts its path
where the pipeline needs it, and nothing else puts it on the command line.
The whole pipeline is one unquoted argument, run by `bash` with `pipefail`,
so that a failure anywhere in it fails the step; `gzip -dc` fails on a file
that is not gzipped, and without `pipefail` the step would succeed with an
empty `head.txt`, since the status of a pipeline is otherwise its last
command's. Given a gzipped file of three lines, `head.txt` holds the first
two. Exit codes other than `0` fail the step unless `.success_codes(...)`
lists them.

## Several sources into one input

A step input fed by several outputs is CWL's `WorkflowStepInput` with a list
`source`, written as a [record](types-and-records.md): `StepInput` in Python,
`!cwl {...}` in `.wic`. The tool's side of it is the type: an input that takes
several files is a `File[]`, and `linkMerge: merge_flattened` concatenates
arrays into it while `merge_nested`, the default, makes a list with one entry
per source. `pickValue` chooses among the outputs of conditional steps, which
give `null` when they do not run.

## What the runner reports

The compiled CWL renames each step `<workflow>__step__<n>__<id>`, and a port
exposed as a workflow input or output `<renamed step>___<port>`. The runner
knows only these names. Sophios writes `autogenerated/<workflow>.names.json`,
which maps each one back to what you wrote, and during a run cwltool's
messages name each step both ways. On a machine without `samtools`, a run of
the `bam_stats` workflow above ends with:

```text
ERROR 'samtools' not found: [Errno 2] No such file or directory: 'samtools'
ERROR [step 1 'index_stats' (bam_stats__step__1__index_stats)] Output is missing expected field ...
Failure! step 1 'index_stats' (bam_stats__step__1__index_stats) could not run, or its outputs could not be collected. Its own messages are above, under its name.
```

`autogenerated/output_<workflow>.json` lists the run's outputs: a workflow
output you named under your name, and step outputs under their renamed form.
[Running workflows](running.md) lists everything a run writes.
