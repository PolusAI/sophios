cwlVersion: v1.2
class: CommandLineTool
doc: An output collected by a list of globs.
baseCommand: 'true'
inputs:
  f: File
outputs:
  result: {type: File, outputBinding: {glob: [$(inputs.f)]}}
