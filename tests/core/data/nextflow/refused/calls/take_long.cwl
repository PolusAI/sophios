cwlVersion: v1.2
class: CommandLineTool
doc: Echo a long.
baseCommand: echo
inputs:
  n: {type: long, inputBinding: {position: 1}}
outputs: {}
