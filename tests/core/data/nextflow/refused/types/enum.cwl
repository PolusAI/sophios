cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs:
  mode:
    type: {type: enum, name: Mode, symbols: [fast, slow]}
    inputBinding: {position: 1}
outputs: {}
