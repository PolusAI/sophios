cwlVersion: v1.2
class: CommandLineTool
doc: An entry staged under a literal name, and referenced in the command by its input, which names the input.
requirements:
  InitialWorkDirRequirement:
    listing: [{entry: $(inputs.source), entryname: renamed.txt}]
baseCommand: cat
arguments: [$(inputs.source)]
stdout: $(inputs.source)
inputs:
  source: File
outputs: {}
