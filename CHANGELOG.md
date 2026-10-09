# Changelog

All notable changes to Sophios will be documented in this file.

## Unreleased

### Added

- `StepInput(source=, link_merge=, pick_value=, value_from=, default=, load_contents=, load_listing=)` binds a CWL step input whose sources are port objects; a list of ports bound directly is an error.
- `Workflow.from_wic(path, tool_registry=, workflow_paths=)` builds `Workflow` and `Step` objects from a `.wic` file; what the Python API cannot express is refused with `api006`.

### Breaking changes

- `Workflow.add_input()` is gone; declare it with `workflow.inputs.name.as_type(...)`, or reference it with `step.inputs.x = workflow.inputs.name`.
- `Output(from_input=...)` is gone; write the glob as a CWL expression, e.g. `glob="$(inputs.output.basename)"` for a File or Directory input, `glob="$(inputs.output)"` for a string one.
- `Step.bind_input()`, `Step.get_output()`, `Workflow.bind_input()`, `Workflow.bind_output()` and `Workflow.add_output()` are private. Use `step.inputs.x = ...`, `step.outputs.x`, `workflow.outputs.x = ...`.
- `Workflow.write_wic()` writes a self-contained bundle (the root `.wic`, each nested workflow's `.wic` and each tool's `.cwl`) and no longer takes `inline_subworkflows`; the inline `subtree:` form is gone.
- `Workflow.to_wic_yaml()` and `Workflow.yaml` are gone: `Workflow.write_wic()` is the one way to write `.wic` from Python; read the file it writes.
- A written workflow output names its authored step (`step/port`), and every step carries `run: <stem>.cwl`; two different tools sharing one file stem in a workflow are rejected, and so is a different `<stem>.cwl` already in the target directory, which `write_wic` used to overwrite silently (a file holding the same tool is left untouched, and a tool's relative imports such as `$import` are not copied).
- (since 0.6.0) `Fields.to_list()` is gone and `SecondaryFile.to_dict()` returns a mapping: the tool builder renders through cwl_utils.
- (since 0.6.0) `sophios.api.rest` and `sophios.api.utils` moved to `sophios.contrib.rest` and `sophios.contrib.converter` / `sophios.contrib.ict`.
- (since 0.6.0) library code raises `SophiosError` instead of calling `sys.exit(1)`; `!&` is legal only on an `out:` entry; a sequence step carries its name in `id:`; `wic021` folded into `wic006`.
- `<<` merge keys apply everywhere, as YAML defines them: in `!ii` values, in `in:` and in plain CWL passed through, which used to carry a literal `<<` key or make an input named `<<`. A merged mapping keeps the key order the YAML loader gives it, so steps merged into `steps:` run in that order. To keep a key named `<<`, quote it: `"<<": value`.

### Fixed

- A document the YAML loader cannot load (`!!int abc`, `!!str [a]`, an impossible date such as `2020-13-45`, an unknown `!!` tag, a `<<` that merges a scalar) is reported as `wic009` at the loader's position, with the loader's own message. The parser used to accept it or crash on it. Correct the value at that position.
- A Sophios tag used as a mapping key (`!ii a: b`) is `wic009`; put the tag on the value: `a: !ii b`.
