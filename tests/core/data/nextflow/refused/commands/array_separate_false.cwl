cwlVersion: v1.2
class: CommandLineTool
doc: An array input bound with separate false.
baseCommand: echo
inputs:
  xs: {type: 'string[]', inputBinding: {position: 1, separate: false}}
outputs: {}
