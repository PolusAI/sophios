cwlVersion: v1.2
class: CommandLineTool
doc: An entry linked under its own basename, read as stdin by that basename.
requirements:
  InitialWorkDirRequirement: {listing: [$(inputs.f)]}
baseCommand: cat
stdin: $(inputs.f.basename)
inputs:
  f: File
outputs: {}
