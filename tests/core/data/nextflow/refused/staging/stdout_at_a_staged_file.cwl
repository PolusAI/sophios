cwlVersion: v1.2
class: CommandLineTool
doc: A stdout written through the link the input is staged under, into the input.
baseCommand: 'true'
stdout: $(inputs.source.basename)
inputs:
  source: File
outputs: {}
