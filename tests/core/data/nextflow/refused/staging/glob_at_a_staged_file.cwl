cwlVersion: v1.2
class: CommandLineTool
doc: An output globbed at the name the staged file is linked under, or inside it.
baseCommand: 'true'
inputs:
  source: File
outputs:
  result: {type: File, outputBinding: {glob: ' $(inputs.source.basename) '}}
