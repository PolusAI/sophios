cwlVersion: v1.2
class: CommandLineTool
doc: A stderr written through the link the input is staged under, into the input.
baseCommand: 'true'
stderr: $(inputs.source.basename)/keep.txt
inputs:
  source: Directory
outputs: {}
