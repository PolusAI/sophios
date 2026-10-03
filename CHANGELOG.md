# Changelog

All notable changes to Sophios will be documented in this file.

## Unreleased

### Added

- `StepInput(source=, link_merge=, pick_value=, value_from=, default=, load_contents=, load_listing=)` binds a CWL step input whose sources are port objects.

### Breaking changes

- `Workflow.add_input()` is gone; declare an input by binding it: `workflow.inputs.name = ...` or `step.inputs.x = workflow.inputs.name`.
- `Output(from_input=...)` is gone; write the glob as a CWL expression, e.g. `glob="$(inputs.output.basename)"` for a File or Directory input, `glob="$(inputs.output)"` for a string one.
- `Step.bind_input()`, `Step.get_output()`, `Workflow.bind_input()`, `Workflow.bind_output()` and `Workflow.add_output()` are private. Use `step.inputs.x = ...`, `step.outputs.x`, `workflow.outputs.x = ...`.
- `Workflow.write_wic()` writes a self-contained bundle (the root `.wic`, each nested workflow's `.wic` and each tool's `.cwl`) and no longer takes `inline_subworkflows`; `to_wic_yaml()` likewise; the inline `subtree:` form is gone.
- A written workflow output names its authored step (`step/port`), and a step named apart from its tool carries `run: <stem>.cwl`; two different tools sharing one file stem in a workflow are rejected.
- (since 0.6.0) `Fields.to_list()` is gone and `SecondaryFile.to_dict()` returns a mapping: the tool builder renders through cwl_utils.
- (since 0.6.0) `sophios.api.rest` and `sophios.api.utils` moved to `sophios.contrib.rest` and `sophios.contrib.converter` / `sophios.contrib.ict`.
- (since 0.6.0) library code raises `SophiosError` instead of calling `sys.exit(1)`; `!&` is legal only on an `out:` entry; a sequence step carries its name in `id:`; `wic021` folded into `wic006`.
