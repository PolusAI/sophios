cwlVersion: v1.2
class: CommandLineTool
doc: An output globbed at the name the staged directory is linked under, or inside it.
baseCommand: 'true'
inputs:
  source: Directory
outputs:
  result: {type: File, outputBinding: {glob: '$(inputs.source.basename)/keep.txt'}}
