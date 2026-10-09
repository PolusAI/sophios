# Changelog

All notable changes to Sophios will be documented in this file.

Each release's generated notes list what changed. Where a release breaks
something that worked in the one before it, an upgrade section beneath its
notes says what to change in your files and code.

## 0.7.0: upgrading from 0.6.0

Codes in parentheses are the codes Sophios prints with each diagnostic.

### `.wic` files

- A literal must already have its input's type; nothing is converted any more
  (wic020). Write `!ii 2` rather than `!ii 2.9` on an `int` input, `!ii true`
  rather than `!ii yes` on a `boolean` input, `!ii 7` rather than `!ii '007'`
  on an `int` input, and `!ii 1.0e-5` rather than `!ii 1e-5` (YAML 1.1 reads
  the latter as text). A `string` input still takes any scalar, and an `int`
  still feeds a `float`.
- A boolean on a `string` input reaches the tool as `true` or `false`, where it
  was `True` or `False`. A tool that compares against `True` must compare
  against `true`.
- A scalar literal on an input listed in `scatter:` is an error (wic020); it
  used to be wrapped into a one-element list, so the scatter ran once. Write
  a list, or take the input out of `scatter:`.
- A mapping in `in:` made only of CWL step-input fields (`source`, `default`,
  `valueFrom`, `linkMerge`, ...) is reported (wic038); it used to be sent to
  the tool as a literal object. Tag it `!ii` if the tool should receive that
  mapping, or write it as a `!cwl` step-input record if it binds the input.
- An output with `linkMerge`, `pickValue` or a list `outputSource` is reported
  (wic038). Point `outputSource` at one `step/port`.
- `!&` belongs on outputs only (wic019). It never compiled on an input; move it
  to the producing step's `out:` and consume it with `!*`.
- A step written as a single-key entry in a sequence (`- touch: {...}`) is
  reported (wic006); it never compiled. Write `- id: touch`, or use the
  mapping form `steps: {touch: {...}}`.
- A document the YAML loader cannot load (`!!int abc`, `!!str [a]`, an
  impossible date such as `2020-13-45`, an unknown `!!` tag such as `!!foo: 1`,
  `k: !!foo 1` or a verbatim `!<tag:...>`, a `<<` that merges a scalar) is
  reported (wic009) at the loader's position, with the loader's own message,
  as an unknown `!` tag is; it used to pass the parser or crash it. Correct the
  value at that position, or quote the text if it was meant literally.
- An `!ii` scalar is read as YAML reads the same text written plain, so
  `!ii 2020-13-45` is reported (wic009) as `2020-13-45` is; it used to become
  the text `2020-13-45`. Quote the text if it was meant literally.
- A Sophios tag used as a mapping key (`!ii a: b`) is reported (wic005): a key
  is a name. Put the tag on the value: `a: !ii b`.
- `<<` merge keys apply everywhere, as YAML defines them: in `!ii` values, in
  `in:` and in plain CWL passed through, which used to carry a literal `<<` key
  or make an input named `<<`. A merged mapping keeps the key order the YAML
  loader gives it, so steps merged into `steps:` run in that order. To keep a
  key named `<<`, quote it: `"<<": value`.
- A step whose run is a CWL Workflow (a top-level `class: Workflow`, or a packed
  `$graph` whose `main` is a Workflow) is refused (wic013), whether it is a
  stem on `search_paths_cwl`, a `run:` path or an inline `run:` body. It used
  to compile with exit 0 and then fail in cwltool, or fail with wic028. Write
  it as a `.wic` subworkflow and call that, or run a CWL Workflow file on its
  own with `--allow_raw_cwl`. In Python, a `Step` whose `clt_path` is a CWL
  Workflow is refused the same way: build it as a nested `Workflow` of `Step`s.
- A `cwl_subinterpreter` step declares a real-time analysis, which Sophios
  runs beside the workflow under `--run_local` (see Real-time Analysis in
  `docs/advanced.md`); it is no longer emitted as a CWL step. Every input is a
  literal, and a malformed declaration is reported (wic044). The adapter's
  `cachedir_path`, `root_workflow_yml_path` and `homedir` inputs and its
  `output_log_path` output are gone: remove a binding of those inputs and any
  edge from `output_log_path`. `max_times` is an `int`; a string integer such
  as `'20'` is still accepted. From Python, a `.wic` analysis is found through
  `Workflow.run(workflow_paths=...)`, shaped like `Workflow.from_wic`'s.

### Command line

- The runner's default output directory is named after the workflow too,
  `outdir_<runner>_<workflow>_<timestamp>`, like the provenance, summary and
  job-store paths beside it. Two different workflows started in the same
  second from one directory no longer share it, where cwltool could fail with
  `File exists` or merge their outputs. Two runs of the same workflow started
  in the same second from one directory still share every path the run writes.
- Diagnostics are printed on stderr, each with its `file:line:col` and code, and
  so are the compile-failure banners. A script that read them from stdout must
  read stderr.
- A working directory where `autogenerated/` cannot be created or written is
  reported as `wic021`, naming the directory, before anything is compiled; it
  used to be a `PermissionError` traceback.
- A `--yaml` or `--inputs_file` that names no file is a usage error: one line
  and exit code 2, as a missing `--config_file` already was. It used to be a
  `FileNotFoundError` traceback.
- An unexpected failure is one line: what stopped, and the
  `error_<workflow>.txt` that now holds the whole traceback (it used to be
  written without its stack frames). If the line does not say what to change,
  report it with that file.
- An unrecognised argument exits with code 2. Pass `--passthrough_flags yes` to
  hand unknown flags to the runner; `--passthrough_flags` accepts only `yes` or
  `no`.
- A flag is never read as an abbreviation of a longer one: write each flag in
  full (`--inputs_file`, not `--inputs`). A runner flag such as `--validate`
  now reaches the runner instead of being taken for `--validate_plugins`.
- `--run_local` exits with the runner's exit code when the run fails; it used
  to exit 0. A script that checked the output instead of the exit code can
  check the exit code.
- `--quiet` is passed to cwltool only when you give it.
- `--cachedir` is passed to cwltool when you give it, so cwltool caches each
  step there and reuses a step whose tool and inputs are unchanged. It used to
  be accepted and ignored, and its default, `cachedir`, is now unset: a run
  caches only when you ask, or in `cachedir/` when it runs a real-time
  analysis. Give `--cachedir cachedir` to keep the old directory.
- A relative `location` or `path` of a File or Directory in `--inputs_file`
  is read from the directory of the inputs file, at any depth, as CWL v1.2
  section 5.1.5 says. Only a top-level `location` was rewritten before, and it
  was read from the working directory, so a job file that sat elsewhere
  worked. If you keep an inputs file outside the directory you run from and
  wrote its paths relative to that directory, make them relative to the file,
  or absolute.
- Before a run, Sophios checks that each File and Directory a job names
  exists, is the right kind and is readable (wic016). An inputs file that is
  not a mapping of input names to values is reported (wic002). It also checks
  that it can create and write the directories the run writes to:
  `autogenerated/` or `Workflow.run()`'s `basepath`, and `--outdir` or
  `run_args_dict={"outdir": ...}` (wic021).
- A `!ii` File or Directory read from beside the workflow is run where it is:
  the job names it by its absolute path, and nothing is copied. A File used to
  be copied into `autogenerated/`, however large, and a Directory was not, so a
  tool that read it failed.
- The container engine is checked only when a step runs in a container, with
  `<engine> info` rather than a hello-world container. A workflow with no
  container runs without an engine.
- Before a local run, a podman run pulls its images with podman; it used to pull
  them with docker. A tool whose image comes from `dockerLoad` or `dockerImport`
  now has it loaded before the run; the run never loaded it before. An image
  that cannot be pulled, loaded or imported is reported (wic037) instead of
  ending in a traceback.
- `--write_intermediate_wic` is gone.
- `--ignore_validation_errors` is still accepted but does nothing beyond a
  warning: there is no separate validation pass left to ignore. Remove it.
- `--generate_schemas` writes only the language schema,
  `autogenerated/schemas/wic.json`. Per-tool and per-workflow schemas, and
  `schema_store.json`, are no longer written: point an editor at `wic.json`.
- `--cwl_inline_subworkflows` flattens subworkflow calls into the root again,
  and `--cwl_inline_runtag` embeds each tool; pass both for one self-contained
  file. A call that carries anything besides `in` and `out` (such as
  `scatter:` or `when:`), or whose workflow says `wic: {inlineable: false}`,
  stays nested and is named on stderr.
- The `cwl_subinterpreter` command is gone; `--run_local` runs a declared
  real-time analysis itself.

### Python API and embedding

- `Workflow.run()` raises `WorkflowRunError` (api005, importable from
  `sophios.api.python.workflow`) when the run fails, instead of returning.
  Catch it where you used to inspect the result.
- Library code raises `SophiosError` (`sophios.lang.diagnostics`) instead of
  calling `sys.exit(1)`, so a bad workflow no longer ends the calling process.
  Catch `SophiosError` where you caught `SystemExit`. The CLI's exit codes are
  unchanged.
- A numpy `int`, `float32` or `bool` passed as a literal is no longer
  converted; pass `int(...)`, `float(...)` or `bool(...)`.
- Contrib modules moved under `sophios.contrib`:
  `sophios.api.utils.converter` is `sophios.contrib.converter`,
  `sophios.api.utils.wfb_util` is `sophios.contrib.wfb_util`,
  `sophios.api.utils.ict.*` is `sophios.contrib.ict.*`, and
  `sophios.api.rest.api` is `sophios.contrib.rest.api`. The REST API's HTTP
  behaviour is unchanged.
- The modules `sophios.ast`, `sophios.inference`, `sophios.inlineing` and
  `sophios.schemas` are gone with the compiler they belonged to. Compile
  through the CLI or `sophios.api.python`.
- Ports are bound and read through their objects. `Workflow.add_input()` is
  gone; declare an input by binding it: `workflow.inputs.name = ...` or
  `step.inputs.x = workflow.inputs.name`.
- `Step.bind_input()`, `Step.get_output()`, `Workflow.bind_input()`,
  `Workflow.bind_output()` and `Workflow.add_output()` are private. Use
  `step.inputs.x = ...`, `step.outputs.x` and `workflow.outputs.x = ...`.
- `Output(from_input=...)` is gone; write the glob as a CWL expression, e.g.
  `glob="$(inputs.output.basename)"` for a File or Directory input,
  `glob="$(inputs.output)"` for a string one.
- `Fields.to_list()` is gone and `SecondaryFile.to_dict()` returns a mapping:
  the tool builder renders through cwl_utils.
- `Workflow.write_wic()` writes a self-contained bundle (the root `.wic`, each
  nested workflow's `.wic` and each tool's `.cwl`) and no longer takes
  `inline_subworkflows`. The inline `subtree:` form is gone. Drop the
  argument and read the nested workflows from their own files.
- `Workflow.to_wic_yaml()` and the `Workflow.yaml` property are gone:
  `Workflow.write_wic()` is the one way to write `.wic` from Python. Call it
  and read the file it writes.
- A written workflow output names its authored step (`step/port`), and every
  step carries `run: <stem>.cwl`. Two different tools sharing one file stem in
  a workflow are rejected; rename one tool's file. So is a different
  `<stem>.cwl` already in the target directory, which `write_wic` used to
  overwrite silently; a file holding the same tool is left untouched. A tool's
  relative imports such as `$import` are not copied.
- Several sources, `linkMerge`, `pickValue`, `valueFrom`, `default`,
  `loadContents` and `loadListing` are bound with `StepInput(source=,
  link_merge=, pick_value=, value_from=, default=, load_contents=,
  load_listing=)`, whose sources are port objects. A list of ports, or a port
  anywhere inside a bound list or mapping, raises `InvalidInputValueError`
  where it is bound; it used to reach the compiler as a literal and fail with
  wic020. Wrap the ports in `StepInput(source=[...])`.
- `Step(path, config_path=...)` is `Step(path, step_inputs_file=...)`. Its
  values are bound as written: a File or Directory in it is no longer checked
  when the step is built (api001), and a local run reports a missing one
  before it starts (wic016), reading a relative path from the working
  directory. `Step.from_cwl_document(config=...)` and
  `CommandLineTool.to_step(config=...)` are `step_inputs=...`, bound the same
  way.
- `Workflow.run(user_env_vars=...)` passes each value to the runner exactly as
  given; `$`, `!`, `&`, quotes, parentheses and newlines used to be stripped
  from it. Write a value as the runner should see it. A key that is not an
  environment variable name raises `ValueError` before the run starts, where
  it used to be dropped with a warning; rename or remove it.
- `compile_source` takes the bundle and the compiler options only: drop
  `yaml_tag_paths`, the graph settings and `graph_target`.
  `sophios.cli.default_compilation_settings()` returns the compiler options, and
  `get_dicts_for_compilation()` returns `(compiler_options, graph_settings)`.
  The compiler no longer draws: `CompilationArtifact.graph_view` and
  `sophios.utils_graphs` are gone, and `sophios.drawing.draw(result, bundle,
  graph_settings, name)` draws a compiled workflow. The
  `sophios.cwl_subinterpreter` module is gone.
- `sophios.utils_yaml.wic_loader()` and its `WicLoader` are gone. Read a
  `.wic` with `sophios.lang.parse` or `Workflow.from_wic`.

<!-- Breaking changes merged before 0.7.0 is released add their upgrade steps
     to the section above that they belong to. -->

### Packaging

- The `runners-src` extra is gone; PyPI does not accept a dependency on a Git
  URL. Install `cwl-utils` from Git yourself if you need an unreleased
  version.
- cwltool 3.1.20241217163858 or newer is required. From that release cwltool
  names a singularity image with no tag as `cwl-docker-extract` pulls it
  (`<name>_latest`); with an older cwltool, a singularity run of such an
  image cannot find it. `pip install -U cwltool` if pip reports a conflict.

### New diagnostics that do not fail a compile

These print on stderr and change no output; nothing needs to change.

- When inference picks between equally good producers it says so: wic042 when
  one producer offered several matching outputs, wic043 when an earlier
  producer also matched. `--inference_strict` makes both errors; pin the edge
  with `!&`/`!*` to choose explicitly.
- A generated step name in `outputSource`, a `when:` that reads a generated or
  undeclared input, a `wic: steps:` key or a `wic: graphviz: ranksame` entry
  that addresses no step, and a call left nested by `--cwl_inline_subworkflows`
  are each named on stderr.
