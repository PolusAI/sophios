cwlVersion: v1.2
class: CommandLineTool
doc: Print n, which is 5 unless a step binds it.
baseCommand: echo
inputs:
  n: {type: int, default: 5, inputBinding: {position: 1}}
  m: int
outputs: {}
