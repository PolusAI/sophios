cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
requirements: {InlineJavascriptRequirement: {}}
inputs:
  source: File
  reference: {type: File, format: $(inputs.source.format)}
  body: {type: File, format: '${ return inputs.source.format; }'}
outputs: {}
