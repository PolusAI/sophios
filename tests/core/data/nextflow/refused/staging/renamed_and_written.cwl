cwlVersion: v1.2
class: CommandLineTool
doc: An entry staged under a literal name, which stdout names by its input; the rename alone is refused.
requirements:
  InitialWorkDirRequirement: {listing: [{entry: $(inputs.f), entryname: x.txt}]}
baseCommand: 'true'
stdout: $(inputs.f)
inputs:
  f: File
outputs:
  result: {type: File, outputBinding: {glob: o}}
