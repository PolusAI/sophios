# Command-line reference

What does each flag of the `sophios` command do? This page lists the flags,
grouped by what they affect. A flag is spelled with underscores
(`--run_local`); abbreviations are not accepted, and an unknown flag is an
error unless `--passthrough_flags yes` sends it to the runner.

```text
sophios --yaml WORKFLOW (--generate_cwl_workflow | --check | --run_local | --generate_run_script) [options]
sophios --generate_config | --generate_schemas | --explain CODE | --version | --help
```

## What to do

With `--yaml`, exactly one of `--generate_cwl_workflow`, `--check`, `--run_local` and `--generate_run_script`.
Without one of them the command compiles and then asks for one. Under `--target nextflow` the modes are `--check`,
`--run_local` and `--generate_run_script`, and with none of them the command writes the four
[Nextflow](nextflow.md) files. `--generate_config`, `--generate_schemas`, `--explain` and `--version` take no `--yaml`.

## Flags

<!-- cli-flag-tables -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | done; for `--check`, nothing is in the way of a run |
| `1` | a reported problem (its diagnostics are on stderr), a failure Sophios did not expect (one line on stderr, the traceback in `error_<workflow>.txt`), or no mode flag was given |
| `2` | a command-line error: an unknown flag, a missing file, a code `--explain` does not have |
| the runner's | a run that ended with that exit code |
| `130` | interrupted with Ctrl-C |

## The same options from Python

`Workflow.run()`, and `Workflow.start()` and `Workflow.run_async()` with the same arguments
([Follow a run from Python](running.md#follow-a-run-from-python)), take the running options as values: the runner
is `runner=Cwltool(...)`, `Toil(...)` or `Nextflow(...)`, whose positional arguments reach the runner as they
are, the outputs folder is `config=RunConfig(folder)`, and the rest is `inputs_file=` and `generate_run_script=`.

| Flag | Python |
|---|---|
| `--cwl_runner` | the runner: `Cwltool` or `Toil` |
| `--container_engine`, `--pull_dir` | the runner's `container_engine=`, `pull_dir=` |
| `--cachedir` | `Cwltool(cachedir=)`; Toil has none |
| `--no_provenance` | the runner's `provenance=False` |
| `--docker_remove_entrypoints` | the runner's `remove_entrypoints=True` |
| `--ignore_docker_install`, `--ignore_docker_processes` | the runner's `ignore_install=`, `ignore_processes=` |
| `--passthrough_flags yes` and the flags | the runner's positional arguments |
| `--outdir` | `config=RunConfig(folder)`, a relative folder read from its `relative_to`, else from the current directory |
| `--inputs_file`, `--generate_run_script` | `inputs_file=`, `generate_run_script=` |
| `--quiet` | `Cwltool(quiet=)`; the command line is not quiet unless asked, Python is; Toil has none |

`Nextflow` takes its flags and `container_engine=`, `docker` or `singularity`, and `inputs_file=` is refused with
`target='nextflow'`; so is a runner the target does not have. Each runner takes `container_engine='docker'` unless
told otherwise, as the command line does. `Workflow.compile()`
takes `target=`, `lang_version=`, `tool_registry=`, `inference=` and `policy=`, and nothing else.
The partial-failure flags map onto `RunPolicy(partial_failure=, success_codes=)`
([Partial failures](partial-failures.md#in-python)), and the inference flags onto `Inference(enabled=, strict=, naming_conventions=, insert_steps=)`, and the
configuration file's `renaming_conventions` and `inference_rules` onto its `renamings=` and `format_rules=`, where Python starts from the three renamings Sophios ships and has no format rules unless passed, as it reads no configuration file;
`Workflow.run()`, `start()` and `run_async()` take the same `inference=` and `policy=`, and `target='nextflow'` to run the
[Nextflow](nextflow.md#running-on-the-nextflow-target) pipeline instead, where an option only a CWL run reads is refused.
The rewriting options are command-line only;
`Workflow.draw()` takes the drawing ones ([Drawing](drawing.md)).
