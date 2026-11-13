cwlVersion: v1.2
class: CommandLineTool
doc: Stage the source under its own basename and do nothing; both globs match the entry.
requirements:
  InitialWorkDirRequirement:
    listing:
    - $(inputs.source)
baseCommand: 'true'
inputs:
  source: File
outputs:
  wild:
    type: File
    outputBinding: {glob: lines*}
  same:
    type: File
    outputBinding: {glob: $(inputs.source.basename)}
