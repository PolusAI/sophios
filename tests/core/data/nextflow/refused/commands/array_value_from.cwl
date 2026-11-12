cwlVersion: v1.2
class: CommandLineTool
doc: An array input bound with a valueFrom.
baseCommand: echo
inputs:
  xs: {type: 'string[]', inputBinding: {valueFrom: $(inputs.xs)}}
outputs: {}
