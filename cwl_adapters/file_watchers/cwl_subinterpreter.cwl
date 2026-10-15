#!/usr/bin/env cwl-runner
cwlVersion: v1.0

class: CommandLineTool

label: Declares a real-time analysis, which Sophios runs beside the workflow as the files it watches change and once the workflow ends.

doc: |-
  Declares a real-time analysis. This tool is never run and never emitted: Sophios takes
  a step that uses it out of the workflow at compile time, compiles the analysis
  (cwl_tool, configured by config) once, and with --run_local runs it on the host
  each time a file matching file_pattern changes in a step's output directory, when
  that step finishes, and once more when the workflow ends, at most max_times in all.
  Every input is a literal (!ii). See docs/advanced.md, Real-time analysis.

baseCommand: 'false'

inputs:
  file_pattern:
    label: A glob, matched against the names of the files the workflow's steps write, i.e. '*prod.trr'
    type: string

  cwl_tool:
    label: The analysis to run, a registered tool (by name) or a .wic workflow (with its extension)
    type: string

  max_times:
    label: The most times the analysis runs, including the final run when the workflow ends
    type: int

  interval:
    label: The fewest seconds between two runs triggered by a watched file changing (default 60)
    type: int?

  config:
    label: |-
      For a .wic analysis its `wic: steps:` entries, for a tool the step's own keys
      such as `in:`. Every `in:` value is a literal, and a file is named by its basename.
    type: Any

outputs: []
