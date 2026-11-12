cwlVersion: v1.2
class: CommandLineTool
doc: Stage the source as renamed.txt and do nothing; the pattern matches the entry.
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.source)
      entryname: renamed.txt
baseCommand: 'true'
inputs:
  source: File
outputs:
  wild:
    type: File
    outputBinding: {glob: renamed*}
