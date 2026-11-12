cwlVersion: v1.2
class: CommandLineTool
doc: A boolean valueFrom on an optional input, which would write its absence.
baseCommand: echo
inputs:
  b: boolean
  s: {type: 'string?', inputBinding: {valueFrom: $(inputs.b), prefix: --flag}}
outputs: {}
